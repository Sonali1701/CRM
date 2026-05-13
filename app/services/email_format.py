"""Format helpers shared by all email send paths (Graph + SMTP).

Reps type messages in a textarea — line breaks must survive into the
delivered email. Both Graph and SMTP send content-type HTML by default,
and HTML collapses whitespace, so plain-text bodies arrive as a single
paragraph. This helper converts plain text to HTML-safe markup while
leaving real HTML alone."""

from __future__ import annotations

import html as html_lib
import re


_HTML_TAG_RE = re.compile(
    r"<(?:p|br|div|span|a|b|strong|i|em|u|ul|ol|li|h[1-6]|table|tr|td|th|blockquote|hr|img|pre|code)\b",
    re.IGNORECASE,
)


def text_to_html(body: str | None) -> str:
    """Return HTML for the given body.

    - Empty input → empty string.
    - Body that already contains HTML tags is returned as-is.
    - Plain text is HTML-escaped and \\n is replaced with <br>\\n.
    """
    if not body:
        return ""
    if _HTML_TAG_RE.search(body):
        return body
    escaped = html_lib.escape(body)
    return escaped.replace("\r\n", "\n").replace("\n", "<br>\n")
