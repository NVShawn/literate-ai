"""Actual public retained run, nonexecuting promotion and separate receipt update.

The editable-source distribution observation is explicit. Installed-wheel and
requesting consumer qualification remain separate required evidence.
"""

import hashlib
import io
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    InstalledFrameworkDistributionMember,
    resolve_standard_project_lifecycle_driver,
)
from literate_ai.cli import main
from literate_ai.contracts import (
    BlobRef,
    ProjectSourceIntelligencePolicy,
    ProjectTestReceiptFinalizedCandidate,
    ProjectTestReceiptPolicy,
    SourceIntelligenceArtifactPublication,
    SourceIntelligenceMode,
    SourceIntelligenceStage,
    StandardProjectLifecycleDriver,
    canonical_json_bytes,
    load_current_standard_lifecycle_policy,
)
from literate_ai.storage.cas import FileSystemCAS
from tests.support.fixtures_public_retained_promotion import (
    author_public_retained_project,
    forge_public_run_evidence,
    refresh_public_retained_manifest,
)


def snapshot(root, *, physical=False, exclude=()):
    if not os.path.lexists(root):
        return None
    result = {}
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        for path in root.rglob("*"):
            if len(paths) >= 32768:
                raise AssertionError("bounded native fixture snapshot required")
            paths.append(path)
    total = 0
    for path in sorted(paths):
        if any(
            path == excluded or path.is_relative_to(excluded) for excluded in exclude
        ):
            continue
        node = path.lstat()
        value = (node.st_mode, node.st_size)
        if physical:
            value += (
                node.st_dev,
                node.st_ino,
                node.st_uid,
                node.st_gid,
                node.st_nlink,
                node.st_mtime_ns,
                node.st_ctime_ns,
            )
        if path.is_symlink():
            value += (os.readlink(path),)
        elif path.is_file():
            total += node.st_size
            if node.st_size > 64 * 1024**2 or total > 512 * 1024**2:
                raise AssertionError("bounded native fixture bytes required")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    node.st_dev,
                    node.st_ino,
                    node.st_size,
                ):
                    raise AssertionError("fixture snapshot source changed")
                digest, count = hashlib.sha256(), 0
                while count <= opened.st_size:
                    block = os.read(fd, min(65536, opened.st_size - count + 1))
                    if not block:
                        break
                    digest.update(block)
                    count += len(block)
                after = os.fstat(fd)
                if count != opened.st_size or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise AssertionError("fixture snapshot bytes changed")
                value += (digest.hexdigest(),)
            finally:
                os.close(fd)
        result[path.relative_to(root).as_posix()] = value
    return result


@unittest.skipUnless(os.name != "nt" and shutil.which("cc"), "native POSIX C required")
class RetainedReceiptPromotionTests(unittest.TestCase):
    def test_public_original_run_promotion_update_and_bounded_refusals(self):
        root = Path(tempfile.mkdtemp(prefix="retained receipt public ")).resolve()
        passed = False
        evidence = root / "evidence"
        evidence.mkdir()
        calls = []
        distribution = InstalledFrameworkDistribution(
            "literate-ai",
            "1.4.0",
            (
                InstalledFrameworkDistributionMember(
                    "literate_ai/__init__.py",
                    1,
                    "sha256:" + "a" * 64,
                ),
            ),
        )
        standard_policy = load_current_standard_lifecycle_policy()
        driver = StandardProjectLifecycleDriver(
            distribution.identity, standard_policy.identity
        )
        receipt = ProjectTestReceiptPolicy(
            standard_policy.policy_id,
            standard_policy.policy_version,
            driver.identity,
            standard_policy.required_evidence_kinds,
            standard_policy.minimum_test_count,
        )
        environment = dict(os.environ)
        for name in tuple(environment):
            if name.startswith(("LITAI_", "GIT_")) or name in {
                "PYTHONPATH",
                "PYTHONHOME",
            }:
                environment.pop(name)
        environment.update(
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "BUILD_DIR": str(root / "build-cache"),
                "OBJ_DIR": str(root / "objects"),
            }
        )

        def invoke(arguments, *, allow_output_effects=False, readonly_plan=False):
            promotion = arguments[:3] == ["project", "test-receipt", "promote-retained"]
            guarded = promotion or readonly_plan
            protected_roots = [
                root / name
                for name in (
                    "project",
                    "config",
                    "state",
                    "cache",
                    "build-cache",
                    "objects",
                )
            ]
            protected_before = (
                {str(path): snapshot(path, physical=True) for path in protected_roots}
                if guarded
                else None
            )
            all_before = (
                snapshot(root, physical=True, exclude=(evidence,)) if guarded else None
            )
            output, errors = io.StringIO(), io.StringIO()
            status = None
            calls.append(tuple(arguments))
            record = evidence / f"call-{len(calls):02}"
            try:
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "literate_ai.cli.rebuild.resolve_standard_project_lifecycle_driver",
                        lambda value: resolve_standard_project_lifecycle_driver(
                            value,
                            distribution_observer=lambda: distribution,
                        ),
                    ),
                    redirect_stdout(output),
                    redirect_stderr(errors),
                ):
                    status = main(["--json", *arguments])
            finally:
                record.with_suffix(".stdout").write_text(output.getvalue())
                record.with_suffix(".stderr").write_text(errors.getvalue())
                record.with_suffix(".json").write_text(
                    json.dumps(
                        {
                            "arguments": arguments,
                            "actual_exit": status,
                        }
                    )
                )
            if guarded:
                if readonly_plan or "--dry-run" in arguments or status != 0:
                    self.assertEqual(
                        {
                            str(path): snapshot(path, physical=True)
                            for path in protected_roots
                        },
                        protected_before,
                        "promotion changed protected project/CAS/cache/config/state",
                    )
                if (
                    readonly_plan
                    or "--dry-run" in arguments
                    or status != 0
                    and not allow_output_effects
                ):
                    self.assertEqual(
                        snapshot(root, physical=True, exclude=(evidence,)),
                        all_before,
                        "readonly promotion changed files outside raw evidence",
                    )
            raw = output.getvalue() or errors.getvalue()
            self.assertTrue(raw, (status, arguments))
            return status, json.loads(raw)

        try:
            project, policy = author_public_retained_project(
                root,
                invoke,
                driver=driver,
                receipt=receipt,
            )
            selectors = [
                "--project",
                str(project),
                "--retained-project",
                "manifest.json",
                "--retained-project-profile",
                "build/profile.json",
            ]
            before_plan = snapshot(project)
            status, envelope = invoke(
                ["build", *selectors, "--retained-project-plan"], readonly_plan=True
            )
            self.assertEqual(status, 0, envelope)
            self.assertEqual(snapshot(project), before_plan)
            for abbreviation in ("--retained-project-pla", "--retained-project-pl"):
                with self.subTest(readonly_plan_spelling=abbreviation):
                    status, abbreviated = invoke(
                        ["build", *selectors, abbreviation], readonly_plan=True
                    )
                    self.assertEqual(status, 0, abbreviated)
                    self.assertEqual(
                        abbreviated["result"].get("plan_identity")
                        or abbreviated["result"]["authorization_identity"],
                        envelope["result"].get("plan_identity")
                        or envelope["result"]["authorization_identity"],
                    )
            for spelling in (
                "--retained-project-plan",
                "--retained-project-pla",
                "--retained-project-pl",
            ):
                for forbidden in (
                    ["--discover-mcps"],
                    ["--debug", str(root / "forbidden-plan-debug.txt")],
                ):
                    with self.subTest(
                        readonly_plan_spelling=spelling, forbidden=forbidden
                    ):
                        status, refusal = invoke(
                            ["build", *selectors, spelling, *forbidden],
                            readonly_plan=True,
                        )
                        self.assertNotEqual(status, 0, refusal)
                        self.assertEqual(
                            refusal["error"]["code"], "orchestration.read_only_options"
                        )
            plan = envelope["result"]
            authorization = plan.get("plan_identity") or plan["authorization_identity"]
            status, envelope = invoke(
                [
                    "build",
                    *selectors,
                    "--authorize-retained-project",
                    authorization,
                    "--allow-host-execution",
                    "--retain-promotion-evidence",
                ]
            )
            self.assertEqual(status, 0, envelope)
            run = envelope["result"]
            self.assertTrue(run["admitted"])
            self.assertFalse(run["package_qualified"])
            self.assertFalse(run["release_authorized"])
            self.assertEqual(run["retained_receipt"]["summary"]["passed"], 1)
            bundle = BlobRef.from_dict(run["promotion_evidence"])
            candidate = root / "candidate.json"
            arguments = [
                "project",
                "test-receipt",
                "promote-retained",
                str(candidate),
                *selectors,
                "--custody-root",
                run["custody_root"],
                "--retained-receipt",
                run["retained_receipt_identity"],
                "--promotion-evidence",
                bundle.identity,
            ]
            # Only the true external metadata boundary is a fixture: it reports
            # a version, but any attempt to initialize/synchronize is observable.
            # No index/currentness facts or live owners are supplied by this tool.
            metadata_tools = root / "metadata-tools"
            metadata_tools.mkdir()
            mutation = evidence / "forbidden-source-intelligence-sync"
            command = metadata_tools / "litai-retained-metadata-probe"
            command.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then printf "1.1.1\\n"; exit 0; fi\n'
                f": > {shlex.quote(str(mutation))}\nexit 73\n"
            )
            command.chmod(0o755)
            source_policy = ProjectSourceIntelligencePolicy(
                provider_id="codegraph-cli",
                command=command.name,
                minimum_version="1.1.1",
                artifact_path=".codegraph/codegraph.db",
                stages=tuple(
                    (
                        stage,
                        SourceIntelligenceMode.REQUIRED
                        if stage is SourceIntelligenceStage.PROJECT_MAINTENANCE
                        else SourceIntelligenceMode.OFF,
                    )
                    for stage in SourceIntelligenceStage
                ),
                artifact_publication=(
                    SourceIntelligenceArtifactPublication.METADATA_ONLY
                ),
            )
            configuration_path = project / "literate.project.json"
            original_configuration = configuration_path.read_bytes()
            original_path = environment.get("PATH", "")
            try:
                configured = json.loads(original_configuration)
                configured["source_intelligence"] = source_policy.to_dict()
                configuration_path.write_bytes(canonical_json_bytes(configured))
                environment["PATH"] = str(metadata_tools) + os.pathsep + original_path
                status, reviewed = invoke(
                    ["project", "documentation-review", str(project), "--record"]
                )
                self.assertEqual(status, 0, reviewed)
                self.assertFalse((project / ".codegraph").exists())
                status, missing_index = invoke([*arguments, "--dry-run"])
                self.assertNotEqual(status, 0, missing_index)
                self.assertEqual(
                    missing_index["error"]["code"],
                    "project.source_intelligence_missing",
                )
                self.assertFalse(mutation.exists())
                self.assertFalse((project / ".codegraph").exists())
                self.assertFalse(candidate.exists())
            finally:
                configuration_path.write_bytes(original_configuration)
                environment["PATH"] = original_path
                status, reviewed = invoke(
                    ["project", "documentation-review", str(project), "--record"]
                )
                self.assertEqual(status, 0, reviewed)
            before = snapshot(project)
            status, envelope = invoke([*arguments, "--dry-run"])
            self.assertEqual(status, 0, envelope)
            self.assertFalse(envelope["result"]["written"])
            self.assertFalse(candidate.exists())
            self.assertEqual(snapshot(project), before)
            for suffix, code in (
                (
                    ["--promotion-maximum-metadata-bytes", "1"],
                    "retained_promotion.run_unqualified",
                ),
                (
                    ["--promotion-maximum-memory-bytes", "1"],
                    "retained_promotion.run_unqualified",
                ),
                (
                    ["--promotion-maximum-payload-bytes", "1"],
                    "retained_promotion.run_unqualified",
                ),
            ):
                with self.subTest(budget=suffix):
                    status, refused = invoke([*arguments, "--dry-run", *suffix])
                    self.assertNotEqual(status, 0, refused)
                    self.assertEqual(refused["error"]["code"], code)
                    self.assertFalse(candidate.exists())
                    self.assertEqual(snapshot(project), before)
            store = FileSystemCAS(Path(run["custody_root"]), create=False)
            for kind, required_message in (
                ("report", "retained.unit.test-report-content-differs"),
                ("nonobject-record", "retained.unit.canonical-run-document-required"),
                ("stdout", "retained.completion.acceptance-observation-mismatch"),
                ("stderr", "retained.completion.acceptance-observation-mismatch"),
            ):
                with self.subTest(coherent_forgery=kind):
                    forged_receipt, forged_bundle = forge_public_run_evidence(
                        store,
                        run["retained_receipt"],
                        bundle,
                        kind=kind,
                    )
                    hostile = list(arguments)
                    hostile[hostile.index("--retained-receipt") + 1] = (
                        forged_receipt.identity
                    )
                    hostile[hostile.index("--promotion-evidence") + 1] = (
                        forged_bundle.identity
                    )
                    status, refused = invoke([*hostile, "--dry-run"])
                    self.assertNotEqual(status, 0, refused)
                    self.assertEqual(
                        refused["error"]["code"], "retained_promotion.run_unqualified"
                    )
                    self.assertIn(required_message, refused["error"]["message"])
                    self.assertFalse(candidate.exists())
                    self.assertEqual(snapshot(project), before)
            from literate_ai.contracts.execution_dispatch import (
                ExecutionWorker,
                ExecutionWorkerCatalog,
                ExecutionWorkerKind,
            )

            remote = ExecutionWorker(
                "unexecuted-remote",
                ExecutionWorkerKind.SSH,
                endpoint="fixture@unused.invalid",
                workspace="~/retained-promotion-unused",
            )
            catalog = root / "unexecuted-workers.json"
            catalog.write_bytes(
                canonical_json_bytes(ExecutionWorkerCatalog((remote,)).to_dict())
            )
            profile_path = project / "build/profile.json"
            old_profile, old_manifest = (
                profile_path.read_bytes(),
                (project / "manifest.json").read_bytes(),
            )
            changed_profile = json.loads(old_profile)
            changed_profile["worker_identity"] = remote.identity.uri
            try:
                profile_path.write_bytes(canonical_json_bytes(changed_profile))
                refresh_public_retained_manifest(project)
                status, recorded = invoke(
                    ["project", "documentation-review", str(project), "--record"]
                )
                self.assertEqual(status, 0, recorded)
                before_remote = snapshot(project)
                status, refused = invoke(
                    [
                        *arguments,
                        "--dry-run",
                        "--worker",
                        remote.worker_id,
                        "--worker-config",
                        str(catalog),
                    ]
                )
                self.assertNotEqual(status, 0, refused)
                self.assertEqual(refused["error"]["code"], "retained_promotion.invalid")
                self.assertIn(
                    "authenticated-remote-ingress-required", refused["error"]["message"]
                )
                self.assertEqual(snapshot(project), before_remote)
                self.assertFalse(candidate.exists())
            finally:
                profile_path.write_bytes(old_profile)
                (project / "manifest.json").write_bytes(old_manifest)
                status, recorded = invoke(
                    ["project", "documentation-review", str(project), "--record"]
                )
                self.assertEqual(status, 0, recorded)
            self.assertEqual(snapshot(project), before)
            unsafe_parent = root / "foreign-writable-output-parent"
            unsafe_parent.mkdir()
            unsafe_parent.chmod(0o777)
            unsafe_arguments = list(arguments)
            unsafe_arguments[3] = str(unsafe_parent / "candidate.json")
            status, refused = invoke(unsafe_arguments)
            self.assertNotEqual(status, 0, refused)
            self.assertEqual(
                refused["error"]["code"], "project.test_receipt_candidate_write_failed"
            )
            self.assertEqual(list(unsafe_parent.iterdir()), [])
            self.assertEqual(snapshot(project), before)
            race_parent = root / "external-publication-parent"
            race_parent.mkdir(mode=0o700)
            retained_parent = root / "retained-external-publication-parent"
            raced = []
            original_write = os.write

            def redirect_actual_stage(descriptor, content):
                # Synchronize at the actual external OS write boundary. Every
                # write still calls the genuine native operation on its original
                # FD; no publisher, owner or observation is replaced.
                if not raced:
                    node = os.fstat(descriptor)
                    for stage in race_parent.glob(
                        ".litai-retained-stage-*/.litai-retained-candidate-*"
                    ):
                        if (stage.stat().st_dev, stage.stat().st_ino) == (
                            node.st_dev,
                            node.st_ino,
                        ):
                            race_parent.rename(retained_parent)
                            race_parent.symlink_to(project, target_is_directory=True)
                            raced.append(True)
                            break
                return original_write(descriptor, content)

            raced_arguments = list(arguments)
            raced_arguments[3] = str(race_parent / "raced-candidate.json")
            with patch("os.write", side_effect=redirect_actual_stage):
                status, refused = invoke(raced_arguments, allow_output_effects=True)
            self.assertEqual(raced, [True])
            self.assertNotEqual(status, 0, refused)
            self.assertEqual(
                refused["error"]["code"], "project.test_receipt_candidate_write_failed"
            )
            self.assertEqual(snapshot(project), before)
            self.assertFalse((project / "raced-candidate.json").exists())
            self.assertTrue(
                list(
                    retained_parent.glob(
                        ".litai-retained-stage-*/.litai-retained-candidate-*"
                    )
                )
            )
            # Preserve both the interrupted original namespace and redirected
            # foreign target for evidence; do not repair paths to make a rerun pass.
            status, envelope = invoke(arguments)
            self.assertEqual(status, 0, envelope)
            self.assertTrue(envelope["result"]["written"])
            saved = candidate.read_bytes()
            final = json.loads(saved)
            finalized = ProjectTestReceiptFinalizedCandidate.from_dict(final)
            self.assertEqual(finalized.to_dict(), final)
            receipt_document = finalized.receipt
            for count in ("passed", "failed", "skipped"):
                self.assertEqual(
                    getattr(receipt_document.summary, count),
                    run["retained_receipt"]["summary"][count],
                )
            self.assertEqual(receipt_document.summary.total, 1)
            self.assertEqual(receipt_document.summary.passed, 1)
            self.assertEqual(
                finalized.identity.uri, envelope["result"]["candidate_identity"]
            )
            self.assertEqual(
                finalized.receipt_identity.uri, envelope["result"]["receipt_identity"]
            )
            self.assertEqual(
                envelope["result"]["tests_passed"], receipt_document.summary.passed
            )
            evidence_kinds = {item.kind for item in receipt_document.evidence}
            self.assertTrue(set(policy.required_evidence_kinds) <= evidence_kinds)
            self.assertEqual(snapshot(project), before)
            status, refused = invoke(arguments)
            self.assertNotEqual(status, 0, refused)
            self.assertEqual(candidate.read_bytes(), saved)
            self.assertEqual(snapshot(project), before)
            sgid_parent = root / "setgid-output-parent"
            sgid_parent.mkdir(mode=0o700)
            sgid_parent.chmod(0o2700)
            sgid_arguments = list(arguments)
            sgid_arguments[3] = str(sgid_parent / "candidate.json")
            status, sgid_result = invoke(sgid_arguments)
            self.assertEqual(status, 0, sgid_result)
            self.assertEqual((sgid_parent / "candidate.json").read_bytes(), saved)
            private_wrappers = list(sgid_parent.glob(".litai-retained-stage-*"))
            self.assertEqual(len(private_wrappers), 1)
            self.assertEqual(stat.S_IMODE(sgid_parent.stat().st_mode), 0o2700)
            wrapper_mode = stat.S_IMODE(private_wrappers[0].stat().st_mode)
            if sys.platform.startswith("linux"):
                self.assertEqual(wrapper_mode, 0o2700)
            else:
                self.assertIn(wrapper_mode, {0o700, 0o2700})
            self.assertEqual(list(private_wrappers[0].iterdir()), [])
            self.assertEqual(snapshot(project), before)
            status, updated = invoke(
                [
                    "project",
                    "test-receipt",
                    "update",
                    str(candidate),
                    "--project",
                    str(project),
                ]
            )
            self.assertEqual(status, 0, updated)
            self.assertTrue(updated["result"]["updated"])
            self.assertEqual(
                updated["result"]["identity"], finalized.receipt_identity.uri
            )
            self.assertEqual(
                updated["result"]["finalized_candidate_identity"],
                finalized.identity.uri,
            )
            status, checked = invoke(
                [
                    "project",
                    "test-receipt",
                    "check",
                    str(candidate),
                    "--project",
                    str(project),
                ]
            )
            self.assertEqual(status, 0, checked)
            self.assertTrue(checked["result"]["matched"])
            self.assertEqual(
                checked["result"]["identity"], finalized.receipt_identity.uri
            )
            self.assertEqual(candidate.read_bytes(), saved)
            passed = True
        finally:
            if passed:
                shutil.rmtree(root)
            else:
                print(
                    f"Retained failed public promotion fixture: {root}", file=sys.stderr
                )
