#!/usr/bin/env python3
"""Prepare untrusted authored browser inputs; only litai may admit or receipt them.

Run with the published 1.3.1 Python environment. This is an instructor authoring
tool, not a replacement for rebuild, browser acceptance, or cache admission.
"""

from __future__ import annotations

import argparse
import base64
import json
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
from literate_ai.contracts import CycloneDxLifecycle, canonical_json_bytes
from literate_ai.contracts.model_scopes import RETAINED_SOURCE_PROVIDER
from literate_ai.projects import discover_project

CHECKS = r"""
function evaluate(check) {
  if (check === 'shell') return PAGE.startsWith('<!doctype html>') && PAGE.includes('<canvas') && PAGE.includes('data-controls');
  if (check === 'syntax') {
    for (const m of PAGE.matchAll(/<script([^>]*)>([\s\S]*?)<\/script>/gi)) {
      if (!m[1].includes('application/json')) new (require('node:vm').Script)(m[2]);
    }
    return true;
  }
  if (check === 'offline') return !/<script[^>]+src=/i.test(PAGE) && !/<link[^>]+href=["']https?:/i.test(PAGE);
  throw new Error('Unknown check: ' + check);
}
function testAll() {
  return {schema: 'literate-ai/generated-test-results@1', cases: ['shell', 'syntax', 'offline'].map(id => {
    if (!evaluate(id)) throw new Error(id + ' failed');
    return {case_id: 'lab-' + id, outcome: 'passed'};
  })};
}
"""

SERVER = r"""
const args = process.argv.slice(2);
if (args[0] === '--litai-serve') {
  const port = Number(args[1]);
  if (!Number.isInteger(port) || port <= 0) throw new Error('Expected port');
  require('node:http').createServer((request, response) => {
    if (request.url === '/health') {
      response.writeHead(200, {'Content-Type': 'application/json'});
      response.end('{"ready":true}');
    } else if (request.url === '/') {
      response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8'});
      response.end(PAGE);
    } else { response.writeHead(404); response.end('Not found'); }
  }).listen(port, '127.0.0.1');
} else if (args[0] === '--litai-test') console.log(JSON.stringify(testAll()));
else if (args[0] === '--litai-smoke') console.log(JSON.stringify({ready: evaluate('shell'), offline: evaluate('offline')}));
else {
  const parsed = JSON.parse(args[0]);
  if (!Array.isArray(parsed) || parsed.length !== 1) throw new Error('Expected one check');
  console.log(JSON.stringify(evaluate(parsed[0])));
}
"""


def author(lab: Path) -> dict:
    root = lab / "project"
    project = discover_project(root)
    component = root / project.definition.repository_policy.default_component
    prepared = FilesystemLockedGenerationApplicationAdapter().prepare(
        GenerationPreparationRequest(
            component_root=component,
            target_name="host",
            flavor_selectors=project.flavor_selectors_for(component, ()),
            flavor_roots=project.roots("flavor"),
        )
    )
    snapshot = prepared.locked_authority_snapshot
    selector = LockedComponentModelSelectionAdapter()
    execution = FilesystemStandardProjectPlanningAdapter(
        model_selector=selector
    ).plan_for_coding_cli(snapshot, coding_cli=RETAINED_SOURCE_PROVIDER)
    plan = next(
        p
        for p in execution.generation_plans
        if p.component_revision == execution.root_revision
    )
    recipe = (
        LockedComponentNodePreparationAdapter(
            model_selector=selector, coding_cli=RETAINED_SOURCE_PROVIDER
        )
        .project(snapshot, plan)
        .recipe
    )
    reference = next(
        p for p in recipe.non_acceptance_document_paths if p.endswith("component.md")
    )
    payload = json.dumps(
        base64.b64encode((lab / "index.html").read_bytes()).decode("ascii")
    )
    program = (
        "'use strict';\nconst PAGE = Buffer.from("
        + payload
        + ", 'base64').toString('utf8');\n"
        + CHECKS
        + SERVER
    )
    cases = [
        {
            "case_id": "lab-" + name,
            "category": category,
            "arguments": [name],
            "expected_result": True,
            "specification_refs": [reference],
        }
        for name, category in [
            ("shell", "example"),
            ("syntax", "boundary"),
            ("offline", "invariant"),
        ]
    ]
    manifest = {
        "schema": "urn:literate-ai:schema:v1:generated-test-suite",
        "recipe_identity": recipe.identity,
        "generation_mode": "major-rebuild",
        "cases": cases,
    }
    components, edges = _recipe_authority_sbom(recipe)
    sbom, _ = build_cyclonedx_bom(
        lifecycle=CycloneDxLifecycle.SOURCE,
        managed_graph=recipe.managed_sbom_graph,
        additional_components=components,
        additional_edges=edges,
    )
    make = """.PHONY: all test clean
LITAI_LANGUAGE_TOOL ?= node
EXPORT_PATH ?= _build/lab.js
all:
\t"$(LITAI_LANGUAGE_TOOL)" tools/build.js "$(EXPORT_PATH)"
test: all
\t"$(LITAI_LANGUAGE_TOOL)" "$(EXPORT_PATH)" --litai-test
clean:
\t@echo "Disposable outputs belong to the lifecycle-owned build directory."
"""
    build = "const fs=require('node:fs'),path=require('node:path');const out=path.resolve(process.argv[2]);fs.mkdirSync(path.dirname(out),{recursive:true});fs.copyFileSync('main.js',out);\n"
    destination = root / "retained-input"
    files = {
        "main.js": program.encode(),
        "tests/litai_test.js": CHECKS.encode(),
        "tests/manifest.json": canonical_json_bytes(manifest),
        "Makefile": make.encode(),
        "tools/build.js": build.encode(),
        ".literate/sbom.cdx.json": sbom,
    }
    for name, data in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {
        "lab": lab.name,
        "recipe_identity": recipe.identity,
        "files": len(files),
        "admission_claimed": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lab", type=Path)
    print(json.dumps(author(parser.parse_args().lab.resolve())))
