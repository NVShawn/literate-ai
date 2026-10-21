"""Initial original-source choice; never conversion or execution qualification."""

import re
from dataclasses import dataclass

from .identity import ContentIdentity, canonical_identity


@dataclass(frozen=True, slots=True)
class OriginalProjectAdoptionPlan:
    project_id: str
    project_authority: ContentIdentity
    git_commit: str
    git_tree: str

    def __post_init__(self):
        if not isinstance(self.project_id, str) or not self.project_id:
            raise ValueError("original_adoption.project-id-required")
        if not isinstance(self.project_authority, ContentIdentity):
            raise TypeError("original_adoption.typed-project-authority-required")
        for value in (self.git_commit, self.git_tree):
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value
            ):
                raise ValueError("original_adoption.git-object-identity-required")
        if len(self.git_commit) != len(self.git_tree):
            raise ValueError("original_adoption.git-object-format-mismatch")

    def to_dict(self):
        return {
            "schema": "literate-ai/original-project-adoption@1",
            "project_id": self.project_id,
            "project_authority": self.project_authority.uri,
            "git_commit": self.git_commit,
            "git_tree": self.git_tree,
            "initial_stage": "original",
            "release_authority": "original-source",
            "source_moved": False,
            "execution_qualified": False,
        }

    @property
    def identity(self):
        return canonical_identity(self.to_dict())
