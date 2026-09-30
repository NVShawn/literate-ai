"""Admit-then-continue lookup identity for copied filesystem-v2 membership.

GitHub #139 / CACHE-CONTINUATION-001: a published exact source-admission
membership copied into an isolated project's ``generated/accepted-source-cache``
must be discovered by continuation. Lookup used to miss because
``execution_plan_identity`` embedded the live coding-CLI executable digest,
which ``AcceptedSourceCodingCliSelection`` cannot reconstruct.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

from literate_ai.adapters.cache import (
    FileSystemSourceCache,
    FilesystemStandardSourceRestorer,
    SourceCacheMaterializer,
    SourceCacheResolver,
)
from literate_ai.adapters.component_markdown import parse_component_markdown
from literate_ai.adapters.generation_preparation import (
    GenerationExecutionPlanningAdapter,
)
from literate_ai.adapters.models.coding_cli import (
    AcceptedSourceCodingCliSelection,
    CodingCliSelection,
)
from literate_ai.application.planning import compile_generation_execution_plan
from literate_ai.contracts import (
    Capability,
    ComponentDefinition,
    ContentIdentity,
    ContentReference,
    HashAlgorithm,
    SourceCacheMode,
    SourceCacheModelBinding,
    SourceDerivationCacheKey,
)
from literate_ai.models import Locality, ModelEndpoint
from literate_ai.storage import FileSystemCAS
from tests.unit.test_source_cache import _configuration, _identity, _target
from tests.unit.test_standard_source_cache_roundtrip import _source_admission_entry

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE_ROOT = _REPOSITORY_ROOT / "samples" / "hello-component"
_LOOKUP_FIELDS = (
    "recipe_identity",
    "execution_plan_identity",
    "coding_cli_tool_binding_identity",
    "model_binding",
    "source_semantics_identity",
)


def _reference(kind: str, uri: str, content: bytes) -> ContentReference:
    return ContentReference(
        kind,
        uri,
        ContentIdentity(HashAlgorithm.SHA256, hashlib.sha256(content).hexdigest()),
    )


def _hello_component_authority() -> tuple[ComponentDefinition, bytes, bytes]:
    authoring_path = _SAMPLE_ROOT / "component.md"
    authoring = parse_component_markdown(
        authoring_path,
        authoring_path.read_text(encoding="utf-8"),
        project_root=_REPOSITORY_ROOT,
    )
    workflow = (_REPOSITORY_ROOT / authoring.workflow_definition.uri).read_bytes()
    routing = (_REPOSITORY_ROOT / authoring.routing_policy.uri).read_bytes()
    definition = ComponentDefinition(
        coordinate=authoring.coordinate,
        version=authoring.version,
        display_name=authoring.display_name,
        description=authoring.description,
        profiles=authoring.profiles,
        sample=authoring.sample,
        provides=tuple(
            Capability(item.name, item.version, None) for item in authoring.provides
        ),
        requires=authoring.requires,
        specification_provider=authoring.specification_provider,
        specification_roots=authoring.specification_roots,
        authoring_inputs=(),
        workflow_definition=_reference(
            "workflow", authoring.workflow_definition.uri, workflow
        ),
        routing_policy=_reference(
            "routing-policy", authoring.routing_policy.uri, routing
        ),
        flavor_slots=authoring.flavor_slots,
        entrypoints=authoring.entrypoints,
        acceptance_contracts=(),
    )
    return definition, workflow, routing


def _live_and_lookup_selections() -> tuple[
    CodingCliSelection, AcceptedSourceCodingCliSelection
]:
    live = CodingCliSelection(
        "codex",
        str(Path(sys.executable).resolve()),
        canonical_executable_digest(),
    )
    lookup = AcceptedSourceCodingCliSelection(
        live.name,
        ContentIdentity.parse_uri(live.tool_binding_identity),
    )
    return live, lookup


def canonical_executable_digest() -> str:
    return ContentIdentity(
        HashAlgorithm.SHA256,
        hashlib.sha256(b"admitted-coding-cli-executable").hexdigest(),
    ).uri


def _plan_for_selection(
    selection: CodingCliSelection | AcceptedSourceCodingCliSelection,
    definition: ComponentDefinition,
    workflow: bytes,
    routing: bytes,
    *,
    model: str = "fixture-model",
):
    prepared = SimpleNamespace(
        definition=definition,
        recipe=SimpleNamespace(
            prompt=lambda: "Exact generated recipe sha256:fixture",
            resolved_skills=(),
        ),
    )
    return GenerationExecutionPlanningAdapter._compile(
        prepared,
        workflow=workflow,
        routing=routing,
        selection=selection,
        model=model,
    )


def _cache_key_for_plan(
    plan,
    selection: CodingCliSelection | AcceptedSourceCodingCliSelection,
    *,
    request_label: str,
) -> SourceDerivationCacheKey:
    return SourceDerivationCacheKey(
        recipe_identity=_identity("continuation-recipe"),
        execution_plan_identity=plan.identity,
        coding_cli_tool_binding_identity=ContentIdentity.parse_uri(
            selection.tool_binding_identity
        ),
        model_binding=SourceCacheModelBinding(selection.name, "fixture-model"),
        request_identity=_identity(request_label),
        source_semantics_identity=_identity("stable-source-semantics"),
    )


def _lookup_field_diffs(
    admitted: SourceDerivationCacheKey, continuation: SourceDerivationCacheKey
) -> tuple[str, ...]:
    left = admitted.accepted_source_lookup
    right = continuation.accepted_source_lookup
    return tuple(
        name for name in _LOOKUP_FIELDS if getattr(left, name) != getattr(right, name)
    )


def _legacy_endpoint_url(selection: object) -> str:
    return (
        f"cli://{selection.name}/tool"
        f"?identity={quote(selection.executable_identity, safe='')}"
        f"&binding={quote(selection.tool_binding_identity, safe='')}"
    )


def _plan_for_endpoint_url(
    url: str,
    definition: ComponentDefinition,
    workflow: bytes,
    routing: bytes,
    *,
    provider: str,
):
    endpoint = ModelEndpoint(
        f"coding-cli-{provider}",
        f"coding-cli/{provider}",
        "fixture-model",
        url,
        Locality.UNKNOWN,
        ("structured-output", "source-generation"),
        1_000_000,
        model_revision=None,
    )
    return compile_generation_execution_plan(
        component=definition,
        workflow_content=workflow,
        routing_content=routing,
        endpoint=endpoint,
        generation_prompt="Exact generated recipe sha256:fixture",
        project_root=_REPOSITORY_ROOT,
    )


class CopiedAcceptedSourceContinuationTests(unittest.TestCase):
    def test_legacy_endpoint_identity_query_differs_on_execution_plan_identity(self):
        live, lookup = _live_and_lookup_selections()
        definition, workflow, routing = _hello_component_authority()
        self.assertEqual(live.tool_binding_identity, lookup.tool_binding_identity)
        self.assertNotEqual(live.executable_identity, lookup.executable_identity)

        admitted_plan = _plan_for_endpoint_url(
            _legacy_endpoint_url(live),
            definition,
            workflow,
            routing,
            provider=live.name,
        )
        continuation_plan = _plan_for_endpoint_url(
            _legacy_endpoint_url(lookup),
            definition,
            workflow,
            routing,
            provider=lookup.name,
        )
        admitted = _cache_key_for_plan(admitted_plan, live, request_label="session-a")
        continuation = _cache_key_for_plan(
            continuation_plan, lookup, request_label="session-b"
        )

        self.assertEqual(
            _lookup_field_diffs(admitted, continuation),
            ("execution_plan_identity",),
        )

    def test_compiled_lookup_plan_matches_live_admission_plan(self):
        live, lookup = _live_and_lookup_selections()
        definition, workflow, routing = _hello_component_authority()
        admitted_plan = _plan_for_selection(live, definition, workflow, routing)
        continuation_plan = _plan_for_selection(lookup, definition, workflow, routing)
        admitted = _cache_key_for_plan(admitted_plan, live, request_label="session-a")
        continuation = _cache_key_for_plan(
            continuation_plan, lookup, request_label="session-b"
        )

        self.assertNotEqual(admitted.request_identity, continuation.request_identity)
        self.assertEqual(
            admitted.accepted_source_lookup, continuation.accepted_source_lookup
        )
        self.assertEqual(_lookup_field_diffs(admitted, continuation), ())

    def test_copied_filesystem_v2_membership_is_found_by_continuation(self):
        live, lookup = _live_and_lookup_selections()
        definition, workflow, routing = _hello_component_authority()
        admitted_plan = _plan_for_selection(live, definition, workflow, routing)
        continuation_plan = _plan_for_selection(lookup, definition, workflow, routing)
        admitted_key = _cache_key_for_plan(
            admitted_plan, live, request_label="session-a"
        )
        continuation_key = _cache_key_for_plan(
            continuation_plan, lookup, request_label="session-b"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            admit_project = root / "admit-project"
            continue_project = root / "continue-project"
            admit_project.mkdir()
            continue_project.mkdir()
            caller_cas = FileSystemCAS(root / "caller-cas")
            entry = _source_admission_entry(caller_cas, cache_key=admitted_key)
            admitted_cache = FileSystemSourceCache(
                "runtime", admit_project / "generated" / "accepted-source-cache"
            )
            admitted_cache.publish(entry, caller_cas=caller_cas)
            shutil.copytree(
                admitted_cache.root,
                continue_project / "generated" / "accepted-source-cache",
            )

            continued = FileSystemSourceCache(
                "runtime",
                continue_project / "generated" / "accepted-source-cache",
                writable=False,
            )
            self.assertEqual(continued.candidates(continuation_key), (entry,))

            resolver = SourceCacheResolver(
                _configuration(SourceCacheMode.READ_ONLY, _target("runtime")),
                {"runtime": continued},
            )
            restorer = FilesystemStandardSourceRestorer(
                resolver=resolver,
                materializer=SourceCacheMaterializer(),
            )
            restored = restorer.restore(
                continuation_key,
                continue_project / "restored-source",
                component_lock_identity=entry.component_lock_identity,
            )

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.membership, entry.membership)
            self.assertEqual(restored.entry_identity, entry.identity)
