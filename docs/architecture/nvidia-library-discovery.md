# NVIDIA library discovery and integration

This assessment implements the discovery design slice of NVIDIA-LIBS-001 in the
[active queue](../roadmap/active-work.md). Library admission and execution
qualification remain open. Sources were consulted on 2026-09-26; the links below
are discovery references, not immutable build inputs or compatibility pins.

## Desired behavior

A generated application should be able to start with a need such as matrix
multiplication, sparse solving, neural-network primitives, or ray queries and
discover an appropriate NVIDIA implementation for its selected language and target.
The result must explain how to obtain, integrate, and prove that implementation.
Prioritize NVIDIA OSS, while identifying separately any required binary SDK,
runtime, driver, or proprietary backend. A public repository or Python wrapper
does not establish that the entire dependency closure is open source.

Use capability and language as the entry points. Vendor and product are discovery
facets and provenance. They do not introduce a Flavor axis. Multiple libraries
must be able to coexist in one application without competing for an artificial
exclusive vendor or library slot.

## Existing ownership and overlap

- CUDA Flavor (`flavors/accel-nvidia-cuda/flavor.md`): accelerator requirements and
  authoring inputs; currently scoped to `sample.portable-app` applicability.
- Stack selection
  (`skills/agent/select-nvidia-accelerated-stack/SKILL.md`): current worker
  observations, retained compatibility authority, exact packages, and
  `litai worker resolve-nvidia`.
- CUDA generation
  (`skills/specification-to-source/generate-nvidia-cuda-application/SKILL.md`):
  C++ and Python technique, dependency evidence, and actual device execution.
- [Flavor composition](component-flavors.md): existing language, accelerator,
  toolchain, build, packaging, and deployment dimensions.
- [Skill architecture](skills.md): agent-side discovery/preparation and separately
  pinned generation instructions; SkillEvaluator admission already exists.
- [OVA boundary](../user/ova-and-migration.md): product policy and Omniverse/Isaac
  integration remain downstream. Reusable numerical or geometry libraries need
  not become an OVA dependency merely because NVIDIA authored them.

An external library remains a package/source dependency unless it is deliberately
wrapped as a Component providing observable behavior. Do not turn every package
into a Component. Add a Flavor only when there is a real target variation on an
existing axis; attach technique to the Component or selected Flavor that needs it.

## Initial discovery inventory

These are candidates, not a claim of Literate AI support or an exhaustive `cu*`
inventory. Each family needs separate per-library, per-language admission records.

| Need | Candidates / primary discovery source | Language and integration direction |
| --- | --- | --- |
| Python device control and kernel execution | [CUDA Python](https://github.com/NVIDIA/cuda-python) | Separate `cuda.core` and low-level `cuda.bindings`; select exact distributions and APIs for Python + CUDA. Kernel authoring/compiler choices are separate dependencies. |
| Dense, sparse, FFT, random, tensor, and solver operations | [CUDA library documentation](https://docs.nvidia.com/cuda-libraries/) — cuBLAS, cuSPARSE, cuFFT, cuRAND, cuSOLVER; [CUDA-X](https://developer.nvidia.com/cuda/cuda-x-libraries) — cuTENSOR | Inspect each native API and binding. Toolkit/library binaries, language wrappers, and their licenses are separate records. |
| Python access to math libraries | [nvmath-python](https://developer.nvidia.com/cuda/cuda-x-libraries) | Candidate Python interface for supported math operations; do not infer complete coverage of every native API. |
| Custom matrix kernels | [CUTLASS / CuTe](https://github.com/NVIDIA/cutlass) | C++ templates and Python DSLs; choose by required kernel customization, precision, and hardware. |
| Parallel primitives | [CCCL](https://github.com/NVIDIA/cccl) | Thrust, CUB, and libcu++; inspect the release-specific Python interfaces separately. |
| Neural-network primitives | [cuDNN Frontend](https://github.com/NVIDIA/cudnn-frontend) | C++ and Python interfaces, plus selected OSS kernels; record backend requirements independently. Framework-mediated use may already supply the required integration. |
| Dataframes, ML, graphs, search, optimization, images | [CUDA-X catalog](https://developer.nvidia.com/cuda/cuda-x-libraries) — cuDF, cuML, cuGraph, cuVS, cuOpt, cuCIM | Inspect the product's native/Python APIs and installation matrix separately; prefix membership is not a shared language or platform guarantee. |
| Ray tracing | [OptiX SDK](https://github.com/NVIDIA/optix-sdk) | Native SDK headers and samples call driver-provided functionality. Inspect SDK terms and any Python binding separately; do not label the whole stack OSS. |

The broader inventory must also assess cuBLASLt, cuSPARSELt, cuDSS,
cuFFTMp, cuTENSORMg, cuQuantum and its constituent libraries, cuEquivariance,
cuPQC, and domain-specific `cu*` offerings. This is a discovery backlog, not
verified availability. Include useful non-`cu*` projects such as NCCL, NVSHMEM,
NIXL, Warp, AmgX, DALI, CV-CUDA, nvComp, and TensorRT where relevant. Naming is
not the coverage boundary. Track renamed, deprecated, unavailable, and proprietary
entries explicitly so that omission cannot masquerade as full coverage.

The [NVIDIA skills catalog](https://github.com/NVIDIA/skills) is a supplementary
technique source. Its product repositories and official library documentation
remain necessary for installation, binding coverage, and compatibility evidence.
Assess individual skills against the existing contracts; do not load the suite
into every CUDA recipe.

## Information required for each admitted library

Keep the first inventory in documentation. Introduce a typed catalog only when
selection/validation consumes it, reusing existing dependency and lock contracts.
An admission record needs:

1. Capability and limitations: operation, shapes, precision, error tolerance,
   memory footprint, batching, and CPU/GPU crossover where relevant.
2. Source and ownership: official repository/documentation, reviewed revision,
   retained content identity, upstream skill if used, and review date.
3. Licensing by artifact: library source, wrapper, SDK, runtime/backend, and
   redistribution requirements; unknown remains unknown until verified.
4. Language access: official direct API, official binding, framework-mediated
   access, community binding, or unavailable. A C ABI alone is not proof that
   every Literate AI language has a supported integration. Record upstream
   language support separately from Literate AI's qualified language Flavors.
5. Installation: exact distribution names versus import names, repository/native
   package/wheel/container route, hashes, supported OS/architecture/Python ABI,
   driver floor, toolkit/runtime compatibility, and device requirements.
6. Integration: include/link targets or imports, build-system wiring, memory
   ownership, layout, streams, synchronization, errors, and framework interop.
7. Evidence/status: discovered, assessed, admitted, or qualified for a specific
   target tuple. Bind the receipt and SBOM to that tuple, not to the vendor name.

Discovery can consult current upstream sources. Generation consumes only the
selected, retained, exact authority. Upstream documentation changes must not
silently alter a locked recipe. Network lookup during discovery is not permission
to install packages or execute generated code.

## First realization and subsequent admission

Start with Python + CUDA using CUDA Python. Compare `cuda.core` and
`cuda.bindings` against the desired operation and pin the required packages;
do not assume installing the `cuda-python` metapackage supplies every interface.
Preserve the existing CuPy path as a distinct implementation choice. Python +
CUDA constrains the target but does not uniquely select CUDA Python, CuPy,
nvmath-python, or a framework. Record that remaining choice explicitly before
generation, using existing dependency/authoring contracts where possible.

Next, qualify a matrix operation through a suitable math interface, then one
cuDNN primitive, and one ray-query application. For cuDNN, select the required
operation, precision, shape, hardware, and direct versus framework access;
neural-network intent alone does not imply universal applicability or a direct
cuDNN dependency. Ray-query acceptance should use known geometric intersections,
not a subjective rendered-image check.

Each realization needs a reproducible installation in an isolated environment,
an exact dependency closure in source/resolved SBOMs, actual GPU execution,
independent correctness acceptance, and negative compatibility tests. Measure
performance only with a declared workload and include transfers and startup when
the application contract requires them. A successful import, matching output,
or CPU fallback alone cannot qualify GPU execution.

Admit narrowly scoped generation skills only after their upstream resources and
licenses are retained, contradictions with Literate AI authority are resolved,
and `make skills-check` plus the affected behavioral scenario pass. Coverage of
all families is a maintained inventory objective; execution support grows through
these explicitly evidenced realizations.
