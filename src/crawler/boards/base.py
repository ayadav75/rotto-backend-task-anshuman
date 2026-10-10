"""Board interface. Boards only fetch+parse; they never touch the database."""
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Card:
    """A listing seen in search results."""
    source_job_id: str
    url: str
    signal: str | None = None      # cheap change signal from the card (e.g. dateUpdated)
    expired: bool = False          # card itself says the listing is expired
    extra: dict = field(default_factory=dict)
    inline: "DetailResult | None" = None   # full content already delivered with the card (no detail fetch)


@dataclass
class DetailResult:
    kind: str                      # "job" | "expired" | "gone" | "failed" | "skip"
    job: dict | None = None
    reason: str | None = None      # why skipped/failed (e.g. "non-uk")
    permanent: bool = True         # for "skip": False = look at this listing again next run


class Board:
    name: str

    def cards(self, client, should_stop) -> Iterator[Card]:
        raise NotImplementedError

    def detail(self, client, card: Card) -> DetailResult:
        raise NotImplementedError

    def check(self, client, source_job_id: str, url: str) -> DetailResult:
        """Re-check a known listing (used for removal verification)."""
        return self.detail(client, Card(source_job_id, url))
