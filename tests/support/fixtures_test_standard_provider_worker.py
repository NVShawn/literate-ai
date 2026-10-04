"""Shared fixtures extracted from ``tests.unit.test_standard_provider_worker``."""































_CHILD = """
import json, os, sys
from contextlib import contextmanager
from pathlib import Path
from literate_ai.build_worker import main
from literate_ai.adapters.dependencies import HostDependencyObservation
from literate_ai.adapters.lifecycle import LocalComponentToolBinding
from literate_ai.adapters.standard_project import (
    project_standard_toolchain_closure, assemble_standard_lifecycle_ports,
)
from literate_ai.contracts import (
    ComponentCommandContract, ComponentCommandPhase, canonical_identity,
)

@contextmanager
def runtime(admitted, registry, recorder):
    binding = LocalComponentToolBinding(sys.executable)
    custody = registry.evidence(admitted.candidate.tree_identity)
    root_ref = custody.managed_graph.root_ref
    closure = project_standard_toolchain_closure(
        admitted.execution_plan,
        contracts=tuple(ComponentCommandContract.from_dict(item)
            for item in json.loads(os.environ['CHAIN_CONTRACTS'])),
        tool_bindings=(binding,),
        provider_environment={key:tuple(value) for key,value in
            json.loads(os.environ['CHAIN_PROVIDERS']).items()},
        dependency_observation=HostDependencyObservation(
            ({'type':'application','name':'fixture-python','version':sys.version.split()[0],
              'bom-ref':binding.toolchain_identity.uri,
              'properties':[
                  {'name':'literate-ai:dependency-kind','value':'toolchain'},
                  {'name':'literate-ai:dependency-scope','value':'build'},
              ]},),
            ((root_ref,binding.toolchain_identity.uri),),
        ),
        observer_identity=canonical_identity({'observer':'chain-worker'}),
    )
    composition = assemble_standard_lifecycle_ports(
        source_trees=registry, object_root=Path('objects').resolve(),
        toolchain_closure=closure, command_phases=(ComponentCommandPhase.BUILD,),
    )
    composition.ports.retain_evidence_with(recorder)
    yield composition.ports

raise SystemExit(main(runtime_factory=runtime))
"""

