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
from literate_ai.adapters.execution_dispatch import ExecutionDispatchAdapterError
from literate_ai.adapters.live_test_selection import LiveTestSelection
from literate_ai.adapters.models.coding_cli import CodingCliError
from literate_ai.adapters.ssh_transport import SshProcessResult, SshTransportError
from literate_ai.contracts import (
    CiExecutionMode,
    CiTarget,
    CiTargetPreference,
    ExecutionRequirements,
    ExecutionWorker,
    ExecutionWorkerCatalog,
    ExecutionWorkerKind,
)
from literate_ai.evidence_ledger import latest_run, load_run, open_run
from literate_ai.project_releases import (
    LEGACY_PREPARED_RELEASE_SCHEMA,
    PREPARED_RELEASE_SCHEMA,
    PUBLISHED_VERIFICATION_SCHEMA,
    ProjectReleaseError,
    ReleasePolicy,
    advance_default_branch_version,
    check_release,
    create_release_plan,
    prepare_release,
    publish_release,
    release_state,
    set_release_state,
    verify_published_release,
)
from tests.unit.test_schema_catalog import V2_ROOT, SchemaCatalog


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

    def write_v2_policy(self, root: Path) -> None:
        self.write_policy(root)
        path = root / "literate.release.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy.update(
            {
                "schema": "literate-ai/release-policy@2",
            }
        )
        path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")

    def test_release_policy_does_not_duplicate_repository_state(self) -> None:
        policy = json.loads(
            (Path(__file__).resolve().parents[2] / "literate.release.json").read_text()
        )
        first = ReleasePolicy.from_dict(policy)
        encoded = first.to_dict()
        self.assertNotIn("default_branch", encoded)
        self.assertNotIn("main_state", encoded)
        self.assertNotIn("pre_release_version", encoded)

    def test_v2_policy_rejects_duplicated_repository_state(self) -> None:
        policy = json.loads(
            (Path(__file__).resolve().parents[2] / "literate.release.json").read_text()
        )
        policy["main_state"] = "free"
        with self.assertRaisesRegex(ProjectReleaseError, "unknown main_state"):
            ReleasePolicy.from_dict(policy)

    def test_release_state_is_read_only_and_state_set_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "project"
            root.mkdir()
            source_manifest = (
                Path(__file__).resolve().parents[2] / "literate.project.json"
            )
            (root / "literate.project.json").write_bytes(source_manifest.read_bytes())
            (root / "literate.release.json").write_bytes(
                (
                    Path(__file__).resolve().parents[2] / "literate.release.json"
                ).read_bytes()
            )
            (root / "README.md").write_text(
                "# Project\n\n## Release Engineers\n\n- `release-admin`\n",
                encoding="utf-8",
            )
            self.git(root, "init", "-b", "main")
            self.git(root, "config", "user.email", "release@example.invalid")
            self.git(root, "config", "user.name", "Release Test")
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "configure release state")
            from literate_ai.projects import ProjectConfigurationStore

            store = ProjectConfigurationStore(root)
            snapshot = store.read()
            repository_policy = replace(
                snapshot.definition.repository_policy,
                main_state=type(snapshot.definition.repository_policy.main_state)(
                    "pre-release"
                ),
                pre_release_version="1.2",
                release_engineer_source="README.md#release-engineers",
            )
            store.update(
                snapshot,
                replace(snapshot.definition, repository_policy=repository_policy),
            )
            before = (root / "literate.release.json").read_bytes()
            status = release_state(root)
            self.assertEqual(status["main_state"], "pre-release")
            self.assertTrue(status["writable"])
            self.assertEqual((root / "literate.release.json").read_bytes(), before)
            authenticated = subprocess.CompletedProcess(
                ("gh",), 0, stdout="release-admin\n", stderr=""
            )
            with patch(
                "literate_ai.project_releases.subprocess.run",
                return_value=authenticated,
            ):
                updated = set_release_state(
                    root,
                    mode="free",
                    pre_release_version=None,
                    actor="release-admin",
                )
            self.assertEqual(updated["main_state"], "free")
            persisted = (
                ProjectConfigurationStore(root).read().definition.repository_policy
            )
            self.assertEqual(persisted.main_state.value, "free")
            self.assertIsNone(persisted.pre_release_version)

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

    def test_release_engineer_source_fragment_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "MAINTAINERS.md").write_text(
                "# Maintainers\n\n### Shipping Team\n\n"
                "- `release-admin`\n\n### Other\n",
                encoding="utf-8",
            )
            project = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(
                    repository_policy=SimpleNamespace(
                        release_engineer_source="MAINTAINERS.md#shipping-team"
                    )
                ),
            )
            with patch(
                "literate_ai.project_releases.discover_project", return_value=project
            ):
                self.assertEqual(
                    project_releases._release_engineers(root), ("release-admin",)
                )

    def test_rc_syntax_is_scheme_specific_and_ci_workflow_is_forwarded(self) -> None:
        semver = project_releases._canonical_version("1.2.3-rc.1", "semver")
        pep440 = project_releases._canonical_version("1.2.3rc1", "pep440")
        self.assertEqual(semver, "1.2.3-rc.1")
        self.assertEqual(pep440, "1.2.3rc1")
        with patch(
            "literate_ai.project_releases.subprocess.run",
            return_value=subprocess.CompletedProcess(("gh",), 0, "[]", ""),
        ) as run:
            project_releases._github_run_matches(
                Path("."), "example/project", "main", "a" * 40, "Release CI"
            )
        self.assertIn("--workflow", run.call_args.args[0])
        self.assertIn("Release CI", run.call_args.args[0])

    def test_guarded_release_pr_merge_checks_base_trailer_and_green_checks(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[2]
        metadata = {
            "number": 42,
            "state": "OPEN",
            "isDraft": False,
            "baseRefName": "release/0.8.x",
            "headRefOid": "a" * 40,
            "body": "Fix\n\nRelease-Line: release/0.8.x",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}],
        }
        responses = [
            subprocess.CompletedProcess(("gh",), 0, "jordanhubbard\n", ""),
            subprocess.CompletedProcess(("gh",), 0, json.dumps(metadata), ""),
            subprocess.CompletedProcess(("gh",), 0, "", ""),
        ]
        with patch(
            "literate_ai.project_releases.subprocess.run", side_effect=responses
        ) as run:
            result = project_releases.merge_release_pull_request(
                root,
                number=42,
                actor="jordanhubbard",
                authorize_external_write=True,
            )
        self.assertEqual(result["base"], "release/0.8.x")
        merge_argv = run.call_args_list[-1].args[0]
        self.assertIn("--match-head-commit", merge_argv)
        self.assertIn("--merge", merge_argv)

    def test_release_class_comes_from_highest_stable_tag(self) -> None:
        self.assertEqual(
            project_releases._derived_release_class(
                "0.10.0", ("0.8.3", "0.9.0"), scheme="pep440"
            ),
            ("minor", "0.9.0"),
        )
        self.assertEqual(
            project_releases._derived_release_class(
                "1.0.0rc1", ("0.9.0",), scheme="pep440"
            ),
            ("major", "0.9.0"),
        )
        self.assertEqual(
            project_releases._derived_release_class(
                "1.0.1", ("1.0.0", "1.0.1rc1"), scheme="pep440"
            ),
            ("patch", "1.0.0"),
        )

    def test_required_collateral_binds_local_remote_and_release_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.git(root, "init", "-b", "main")
            self.git(root, "config", "user.email", "release@example.invalid")
            self.git(root, "config", "user.name", "Release Test")
            presentation = root / "docs" / "deck.pptx"
            narrative = root / "docs" / "narrative.docx"
            presentation.parent.mkdir()
            presentation.write_bytes(b"presentation")
            narrative.write_bytes(b"narrative")
            build = root / "_build"
            build.mkdir()
            report = {"schema": "pair@1", "accepted": True}
            (build / "acceptance.json").write_text(json.dumps(report), encoding="utf-8")
            self.write_policy(root)
            policy_data = json.loads(
                (root / "literate.release.json").read_text(encoding="utf-8")
            )
            policy_data["collateral"] = [
                {
                    "kind": "document-pair",
                    "name": "overview",
                    "ecosystem": "google-workspace",
                    "required_for": ["minor", "major"],
                    "artifacts": {
                        "presentation": "docs/deck.pptx",
                        "narrative": "docs/narrative.docx",
                    },
                    "verification_report": "_build/acceptance.json",
                    "publication_receipt": "_build/publication.json",
                    "resources": {
                        "presentation": "slides-resource-123",
                        "narrative": "document-resource-123",
                    },
                }
            ]
            (root / "literate.release.json").write_text(
                json.dumps(policy_data), encoding="utf-8"
            )
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "collateral")
            revision = self.git(root, "rev-parse", "HEAD")
            source = {
                "presentation": {
                    "path": "docs/deck.pptx",
                    "sha256": project_releases._sha256_file(presentation),
                },
                "narrative": {
                    "path": "docs/narrative.docx",
                    "sha256": project_releases._sha256_file(narrative),
                },
            }
            receipt: dict[str, object] = {
                "schema": project_releases.DOCUMENT_PAIR_PUBLICATION_SCHEMA,
                "release_version": "1.3.0",
                "source_revision": revision,
                "account": "release@example.com",
                "source": source,
                "members": {
                    "presentation": {
                        "id": "slides-resource-123",
                        "updated": True,
                    },
                    "narrative": {
                        "id": "document-resource-123",
                        "updated": True,
                    },
                },
                "exports": {
                    "presentation": {"sha256": "a" * 64},
                    "narrative": {"sha256": "b" * 64},
                },
                "complete": True,
            }
            receipt["identity"] = project_releases.canonical_identity(receipt).uri
            self.write_record(build / "publication.json", receipt)
            policy = ReleasePolicy.from_dict(policy_data)
            summary = project_releases._required_release_collateral(
                root,
                policy,
                version="1.3.0",
                release_class="minor",
                revision=revision,
            )
            self.assertEqual(summary[0]["source_revision"], revision)
            self.assertEqual(
                summary[0]["artifacts"]["presentation"]["resource_id"],
                "slides-resource-123",
            )
            presentation.write_bytes(b"changed")
            with self.assertRaises(ProjectReleaseError) as stale:
                project_releases._required_release_collateral(
                    root,
                    policy,
                    version="1.3.0",
                    release_class="minor",
                    revision=revision,
                )
            self.assertEqual(
                stale.exception.code, "release.collateral_publication_mismatch"
            )

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

    def test_write_transaction_rolls_back_exact_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.txt"
            second = root / "second.txt"
            first.write_bytes(b"first\r\n")
            second.write_bytes(b"second\n")
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
                with self.assertRaises(ProjectReleaseError):
                    project_releases._write_transaction(
                        ((first, "changed first\n"), (second, "changed second\n"))
                    )

            self.assertEqual(first.read_bytes(), b"first\r\n")
            self.assertEqual(second.read_bytes(), b"second\n")

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

    def test_prepare_updates_cargo_authority_and_selected_lock_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            cargo = root / "Cargo.toml"
            cargo.write_text('[workspace.package]\nversion = "1.2.3"\n')
            lock = root / "Cargo.lock"
            original = (
                "# generated lockfile\r\nversion = 4\r\n\r\n"
                '[[package]]\r\nname = "app-client"\r\n'
                'version = "1.2.3" # client\r\n\r\n'
                '[[package]]\r\nname = "dependency"\r\nversion = "1.2.3"\r\n'
                'source = "registry+https://example.test/index"\r\n\r\n'
                '[[package]]\r\nname = "app-server"\r\nversion = "1.2.3"\r\n'
                'dependencies = ["app-client", "dependency"]\r\n'
            )
            lock.write_bytes(original.encode())
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text())
            policy["version_mirrors"].append(policy["version_authority"])
            policy["version_authority"] = {
                "path": "Cargo.toml",
                "format": "quoted-constant",
                "selectors": ["version"],
            }
            policy["version_mirrors"].append(
                {
                    "path": "Cargo.lock",
                    "format": "cargo-lock-package",
                    "selectors": ["app-server", "app-client"],
                }
            )
            SchemaCatalog(V2_ROOT).validate("literate-ai/release-policy@1", policy)
            policy_path.write_text(json.dumps(policy))
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "Bind Cargo versions")
            self.git(root, "push", "origin", "main")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepared = prepare_release(root, plan_path)
            expected = original.replace(
                'version = "1.2.3" # client', 'version = "1.2.4" # client'
            )
            expected = expected.replace(
                'name = "app-server"\r\nversion = "1.2.3"',
                'name = "app-server"\r\nversion = "1.2.4"',
            )
            self.assertEqual(lock.read_bytes(), expected.encode())
            self.assertIn('version = "1.2.4"', cargo.read_text())
            self.assertIn("Cargo.lock", prepared["changed_paths"])
            self.assertEqual(
                project_releases.current_release_version(
                    root, ReleasePolicy.from_dict(policy)
                ),
                "1.2.4",
            )
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "Prepare Cargo release")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json"
                )
            self.assertEqual(checked["gate"]["exit_status"], 0)

    def test_cargo_lock_bindings_reject_missing_ambiguous_sourced_or_malformed_packages(
        self,
    ) -> None:
        base = 'version = 4\n[[package]]\nname = "app"\nversion = "1.2.3"\n'
        cases = {
            "missing": base.replace('name = "app"', 'name = "other"'),
            "ambiguous": base + '[[package]]\nname = "app"\nversion = "1.2.2"\n',
            "sourced": base + 'source = "registry+https://example.test/index"\n',
            "malformed": base + 'version = "duplicate key"\n',
            "wrong-shape": "package = [1]\n",
            "integer-dependencies": base + "dependencies = 42\n",
            "string-dependencies": base + 'dependencies = "app 1.2.3"\n',
            "table-dependencies": base + 'dependencies = { app = "1.2.3" }\n',
            "non-string-dependency": base + "dependencies = [42]\n",
            "invalid-cargo-version": base.replace('"1.2.3"', '"1.2.3rc1"'),
            "qualified-reference": base
            + '[[package]]\nname = "consumer"\nversion = "1.2.3"\n'
            'dependencies = ["app 1.2.3"]\n',
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = project_releases.VersionBinding.from_dict(
                {
                    "path": "Cargo.lock",
                    "format": "cargo-lock-package",
                    "selectors": ["app"],
                },
                path="binding",
            )
            for name, text in cases.items():
                with self.subTest(name=name):
                    (root / "Cargo.lock").write_text(text)
                    with self.assertRaises(ProjectReleaseError) as caught:
                        project_releases._render_binding(
                            root,
                            binding,
                            current="1.2.3",
                            replacement="1.2.4",
                            version_scheme="semver",
                        )
                    self.assertEqual(caught.exception.code, "release.binding_invalid")
                    self.assertEqual((root / "Cargo.lock").read_text(), text)
            (root / "Cargo.lock").write_text(base)
            with self.assertRaises(ProjectReleaseError) as caught:
                project_releases._render_binding(
                    root,
                    binding,
                    current="1.2.2",
                    replacement="1.2.4",
                    version_scheme="semver",
                )
            self.assertEqual(caught.exception.code, "release.version_drift")
            self.assertEqual((root / "Cargo.lock").read_text(), base)
            for selectors in (["app", "app"], ["../app"], ["app version"]):
                with (
                    self.subTest(selectors=selectors),
                    self.assertRaises(ProjectReleaseError),
                ):
                    project_releases.VersionBinding.from_dict(
                        {
                            "path": "Cargo.lock",
                            "format": "cargo-lock-package",
                            "selectors": selectors,
                        },
                        path="binding",
                    )

    def test_cargo_mirror_refuses_pep440_prerelease_before_any_release_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text())
            policy["version_scheme"] = "pep440"
            policy["allowed_transitions"] = ["patch", "prerelease"]
            policy["version_mirrors"].append(
                {
                    "path": "Cargo.lock",
                    "format": "cargo-lock-package",
                    "selectors": ["app"],
                }
            )
            policy_path.write_text(json.dumps(policy))
            (root / "Cargo.lock").write_text(
                'version = 4\n[[package]]\nname = "app"\nversion = "1.2.3"\n'
            )
            self.git(root, "add", ".")
            self.git(root, "commit", "-m", "Bind Cargo mirror under PEP 440 policy")
            self.git(root, "push", "origin", "main")
            names = self.git(root, "ls-files").splitlines()
            originals = {name: (root / name).read_bytes() for name in names}
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.2.4rc1"
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                with self.assertRaises(ProjectReleaseError) as raised:
                    prepare_release(root, plan_path)
            self.assertEqual(raised.exception.code, "release.binding_invalid")
            self.assertEqual(self.git(root, "status", "--porcelain"), "")
            for name, content in originals.items():
                self.assertEqual((root / name).read_bytes(), content, name)

    def test_prepare_updates_an_indented_quoted_constant_version_mirror(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            formula = root / "formula.rb"
            formula.write_text(
                "class Formula\n"
                '  RELEASE_VERSION = "1.2.3"\n'
                "  version RELEASE_VERSION\n"
                '  url "https://example.test/v#{RELEASE_VERSION}.tar.gz"\n'
                "end\n",
                encoding="utf-8",
            )
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["version_mirrors"].append(
                {
                    "path": "formula.rb",
                    "format": "quoted-constant",
                    "selectors": ["RELEASE_VERSION"],
                }
            )
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            self.git(root, "add", "formula.rb", "literate.release.json")
            self.git(root, "commit", "-m", "Bind formula version")
            self.git(root, "push", "origin", "main")

            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepared = prepare_release(root, plan_path)

            text = formula.read_text(encoding="utf-8")
            self.assertIn('  RELEASE_VERSION = "1.2.4"', text)
            self.assertIn("version RELEASE_VERSION", text)
            self.assertIn("v#{RELEASE_VERSION}.tar.gz", text)
            self.assertIn("formula.rb", prepared["changed_paths"])

    def test_verify_published_fails_when_the_remote_tag_is_missing(self) -> None:
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
                publish_release(root, prepared_path, authorize_external_write=True)
                self.git(remote, "update-ref", "-d", "refs/tags/v1.2.4")
                with self.assertRaises(ProjectReleaseError) as raised:
                    verify_published_release(root, prepared_path)
                self.assertEqual(raised.exception.code, "release.published_tag_missing")

    def test_verify_published_fails_when_the_github_release_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["provider"] = {
                "kind": "github",
                "repository": "example/literate-ai",
            }
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            self.git(root, "add", "literate.release.json")
            self.git(root, "commit", "-m", "configure GitHub provider")
            self.git(root, "push", "origin", "main")
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
                published = {
                    "url": "https://github.com/example/literate-ai/releases/tag/v1.2.4",
                    "draft": False,
                    "prerelease": False,
                    "notes_present": True,
                    "assets": [],
                }
                with patch(
                    "literate_ai.project_releases._github_release_evidence",
                    return_value=published,
                ):
                    publish_release(root, prepared_path, authorize_external_write=True)
                with patch(
                    "literate_ai.project_releases._github_release_evidence",
                    return_value=None,
                ):
                    with self.assertRaises(ProjectReleaseError) as raised:
                        verify_published_release(root, prepared_path)
                self.assertEqual(
                    raised.exception.code, "release.published_provider_missing"
                )

    def test_stable_github_release_requires_notes_and_stable_state(self) -> None:
        root = Path(__file__).resolve().parents[2]
        base = {
            "url": "https://github.com/example/literate-ai/releases/tag/v1.2.4",
            "draft": False,
            "prerelease": False,
            "notes_present": True,
            "assets": [],
        }
        cases = (
            ({**base, "draft": True}, "release.published_provider_unstable"),
            ({**base, "prerelease": True}, "release.published_provider_unstable"),
            ({**base, "notes_present": False}, "release.published_notes_missing"),
        )
        for evidence, code in cases:
            with (
                self.subTest(code=code, evidence=evidence),
                patch(
                    "literate_ai.project_releases._github_release_evidence",
                    return_value=evidence,
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                project_releases._require_stable_github_release(
                    root,
                    "example/literate-ai",
                    "v1.2.4",
                    version="1.2.4",
                    require_wheel=False,
                )
            self.assertEqual(raised.exception.code, code)

    def test_stable_github_release_requires_nonempty_project_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "example-project"\nversion = "1.2.4"\n',
                encoding="utf-8",
            )
            base = {
                "url": "https://github.com/example/project/releases/tag/v1.2.4",
                "draft": False,
                "prerelease": False,
                "notes_present": True,
                "assets": [],
            }
            with (
                patch(
                    "literate_ai.project_releases._github_release_evidence",
                    return_value=base,
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                project_releases._require_stable_github_release(
                    root,
                    "example/project",
                    "v1.2.4",
                    version="1.2.4",
                    require_wheel=True,
                )
            self.assertEqual(raised.exception.code, "release.published_asset_missing")

            complete = {
                **base,
                "assets": [
                    {
                        "name": "example_project-1.2.4-py3-none-any.whl",
                        "size": 42,
                        "state": "uploaded",
                        "digest": "sha256:" + "a" * 64,
                    }
                ],
            }
            with patch(
                "literate_ai.project_releases._github_release_evidence",
                return_value=complete,
            ):
                self.assertEqual(
                    project_releases._require_stable_github_release(
                        root,
                        "example/project",
                        "v1.2.4",
                        version="1.2.4",
                        require_wheel=True,
                    ),
                    complete,
                )

    def test_prepare_inserts_tag_pinned_readme_link_for_github_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["provider"] = {
                "kind": "github",
                "repository": "example/literate-ai",
            }
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            self.git(root, "add", "literate.release.json")
            self.git(root, "commit", "-m", "configure GitHub provider")
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
            changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
            self.assertIn("## 1.2.4 -", changelog)
            self.assertRegex(changelog, r"(?m)^## 1\.2\.4 - ")
            self.assertNotRegex(changelog, r"(?m)^## \[1\.2\.4\]")
            self.assertIn(
                "[README.md](https://github.com/example/literate-ai/blob/v1.2.4/README.md)",
                changelog,
            )

    def test_check_release_does_not_join_foreign_ambient_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            foreign = parent / "foreign"
            foreign.mkdir()
            ambient = open_run(foreign, operation="ambient")
            self.assertIsNotNone(ambient)
            assert ambient is not None
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
                with patch.dict(
                    project_releases.os.environ,
                    {
                        "LITAI_EVIDENCE_RUN": str(ambient.root),
                        "LITAI_EVIDENCE_PARENT": "",
                    },
                    clear=False,
                ):
                    checked = check_release(
                        root,
                        plan_path,
                        output=parent / "prepared.json",
                    )

            evidence = checked["evidence"]
            self.assertIsInstance(evidence, dict)
            assert isinstance(evidence, dict)
            self.assertEqual(evidence["node_id"], "n0001")
            self.assertNotEqual(evidence["run_id"], ambient.run_id)
            self.assertEqual(latest_run(foreign).run_id, ambient.run_id)
            project_run = load_run(root, str(evidence["run_id"]))
            self.assertIsNotNone(project_run)
            assert project_run is not None
            self.assertEqual(latest_run(root).run_id, project_run.run_id)

    def test_release_check_without_evidence_writes_legacy_record(self) -> None:
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
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                with (
                    patch("literate_ai.project_releases.attach_run", return_value=None),
                    patch("literate_ai.project_releases.open_run", return_value=None),
                ):
                    checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["schema"], LEGACY_PREPARED_RELEASE_SCHEMA)
                self.assertNotIn("evidence", checked)
                record = json.loads(prepared_path.read_text(encoding="utf-8"))
                SchemaCatalog(V2_ROOT).validate(LEGACY_PREPARED_RELEASE_SCHEMA, record)

    def _initialize_with_default_branch(self, parent: Path) -> tuple[Path, Path]:
        root, remote = self.initialize(parent)
        policy_path = root / "literate.release.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["default_branch"] = "main"
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        self.git(root, "add", "literate.release.json")
        self.git(root, "commit", "-m", "declare main as the default branch")
        self.git(root, "push", "origin", "main")
        return root, remote

    def _initialize_v2_minor_release_line(
        self, parent: Path
    ) -> tuple[Path, Path, SimpleNamespace]:
        root, remote = self.initialize(parent)
        self.git(root, "tag", "v1.2.3")
        self.git(root, "push", "origin", "v1.2.3")
        self.write_v2_policy(root)
        (root / "project.json").write_text(
            json.dumps({"version": "1.3.0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "version.py").write_text('VERSION = "1.3.0"\n', encoding="utf-8")
        self.git(root, "add", "literate.release.json", "project.json", "version.py")
        self.git(root, "commit", "-m", "Target 1.3")
        self.git(root, "push", "origin", "main")
        self.git(root, "checkout", "-b", "release/1.3.x")
        self.git(root, "push", "-u", "origin", "release/1.3.x")
        project = SimpleNamespace(
            root=root,
            definition=SimpleNamespace(
                repository_policy=SimpleNamespace(
                    default_branch="main",
                    main_state=SimpleNamespace(value="pre-release"),
                    pre_release_version="1.3",
                )
            ),
        )
        return root, remote, project

    def _prepare_v2_minor_release(
        self, parent: Path
    ) -> tuple[Path, Path, SimpleNamespace, Path]:
        root, remote, project = self._initialize_v2_minor_release_line(parent)
        with (
            patch(
                "literate_ai.project_releases.discover_project",
                return_value=project,
            ),
            patch(
                "literate_ai.project_releases._authorize",
                return_value={
                    "actor": "release-admin",
                    "mode": "strict",
                    "reason": None,
                },
            ),
        ):
            plan = create_release_plan(
                root, transition="explicit", explicit_version="1.3.0"
            )
            plan_path = parent / "plan.json"
            self.write_record(plan_path, plan)
            prepare_release(root, plan_path)
        self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
        self.git(root, "commit", "-m", "Prepare 1.3.0")
        return root, remote, project, plan_path

    def _advance_remote_main_after_minor_preparation(self, root: Path) -> None:
        self.git(root, "checkout", "main")
        (root / "future.txt").write_text("future\n", encoding="utf-8")
        self.git(root, "add", "future.txt")
        self.git(root, "commit", "-m", "Advance main after release preparation")
        self.git(root, "push", "origin", "main")
        self.git(root, "checkout", "release/1.3.x")

    def test_first_minor_plan_rejects_a_release_line_behind_remote_main(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote, project = self._initialize_v2_minor_release_line(parent)
            self.git(root, "checkout", "main")
            (root / "approved.txt").write_text("approved\n", encoding="utf-8")
            self.git(root, "add", "approved.txt")
            self.git(root, "commit", "-m", "Land approved 1.3 work")
            self.git(root, "push", "origin", "main")
            self.git(root, "checkout", "release/1.3.x")

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                create_release_plan(
                    root, transition="explicit", explicit_version="1.3.0"
                )

            self.assertEqual(
                raised.exception.code, "release.release_line_behind_default"
            )

    def test_first_minor_prepare_rechecks_remote_main_after_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote, project = self._initialize_v2_minor_release_line(parent)
            with patch(
                "literate_ai.project_releases.discover_project", return_value=project
            ):
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.3.0"
                )
            plan_path = parent / "plan.json"
            self.write_record(plan_path, plan)

            self.git(root, "checkout", "main")
            (root / "late.txt").write_text("late approved work\n", encoding="utf-8")
            self.git(root, "add", "late.txt")
            self.git(root, "commit", "-m", "Land late approved 1.3 work")
            self.git(root, "push", "origin", "main")
            self.git(root, "checkout", "release/1.3.x")

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases._authorize",
                    return_value={
                        "actor": "release-admin",
                        "mode": "strict",
                        "reason": None,
                    },
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                prepare_release(root, plan_path)

            self.assertEqual(
                raised.exception.code, "release.release_line_behind_default"
            )
            self.assertEqual(self.git(root, "status", "--porcelain"), "")

    def test_first_minor_check_rechecks_remote_main_after_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote, project, plan_path = self._prepare_v2_minor_release(parent)
            self._advance_remote_main_after_minor_preparation(root)

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases._authorize",
                    return_value={
                        "actor": "release-admin",
                        "mode": "strict",
                        "reason": None,
                    },
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                check_release(root, plan_path, output=parent / "prepared.json")

            self.assertEqual(
                raised.exception.code, "release.release_line_behind_default"
            )

    def test_first_minor_publish_rechecks_remote_main_after_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote, project, plan_path = self._prepare_v2_minor_release(parent)
            prepared_path = parent / "prepared.json"
            authorization = {
                "actor": "release-admin",
                "mode": "strict",
                "reason": None,
            }
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases._authorize",
                    return_value=authorization,
                ),
            ):
                check_release(root, plan_path, output=prepared_path)
            self._advance_remote_main_after_minor_preparation(root)

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases._authorize",
                    return_value=authorization,
                ),
                self.assertRaises(ProjectReleaseError) as raised,
            ):
                publish_release(
                    root,
                    prepared_path,
                    authorize_external_write=True,
                )

            self.assertEqual(
                raised.exception.code, "release.release_line_behind_default"
            )
            self.assertEqual(self.git(remote, "tag", "--list", "v1.3.0"), "")

    def test_published_minor_remains_verifiable_after_main_advances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote, project, plan_path = self._prepare_v2_minor_release(parent)
            prepared_path = parent / "prepared.json"
            authorization = {
                "actor": "release-admin",
                "mode": "strict",
                "reason": None,
            }
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases._authorize",
                    return_value=authorization,
                ),
            ):
                check_release(root, plan_path, output=prepared_path)
                published = publish_release(
                    root,
                    prepared_path,
                    authorize_external_write=True,
                )
            self._advance_remote_main_after_minor_preparation(root)

            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=project,
            ):
                verified = verify_published_release(root, prepared_path)

            self.assertEqual(verified["revision"], published["revision"])
            self.assertEqual(verified["tag"], "v1.3.0")

    def test_patch_plan_may_exclude_unrelated_newer_main_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote, project = self._initialize_v2_minor_release_line(parent)
            self.git(root, "tag", "v1.3.0")
            self.git(root, "push", "origin", "v1.3.0")
            self.git(root, "checkout", "main")
            (root / "future.txt").write_text("future\n", encoding="utf-8")
            self.git(root, "add", "future.txt")
            self.git(root, "commit", "-m", "Land future work")
            self.git(root, "push", "origin", "main")
            self.git(root, "checkout", "release/1.3.x")

            with patch(
                "literate_ai.project_releases.discover_project", return_value=project
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )

            self.assertEqual(plan["release_class"], "patch")
            self.assertEqual(plan["next_version"], "1.3.1")

    def test_publish_does_not_advance_the_default_branch_when_published_elsewhere(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.3.x")
            self.git(root, "push", "-u", "origin", "release/1.3.x")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.3.0"
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.3.0")
                prepared_path = parent / "prepared.json"
                check_release(root, plan_path, output=prepared_path)
                receipt = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
            self.assertEqual(receipt["tag"], "v1.3.0")
            self.assertIsNone(receipt["default_branch_advance"])
            remote_project = json.loads(
                self.git(remote, "show", "refs/heads/main:project.json")
            )
            self.assertEqual(remote_project["version"], "1.2.3")
            # The release branch itself was untouched by the advance.
            self.assertEqual(
                self.git(remote, "show", "refs/heads/release/1.3.x:project.json"),
                self.git(root, "show", "HEAD:project.json"),
            )

    def test_publish_never_mutates_an_already_ahead_default_branch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.3.x")
            self.git(root, "push", "-u", "origin", "release/1.3.x")
            self.git(root, "checkout", "main")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                # main independently advances past what the release branch
                # is about to publish -- nothing for the automatic step to do.
                (root / "project.json").write_text(
                    json.dumps({"version": "2.0.0"}, indent=2) + "\n",
                    encoding="utf-8",
                )
                (root / "version.py").write_text(
                    'VERSION = "2.0.0"\n', encoding="utf-8"
                )
                self.git(root, "add", "project.json", "version.py")
                self.git(root, "commit", "-m", "Advance main to 2.0.0")
                self.git(root, "push", "origin", "main")

                self.git(root, "checkout", "release/1.3.x")
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.3.0"
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.3.0")
                prepared_path = parent / "prepared.json"
                check_release(root, plan_path, output=prepared_path)
                receipt = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
            self.assertIsNone(receipt["default_branch_advance"])
            self.assertEqual(
                json.loads(self.git(remote, "show", "refs/heads/main:project.json"))[
                    "version"
                ],
                "2.0.0",
            )

    def test_plan_on_default_branch_records_create_when_the_line_is_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
            self.assertEqual(plan["source_branch"], "main")
            self.assertEqual(
                plan["release_line"],
                {"name": "release/1.2.x", "create": True},
            )
            SchemaCatalog(V2_ROOT).validate("literate-ai/release-plan@1", plan)

    def test_plan_rejects_default_branch_when_the_release_line_already_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.2.x")
            self.git(root, "checkout", "main")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    create_release_plan(root, transition="patch", explicit_version=None)
            self.assertEqual(
                raised.exception.code, "release.default_branch_cut_forbidden"
            )
            self.assertIn("release/1.2.x", raised.exception.message)

    def test_plan_rejects_default_branch_when_the_line_exists_only_on_the_remote(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.2.x")
            self.git(root, "push", "-u", "origin", "release/1.2.x")
            self.git(root, "checkout", "main")
            self.git(root, "branch", "-d", "release/1.2.x")
            self.git(root, "update-ref", "-d", "refs/remotes/origin/release/1.2.x")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    create_release_plan(root, transition="patch", explicit_version=None)
            self.assertEqual(
                raised.exception.code, "release.default_branch_cut_forbidden"
            )

    def test_plan_rejects_a_per_patch_release_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.2.4")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    create_release_plan(root, transition="patch", explicit_version=None)
            self.assertEqual(raised.exception.code, "release.branch_not_release_line")
            self.assertIn("release/1.2.x", raised.exception.message)

    def test_plan_accepts_the_matching_release_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            self.git(root, "checkout", "-b", "release/1.2.x")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
            self.assertEqual(plan["source_branch"], "release/1.2.x")
            self.assertEqual(plan["next_version"], "1.2.4")
            self.assertEqual(
                plan["release_line"],
                {"name": "release/1.2.x", "create": False},
            )
            SchemaCatalog(V2_ROOT).validate("literate-ai/release-plan@1", plan)

    def test_prepare_creates_and_checks_out_the_release_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, remote = self._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                self.assertEqual(
                    plan["release_line"],
                    {"name": "release/1.2.x", "create": True},
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepared_change = prepare_release(root, plan_path)
            self.assertEqual(prepared_change["branch"], "release/1.2.x")
            self.assertTrue(prepared_change["release_line_created"])
            self.assertEqual(
                self.git(root, "symbolic-ref", "--short", "HEAD"),
                "release/1.2.x",
            )
            self.assertEqual(
                json.loads((root / "project.json").read_text())["version"],
                "1.2.4",
            )
            self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
            self.git(root, "commit", "-m", "Prepare 1.2.4")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                prepared_path = parent / "prepared.json"
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["branch"], "release/1.2.x")
                receipt = publish_release(
                    root, prepared_path, authorize_external_write=True
                )
            self.assertEqual(receipt["tag"], "v1.2.4")
            self.assertEqual(
                self.git(remote, "rev-parse", "--abbrev-ref", "HEAD"),
                "main",
            )
            # The cut was pushed; main may have been patch-advanced past 1.2.4.
            self.assertTrue(
                self.git(remote, "rev-parse", "--verify", "refs/heads/release/1.2.x")
            )

    def test_prepare_refuses_to_create_when_the_line_appears_after_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
            self.git(root, "checkout", "-b", "release/1.2.x")
            self.git(root, "checkout", "main")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    prepare_release(root, plan_path)
            self.assertEqual(raised.exception.code, "release.branch_exists")
            self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")

    def test_prepare_unwinds_a_new_line_when_writes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self._initialize_with_default_branch(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                with patch(
                    "literate_ai.project_releases._write_transaction",
                    side_effect=ProjectReleaseError(
                        "release.prepare_failed", "simulated write failure"
                    ),
                ):
                    with self.assertRaises(ProjectReleaseError) as raised:
                        prepare_release(root, plan_path)
            self.assertEqual(raised.exception.code, "release.prepare_failed")
            self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")
            completed = subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/heads/release/1.2.x",
                ),
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_prepare_rejects_dirty_tree_and_backward_explicit_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as backward:
                    create_release_plan(
                        root, transition="explicit", explicit_version="1.2.2"
                    )
                self.assertEqual(backward.exception.code, "release.version_not_forward")
                same = create_release_plan(
                    root, transition="explicit", explicit_version="1.2.3"
                )
                self.assertEqual(same["current_version"], "1.2.3")
                self.assertEqual(same["next_version"], "1.2.3")
                self.git(root, "tag", "v1.2.3")
                with self.assertRaises(ProjectReleaseError) as tagged:
                    create_release_plan(
                        root, transition="explicit", explicit_version="1.2.3"
                    )
                self.assertEqual(tagged.exception.code, "release.tag_exists")
                self.git(root, "tag", "-d", "v1.2.3")
                plan = create_release_plan(
                    root, transition="minor", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                (root / "untracked.txt").write_text("dirty\n", encoding="utf-8")
                with self.assertRaises(ProjectReleaseError) as dirty:
                    prepare_release(root, plan_path)
                self.assertEqual(dirty.exception.code, "release.prepare_dirty")

    def test_current_untagged_release_can_resume_after_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            changelog = root / "CHANGELOG.md"
            changelog.write_text(
                "# Changelog\n\n"
                "## Unreleased\n\n"
                "## 1.2.3 - 2026-08-25\n\n"
                "- Prepared feature.\n",
                encoding="utf-8",
            )
            self.git(root, "add", "CHANGELOG.md")
            self.git(root, "commit", "-m", "Prepare 1.2.3")
            self.git(root, "push", "origin", "main")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.2.3"
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                with patch(
                    "literate_ai.project_releases._atomic_write_text",
                    wraps=project_releases._atomic_write_text,
                ) as atomic_write:
                    prepare_release(root, plan_path)
                self.assertEqual(atomic_write.call_count, 0)
                text = changelog.read_text(encoding="utf-8")
                self.assertEqual(text.count("## 1.2.3 -"), 1)
                self.assertNotIn("https://github.com/", text)
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json"
                )
            self.assertEqual(checked["prepared_revision"], plan["source_revision"])

    def test_check_rejects_unplanned_files_in_prepared_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="major", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                (root / "unrelated.txt").write_text("not release authority\n")
                self.git(root, "add", ".")
                self.git(root, "commit", "-m", "mixed release")
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(root, plan_path, output=parent / "prepared.json")
                self.assertEqual(
                    raised.exception.code, "release.prepared_scope_invalid"
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

    def write_policy_with_documentation_authority(self, root: Path) -> None:
        self.write_policy(root)
        policy_path = root / "literate.release.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["documentation_authority"] = ["AUTHORITY.md"]
        policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
        (root / "AUTHORITY.md").write_text(
            "<!-- literate-ai:authority-reviewed sha256:" + "0" * 64 + " -->\nstale\n",
            encoding="utf-8",
        )

    def test_prepare_refreshes_a_declared_stale_documentation_authority_marker(
        self,
    ) -> None:
        # Bumping the version legitimately changes the declared release
        # authority files, which stales a separately-tracked documentation
        # authority marker (see issue #55). ``prepare_release`` must be able
        # to refresh a marker it explicitly declared in the same pass.
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            self.write_policy_with_documentation_authority(root)
            self.git(root, "add", "literate.release.json", "AUTHORITY.md")
            self.git(root, "commit", "-m", "declare documentation authority")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                expected_marker = (
                    "<!-- literate-ai:authority-reviewed sha256:" + "f" * 64 + " -->"
                )
                with patch(
                    "literate_ai.adapters.project_validation."
                    "FilesystemProjectValidationAdapter.documentation_review",
                    return_value={
                        "state": "stale",
                        "document": "AUTHORITY.md",
                        "expected_marker": expected_marker,
                    },
                ):
                    prepared_change = prepare_release(root, plan_path)
                self.assertIn("AUTHORITY.md", prepared_change["changed_paths"])
                self.assertIn(
                    expected_marker,
                    (root / "AUTHORITY.md").read_text(encoding="utf-8"),
                )

    def test_prepare_fails_closed_on_an_unrecordable_documentation_authority_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            self.write_policy_with_documentation_authority(root)
            self.git(root, "add", "literate.release.json", "AUTHORITY.md")
            self.git(root, "commit", "-m", "declare documentation authority")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                original = (root / "AUTHORITY.md").read_bytes()
                with patch(
                    "literate_ai.adapters.project_validation."
                    "FilesystemProjectValidationAdapter.documentation_review",
                    return_value={
                        "state": "missing",
                        "document": None,
                        "expected_marker": "<!-- literate-ai:authority-reviewed "
                        "sha256:" + "f" * 64 + " -->",
                    },
                ):
                    with self.assertRaises(ProjectReleaseError) as raised:
                        prepare_release(root, plan_path)
                self.assertEqual(
                    raised.exception.code,
                    "release.documentation_authority_unrecordable",
                )
                self.assertEqual((root / "AUTHORITY.md").read_bytes(), original)

    def test_check_release_declares_documentation_authority_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            self.write_policy_with_documentation_authority(root)
            self.git(root, "add", "literate.release.json", "AUTHORITY.md")
            self.git(root, "commit", "-m", "declare documentation authority")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                expected_marker = (
                    "<!-- literate-ai:authority-reviewed sha256:" + "f" * 64 + " -->"
                )
                with patch(
                    "literate_ai.adapters.project_validation."
                    "FilesystemProjectValidationAdapter.documentation_review",
                    return_value={
                        "state": "stale",
                        "document": "AUTHORITY.md",
                        "expected_marker": expected_marker,
                    },
                ):
                    prepare_release(root, plan_path)
                self.git(
                    root,
                    "add",
                    "project.json",
                    "version.py",
                    "CHANGELOG.md",
                    "AUTHORITY.md",
                )
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                prepared_path = parent / "prepared.json"
                # check_release's own scope check (not documentation_review) is
                # what this test exercises: the refreshed marker file must be
                # accepted as part of the prepared commit's declared diff.
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["gate"]["exit_status"], 0)

    def test_check_release_admits_reviewed_marker_for_legacy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            authority = root / "AUTHORITY.md"
            authority.write_text(
                "<!-- literate-ai:authority-reviewed sha256:" + "0" * 64 + " -->\n",
                encoding="utf-8",
            )
            self.git(root, "add", "AUTHORITY.md")
            self.git(root, "commit", "-m", "add legacy authority marker")
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
                self.git(
                    root,
                    "add",
                    "project.json",
                    "version.py",
                    "CHANGELOG.md",
                )
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                authority.write_text(
                    "<!-- literate-ai:authority-reviewed sha256:" + "f" * 64 + " -->\n",
                    encoding="utf-8",
                )
                self.git(root, "add", "AUTHORITY.md")
                self.git(root, "commit", "-m", "Review release authority")
                with patch(
                    "literate_ai.adapters.project_validation."
                    "FilesystemProjectValidationAdapter.documentation_review",
                    return_value={
                        "state": "current",
                        "document": "AUTHORITY.md",
                    },
                ):
                    checked = check_release(
                        root, plan_path, output=parent / "prepared.json"
                    )
                self.assertEqual(checked["gate"]["exit_status"], 0)

    def test_check_release_declares_the_project_test_receipt_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            (root / "verification").mkdir()
            (root / "verification" / "current.json").write_text(
                '{"state": "stale"}\n', encoding="utf-8"
            )
            self.git(root, "add", "verification/current.json")
            self.git(root, "commit", "-m", "add a placeholder test receipt")
            project_double = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(test_receipt="verification/current.json"),
            )
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=project_double,
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                # The refreshed receipt binds to the exact prepared revision, so
                # it can only be rewritten after the prepare commit -- this must
                # be accepted as scope even though it is not a declared authority
                # path, or every project with a test-receipt policy could never
                # pass a fresh check_release right after prepare.
                (root / "verification" / "current.json").write_text(
                    '{"state": "current"}\n', encoding="utf-8"
                )
                self.git(root, "add", "verification/current.json")
                self.git(root, "commit", "-m", "Refresh the test receipt")
                prepared_path = parent / "prepared.json"
                checked = check_release(root, plan_path, output=prepared_path)
                self.assertEqual(checked["gate"]["exit_status"], 0)

    def test_check_release_still_rejects_undeclared_scope_creep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            project_double = SimpleNamespace(
                root=root,
                definition=SimpleNamespace(test_receipt="verification/current.json"),
            )
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=project_double,
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.2.4")
                (root / "unrelated.txt").write_text("scope creep\n", encoding="utf-8")
                self.git(root, "add", "unrelated.txt")
                self.git(root, "commit", "-m", "an undeclared change")
                prepared_path = parent / "prepared.json"
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(root, plan_path, output=prepared_path)
                self.assertEqual(
                    raised.exception.code, "release.prepared_scope_invalid"
                )

    def test_pep440_prerelease_is_classified_and_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["version_scheme"] = "pep440"
            policy["allowed_transitions"] = ["patch", "prerelease"]
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            self.git(root, "add", "literate.release.json")
            self.git(root, "commit", "-m", "select PEP 440 releases")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="explicit", explicit_version="1.2.4rc1"
                )
                self.assertEqual(plan["transition"], "prerelease")
                self.assertEqual(plan["next_version"], "1.2.4rc1")
                with self.assertRaises(ProjectReleaseError) as noncanonical:
                    create_release_plan(
                        root, transition="explicit", explicit_version="1.2.4RC1"
                    )
                self.assertEqual(noncanonical.exception.code, "release.binding_invalid")

    def test_advance_default_branch_version_advances_past_the_highest_released_tag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            # A release cut on a dedicated branch from a tag, never merged
            # back -- the checked-out branch never saw this commit or tag.
            self.git(root, "tag", "v1.5.0")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                result = advance_default_branch_version(
                    root, branch="main", transition="minor", explicit_version=None
                )
            self.assertEqual(result["schema"], "literate-ai/release-branch-advance@1")
            self.assertEqual(result["branch"], "main")
            self.assertEqual(result["previous_version"], "1.2.3")
            self.assertEqual(result["highest_released_version"], "1.5.0")
            self.assertEqual(result["next_version"], "1.6.0")
            self.assertEqual(
                json.loads((root / "project.json").read_text())["version"], "1.6.0"
            )
            self.assertEqual(
                (root / "version.py").read_text(encoding="utf-8"),
                'VERSION = "1.6.0"\n',
            )
            # Files only -- no commit, no tag, no push.
            status = self.git(root, "status", "--porcelain")
            self.assertIn("project.json", status)
            self.assertIn("version.py", status)
            self.assertEqual(self.git(root, "tag", "--list", "v1.6.0"), "")

    def test_advance_default_branch_version_never_regresses_past_the_branch_own_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="minor", explicit_version=None
                )
                plan_path = parent / "plan.json"
                self.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                self.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
                self.git(root, "commit", "-m", "Prepare 1.3.0")
                self.git(root, "tag", "v1.0.0")
                result = advance_default_branch_version(
                    root, branch="main", transition="patch", explicit_version=None
                )
            self.assertEqual(result["previous_version"], "1.3.0")
            # The tag (v1.0.0) is lower than the branch's own version -- the
            # branch's own version is what the bump is based on, not the tag.
            self.assertEqual(result["highest_released_version"], "1.0.0")
            self.assertEqual(result["next_version"], "1.3.1")

    def test_advance_default_branch_version_requires_a_clean_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            (root / "project.json").write_text("dirty\n", encoding="utf-8")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    advance_default_branch_version(
                        root, branch="main", transition="minor", explicit_version=None
                    )
            self.assertEqual(raised.exception.code, "release.prepare_dirty")

    def test_advance_default_branch_version_requires_the_checked_out_branch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    advance_default_branch_version(
                        root,
                        branch="not-the-checked-out-branch",
                        transition="minor",
                        explicit_version=None,
                    )
            self.assertEqual(raised.exception.code, "release.branch_mismatch")

    def test_advance_default_branch_version_with_no_released_tags_bumps_own_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = self.initialize(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                result = advance_default_branch_version(
                    root, branch="main", transition="patch", explicit_version=None
                )
            self.assertIsNone(result["highest_released_version"])
            self.assertEqual(result["next_version"], "1.2.4")

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


class ReleaseGateEnvironmentTests(unittest.TestCase):
    """The gate must reproduce the same verdict regardless of how it was invoked.

    `make release` needs PYTHONPATH=src to invoke litai from the source tree,
    but that leaking into the gate subprocess made every command the gate
    spawns resolve literate_ai from both the source tree and the installed
    distribution, failing `standard_binding.distribution_ambiguous` on a
    revision that passed a direct `make release-check` (see #65 /
    RELEASE-ENV-001).
    """

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

    def test_release_check_gate_does_not_observe_a_parent_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = ProjectReleaseTests().initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["gate"]["argv"] = [
                sys.executable,
                "-c",
                "import os, sys; sys.exit(1 if 'PYTHONPATH' in os.environ else 0)",
            ]
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            ProjectReleaseTests().git(root, "add", "literate.release.json")
            ProjectReleaseTests().git(
                root, "commit", "-m", "gate asserts no PYTHONPATH"
            )
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch.dict("os.environ", {"PYTHONPATH": "/leaked/from/make/release"}),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                ProjectReleaseTests.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                ProjectReleaseTests().git(
                    root, "add", "project.json", "version.py", "CHANGELOG.md"
                )
                ProjectReleaseTests().git(root, "commit", "-m", "Prepare 1.2.4")
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json"
                )
        self.assertEqual(checked["gate"]["exit_status"], 0)


class ReleaseGateFailureDetailTests(unittest.TestCase):
    """A failed gate must say what it said, not just that it failed."""

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

    def test_failed_gate_includes_stderr_in_the_public_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, _remote = ProjectReleaseTests().initialize(parent)
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["gate"]["argv"] = [
                sys.executable,
                "-c",
                "import sys; print('noise'); "
                "print('GATE-STDERR-UNIQUE', file=sys.stderr); raise SystemExit(2)",
            ]
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            ProjectReleaseTests().git(root, "add", "literate.release.json")
            ProjectReleaseTests().git(root, "commit", "-m", "failing gate")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                plan = create_release_plan(
                    root, transition="patch", explicit_version=None
                )
                plan_path = parent / "plan.json"
                ProjectReleaseTests.write_record(plan_path, plan)
                prepare_release(root, plan_path)
                ProjectReleaseTests().git(
                    root, "add", "project.json", "version.py", "CHANGELOG.md"
                )
                ProjectReleaseTests().git(root, "commit", "-m", "Prepare 1.2.4")
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(root, plan_path, output=parent / "prepared.json")
        self.assertEqual(raised.exception.code, "release.gate_failed")
        self.assertIn("GATE-STDERR-UNIQUE", str(raised.exception))
        self.assertIn("last stderr:", str(raised.exception))

    def test_detail_is_bounded_and_keeps_the_tail(self) -> None:
        completed = subprocess.CompletedProcess([], 2, "", "x" * 10_000 + " FINAL")
        detail = project_releases._release_gate_failure_detail(completed)
        self.assertLessEqual(
            len(detail),
            project_releases._GATE_FAILURE_DETAIL_BYTES + len("last stderr: "),
        )
        self.assertTrue(detail.endswith("FINAL"))


class ReleaseTargetTests(unittest.TestCase):
    """The release gate's execution target must be an explicit, durable choice.

    See RELEASE-005 / issue #58: `--target local` dispatches through the
    configured private worker fleet and fails closed when unconfigured or
    unreachable; `--target github` polls GitHub Actions for the checked
    revision; `--target gitlab` is recognized but rejected until a real
    GitLab provider surface exists; the evidence records which target
    produced a passing gate.
    """

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

    def _prepared(
        self,
        parent: Path,
        *,
        github_repository: str | None = None,
        gate_timeout_seconds: int | None = None,
    ) -> tuple[Path, Path, str]:
        base = ProjectReleaseTests()
        root, _remote = base.initialize(parent)
        if github_repository is not None or gate_timeout_seconds is not None:
            policy_path = root / "literate.release.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            if github_repository is not None:
                policy["provider"] = {
                    "kind": "github",
                    "repository": github_repository,
                }
            if gate_timeout_seconds is not None:
                policy["gate"]["timeout_seconds"] = gate_timeout_seconds
            policy_path.write_text(
                json.dumps(policy, indent=2) + "\n", encoding="utf-8"
            )
            base.git(root, "add", "literate.release.json")
            base.git(root, "commit", "-m", "adjust release policy for this test")
        with patch(
            "literate_ai.project_releases.discover_project",
            return_value=SimpleNamespace(root=root),
        ):
            plan = create_release_plan(root, transition="patch", explicit_version=None)
            plan_path = parent / "plan.json"
            base.write_record(plan_path, plan)
            prepare_release(root, plan_path)
        base.git(root, "add", "project.json", "version.py", "CHANGELOG.md")
        base.git(root, "commit", "-m", "Prepare 1.2.4")
        revision = base.git(root, "rev-parse", "HEAD")
        return root, plan_path, revision

    def _ignored_live_test_config(self, root: Path, payload: dict[str, object]) -> Path:
        exclude = root / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if "literate.test.json" not in current.splitlines():
            exclude.write_text(current + "literate.test.json\n", encoding="utf-8")
        path = root / "literate.test.json"
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    def test_target_local_dispatches_through_the_configured_worker_fleet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(parent)
            worker = ExecutionWorker(
                "worker-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@worker-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((worker,))
            probe = SshProcessResult(0, (revision + "\n").encode("utf-8"), b"")
            gate = SshProcessResult(0, b"gate ok\n", b"")
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    side_effect=[probe, gate],
                ) as run,
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json", target="local"
                )
            self.assertEqual(checked["target"]["kind"], "local")
            self.assertEqual(checked["target"]["excluded_workers"], [])
            self.assertEqual(len(checked["target"]["workers"]), 1)
            worker_result = checked["target"]["workers"][0]
            self.assertEqual(worker_result["worker_id"], "worker-a")
            self.assertEqual(worker_result["endpoint"], worker.endpoint)
            self.assertIsInstance(worker_result["duration_ms"], int)
            self.assertEqual(worker_result["gate"]["exit_status"], 0)
            self.assertEqual(checked["gate"]["exit_status"], 0)
            self.assertEqual(checked["gate"]["worker_count"], 1)
            self.assertEqual(run.call_count, 2)
            probe_argv = run.call_args_list[0].args[0]
            gate_argv = run.call_args_list[1].args[0]
            self.assertIn("git -C", probe_argv[-1])
            self.assertIn("rev-parse HEAD", probe_argv[-1])
            self.assertIn("literate-ai", gate_argv[-1])

    def test_target_local_fans_out_to_every_posix_worker_and_excludes_windows(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(parent)
            posix_a = ExecutionWorker(
                "posix-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@posix-a.example.invalid",
                workspace="~/literate-ai",
            )
            posix_b = ExecutionWorker(
                "posix-b",
                ExecutionWorkerKind.SSH,
                endpoint="release@posix-b.example.invalid",
                workspace="~/literate-ai",
            )
            windows = ExecutionWorker(
                "win-a",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family="windows"),
                endpoint="release@win-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((posix_a, posix_b, windows))
            probe = SshProcessResult(0, (revision + "\n").encode("utf-8"), b"")
            gate = SshProcessResult(0, b"gate ok\n", b"")

            def dispatch(argv, **_kwargs):
                command = argv[-1] if argv else ""
                if "rev-parse HEAD" in str(command):
                    return probe
                return gate

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    side_effect=dispatch,
                ),
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json", target="local"
                )
            self.assertEqual(checked["target"]["kind"], "local")
            dispatched_ids = sorted(
                item["worker_id"] for item in checked["target"]["workers"]
            )
            self.assertEqual(dispatched_ids, ["posix-a", "posix-b"])
            self.assertEqual(
                checked["target"]["excluded_workers"],
                [
                    {
                        "worker_id": "win-a",
                        "endpoint": windows.endpoint,
                        "reason": "release.windows_gate_unsupported",
                    }
                ],
            )
            self.assertEqual(checked["gate"]["worker_count"], 2)

    def test_target_local_fails_closed_when_only_windows_workers_are_configured(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            windows = ExecutionWorker(
                "win-a",
                ExecutionWorkerKind.SSH,
                requirements=ExecutionRequirements(os_family="windows"),
                endpoint="release@win-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((windows,))
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "release.target_unconfigured")

    def test_target_local_names_the_first_failing_worker_deterministically(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(parent)
            posix_a = ExecutionWorker(
                "posix-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@posix-a.example.invalid",
                workspace="~/literate-ai",
            )
            posix_b = ExecutionWorker(
                "posix-b",
                ExecutionWorkerKind.SSH,
                endpoint="release@posix-b.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((posix_a, posix_b))
            probe = SshProcessResult(0, (revision + "\n").encode("utf-8"), b"")
            failing_gate = SshProcessResult(2, b"", b"boom\n")

            def run_for_endpoint(argv, *, cwd, timeout_seconds):
                del cwd, timeout_seconds
                if "git -C" in argv[-1]:
                    return probe
                return failing_gate

            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    side_effect=run_for_endpoint,
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "release.gate_failed")
            self.assertIn("posix-a", raised.exception.message)
            run = latest_run(root)
            self.assertIsNotNone(run)
            assert run is not None
            nodes = {
                item["path"]: item
                for item in run.reduced()["nodes"]
                if isinstance(item, dict)
            }
            failed = nodes["release/target/local/posix-a"]
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(
                (run.root / str(failed["node_id"]) / "gate-stderr.log").is_file()
            )

    def test_target_local_fails_closed_when_worker_fleet_is_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    side_effect=ExecutionDispatchAdapterError(
                        "execution.worker_catalog_unavailable",
                        "worker catalog must be a regular non-symlink file",
                    ),
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "release.target_unconfigured")

    def test_target_local_fails_closed_when_workers_json_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            self.assertFalse((root / "literate.workers.json").is_file())
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "release.target_unconfigured")
            self.assertIn("workers.json", raised.exception.message)

    def test_target_local_fails_closed_when_test_config_names_a_non_opencode_cli(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(parent)
            self._ignored_live_test_config(
                root, {"coding_cli": "claude", "model": "any"}
            )
            worker = ExecutionWorker(
                "worker-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@worker-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((worker,))
            probe = SshProcessResult(0, (revision + "\n").encode("utf-8"), b"")
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    return_value=probe,
                ) as run,
                patch(
                    "literate_ai.project_releases.try_resolve_live_test_selection",
                    side_effect=CodingCliError(
                        "coding_cli.remote_prerequisite",
                        "remote live qualification requires coding CLI opencode",
                    ),
                ),
            ):
                with self.assertRaises(CodingCliError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "coding_cli.remote_prerequisite")
            self.assertIn("opencode", raised.exception.message)
            self.assertEqual(run.call_count, 1)

    def test_target_local_exports_opencode_model_and_remote_gate_without_secrets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(parent)
            self._ignored_live_test_config(
                root, {"coding_cli": "opencode", "model": "openai/gpt-5"}
            )
            worker = ExecutionWorker(
                "worker-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@worker-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((worker,))
            probe = SshProcessResult(0, (revision + "\n").encode("utf-8"), b"")
            gate = SshProcessResult(0, b"gate ok\n", b"")
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    side_effect=[probe, gate],
                ) as run,
                patch(
                    "literate_ai.project_releases.try_resolve_live_test_selection",
                    return_value=LiveTestSelection(
                        "opencode",
                        "openai/gpt-5",
                        "test-config",
                        "test-config",
                    ),
                ),
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json", target="local"
                )
            self.assertEqual(checked["target"]["kind"], "local")
            self.assertEqual(run.call_count, 2)
            remote_command = str(run.call_args_list[1].args[0][-1])
            self.assertIn("export ", remote_command)
            self.assertIn("LITAI_REMOTE_LIVE_GATE=1", remote_command)
            self.assertIn("CODING_CLI=opencode", remote_command)
            self.assertIn("LITAI_LIVE_MODEL=openai/gpt-5", remote_command)
            self.assertNotIn("OPENAI_API_KEY", remote_command)

    def test_target_local_fails_closed_when_worker_is_unreachable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            worker = ExecutionWorker(
                "worker-a",
                ExecutionWorkerKind.SSH,
                endpoint="release@worker-a.example.invalid",
                workspace="~/literate-ai",
            )
            catalog = ExecutionWorkerCatalog((worker,))
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    return_value=catalog,
                ),
                patch.object(
                    project_releases.BoundedSshProcessRunner,
                    "run",
                    side_effect=SshTransportError(
                        "execution.ssh_unavailable",
                        "SSH transport executable is unavailable",
                    ),
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root, plan_path, output=parent / "prepared.json", target="local"
                    )
            self.assertEqual(raised.exception.code, "release.worker_unreachable")

    def test_target_github_polls_to_a_passing_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(
                parent, github_repository="example/literate-ai"
            )
            run = {
                "databaseId": 4242,
                "headSha": revision,
                "status": "completed",
                "conclusion": "success",
                "url": "https://github.com/example/literate-ai/actions/runs/4242",
            }
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases._github_run_matches",
                    return_value=[run],
                ),
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json", target="github"
                )
            self.assertEqual(checked["target"]["kind"], "github")
            self.assertEqual(checked["target"]["run_id"], 4242)
            self.assertEqual(checked["target"]["conclusion"], "success")
            self.assertEqual(checked["gate"]["exit_status"], 0)

    def test_target_github_fails_on_a_non_success_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, revision = self._prepared(
                parent, github_repository="example/literate-ai"
            )
            run = {
                "databaseId": 4243,
                "headSha": revision,
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.com/example/literate-ai/actions/runs/4243",
            }
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases._github_run_matches",
                    return_value=[run],
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root,
                        plan_path,
                        output=parent / "prepared.json",
                        target="github",
                    )
            self.assertEqual(raised.exception.code, "release.gate_failed")

    def test_target_github_times_out_when_no_run_completes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(
                parent, github_repository="example/literate-ai", gate_timeout_seconds=1
            )
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=SimpleNamespace(root=root),
                ),
                patch(
                    "literate_ai.project_releases._github_run_matches",
                    return_value=[],
                ),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root,
                        plan_path,
                        output=parent / "prepared.json",
                        target="github",
                    )
            self.assertEqual(raised.exception.code, "release.gate_timeout")

    def test_target_gitlab_is_rejected_with_a_typed_not_yet_supported_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(
                        root,
                        plan_path,
                        output=parent / "prepared.json",
                        target="gitlab",
                    )
            self.assertEqual(raised.exception.code, "release.target_unsupported")

    def test_declared_ci_targets_preference_is_used_when_no_explicit_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            preference = CiTargetPreference(
                target=CiTarget.LOCAL, mode=CiExecutionMode.SERIAL
            )
            project = SimpleNamespace(
                root=root, definition=SimpleNamespace(ci_targets=(preference,))
            )
            with (
                patch(
                    "literate_ai.project_releases.discover_project",
                    return_value=project,
                ),
                patch(
                    "literate_ai.project_releases.load_execution_worker_catalog",
                    side_effect=ExecutionDispatchAdapterError(
                        "execution.worker_catalog_unavailable",
                        "worker catalog must be a regular non-symlink file",
                    ),
                ),
            ):
                # No --target passed. If the declared ci_targets preference
                # (local) were ignored in favor of the legacy default, this
                # would succeed instead of failing closed on the unconfigured
                # worker fleet.
                with self.assertRaises(ProjectReleaseError) as raised:
                    check_release(root, plan_path, output=parent / "prepared.json")
            self.assertEqual(raised.exception.code, "release.target_unconfigured")

    def test_legacy_default_runs_on_the_invoking_machine_when_undeclared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = self._prepared(parent)
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                checked = check_release(
                    root, plan_path, output=parent / "prepared.json"
                )
            self.assertEqual(checked["target"], {"kind": "legacy-local"})
            self.assertEqual(checked["gate"]["exit_status"], 0)


class ChangelogReleaseNotesTests(unittest.TestCase):
    """See #143: publishing the whole CHANGELOG.md as GitHub release notes made
    every release page open with the permanent, always-empty "Unreleased"
    heading instead of that version's own entries."""

    def git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(root), *arguments),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_missing_version_heading_fails_closed(self) -> None:
        text = "# Changelog\n\n## Unreleased\n\n## 0.5.0 - 2026-08-19\n\n- Entry.\n"
        with self.assertRaises(ProjectReleaseError) as raised:
            project_releases._changelog_release_notes(text, "9.9.9")
        self.assertEqual(raised.exception.code, "release.changelog_invalid")

    def test_github_publish_sends_only_the_new_versions_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root, plan_path, _revision = ReleaseTargetTests()._prepared(
                parent, github_repository="example/literate-ai"
            )
            self.git(root, "push", "origin", "HEAD:main")
            prepared_path = parent / "prepared.json"
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                check_release(root, plan_path, output=prepared_path)

                real_run = subprocess.run
                captured: dict[str, str] = {}
                release_created = False

                def fake_run(argv, **kwargs):
                    nonlocal release_created
                    if argv[0] != "gh":
                        cmd = argv if isinstance(argv, (list, tuple)) else [argv]
                        if any("pip" in str(a) for a in cmd):
                            wheel_dir = None
                            for i, a in enumerate(cmd):
                                if str(a) == "--wheel-dir" and i + 1 < len(cmd):
                                    wheel_dir = cmd[i + 1]
                            if wheel_dir is not None:
                                Path(wheel_dir).mkdir(parents=True, exist_ok=True)
                                (
                                    Path(wheel_dir) / "fake-0.0.0-py3-none-any.whl"
                                ).touch()
                            return subprocess.CompletedProcess(argv, 0, "", "")
                        return real_run(argv, **kwargs)
                    if "--notes-file" in argv:
                        notes_path = argv[argv.index("--notes-file") + 1]
                        captured["notes"] = Path(notes_path).read_text(encoding="utf-8")
                        release_created = True
                        return subprocess.CompletedProcess(
                            argv, 0, "https://github.com/example/literate-ai/x", ""
                        )
                    if "release" in argv and "upload" in argv:
                        return subprocess.CompletedProcess(argv, 0, "", "")
                    if "view" in argv and release_created:
                        return subprocess.CompletedProcess(
                            argv,
                            0,
                            json.dumps(
                                {
                                    "url": "https://github.com/example/literate-ai/x",
                                    "isDraft": False,
                                    "isPrerelease": False,
                                    "body": "release notes",
                                    "assets": [],
                                }
                            ),
                            "",
                        )
                    # First gh release view: no pre-existing release.
                    return subprocess.CompletedProcess(argv, 1, "", "not found")

                with patch(
                    "literate_ai.project_releases.subprocess.run",
                    side_effect=fake_run,
                ):
                    publish_release(root, prepared_path, authorize_external_write=True)

            self.assertIn("- Prepared feature.", captured["notes"])
            self.assertNotIn("Unreleased", captured["notes"])
            self.assertIn(
                "[README.md](https://github.com/example/literate-ai/blob/v1.2.4/README.md)",
                captured["notes"],
            )


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

    def test_backport_creates_branch_from_ref_and_cherry_picks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self.initialize(parent)
            fix = self.commit_file(root, "fix.txt", "fixed\n", "Fix #1")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                result = project_releases.backport_commits(
                    root,
                    commits=(fix,),
                    to_branch="release/1.0.x",
                    create_from="v1.0.0",
                )
            self.assertTrue(result["branch_created"])
            self.assertEqual(len(result["commits"]), 1)
            self.assertEqual(result["commits"][0]["source_commit"], fix)
            self.assertEqual(self.git(root, "show", "release/1.0.x:fix.txt"), "fixed")
            # The current checkout (main) must be untouched by the backport,
            # and its worktree registration cleaned up.
            self.assertEqual(self.git(root, "symbolic-ref", "--short", "HEAD"), "main")
            self.assertEqual(self.git(root, "worktree", "list").count("\n"), 0)

    def test_backport_requires_create_from_when_branch_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self.initialize(parent)
            fix = self.commit_file(root, "fix.txt", "fixed\n", "Fix #1")
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                with self.assertRaises(ProjectReleaseError) as raised:
                    project_releases.backport_commits(
                        root,
                        commits=(fix,),
                        to_branch="release/1.0.x",
                        create_from=None,
                    )
            self.assertEqual(raised.exception.code, "release.backport_branch_missing")

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

    def test_backport_status_lists_pending_commits_since_the_branch_cut(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = self.initialize(parent)
            self.git(root, "branch", "release/1.0.x", "v1.0.0")
            src_fix = self.commit_file(
                root, "src_file.txt", "fix\n", "Fix touching src"
            )
            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                status = project_releases.backport_status(
                    root, branch="release/1.0.x", against="main"
                )
            self.assertEqual(status["pending_commit_count"], 1)
            self.assertEqual(status["pending_commits"][0]["commit"], src_fix)

            with patch(
                "literate_ai.project_releases.discover_project",
                return_value=SimpleNamespace(root=root),
            ):
                project_releases.backport_commits(
                    root,
                    commits=(src_fix,),
                    to_branch="release/1.0.x",
                    create_from=None,
                )
                status_after = project_releases.backport_status(
                    root, branch="release/1.0.x", against="main"
                )
            self.assertEqual(status_after["pending_commit_count"], 0)


if __name__ == "__main__":
    unittest.main()
