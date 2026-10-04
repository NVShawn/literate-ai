from __future__ import annotations
"""Shared fixtures extracted from ``tests.unit.test_evidence_graph``."""

import hashlib



from dataclasses import replace


from unittest.mock import Mock


from literate_ai.application.evidence_resolution import ConfiguredEvidenceResolver

from literate_ai.application.evidence_verification import verify_run_evidence_graph

from literate_ai.contracts.blobs import BlobRef

from literate_ai.contracts.identity import canonical_identity

from literate_ai.security.evidence import (
    DSSE_MEDIA_TYPE,
    STATEMENT_MEDIA_TYPE,
    DerivationRun,
    Ed25519EvidenceSigner,
    EvidenceArtifact,
    EvidenceLocator,
    EvidenceMatrix,
    EvidenceRevocations,
    EvidenceRunContext,
    EvidenceSignerRule,
    EvidenceStatement,
    EvidenceTrustPolicy,
    PlatformRun,
    RunEvidenceExpectation,
)

from literate_ai.security.evidence.graph import (
    EvidenceRunRequirement,
    EvidenceVerificationState,
)

from literate_ai.security.evidence.records import PREDICATE_TYPES

from literate_ai.security.evidence.storage import (
    EvidenceNotFoundError,
)

class _Graph:
    """Fixed trusted baseline; later signed mutations leave its expectations intact."""

    def __init__(self):
        self.contents = {}
        self.reads = []
        self.signer = Ed25519EvidenceSigner(bytes(range(32)))
        self.records = {}
        self.requirements = {}
        self.refs = {}
        self.contexts = {
            name: EvidenceRunContext(
                name,
                "https://example.test/repository",
                "a" * 40,
                canonical_identity("workflow"),
                canonical_identity(name),
                100,
                200,
            )
            for name in ("derive", "linux", "windows", "matrix")
        }
        source = self.blob(b"sources")
        spec = self.blob(b"specification")
        journal = self.blob(b"journal")
        derivation = DerivationRun(
            self.contexts["derive"],
            source,
            (EvidenceArtifact("spec", spec),),
            journal,
            "passed",
        )
        self.add(
            "derive",
            derivation,
            (),
            (("input/spec", spec), ("journal", journal), ("subject", source)),
        )
        for platform in ("linux", "windows"):
            subject = self.blob(platform.encode())
            environment = self.blob((platform + "-environment").encode())
            check = self.blob(b"passed")
            record = PlatformRun(
                self.contexts[platform],
                subject,
                self.refs["derive"],
                environment,
                (EvidenceArtifact("tests", check),),
                "passed",
            )
            self.add(
                platform,
                record,
                (EvidenceArtifact("derivation", self.refs["derive"]),),
                (
                    ("check/tests", check),
                    ("environment", environment),
                    ("subject", subject),
                ),
            )
        subject = self.blob(b"release-manifest")
        cells = tuple(
            EvidenceArtifact(name, self.refs[name]) for name in ("linux", "windows")
        )
        self.add(
            "matrix",
            EvidenceMatrix(
                self.contexts["matrix"], subject, ("linux", "windows"), cells
            ),
            cells,
            (("subject", subject),),
        )
        self.policy = EvidenceTrustPolicy(
            (
                EvidenceSignerRule(
                    self.signer.public_key,
                    "local:test",
                    (self.contexts["matrix"].repository,),
                    (self.contexts["matrix"].workflow,),
                    tuple(
                        sorted(
                            (c.target for c in self.contexts.values()),
                            key=lambda x: x.uri,
                        )
                    ),
                    tuple(sorted(PREDICATE_TYPES)),
                    ("local",),
                    0,
                    1000,
                ),
            ),
            1,
            60,
            300,
            2,
            60,
            90,
        )
        self.snapshot = EvidenceVerificationState(
            210, EvidenceRevocations(200, 250, (), (), ())
        )
        self.state = Mock(return_value=self.snapshot)
        self.resolver = ConfiguredEvidenceResolver({"local": self})

    def blob(self, content, media="application/octet-stream"):
        reference = BlobRef(
            hashlib.sha256(content).hexdigest(), len(content), media_type=media
        )
        self.contents[reference.identity] = content
        return reference

    def envelope(self, record):
        return self.blob(
            self.signer.sign(
                STATEMENT_MEDIA_TYPE, EvidenceStatement(record).to_bytes()
            ).to_bytes(),
            DSSE_MEDIA_TYPE,
        )

    def add(self, name, record, children, artifacts):
        self.records[name] = record
        reference = self.envelope(record)
        self.refs[name] = reference
        context = self.contexts[name]
        expectation = RunEvidenceExpectation(
            record.SCHEMA,
            record.subject,
            context.invocation_id,
            context.repository,
            context.revision,
            context.workflow,
            context.target,
            90,
            220,
            record.required_cells if isinstance(record, EvidenceMatrix) else (),
        )
        self.requirements[name] = EvidenceRunRequirement(
            reference, expectation, children, artifacts
        )

    def resign(self, name, record):
        """Bind new envelope identities, preserving independently expected contents."""
        old = self.refs[name]
        self.records[name] = record
        new = self.envelope(record)
        self.refs[name] = new
        self.requirements[name] = replace(self.requirements[name], envelope=new)
        for parent in tuple(self.records):
            if parent == name:
                continue
            parent_record = self.records[parent]
            if (
                isinstance(parent_record, PlatformRun)
                and parent_record.derivation == old
            ):
                updated = replace(parent_record, derivation=new)
            elif isinstance(parent_record, EvidenceMatrix) and any(
                c.blob == old for c in parent_record.cells
            ):
                updated = replace(
                    parent_record,
                    cells=tuple(
                        replace(c, blob=new) if c.blob == old else c
                        for c in parent_record.cells
                    ),
                )
            else:
                continue
            requirement = self.requirements[parent]
            self.requirements[parent] = replace(
                requirement,
                children=tuple(
                    replace(c, blob=new) if c.blob == old else c
                    for c in requirement.children
                ),
            )
            self.resign(parent, updated)

    def get_bytes(self, reference):
        self.reads.append(reference)
        try:
            return self.contents[reference.identity]
        except KeyError:
            raise EvidenceNotFoundError() from None

    def locators(self):
        refs = {}
        for req in self.requirements.values():
            for ref in (
                req.envelope,
                *(child.blob for child in req.children),
                *(ref for _, ref in req.artifacts),
            ):
                refs[ref.identity] = ref
        return tuple(EvidenceLocator("local", refs[key], 1000) for key in sorted(refs))

    def verify(self, **kwargs):
        arguments = dict(
            requirements=tuple(self.requirements.values()),
            resolver=self.resolver,
            locators=self.locators(),
            policy=self.policy,
            current_state=self.state,
        )
        arguments.update(kwargs)
        return verify_run_evidence_graph(self.refs["matrix"], **arguments)

