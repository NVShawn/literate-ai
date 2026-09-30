"""Hand-authored library surface whose HTML render is snapshot-accepted."""

from __future__ import annotations

TITLE = "Exact greeting"


def render() -> str:
    return (
        '<article data-stable="true"><h1>'
        f"{TITLE}"
        "</h1><p>library surface</p></article>\n"
    )
