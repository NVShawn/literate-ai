"""Build a Claude/Codex plugin bundle from the checked-in skill catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from literate_ai.version import DISTRIBUTION_VERSION


def build_plugin_bundle(*, repository: Path, output: Path) -> dict[str, object]:
    """Copy root SKILL.md and skills/agent into one disposable plugin tree."""

    repository = repository.resolve()
    if output.is_symlink():
        raise ValueError("plugin output must not be a symlink")
    output = output.resolve()
    if (
        output == repository
        or repository.is_relative_to(output)
        or output.is_relative_to(repository / "skills")
    ):
        raise ValueError("plugin output must not replace repository authority")
    if output.exists():
        if not (output / ".claude-plugin" / "plugin.json").is_file():
            raise ValueError(
                "refusing to replace output not owned by the plugin builder"
            )
        shutil.rmtree(output)
    output.mkdir(parents=True)
    manifest_source = repository / ".claude-plugin" / "plugin.json"
    manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    manifest["version"] = DISTRIBUTION_VERSION
    (output / ".claude-plugin").mkdir()
    (output / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy2(repository / "SKILL.md", output / "SKILL.md")
    shutil.copytree(repository / "skills" / "agent", output / "skills" / "agent")
    copied = sorted(
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    )
    return {
        "schema": "literate-ai/plugin-bundle@1",
        "version": DISTRIBUTION_VERSION,
        "output": str(output),
        "files": copied,
    }


def build_release_plugins(*, repository: Path, output: Path) -> dict[str, object]:
    """Project one canonical catalog into deterministic provider archives."""
    from literate_ai.contracts import canonical_identity
    from literate_ai.release_files import file_identity

    repository = repository.resolve(strict=True)
    output = output.absolute()
    if (
        output.is_symlink()
        or output.resolve() == repository
        or repository.is_relative_to(output.resolve())
    ):
        raise ValueError("plugin output must not replace repository authority")
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z", "SKILL.md", "PROJECT.md", "skills/agent", "docs"],
            cwd=repository,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    content: dict[str, bytes] = {}
    for relative in sorted(filter(None, tracked)):
        if relative.startswith("docs/") and not relative.endswith(".md"):
            continue
        path = repository / relative
        if path.is_symlink() or any(
            parent.is_symlink()
            for parent in path.parents
            if parent != repository.parent
        ):
            raise ValueError(f"plugin authority must not be a symlink: {relative}")
        content[relative] = path.read_bytes()
    if "SKILL.md" not in content or "skills/agent/SKILL.md" not in content:
        raise ValueError("plugin authority is incomplete")
    inventory = [
        {"path": path, "identity": "sha256:" + hashlib.sha256(data).hexdigest()}
        for path, data in sorted(content.items())
    ]
    manifest = {
        "schema": "literate-ai/coding-cli-plugin-bundle@1",
        "version": DISTRIBUTION_VERSION,
        "source_revision": revision,
        "catalog": inventory,
        "providers": {
            "codex": "supported",
            "claude": "supported",
            "cursor": "unsupported: no release adapter qualified",
            "opencode": "unsupported: no release adapter qualified",
        },
    }
    manifest["identity"] = canonical_identity(manifest).uri
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for provider in ("codex", "claude"):
        provider_manifest: dict[str, object] = {
            "name": "literate-ai",
            "version": DISTRIBUTION_VERSION,
            "description": "Specification-led development and release engineering.",
            "author": {"name": "Literate AI maintainers"},
            "skills": "./skills/",
        }
        if provider == "codex":
            provider_manifest["interface"] = {
                "displayName": "Literate AI",
                "shortDescription": "Specification-led software lifecycle",
                "longDescription": "Create projects and prepare verified releases.",
                "developerName": "Literate AI maintainers",
                "category": "Productivity",
                "capabilities": ["Read", "Write"],
                "defaultPrompt": ["Help me create or adopt a Literate AI project."],
            }
        files = {
            "literate-ai/skills/literate-ai/" + path: data
            for path, data in content.items()
        }
        files[f"literate-ai/.{provider}-plugin/plugin.json"] = (
            json.dumps(provider_manifest, sort_keys=True, indent=2) + "\n"
        ).encode()
        files["literate-ai/bundle.json"] = (
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        ).encode()
        archive = output / f"literate_ai-{DISTRIBUTION_VERSION}-{provider}-plugin.zip"
        if archive.is_symlink():
            raise ValueError("plugin archive destination cannot be a symlink")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
            for name, data in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        validate_release_plugin(archive, provider=provider, revision=revision)
        artifacts.append(
            {
                "role": f"{provider}-plugin",
                "path": str(archive),
                "size": archive.stat().st_size,
                "identity": file_identity(archive),
            }
        )
    return {"manifest": manifest, "artifacts": artifacts}


def validate_release_plugin(archive: Path, *, provider: str, revision: str) -> None:
    """Verify archive inventory, canonical inputs and provider discovery metadata."""
    from literate_ai.contracts import canonical_identity

    if provider not in {"codex", "claude"}:
        raise ValueError("unsupported plugin adapter")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError("plugin archive has duplicate paths")
        manifest = json.loads(bundle.read("literate-ai/bundle.json"))
        if set(manifest) != {
            "schema",
            "version",
            "source_revision",
            "catalog",
            "providers",
            "identity",
        }:
            raise ValueError("plugin bundle manifest has missing or unknown fields")
        body = {key: value for key, value in manifest.items() if key != "identity"}
        if (
            manifest["schema"] != "literate-ai/coding-cli-plugin-bundle@1"
            or manifest["source_revision"] != revision
            or manifest["version"] != DISTRIBUTION_VERSION
            or manifest["identity"] != canonical_identity(body).uri
        ):
            raise ValueError("plugin bundle source, version or identity mismatch")
        if manifest["providers"].get(provider) != "supported":
            raise ValueError("plugin provider support is not declared")
        manifest_path = f"literate-ai/.{provider}-plugin/plugin.json"
        metadata = json.loads(bundle.read(manifest_path))
        if (
            metadata.get("name") != "literate-ai"
            or metadata.get("version") != DISTRIBUTION_VERSION
            or metadata.get("skills") != "./skills/"
            or "mcpServers" in metadata
            or "hooks" in metadata
        ):
            raise ValueError("plugin discovery metadata is invalid")
        expected = {manifest_path, "literate-ai/bundle.json"}
        for item in manifest["catalog"]:
            if set(item) != {"path", "identity"}:
                raise ValueError("plugin catalog item has unknown fields")
            path = item["path"]
            if (
                path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or "\\" in path
            ):
                raise ValueError("plugin catalog path is unsafe")
            name = "literate-ai/skills/literate-ai/" + path
            if name in expected:
                raise ValueError("plugin catalog repeats a path")
            expected.add(name)
            if (
                "sha256:" + hashlib.sha256(bundle.read(name)).hexdigest()
                != item["identity"]
            ):
                raise ValueError("plugin catalog content differs from its identity")
        if set(names) != expected:
            raise ValueError("plugin archive has missing or undeclared files")
        if "literate-ai/skills/literate-ai/SKILL.md" not in expected:
            raise ValueError("plugin has no discoverable root skill")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--release", action="store_true", help="build deterministic provider archives"
    )
    arguments = parser.parse_args(argv)
    builder = build_release_plugins if arguments.release else build_plugin_bundle
    result = builder(repository=arguments.repository, output=arguments.output)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
