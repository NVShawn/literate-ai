"""Distinct local retained-origin receipts; parsing is never admission."""

from dataclasses import dataclass
from typing import ClassVar

from .blobs import BlobRef
from .identity import canonical_identity
from .retained_project_lifecycle import _fields, _name

RETAINED_RECEIPT_ROOTS = frozenset(
    {
        "plan",
        "completed-run",
        "input-manifest",
        "output-manifest",
        "payload-manifest",
        "source-bom",
        "resolved-bom",
        "acceptance-policy-bytes",
    }
)


@dataclass(frozen=True, slots=True)
class RetainedProjectReceipt:
    project_id: str
    roots: tuple[tuple[str, BlobRef], ...]
    records: tuple[BlobRef, ...]
    passed: int
    skipped: int

    SCHEMA: ClassVar[str] = "literate-ai/retained-project-receipt@1"

    def __post_init__(self):
        _name(self.project_id)
        if (
            type(self.roots) is not tuple
            or len(self.roots) != len(RETAINED_RECEIPT_ROOTS)
            or any(
                type(v) is not tuple or len(v) != 2 or not isinstance(v[1], BlobRef)
                for v in self.roots
            )
            or tuple(v[0] for v in self.roots) != tuple(sorted(RETAINED_RECEIPT_ROOTS))
        ):
            raise ValueError("retained.receipt.exact-roots-required")
        if (
            type(self.records) is not tuple
            or not 1 <= len(self.records) <= 1_000_000
            or any(not isinstance(v, BlobRef) for v in self.records)
            or tuple(v.identity for v in self.records)
            != tuple(sorted({v.identity for v in self.records}))
            or not {v for _, v in self.roots} <= set(self.records)
        ):
            raise ValueError("retained.receipt.canonical-records-required")
        if (
            type(self.passed) is not int
            or self.passed < 1
            or type(self.skipped) is not int
            or self.skipped < 0
        ):
            raise ValueError("retained.receipt.successful-summary-required")

    def root(self, name):
        return dict(self.roots)[name]

    def to_dict(self):
        return {
            "schema": self.SCHEMA,
            "authority_mode": "retained-project",
            "source_authority": "original",
            "verification_scope": "local-runtime",
            "project_id": self.project_id,
            "outcome": "passed",
            "roots": {name: ref.to_dict() for name, ref in self.roots},
            "records": [v.to_dict() for v in self.records],
            "summary": {"passed": self.passed, "skipped": self.skipped, "failed": 0},
            "conversion_advanced": False,
            "release_authorized": False,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())

    @classmethod
    def from_dict(cls, value):
        value = _fields(
            value,
            {
                "schema",
                "authority_mode",
                "source_authority",
                "verification_scope",
                "project_id",
                "outcome",
                "roots",
                "records",
                "summary",
                "conversion_advanced",
                "release_authorized",
            },
        )
        summary = _fields(value["summary"], {"passed", "skipped", "failed"})
        roots = _fields(value["roots"], RETAINED_RECEIPT_ROOTS)
        if (
            value["schema"] != cls.SCHEMA
            or value["authority_mode"] != "retained-project"
            or value["source_authority"] != "original"
            or value["verification_scope"] != "local-runtime"
            or value["outcome"] != "passed"
            or type(summary["failed"]) is not int
            or summary["failed"] != 0
            or value["conversion_advanced"] is not False
            or value["release_authorized"] is not False
            or not isinstance(value["records"], list)
            or len(value["records"]) > 1_000_000
        ):
            raise ValueError("retained.receipt.original-local-authority-required")
        return cls(
            value["project_id"],
            tuple((name, BlobRef.from_dict(roots[name])) for name in sorted(roots)),
            tuple(BlobRef.from_dict(v) for v in value["records"]),
            summary["passed"],
            summary["skipped"],
        )
