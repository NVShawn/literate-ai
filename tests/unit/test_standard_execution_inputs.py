"""Runtime authority varies independently of consumer compilation."""

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from literate_ai.application.standard_execution_inputs import (
    plan_standard_execution_inputs,
    plan_standard_execution_receipts,
    standard_execution_provider_revisions,
)
from literate_ai.contracts import (
    StandardExecutionAuthority,
    StandardExecutionInputScope,
)
from literate_ai.contracts.capabilities import DependencyKind
from literate_ai.contracts.identity import canonical_identity
from literate_ai.contracts.standard_execution_inputs import standard_execution_request
from literate_ai.contracts.standard_lifecycle_membership import (
    StandardNodeFailureEvidence,
    StandardNodeFailurePhase,
)
from literate_ai.security import AuthorizationError, BuildAuthorization, SecurityProfile
from tests.unit.test_component_execution_planning import _diamond_lock
from tests.unit.test_component_generation_scheduling import _names, _prepared_execution
from tests.unit.test_schema_catalog import SchemaCatalog
from tests.unit.test_standard_project_lifecycle import (
    _decision,
    _identity,
    _prepared_nodes,
    _service,
)
from tests.unit.test_standard_runtime_scheduling import ScopedRuntimePorts


class StandardExecutionInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lock = _diamond_lock(dependency_kind=DependencyKind.RUNTIME)
        cls.execution, requests = _prepared_execution(lock)
        names = _names(lock)
        ports = ScopedRuntimePorts(cls.execution, names)
        result = _service(ports).execute(
            cls.execution,
            component_lock=lock,
            invalidation=_decision(
                cls.execution, names, "money", tuple(names.values())
            ),
            prepared_nodes=_prepared_nodes(cls.execution, requests),
            max_parallelism=2,
        )
        assert result.successful
        cls.consumer = next(
            item
            for item in result.node_results
            if names[item.component_revision.uri] == "invoice-cli"
        )
        cls.plan = ports.plans[cls.consumer.component_revision.uri]
        cls.edges = {
            edge.identity: edge
            for action in cls.execution.action_plans
            for edge in action.dependency_edges
            if edge.kind is DependencyKind.RUNTIME
        }
        required = set(
            standard_execution_provider_revisions(
                cls.execution, cls.consumer.component_revision
            )
        )
        cls.results = {item.component_revision: item for item in result.node_results}
        cls.providers = tuple(
            item for item in result.node_results if item.component_revision in required
        )
        assert cls.providers
        schemas = SchemaCatalog()
        registry = Registry().with_resources(
            (uri, Resource.from_contents(document, default_specification=DRAFT202012))
            for uri, document in schemas.resources.items()
        )
        schema = schemas.resources[StandardExecutionInputScope.SCHEMA]
        Draft202012Validator.check_schema(schema)
        cls.validator = Draft202012Validator(schema, registry=registry)
        cls.authority_validator = Draft202012Validator(
            {"$ref": StandardExecutionAuthority.SCHEMA}, registry=registry
        )

    def scope(self, providers=None, exports=None):
        return plan_standard_execution_inputs(
            self.execution,
            self.plan,
            self.consumer.exports if exports is None else exports,
            self.providers if providers is None else providers,
        )

    def authority(self):
        scope = self.scope()
        source, command, runtime = (
            _identity(label) for label in ("source", "command", "runtime")
        )
        request = standard_execution_request(scope, source, command, runtime)
        now = datetime(2026, 9, 30, tzinfo=UTC)
        grant = BuildAuthorization(
            "fixture-execution",
            scope.identity.uri,
            canonical_identity(request.to_dict()).uri,
            scope.component_revision.uri,
            "fixture",
            "exact execution inputs",
            SecurityProfile.CONSTRAINED,
            request.requested_privileges,
            now,
            now + timedelta(minutes=5),
        )
        return StandardExecutionAuthority(scope, source, command, runtime, grant)

    def test_receipts_derive_the_same_full_runtime_scope_as_lifecycle_results(self):
        receipts = tuple(item.acceptance_evidence for item in self.providers)
        actual = plan_standard_execution_receipts(
            self.execution, self.plan, self.consumer.exports, receipts
        )
        self.assertEqual(actual, self.scope())
        self.assertEqual(self.plan.provider_artifact_identities, ())
        self.assertTrue(actual.runtime_dependencies)
        for changed in (receipts[:-1], (*receipts, receipts[0]), (None,)):
            with self.subTest(receipts=changed), self.assertRaises(ValueError):
                plan_standard_execution_receipts(
                    self.execution, self.plan, self.consumer.exports, changed
                )

    def test_scoped_grant_roundtrip_and_live_expiration(self):
        authority = self.authority()
        self.authority_validator.validate(authority.to_dict())
        self.assertEqual(
            authority, StandardExecutionAuthority.from_dict(authority.to_dict())
        )
        authority.require_valid(now=authority.grant.issued_at)
        for now in (
            authority.grant.issued_at - timedelta(seconds=1),
            authority.grant.expires_at,
        ):
            with self.assertRaises(AuthorizationError):
                authority.require_valid(now=now)

    def test_grant_rejects_input_command_runtime_or_privilege_substitution(self):
        authority = self.authority()
        for field in (
            "source_tree_identity",
            "command_contract_identity",
            "runtime_identity",
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(authority, **{field: _identity("substitution")})
        with self.assertRaises(ValueError):
            replace(
                authority,
                input_scope=replace(
                    authority.input_scope,
                    execution_plan_identity=_identity("other-plan"),
                ),
            )
        for changes in (
            {"revoked": True},
            {"profile": SecurityProfile.BLOCKED},
            {"privileges": (*authority.grant.privileges, "network-access")},
            {"classification_digest": _identity("other-scope").uri},
            {"request_digest": _identity("other-request").uri},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(authority, grant=replace(authority.grant, **changes))

    def test_exact_scope_roundtrip_schema_and_input_order(self):
        scope = self.scope()
        self.assertEqual(scope, StandardExecutionInputScope.from_dict(scope.to_dict()))
        self.validator.validate(scope.to_dict())
        self.assertEqual(scope, self.scope(tuple(reversed(self.providers))))
        self.assertEqual(scope.build_plan_identity, self.plan.identity)
        self.assertEqual(
            set(scope.export_identities),
            {item.identity for item in self.consumer.exports},
        )
        expected = {
            (
                output.identity,
                export.identity,
                edge.identity,
                provider.acceptance_identity,
            )
            for edge in self.edges.values()
            for provider in self.providers
            if provider.component_revision == edge.provider_revision
            for output in self.results[edge.consumer_revision].exports
            for export in provider.exports
        }
        self.assertEqual(
            expected,
            {
                (
                    item.consumer_artifact_identity,
                    item.provider_artifact_identity,
                    item.dependency_edge_identity,
                    item.provider_acceptance_identity,
                )
                for item in scope.runtime_dependencies
            },
        )

    def test_changed_provider_acceptance_invalidates_scope_without_compilation(self):
        original = self.scope()
        changed = self.scope(
            (
                replace(
                    self.providers[0],
                    acceptance_identity=_identity("fresh-provider-acceptance"),
                    acceptance_evidence=None,
                ),
                *self.providers[1:],
            )
        )
        self.assertNotEqual(original.identity, changed.identity)
        self.assertEqual(original.build_plan_identity, changed.build_plan_identity)
        self.assertEqual(original.export_identities, changed.export_identities)
        self.assertEqual(
            original.provider_artifact_identities, changed.provider_artifact_identities
        )

    def test_changed_runtime_artifact_invalidates_scope_without_compilation(self):
        original = self.scope()
        provider = self.providers[0]
        export = provider.exports[0]
        altered = replace(export, blob=replace(export.blob, digest="ff" * 32))
        changed = self.scope(
            (
                replace(provider, exports=(altered, *provider.exports[1:])),
                *self.providers[1:],
            )
        )
        self.assertNotEqual(original.identity, changed.identity)
        self.assertEqual(original.build_plan_identity, changed.build_plan_identity)
        self.assertEqual(original.export_identities, changed.export_identities)
        self.assertIn(altered.identity, changed.provider_artifact_identities)

    def test_missing_duplicate_foreign_failed_and_substituted_providers_refused(self):
        provider = self.providers[0]
        rejected = replace(
            provider,
            acceptance_identity=None,
            acceptance_evidence=None,
            source_cache_membership=None,
            source_cache_publication_identity=None,
        )
        failed = replace(
            rejected,
            failure_code="fixture.failure",
            failure_evidence=StandardNodeFailureEvidence(
                provider.component_revision,
                StandardNodeFailurePhase.EXECUTE,
                "fixture.failure",
                provider.build_plan_identity,
                "fixture rejection",
            ),
        )
        for providers in (
            self.providers[1:],
            (*self.providers, provider),
            (*self.providers, self.consumer),
            (rejected, *self.providers[1:]),
            (failed, *self.providers[1:]),
            (replace(provider, exports=self.consumer.exports), *self.providers[1:]),
            (
                replace(provider, exports=(*provider.exports, provider.exports[0])),
                *self.providers[1:],
            ),
        ):
            with self.subTest(
                providers=tuple(item.component_revision for item in providers)
            ):
                with self.assertRaisesRegex(ValueError, "accepted runtime providers"):
                    self.scope(providers)

    def test_consumer_exports_must_match_authorized_build_declarations(self):
        with self.assertRaises(ValueError):
            self.scope(exports=self.providers[0].exports)
        with self.assertRaises(ValueError):
            self.scope(exports=())

    def test_contract_refuses_nonruntime_foreign_duplicate_or_mutable_bindings(self):
        scope = self.scope()
        first = scope.runtime_dependencies[0]
        for bindings in (
            list(scope.runtime_dependencies),
            (first, first),
            (replace(first, dependency_kind=DependencyKind.PACKAGING),),
            (replace(first, consumer_artifact_identity=_identity("foreign-export")),),
            ("untyped",),
            (first,) * 16385,
        ):
            with self.subTest(kind=type(bindings).__name__):
                with self.assertRaises(ValueError):
                    replace(scope, runtime_dependencies=bindings)
        for outputs in (
            (),
            list(scope.export_identities),
            ("untyped",),
            scope.export_identities * 2,
        ):
            with self.assertRaises(ValueError):
                replace(scope, export_identities=outputs)

    def test_multiple_outputs_require_complete_consistent_runtime_bindings(self):
        scope = self.scope()
        outputs = tuple(
            sorted(
                (*scope.export_identities, _identity("second-output")),
                key=lambda item: item.uri,
            )
        )
        with self.assertRaisesRegex(ValueError, "every consumer export"):
            replace(scope, export_identities=outputs)
        added = tuple(
            replace(item, consumer_artifact_identity=_identity("second-output"))
            for item in scope.runtime_dependencies
            if item.consumer_artifact_identity in scope.export_identities
        )
        bindings = tuple(
            sorted(
                (*scope.runtime_dependencies, *added),
                key=lambda item: item.identity.uri,
            )
        )
        multi = replace(scope, export_identities=outputs, runtime_dependencies=bindings)
        self.validator.validate(multi.to_dict())
        altered = replace(
            added[0], provider_acceptance_identity=_identity("different-acceptance")
        )
        with self.assertRaisesRegex(ValueError, "inconsistent acceptance"):
            replace(
                scope,
                export_identities=outputs,
                runtime_dependencies=tuple(
                    sorted(
                        (*scope.runtime_dependencies, altered, *added[1:]),
                        key=lambda item: item.identity.uri,
                    )
                ),
            )

    def test_indirect_runtime_cycle_is_rejected(self):
        scope = self.scope()
        indirect = next(
            item
            for item in scope.runtime_dependencies
            if item.consumer_artifact_identity not in scope.export_identities
        )
        acceptance = next(
            item.provider_acceptance_identity
            for item in scope.runtime_dependencies
            if item.provider_artifact_identity == indirect.consumer_artifact_identity
        )
        reverse = replace(
            indirect,
            consumer_artifact_identity=indirect.provider_artifact_identity,
            provider_artifact_identity=indirect.consumer_artifact_identity,
            provider_acceptance_identity=acceptance,
            dependency_edge_identity=_identity("cycle"),
        )
        with self.assertRaisesRegex(ValueError, "acyclic"):
            replace(
                scope,
                runtime_dependencies=tuple(
                    sorted(
                        (*scope.runtime_dependencies, reverse),
                        key=lambda item: item.identity.uri,
                    )
                ),
            )

    def test_absent_compiled_provider_provenance_is_rejected(self):
        provider = self.providers[0]
        altered = replace(
            provider.exports[0],
            dependency_artifact_identities=(_identity("missing-compiled-input"),),
        )
        with self.assertRaisesRegex(ValueError, "provenance is incomplete"):
            self.scope((replace(provider, exports=(altered,)), *self.providers[1:]))

    def test_empty_runtime_scope_and_unknown_fields(self):
        scope = replace(self.scope(), runtime_dependencies=())
        self.assertEqual(
            scope.provider_artifact_identities, scope.build_provider_artifact_identities
        )
        self.assertEqual(scope, StandardExecutionInputScope.from_dict(scope.to_dict()))
        self.validator.validate(scope.to_dict())
        with self.assertRaises(ValueError):
            StandardExecutionInputScope.from_dict(
                {**scope.to_dict(), "grant": "implicit"}
            )

    def test_public_schema_rejects_packaging_inputs_and_empty_outputs(self):
        scope = self.scope()
        wire = scope.to_dict()
        wire["runtime_dependencies"][0]["dependency_kind"] = "packaging"
        with self.assertRaises(ValidationError):
            self.validator.validate(wire)
        wire = scope.to_dict()
        wire["export_identities"] = []
        with self.assertRaises(ValidationError):
            self.validator.validate(wire)

    def test_same_provider_cannot_claim_different_acceptance_through_two_edges(self):
        scope = self.scope()
        first = scope.runtime_dependencies[0]
        changed = replace(
            first,
            dependency_edge_identity=_identity("another-runtime-capability"),
            provider_acceptance_identity=_identity("inconsistent-provider-state"),
        )
        with self.assertRaisesRegex(ValueError, "inconsistent acceptance"):
            replace(
                scope,
                runtime_dependencies=tuple(
                    sorted(
                        (*scope.runtime_dependencies, changed),
                        key=lambda item: item.identity.uri,
                    )
                ),
            )
