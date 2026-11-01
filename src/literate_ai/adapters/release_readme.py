"""Read-only provider/default-branch checks for an explicitly managed README."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from literate_ai.adapters._processes import run_with_tree_kill

_POLICY_SCHEMA = "literate-ai/readme-latest-release-policy@1"
_MAX_BYTES = 2 * 1024 * 1024
_VERSION = re.compile(
    r"(?<![\w.])v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)
_CLAIM = re.compile(
    r"\b(?:latest|current)(?:\s+(?:(?:stable|published)\s+)?"
    r"(?:release|version|downloads?)\b|\s*:)"
    r"|\(\s*(?:latest|current)\s*\)"
    r"|^\s*#{1,6}\s+(?:latest|current)\s*$",
    re.I | re.M,
)
_RELEASE_LINK = re.compile(r"https://github\.com/[^\s)]+/releases/tag/([^\s)]+)", re.I)


class ReleaseReadmeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReadmeLatestReleasePolicy:
    marker: str
    include_prereleases: bool = False

    def __post_init__(self):
        if (
            not isinstance(self.marker, str)
            or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.marker) is None
        ):
            raise ValueError("README marker must be a bounded lowercase identifier")
        if type(self.include_prereleases) is not bool:
            raise ValueError("README include_prereleases must be boolean")

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "path", "marker", "include_prereleases"}
            or value["schema"] != _POLICY_SCHEMA
            or value["path"] != "README.md"
        ):
            raise ValueError(
                "README policy requires exact versioned fields and root README.md"
            )
        return cls(value["marker"], value["include_prereleases"])

    def to_dict(self):
        return {
            "schema": _POLICY_SCHEMA,
            "path": "README.md",
            "marker": self.marker,
            "include_prereleases": self.include_prereleases,
        }

    @property
    def begin(self):
        return f"<!-- {self.marker}:begin -->"

    @property
    def end(self):
        return f"<!-- {self.marker}:end -->"


def _github_json(root, endpoint, *, missing_ok=False):
    try:
        result = run_with_tree_kill(
            ["gh", "api", endpoint],
            cwd=root,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseReadmeError(
            "release.readme_unavailable", "GitHub README readback is unavailable"
        ) from exc
    if result.returncode:
        if missing_ok and b"HTTP 404" in result.stderr:
            return None
        raise ReleaseReadmeError(
            "release.readme_unavailable",
            "GitHub README/provider readback failed: "
            + result.stderr.decode("utf-8", errors="replace")[:1024],
        )
    if len(result.stdout) > _MAX_BYTES:
        raise ReleaseReadmeError(
            "release.readme_invalid", "GitHub readback exceeds the metadata limit"
        )
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as exc:
        raise ReleaseReadmeError(
            "release.readme_invalid", "GitHub readback must be a JSON object"
        ) from exc


def _asset_digest(root, repository, asset):
    digest = asset.get("digest")
    if digest is not None:
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise ReleaseReadmeError(
                "release.readme_provider_invalid",
                "published asset has a malformed SHA-256 digest",
            )
        return digest
    identity = asset.get("id")
    if type(identity) is not int or identity <= 0:
        raise ReleaseReadmeError(
            "release.readme_provider_invalid",
            "published asset without digest requires a provider asset ID",
        )
    try:
        with tempfile.TemporaryDirectory(prefix="litai-readme-asset-") as directory:
            if shutil.disk_usage(directory).free < asset["size"] + 1024 * 1024 * 1024:
                raise ReleaseReadmeError(
                    "release.readme_asset_unavailable",
                    "insufficient space to verify the published asset digest",
                )
            path = Path(directory) / "asset"
            with path.open("wb") as output:
                result = run_with_tree_kill(
                    [
                        "gh",
                        "api",
                        f"repos/{repository}/releases/assets/{identity}",
                        "--header",
                        "Accept: application/octet-stream",
                    ],
                    cwd=root,
                    stdout=output,
                    stderr=subprocess.PIPE,
                    timeout=300,
                    check=False,
                )
            if result.returncode or path.stat().st_size != asset["size"]:
                raise ReleaseReadmeError(
                    "release.readme_asset_mismatch",
                    "downloaded asset size differs from the published provider asset",
                )
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            return "sha256:" + hasher.hexdigest()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseReadmeError(
            "release.readme_asset_unavailable",
            "cannot download published bytes for README digest verification",
        ) from exc


def published_readme_release(root, repository, *, tag, prerelease, policy):
    preserve_stable = prerelease and not policy.include_prereleases
    suffix = "latest" if preserve_stable else "tags/" + quote(tag, safe="")
    release = _github_json(
        root, f"repos/{repository}/releases/{suffix}", missing_ok=preserve_stable
    )
    if release is None:
        return None
    actual_tag = release.get("tag_name")
    title = release.get("name")
    url = release.get("html_url")
    assets = release.get("assets")
    expected_prerelease = False if preserve_stable else prerelease
    if (
        release.get("draft") is not False
        or release.get("prerelease") is not expected_prerelease
        or not isinstance(actual_tag, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,319}", actual_tag) is None
        or (not preserve_stable and actual_tag != tag)
        or not isinstance(title, str)
        or not title.strip()
        or len(title) > 512
        or any(c in title for c in "\r\n\x00")
        or not isinstance(url, str)
        or not isinstance(assets, list)
        or len(assets) > 512
    ):
        raise ReleaseReadmeError(
            "release.readme_provider_invalid",
            "README verification requires the exact published release "
            "title, tag, state and assets",
        )
    expected_path = f"/{repository}/releases/tag/{actual_tag}"
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or unquote(parts.path) != expected_path
        or parts.query
        or parts.fragment
    ):
        raise ReleaseReadmeError(
            "release.readme_provider_invalid",
            "published release URL does not match its repository and tag",
        )
    observed = []
    names = set()
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseReadmeError(
                "release.readme_provider_invalid", "published asset must be an object"
            )
        name = asset.get("name")
        size = asset.get("size")
        download = asset.get("browser_download_url")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}", name) is None
            or name in names
            or type(size) is not int
            or size < 0
            or asset.get("state") != "uploaded"
            or not isinstance(download, str)
        ):
            raise ReleaseReadmeError(
                "release.readme_provider_invalid",
                "published asset name, size, state or download URL is invalid",
            )
        address = urlsplit(download)
        if (
            address.scheme != "https"
            or address.netloc.lower() != "github.com"
            or unquote(address.path)
            != f"/{repository}/releases/download/{actual_tag}/{name}"
            or address.query
            or address.fragment
        ):
            raise ReleaseReadmeError(
                "release.readme_provider_invalid",
                "published asset URL does not match its repository, tag and name",
            )
        names.add(name)
        observed.append(
            {
                "name": name,
                "size": size,
                "url": download,
                "digest": _asset_digest(root, repository, asset),
            }
        )
    return {
        "tag": actual_tag,
        "title": title,
        "url": url,
        "assets": sorted(observed, key=lambda item: item["name"]),
    }


def _markdown(value):
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", html.escape(value, quote=False))


def render_latest_release_section(policy, release):
    lines = [policy.begin, "## Latest release", ""]
    if release is None:
        lines.append("No stable release has been published.")
    else:
        lines.extend(
            [
                f"[{_markdown(release['title'])}]({release['url']}) "
                f"(`{release['tag']}`)",
                "",
                "| Asset | Bytes | SHA-256 |",
                "| --- | ---: | --- |",
            ]
        )
        for asset in release["assets"]:
            lines.append(
                f"| [{_markdown(asset['name'])}]({asset['url']}) | "
                f"{asset['size']} | `{asset['digest']}` |"
            )
    return "\n".join([*lines, policy.end])


def _stale(reason, *, expected, observed):
    detail = json.dumps(
        {"expected": expected, "observed": observed}, ensure_ascii=True, sort_keys=True
    )
    raise ReleaseReadmeError(
        "release.readme_latest_stale", reason + ": " + detail[:12000]
    )


def validate_latest_release_readme(content, *, policy, release):
    if not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_BYTES:
        raise ReleaseReadmeError(
            "release.readme_invalid", "README must be bounded UTF-8 text"
        )
    content = content.replace("\r\n", "\n")
    expected = render_latest_release_section(policy, release)
    if content.count(policy.begin) != 1 or content.count(policy.end) != 1:
        _stale(
            "README requires exactly one declared latest-release section",
            expected=expected,
            observed={
                "begin_markers": content.count(policy.begin),
                "end_markers": content.count(policy.end),
            },
        )
    start = content.index(policy.begin)
    end = content.index(policy.end) + len(policy.end)
    if end <= start:
        _stale(
            "README release markers are reversed",
            expected=expected,
            observed="reversed markers",
        )
    section = content[start:end]
    if section != expected:
        _stale(
            "README latest release differs from the published provider release",
            expected=expected,
            observed=section,
        )
    outside = content[:start] + content[end:]
    selected_tag = None if release is None else release["tag"]
    selected_versions = (
        set()
        if selected_tag is None
        else {m.group().removeprefix("v") for m in _VERSION.finditer(selected_tag)}
    )
    # Markdown headings retain scope across blank lines until a peer/parent
    # heading, so a stale link cannot hide in the next paragraph.
    claim_level = None
    for paragraph in re.split(r"\n\s*\n", outside):
        heading = re.match(r"^[ \t]*(#{1,6})[ \t]+", paragraph)
        if heading:
            level = len(heading.group(1))
            if claim_level is not None and level <= claim_level:
                claim_level = None
            if _CLAIM.search(paragraph):
                claim_level = level
        if claim_level is None and not _CLAIM.search(paragraph):
            continue
        versions = {m.group().removeprefix("v") for m in _VERSION.finditer(paragraph)}
        tags = {
            unquote(m.group(1)).rstrip(".,") for m in _RELEASE_LINK.finditer(paragraph)
        }
        if versions - selected_versions or tags - {selected_tag}:
            _stale(
                "README describes another release as latest/current",
                expected={"tag": selected_tag},
                observed=paragraph,
            )


def verify_remote_release_readme(root, repository, *, tag, prerelease, policy):
    release = published_readme_release(
        root, repository, tag=tag, prerelease=prerelease, policy=policy
    )
    metadata = _github_json(root, f"repos/{repository}")
    branch = metadata.get("default_branch")
    if (
        not isinstance(branch, str)
        or not branch
        or len(branch) > 255
        or any(c in branch for c in "\r\n\x00")
    ):
        raise ReleaseReadmeError(
            "release.readme_invalid", "repository does not identify its default branch"
        )
    reference = _github_json(
        root, f"repos/{repository}/git/ref/heads/{quote(branch, safe='/')}"
    )
    obj = reference.get("object")
    revision = obj.get("sha") if isinstance(obj, dict) else None
    if (
        not isinstance(revision, str)
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) is None
        or obj.get("type") != "commit"
    ):
        raise ReleaseReadmeError(
            "release.readme_invalid",
            "remote default branch does not identify a Git commit",
        )
    document = _github_json(
        root, f"repos/{repository}/contents/README.md?ref={revision}"
    )
    size = document.get("size")
    if (
        document.get("type") != "file"
        or document.get("encoding") != "base64"
        or type(size) is not int
        or not 0 <= size <= _MAX_BYTES
        or not isinstance(document.get("content"), str)
    ):
        raise ReleaseReadmeError(
            "release.readme_invalid",
            "remote root README must be a bounded regular UTF-8 file",
        )
    try:
        data = base64.b64decode("".join(document["content"].split()), validate=True)
        if len(data) != size:
            raise ValueError
        content = data.decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ReleaseReadmeError(
            "release.readme_invalid",
            "remote README bytes differ from their declared encoding or size",
        ) from exc
    validate_latest_release_readme(content, policy=policy, release=release)
