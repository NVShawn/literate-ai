"""Qualify real adopted-project renames through the noneditable public CLI.

The small retained application actually builds, runs two unittest cases and
packages. This proves retained parity, not regenerated/native Component acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    require(
        __debug__ and sys.flags.isolated and len(sys.argv) == 4,
        "isolated installed Python, fresh root, distribution and parent required",
    )
    import literate_ai
    from literate_ai.adapters.standard_lifecycle_binding import (
        observe_installed_framework_distribution,
    )
    from literate_ai.remote_worker_bootstrap import installed_distribution_identity

    prefix = Path(sys.prefix).resolve(strict=True)
    expected = sys.argv[2]

    def installed():
        require(
            installed_distribution_identity(prefix) == expected
            and observe_installed_framework_distribution().identity.uri == expected,
            "installed framework distribution changed",
        )
        for name, module in tuple(sys.modules.items()):
            filename = getattr(module, "__file__", None)
            if (name == "literate_ai" or name.startswith("literate_ai.")) and filename:
                require(
                    Path(filename).resolve(strict=True).is_relative_to(prefix),
                    "framework loaded outside installed prefix",
                )

    installed()
    require(Path(literate_ai.__file__).is_file(), "installed framework unavailable")
    root = Path(sys.argv[1]).absolute()
    require(not os.path.lexists(root), "preserve previous migration fixture")
    root.mkdir()
    project = root / "alpha"
    project.mkdir()
    executable = Path(sys.executable).as_posix()
    litai = Path(sysconfig.get_path("scripts")) / (
        "litai.exe" if os.name == "nt" else "litai"
    )
    environment = dict(os.environ)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PYTHONPYCACHEPREFIX",
        "__PYVENV_LAUNCHER__",
    ):
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    refusals = []

    def run(arguments, *, cwd=root, expected_exit=0):
        completed = subprocess.run(
            [str(value) for value in arguments],
            cwd=cwd,
            env=environment,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        require(
            completed.returncode == expected_exit,
            f"fixture command {arguments[0]} exited {completed.returncode}: "
            f"{completed.stdout[-2000:]} {completed.stderr[-2000:]}",
        )
        return completed

    def cli(*arguments, expected_exit=0):
        completed = run([litai, "--json", *arguments], expected_exit=expected_exit)
        payload = json.loads(
            completed.stdout if expected_exit == 0 else completed.stderr
        )
        if expected_exit:
            refusals.append(payload["error"]["code"])
            return payload
        require(payload["ok"] is True, "public CLI did not succeed")
        return payload["result"]

    authored = {
        "app.py": "def add(a, b):\n    return a + b\n",
        "test_app.py": (
            "import unittest\nfrom app import add\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
            "    def test_negative(self): self.assertEqual(add(-3, 1), -2)\n"
        ),
        "build.py": (
            "from pathlib import Path\nimport hashlib, sys\nfrom app import add\n"
            "assert add(2, 3) == 5\n"
            'folder = Path("build" if sys.argv[1] == "build" else "dist")\n'
            "folder.mkdir(exist_ok=True)\n"
            '(folder / "app.sha256").write_text('
            'hashlib.sha256(Path("app.py").read_bytes()).hexdigest() + "\\n")\n'
        ),
        ".gitignore": "build/\ndist/\n__pycache__/\n",
        "Makefile": (
            f'build:\n\t"{executable}" build.py build\n'
            f'test:\n\t"{executable}" -m unittest -v test_app\n'
            f'package:\n\t"{executable}" build.py package\n'
        ),
    }
    for name, text in authored.items():
        (project / name).write_text(text, encoding="utf-8")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Migration Qualification"),
        ("config", "user.email", "migration@example.invalid"),
        ("add", "."),
        ("-c", "commit.gpgsign=false", "commit", "-m", "Create retained application"),
    ):
        run(["git", *args], cwd=project)
    adopt = (
        "onboard",
        "adopt",
        str(project),
        "--from",
        sys.argv[3],
        "--default-branch",
        "main",
        "--run-baseline",
    )
    plan = cli(*adopt)
    require(plan["writes"] is False, "adoption plan must be read-only")
    cli(*adopt, "--apply", "--acknowledge", "--expect-plan", plan["plan_identity"])
    source = project / "components/legacy-project-wrapper/implementation"
    original = {name: (source / name).read_bytes() for name in authored}
    manifest = project / "literate.project.json"
    runs = []

    def receipt(name):
        candidate = root / (name + ".json")
        observed = cli(
            "project",
            "test-receipt",
            "run-retained",
            str(candidate),
            "--project",
            str(project),
            "--worker-id",
            "local",
        )
        evidence = json.loads((root / (name + ".evidence.json")).read_bytes())
        require(
            observed["tests"] == evidence["tests"] == 2
            and evidence["project_id"] == name
            and evidence["classification"] == "retained-legacy-parity"
            and evidence["native_component_acceptance"] is False
            and next(
                p["test_collection"] for p in evidence["phases"] if p["phase"] == "test"
            )
            == {
                "failed": 0,
                "known_failed": 0,
                "passed": 2,
                "skipped": 0,
                "state": "nonempty",
                "total": 2,
            }
            and all(
                p["exit_code"] == 0 and not p["timed_out"] for p in evidence["phases"]
            )
            and {p["phase"] for p in evidence["phases"]}
            == {"build", "test", "package"},
            "real retained phases and two collected tests required",
        )
        cli(
            "project",
            "test-receipt",
            "update",
            str(candidate),
            "--project",
            str(project),
        )
        runs.append(
            {
                k: observed[k]
                for k in (
                    "candidate_identity",
                    "evidence_identity",
                    "tests",
                    "classification",
                )
            }
        )

    receipt("alpha")
    cli(
        "project",
        "convert-stage",
        "advance",
        "--to",
        "retained",
        "--project",
        str(project),
    )
    historical = {
        p: p.read_bytes() for p in (project / ".literate").glob("legacy-*.json")
    }
    require(len(historical) == 3, "three immutable adoption records required")

    def authority_snapshot():
        paths = [manifest, project / "verification/current.json"]
        paths.extend(p for p in (project / ".literate").rglob("*") if p.is_file())
        paths.extend(source / name for name in authored)
        return {p.relative_to(project).as_posix(): p.read_bytes() for p in paths}

    records = []
    for previous, name in (("alpha", "beta"), ("beta", "gamma")):
        old_receipt = (project / "verification/current.json").read_bytes()
        definition = json.loads(manifest.read_bytes())
        require(
            definition["project_id"] == previous, "unexpected current project identity"
        )
        definition["project_id"] = name
        manifest.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
        migrate = (
            "project",
            "identity",
            "migrate",
            "--project",
            str(project),
            "--from-project-id",
            previous,
        )
        before = authority_snapshot()
        plan = cli(*migrate)
        require(authority_snapshot() == before, "migration plan changed authority")
        require(
            plan["requalification_required"], "migration must invalidate qualification"
        )
        refused = cli(
            *migrate,
            "--apply",
            "--expected-plan-identity",
            plan["plan_identity"],
            expected_exit=2,
        )
        require(
            refused["error"]["code"] == "project_identity.acknowledgement_required",
            "missing acknowledgement admitted",
        )
        require(
            authority_snapshot() == before, "acknowledgement refusal changed authority"
        )
        application = source / "app.py"
        original_application = application.read_bytes()
        application.write_bytes(original_application + b"# changed after plan\n")
        changed = authority_snapshot()
        refused = cli(
            *migrate,
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
            expected_exit=2,
        )
        require(
            refused["error"]["code"] == "project_identity.plan_stale",
            "changed source accepted",
        )
        require(authority_snapshot() == changed, "stale-plan refusal changed authority")
        application.write_bytes(original_application)
        applied = cli(
            *migrate,
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )
        history = project / applied["history"]
        records.append((history, history.read_bytes()))
        require(
            (project / "verification/current.json").read_bytes() == old_receipt,
            "old receipt rewritten",
        )
        cli(
            "project",
            "test-receipt",
            "require-current",
            "--project",
            str(project),
            expected_exit=2,
        )
        refresh = ("project", "retained-scope", "refresh", "--project", str(project))
        plan = cli(*refresh)
        cli(
            *refresh,
            "--apply",
            "--acknowledge",
            "--expected-plan-identity",
            plan["plan_identity"],
        )
        stale = cli(
            "project",
            "test-receipt",
            "require-current",
            "--project",
            str(project),
            expected_exit=2,
        )
        require(
            stale["error"]["code"] != "project.documentation_authority_review_stale",
            "renewed scope must expose the old receipt refusal",
        )
        receipt(name)
        require(
            cli(
                "project", "test-receipt", "require-current", "--project", str(project)
            )["state"]
            == "current",
            "fresh receipt not current",
        )
        require(
            all(p.read_bytes() == raw for p, raw in historical.items()),
            "historical adoption changed",
        )
        require(
            all(p.read_bytes() == raw for p, raw in records), "rename history changed"
        )
        require(
            all((source / name).read_bytes() == raw for name, raw in original.items()),
            "retained source changed",
        )
    installed()
    print(
        json.dumps(
            {
                "qualified": True,
                "framework_distribution": expected,
                "renames": 2,
                "native_retained_runs": runs,
                "refusals": refusals,
                "immutable_adoption_records": len(historical),
                "immutable_rename_records": len(records),
                "source_files_preserved": len(original),
                "synthetic_framework_observers": False,
                "fixture_source_sha256": {
                    name: hashlib.sha256(raw).hexdigest()
                    for name, raw in original.items()
                },
                "scope": (
                    "Installed public CLI and real retained parity; "
                    "no regenerated Component acceptance"
                ),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
