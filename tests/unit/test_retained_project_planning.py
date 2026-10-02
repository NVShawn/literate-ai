"""Original authority and complete reviewed inventory are planning prerequisites."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from literate_ai.adapters.retained_project_inputs import (
    discover_retained_project_inputs,
)
from literate_ai.adapters.retained_project_planning import (
    plan_retained_project,
    retained_role_identity,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from literate_ai.contracts.operator_adoption import (
    ConversionAuthorityStage,
    ConversionAuthorityState,
)
from literate_ai.contracts.retained_project import RetainedProjectLimits
from literate_ai.contracts.retained_project_lifecycle import (
    RetainedProjectAction,
    RetainedProjectProfile,
)


def byte_identity(content):
    return ContentIdentity.parse_uri("sha256:" + hashlib.sha256(content).hexdigest())


class RetainedPlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.roles = {
            "source.txt": "source",
            "tests.txt": "test-definition",
            "docs.txt": "docs-definition",
            "test-policy.json": "test-policy",
            "release-policy.json": "release-policy",
            "tool": "toolchain",
            "profile.json": "build-definition",
            "binary.dat": "dependency",
        }
        for name in self.roles:
            (self.root / name).write_bytes(
                b"\xff\x00retained" if name == "binary.dat" else name.encode()
            )
        (self.root / "tool").chmod(0o755)
        self.limits = RetainedProjectLimits(100, 100_000, 1_000_000)
        base = self.discover()
        self.worker = canonical_identity({"worker": "qualified-fixture"})
        self.actions = tuple(
            RetainedProjectAction(
                stage,
                stage,
                "tool",
                (stage,),
                () if i == 0 else (stages[i - 1],),
                (stage + "-inventory",),
                ("output/" + stage,),
                300,
            )
            for stages in [("build", "test", "docs", "accept")]
            for i, stage in enumerate(stages)
        )
        self.profile = RetainedProjectProfile(
            "host",
            self.worker,
            retained_role_identity(base, {"sdk", "toolchain"}),
            retained_role_identity(base, {"dependency"}),
            byte_identity(b"test-policy.json"),
            byte_identity(b"release-policy.json"),
            (("tool", "tool", byte_identity(b"tool")),),
            self.actions,
            ("output/build",),
        )
        self.profile_bytes = json.dumps(self.profile.to_dict()).encode()
        (self.root / "profile.json").write_bytes(self.profile_bytes)
        self.manifest = self.discover()
        self.project = SimpleNamespace(
            root=self.root, definition=SimpleNamespace(project_id="retained-fixture")
        )
        self.binding = SimpleNamespace(
            require_unchanged=Mock(),
            trust_binding=SimpleNamespace(to_dict=lambda: {"standard": "exact"}),
        )
        self.conversion = ConversionAuthorityState(
            "retained-fixture",
            ConversionAuthorityStage.WRAPPED,
            (canonical_identity({"original": "fixture"}),),
        )
        for target, value in (
            (
                "validated_project_authority_identity",
                canonical_identity({"project": 1}),
            ),
            (
                "current_project_component_lock_identities",
                (canonical_identity({"lock": 1}),),
            ),
        ):
            self.addCleanup(patch.stopall)
            patch(
                "literate_ai.adapters.retained_project_planning." + target,
                return_value=value,
            ).start()
        self.store = patch(
            "literate_ai.adapters.retained_project_planning.FilesystemConversionAuthorityStore"
        ).start()
        self.store.return_value.load_optional.return_value = self.conversion

    def discover(self):
        return discover_retained_project_inputs(self.root, self.roles, self.limits)

    def plan(self, **changes):
        values = dict(
            worker_identity=self.worker,
            target="host",
            profile_path=self.root / "profile.json",
            profile_bytes=self.profile_bytes,
        )
        values.update(changes)
        return plan_retained_project(
            self.project, self.binding, self.manifest, self.profile, **values
        )

    def test_read_only_plan_preserves_original_authority_and_requires_exact_ack(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        plan = self.plan()
        self.assertFalse(plan.to_dict()["admitted"])
        self.assertEqual(plan.to_dict()["source_authority"], "original")
        self.assertEqual(plan.conversion_identity, self.conversion.identity)
        plan.require_authorization(plan.identity.uri)
        with self.assertRaises(ValueError):
            plan.require_authorization(plan.manifest_identity.uri)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_stale_binary_dependency_rejected(self):
        (self.root / "binary.dat").write_bytes(b"changed dependency")
        with self.assertRaises(ValueError):
            self.plan()

    def test_wrong_worker_and_target_rejected(self):
        for changes in (
            {"worker_identity": canonical_identity({"wrong": 1})},
            {"target": "other"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)

    def test_missing_and_foreign_conversion_authority_rejected(self):
        for value in (None, replace(self.conversion, project_id="another-project")):
            self.store.return_value.load_optional.return_value = value
            with self.assertRaises(ValueError):
                self.plan()

    def test_profile_roundtrip_and_unknown_fields_rejected(self):
        self.assertEqual(
            RetainedProjectProfile.from_dict(self.profile.to_dict()), self.profile
        )
        value = self.profile.to_dict()
        value["allow_unqualified"] = True
        with self.assertRaises(ValueError):
            RetainedProjectProfile.from_dict(value)

    def test_omitted_stage_or_duplicate_inventory_rejected(self):
        for actions in (
            self.actions[:-1],
            (self.actions[0], self.actions[0], *self.actions[1:]),
        ):
            with self.assertRaises(ValueError):
                replace(self.profile, actions=actions)

    def test_dependency_graph_and_unsafe_output_rejected(self):
        with self.assertRaises(ValueError):
            replace(
                self.profile,
                actions=(
                    replace(self.actions[0], requires=("later",)),
                    *self.actions[1:],
                ),
            )
        with self.assertRaises(ValueError):
            replace(self.actions[0], outputs=("../escape",))

    def test_mutated_profile_bytes_rejected_even_when_input_still_matches(self):
        with self.assertRaises(ValueError):
            self.plan(profile_bytes=b"substituted")

    def test_unbound_dependency_identity_rejected(self):
        self.profile = replace(
            self.profile, dependency_identity=canonical_identity({"ambient": "sdk"})
        )
        with self.assertRaises(ValueError):
            self.plan()

    def test_substituted_typed_profile_rejected(self):
        self.profile = replace(
            self.profile, release_policy_identity=canonical_identity({"other": 1})
        )
        with self.assertRaises(ValueError):
            self.plan()

    def test_acceptance_cannot_precede_or_omit_declared_tests(self):
        for index in (1, 2, 3):
            actions = list(self.actions)
            actions[index] = replace(actions[index], requires=())
            with self.assertRaises(ValueError):
                replace(self.profile, actions=tuple(actions))
