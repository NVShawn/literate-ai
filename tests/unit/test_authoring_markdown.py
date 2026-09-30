from __future__ import annotations

import hashlib
import unittest

from literate_ai.contracts.authoring_markdown import (
    AGENT_SKILL_EVALUATION_AUTHOR,
    AuthoringMarkdownError,
    parse_authoring_markdown,
    project_typed_agent_skill,
    render_authoring_markdown,
)
from literate_ai.contracts.identity import ContentIdentity, ContentReference
from literate_ai.contracts.skills import ResolvedSpecificationToSourceSkill


class AuthoringMarkdownTests(unittest.TestCase):
    def test_nested_frontmatter_round_trips_canonically(self) -> None:
        metadata = {
            "schema": "example/authority@1",
            "name": "portable-example",
            "stages": ["plan", "generate"],
            "dependency": {
                "name": "base",
                "identity": {"algorithm": "sha256", "digest": "a" * 64},
            },
            "records": [
                {"name": "one", "values": ["a", "b"]},
                {"name": "two", "enabled": True},
            ],
        }
        rendered = render_authoring_markdown(metadata, "Do useful work.\n\nBe exact.")

        parsed, body = parse_authoring_markdown(rendered, source="SKILL.md")

        self.assertEqual(parsed, metadata)
        self.assertEqual(body, "Do useful work.\n\nBe exact.")
        self.assertEqual(
            render_authoring_markdown(parsed, body),
            rendered,
        )

    def test_rejects_ambiguous_or_empty_documents(self) -> None:
        for content, code in (
            (b"name: no-frontmatter\n", "authoring_markdown.frontmatter_missing"),
            (b"---\nname: example\n", "authoring_markdown.frontmatter_unterminated"),
            (b"---\nname: example\n---\n", "authoring_markdown.body_missing"),
            (
                b"---\r\nname: example\r\n---\r\nbody\r\n",
                "authoring_markdown.newline_invalid",
            ),
        ):
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(AuthoringMarkdownError, code),
            ):
                parse_authoring_markdown(content, source="invalid.md")

    def test_forward_skill_uses_body_as_only_instruction_authority(self) -> None:
        metadata = {
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "example",
            "version": "1.0.0",
            "title": "Example skill",
            "stages": ["generate"],
            "dependencies": [],
            "limitations": ["No network access."],
            "trust": "repository-reviewed",
        }
        content = render_authoring_markdown(metadata, "Generate the complete program.")
        identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(content).hexdigest()
        )

        resolved = ResolvedSpecificationToSourceSkill.from_reference(
            ContentReference(
                "specification-to-source-skill",
                "skills/example/SKILL.md",
                identity,
            ),
            content,
            source="test",
        )

        self.assertEqual(resolved.instructions, "Generate the complete program.")
        self.assertEqual(resolved.content_identity, identity)

    def test_agent_discovery_envelope_is_validated_then_removed(self) -> None:
        metadata = {
            "name": "example",
            "description": "Example skill. Use when generating examples.",
            "metadata": {"author": AGENT_SKILL_EVALUATION_AUTHOR},
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "example",
            "version": "1.0.0",
            "title": "Example skill",
            "stages": ["generate"],
            "dependencies": [],
            "limitations": ["No network access."],
            "trust": "repository-reviewed",
        }

        typed, instructions = project_typed_agent_skill(
            metadata,
            "# Example skill\n\nGenerate the complete program.",
            source="SKILL.md",
        )

        self.assertNotIn("name", typed)
        self.assertNotIn("description", typed)
        self.assertNotIn("metadata", typed)
        self.assertEqual(instructions, "Generate the complete program.")

    def test_agent_discovery_envelope_rejects_partial_or_mismatched_authority(
        self,
    ) -> None:
        base = {
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "example",
            "version": "1.0.0",
            "title": "Example skill",
            "stages": ["generate"],
            "dependencies": [],
            "limitations": ["No network access."],
            "trust": "repository-reviewed",
        }
        cases = (
            ({**base, "name": "example"}, "skill_discovery_incomplete"),
            (
                {
                    **base,
                    "name": "wrong",
                    "description": "Description.",
                    "metadata": {"author": AGENT_SKILL_EVALUATION_AUTHOR},
                },
                "skill_discovery_invalid",
            ),
        )
        for metadata, code in cases:
            with (
                self.subTest(code=code),
                self.assertRaisesRegex(AuthoringMarkdownError, code),
            ):
                project_typed_agent_skill(
                    metadata,
                    "# Example skill\n\nGenerate it.",
                    source="SKILL.md",
                )

    def test_forward_skill_rejects_duplicate_instruction_authority(self) -> None:
        metadata = {
            "schema": "urn:literate-ai:schema:v1:specification-to-source-skill",
            "skill_id": "example",
            "version": "1.0.0",
            "title": "Example skill",
            "stages": ["generate"],
            "dependencies": [],
            "limitations": ["No network access."],
            "trust": "repository-reviewed",
            "instructions": "frontmatter authority",
        }
        content = render_authoring_markdown(metadata, "body authority")
        identity = ContentIdentity.parse_uri(
            "sha256:" + hashlib.sha256(content).hexdigest()
        )

        with self.assertRaisesRegex(ValueError, "instructions must exist only"):
            ResolvedSpecificationToSourceSkill.from_reference(
                ContentReference(
                    "specification-to-source-skill",
                    "skills/example/SKILL.md",
                    identity,
                ),
                content,
                source="test",
            )


if __name__ == "__main__":
    unittest.main()
