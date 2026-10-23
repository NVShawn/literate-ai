"""Reviewed scope changes must invalidate the plan; data cannot grant access."""

import hashlib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.retained_project_execution import (
    RetainedProjectExecutionWorkspace,
)
from literate_ai.contracts import canonical_identity
from literate_ai.contracts.retained_project import (
    RetainedProjectLimits,
    RetainedProjectManifest,
    RetainedProjectMember,
)
from literate_ai.contracts.retained_project_lifecycle import RetainedProjectProfile
from literate_ai.contracts.retained_secondary_access import (
    RetainedReadOnlyView,
    RetainedSecondaryAccessPolicy,
)
from literate_ai.contracts.retained_test_identity import RetainedSecondaryTestIdentity
from tests.support.fixtures_retained_project_sbom import retained_project_sbom_fixture


class RetainedSecondaryAccessTests(unittest.TestCase):
    def setUp(self):
        self.plan, _ = retained_project_sbom_fixture()
        self.policy = RetainedSecondaryAccessPolicy(
            self.plan.profile.worker_identity,
            RetainedSecondaryTestIdentity("litaitest", 2001, 2001, (2001,)),
            ("test",),
            (
                RetainedReadOnlyView("runtime", "inputs", "sdk"),
                RetainedReadOnlyView("products", "exports", "app"),
            ),
            ("work",),
        )

    def test_wire_roundtrip_and_nested_profile_identity(self):
        profile = replace(self.plan.profile, secondary_access=self.policy)
        self.assertEqual(RetainedProjectProfile.from_dict(profile.to_dict()), profile)
        self.assertEqual(
            RetainedSecondaryAccessPolicy.from_dict(self.policy.to_dict()), self.policy
        )
        self.assertNotEqual(profile.identity, self.plan.profile.identity)
        plan = replace(self.plan, profile=profile)
        self.assertNotEqual(plan.identity, self.plan.identity)
        with self.assertRaises(ValueError):
            plan.require_authorization(self.plan.identity.uri)
        for policy in (
            replace(self.policy, fixtures=("scratch",)),
            replace(
                self.policy, account=replace(self.policy.account, groups=(2001, 2002))
            ),
            replace(
                self.policy, views=(replace(self.policy.views[0], prefix="other"),)
            ),
            replace(
                self.policy, views=(replace(self.policy.views[1], name="binaries"),)
            ),
        ):
            self.assertNotEqual(
                replace(profile, secondary_access=policy).identity, profile.identity
            )

    def test_absent_policy_preserves_legacy_wire_and_identity(self):
        profile = self.plan.profile
        wire = profile.to_dict()
        self.assertNotIn("secondary_access", wire)
        self.assertEqual(RetainedProjectProfile.from_dict(wire).to_dict(), wire)
        self.assertEqual(
            replace(profile, secondary_access=None).identity, profile.identity
        )

    def test_config_cannot_claim_provisioning_or_choose_observer_endpoint(self):
        for key, value in (
            ("disposable", True),
            ("allocation", "lease"),
            ("observer", "/tmp/socket"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                RetainedSecondaryAccessPolicy.from_dict(
                    self.policy.to_dict() | {key: value}
                )

    def test_worker_mismatch_and_non_test_or_missing_actions_refuse(self):
        for policy in (
            replace(self.policy, worker_identity=canonical_identity("another worker")),
            replace(self.policy, action_ids=("build",)),
            replace(self.policy, action_ids=("accept",)),
            replace(self.policy, action_ids=("missing",)),
        ):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                replace(self.plan.profile, secondary_access=policy)

    def test_export_view_requires_build_producer(self):
        policy = replace(
            self.policy, views=(RetainedReadOnlyView("products", "exports", "unbuilt"),)
        )
        with self.assertRaisesRegex(ValueError, "build-producer-required"):
            replace(self.plan.profile, secondary_access=policy)

    def test_names_cannot_alias_views_or_fixture_paths(self):
        for name in (
            "/tmp/work",
            "../work",
            "runtime/child",
            "Runtime",
            "work\n",
            "x" * 64,
            "con",
            "aux",
            "com1",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace(self.policy, fixtures=(name,))
        for fixtures in (("runtime",), ("work", "work")):
            with self.assertRaisesRegex(ValueError, "namespace-collision"):
                replace(self.policy, fixtures=fixtures)
        with self.assertRaisesRegex(ValueError, "namespace-collision"):
            replace(self.policy, views=(self.policy.views[0], self.policy.views[0]))

    def test_views_reject_escaping_roots_aliases_and_overlapping_sources(self):
        for prefix in (".", "/", "../sdk", "sdk/../other", "sdk//bin", "sdk\\bin"):
            with (
                self.subTest(prefix=prefix),
                self.assertRaises((TypeError, ValueError)),
            ):
                RetainedReadOnlyView("runtime", "inputs", prefix)
        for prefix in ("sdk", "SDK", "sdk/bin"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                replace(
                    self.policy,
                    views=(
                        self.policy.views[0],
                        RetainedReadOnlyView("alias", "inputs", prefix),
                    ),
                )
        with self.assertRaises(ValueError):
            RetainedReadOnlyView("runtime", "host", "etc")

    def test_collections_are_bounded_typed_and_nonempty(self):
        for field in ("action_ids", "views", "fixtures"):
            for value in ((), [], tuple(range(33))):
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    replace(self.policy, **{field: value})
        with self.assertRaises(ValueError):
            replace(self.policy, action_ids=("test", "test"))
        with self.assertRaises(TypeError):
            replace(self.policy, views=({},))
        with self.assertRaises(TypeError):
            replace(self.policy, account=self.policy.account.to_dict())
        with self.assertRaises(TypeError):
            replace(self.plan.profile, secondary_access=self.policy.to_dict())

    def test_explicit_empty_or_null_wire_policy_is_not_legacy_absence(self):
        for value in (None, {}, [], False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RetainedProjectProfile.from_dict(
                    self.plan.profile.to_dict() | {"secondary_access": value}
                )
        for key in ("action_ids", "views", "fixtures"):
            with self.assertRaises(ValueError):
                RetainedSecondaryAccessPolicy.from_dict(
                    self.policy.to_dict() | {key: ()}
                )

    def test_unintegrated_workspace_refuses_before_touching_filesystem(self):
        plan = replace(
            self.plan, profile=replace(self.plan.profile, secondary_access=self.policy)
        )
        with (
            patch("pathlib.Path.mkdir") as mkdir,
            self.assertRaisesRegex(ValueError, "provisioning-owner-required"),
        ):
            RetainedProjectExecutionWorkspace(
                Path("missing-source"),
                None,
                plan,
                parent=Path("missing-parent"),
                current_plan=lambda: plan,
                authorization=plan.identity.uri,
                allow_host_execution=True,
                test_receipt_policy=None,
            )
        mkdir.assert_not_called()

    def manifest(self, target):
        return RetainedProjectManifest(
            ("sdk",),
            (
                RetainedProjectMember(
                    "sdk/private/file", 0, 0o644, hashlib.sha256(b"").hexdigest(), "sdk"
                ),
                RetainedProjectMember(
                    "sdk/runtime/alias",
                    len(target),
                    0o120000,
                    hashlib.sha256(target.encode()).hexdigest(),
                    "sdk",
                    target,
                ),
                RetainedProjectMember(
                    "sdk/runtime/file", 0, 0o644, hashlib.sha256(b"").hexdigest(), "sdk"
                ),
            ),
            RetainedProjectLimits(100, 4096, 4096),
            ("sdk", "sdk/private", "sdk/runtime"),
        )

    def test_view_binding_rejects_cross_scope_alias_and_escape_then_return(self):
        view = RetainedReadOnlyView("runtime", "inputs", "sdk/runtime")
        for target in ("../private/file", "../runtime/file"):
            manifest = self.manifest(target)
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(ValueError, "alias-escapes-view"),
            ):
                view.bind_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "declared-directory-required"):
            replace(view, prefix="sdk/runtime/file").bind_manifest(
                self.manifest("file")
            )

    def test_internal_alias_binding_is_exact_and_grants_no_filesystem_access(self):
        view = RetainedReadOnlyView("runtime", "inputs", "sdk/runtime")
        manifest = self.manifest("file")
        with patch("os.open") as opened:
            binding = view.bind_manifest(manifest)
        opened.assert_not_called()
        self.assertNotEqual(
            binding, replace(view, name="other").bind_manifest(manifest)
        )
        changed = replace(
            manifest,
            members=(replace(manifest.members[0], mode=0o755), *manifest.members[1:]),
        )
        self.assertNotEqual(binding, view.bind_manifest(changed))
        with self.assertRaises(TypeError):
            view.bind_manifest(manifest.to_dict())
