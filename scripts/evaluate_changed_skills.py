"""Run NVIDIA SkillEvaluator only for new or modified Literate AI skills."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from literate_ai.application.agent_skill_catalog import AgentSkillCatalog

CHECKS = "schema,pii,license,quality,unicode,lint"
ORCHESTRATION_TEMPLATES = "src/literate_ai/project_template/orchestration"
_ORCHESTRATION_RENDERERS = {
    "src/literate_ai/adapters/orchestration_scaffold.py",
    "scripts/evaluate_changed_skills.py",
}


def _git_paths(repository: Path, base: str | None) -> set[str]:
    commands = []
    if base:
        commands.append(
            ("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...HEAD")
        )
    commands.extend(
        (
            ("diff", "--name-only", "--diff-filter=ACMRD"),
            ("diff", "--cached", "--name-only", "--diff-filter=ACMRD"),
            ("ls-files", "--others", "--exclude-standard"),
        )
    )
    paths: set[str] = set()
    for arguments in commands:
        completed = subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            capture_output=True,
            text=True,
        )
        paths.update(line for line in completed.stdout.splitlines() if line)
    return paths


def _catalogs(repository: Path) -> tuple[AgentSkillCatalog, ...]:
    catalogs = []
    skills = repository / "skills"
    if skills.is_dir():
        catalogs.append(
            AgentSkillCatalog.discover(
                repository,
                catalog_roots=(skills,),
                root_manifests=(repository / "SKILL.md",)
                if (repository / "SKILL.md").is_file()
                else (),
                validate_references=True,
            )
        )
    project_template = repository / "src" / "literate_ai" / "project_template"
    if (project_template / "SKILL.md").is_file():
        template_skills = project_template / "skills"
        catalogs.append(
            AgentSkillCatalog.discover(
                project_template,
                catalog_roots=(template_skills,) if template_skills.is_dir() else (),
                root_manifests=(project_template / "SKILL.md",),
                validate_references=True,
            )
        )
    return tuple(catalogs)


def _skill_directories(repository: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            item.manifest_path.parent
            for catalog in _catalogs(repository)
            for item in catalog.skills
            if item.manifest_path is not None
        )
    )


def changed_skills(
    repository: Path, *, base: str | None = None, all_skills: bool = False
) -> tuple[Path, ...]:
    catalogs = _catalogs(repository)
    if all_skills:
        return tuple(
            sorted(
                item.manifest_path.parent
                for catalog in catalogs
                for item in catalog.skills
                if item.manifest_path is not None
            )
        )
    paths = _git_paths(repository, base)
    selected: set[Path] = set()
    resolved_repository = repository.resolve()
    for catalog in catalogs:
        logical_paths = []
        catalog_root = next(
            (
                item.manifest_path.parents[len(item.logical_manifest.parts) - 1]
                for item in catalog.skills
                if item.manifest_path is not None
            ),
            repository,
        )
        for path in paths:
            physical = (repository / path).resolve(strict=False)
            try:
                logical_paths.append(physical.relative_to(catalog_root).as_posix())
            except ValueError:
                continue
        selected.update(
            item.manifest_path.parent
            for item in catalog.impacted(logical_paths)
            if item.manifest_path is not None
        )
    return tuple(
        sorted(repository / path.relative_to(resolved_repository) for path in selected)
    )


def _materialize_evaluation_copy(
    source: Path,
    destination: Path,
    *,
    catalog: AgentSkillCatalog | None = None,
    include_resources: bool = False,
) -> Path:
    content = (source / "SKILL.md").read_text(encoding="utf-8")
    if not content.startswith("---\n"):
        raise ValueError(f"agent skill has no YAML frontmatter: {source}")
    close = content.find("\n---\n", 4)
    if close < 0:
        raise ValueError(f"agent skill frontmatter is not closed: {source}")
    frontmatter = content[4:close]
    discovery = {
        line.partition(":")[0]: line.partition(":")[2].strip().strip("\"'")
        for line in frontmatter.splitlines()
        if line and not line.startswith(" ") and ":" in line
    }
    name = discovery.get("name")
    if not name:
        raise ValueError(
            f"canonical skill must expose direct evaluator discovery metadata: {source}"
        )
    if catalog is not None:
        selected = next(
            item
            for item in catalog.skills
            if item.manifest_path is not None and item.manifest_path.parent == source
        )
        projected = catalog.materialize(selected, destination)
    else:
        projected = destination / name
        if include_resources:
            shutil.copytree(source, projected)
        else:
            projected.mkdir(parents=True)
    target = projected / "SKILL.md"
    target.write_bytes((source / "SKILL.md").read_bytes())
    if target.read_bytes() != (source / "SKILL.md").read_bytes():
        raise ValueError(f"evaluation copy changed canonical skill bytes: {source}")
    return projected


def orchestration_views_changed(
    repository: Path, *, base: str | None = None, all_skills: bool = False
) -> bool:
    """Include deleted assets so a broken renderer cannot silently skip admission."""
    present = (repository / ORCHESTRATION_TEMPLATES).is_dir()
    if all_skills:
        return (
            present
            or (
                repository / "src/literate_ai/adapters/orchestration_scaffold.py"
            ).is_file()
        )
    paths = _git_paths(repository, base)
    return any(path.startswith(ORCHESTRATION_TEMPLATES + "/") for path in paths) or (
        present and bool(paths & _ORCHESTRATION_RENDERERS)
    )


def _evaluate_orchestration_views(
    repository: Path, destination: Path, evaluator: str
) -> None:
    # Synthetic authority is only a rendering fixture: no Git, child acceptance,
    # test receipt or release qualification is performed or inferred here.
    from literate_ai.adapters.orchestration_scaffold import (
        prepare_orchestration_scaffold,
    )
    from literate_ai.contracts.repository_orchestration import (
        RepositoryOrchestration,
        RepositoryPin,
    )
    from literate_ai.projects import discover_project, project_skill_catalog

    scaffold = prepare_orchestration_scaffold(
        RepositoryOrchestration(
            "sha256:" + "0" * 64,
            (RepositoryPin("child", "child", "../child.git", "1" * 40),),
        ),
        project_id="admission-fixture",
        version="0.0.0",
        template_root=repository / ORCHESTRATION_TEMPLATES,
    )
    root = (destination / "root").resolve()
    for relative, content in scaffold.files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    catalog = project_skill_catalog(discover_project(root))
    for index, item in enumerate(catalog.skills):
        if item.manifest_path is None:
            raise ValueError("orchestration admission requires a manifest-backed skill")
        candidate = _materialize_evaluation_copy(
            item.manifest_path.parent,
            destination / f"view-{index:04d}",
            catalog=catalog,
        )
        print(
            "SkillEvaluator: validating generated orchestration view "
            + item.manifest_path.relative_to(root).as_posix()
        )
        _evaluate(evaluator, candidate)


def _evaluate(evaluator: str, skill: Path) -> None:
    completed = subprocess.run(
        (
            evaluator,
            "validate",
            str(skill),
            "--checks",
            CHECKS,
            "--no-llm",
            "--no-dedup",
        ),
        check=False,
        cwd=skill.parent,
    )
    if completed.returncode:
        raise RuntimeError(
            f"SkillEvaluator rejected {skill} with exit status {completed.returncode}"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--base", default=os.environ.get("LITAI_SKILL_EVALUATION_BASE"))
    parser.add_argument("--all", action="store_true", dest="all_skills")
    parser.add_argument("--list", action="store_true", dest="list_only")
    parser.add_argument("--evaluator", default=os.environ.get("SKILL_EVALUATOR"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repository = arguments.repository.resolve(strict=True)
    skills = changed_skills(
        repository, base=arguments.base, all_skills=arguments.all_skills
    )
    orchestration = orchestration_views_changed(
        repository, base=arguments.base, all_skills=arguments.all_skills
    )
    if arguments.list_only:
        for skill in skills:
            print(skill.relative_to(repository).as_posix() or ".")
        if orchestration:
            print(ORCHESTRATION_TEMPLATES + " (generated skill views)")
        return 0
    if not skills and not orchestration:
        print("SkillEvaluator: no new or modified skills; gate skipped")
        return 0
    evaluator = arguments.evaluator or shutil.which("skillevaluator")
    if not evaluator:
        raise RuntimeError(
            "new or modified skills require NVIDIA SkillEvaluator; run "
            "`make skill-evaluator-install` or set SKILL_EVALUATOR"
        )
    with tempfile.TemporaryDirectory(prefix="litai-skill-evaluation-") as temporary:
        projection_root = Path(temporary)
        catalogs = _catalogs(repository)
        for index, skill in enumerate(skills):
            if not (skill / "SKILL.md").is_file():
                raise ValueError(
                    f"legacy skill.json must migrate to canonical SKILL.md: {skill}"
                )
            candidate = _materialize_evaluation_copy(
                skill,
                projection_root / f"{index:04d}",
                catalog=next(
                    catalog
                    for catalog in catalogs
                    if any(
                        item.manifest_path is not None
                        and item.manifest_path.parent == skill
                        for item in catalog.skills
                    )
                ),
                include_resources=(repository / "skills") in skill.parents,
            )
            print(f"SkillEvaluator: validating {skill.relative_to(repository) or '.'}")
            _evaluate(evaluator, candidate)
        if orchestration:
            _evaluate_orchestration_views(
                repository, projection_root / "orchestration", evaluator
            )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"skill evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
