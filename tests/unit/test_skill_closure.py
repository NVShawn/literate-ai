"""Exact transitive specification-to-source skill closure."""

from __future__ import annotations

import hashlib
import json
import unittest

from literate_ai.application.skill_closure import (
    SkillClosureError,
    close_specification_to_source_skills,
    skill_closure_identity,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    SkillReference,
    SpecificationToSourceSkill,
)
from literate_ai.contracts.skills import ResolvedSpecificationToSourceSkill


def _skill(
    skill_id: str,
    *,
    dependencies: tuple[ResolvedSpecificationToSourceSkill, ...] = (),
    stages: tuple[str, ...] = ("generate",),
    output_trees: tuple[str, ...] = (),
) -> ResolvedSpecificationToSourceSkill:
    manifest = SpecificationToSourceSkill(
        skill_id=skill_id,
        version="1.0.0",
        title=skill_id,
        stages=stages,
        dependencies=tuple(item.ref for item in dependencies),
        instructions=f"Follow {skill_id}.",
        limitations=("Do not invent behavior.",),
        trust="fixture-reviewed",
        output_trees=output_trees,
    )
    content = json.dumps(manifest.to_dict(), sort_keys=True).encode()
    identity = ContentIdentity.parse_uri(
        "sha256:" + hashlib.sha256(content).hexdigest()
    )
    return ResolvedSpecificationToSourceSkill(
        manifest,
        identity,
        source=f"fixture:{skill_id}",
    )


class SkillClosureTests(unittest.TestCase):
    def test_naming_one_parent_receives_transitive_skills_dependency_first(
        self,
    ) -> None:
        layout = _skill("repository-layout", stages=("plan", "generate"))
        planning = _skill("planning", stages=("plan",), dependencies=(layout,))
        implementation = _skill(
            "implementation", stages=("generate",), dependencies=(planning,)
        )
        closed = close_specification_to_source_skills(
            (implementation,),
            (layout, planning, implementation),
        )
        self.assertEqual(
            [item.skill_id for item in closed],
            ["repository-layout", "planning", "implementation"],
        )
        self.assertNotEqual(
            skill_closure_identity(closed),
            skill_closure_identity(closed[:-1]),
        )

    def test_diamond_unifies_the_shared_dependency(self) -> None:
        root = _skill("layout")
        left = _skill("left", dependencies=(root,))
        right = _skill("right", dependencies=(root,))
        top = _skill("top", dependencies=(left, right))
        closed = close_specification_to_source_skills((top,), (root, left, right, top))
        self.assertEqual(
            [item.skill_id for item in closed],
            ["layout", "left", "right", "top"],
        )

    def test_cycles_missing_authority_and_identity_conflicts_fail_closed(
        self,
    ) -> None:
        a_identity = ContentIdentity.parse_uri("sha256:" + "1" * 64)
        b_identity = ContentIdentity.parse_uri("sha256:" + "2" * 64)
        cyclic_a = ResolvedSpecificationToSourceSkill(
            SpecificationToSourceSkill(
                skill_id="cycle-a",
                version="1.0.0",
                title="cycle-a",
                stages=("generate",),
                dependencies=(SkillReference("cycle-b", "1.0.0", b_identity),),
                instructions="Follow cycle-a.",
                limitations=("Do not invent behavior.",),
                trust="fixture-reviewed",
            ),
            a_identity,
            source="fixture:cycle-a",
        )
        cyclic_b = ResolvedSpecificationToSourceSkill(
            SpecificationToSourceSkill(
                skill_id="cycle-b",
                version="1.0.0",
                title="cycle-b",
                stages=("generate",),
                dependencies=(SkillReference("cycle-a", "1.0.0", a_identity),),
                instructions="Follow cycle-b.",
                limitations=("Do not invent behavior.",),
                trust="fixture-reviewed",
            ),
            b_identity,
            source="fixture:cycle-b",
        )
        with self.assertRaises(SkillClosureError) as cycle:
            close_specification_to_source_skills((cyclic_a, cyclic_b))
        self.assertEqual(cycle.exception.code, "skill_closure.cycle")

        parent = _skill("parent")
        child = _skill("child", dependencies=(parent,))
        with self.assertRaises(SkillClosureError) as missing:
            close_specification_to_source_skills((child,))
        self.assertEqual(missing.exception.code, "skill_closure.dependency_missing")

        other_parent = _skill("parent", stages=("plan",))
        mismatched = ResolvedSpecificationToSourceSkill(
            SpecificationToSourceSkill(
                skill_id="child",
                version="1.0.0",
                title="child",
                stages=("generate",),
                dependencies=(
                    SkillReference("parent", "1.0.0", other_parent.content_identity),
                ),
                instructions="Follow child.",
                limitations=("Do not invent behavior.",),
                trust="fixture-reviewed",
            ),
            child.content_identity,
            source="fixture:child-mismatch",
        )
        with self.assertRaises(SkillClosureError) as conflict:
            close_specification_to_source_skills((mismatched,), (parent,))
        self.assertEqual(conflict.exception.code, "skill_closure.identity_mismatch")

    def test_stage_incompatible_and_duplicate_identity_conflicts_fail_closed(
        self,
    ) -> None:
        extra = _skill("extra", stages=("publish",))
        with self.assertRaises(SkillClosureError) as stage:
            close_specification_to_source_skills(
                (extra,),
                workflow_stages=("plan", "generate"),
            )
        self.assertEqual(stage.exception.code, "skill_closure.stage_incompatible")

        first = _skill("shared")
        second = _skill("shared", stages=("plan",))
        with self.assertRaises(SkillClosureError) as duplicate:
            close_specification_to_source_skills((first, second))
        self.assertEqual(duplicate.exception.code, "skill_closure.duplicate_conflict")

    def test_already_closed_caller_order_is_stable(self) -> None:
        planning = _skill("planning", stages=("plan",))
        implementation = _skill(
            "implementation", stages=("generate",), dependencies=(planning,)
        )
        closed = close_specification_to_source_skills((planning, implementation))
        self.assertEqual(
            [item.skill_id for item in closed],
            ["planning", "implementation"],
        )

    def test_multi_output_trees_are_optional_and_validated(self) -> None:
        skill = _skill(
            "multi",
            output_trees=("source/backend", "source/frontend"),
        )
        self.assertEqual(skill.output_trees, ("source/backend", "source/frontend"))
        with self.assertRaises(ContractValidationError):
            _skill("bad", output_trees=("backend",))
        with self.assertRaises(ContractValidationError):
            _skill("escape", output_trees=("source/../secret",))


if __name__ == "__main__":
    unittest.main()
