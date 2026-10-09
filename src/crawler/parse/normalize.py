import re

from selectolax.parser import HTMLParser

_BLOCK = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table|section)>|<br\s*/?>", re.I)


def html_to_text(html: str | None) -> str:
    """Plain text from HTML, keeping paragraph/list line breaks."""
    if not html:
        return ""
    html = re.sub(r"<li[^>]*>", "- ", html, flags=re.I)
    html = _BLOCK.sub(lambda m: m.group(0) + "\n", html)
    text = HTMLParser(html).text(separator="")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
