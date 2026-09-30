from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from literate_ai.application.project_authority import (
    AUTHORITY_REVIEW_PLACEHOLDER,
    AuthorityReviewDocument,
    CatalogAuthorityReviewEntry,
    ComponentAuthorityReviewEntry,
    FlavorAuthorityReviewEntry,
    ForwardSkillAuthorityReviewEntry,
    InverseSkillAuthorityReviewEntry,
    ProjectAuthorityError,
    ProjectAuthorityInventory,
    documentation_path_is_execution_queue,
    review_project_authority,
)
from literate_ai.contracts import canonical_identity


def _identity(label: str):
    return canonical_identity({"fixture": label})


def _inventory(
    documents: tuple[AuthorityReviewDocument, ...] | None = None,
) -> ProjectAuthorityInventory:
    return ProjectAuthorityInventory(
        project_definition=_identity("project"),
        repository_lineage=_identity("repository-lineage"),
        onboarding_skill=_identity("onboarding"),
        components=(
            ComponentAuthorityReviewEntry(
                "component://example/z@1",
                _identity("z-revision").uri,
                _identity("z-workflow").uri,
                _identity("z-routing").uri,
                _identity("z-closure").uri,
            ),
            ComponentAuthorityReviewEntry(
                "component://example/a@1",
                _identity("a-revision").uri,
                _identity("a-workflow").uri,
                _identity("a-routing").uri,
                _identity("a-closure").uri,
                _identity("a-convergence").uri,
            ),
        ),
        flavors=(
            FlavorAuthorityReviewEntry(
                "flavor://example/python@1",
                _identity("flavor").uri,
                _identity("flavor-specifications").uri,
            ),
        ),
        specification_to_source_skills=(
            ForwardSkillAuthorityReviewEntry(
                "python-generation", "1.0.0", _identity("forward-skill").uri
            ),
        ),
        source_to_specification_skills=(
            InverseSkillAuthorityReviewEntry(
                "python-extraction", "1.0.0", _identity("inverse-skill").uri
            ),
        ),
        workflows=(
            CatalogAuthorityReviewEntry("workflows/default.md", _identity("wf").uri),
        ),
        routing=(
            CatalogAuthorityReviewEntry("routing/default.md", _identity("route").uri),
        ),
        documentation=(
            documents
            if documents is not None
            else (AuthorityReviewDocument("README.md", b"Project documentation\n"),)
        ),
        documentation_assets=(
            AuthorityReviewDocument("docs/architecture.svg", b"<svg/>"),
        ),
    )


class ProjectAuthorityTests(unittest.TestCase):
    def test_identity_binds_the_complete_current_inventory(self) -> None:
        inventory = _inventory()
        legacy = {
            "schema": "literate-ai/authority-review-input@2",
            "project_definition": inventory.project_definition.uri,
            "repository_lineage": inventory.repository_lineage.uri,
            "onboarding_skill": inventory.onboarding_skill.uri,
            "components": [
                item.to_dict()
                for item in sorted(
                    inventory.components, key=lambda item: item.coordinate
                )
            ],
            "flavors": [item.to_dict() for item in inventory.flavors],
            "specification_to_source_skills": [
                item.to_dict() for item in inventory.specification_to_source_skills
            ],
            "source_to_specification_skills": [
                item.to_dict() for item in inventory.source_to_specification_skills
            ],
            "workflows": [item.to_dict() for item in inventory.workflows],
            "routing": [item.to_dict() for item in inventory.routing],
            "documentation": [
                {
                    "path": "README.md",
                    "identity": "sha256:"
                    + hashlib.sha256(b"Project documentation\n").hexdigest(),
                }
            ],
            "documentation_assets": [
                {
                    "path": "docs/architecture.svg",
                    "identity": "sha256:" + hashlib.sha256(b"<svg/>").hexdigest(),
                }
            ],
        }

        review = review_project_authority(inventory)

        self.assertEqual(review.authority_identity, canonical_identity(legacy))
        self.assertEqual(
            review.expected_marker,
            f"<!-- literate-ai:authority-reviewed {canonical_identity(legacy).uri} -->",
        )

    def test_marker_states_and_required_error_are_exact(self) -> None:
        missing = review_project_authority(_inventory())
        stale_inventory = _inventory(
            (
                AuthorityReviewDocument(
                    "README.md",
                    b"<!-- literate-ai:authority-reviewed sha256:"
                    + b"0" * 64
                    + b" -->",
                ),
            )
        )
        stale = review_project_authority(stale_inventory)
        current_inventory = _inventory(
            (
                AuthorityReviewDocument(
                    "README.md",
                    b"Project documentation\n" + missing.expected_marker.encode(),
                ),
            )
        )
        current = review_project_authority(current_inventory)
        duplicate = review_project_authority(
            _inventory(
                (
                    AuthorityReviewDocument(
                        "README.md",
                        (
                            "Project documentation\n"
                            + missing.expected_marker
                            + "\n"
                            + missing.expected_marker
                        ).encode(),
                    ),
                )
            )
        )

        self.assertEqual(
            (missing.state, stale.state, current.state, duplicate.state),
            ("missing", "stale", "current", "duplicate"),
        )
        self.assertEqual(current.document, "README.md")
        with self.assertRaises(ProjectAuthorityError) as raised:
            review_project_authority(_inventory(), required=True)
        self.assertEqual(
            raised.exception.code,
            "project.documentation_authority_review_missing",
        )
        self.assertEqual(
            str(raised.exception),
            "documentation authority review is missing; review current authority "
            f"and record exactly: {missing.expected_marker}",
        )

    def test_marker_and_placeholder_are_excluded_from_document_identity(self) -> None:
        plain = review_project_authority(
            _inventory((AuthorityReviewDocument("README.md", b"beforeafter"),))
        )
        pending = review_project_authority(
            _inventory(
                (
                    AuthorityReviewDocument(
                        "README.md",
                        b"before" + AUTHORITY_REVIEW_PLACEHOLDER.encode() + b"after",
                    ),
                )
            )
        )
        marked = review_project_authority(
            _inventory(
                (
                    AuthorityReviewDocument(
                        "README.md",
                        b"before" + plain.expected_marker.encode() + b"after",
                    ),
                )
            )
        )

        self.assertEqual(plain.authority_identity, pending.authority_identity)
        self.assertEqual(plain.authority_identity, marked.authority_identity)
        self.assertEqual(marked.state, "current")

    def test_execution_queue_paths_are_docs_roadmap_descendants(self) -> None:
        self.assertTrue(
            documentation_path_is_execution_queue("docs/roadmap/active-work.md")
        )
        self.assertTrue(
            documentation_path_is_execution_queue(
                "docs/roadmap/public-api-test-strategy.md"
            )
        )
        self.assertFalse(
            documentation_path_is_execution_queue(
                "docs/architecture/design-traceability.md"
            )
        )
        self.assertFalse(
            documentation_path_is_execution_queue("docs/user/getting-started.md")
        )
        self.assertFalse(documentation_path_is_execution_queue("docs/roadmap"))

    def test_normalized_sequences_do_not_depend_on_input_order(self) -> None:
        inventory = _inventory(
            (
                AuthorityReviewDocument("z.md", b"z"),
                AuthorityReviewDocument("a.md", b"a"),
            )
        )
        reversed_inventory = replace(
            inventory,
            components=tuple(reversed(inventory.components)),
            documentation=tuple(reversed(inventory.documentation)),
            documentation_assets=tuple(reversed(inventory.documentation_assets)),
        )

        self.assertEqual(
            review_project_authority(inventory).authority_identity,
            review_project_authority(reversed_inventory).authority_identity,
        )

    def test_authority_input_mutations_change_identity(self) -> None:
        inventory = _inventory()
        baseline = review_project_authority(inventory).authority_identity
        changed_component = replace(
            inventory.components[0], revision=_identity("changed").uri
        )
        changed_inventory = replace(
            inventory,
            components=(changed_component, *inventory.components[1:]),
        )
        changed_document = replace(
            inventory,
            documentation=(AuthorityReviewDocument("README.md", b"changed"),),
        )

        self.assertNotEqual(
            baseline, review_project_authority(changed_inventory).authority_identity
        )
        self.assertNotEqual(
            baseline, review_project_authority(changed_document).authority_identity
        )

    def test_review_wire_shape_is_preserved(self) -> None:
        review = review_project_authority(_inventory())

        self.assertEqual(
            review.to_dict(),
            {
                "schema": "literate-ai/authority-review@1",
                "state": "missing",
                "authority_identity": review.authority_identity.uri,
                "expected_marker": review.expected_marker,
                "document": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
