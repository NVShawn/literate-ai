from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import literate_ai.project_releases as project_releases
from literate_ai.evidence_ledger import load_run
from literate_ai.project_releases import (
    PREPARED_RELEASE_SCHEMA,
    PUBLISHED_VERIFICATION_SCHEMA,
    ProjectReleaseError,
    ReleasePolicy,
    check_release,
    create_release_plan,
    prepare_release,
    publish_release,
    verify_published_release,
)
from tests.support.fixtures_test_schema_catalog import V2_ROOT, SchemaCatalog


class ProjectReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        evidence_environment = patch.dict(
            project_releases.os.environ,
            {
                "OBJ_DIR": "",
                "LITAI_EVIDENCE_RUN": "",
                "LITAI_EVIDENCE_PARENT": "",
            },
            clear=False,
        )
        evidence_environment.start()
        self.addCleanup(evidence_environment.stop)

    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def write_policy(self, root: Path) -> None:
        policy = {
            "schema": "literate-ai/release-policy@1",
            "version_scheme": "semver",
            "version_authority": {
                "path": "project.json",
                "format": "json-pointer",
                "selectors": ["/version"],
            },
            "version_mirrors": [
                {
                    "path": "version.py",
                    "format": "python-constant",
                    "selectors": ["VERSION"],
                }
            ],
            "allowed_transitions": ["patch", "minor", "major", "explicit"],
            "gate": {
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('project.json').is_file()",
                ],
                "timeout_seconds": 30,
            },
            "changelog": {
                "path": "CHANGELOG.md",
                "unreleased_heading": "## Unreleased",
            },
            "tag": {"prefix": "v", "signed": False},
            "remote": "origin",
            "provider": None,
        }
        (root / "literate.release.json").write_text(
            json.dumps(policy, indent=2) + "\n", encoding="utf-8"
        )

    def test_authorization_strict_and_loose_break_glass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text(
                "## Release Engineers\n\n- `release-admin`\n\n## Other\n",
                encoding="utf-8",
            )
            policy_data = json.loads(
                (
                    Path(__file__).resolve().parents[2] / "literate.release.json"
                ).read_text()
            )
            policy = ReleasePolicy.from_dict(policy_data)
            strict = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(
                    repository_policy=SimpleNamespace(
                        patch_authority="strict",
                        writers=("writer",),
                        release_engineer_source="README.md#release-engineers",
                    )
                ),
            )
            policy = replace(policy, provider_kind=None, provider_repository=None)
            with patch(
                "literate_ai.project_releases.discover_project", return_value=strict
            ):
                with self.assertRaises(ProjectReleaseError) as denied:
                    project_releases._authorize(
                        root,
                        policy,
                        actor="writer",
                        operation="patch",
                        allow_break_glass=True,
                        break_glass_reason="urgent fix",
                    )
            self.assertEqual(denied.exception.code, "release.actor_unauthorized")
            loose = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(
                    repository_policy=SimpleNamespace(
                        patch_authority="loose",
                        writers=("writer",),
                        release_engineer_source="README.md#release-engineers",
                    )
                ),
            )
            with patch(
                "literate_ai.project_releases.discover_project", return_value=loose
            ):
                with self.assertRaises(ProjectReleaseError):
                    project_releases._authorize(
                        root,
                        policy,
                        actor="writer",
                        operation="patch",
                        allow_break_glass=True,
                    )
                authorization = project_releases._authorize(
                    root,
                    policy,
                    actor="writer",
                    operation="patch",
                    allow_break_glass=True,
                    break_glass_reason="urgent fix",
                )
            self.assertEqual(authorization["mode"], "break-glass")
            self.assertEqual(authorization["reason"], "urgent fix")

    def test_github_explicit_actor_cannot_spoof_authenticated_login(self) -> None:
        root = Path(__file__).resolve().parents[2]
        policy = ReleasePolicy.from_dict(
            json.loads((root / "literate.release.json").read_text())
        )
        completed = subprocess.CompletedProcess(
            ("gh",), 0, stdout="authenticated-user\n", stderr=""
        )
        with patch(
            "literate_ai.project_releases.subprocess.run", return_value=completed
        ):
            with self.assertRaises(ProjectReleaseError) as raised:
                project_releases._resolve_actor(root, policy, "release-admin")
        self.assertEqual(raised.exception.code, "release.actor_mismatch")

    def initialize(self, parent: Path) -> tuple[Path, Path]:
        root = parent / "project"
        remote = parent / "remote.git"
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "release@example.invalid")
        self.git(root, "config", "user.name", "Release Test")
        (root / "literate.project.json").write_text("{}\n", encoding="utf-8")
        (root / "project.json").write_text(
            json.dumps({"version": "1.2.3"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "version.py").write_text('VERSION = "1.2.3"\n', encoding="utf-8")
        (root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## Unreleased\n\n- Prepared feature.\n",
            encoding="utf-8",
        )
        (root / ".gitignore").write_text(
            "_build/\n.litai-cache-locks/\n", encoding="utf-8"
        )
        self.write_policy(root)
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        self.git(parent, "init", "--bare", str(remote))
        self.git(root, "remote", "add", "origin", str(remote))
        self.git(root, "push", "-u", "origin", "main")
        # A newly initialized bare repository may still point HEAD at ``master``.
        # Pin its advertised default branch so clones are independent of the host's
        # init.defaultBranch setting and always check out the commit just pushed.
        self.git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
        return root, remote

    @staticmethod
    def write_record(path: Path, record: dict[str, object]) -> None:
        path.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def test_plan_prepare_check_and_publish_local_git_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                self.assertTrue(plan["source_clean"])
                self.assertEqual(plan["current_version"], "1.2.3")
                self.assertEqual(plan["next_version"], "1.2.4")
                self.assertEqual(plan["tag"], "v1.2.4")
                self.assertNotIn("project", plan)
                self.assertNotIn("release_line", plan)
                schemas = SchemaCatalog(V2_ROOT)
                schemas.validate("literate-ai/release-plan@1", plan)

                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepared_change = prepare_release(root, plan_path)
                self.assertEqual(prepared_change["next_version"], "1.2.4")
                self.assertEqual(
                    json.loads((root / "project.json").read_text())["version"],
                    "1.2.4",
                )
                changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
                self.assertIn("## 1.2.4 -", changelog_text)
                self.assertNotIn(
                    "https://github.com/",
                    changelog_text,
                    "projects without a GitHub provider must not invent blob URLs",
                )

                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["schema"], PREPARED_RELEASE_SCHEMA)
                self.assertEqual(checked["gate"]["exit_status"], 0)
                evidence = checked["evidence"]
                self.assertIsInstance(evidence, dict)
                assert isinstance(evidence, dict)
                run = load_run(root, str(evidence["run_id"]))
                self.assertIsNotNone(run)
                assert run is not None
                self.assertEqual(evidence["node_id"], "n0001")
                nodes = {
                    item["path"]: item
                    for item in run.reduced()["nodes"]
                    if isinstance(item, dict)
                }
                self.assertEqual(nodes["release"]["state"], "passed")
                gate = nodes["release/gate"]
                self.assertEqual(gate["state"], "passed")
                self.assertTrue(
                    (run.root / str(gate["node_id"]) / "stdout.log").is_file()
                )
                self.assertTrue(
                    (run.root / str(gate["node_id"]) / "stderr.log").is_file()
                )
                self.assertEqual(
                    {item["path"] for item in run.reduced()["nodes"]},
                    {
                        "release",
                        "release/gate",
                        "release/target/local",
                        "release/target/github",
                    },
                )
                checked_record = {
                    key: value for key, value in checked.items() if key != "record"
                }
                schemas.validate(PREPARED_RELEASE_SCHEMA, checked_record)
                receipt = publish_release(
                    root,
                    prepared_path,
                    authorize_external_write=True,
                )
                self.assertEqual(receipt["tag"], "v1.2.4")
                schemas.validate(receipt["schema"], receipt)
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/tags/v1.2.4^{}"),
                    receipt["revision"],
                )
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/heads/main"),
                    receipt["revision"],
                )
                verified = verify_published_release(root, prepared_path)
                self.assertEqual(verified["schema"], PUBLISHED_VERIFICATION_SCHEMA)
                self.assertEqual(verified["revision"], receipt["revision"])
                self.assertEqual(verified["tag"], receipt["tag"])
                self.assertTrue(verified["remote_tag_annotated"])
                schemas.validate(verified["schema"], verified)
                retried = publish_release(
                    root,
                    prepared_path,
                    authorize_external_write=True,
                )
                self.assertEqual(retried, receipt)

    def test_artifact_gate_binds_checked_bytes_before_any_publication(self) -> None:
        from literate_ai.contracts import canonical_identity
        from literate_ai.release_files import SCHEMA, file_identity

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root, remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text())
            gate_argv = ["fixture-release-qualifier"]
            policy["artifact_gate"] = {
                "argv": gate_argv,
                "timeout_seconds": 30,
                "manifest": "_build/qualified/manifest.json",
                "required_roles": ["fixture"],
            }
            self.write_record(policy_path, policy)
            self.git(root, "add", "literate.release.json")
            self.git(root, "commit", "-m", "Require artifact qualification")
            self.git(root, "push", "origin", "main")
            original_run = subprocess.run
            artifact = root / "_build/qualified/package.bin"
            invocations = []

            def run(argv, **kwargs):
                if argv != gate_argv:
                    return original_run(argv, **kwargs)
                invocations.append(argv)
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(b"qualified bytes")
                manifest = {
                    "schema": SCHEMA,
                    "revision": self.git(root, "rev-parse", "HEAD"),
                    "version": "1.2.4",
                    "files": [
                        {
                            "role": "fixture",
                            "path": artifact.relative_to(root).as_posix(),
                            "size": artifact.stat().st_size,
                            "identity": file_identity(artifact),
                        }
                    ],
                }
                manifest["identity"] = canonical_identity(manifest).uri
                self.write_record(artifact.parent / "manifest.json", manifest)
                return subprocess.CompletedProcess(argv, 0)

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch("literate_ai.project_releases.subprocess.run", side_effect=run),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(len(invocations), 1)
                self.assertEqual(
                    checked["artifacts"]["files"][0]["identity"],
                    file_identity(artifact),
                )
                SchemaCatalog(V2_ROOT).validate(
                    checked["schema"],
                    {key: value for key, value in checked.items() if key != "record"},
                )
                artifact.write_bytes(b"substitute data")
                with self.assertRaises(ProjectReleaseError) as raised:
                    publish_release(root, prepared_path, authorize_external_write=True)
                self.assertEqual(raised.exception.code, "release.artifacts_invalid")
                self.assertEqual(self.git(root, "tag", "--list"), "")
                self.assertEqual(self.git(remote, "tag", "--list"), "")
                artifact.write_bytes(b"qualified bytes")
                receipt = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/tags/v1.2.4^{}"),
                    receipt["revision"],
                )
                self.assertEqual(len(invocations), 1, "publication must not rebuild")

    def test_release_reauthenticates_current_receipt_before_tagging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root, remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text())
            identity = "sha256:" + "a" * 64
            policy["authenticated_receipt"] = {
                "plan_path": "verification/plan.json",
                "plan_identity": identity,
                "policy_path": "verification/policy.json",
                "policy_identity": identity,
                "revocations_path": "verification/revocations.json",
                "bundle_store": "verification/bundle",
                "stores": [
                    {
                        "id": "local",
                        "kind": "filesystem",
                        "location": "verification/source",
                    }
                ],
            }
            self.write_record(policy_path, policy)
            self.git(root, "add", "literate.release.json")
            self.git(root, "commit", "-m", "Require authenticated receipt")
            self.git(root, "push", "origin", "main")
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.adapters.release_evidence.verify_release_current_evidence",
                    return_value=identity,
                ) as verify,
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                verify.side_effect = ValueError("unsigned receipt despite passing gate")
                with self.assertRaises(ProjectReleaseError):
                    check_release(root, plan_path, output=prepared_path)
                self.assertFalse(prepared_path.exists())
                verify.side_effect = None
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["authenticated_receipt_identity"], identity)
                SchemaCatalog(V2_ROOT).validate(
                    checked["schema"],
                    {key: value for key, value in checked.items() if key != "record"},
                )
                for refusal in (
                    [ValueError("revoked")],
                    [identity, ValueError("revoked before tag")],
                    ["sha256:" + "b" * 64],
                ):
                    verify.side_effect = refusal
                    with self.assertRaises(ProjectReleaseError):
                        publish_release(
                            root, prepared_path, authorize_external_write=True
                        )
                    self.assertEqual(self.git(root, "tag", "--list"), "")
                    self.assertEqual(self.git(remote, "tag", "--list"), "")
                verify.side_effect = [
                    identity,
                    identity,
                    ValueError("revoked before push"),
                ]
                with self.assertRaises(ProjectReleaseError):
                    publish_release(root, prepared_path, authorize_external_write=True)
                self.assertEqual(self.git(remote, "tag", "--list"), "")
                self.assertEqual(self.git(root, "tag", "--list"), "v1.2.4")
                verify.side_effect = None
                result = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
                self.assertEqual(
                    self.git(remote, "rev-parse", "refs/tags/v1.2.4^{}"),
                    result["revision"],
                )

    def test_prepare_rolls_back_when_one_declared_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                originals = {
                    path: (root / path).read_text(encoding="utf-8")
                    for path in ("project.json", "version.py", "CHANGELOG.md")
                }
                actual_write = project_releases._atomic_write_text
                calls = 0

                def fail_second(path: Path, text: str) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise OSError("simulated replacement failure")
                    actual_write(path, text)

                with patch(
                    "literate_ai.project_releases._atomic_write_text",
                    side_effect=fail_second,
                ):
                    with self.assertRaises(ProjectReleaseError) as raised:
                        prepare_release(root, plan_path)
                self.assertEqual(raised.exception.code, "release.prepare_write_failed")
                for path, original in originals.items():
                    self.assertEqual(
                        (root / path).read_text(encoding="utf-8"), original
                    )
                self.assertEqual(self.git(root, "status", "--porcelain"), "")

    def test_publish_rejects_a_diverged_remote_before_creating_a_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                check_release(root, plan_path, output=prepared_path)

                intruder = parent / "intruder"
                self.git(parent, "clone", str(remote), str(intruder))
                self.git(intruder, "config", "user.email", "intruder@example.invalid")
                self.git(intruder, "config", "user.name", "Remote Writer")
                (intruder / "remote-change.txt").write_text(
                    "diverged\n", encoding="utf-8"
                )
                self.git(intruder, "add", "remote-change.txt")
                self.git(intruder, "commit", "-m", "Advance remote independently")
                self.git(intruder, "push", "origin", "main")

                with self.assertRaises(ProjectReleaseError) as raised:
                    publish_release(
                        root,
                        prepared_path,
                        authorize_external_write=True,
                    )
                self.assertEqual(
                    raised.exception.code, "release.remote_branch_conflict"
                )
                self.assertEqual(self.git(root, "tag", "--list", "v1.2.4"), "")

    def test_publish_rejects_a_conflicting_remote_tag_before_local_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                check_release(root, plan_path, output=prepared_path)

                intruder = parent / "tagger"
                self.git(parent, "clone", str(remote), str(intruder))
                self.git(intruder, "config", "user.email", "tagger@example.invalid")
                self.git(intruder, "config", "user.name", "Remote Tagger")
                self.git(intruder, "tag", "-a", "v1.2.4", "-m", "Conflicting tag")
                self.git(intruder, "push", "origin", "refs/tags/v1.2.4")

                with self.assertRaises(ProjectReleaseError) as raised:
                    publish_release(
                        root,
                        prepared_path,
                        authorize_external_write=True,
                    )
                self.assertEqual(raised.exception.code, "release.remote_tag_conflict")
                self.assertEqual(self.git(root, "tag", "--list", "v1.2.4"), "")


class ReleaseBackportTests(unittest.TestCase):
    """litai release backport / backport-status: cherry-pick onto a release branch.

    See docs/history/roadmap/release-branching-model.md's "fix-forward-then-cherry-pick"
    model: a fix lands on the trunk branch first, then gets replayed onto an
    already-cut (or post-facto, tag-rooted) release branch.
    """

    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def initialize(self, parent: Path) -> Path:
        root = parent / "project"
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.email", "release@example.invalid")
        self.git(root, "config", "user.name", "Release Test")
        (root / "literate.project.json").write_text("{}\n", encoding="utf-8")
        (root / "file.txt").write_text("base\n", encoding="utf-8")
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        self.git(root, "tag", "v1.0.0")
        return root

    def commit_file(self, root: Path, name: str, content: str, message: str) -> str:
        (root / name).write_text(content, encoding="utf-8")
        self.git(root, "add", name)
        self.git(root, "commit", "-m", message)
        return self.git(root, "rev-parse", "HEAD")

    def test_backport_conflict_leaves_branch_and_checkout_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self.initialize(parent)
            self.git(root, "branch", "release/1.0.x", "v1.0.0")
            self.commit_file(root, "file.txt", "diverged-on-branch\n", "Diverge")
            self.git(root, "checkout", "release/1.0.x")
            self.git(root, "checkout", "main")
            conflicting = self.commit_file(
                root, "file.txt", "conflicting-on-main\n", "Conflicting change"
            )
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    project_releases.backport_commits(
                        root,
                        commits=(conflicting,),
                        to_branch="release/1.0.x",
                        create_from=None,
                    )
            self.assertEqual(raised.exception.code, "release.backport_conflict")
            self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")
            status = self.git(root, "status", "--porcelain")
            self.assertEqual(status, "")


if __name__ == "__main__":
    unittest.main()
