from __future__ import annotations

import re

import nh3
from markdown_it import MarkdownIt

README_MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])

# Server-side allowlist sanitization is defense-in-depth: the client re-sanitizes
# content_html before injecting it, but a third-party README is untrusted, so we
# never emit raw HTML from render() to the API. Tags/attributes/URL schemes are
# limited to what a README legitimately needs.
_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "img",
    "kbd",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "p": {"align"},
    "img": {"src", "alt", "title", "width", "height"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class", "align"},
    "details": {"open"},
    "ol": {"start"},
    "li": {"value"},
    # markdown-it renders table column alignment as inline `text-align` styles;
    # allow the attribute but constrain its value in the filter below.
    "td": {"style", "colspan", "rowspan"},
    "th": {"style", "colspan", "rowspan"},
}
_ALLOWED_URL_SCHEMES = {"http", "https"}
# Only the fixed set of alignment declarations GitHub-flavored tables produce;
# anything else (e.g. background:url(...) beacons) is dropped.
_SAFE_STYLE_RE = re.compile(r"^text-align:\s*(left|right|center)$")
_SAFE_LANGUAGE_CLASS_RE = re.compile(r"^language-[A-Za-z0-9_+-]+$")
_INTEGER_ATTRIBUTE_LIMITS = {
    ("img", "width"): (16, 1200),
    ("img", "height"): (16, 1200),
    ("td", "colspan"): (1, 20),
    ("th", "colspan"): (1, 20),
    ("td", "rowspan"): (1, 100),
    ("th", "rowspan"): (1, 100),
    ("ol", "start"): (1, 100_000),
    ("li", "value"): (1, 100_000),
}


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute == "style":
        return value if _SAFE_STYLE_RE.match(value.strip().rstrip(";")) else None
    if attribute == "align":
        normalized = value.strip().lower()
        return normalized if normalized in {"left", "center", "right"} else None
    if attribute == "class" and tag == "code":
        return value if _SAFE_LANGUAGE_CLASS_RE.fullmatch(value.strip()) else None
    if attribute == "open":
        return "" if tag == "details" else None
    limits = _INTEGER_ATTRIBUTE_LIMITS.get((tag, attribute))
    if limits is not None:
        try:
            number = int(value)
        except ValueError:
            return None
        return str(number) if limits[0] <= number <= limits[1] else None
    return value


def render_readme_html(content: str) -> str:
    html = README_MARKDOWN.render(content)
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        attribute_filter=_attribute_filter,
        link_rel="noopener noreferrer nofollow",
    )
