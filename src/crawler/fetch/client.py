"""Single HTTP client: proxy from env, retries with backoff, block classification, request log."""
import json
import os
import random
import threading
import time
from collections import Counter
import zlib
from dataclasses import dataclass
from urllib.parse import quote

from curl_cffi import requests as cffi

BLOCK_MARKERS = ("captcha", "are you a robot", "access denied", "unusual traffic",
                 "cf-challenge", "just a moment", "authwall", "verify you are human",
                 "security check")
RETRY_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504, 999}


def proxy_url() -> str | None:
    url = os.environ.get("PROXY_URL")
    if url:
        return url
    host = os.environ.get("PROXY_HOST")
    if not host:
        return None
    port = os.environ.get("PROXY_PORT")
    hostport = f"{host}:{port}" if port and ":" not in host else host
    user = quote(os.environ.get("PROXY_USERNAME", ""), safe="")
    pw = quote(os.environ.get("PROXY_PASSWORD", ""), safe="")
    return f"http://{user}:{pw}@{hostport}"


def wire_bytes(r) -> int:
    """Estimated proxy traffic: curl_cffi hands us decoded bodies, so re-compress when the
    server sent gzip/br, plus ~1.5KB for headers/TLS. Calibrate against the proxy dashboard."""
    body = r.content
    if r.headers.get("content-encoding") in ("gzip", "br", "deflate", "zstd"):
        return len(zlib.compress(body, 6)) + 1500
    return len(body) + 1500


@dataclass
class Response:
    url: str
    status: int
    text: str
    bytes: int
    blocked: bool
    block_reason: str | None


def classify(status: int, text: str, final_url: str) -> str | None:
    if status in (403, 429, 999):
        return f"http_{status}"
    if "authwall" in final_url or "/login" in final_url and "linkedin" in final_url:
        return "authwall_redirect"
    if status == 200:
        head = text[:4000].lower()
        for m in BLOCK_MARKERS:
            if m in head and len(text) < 30000:
                return f"marker:{m}"
    return None


class Client:
    def __init__(self, conn=None, run_id=None, max_attempts=4):
        self.conn, self.run_id, self.max_attempts = conn, run_id, max_attempts
        self.proxy = proxy_url()
        self.bytes_total = 0
        self.requests_total = 0
        self.requests_by_source = Counter()
        self.ok_by_source = Counter()          # 200s that were not classified as blocked
        self.fail_streak = Counter()           # consecutive failed/blocked requests per source
        self._lock = threading.Lock()          # counters + the shared request-log connection
        self._local = threading.local()        # curl_cffi sessions are per thread

    @property
    def sessions(self):
        if not hasattr(self._local, "sessions"):
            self._local.sessions = {}
        return self._local.sessions

    def _session(self, profile):
        if profile not in self.sessions:
            self.sessions[profile] = cffi.Session(
                impersonate=profile, timeout=45,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None)
        return self.sessions[profile]

    def _rotate(self, profile):
        """Drop the keep-alive connection so the rotating pool hands us a fresh exit IP."""
        sess = self.sessions.pop(profile, None)
        if sess is not None:
            sess.close()

    def _log(self, source, url, status, nbytes, blocked, reason, attempt):
        with self._lock:
            self.requests_total += 1
            self.requests_by_source[source] += 1
            if status == 200 and not blocked:
                self.ok_by_source[source] += 1
                self.fail_streak[source] = 0
            else:
                self.fail_streak[source] += 1
            self.bytes_total += nbytes or 0
            if self.conn is not None:
                self.conn.execute(
                    "INSERT INTO requests (run_id, source, url, status, bytes, blocked, block_reason, attempt)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (self.run_id, source, url, status, nbytes, blocked, reason, attempt))
                self.conn.commit()

    def get(self, url, source=None, headers=None, profile="chrome") -> Response:
        return self.request("GET", url, source=source, headers=headers, profile=profile)

    def post_json(self, url, payload, source=None, headers=None, profile="chrome") -> Response:
        return self.request("POST", url, source=source, headers=headers, profile=profile,
                            data=json.dumps(payload))

    def request(self, method, url, source=None, headers=None, profile="chrome", data=None) -> Response:
        """Request with bounded retries. 404/410 (and 400) are returned as-is (evidence), not retried."""
        last = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self._session(profile).request(method, url, headers=headers, data=data, allow_redirects=True)
                text = r.text
                reason = classify(r.status_code, text, str(r.url))
                nbytes = wire_bytes(r)
                self._log(source, url, r.status_code, nbytes, reason is not None, reason, attempt)
                last = Response(str(r.url), r.status_code, text, nbytes, reason is not None, reason)
                if reason is None and r.status_code not in RETRY_STATUSES:
                    return last
            except Exception as e:  # network/proxy error
                self._log(source, url, 0, 0, False, f"error:{type(e).__name__}", attempt)
                last = Response(url, 0, "", 0, False, f"error:{type(e).__name__}")
            self._rotate(profile)
            time.sleep(min(30, 2 ** attempt) * random.uniform(0.5, 1.5))
        return last
