"""Exercise verifier launch isolation with real Python calls and fixture custody."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from literate_ai.adapters.component_acceptance import (
    DeclaredLibraryAcceptanceCase,
    LibraryAcceptance,
)
from literate_ai.adapters.lifecycle import (
    LocalSourceTreeRegistry,
    LocalStandardLifecyclePorts,
    local_tree_identity,
)
from literate_ai.adapters.lifecycle.standard_local import LocalStandardLifecycleError
from literate_ai.adapters.standard_project import (
    project_locked_standard_toolchain_closure,
)
from literate_ai.contracts import ContentIdentity, canonical_identity
from tests.support.fixtures_test_standard_command_projection import (
    _locked_snapshot,
    _observation,
    _tool,
)

_HARNESS = b"""import importlib,json,pathlib,sys
assert sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
export=pathlib.Path(sys.argv[1]).resolve()
surface=json.loads(sys.argv[2]); cases=json.loads(sys.argv[3])
sys.path.insert(0,str(export/'source'))
capability=surface['capabilities'][0]
module=importlib.import_module(capability['module'])
assert pathlib.Path(module.__file__).resolve().is_relative_to(export)
function=getattr(module,capability['symbols'][0])
print(json.dumps({'schema':'literate-ai/library-acceptance-results@1','cases':[
 {'case_id':c['case_id'],'capability':c['capability'],'result':function(*c['arguments'])}
 for c in cases]}))
"""


class PythonLibraryVerifierIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        _, self.snapshot, execution = _locked_snapshot(
            self.root, language="python", no_entrypoint=True
        )
        closure = project_locked_standard_toolchain_closure(
            self.snapshot,
            execution,
            host_platform="macos",
            toolchain_discoverer=lambda name, *_: _tool(name),
            dependency_observer=_observation,
        )
        contract = closure.contracts[0]
        surface = contract.library_import_surface
        self.capability = surface.capabilities[0]
        self.export = self.root / "sealed" / contract.artifact_export.export_id
        self.module = (
            self.export
            / "source"
            / Path(*self.capability.module.split(".")).with_suffix(".py")
        )
        self.module.parent.mkdir(parents=True)
        node = next(
            n
            for n in self.snapshot.authority.lock.nodes
            if n.revision.identity == self.snapshot.authority.lock.root_revision
        )
        self.oracle = LibraryAcceptance(
            node.revision.coordinate.name,
            node.revision.specification_set_identity,
            tuple(
                sorted(
                    (ref.identity for ref in node.revision.public_interfaces),
                    key=lambda identity: identity.uri,
                )
            ),
            surface.identity,
            "python",
            ContentIdentity.parse_uri("sha256:" + hashlib.sha256(_HARNESS).hexdigest()),
            _HARNESS,
            (
                DeclaredLibraryAcceptanceCase(
                    "zero-argument-value", self.capability.capability, [], 7
                ),
            ),
        )
        self.ports = LocalStandardLifecyclePorts(
            source_trees=LocalSourceTreeRegistry(),
            object_root=self.root / "objects",
            contracts=closure.contracts,
            tool_bindings=closure.tool_bindings,
        )
        export_identity = canonical_identity({"fixture": "sealed-export"})
        self.ports._planned_exports[node.revision.identity.uri] = SimpleNamespace(
            identity=export_identity
        )
        self.export_identity = export_identity

    def accept(self, result):
        self.module.write_text(
            f"def {self.capability.symbols[0]}():\n    return {result}\n"
        )
        custody = SimpleNamespace(
            root=self.export.parent,
            artifact_paths={self.export_identity.uri: self.export},
            tree_identity=local_tree_identity(self.export.parent),
        )
        identity = canonical_identity({"fixture": "unit-launch-custody"})
        return self.ports._accept_library(
            self.snapshot.authority.lock,
            custody,
            SimpleNamespace(identity=identity),
            SimpleNamespace(identity=identity),
            self.oracle,
            identity,
            identity,
        )

    def test_real_verifier_uses_isolated_no_site_startup_and_sealed_import(self):
        ambient = self.root / "ambient"
        ambient.mkdir()
        (ambient / "sitecustomize.py").write_text(
            "raise RuntimeError('ambient site ran')\n"
        )
        with patch.dict("os.environ", {"PYTHONPATH": str(ambient)}):
            self.assertTrue(self.accept(7).uri.startswith("sha256:"))
        self.assertFalse(list(self.export.rglob("__pycache__")))

    def test_isolated_verifier_still_rejects_wrong_library_behavior(self):
        with self.assertRaisesRegex(LocalStandardLifecycleError, "result differs"):
            self.accept(8)
