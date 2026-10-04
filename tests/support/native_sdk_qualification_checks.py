"""Adversarial checks over records produced by the real native SDK fixture."""

from copy import deepcopy
from datetime import datetime, timedelta

from literate_ai.adapters.native_sdk_qualification import sdk_process_fields
from literate_ai.adapters.qualification_capture import (
    QualificationCaptureError,
    QualificationEvidenceReader,
    QualificationEvidenceRecorder,
    verify_qualification_execution,
)
from literate_ai.contracts.executable_components.commands import ComponentCommandPhase
from literate_ai.contracts.identity import ContentIdentity, canonical_identity
from literate_ai.contracts.standard_post_source_evidence import (
    StandardExecutionEvidence,
)
from literate_ai.security import AuthorizationRevocationSet


def check_sdk_records(test, recorder, plan, phase, process, contract):
    command = contract.command(ComponentCommandPhase(phase))

    def reader(excluding=()):
        return QualificationEvidenceReader(
            tuple(item for item in recorder.entries if item[0] not in excluding),
            max_bytes=20_000_000,
            max_records=2000,
        )

    def verify(document, retained=None):
        return sdk_process_fields(
            reader() if retained is None else retained,
            plan=plan,
            process=document,
            phase=phase,
            command_identity=command.identity,
        )

    identity = ContentIdentity.parse_uri(process["native_sdk_execution_identity"])
    record = reader().read_json(identity)
    test.assertEqual(verify(process), {"native_sdk_execution_identity": identity.uri})
    manifest_identity = ContentIdentity.from_dict(record["manifest_identity"])
    manifest = reader().read_json(manifest_identity)
    bindings = [
        reader().read_json(ContentIdentity.from_dict(item["input_identity"]))
        for item in manifest["imports"]
    ]
    for missing in (
        identity,
        manifest_identity,
        canonical_identity(record["request"]),
        canonical_identity(record["authorization"]),
        *(canonical_identity(item) for item in record["dependencies"]),
        *(ContentIdentity.from_dict(item["selection"]) for item in bindings),
        *(
            ContentIdentity.from_dict(item["source_admission"]["build_plan"])
            for item in bindings
        ),
        *(
            ContentIdentity.from_dict(item["input_identity"])
            for item in manifest["imports"]
        ),
    ):
        with (
            test.subTest(missing=missing.uri),
            test.assertRaises(QualificationCaptureError),
        ):
            verify(process, reader((missing,)))
    omitted = {
        key: value
        for key, value in process.items()
        if key != "native_sdk_execution_identity"
    }
    with test.assertRaises(QualificationCaptureError):
        verify(omitted)

    # Re-hash dependent records: these cases must fail on semantics, not merely on
    # an unchanged digest after tampering. Current importer trust is tested elsewhere.
    def reject(value, additions=()):
        # Retain the original fixture and a rehashed copy of its affected records.
        # Windows runtime graphs can nearly fill the original fixture's budget.
        # Allow one further graph plus small mutation fields, still under a bound.
        mutation_bytes = 2 * recorder.max_bytes + 65536
        mutation_records = 2 * recorder.max_records
        changed = QualificationEvidenceRecorder(
            max_bytes=mutation_bytes, max_records=mutation_records
        )
        for _, payload in recorder.entries:
            changed.remember_bytes(payload)
        for document in additions:
            changed.remember_json(document)
        value["request"]["builder_id"] = canonical_identity(
            value["command_binding"]
        ).uri
        value["authorization"]["request_digest"] = canonical_identity(
            value["request"]
        ).uri
        changed.remember_json(value["request"])
        changed.remember_json(value["authorization"])
        altered_process = {
            **process,
            "native_sdk_execution_identity": changed.remember_json(value).uri,
        }
        retained = QualificationEvidenceReader(
            changed.entries, max_bytes=mutation_bytes, max_records=mutation_records
        )
        # Fixture construction and capacity failures must not satisfy rejection.
        with test.assertRaises(QualificationCaptureError):
            verify(altered_process, retained)

    foreign = canonical_identity("foreign SDK proof")
    mutations = (
        (
            "phase",
            lambda value: value.update(phase="build" if phase != "build" else "test"),
        ),
        (
            "command",
            lambda value: value["command_binding"].update(
                command_identity=foreign.to_dict()
            ),
        ),
        ("tool", lambda value: value["request"].update(toolchain_digest=foreign.uri)),
        (
            "source",
            lambda value: value["request"].update(source_bundle_digest=foreign.uri),
        ),
        ("privilege", lambda value: value["authorization"].update(privileges=[])),
        ("blocked", lambda value: value["authorization"].update(profile="blocked")),
        (
            "classification",
            lambda value: value["authorization"].update(
                classification_digest=foreign.uri
            ),
        ),
        ("missing-check", lambda value: value["checks"].pop()),
        (
            "reversed-time",
            lambda value: value["checks"][1].update(
                checked_at=(
                    datetime.fromisoformat(value["checks"][0]["checked_at"])
                    - timedelta(seconds=1)
                ).isoformat()
            ),
        ),
        (
            "expired",
            lambda value: value["checks"][1].update(
                checked_at=value["authorization"]["expires_at"]
            ),
        ),
        (
            "missing-revocations",
            lambda value: value["checks"][0].update(revocations=[]),
        ),
        ("revoked", lambda value: value["authorization"].update(revoked=True)),
    )
    for name, mutate in mutations:
        value = deepcopy(record)
        mutate(value)
        with test.subTest(mutation=name):
            reject(value)

    value = deepcopy(record)
    state = value["checks"][1]["revocations"][0]
    state["state"] = (
        AuthorizationRevocationSet.from_dict(state["state"])
        .revoke(
            value["authorization"]["authorization_id"],
            actor="fixture operator",
            reason="historical grant revoked after launch",
        )
        .to_dict()
    )
    with test.subTest(historical_revocation=True):
        reject(value)

    for name in ("target", "snapshot", "consumer", "foreign-input"):
        value, changed = deepcopy(record), deepcopy(manifest)
        if name == "target":
            changed["target_identity"] = foreign.to_dict()
        elif name == "consumer":
            changed["consumer_revision"] = foreign.to_dict()
        elif name == "foreign-input":
            changed["imports"][0]["input_identity"] = foreign.to_dict()
        else:
            changed["imports"][0]["snapshot"]["license_identity"] = foreign.to_dict()
        updated = canonical_identity(changed).to_dict()
        value["manifest_identity"] = value["command_binding"]["manifest"] = updated
        with test.subTest(manifest=name):
            reject(value, (changed,))

    value = deepcopy(record)
    dependency = value["dependencies"][0]
    image = next(
        item
        for item in dependency["components"]
        if item["bom-ref"].startswith("urn:literate-ai:native-sdk-image:")
    )
    dependency["components"].remove(image)
    dependency["edges"] = [
        edge for edge in dependency["edges"] if image["bom-ref"] not in edge
    ]
    runtime = canonical_identity(
        {
            "consumer": plan.component_revision.to_dict(),
            "target": manifest["target_identity"],
            "inputs": [item["input_identity"] for item in manifest["imports"]],
            "dependencies": [
                canonical_identity(item).to_dict() for item in value["dependencies"]
            ],
        }
    )
    value["runtime_identity"] = value["command_binding"]["runtime"] = runtime.to_dict()
    value["authorization"]["classification_digest"] = runtime.uri
    with test.subTest(missing_native_image=True):
        reject(value, (dependency,))

    if phase == "execute":
        # Export/build membership belongs to the other qualification verifiers.
        # Here use explicit fixture identities to exercise the public process reader.
        export = canonical_identity("fixture export")
        evidence = StandardExecutionEvidence(
            component_revision=plan.component_revision,
            build_evidence_identity=canonical_identity("fixture build evidence"),
            export_identities=(export,),
            root_export_identity=export,
            execution_contract_identity=command.identity,
            runtime_identity=canonical_identity("fixture runtime"),
            artifact_custody_identity=canonical_identity("fixture artifact custody"),
            observation_identity=recorder.remember_json(process),
            stdout_identity=ContentIdentity.parse_uri(process["stdout_identity"]),
            stderr_identity=ContentIdentity.parse_uri(process["stderr_identity"]),
            exit_code=0,
        )
        verify_qualification_execution(
            reader(), build_plan_identity=plan.identity, execution=evidence
        )
