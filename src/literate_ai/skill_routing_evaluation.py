"""Behavioral regression harness for skill/capability selection (SKILL-ROUTING-001).

Skill and capability-contract selection is validated structurally elsewhere in this
framework (schema shape, DAG acyclicity, exact pinned content identities), but nothing
checks that the *right* skill actually gets selected for a given generation intent, or
notices when one only appears to because its description's vocabulary happens to
overlap a prompt rather than because the match is meaningful. This module runs a corpus
of synthetic prompts against the project's real `specification-to-source` skill catalog
and classifies each outcome:

- ``correct``: the intended skill scored highest, with confident description overlap.
- ``fragile_pass``: the intended skill scored highest, but the overlap was thin enough
  that the match likely came from prior/general knowledge rather than the description
  actually being distinguishing.
- ``misroute``: a different skill scored highest, or the intended skill never reached
  the firing threshold at all.
- ``over_greedy``: a prompt with no intended skill (``expected_skill_id`` is ``None``)
  nonetheless had a skill score high enough to "fire".

Scoring is a simple, auditable token-overlap ratio between the prompt and each skill's
frontmatter ``description`` plus ``title`` -- not a live model call. This is deliberate:
it measures whether a skill's *own declared description* actually distinguishes it for
a given prompt, which is exactly the property `#`SKILL-ROUTING-001` cares about. It is
not a claim about what any specific coding CLI or agent would actually pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from literate_ai.contracts.authoring_markdown import parse_authoring_markdown
from literate_ai.projects import LoadedProject, specification_to_source_skill_paths

DEFAULT_FIRE_THRESHOLD = 0.05
DEFAULT_FRAGILE_THRESHOLD = 0.15

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# A minimal, deliberately small stopword list -- just enough that generic
# connective words (and the "Use for Literate AI workflow tasks." boilerplate
# every skill description shares) never drive a match on their own. This is
# not a general-purpose NLP stopword list; it only needs to keep unrelated
# prompts from "firing" on shared function words rather than real content
# overlap, which is exactly the failure this harness exists to catch.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "tasks",
        "that",
        "the",
        "this",
        "to",
        "use",
        "with",
    }
)


class SkillRoutingEvaluationError(RuntimeError):
    """A skill-routing evaluation could not be prepared or run."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _tokenize(text: str) -> frozenset[str]:
    return frozenset(_TOKEN_PATTERN.findall(text.casefold())) - _STOPWORDS


@dataclass(frozen=True, slots=True)
class SkillRoutingCandidate:
    """One catalog skill's routing-relevant text, tokenized once."""

    skill_id: str
    path: str
    description: str
    tokens: frozenset[str]


def load_skill_routing_candidates(
    project: LoadedProject,
) -> tuple[SkillRoutingCandidate, ...]:
    """Load every declared specification-to-source skill's routing text."""

    candidates: list[SkillRoutingCandidate] = []
    for path in sorted(specification_to_source_skill_paths(project)):
        relative = path.relative_to(project.root).as_posix()
        try:
            frontmatter, _body = parse_authoring_markdown(
                path.read_bytes(), source=relative
            )
        except (OSError, UnicodeError) as exc:
            raise SkillRoutingEvaluationError(
                "skill_routing.skill_unreadable",
                f"could not read skill for routing evaluation: {relative}",
            ) from exc
        skill_id = frontmatter.get("skill_id")
        description = frontmatter.get("description")
        title = frontmatter.get("title")
        if not isinstance(skill_id, str) or not skill_id:
            raise SkillRoutingEvaluationError(
                "skill_routing.skill_id_missing",
                f"skill has no usable skill_id for routing evaluation: {relative}",
            )
        if not isinstance(description, str) or not description.strip():
            raise SkillRoutingEvaluationError(
                "skill_routing.description_missing",
                f"skill has no usable description for routing evaluation: {relative}",
            )
        routing_text = (
            description if not isinstance(title, str) else f"{title}. {description}"
        )
        candidates.append(
            SkillRoutingCandidate(
                skill_id, relative, description, _tokenize(routing_text)
            )
        )
    ids = tuple(item.skill_id for item in candidates)
    if len(set(ids)) != len(ids):
        raise SkillRoutingEvaluationError(
            "skill_routing.duplicate_skill_id",
            "skill catalog has duplicate skill IDs; cannot evaluate routing",
        )
    return tuple(candidates)


@dataclass(frozen=True, slots=True)
class SkillRoutingCase:
    """One synthetic prompt and the skill (if any) it is expected to select."""

    case_id: str
    prompt: str
    expected_skill_id: str | None


def load_skill_routing_corpus(path: Path) -> tuple[SkillRoutingCase, ...]:
    """Parse a JSON corpus file into typed evaluation cases."""

    import json

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SkillRoutingEvaluationError(
            "skill_routing.corpus_invalid",
            f"skill routing corpus could not be read: {path}",
        ) from exc
    if not isinstance(raw, list) or not raw:
        raise SkillRoutingEvaluationError(
            "skill_routing.corpus_invalid",
            "skill routing corpus must be a non-empty JSON array",
        )
    cases: list[SkillRoutingCase] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SkillRoutingEvaluationError(
                "skill_routing.corpus_invalid",
                f"skill routing corpus entry {index} must be an object",
            )
        case_id = entry.get("case_id")
        prompt = entry.get("prompt")
        expected = entry.get("expected_skill_id", "__missing__")
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(prompt, str)
            or not prompt.strip()
            or expected == "__missing__"
            or not (expected is None or isinstance(expected, str))
        ):
            raise SkillRoutingEvaluationError(
                "skill_routing.corpus_invalid",
                f"skill routing corpus entry {index} has an invalid shape",
            )
        if case_id in seen_ids:
            raise SkillRoutingEvaluationError(
                "skill_routing.corpus_invalid",
                f"skill routing corpus has a duplicate case_id: {case_id}",
            )
        seen_ids.add(case_id)
        cases.append(SkillRoutingCase(case_id, prompt, expected))
    return tuple(cases)


@dataclass(frozen=True, slots=True)
class SkillRoutingScore:
    skill_id: str
    score: float


@dataclass(frozen=True, slots=True)
class SkillRoutingOutcome:
    case_id: str
    prompt: str
    expected_skill_id: str | None
    winner: SkillRoutingScore | None
    runner_up: SkillRoutingScore | None
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "expected_skill_id": self.expected_skill_id,
            "winner": None
            if self.winner is None
            else {"skill_id": self.winner.skill_id, "score": self.winner.score},
            "runner_up": None
            if self.runner_up is None
            else {
                "skill_id": self.runner_up.skill_id,
                "score": self.runner_up.score,
            },
            "classification": self.classification,
        }


def _score(prompt_tokens: frozenset[str], candidate: SkillRoutingCandidate) -> float:
    """Jaccard overlap between the prompt and one candidate's routing text.

    Deliberately not recall-on-prompt-only (|overlap| / |prompt|): that formula
    structurally favors long, vocabulary-rich descriptions, which then win
    against short, precise descriptions purely by having more chances to share
    an incidental word with any prompt. Jaccard divides by the *union*, so a
    verbose description's own extra, prompt-irrelevant vocabulary works
    against it instead of being free credit.
    """

    if not prompt_tokens and not candidate.tokens:
        return 0.0
    union = prompt_tokens | candidate.tokens
    if not union:
        return 0.0
    overlap = prompt_tokens & candidate.tokens
    return len(overlap) / len(union)


def evaluate_skill_routing_case(
    case: SkillRoutingCase,
    candidates: tuple[SkillRoutingCandidate, ...],
    *,
    fire_threshold: float,
    fragile_threshold: float,
) -> SkillRoutingOutcome:
    prompt_tokens = _tokenize(case.prompt)
    ranked = sorted(
        (
            SkillRoutingScore(item.skill_id, _score(prompt_tokens, item))
            for item in candidates
        ),
        key=lambda scored: scored.score,
        reverse=True,
    )
    winner = ranked[0] if ranked and ranked[0].score >= fire_threshold else None
    runner_up = ranked[1] if len(ranked) > 1 else None

    if case.expected_skill_id is None:
        classification = "over_greedy" if winner is not None else "correct"
    elif winner is None or winner.skill_id != case.expected_skill_id:
        classification = "misroute"
    elif winner.score < fragile_threshold:
        classification = "fragile_pass"
    else:
        classification = "correct"

    return SkillRoutingOutcome(
        case.case_id,
        case.prompt,
        case.expected_skill_id,
        winner,
        runner_up,
        classification,
    )


@dataclass(frozen=True, slots=True)
class SkillRoutingReport:
    outcomes: tuple[SkillRoutingOutcome, ...]
    fire_threshold: float
    fragile_threshold: float

    @property
    def summary(self) -> dict[str, int]:
        counts = {"correct": 0, "misroute": 0, "over_greedy": 0, "fragile_pass": 0}
        for outcome in self.outcomes:
            counts[outcome.classification] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "fire_threshold": self.fire_threshold,
            "fragile_threshold": self.fragile_threshold,
            "summary": self.summary,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def evaluate_skill_routing(
    project: LoadedProject,
    corpus_path: Path,
    *,
    fire_threshold: float = DEFAULT_FIRE_THRESHOLD,
    fragile_threshold: float = DEFAULT_FRAGILE_THRESHOLD,
) -> SkillRoutingReport:
    """Run a synthetic prompt corpus against the project's real skill catalog."""

    if (
        fire_threshold <= 0
        or fragile_threshold <= fire_threshold
        or fragile_threshold > 1
    ):
        raise SkillRoutingEvaluationError(
            "skill_routing.thresholds_invalid",
            "fire_threshold must be positive and less than fragile_threshold <= 1",
        )
    candidates = load_skill_routing_candidates(project)
    corpus = load_skill_routing_corpus(corpus_path)
    known_ids = {item.skill_id for item in candidates}
    for case in corpus:
        if (
            case.expected_skill_id is not None
            and case.expected_skill_id not in known_ids
        ):
            raise SkillRoutingEvaluationError(
                "skill_routing.expected_skill_unknown",
                f"corpus case {case.case_id!r} expects an unknown skill_id: "
                f"{case.expected_skill_id}",
            )
    outcomes = tuple(
        evaluate_skill_routing_case(
            case,
            candidates,
            fire_threshold=fire_threshold,
            fragile_threshold=fragile_threshold,
        )
        for case in corpus
    )
    return SkillRoutingReport(outcomes, fire_threshold, fragile_threshold)
