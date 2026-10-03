#!/usr/bin/env python3
"""Preflight and publish the overview document pair to Google Workspace.

The script never persists a token. It requires the operator to name the expected
active gcloud account, checks both stable resources before the first mutation, updates
them in place, exports them back for verification, and writes a resumable release-bound
receipt beneath the centrally resolved object directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Mapping
from pathlib import Path
from xml.etree import ElementTree

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "src"))

from literate_ai.cache_directories import resolve_cache_directories  # noqa: E402
from literate_ai.contracts.identity import canonical_identity  # noqa: E402
from literate_ai.version import DISTRIBUTION_VERSION  # noqa: E402

HERE = Path(__file__).resolve().parent
SLIDES_ID = "1zGugAIHdxXNSDKJia9jak55_0J0LnpSq2dFt2F5OZpE"
DOC_ID = "1C6jtFrm9oAj6dg4CuLimylzu5HdaP6CovP2KY8U1HRA"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SLIDES_GOOGLE = "application/vnd.google-apps.presentation"
DOCS_GOOGLE = "application/vnd.google-apps.document"
DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"
PREFLIGHT_SCHEMA = "literate-ai/google-document-pair-preflight@1"
RECEIPT_SCHEMA = "literate-ai/release-document-pair-publication@1"


def _ssl() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _active_account(expected: str) -> str:
    if not expected.strip() or expected.strip() != expected:
        raise RuntimeError(
            "name the intended publication principal with --expected-account ACCOUNT"
        )
    try:
        result = subprocess.run(
            (
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "gcloud is unavailable; install it and authenticate the publication account"
        ) from exc
    accounts = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not accounts:
        raise RuntimeError(
            "no active gcloud account; run `gcloud auth login ACCOUNT`, then "
            "`gcloud config set account ACCOUNT`, and retry with "
            "`--expected-account ACCOUNT`"
        )
    if len(accounts) != 1:
        raise RuntimeError(
            "gcloud reported multiple active accounts; select exactly one with "
            "`gcloud config set account ACCOUNT`"
        )
    account = accounts[0]
    if account.casefold() != expected.casefold():
        raise RuntimeError(
            f"active gcloud account is {account!r}, expected {expected!r}; run "
            f"`gcloud config set account {expected}` before publication"
        )
    return account


def _source_revision() -> str:
    try:
        result = subprocess.run(
            ("git", "-C", str(REPOSITORY), "rev-parse", "--verify", "HEAD^{commit}"),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("cannot resolve the publication source revision") from exc
    revision = result.stdout.strip()
    if result.returncode or re.fullmatch(r"[0-9a-f]{40,64}", revision) is None:
        raise RuntimeError("cannot resolve the publication source revision")
    return revision


def _token() -> str:
    try:
        result = subprocess.run(
            ("gcloud", "auth", "print-access-token"),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("gcloud could not produce an access token") from exc
    if result.returncode:
        raise RuntimeError(
            (
                result.stderr
                or result.stdout
                or "gcloud auth print-access-token failed"
            ).strip()[:800]
        )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud returned an empty access token")
    return token


def _request(
    token: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"Authorization": f"Bearer {token}"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=data, method=method, headers=request_headers
    )
    try:
        with urllib.request.urlopen(request, context=_ssl()) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        detail = error.read()[:800]
        endpoint = url.split("?", 1)[0]
        raise RuntimeError(
            f"Drive API {method} {endpoint} failed: {error.code} {detail!r}"
        ) from None


def _json(token: str, method: str, url: str, payload: dict | None = None) -> dict:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=UTF-8"
    status, _response_headers, body = _request(
        token, method, url, headers=headers, data=data
    )
    if not body:
        return {"http_status": status}
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Drive API returned a non-object JSON response")
    parsed["http_status"] = status
    return parsed


def _permissions(token: str, file_id: str) -> list[dict[str, object]]:
    listed = _json(
        token,
        "GET",
        f"{DRIVE}/files/{urllib.parse.quote(file_id)}/permissions"
        "?fields=permissions(id,type,domain,role,allowFileDiscovery)"
        "&supportsAllDrives=true",
    )
    return [
        {
            "type": item.get("type"),
            "domain": item.get("domain"),
            "role": item.get("role"),
            "allowFileDiscovery": item.get("allowFileDiscovery"),
        }
        for item in (listed.get("permissions") or [])
        if isinstance(item, dict)
    ]


def _resource_preflight(
    token: str,
    *,
    member: str,
    file_id: str,
    expected_mime: str,
) -> dict[str, object]:
    metadata = _json(
        token,
        "GET",
        f"{DRIVE}/files/{urllib.parse.quote(file_id)}"
        "?fields=id,name,mimeType,parents,version,"
        "capabilities(canEdit,canDownload,canShare)&supportsAllDrives=true",
    )
    if metadata.get("id") != file_id or metadata.get("mimeType") != expected_mime:
        raise RuntimeError(
            f"{member} resource {file_id} is not the configured {expected_mime} file"
        )
    capabilities = metadata.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities.get("canEdit"):
        raise RuntimeError(
            f"authenticated principal cannot edit the configured {member} resource"
        )
    if not capabilities.get("canDownload"):
        raise RuntimeError(
            f"authenticated principal cannot export the configured {member} resource"
        )
    access = _permissions(token, file_id)
    has_org_reader = any(
        item.get("type") == "domain"
        and item.get("domain") == "nvidia.com"
        and item.get("role") == "reader"
        for item in access
    )
    if not has_org_reader and not capabilities.get("canShare"):
        raise RuntimeError(
            f"{member} lacks the required organization reader and cannot be shared"
        )
    return {
        "id": file_id,
        "name": metadata.get("name"),
        "mime_type": metadata.get("mimeType"),
        "version": metadata.get("version"),
        "parents": metadata.get("parents") or [],
        "capabilities": {
            "can_edit": True,
            "can_download": True,
            "can_share": bool(capabilities.get("canShare")),
        },
        "organization_reader_present": has_org_reader,
        "access": access,
    }


def preflight_document_pair(
    token: str,
    *,
    account: str,
    slides_id: str,
    doc_id: str,
) -> dict[str, object]:
    """Observe both stable resources before the caller may mutate either one."""

    presentation = _resource_preflight(
        token,
        member="presentation",
        file_id=slides_id,
        expected_mime=SLIDES_GOOGLE,
    )
    narrative = _resource_preflight(
        token,
        member="narrative",
        file_id=doc_id,
        expected_mime=DOCS_GOOGLE,
    )
    result: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "account": account,
        "resources": {
            "presentation": presentation,
            "narrative": narrative,
        },
        "ready": True,
    }
    result["identity"] = canonical_identity(result).uri
    return result


def _resumable_update(
    token: str, file_id: str, path: Path, source_mime: str
) -> dict[str, object]:
    meta_url = (
        f"{UPLOAD}/files/{urllib.parse.quote(file_id)}"
        "?uploadType=resumable&supportsAllDrives=true"
    )
    start_headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": source_mime,
        "X-Upload-Content-Length": str(path.stat().st_size),
    }
    _status, response_headers, _body = _request(
        token,
        "PATCH",
        meta_url,
        headers=start_headers,
        data=b"{}",
    )
    location = response_headers.get("Location") or response_headers.get("location")
    if not location:
        raise RuntimeError("Drive resumable update did not return a Location header")
    media = path.read_bytes()
    _status, _headers, body = _request(
        token,
        "PUT",
        location,
        headers={"Content-Type": source_mime, "Content-Length": str(len(media))},
        data=media,
    )
    if not body:
        return {}
    parsed = json.loads(body.decode("utf-8"))
    return dict(parsed) if isinstance(parsed, dict) else {}


def _ensure_org_reader(token: str, file_id: str) -> list[dict[str, object]]:
    access = _permissions(token, file_id)
    if not any(
        item.get("type") == "domain"
        and item.get("domain") == "nvidia.com"
        and item.get("role") == "reader"
        for item in access
    ):
        _json(
            token,
            "POST",
            f"{DRIVE}/files/{urllib.parse.quote(file_id)}/permissions"
            "?supportsAllDrives=true",
            {
                "type": "domain",
                "domain": "nvidia.com",
                "role": "reader",
                "allowFileDiscovery": False,
            },
        )
        access = _permissions(token, file_id)
    return access


def _export(token: str, file_id: str, *, kind: str, destination: Path) -> int:
    if kind == "pptx":
        url = (
            "https://docs.google.com/presentation/d/"
            f"{urllib.parse.quote(file_id)}/export/pptx"
        )
    elif kind == "docx":
        url = (
            f"{DRIVE}/files/{urllib.parse.quote(file_id)}/export?"
            + urllib.parse.urlencode({"mimeType": DOCX_MIME})
        )
    else:
        raise RuntimeError(f"unknown export kind {kind}")
    _status, _headers, body = _request(token, "GET", url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(body)
    return len(body)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pptx_pages(path: Path) -> tuple[int, int]:
    with zipfile.ZipFile(path) as archive:
        slides = [
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ]
        notes = [
            name
            for name in archive.namelist()
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
        ]
    return len(slides), len(notes)


def _docx_headings(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    count = 0
    for paragraph in root.iter(f"{w}p"):
        style = paragraph.find(f"{w}pPr/{w}pStyle")
        if style is not None and style.attrib.get(f"{w}val", "").lower().startswith(
            "heading"
        ):
            count += 1
    return count


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _load_resumable_receipt(
    path: Path, *, expected: Mapping[str, object]
) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if all(value.get(key) == item for key, item in expected.items()):
        return value
    return None


def publish_document_pair(
    *,
    token: str,
    preflight: dict[str, object],
    release_version: str,
    source_revision: str,
    account: str,
    slides_id: str,
    doc_id: str,
    pptx: Path,
    docx: Path,
    receipt_path: Path,
) -> dict[str, object]:
    """Update both members and retain resumable evidence after each mutation."""

    source = {
        "presentation": {
            "path": pptx.relative_to(REPOSITORY).as_posix(),
            "bytes": pptx.stat().st_size,
            "sha256": _sha256(pptx),
        },
        "narrative": {
            "path": docx.relative_to(REPOSITORY).as_posix(),
            "bytes": docx.stat().st_size,
            "sha256": _sha256(docx),
        },
    }
    expected: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "release_version": release_version,
        "source_revision": source_revision,
        "account": account,
        "source": source,
    }
    receipt = _load_resumable_receipt(receipt_path, expected=expected) or {
        **expected,
        "preflight": preflight,
        "members": {},
        "complete": False,
    }
    members = receipt.setdefault("members", {})
    if not isinstance(members, dict):
        members = {}
        receipt["members"] = members
    publication = (
        ("presentation", slides_id, pptx, PPTX_MIME),
        ("narrative", doc_id, docx, DOCX_MIME),
    )
    try:
        for member, file_id, path, mime in publication:
            prior = members.get(member)
            source_hash = source[member]["sha256"]
            if not (
                isinstance(prior, dict)
                and prior.get("id") == file_id
                and prior.get("source_sha256") == source_hash
                and prior.get("updated") is True
            ):
                updated = _resumable_update(token, file_id, path, mime)
                members[member] = {
                    "id": file_id,
                    "source_sha256": source_hash,
                    "updated": True,
                    "version": updated.get("version"),
                    "mime_type": updated.get("mimeType"),
                }
                _atomic_json(receipt_path, receipt)
            members[member]["access"] = _ensure_org_reader(token, file_id)
            _atomic_json(receipt_path, receipt)

        title = (
            f"Literate-AI — Application Foundry Narrative ({release_version} edition)"
        )
        renamed = _json(
            token,
            "PATCH",
            f"{DRIVE}/files/{urllib.parse.quote(doc_id)}?supportsAllDrives=true",
            {"name": title},
        )
        members["narrative"]["name"] = renamed.get("name") or title
        export_root = receipt_path.parent
        exported_pptx = export_root / "published-export.pptx"
        exported_docx = export_root / "published-export.docx"
        _export(token, slides_id, kind="pptx", destination=exported_pptx)
        _export(token, doc_id, kind="docx", destination=exported_docx)
        local_slides, local_notes = _pptx_pages(pptx)
        exported_slides, exported_notes = _pptx_pages(exported_pptx)
        local_headings = _docx_headings(docx)
        exported_headings = _docx_headings(exported_docx)
        if (exported_slides, exported_notes) != (local_slides, local_notes):
            raise RuntimeError(
                "exported Slides page or notes count does not match the local PPTX"
            )
        if exported_headings < 1:
            raise RuntimeError("exported Google Doc has no headings")
        receipt["exports"] = {
            "presentation": {
                "path": str(exported_pptx),
                "bytes": exported_pptx.stat().st_size,
                "sha256": _sha256(exported_pptx),
                "slides": exported_slides,
                "notes": exported_notes,
            },
            "narrative": {
                "path": str(exported_docx),
                "bytes": exported_docx.stat().st_size,
                "sha256": _sha256(exported_docx),
                "headings": exported_headings,
            },
        }
        receipt["local_structure"] = {
            "slides": local_slides,
            "notes": local_notes,
            "headings": local_headings,
        }
        receipt["urls"] = {
            "presentation": f"https://docs.google.com/presentation/d/{slides_id}/edit",
            "narrative": f"https://docs.google.com/document/d/{doc_id}/edit",
        }
        receipt["complete"] = True
        receipt.pop("identity", None)
        receipt["identity"] = canonical_identity(receipt).uri
        _atomic_json(receipt_path, receipt)
        return receipt
    except Exception:
        receipt["complete"] = False
        receipt.pop("identity", None)
        _atomic_json(receipt_path, receipt)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slides-id", default=SLIDES_ID)
    parser.add_argument("--doc-id", default=DOC_ID)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--authorize-external-write", action="store_true")
    parser.add_argument("--release-version", default=DISTRIBUTION_VERSION)
    parser.add_argument("--source-revision")
    parser.add_argument(
        "--pptx",
        type=Path,
        default=HERE / "literate-ai-manager-and-engineering-overview.pptx",
    )
    parser.add_argument(
        "--docx",
        type=Path,
        default=HERE / "literate-ai-manager-and-engineering-overview.docx",
    )
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args(argv)
    if not arguments.pptx.is_file() or not arguments.docx.is_file():
        parser.error("local PPTX and DOCX must both exist before publication")
    try:
        account = _active_account(arguments.expected_account)
        token = _token()
        preflight = preflight_document_pair(
            token,
            account=account,
            slides_id=arguments.slides_id,
            doc_id=arguments.doc_id,
        )
        if arguments.preflight_only:
            json.dump(preflight, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 0
        if not arguments.authorize_external_write:
            parser.error("publication requires --authorize-external-write")
        source_revision = arguments.source_revision or _source_revision()
        receipt = arguments.receipt
        if receipt is None:
            receipt = (
                resolve_cache_directories(REPOSITORY).obj_dir
                / "literate-ai-manager-overview"
                / "publish-receipt.json"
            )
        result = publish_document_pair(
            token=token,
            preflight=preflight,
            release_version=arguments.release_version,
            source_revision=source_revision,
            account=account,
            slides_id=arguments.slides_id,
            doc_id=arguments.doc_id,
            pptx=arguments.pptx.resolve(strict=True),
            docx=arguments.docx.resolve(strict=True),
            receipt_path=receipt,
        )
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
