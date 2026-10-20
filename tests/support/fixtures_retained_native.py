"""Author exact untrusted retained input; only the public lifecycle issues receipts."""

from pathlib import Path

from literate_ai.adapters.dependencies import build_cyclonedx_bom
from literate_ai.adapters.generation_preparation import (
    FilesystemLockedGenerationApplicationAdapter,
    LockedComponentModelSelectionAdapter,
    LockedComponentNodePreparationAdapter,
)
from literate_ai.adapters.models.coding_cli import _recipe_authority_sbom
from literate_ai.adapters.standard_project import (
    FilesystemStandardProjectPlanningAdapter,
)
from literate_ai.application import GenerationPreparationRequest
from literate_ai.contracts import (
    CycloneDxLifecycle,
    canonical_json_bytes,
)
from literate_ai.contracts.model_scopes import RETAINED_SOURCE_PROVIDER
from literate_ai.projects import discover_project


def author_retained_native_fixture(project_root: Path, output: Path) -> None:
    project = discover_project(project_root)
    component = project_root / "samples/hello-component"
    prepared = FilesystemLockedGenerationApplicationAdapter().prepare(
        GenerationPreparationRequest(
            component_root=component,
            target_name="host",
            flavor_selectors=project.flavor_selectors_for(component, ()),
            flavor_roots=project.roots("flavor"),
        )
    )
    snapshot = prepared.locked_authority_snapshot
    model_selector = LockedComponentModelSelectionAdapter()
    execution = FilesystemStandardProjectPlanningAdapter(
        model_selector=model_selector,
    ).plan_for_coding_cli(snapshot, coding_cli=RETAINED_SOURCE_PROVIDER)
    root_plan = next(
        p
        for p in execution.generation_plans
        if p.component_revision == execution.root_revision
    )
    recipe = (
        LockedComponentNodePreparationAdapter(
            model_selector=model_selector,
            coding_cli=RETAINED_SOURCE_PROVIDER,
        )
        .project(snapshot, root_plan)
        .recipe
    )
    reference = next(
        p for p in recipe.non_acceptance_document_paths if p.endswith("component.md")
    )
    # Root initialization and inherited starter projects expose two public hello
    # contracts. Read product authority, never the verifier's invocation values.
    cards = "| `messages` |" in (component / "component.md").read_text()
    cases = (
        (
            "retained-count",
            "example",
            {
                "name": "Retained Worker",
                "messages": ["ports build", "native test passed"],
            },
            {
                "greeting": "Hello, Retained Worker!",
                "recipient_id": "retained-worker",
                "message_count": 2,
                "word_count": 5,
            },
        ),
        (
            "retained-empty",
            "boundary",
            {"name": "Empty", "messages": []},
            {
                "greeting": "Hello, Empty!",
                "recipient_id": "empty",
                "message_count": 0,
                "word_count": 0,
            },
        ),
        (
            "retained-unicode",
            "invariant",
            {"name": "  STRAßE__Runner  ", "messages": ["one\ttwo", "", "three\nfour"]},
            {
                "greeting": "Hello,   STRAßE__Runner  !",
                "recipient_id": "strasse-runner",
                "message_count": 3,
                "word_count": 4,
            },
        ),
    )
    if not cards:
        cases = tuple(
            (
                i,
                category,
                {"name": args["name"]},
                {"greeting": expected["greeting"], "name": args["name"]},
            )
            for i, category, args, expected in cases
        )
    program = """import json,sys

CARDS = PUBLIC_CONTRACT_CARDS

def main(request):
    keys={'name','messages'} if CARDS else {'name'}
    if not isinstance(request,dict) or set(request)!=keys:
        raise ValueError('unexpected input fields')
    name=request['name']
    if not isinstance(name,str): raise ValueError('expected name string')
    if not CARDS: return {'greeting':'Hello, '+name+'!','name':name}
    name,messages=request['name'],request['messages']
    if (not isinstance(name,str) or not isinstance(messages,list)
        or not all(isinstance(x,str) for x in messages)):
        raise ValueError('invalid input types')
    identifier=[]
    for character in name.casefold():
        if character.isalnum(): identifier.append(character)
        elif identifier and identifier[-1]!='-': identifier.append('-')
    return {'greeting':'Hello, '+name+'!','recipient_id':''.join(identifier).strip('-'),
            'message_count':len(messages),
            'word_count':sum(len(x.split()) for x in messages)}

CASES = NATIVE_CASES

def behavior_tests():
    outcomes=[]
    for case_id,arguments,expected in CASES:
        if main(arguments)!=expected: raise AssertionError(case_id+' behavior failed')
        outcomes.append({'case_id':case_id,'outcome':'passed'})
    return {'schema':'literate-ai/generated-test-results@1','cases':outcomes}

if __name__=='__main__':
    if len(sys.argv)!=2: raise ValueError('expected one JSON argument vector')
    mode=sys.argv[1]
    if mode=='--litai-test': result=behavior_tests()
    elif mode=='--litai-smoke': result=main(CASES[0][1])
    else:
        arguments=json.loads(mode)
        if not isinstance(arguments,list) or len(arguments)!=1:
            raise ValueError('expected one argument')
        result=main(*arguments)
    print(json.dumps(result,sort_keys=True))
""".replace("NATIVE_CASES", repr(tuple((i, a, e) for i, c, a, e in cases))).replace(
        "PUBLIC_CONTRACT_CARDS", repr(cards)
    )
    components, edges = _recipe_authority_sbom(recipe)
    sbom, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=recipe.managed_sbom_graph,
        additional_components=components,
        additional_edges=edges,
    )
    manifest = {
        "schema": "urn:literate-ai:schema:v1:generated-test-suite",
        "recipe_identity": recipe.identity,
        "generation_mode": "major-rebuild",
        "cases": [
            {
                "case_id": i,
                "category": c,
                "arguments": [a],
                "expected_result": e,
                "specification_refs": [reference],
            }
            for i, c, a, e in cases
        ],
    }
    files = {
        "main.py": program.encode(),
        "Makefile": (
            b".PHONY: all test\nall:\n"
            b'\t"$(LITAI_LANGUAGE_TOOL)" tools/compile.py "$(EXPORT_PATH)"\n'
            b"test: all\n"
            b'\t"$(LITAI_LANGUAGE_TOOL)" "$(EXPORT_PATH)" --litai-test\n'
        ),
        "tools/compile.py": (
            b"import pathlib,py_compile,sys\n"
            b"output=pathlib.Path(sys.argv[1])\n"
            b"output.parent.mkdir(parents=True,exist_ok=True)\n"
            b'py_compile.compile("main.py",cfile=str(output),dfile="main.py",'
            b"doraise=True,\n"
            b"invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)\n"
        ),
        "tests/manifest.json": canonical_json_bytes(manifest),
        ".literate/sbom.cdx.json": sbom,
    }
    output.mkdir()
    for name, content in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
