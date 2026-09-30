"""Qualify HTML through an installed wheel, without source imports or model calls.

The wheel harness invokes this file with its isolated interpreter and an existing
onboard-created project. The optional local parent fixture proves update and
staleness recovery, not browser acceptance. No host installation or publication
happens here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

UPDATE_AUTHORITY = "samples/hello-component/component.md"
UPDATE_MARKER = b"\n<!-- Installed HTML parent-update acceptance fixture. -->\n"


def run_command(
    litai: Path,
    project: Path,
    environment: dict,
    *arguments: str,
    expected_exit: int = 0,
) -> dict:
    from literate_ai.contracts.html_observability import HtmlRenderResult

    diagnostic_environment = {
        **environment,
        "LITAI_DEBUG": "1",
        "LITAI_DEBUG_JSON": "1",
        "LITAI_DEBUG_LOG": "-",
    }
    try:
        completed = subprocess.run(
            (str(litai), "--json", *arguments),
            cwd=project.parent,
            env=diagnostic_environment,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired can retain bytes even with text=True. Preserve the last
        # stage records before the outer wheel runner discards the child output.
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            "installed HTML CLI exceeded its 300-second deadline; "
            "last diagnostic output:\n" + stderr[-8000:]
        ) from exc
    if completed.returncode != expected_exit:
        raise RuntimeError(
            "installed HTML CLI returned an unexpected exit: "
            + (completed.stdout + completed.stderr)[-4000:]
        )
    payload = json.loads(completed.stdout)
    if (
        payload.get("schema") != "literate-ai/cli-result@1"
        or payload.get("ok") is not True
    ):
        raise RuntimeError("installed HTML CLI returned an invalid envelope")
    if arguments[:2] == ("render", "html"):
        if payload.get("command") != "render.html":
            raise RuntimeError("installed HTML CLI mislabeled its command")
        HtmlRenderResult.from_dict(payload["result"])
    return payload["result"]


def prepare_update_parent(parent: Path, workspace: Path) -> tuple[str, str, bytes]:
    """Advance only an owned local fixture ref, leaving the baseline HEAD intact."""
    parent = parent.resolve(strict=True)
    workspace.mkdir()
    hooks = workspace / "empty-hooks"
    hooks.mkdir()

    def git(*args: str, cwd: Path = workspace) -> str:
        result = subprocess.run(
            (
                "git",
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.eol=lf",
                "-c",
                "user.name=Literate AI HTML Fixture",
                "-c",
                "user.email=html-fixture@example.invalid",
                *args,
            ),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        return result.stdout.strip()

    if git("--git-dir", str(parent), "rev-parse", "--is-bare-repository") != "true":
        raise RuntimeError("HTML update fixture requires a local bare parent")
    baseline = git("--git-dir", str(parent), "rev-parse", "HEAD")
    checkout = workspace / "checkout"
    git("clone", "--no-local", "--no-checkout", str(parent), str(checkout))
    git("checkout", "--detach", baseline, cwd=checkout)
    authority = checkout / UPDATE_AUTHORITY
    updated = authority.read_bytes() + UPDATE_MARKER
    authority.write_bytes(updated)
    git("add", "--", UPDATE_AUTHORITY, cwd=checkout)
    git("commit", "-m", "Advance local HTML acceptance authority", cwd=checkout)
    revision = git("rev-parse", "HEAD", cwd=checkout)
    git("push", str(parent), f"{revision}:refs/heads/html-acceptance", cwd=checkout)
    if (
        revision == baseline
        or git("--git-dir", str(parent), "rev-parse", "HEAD") != baseline
    ):
        raise RuntimeError("HTML update fixture did not preserve its distinct baseline")
    return baseline, revision, updated


class EmbeddedDocuments(HTMLParser):
    """Read the two exact JSON blocks independently of the production emitter."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: dict[str, str] = {}
        self._current: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        identifier = attributes.get("id")
        if tag == "script" and identifier in ("litai-source", "litai-provenance"):
            if (
                identifier in self.documents
                or attributes.get("type") != "application/json"
                or "src" in attributes
            ):
                raise RuntimeError("HTML has ambiguous embedded JSON")
            self._current = identifier
            self.documents[identifier] = ""

    def handle_data(self, data):
        if self._current is not None:
            self.documents[self._current] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self._current = None


def inspect_artifact(
    artifact: dict, content: bytes, *, graph: dict, distribution: str
) -> None:
    """Compare completed bytes with independent public CLI observations."""
    # Imported here so importing this harness never selects an implementation.
    from literate_ai.contracts.html_observability import HtmlArtifact

    typed = HtmlArtifact.from_dict(artifact)
    if not typed.matches_bytes(content):
        raise RuntimeError("HTML bytes do not match the returned artifact")
    parser = EmbeddedDocuments()
    parser.feed(content.decode("utf-8"))
    parser.close()
    if set(parser.documents) != {"litai-source", "litai-provenance"}:
        raise RuntimeError("HTML omitted its embedded source or provenance")
    provenance = json.loads(parser.documents["litai-provenance"])
    if provenance != artifact["provenance"]:
        raise RuntimeError("HTML provenance differs from the CLI result")
    if json.loads(parser.documents["litai-source"]) != graph:
        raise RuntimeError("HTML source differs from the canonical graph")
    if (
        typed.provenance.renderer.framework_distribution_identity.uri != distribution
        or len(typed.provenance.source_bindings) != 1
        or typed.provenance.source_bindings[0].source_identity.uri != graph["identity"]
        or typed.provenance.view.scope_identifier != graph["project_id"]
    ):
        raise RuntimeError(
            "HTML provenance does not bind the installed wheel and graph"
        )


def cache_snapshot(root: Path) -> dict[str, tuple[int, str | None]]:
    """Include names, directory/file mtimes and bytes, not just cache values."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


def qualify(litai: Path, project: Path, distribution: str) -> dict:
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    # Dedicated cache custody prevents an ambient OBJ_DIR from selecting another tree.
    environment.update(OBJ_DIR="wheel-html-graph-objects", BUILD_DIR="wheel-html-build")
    cache = project / "wheel-html-objects"
    if cache.exists() or cache.is_symlink():
        raise RuntimeError("installed HTML qualification requires a fresh cache")

    def command(*arguments: str, expected_exit: int = 0) -> dict:
        return run_command(
            litai, project, environment, *arguments, expected_exit=expected_exit
        )

    graph = command("graph", "--project", str(project), "--format", "json")["graph"]
    if not graph["nodes"] or not graph["edges"]:
        raise RuntimeError("installed HTML fixture must have a nontrivial graph")
    # The graph CLI may record performance data. Keep those writes out of the
    # fresh cache whose absence the render off/read-only checks must prove.
    environment["OBJ_DIR"] = "wheel-html-objects"

    def render(mode: str, output: str, status: str) -> tuple[dict, bytes]:
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--output",
            output,
            "--cache-mode",
            mode,
        )
        if result["status"] != status or result["artifact"]["artifact_path"] != output:
            raise RuntimeError(
                "installed HTML CLI did not honor cache/output selection"
            )
        content = (project / output).read_bytes()
        inspect_artifact(
            result["artifact"], content, graph=graph, distribution=distribution
        )
        return result["artifact"], content

    for mode in ("off", "read-only"):
        render(mode, f"wheel-html-{mode}.html", "rendered")
        if cache.exists():
            raise RuntimeError("non-writing HTML cache mode created a cache")
    first, content = render("read-write", "wheel-graph.html", "rendered")
    snapshot = cache_snapshot(cache)
    if not snapshot:
        raise RuntimeError("HTML read-write render omitted its cache")
    output_mtime = (project / "wheel-graph.html").stat().st_mtime_ns
    for mode in ("read-write", "read-only"):
        repeated, repeated_content = render(mode, "wheel-graph.html", "cached")
        if (
            repeated != first
            or repeated_content != content
            or (project / "wheel-graph.html").stat().st_mtime_ns != output_mtime
            or cache_snapshot(cache) != snapshot
        ):
            raise RuntimeError("HTML cache hit changed its observation, bytes or cache")
    render("write-only", "wheel-write-only.html", "rendered")
    refusals = []
    for flag, value, code in (
        ("--surface", "unknown", "render.unsupported_surface"),
        ("--view", "unknown", "render.unknown_view"),
        ("--output", "../wheel-outside.html", "render.output_outside_project"),
        ("--external-asset-policy", "inline-only", "render.external_asset_forbidden"),
    ):
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--output",
            "wheel-refused.html",
            flag,
            value,
            expected_exit=1,
        )
        if result["status"] != "refused" or result["refusal"]["code"] != code:
            raise RuntimeError("installed HTML CLI lost its typed refusal")
        refusals.append(code)
    if (project / "wheel-refused.html").exists() or (
        project.parent / "wheel-outside.html"
    ).exists():
        raise RuntimeError("refused HTML request published output")
    return {
        "source": "non-editable-wheel-cli",
        "framework_distribution_identity": distribution,
        "graph_identity": graph["identity"],
        "artifact": first,
        "cache_modes": ["off", "read-only", "read-write", "write-only"],
        "repeat_bytes_and_observation_preserved": True,
        "refusals": refusals,
        "post_update_browser_acceptance": "not-exercised",
    }


def qualify_update(litai: Path, project: Path, distribution: str, parent: Path) -> dict:
    """Exercise real update and verification; this does not claim browser evidence."""
    from literate_ai.contracts.html_observability import HtmlRenderRequest, HtmlView
    from literate_ai.projects import ProjectConfigurationStore

    baseline, revision, updated = prepare_update_parent(
        parent, project.parent / "html-update-parent"
    )
    authority = project / UPDATE_AUTHORITY
    previous = authority.read_bytes()
    if updated != previous + UPDATE_MARKER:
        raise RuntimeError(
            "HTML update fixture authority differs from its parent baseline"
        )
    store = ProjectConfigurationStore(project.resolve(strict=True))
    snapshot = store.read()
    if snapshot.definition.html_render_requests:
        raise RuntimeError("HTML update qualification requires an undeclared fixture")
    request = HtmlRenderRequest(
        "authority-graph",
        HtmlView("dag", "1.0.0", "project", snapshot.definition.project_id),
        "graph.html",
        "off",
        "pinned-cdn",
    )
    store.update(
        snapshot, replace(snapshot.definition, html_render_requests=(request,))
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    environment.update(
        OBJ_DIR="wheel-html-update-objects", BUILD_DIR="wheel-html-update-build"
    )

    def command(*args: str, expected_exit: int = 0) -> dict:
        return run_command(
            litai, project, environment, *args, expected_exit=expected_exit
        )

    def graph() -> dict:
        result = command("graph", "--project", str(project), "--format", "json")[
            "graph"
        ]
        if not result["nodes"] or not result["edges"]:
            raise RuntimeError("updated HTML fixture graph must remain nontrivial")
        return result

    def render(observed_graph: dict) -> dict:
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--output",
            "graph.html",
            "--cache-mode",
            "off",
        )
        if (
            result["status"] != "rendered"
            or result["artifact"]["artifact_path"] != "graph.html"
        ):
            raise RuntimeError("HTML update render omitted its exact artifact")
        inspect_artifact(
            result["artifact"],
            (project / "graph.html").read_bytes(),
            graph=observed_graph,
            distribution=distribution,
        )
        return result["artifact"]

    def verify(status: str) -> dict:
        before = cache_snapshot(project / "wheel-html-update-objects")
        html = project / "graph.html"
        content, mtime = html.read_bytes(), html.stat().st_mtime_ns
        result = command(
            "verify",
            str(project),
            "--gate",
            "html-observability",
            expected_exit=1 if status == "stale" else 0,
        )
        report = require_verdict(result, status)
        if (
            cache_snapshot(project / "wheel-html-update-objects") != before
            or html.read_bytes() != content
            or html.stat().st_mtime_ns != mtime
        ):
            raise RuntimeError(
                "HTML verification changed its artifact or object directory"
            )
        return report

    before_graph = graph()
    first = render(before_graph)
    initial = verify("current")
    if (
        initial["expected_render_inputs_identity"]
        != first["provenance"]["render_inputs_identity"]
    ):
        raise RuntimeError("initial HTML verification did not bind the rendered inputs")
    original_html = (project / "graph.html").read_bytes()
    manifest = (project / "literate.project.json").read_bytes()
    plan = command("update", str(project), "--follow-ref", revision)
    require_update(plan, baseline, revision, applied=False)
    if (
        authority.read_bytes() != previous
        or (project / "literate.project.json").read_bytes() != manifest
        or graph() != before_graph
        or (project / "graph.html").read_bytes() != original_html
    ):
        raise RuntimeError("HTML update planning changed project authority")
    applied = command("update", str(project), "--follow-ref", revision, "--apply")
    require_update(applied, baseline, revision, applied=True)
    if (
        authority.read_bytes() != updated
        or (project / "graph.html").read_bytes() != original_html
    ):
        raise RuntimeError(
            "HTML update failed to change authority or rewrote derived HTML"
        )
    stale = verify("stale")
    after_graph = graph()
    if after_graph["identity"] == before_graph["identity"]:
        raise RuntimeError("HTML update did not change the canonical graph")
    final = render(after_graph)
    current = verify("current")
    if (
        first["provenance"]["render_inputs_identity"]
        == final["provenance"]["render_inputs_identity"]
        or stale["observed_render_inputs_identity"]
        != first["provenance"]["render_inputs_identity"]
        or stale["expected_render_inputs_identity"]
        != current["expected_render_inputs_identity"]
        or current["expected_render_inputs_identity"]
        != final["provenance"]["render_inputs_identity"]
    ):
        raise RuntimeError(
            "HTML update observations do not bind the same changed inputs"
        )
    return {
        "source": "non-editable-wheel-cli",
        "parent_before": baseline,
        "parent_after": revision,
        "authority_path": UPDATE_AUTHORITY,
        "authority_identity": "sha256:" + hashlib.sha256(updated).hexdigest(),
        "graph_before": before_graph["identity"],
        "graph_after": after_graph["identity"],
        "verdicts": ["current", "stale", "current"],
        "artifact": final,
        "framework_distribution_identity": distribution,
        "post_update_browser_acceptance": "not-exercised",
    }


def require_update(
    result: dict, baseline: str, revision: str, *, applied: bool
) -> None:
    """Reject a no-op or a selector that did not resolve to the reviewed commit."""
    follow = result.get("follow", {})
    if (
        result.get("schema") != "literate-ai/composite-project-update@1"
        or result.get("mode") != ("applied" if applied else "read-only-plan")
        or result.get("changed") is not True
        or follow.get("from") != baseline
        or follow.get("to") != revision
        or baseline == revision
    ):
        raise RuntimeError("HTML acceptance requires a real exact-revision update")
    if applied and (
        not result.get("repository_lineage", {}).get("applied")
        or not any(
            item.get("resolved_revision") == revision
            and item.get("requested_revision") == revision
            for item in result.get("parent_selectors", [])
        )
    ):
        raise RuntimeError("HTML update did not apply the reviewed parent revision")


def require_verdict(result: dict, status: str) -> dict:
    from literate_ai.contracts.html_observability import HtmlStalenessReport

    gates = result.get("gates", [])
    expected_ok = status == "current"
    if (
        result.get("schema") != "literate-ai/project-verify@1"
        or result.get("ok") is not expected_ok
        or len(gates) != 1
        or gates[0].get("gate") != "html-observability"
        or gates[0].get("state") != ("pass" if expected_ok else "fail")
        or len(gates[0].get("artifacts", [])) != 1
    ):
        raise RuntimeError("HTML update verification omitted its exact gate verdict")
    wire = gates[0]["artifacts"][0]
    report = HtmlStalenessReport.from_dict(wire)
    if (
        report.status != status
        or report.artifact_path != "graph.html"
        or (status == "stale" and report.stale_source_labels != ("authority-graph",))
    ):
        raise RuntimeError("HTML update verification lost the changed source label")
    return wire


def qualify_version_health(litai: Path, project: Path, distribution: str) -> dict:
    """Exercise version rendering and currency through the installed public CLI."""
    from literate_ai.contracts.html_observability import (
        HtmlArtifact,
        HtmlRenderRequest,
        HtmlView,
    )
    from literate_ai.contracts.identity import canonical_identity
    from literate_ai.projects import ProjectConfigurationStore

    store = ProjectConfigurationStore(project.resolve(strict=True))
    original = store.read()
    request = HtmlRenderRequest(
        "version-check",
        HtmlView("health", "1.0.0", "project", original.definition.project_id),
        "version.html",
        "off",
        "inline-only",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    environment.update(OBJ_DIR="wheel-version-objects", BUILD_DIR="wheel-version-build")

    def command(*args: str, expected_exit: int = 0) -> dict:
        return run_command(
            litai, project, environment, *args, expected_exit=expected_exit
        )

    def verify(expected: str) -> None:
        before = (project / request.output_path).read_bytes()
        result = command(
            "verify",
            str(project),
            "--gate",
            "html-observability",
            expected_exit=0 if expected == "current" else 1,
        )
        gates = result["gates"]
        if len(gates) != 1 or len(gates[0].get("artifacts", [])) != 1:
            raise RuntimeError("version health omitted its declared HTML verdict")
        observed = gates[0]["artifacts"][0]
        if (
            observed["status"] != expected
            or observed["artifact_path"] != request.output_path
        ):
            raise RuntimeError("version health lost exact artifact currency")
        if expected == "stale" and observed["stale_source_labels"] != ["version-check"]:
            raise RuntimeError("version health lost the changed source label")
        if (project / request.output_path).read_bytes() != before:
            raise RuntimeError("version health verification mutated output")

    try:
        store.update(
            original, replace(original.definition, html_render_requests=(request,))
        )
        report = command(
            "version", "check", "--project", str(project), "--require-project"
        )
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--surface",
            "version-check",
            "--view",
            "health",
            "--output",
            request.output_path,
            "--cache-mode",
            "off",
            "--external-asset-policy",
            "inline-only",
        )
        if result["status"] != "rendered":
            raise RuntimeError(
                "version health did not render through the installed CLI"
            )
        artifact = HtmlArtifact.from_dict(result["artifact"])
        content = (project / request.output_path).read_bytes()
        parsed = EmbeddedDocuments()
        parsed.feed(content.decode("utf-8"))
        parsed.close()
        if (
            not artifact.matches_bytes(content)
            or json.loads(parsed.documents["litai-source"]) != report
            or json.loads(parsed.documents["litai-provenance"])
            != result["artifact"]["provenance"]
        ):
            raise RuntimeError(
                "version health differs from its public report or artifact"
            )
        if (
            artifact.provenance.external_assets
            or artifact.provenance.renderer.framework_distribution_identity.uri
            != distribution
            or artifact.provenance.source_bindings[0].source_identity
            != canonical_identity(report)
        ):
            raise RuntimeError(
                "version health lost its offline or installed-wheel binding"
            )
        verify("current")
        selected = store.read()
        changed_version = "0.0.1" if selected.definition.version != "0.0.1" else "0.0.2"
        store.update(selected, replace(selected.definition, version=changed_version))
        verify("stale")
        selected = store.read()
        store.update(
            selected, replace(selected.definition, version=original.definition.version)
        )
        verify("current")
        return {
            "source": "non-editable-wheel-cli",
            "artifact": result["artifact"],
            "report_identity": canonical_identity(report).uri,
            "currency": ["current", "stale", "current"],
            "browser_acceptance": "not-exercised",
        }
    finally:
        selected = store.read()
        store.update(selected, original.definition)


def qualify_lock_health(litai: Path, project: Path, distribution: str) -> dict:
    """Exercise lock rendering and currency through the installed public CLI."""
    from literate_ai.adapters.project_lock_health import observe_project_locks
    from literate_ai.contracts.html_observability import (
        HtmlArtifact,
        HtmlRenderRequest,
        HtmlView,
    )
    from literate_ai.contracts.identity import canonical_identity
    from literate_ai.projects import ProjectConfigurationStore

    store = ProjectConfigurationStore(project.resolve(strict=True))
    original = store.read()
    request = HtmlRenderRequest(
        "lock-health",
        HtmlView("health", "1.0.0", "project", original.definition.project_id),
        "locks.html",
        "off",
        "inline-only",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    environment.update(OBJ_DIR="wheel-lock-objects", BUILD_DIR="wheel-lock-build")

    def command(*args: str, expected_exit: int = 0) -> dict:
        return run_command(
            litai, project, environment, *args, expected_exit=expected_exit
        )

    def verify(expected: str) -> None:
        before = (project / request.output_path).read_bytes()
        result = command(
            "verify",
            str(project),
            "--gate",
            "html-observability",
            expected_exit=0 if expected == "current" else 1,
        )
        gates = result["gates"]
        if len(gates) != 1 or len(gates[0].get("artifacts", [])) != 1:
            raise RuntimeError("lock health omitted its declared HTML verdict")
        observed = gates[0]["artifacts"][0]
        if (
            observed["status"] != expected
            or observed["artifact_path"] != request.output_path
        ):
            raise RuntimeError("lock health lost exact artifact currency")
        if expected == "stale" and observed["stale_source_labels"] != ["lock-health"]:
            raise RuntimeError("lock health lost the changed source label")
        if (project / request.output_path).read_bytes() != before:
            raise RuntimeError("lock health verification mutated output")

    def render_report(output_path: str) -> tuple[dict, dict]:
        report = observe_project_locks(project).to_dict()
        gate = command(
            "verify",
            str(project),
            "--gate",
            "locks",
            expected_exit=1 if report["gate"]["state"] == "fail" else 0,
        )["gates"][0]
        if gate != report["gate"]:
            raise RuntimeError("installed lock-health rollup differs from verification")
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--surface",
            "lock-health",
            "--view",
            "health",
            "--output",
            output_path,
            "--cache-mode",
            "off",
            "--external-asset-policy",
            "inline-only",
        )
        if result["status"] != "rendered":
            raise RuntimeError("lock health did not render through the installed CLI")
        artifact = HtmlArtifact.from_dict(result["artifact"])
        content = (project / output_path).read_bytes()
        parsed = EmbeddedDocuments()
        parsed.feed(content.decode("utf-8"))
        parsed.close()
        if (
            not artifact.matches_bytes(content)
            or artifact.artifact_path != output_path
            or json.loads(parsed.documents["litai-source"]) != report
            or json.loads(parsed.documents["litai-provenance"])
            != result["artifact"]["provenance"]
        ):
            raise RuntimeError("lock health differs from its public report or artifact")
        if (
            artifact.provenance.external_assets
            or artifact.provenance.renderer.framework_distribution_identity.uri
            != distribution
            or artifact.provenance.source_bindings[0].source_identity
            != canonical_identity(report)
        ):
            raise RuntimeError(
                "lock health lost its offline or installed-wheel binding"
            )
        return report, result

    def retain_state(name: str, gate: str, row: str | None) -> dict:
        report, result = render_report(f"locks-{name}.html")
        if report["gate"]["state"] != gate or (
            [item["state"] for item in report["components"]]
            != ([] if row is None else [row])
        ):
            raise RuntimeError(f"installed lock-health {name} fixture lost its state")
        return {
            "state": name,
            "gate": gate,
            "artifact": result["artifact"],
            "report_identity": canonical_identity(report).uri,
        }

    component = project / "samples/hello-component"
    artifacts = (
        component / "component.lock.json",
        component / "component.resolution-audit.host.json",
    )
    retained = {
        path: path.read_bytes() if path.exists() else None for path in artifacts
    }
    try:
        store.update(
            original,
            replace(original.definition, html_render_requests=(request,)),
        )
        command("lock", str(component))
        report, result = render_report(request.output_path)
        if report["gate"]["state"] != "pass" or not report["components"]:
            raise RuntimeError(
                "installed lock-health fixture requires current committed locks"
            )
        verify("current")
        audit_path = artifacts[1]
        audit = audit_path.read_bytes()
        audit_path.unlink()
        failed = command("verify", str(project), "--gate", "locks", expected_exit=1)
        if failed["gates"][0]["state"] != "fail":
            raise RuntimeError(
                "missing resolution audit did not fail lock verification"
            )
        verify("stale")
        states = [retain_state("not-current", "fail", "not-current")]
        audit_path.write_bytes(audit)
        verify("current")
        lock_path = artifacts[0]
        lock_bytes = lock_path.read_bytes()
        lock_path.write_bytes(b"{\n")
        states.append(retain_state("error", "fail", "error"))
        lock_path.unlink()
        states.append(retain_state("empty", "skipped", None))
        lock_path.write_bytes(lock_bytes)
        verify("current")
        return {
            "source": "non-editable-wheel-cli",
            "artifact": result["artifact"],
            "report_identity": canonical_identity(report).uri,
            "currency": ["current", "stale", "current"],
            "states": states,
            "browser_acceptance": "not-exercised",
        }
    finally:
        for path, content in retained.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        selected = store.read()
        store.update(selected, original.definition)


def qualify_verification_health(litai: Path, project: Path, distribution: str) -> dict:
    """Bind installed selected-gate reports and verify drift through the public CLI."""
    from literate_ai.adapters.html_surfaces import VERIFICATION_HEALTH_GATES
    from literate_ai.adapters.project_verification import observe_project_verification
    from literate_ai.contracts.html_observability import (
        HtmlArtifact,
        HtmlRenderRequest,
        HtmlView,
    )
    from literate_ai.contracts.identity import canonical_identity
    from literate_ai.projects import ProjectConfigurationStore

    project = project.resolve(strict=True)
    store = ProjectConfigurationStore(project)
    original = store.read()
    request = HtmlRenderRequest(
        "verification-health",
        HtmlView("health", "1.0.0", "project", original.definition.project_id),
        "verification.html",
        "off",
        "inline-only",
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    environment.update(OBJ_DIR="wheel-verify-objects", BUILD_DIR="wheel-verify-build")

    def command(*args: str, expected_exit: int = 0) -> dict:
        return run_command(
            litai, project, environment, *args, expected_exit=expected_exit
        )

    def render(output: str) -> tuple[dict, dict]:
        report, exit_code = observe_project_verification(
            project, gates=VERIFICATION_HEALTH_GATES
        )
        gate_args = tuple(
            value for gate in VERIFICATION_HEALTH_GATES for value in ("--gate", gate)
        )
        if (
            command("verify", str(project), *gate_args, expected_exit=exit_code)
            != report
        ):
            raise RuntimeError(
                "installed verification dashboard differs from CLI gates"
            )
        result = command(
            "render",
            "html",
            "--project",
            str(project),
            "--surface",
            request.surface_id,
            "--view",
            "health",
            "--output",
            output,
            "--cache-mode",
            "off",
            "--external-asset-policy",
            "inline-only",
        )
        if result["status"] != "rendered":
            raise RuntimeError("installed verification dashboard did not render")
        artifact = HtmlArtifact.from_dict(result["artifact"])
        content = (project / output).read_bytes()
        parsed = EmbeddedDocuments()
        parsed.feed(content.decode("utf-8"))
        parsed.close()
        if (
            not artifact.matches_bytes(content)
            or artifact.artifact_path != output
            or json.loads(parsed.documents["litai-source"]) != report
            or json.loads(parsed.documents["litai-provenance"])
            != artifact.provenance.to_dict()
            or artifact.provenance.external_assets
            or artifact.provenance.renderer.framework_distribution_identity.uri
            != distribution
            or artifact.provenance.source_bindings[0].source_identity
            != canonical_identity(report)
        ):
            raise RuntimeError(
                "installed verification dashboard lost report or wheel binding"
            )
        return report, result

    def verify(expected: str) -> None:
        output = project / request.output_path
        before = output.read_bytes()
        result = command(
            "verify",
            str(project),
            "--gate",
            "html-observability",
            expected_exit=0 if expected == "current" else 1,
        )
        artifacts = result["gates"][0].get("artifacts", [])
        if len(artifacts) != 1 or artifacts[0]["status"] != expected:
            raise RuntimeError("installed verification dashboard lost artifact verdict")
        if artifacts[0]["artifact_path"] != request.output_path:
            raise RuntimeError("installed verification checked another artifact")
        if expected == "stale" and artifacts[0]["stale_source_labels"] != [
            request.surface_id
        ]:
            raise RuntimeError("installed verification dashboard lost source drift")
        if output.read_bytes() != before:
            raise RuntimeError("installed verification mutated the retained dashboard")

    component = project / "samples/hello-component"
    lock = component / "component.lock.json"
    paths = (lock, component / "component.resolution-audit.host.json")
    retained = {path: path.read_bytes() if path.exists() else None for path in paths}
    try:
        store.update(
            original, replace(original.definition, html_render_requests=(request,))
        )
        command("lock", str(component))
        report, result = render(request.output_path)
        if (
            next(row for row in report["gates"] if row["gate"] == "locks")["state"]
            != "pass"
        ):
            raise RuntimeError("verification fixture did not create current locks")
        verify("current")
        content = (project / request.output_path).read_bytes()
        (project / request.output_path).write_bytes(
            content.replace(b"Verification checks", b"Edited checks")
        )
        verify("unpinned")
        (project / request.output_path).write_bytes(content)
        lock_bytes = lock.read_bytes()
        lock.write_bytes(b"{\n")
        verify("stale")
        failed_report, failed = render("verification-failed.html")
        if (
            failed_report["ok"]
            or next(row for row in failed_report["gates"] if row["gate"] == "locks")[
                "state"
            ]
            != "fail"
        ):
            raise RuntimeError(
                "verification dashboard suppressed malformed lock failure"
            )
        lock.write_bytes(lock_bytes)
        verify("current")
        return {
            "source": "non-editable-wheel-cli",
            "artifact": result["artifact"],
            "report_identity": canonical_identity(report).uri,
            "currency": ["current", "unpinned", "stale", "current"],
            "failed": {
                "artifact": failed["artifact"],
                "report_identity": canonical_identity(failed_report).uri,
            },
            "browser_acceptance": "not-exercised",
        }
    finally:
        for path, content in retained.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        store.update(store.read(), original.definition)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("litai", type=Path)
    parser.add_argument("project", type=Path)
    parser.add_argument("distribution")
    parser.add_argument("--parent-repository", type=Path)
    args = parser.parse_args()
    result = qualify(args.litai, args.project, args.distribution)
    if args.parent_repository is not None:
        result["update"] = qualify_update(
            args.litai, args.project, args.distribution, args.parent_repository
        )
    result["version_health"] = qualify_version_health(
        args.litai, args.project, args.distribution
    )
    result["lock_health"] = qualify_lock_health(
        args.litai, args.project, args.distribution
    )
    result["verification_health"] = qualify_verification_health(
        args.litai, args.project, args.distribution
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
