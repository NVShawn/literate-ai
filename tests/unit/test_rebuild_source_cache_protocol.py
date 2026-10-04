"""Typed rebuild/source-cache negotiation and post-acceptance publication."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.cache import SourceCacheResolver
from literate_ai.adapters.cache.rebuild import (
    RebuildSourceCacheProtocolError,
    RebuildSourceCacheSession,
    protocol_directory_identity,
    read_rebuild_source_cache_control,
    write_rebuild_source_cache_control,
    write_rebuild_source_cache_derivation_manifest,
    write_rebuild_source_cache_publication,
)
from literate_ai.cli.rebuild_cache import (
    RebuildSourceCacheProtocol,
    publish_rebuild_source_cache_offer,
)
from literate_ai.contracts import (
    ContentIdentity,
    ContractValidationError,
    ProjectTestEvidence,
    ProjectTestReceipt,
    ProjectTestSummary,
    SourceCacheMode,
    VersionedContentRef,
    canonical_identity,
)
from literate_ai.contracts.rebuild_cache import (
    RebuildSourceCacheControl,
    RebuildSourceCacheDecision,
    RebuildSourceCacheDecisionItem,
    RebuildSourceCacheDerivationManifest,
    RebuildSourceCacheLifecycleBinding,
    RebuildSourceCacheLifecycleMember,
    RebuildSourceCacheOperatorRoot,
    RebuildSourceCacheOutcome,
    RebuildSourceCachePublicationMember,
    RebuildSourceCachePublicationOffer,
    rebuild_project_authority_identity,
)
from literate_ai.projects import LoadedProject
from literate_ai.storage import FileSystemCAS
from tests.support.fixtures_test_schema_catalog import SchemaCatalog
from tests.support.fixtures_test_source_cache import (
    _accepted_entry,
    _cache_key,
    _configuration,
    _target,
)
from tests.support.fixtures_test_source_cache_hardening import _project


def _identity(label: str) -> ContentIdentity:
    return canonical_identity({"label": label})


def _manifest(
    *,
    project_revision_identity: ContentIdentity | None = None,
    key=None,
) -> RebuildSourceCacheDerivationManifest:
    selected_key = _cache_key() if key is None else key
    component_locks = (_identity("component-lock"),)
    plan_identity = canonical_identity(
        {
            "schema": "literate-ai/rebuild-derivation-plan@2",
            "component_lock_identities": [identity.uri for identity in component_locks],
            "cache_key_identities": [selected_key.identity.uri],
        }
    )
    return RebuildSourceCacheDerivationManifest(
        planning_request_identity=_identity("planning-request"),
        project_revision_identity=(
            _identity("project")
            if project_revision_identity is None
            else project_revision_identity
        ),
        lifecycle_driver_identity=_identity("lifecycle-driver"),
        lifecycle_plan_identity=plan_identity,
        component_lock_identities=component_locks,
        cache_keys=(selected_key,),
    )


def _accepted_entry_for_lock(
    cas: FileSystemCAS,
    *,
    suffix: str = "one",
    key=None,
    component_lock_identity: ContentIdentity | None = None,
):
    selected_lock = (
        _identity("component-lock")
        if component_lock_identity is None
        else component_lock_identity
    )
    seed = _accepted_entry(cas, suffix=suffix, key=key)
    managed_graph = replace(
        seed.managed_sbom_graph,
        resolved_graph_identity=selected_lock,
    )
    return _accepted_entry(
        cas,
        suffix=suffix,
        key=key,
        managed_graph=managed_graph,
    )


class RebuildSourceCacheContractTests(unittest.TestCase):
    def test_protocol_reads_are_bounded_and_bind_the_pinned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary).resolve()
            protocol_root = runtime / ".litai"
            protocol_root.mkdir()
            control = RebuildSourceCacheControl(
                _identity("request"),
                _identity("project"),
                None,
                _manifest(),
                (_identity("component-lock"),),
            )
            control_path = protocol_root / "source-cache-control.json"
            identity = protocol_directory_identity(protocol_root)
            write_rebuild_source_cache_control(
                control_path,
                control,
                directory_identity=identity,
            )

            with patch(
                "literate_ai.adapters.cache.rebuild._MAXIMUM_PROTOCOL_BYTES",
                16,
            ):
                with self.assertRaises(RebuildSourceCacheProtocolError):
                    read_rebuild_source_cache_control(
                        control_path,
                        directory_identity=identity,
                    )

            original = runtime / ".litai-original"
            protocol_root.rename(original)
            protocol_root.mkdir()
            with self.assertRaises(RebuildSourceCacheProtocolError):
                read_rebuild_source_cache_control(
                    control_path,
                    directory_identity=identity,
                )

    def test_decision_cardinality_is_bounded_before_duplicate_work(self) -> None:
        key = _cache_key()
        item = RebuildSourceCacheDecisionItem(
            key,
            RebuildSourceCacheOutcome.MISS,
        )
        with self.assertRaisesRegex(ContractValidationError, "at most 64"):
            RebuildSourceCacheDecision(
                _identity("control"),
                _identity("request"),
                _identity("configuration"),
                SourceCacheMode.READ_ONLY.value,
                True,
                (item,) * 65,
            )

    def test_cache_and_artifact_publications_have_distinct_receipt_evidence(
        self,
    ) -> None:
        cache = ProjectTestEvidence(
            "source-cache-publication-result", _identity("cache-publication")
        )
        artifact = ProjectTestEvidence(
            "artifact-publication-result", _identity("artifact-publication")
        )

        self.assertNotEqual(cache.kind, artifact.kind)
        self.assertNotEqual(cache.identity, artifact.identity)

    def test_unconfigured_control_has_one_cli_owned_fresh_decision(self) -> None:
        control = RebuildSourceCacheControl(
            _identity("request"),
            _identity("project"),
            None,
            _manifest(),
            (_identity("component-lock"),),
            force_regeneration=True,
        )
        decision = RebuildSourceCacheDecision.fresh(control)

        self.assertEqual(
            RebuildSourceCacheControl.from_dict(control.to_dict()), control
        )
        self.assertEqual(
            RebuildSourceCacheDecision.from_dict(decision.to_dict()), decision
        )
        decision.validate_against(control)
        self.assertEqual(decision.mode, "unconfigured")
        self.assertTrue(decision.fresh_generation_required)
        self.assertEqual(len(decision.items), 1)
        self.assertEqual(
            decision.items[0].outcome, RebuildSourceCacheOutcome.UNCONFIGURED
        )
        schemas = SchemaCatalog()
        schemas.validate(
            control.derivation_manifest.SCHEMA,
            control.derivation_manifest.to_dict(),
        )
        schemas.validate(control.SCHEMA, control.to_dict())
        schemas.validate(decision.SCHEMA, decision.to_dict())

    def test_decision_cannot_omit_or_add_outer_planned_derivations(self) -> None:
        control = RebuildSourceCacheControl(
            _identity("request"),
            _identity("project"),
            None,
            _manifest(),
            (_identity("component-lock"),),
        )
        decision = RebuildSourceCacheDecision.fresh(control)

        with self.assertRaisesRegex(ContractValidationError, "exactly cover"):
            replace(decision, items=()).validate_against(control)

        extra = RebuildSourceCacheDecisionItem(
            _cache_key(selector="model-b"),
            RebuildSourceCacheOutcome.UNCONFIGURED,
        )
        added = replace(
            decision,
            items=tuple(
                sorted(
                    (*decision.items, extra),
                    key=lambda item: item.cache_key.identity.uri,
                )
            ),
        )
        with self.assertRaisesRegex(ContractValidationError, "exactly cover"):
            added.validate_against(control)

    def test_manifest_plan_identity_is_canonical_for_its_exact_key_set(self) -> None:
        manifest = _manifest()

        with self.assertRaisesRegex(ContractValidationError, "canonical identity"):
            replace(
                manifest,
                lifecycle_plan_identity=_identity("attacker-selected-plan"),
            )

    def test_project_rebuild_authority_binds_the_exact_canonical_lock_set(
        self,
    ) -> None:
        base_authority = _identity("validated-project-authority")
        component_locks = tuple(
            sorted(
                (_identity("component-lock-a"), _identity("component-lock-b")),
                key=lambda identity: identity.uri,
            )
        )
        key = _cache_key()
        project_authority = rebuild_project_authority_identity(
            base_authority,
            component_locks,
        )
        plan_identity = canonical_identity(
            {
                "schema": "literate-ai/rebuild-derivation-plan@2",
                "component_lock_identities": [
                    identity.uri for identity in component_locks
                ],
                "cache_key_identities": [key.identity.uri],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=_identity("planning-request"),
            project_revision_identity=project_authority,
            lifecycle_driver_identity=_identity("lifecycle-driver"),
            lifecycle_plan_identity=plan_identity,
            component_lock_identities=component_locks,
            cache_keys=(key,),
        )
        control = RebuildSourceCacheControl(
            lifecycle_request_identity=_identity("request"),
            project_revision_identity=project_authority,
            configuration=None,
            derivation_manifest=manifest,
            component_lock_identities=component_locks,
        )

        self.assertEqual(
            RebuildSourceCacheDerivationManifest.from_dict(manifest.to_dict()),
            manifest,
        )
        self.assertEqual(
            RebuildSourceCacheControl.from_dict(control.to_dict()), control
        )
        self.assertNotEqual(
            rebuild_project_authority_identity(
                base_authority,
                (component_locks[0],),
            ),
            project_authority,
        )
        with self.assertRaisesRegex(ContractValidationError, "exactly match"):
            replace(
                control,
                component_lock_identities=(component_locks[0],),
            )
        with self.assertRaisesRegex(
            ContractValidationError, "canonical identity order"
        ):
            rebuild_project_authority_identity(
                base_authority,
                tuple(reversed(component_locks)),
            )
        omitted = manifest.to_dict()
        del omitted["component_lock_identities"]
        with self.assertRaises(ContractValidationError):
            RebuildSourceCacheDerivationManifest.from_dict(omitted)

    def test_lifecycle_cannot_omit_or_add_outer_planned_derivations(self) -> None:
        keys = tuple(
            sorted(
                (_cache_key(), _cache_key(selector="model-b")),
                key=lambda key: key.identity.uri,
            )
        )
        plan_identity = canonical_identity(
            {
                "schema": "literate-ai/rebuild-derivation-plan@2",
                "component_lock_identities": [_identity("component-lock").uri],
                "cache_key_identities": [key.identity.uri for key in keys],
            }
        )
        manifest = RebuildSourceCacheDerivationManifest(
            planning_request_identity=_identity("planning-request"),
            project_revision_identity=_identity("project"),
            lifecycle_driver_identity=_identity("lifecycle-driver"),
            lifecycle_plan_identity=plan_identity,
            component_lock_identities=(_identity("component-lock"),),
            cache_keys=keys,
        )
        control = RebuildSourceCacheControl(
            _identity("request"),
            _identity("project"),
            None,
            manifest,
            manifest.component_lock_identities,
        )
        decision = RebuildSourceCacheDecision.fresh(control)
        subject = _identity("subject")

        def member(key) -> RebuildSourceCacheLifecycleMember:
            def evidence(label: str) -> ContentIdentity:
                return canonical_identity({"label": label, "key": key.identity.uri})

            return RebuildSourceCacheLifecycleMember(
                cache_key=key,
                accepted_entry_identity=None,
                source_tree_identity=evidence("tree"),
                source_intelligence_identity=evidence("index"),
                build_evidence_identity=evidence("build"),
                test_evidence_identity=evidence("test"),
                acceptance_evidence_identity=evidence("acceptance"),
                workspace_admission_identity=evidence("workspace"),
                provenance_evidence_identity=evidence("provenance"),
                source_sbom_identity=evidence("source-sbom"),
                resolved_sbom_identity=evidence("resolved-sbom"),
            )

        def binding(selected_keys) -> RebuildSourceCacheLifecycleBinding:
            members = tuple(
                sorted(
                    (member(key) for key in selected_keys),
                    key=lambda item: item.cache_key.identity.uri,
                )
            )
            return RebuildSourceCacheLifecycleBinding(
                control_identity=control.identity,
                decision_identity=decision.identity,
                lifecycle_request_identity=control.lifecycle_request_identity,
                lifecycle_plan_identity=manifest.lifecycle_plan_identity,
                receipt_subject_identity=subject,
                component_lock_identities=control.component_lock_identities,
                derivation_key_identities=tuple(
                    item.cache_key.identity for item in members
                ),
                members=members,
            )

        def receipt_for(
            lifecycle: RebuildSourceCacheLifecycleBinding,
        ) -> ProjectTestReceipt:
            evidence = {
                "lifecycle-plan": lifecycle.lifecycle_plan_identity,
                "source-cache-lifecycle": lifecycle.identity,
                **{
                    kind: lifecycle.evidence_identity(kind)
                    for kind in lifecycle.EVIDENCE_KINDS
                },
            }
            return ProjectTestReceipt(
                project_id="fixture",
                project_revision_identity=control.project_revision_identity,
                subject_identity=subject,
                suite=VersionedContentRef(
                    "test-suite", "fixture", "1.0.0", _identity("suite")
                ),
                outcome="passed",
                summary=ProjectTestSummary(1, 1, 0, 0),
                result_identity=_identity("result"),
                evidence=tuple(
                    ProjectTestEvidence(kind, identity)
                    for kind, identity in sorted(evidence.items())
                ),
            )

        complete = binding(keys)
        complete.validate_against(control, decision, receipt_for(complete))

        omitted = binding(keys[:1])
        with self.assertRaisesRegex(ContractValidationError, "exactly cover"):
            omitted.validate_against(control, decision, receipt_for(omitted))

        added = binding((*keys, _cache_key(selector="model-c")))
        with self.assertRaisesRegex(ContractValidationError, "exactly cover"):
            added.validate_against(control, decision, receipt_for(added))

    def test_hit_can_skip_only_generation_and_never_claim_acceptance(self) -> None:
        key = _cache_key()
        entry = _identity("entry")
        item = RebuildSourceCacheDecisionItem(
            key,
            RebuildSourceCacheOutcome.HIT,
            candidate_identities=(entry,),
            selected_entry_identity=entry,
            source_tree_identity=_identity("tree"),
            current_acceptance_trusted=False,
            generation_skipped=True,
        )
        self.assertTrue(item.generation_skipped)
        self.assertFalse(item.current_acceptance_trusted)
        SchemaCatalog().validate(item.SCHEMA, item.to_dict())
        with self.assertRaises(ContractValidationError):
            replace(item, current_acceptance_trusted=True)

    def test_driver_session_returns_an_exact_non_mutating_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = _project(root / "project")
            configuration = _configuration(SourceCacheMode.READ_WRITE, _target())
            project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=configuration),
            )
            cache_root = root / "absent-cache"
            control = RebuildSourceCacheControl(
                _identity("request"),
                _identity("project"),
                configuration,
                _manifest(),
                (_identity("component-lock"),),
                operator_roots=(
                    RebuildSourceCacheOperatorRoot("cache-root", str(cache_root)),
                ),
            )
            session = RebuildSourceCacheSession(control, project=project)

            self.assertIsNone(
                session.resolve(_cache_key(), destination=root / "cached-source")
            )
            decision = session.decision()

            self.assertEqual(decision.items[0].outcome, RebuildSourceCacheOutcome.MISS)
            self.assertFalse(decision.items[0].generation_skipped)
            self.assertFalse(decision.items[0].current_acceptance_trusted)
            self.assertFalse(cache_root.exists())
            self.assertFalse((root / "cached-source").exists())

    def test_explicit_identity_selects_one_ambiguous_exact_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "cache"
            caller_cas = FileSystemCAS(root / "caller-cas")
            key = _cache_key()
            first = _accepted_entry_for_lock(caller_cas, suffix="first", key=key)
            selected = _accepted_entry_for_lock(caller_cas, suffix="selected", key=key)
            writable = _configuration(SourceCacheMode.READ_WRITE, _target())
            project = _project(root / "project")
            writable_project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=writable),
            )
            publisher = SourceCacheResolver.from_configuration(
                writable,
                project=writable_project,
                operator_roots={"cache-root": cache_root},
            )
            publisher.publish(first, caller_cas=caller_cas)
            publisher.publish(selected, caller_cas=caller_cas)
            readable = _configuration(
                SourceCacheMode.READ_ONLY,
                _target(),
                require_unique=True,
            )
            project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=readable),
            )
            control = RebuildSourceCacheControl(
                _identity("request"),
                _identity("project"),
                readable,
                _manifest(key=key),
                (_identity("component-lock"),),
                requested_entry_identities=(selected.identity,),
                operator_roots=(
                    RebuildSourceCacheOperatorRoot("cache-root", str(cache_root)),
                ),
            )
            session = RebuildSourceCacheSession(control, project=project)

            materialized = session.resolve(
                key,
                destination=root / "selected-source",
            )
            decision = session.decision()

            assert materialized is not None
            self.assertEqual(materialized.entry_identity, selected.identity)
            self.assertEqual(
                decision.items[0].selected_entry_identity, selected.identity
            )
            self.assertEqual(decision.items[0].outcome, RebuildSourceCacheOutcome.HIT)
            self.assertTrue(decision.items[0].generation_skipped)
            self.assertFalse(decision.items[0].current_acceptance_trusted)

    def test_cache_hit_rejects_a_foreign_component_lock_before_materialization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            cache_root = root / "cache"
            caller_cas = FileSystemCAS(root / "caller-cas")
            key = _cache_key()
            foreign = _accepted_entry_for_lock(
                caller_cas,
                key=key,
                component_lock_identity=_identity("foreign-component-lock"),
            )
            writable = _configuration(SourceCacheMode.READ_WRITE, _target())
            project = _project(root / "project")
            writable_project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=writable),
            )
            SourceCacheResolver.from_configuration(
                writable,
                project=writable_project,
                operator_roots={"cache-root": cache_root},
            ).publish(foreign, caller_cas=caller_cas)
            readable = _configuration(SourceCacheMode.READ_ONLY, _target())
            project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=readable),
            )
            control = RebuildSourceCacheControl(
                _identity("request"),
                _identity("project"),
                readable,
                _manifest(key=key),
                (_identity("component-lock"),),
                operator_roots=(
                    RebuildSourceCacheOperatorRoot("cache-root", str(cache_root)),
                ),
            )
            destination = root / "cached-source"

            with self.assertRaisesRegex(
                RebuildSourceCacheProtocolError, "outside the outer-owned rebuild plan"
            ):
                RebuildSourceCacheSession(control, project=project).resolve(
                    key, destination=destination
                )
            self.assertFalse(destination.exists())


class RebuildSourceCachePublicationTests(unittest.TestCase):
    def test_outer_cli_realizes_offer_only_after_receipt_bindings_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runtime = root / "runtime"
            protocol_root = runtime / ".litai"
            protocol_root.mkdir(parents=True)
            project = _project(root / "project")
            configuration = _configuration(SourceCacheMode.READ_WRITE, _target())
            project = LoadedProject(
                project.root,
                replace(project.definition, source_cache=configuration),
            )
            cache_root = root / "published-cache"
            request = _identity("request")
            caller_cas = FileSystemCAS(runtime / "caller-cas")
            entry = _accepted_entry_for_lock(caller_cas)
            control = RebuildSourceCacheControl(
                request,
                _identity("project"),
                configuration,
                _manifest(key=entry.derivation.cache_key),
                (_identity("component-lock"),),
                operator_roots=(
                    RebuildSourceCacheOperatorRoot("cache-root", str(cache_root)),
                ),
            )
            item = RebuildSourceCacheDecisionItem(
                entry.derivation.cache_key,
                RebuildSourceCacheOutcome.MISS,
            )
            decision = RebuildSourceCacheDecision(
                control.identity,
                request,
                control.configuration_identity,
                SourceCacheMode.READ_WRITE.value,
                True,
                (item,),
            )
            subject = _identity("subject")
            lifecycle_plan = control.derivation_manifest.lifecycle_plan_identity
            lifecycle_member = RebuildSourceCacheLifecycleMember(
                cache_key=entry.derivation.cache_key,
                accepted_entry_identity=entry.identity,
                source_tree_identity=entry.derivation.source_tree_identity,
                source_intelligence_identity=_identity("source-intelligence"),
                build_evidence_identity=entry.derivation.build_evidence_identity,
                test_evidence_identity=entry.derivation.test_evidence_identity,
                acceptance_evidence_identity=entry.derivation.acceptance_identity,
                workspace_admission_identity=_identity("current-workspace-admission"),
                provenance_evidence_identity=entry.derivation.provenance_identity,
                source_sbom_identity=entry.derivation.source_sbom_identity,
                resolved_sbom_identity=entry.derivation.resolved_sbom_identity,
            )
            lifecycle = RebuildSourceCacheLifecycleBinding(
                control_identity=control.identity,
                decision_identity=decision.identity,
                lifecycle_request_identity=request,
                lifecycle_plan_identity=lifecycle_plan,
                receipt_subject_identity=subject,
                component_lock_identities=control.component_lock_identities,
                derivation_key_identities=(entry.derivation.cache_key.identity,),
                members=(lifecycle_member,),
            )
            acceptance = lifecycle.evidence_identity("acceptance-result")
            admission = lifecycle.evidence_identity("workspace-admission")
            offer = RebuildSourceCachePublicationOffer(
                control.identity,
                decision.identity,
                request,
                control.component_lock_identities,
                subject,
                acceptance,
                admission,
                "local",
                (RebuildSourceCachePublicationMember(entry, "caller-cas"),),
            )
            foreign_entry = _accepted_entry_for_lock(
                caller_cas,
                suffix="foreign",
                key=entry.derivation.cache_key,
                component_lock_identity=_identity("foreign-component-lock"),
            )
            with self.assertRaisesRegex(
                ContractValidationError, "outside the exact planned Component lock set"
            ):
                replace(
                    offer,
                    members=(
                        RebuildSourceCachePublicationMember(
                            foreign_entry, "caller-cas"
                        ),
                    ),
                ).validate_against(control, decision, lifecycle)
            control_path = protocol_root / "source-cache-control.json"
            decision_path = protocol_root / "source-cache-decision.json"
            lifecycle_path = protocol_root / "source-cache-lifecycle.json"
            publication_path = protocol_root / "source-cache-publication.json"
            protocol = RebuildSourceCacheProtocol(
                runtime_root=runtime,
                control_path=control_path,
                decision_path=decision_path,
                lifecycle_path=lifecycle_path,
                publication_path=publication_path,
                derivation_manifest_path=(
                    protocol_root / "source-cache-derivation-manifest.json"
                ),
                control=control,
                runtime_root_identity=protocol_directory_identity(runtime),
                protocol_root_identity=protocol_directory_identity(protocol_root),
            )
            write_rebuild_source_cache_derivation_manifest(
                protocol.derivation_manifest_path,
                control.derivation_manifest,
                directory_identity=protocol.protocol_root_identity,
            )
            write_rebuild_source_cache_publication(
                publication_path,
                offer,
                directory_identity=protocol.protocol_root_identity,
            )
            schemas = SchemaCatalog()
            schemas.validate(lifecycle_member.SCHEMA, lifecycle_member.to_dict())
            schemas.validate(lifecycle.SCHEMA, lifecycle.to_dict())
            schemas.validate(offer.members[0].SCHEMA, offer.members[0].to_dict())
            schemas.validate(offer.SCHEMA, offer.to_dict())
            schemas.validate(
                offer.publication_result["schema"], offer.publication_result
            )
            receipt = ProjectTestReceipt.from_dict(
                {
                    "schema": "urn:literate-ai:schema:v1:project-test-receipt",
                    "project": "fixture",
                    "project_revision": control.project_revision_identity.uri,
                    "subject": subject.uri,
                    "suite": {
                        "id": "fixture-e2e",
                        "version": "1.0.0",
                        "revision": _identity("suite").uri,
                    },
                    "tests": 1,
                    "result": _identity("result").uri,
                    "evidence": {
                        **{
                            kind: lifecycle.evidence_identity(kind).uri
                            for kind in lifecycle.EVIDENCE_KINDS
                        },
                        "lifecycle-plan": lifecycle_plan.uri,
                        "source-cache-lifecycle": lifecycle.identity.uri,
                        "source-cache-publication-result": (
                            offer.publication_result_identity.uri
                        ),
                    },
                }
            )
            lifecycle.validate_against(control, decision, receipt)
            substituted_locks = (_identity("substituted-component-lock"),)
            with self.assertRaisesRegex(ContractValidationError, "planned Component"):
                replace(
                    lifecycle,
                    component_lock_identities=substituted_locks,
                ).validate_against(control, decision, receipt)
            with self.assertRaisesRegex(ContractValidationError, "planned Component"):
                replace(
                    offer,
                    component_lock_identities=substituted_locks,
                ).validate_against(control, decision, lifecycle)
            with self.assertRaisesRegex(ContractValidationError, "lifecycle-plan"):
                replace(
                    lifecycle,
                    lifecycle_plan_identity=_identity("stale-plan"),
                ).validate_against(control, decision, receipt)
            with self.assertRaisesRegex(
                ContractValidationError, "exactly one current entry"
            ):
                replace(offer, members=()).validate_against(
                    control,
                    decision,
                    lifecycle,
                )
            stale_lifecycle = replace(
                lifecycle,
                members=(
                    replace(
                        lifecycle_member,
                        test_evidence_identity=_identity("stale-test"),
                    ),
                ),
            )
            with self.assertRaisesRegex(
                ContractValidationError, "test_evidence_identity"
            ):
                offer.validate_against(control, decision, stale_lifecycle)

            hit_item = RebuildSourceCacheDecisionItem(
                entry.derivation.cache_key,
                RebuildSourceCacheOutcome.HIT,
                candidate_identities=(entry.identity,),
                selected_entry_identity=entry.identity,
                source_tree_identity=entry.derivation.source_tree_identity,
                current_acceptance_trusted=False,
                generation_skipped=True,
            )
            hit_decision = replace(
                decision,
                fresh_generation_required=False,
                items=(hit_item,),
            )
            hit_member = replace(
                lifecycle_member,
                accepted_entry_identity=None,
            )
            hit_lifecycle = replace(
                lifecycle,
                decision_identity=hit_decision.identity,
                members=(hit_member,),
            )
            hit_offer = replace(
                offer,
                decision_identity=hit_decision.identity,
                acceptance_evidence_identity=hit_lifecycle.evidence_identity(
                    "acceptance-result"
                ),
                workspace_admission_identity=hit_lifecycle.evidence_identity(
                    "workspace-admission"
                ),
                members=(),
            )
            hit_offer.validate_against(control, hit_decision, hit_lifecycle)
            with self.assertRaisesRegex(ContractValidationError, "omit cache hits"):
                replace(
                    hit_offer,
                    members=(RebuildSourceCachePublicationMember(entry, "caller-cas"),),
                ).validate_against(control, hit_decision, hit_lifecycle)

            published = publish_rebuild_source_cache_offer(
                project,
                protocol,
                decision,
                lifecycle,
                receipt,
                phases=("publish-source-cache",),
            )

            self.assertEqual(published, (entry.identity,))
            self.assertEqual(
                publish_rebuild_source_cache_offer(
                    project,
                    protocol,
                    decision,
                    lifecycle,
                    receipt,
                    phases=("publish-source-cache",),
                ),
                published,
            )
            resolver = SourceCacheResolver.from_configuration(
                configuration,
                project=project,
                operator_roots={"cache-root": cache_root},
            )
            candidates = resolver.resolve(entry.derivation.cache_key)
            self.assertEqual(tuple(item.identity for item in candidates), published)
            self.assertFalse(candidates[0].current_acceptance_trusted)


if __name__ == "__main__":
    unittest.main()
