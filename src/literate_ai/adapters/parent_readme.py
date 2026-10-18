"""Plan or reconcile parent attribution without changing authored README prose."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.adapters.repository_lineage import FilesystemRepositoryLineageStore
from literate_ai.adapters.repository_orchestration import _read_document

_BEGIN = "<!-- literate-ai:parent-repositories -->"
_END = "<!-- /literate-ai:parent-repositories -->"


class ParentReadmeError(ValueError):
    """Parent attribution cannot be resolved or safely reconciled."""


def browser_url(repository: str, override: str | None = None) -> str:
    """Translate a network clone locator; local identities need explicit evidence."""
    parsed = urlsplit(override or repository)
    if override is not None:
        if parsed.scheme not in {"http", "https"}:
            raise ParentReadmeError("browser URL must use HTTP or HTTPS")
    elif parsed.scheme in {"ssh", "git"}:
        host = parsed.hostname or ""
        parsed = parsed._replace(
            scheme="https", netloc=f"[{host}]" if ":" in host else host
        )
    elif parsed.scheme not in {"http", "https"}:
        raise ParentReadmeError(
            "parent has no network browser URL; provide --browser-url REPOSITORY=URL"
        )
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 32 for c in parsed.geturl())
        or any(c.isspace() or c in '<>\\"`{}|' for c in parsed.netloc)
    ):
        raise ParentReadmeError(
            "browser URL is absent, ambiguous or credential-bearing"
        )
    _ = parsed.port  # Reject malformed ports before emitting a Markdown target.
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path or path == "/":
        raise ParentReadmeError("browser URL must identify a repository")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, quote(path, safe="/%:@-._~"), "", "")
    )


def _read(root: Path) -> bytes | None:
    path = root / "README.md"
    if path_is_link_or_reparse(path):
        raise ParentReadmeError("README must be a direct regular file")
    try:
        return _read_document(
            path,
            label="README",
            error_suffix="parent_readme_invalid",
            maximum_bytes=1024 * 1024,
        )
    except FileNotFoundError:
        return None


def _visible_markdown(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    visible = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if marker:
            fence = marker[1]
        elif not line.startswith(("    ", "\t")):
            visible.append(line)
    return re.sub(r"(`+).*?\1", "", "".join(visible))


def _render(previous: bytes | None, urls: tuple[str, ...], title: str) -> bytes | None:
    text = previous.decode("utf-8") if previous is not None else ""
    if text.count(_BEGIN) != text.count(_END) or text.count(_BEGIN) > 1:
        raise ParentReadmeError("README parent markers are ambiguous")
    start = text.find(_BEGIN)
    end = text.find(_END) + len(_END) if start >= 0 else -1
    if start >= 0 and end <= start:
        raise ParentReadmeError("README parent markers are out of order")
    authored = _visible_markdown(text[:start] + text[end:] if start >= 0 else text)
    missing = tuple(
        url
        for url in urls
        if not re.search(
            r"\[[^\]\n]+\]\(<?" + re.escape(url) + r">?(?:\s+[\"\'][^\n]*[\"\'])?\)",
            authored,
        )
    )
    block = ""
    if missing:
        links = []
        for url in missing:
            label = urlsplit(url).netloc + urlsplit(url).path
            label = re.sub(r"([\\`*_{}\[\]<>])", r"\\\1", label)
            links.append(f"- Parent repository: [{label}](<{url}>)")
        block = _BEGIN + "\n" + "\n".join(links) + "\n" + _END
    if start >= 0:
        return (text[:start] + block + text[end:]).encode("utf-8")
    if not missing:
        return previous
    if previous is None:
        text = f"# {title}\n"
    separator = "\n" if text.endswith("\n") else "\n\n"
    return (text + separator + block + "\n").encode("utf-8")


def reconcile_parent_readme(
    root: Path, *, apply: bool = False, browser_urls: dict[str, str] | None = None
) -> dict:
    root = Path(root).resolve(strict=True)
    store = FilesystemRepositoryLineageStore(root)
    recorded = store.load_optional()
    parents = () if recorded is None else recorded[0].parents
    overrides = browser_urls or {}
    if set(overrides) - {p.repository_url for p in parents}:
        raise ParentReadmeError("browser override must name an actual direct parent")
    urls = tuple(
        dict.fromkeys(
            browser_url(p.repository_url, overrides.get(p.repository_url))
            for p in parents
        )
    )
    before = _read(root)
    after = _render(before, urls, root.name)
    changed = before != after
    if apply and changed:
        if store.load_optional() != recorded or _read(root) != before:
            raise ParentReadmeError("parent authority or README changed; plan again")
        path = root / "README.md"
        mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o644
        descriptor, temporary = tempfile.mkstemp(
            prefix=".litai-parent-readme-", dir=root
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(after if after is not None else b"")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name != "nt":
                os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "schema": "literate-ai/parent-readme@1",
        "changed": changed,
        "applied": apply and changed,
        "path": "README.md",
        "parent_urls": list(urls),
    }


def parse_browser_urls(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or key in overrides:
            raise ParentReadmeError(
                "browser overrides require unique REPOSITORY=URL values"
            )
        overrides[key] = value
    return overrides


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--browser-url", action="append", default=[], metavar="REPOSITORY=URL"
    )
    args = parser.parse_args()
    try:
        overrides = parse_browser_urls(args.browser_url)
        result = reconcile_parent_readme(
            args.project, apply=args.apply, browser_urls=overrides
        )
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not echo invalid locators or authored README contents.
        print(
            json.dumps(
                {
                    "schema": "literate-ai/parent-readme@1",
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc)
                    if isinstance(exc, ParentReadmeError)
                    else "parent authority or README is unavailable or invalid",
                }
            )
        )
        return 1
    print(json.dumps({"ok": True, **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
