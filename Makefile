.DEFAULT_GOAL := help

.PHONY: help bootstrap install uninstall install-check installed-project-e2e installed-e2e installed-e2e-status dev-install tools-install skill-evaluator-install skills-check repository-layout-check public-export-check python-version python-check source-intelligence-check provider-resolution-check source-admission-check remote-evidence-check inherited-session-check target-matrix-check native-toolchain-check samples samples-packages roundtrip-host samples-platform-regression migration-baseline-refresh test-receipt-current wheel-check format format-check lint openspec-check documentation-check documentation-review documentation-review-record driver-review driver-review-record doc-toolchain-bootstrap release-contributions release-contributions-check release-collateral-regenerate release-collateral-preflight release-collateral-publish plugin-bundle validate release release-check release-check-reset clean really-clean

PREFIX ?=
BUILD_DIR ?= $(CURDIR)/generated
OBJ_DIR ?= $(CURDIR)/_build
TEST_CONFIG ?=
WORKER_CONFIG ?=
RELEASE_VERSION ?=
RELEASE_MILESTONE ?=
RELEASE_ACCOUNT ?=

ifeq ($(origin PYTHON), undefined)
ifeq ($(OS),Windows_NT)
LITERATE_AI_BOOTSTRAP_SHELL := $(SHELL)
SHELL := cmd.exe
PYTHON := $(shell scripts\find-compatible-python.cmd)
SHELL := $(LITERATE_AI_BOOTSTRAP_SHELL)
else
PYTHON := $(shell $(SHELL) scripts/find-compatible-python.sh)
endif
ifeq ($(strip $(PYTHON)),)
$(error no compatible Python >=3.11 found in PATH; set PYTHON explicitly to fail closed)
endif
PYTHON_COMMAND := "$(PYTHON)"
LITAI_SESSION_KEY := $(shell $(PYTHON_COMMAND) scripts/build_session_key.py)
ifeq ($(strip $(LITAI_SESSION_KEY)),)
$(error could not determine a safe build-session identity; check LITAI_SESSION_ID)
endif
PYTHON_ENV ?= $(OBJ_DIR)/python-envs/$(LITAI_SESSION_KEY)
RUNTIME_STAMP := $(PYTHON_ENV)/.literate-ai-runtime
DEV_STAMP := $(PYTHON_ENV)/.literate-ai-dev
ifeq ($(OS),Windows_NT)
MANAGED_PYTHON := $(abspath $(PYTHON_ENV)/Scripts/python.exe)
MANAGED_RUFF := $(abspath $(PYTHON_ENV)/Scripts/ruff.exe)
else
MANAGED_PYTHON := $(abspath $(PYTHON_ENV)/bin/python)
MANAGED_RUFF := $(abspath $(PYTHON_ENV)/bin/ruff)
endif
RUN_PYTHON_COMMAND := "$(MANAGED_PYTHON)"
RUFF ?= "$(MANAGED_RUFF)"
RUNTIME_PREREQUISITE := $(RUNTIME_STAMP)
DEV_PREREQUISITE := $(DEV_STAMP)
else
ifeq ($(strip $(PYTHON)),)
$(error PYTHON is explicitly empty; set it to one compatible Python >=3.11 command)
endif
PYTHON_COMMAND := $(PYTHON)
RUN_PYTHON_COMMAND := $(PYTHON_COMMAND)
RUFF ?= ruff
endif
STEP := $(RUN_PYTHON_COMMAND) scripts/litai_step.py --name
NPM ?= npm
UV ?= uv
SKILL_EVALUATOR ?= skillevaluator
SKILL_EVALUATOR_COMMIT := 0827e0fab5ca93d525cb8ff1ec2af3d7dfcf6d5d
SKILL_EVALUATOR_SOURCE := skillevaluator[all] @ git+https://github.com/NVIDIA/SkillEvaluator.git@$(SKILL_EVALUATOR_COMMIT)
OPENSPEC_TOOL_DIR := tools/openspec
OPENSPEC_RUNTIME_DIR := $(OBJ_DIR)/tools/openspec
OPENSPEC_TOOL_SOURCES := $(wildcard $(OPENSPEC_TOOL_DIR)/*.json $(OPENSPEC_TOOL_DIR)/scripts/* $(OPENSPEC_TOOL_DIR)/test/*)
OPENSPEC_TOOL_STAMP := $(OPENSPEC_RUNTIME_DIR)/.installed
PUPPETEER_CACHE_DIR := $(OBJ_DIR)/tools/puppeteer
PYTHON_BYTECODE_DIR := $(OBJ_DIR)/pycache
OPENSPEC ?= $(OPENSPEC_RUNTIME_DIR)/node_modules/.bin/openspec
SAMPLE_PYTHON := tests/conformance/support/sample_runner.py scripts/run_samples.py tests/conformance/support/self_hosting_proof.py scripts/run_source_to_specification_fixtures.py scripts/capture_migration_baseline.py scripts/refresh_migration_characterization.py scripts/fanout_samples.py scripts/remote_sample_worker.py scripts/remote_source_guard.py scripts/install_litai.py scripts/uninstall_litai.py scripts/installed_project_smoke.py scripts/installed_roundtrip.py scripts/run_checkpointed_gates.py scripts/run_checkpointed_unittests.py scripts/check_public_export.py
PACKAGE_SAMPLE_PATTERNS ?= --sample hello-component --sample generated-library --sample dependency-planner
MATRIX_FLAVORS ?= --flavor=os.*
RELEASE_GATES := repository-layout-check python-check lint format-check openspec-check documentation-check driver-review skills-check installed-e2e wheel-check install-check installed-project-e2e samples test-receipt-current
RELEASE_CHECKPOINT := $(OBJ_DIR)/release-checkpoint.json
PYTHON_TEST_CHECKPOINT := $(OBJ_DIR)/python-test-checkpoint.json
PYTHON_TEST_PATTERN ?= test*.py
PYTHON_TEST_VERBOSITY ?= 1
RELEASE_PLAN ?=
RELEASE_PREPARED ?= $(OBJ_DIR)/release/prepared.json

export PATH := $(abspath $(OPENSPEC_RUNTIME_DIR)/node_modules/.bin):$(PATH)
export PUPPETEER_CACHE_DIR := $(abspath $(PUPPETEER_CACHE_DIR))
export PYTHON_ENV
export PYTHONPYCACHEPREFIX := $(abspath $(PYTHON_BYTECODE_DIR))

help: ## Show repository validation commands.
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z0-9_.-]+:.*## / && !seen[$$1]++ {printf "  %-18s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

clean: python-version ## Remove generated objects and executables from OBJ_DIR.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" PYTHONPATH=src $(PYTHON_COMMAND) scripts/clean_cache_directories.py --project . --preserve-python-environments

really-clean: python-version ## Remove OBJ_DIR and generated sources from BUILD_DIR.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" PYTHONPATH=src $(PYTHON_COMMAND) scripts/clean_cache_directories.py --project . --really-clean

$(RUNTIME_STAMP): pyproject.toml
	$(PYTHON_COMMAND) -m venv "$(PYTHON_ENV)"
	$(RUN_PYTHON_COMMAND) -m pip install -e .
	@touch "$@"

$(DEV_STAMP): $(RUNTIME_STAMP) pyproject.toml
	$(RUN_PYTHON_COMMAND) -m pip install -e ".[dev]"
	@touch "$@"

bootstrap: install dev-install tools-install skill-evaluator-install validate ## First-time setup: install all runtime and contributor tools, then validate.

install: python-version ## Verify native dependencies, then install litai to the host-native user prefix.
	$(PYTHON_COMMAND) scripts/install_litai.py --source "$(CURDIR)" $(if $(strip $(PREFIX)),--prefix "$(PREFIX)",)

uninstall: python-version ## Remove only the Literate AI private runtime and launcher.
	$(PYTHON_COMMAND) scripts/uninstall_litai.py $(if $(strip $(PREFIX)),--prefix "$(PREFIX)",)

plugin-bundle: python-version $(RUNTIME_PREREQUISITE) ## Stage the Claude/Codex plugin bundle under OBJ_DIR.
	$(RUN_PYTHON_COMMAND) scripts/build_plugin_bundle.py --repository "$(CURDIR)" --output "$(OBJ_DIR)/plugins/literate-ai"

ifneq ($(strip $(RUNTIME_PREREQUISITE)),)
dev-install: $(DEV_PREREQUISITE) ## Install pinned Python contributor tools.
else
dev-install: ## Install pinned Python contributor tools.
	$(RUN_PYTHON_COMMAND) -m pip install -e ".[dev]"
endif

install-check: python-version $(RUNTIME_PREREQUISITE) ## Install to a temporary PREFIX and verify init/validate/lock/plan.
	$(STEP) install-check -- $(RUN_PYTHON_COMMAND) scripts/installed_project_smoke.py --repository "$(CURDIR)" --isolated-tool-bootstrap

installed-project-e2e: python-version $(RUNTIME_PREREQUISITE) ## Prove an installed CLI can init, generate, build, test, and run hello.
	$(STEP) installed-project-e2e -- $(RUN_PYTHON_COMMAND) scripts/installed_project_smoke.py --repository "$(CURDIR)" --live

installed-e2e: python-version $(RUNTIME_PREREQUISITE) ## Offline installed-CLI/command-worker E2E; skipped when unchanged.
	$(STEP) installed-e2e -- $(RUN_PYTHON_COMMAND) scripts/installed_e2e_gate.py --repository "$(CURDIR)" --build-root "$(OBJ_DIR)"

installed-e2e-status: python-version $(RUNTIME_PREREQUISITE) ## Report whether the installed-CLI E2E sentinel is current.
	$(RUN_PYTHON_COMMAND) scripts/installed_e2e_gate.py --repository "$(CURDIR)" --build-root "$(OBJ_DIR)" --status

tools-install: $(OPENSPEC_TOOL_STAMP) ## Install pinned contributor tools under disposable OBJ_DIR.

$(OPENSPEC_TOOL_STAMP): $(OPENSPEC_TOOL_SOURCES) scripts/stage_node_tool.py
	$(PYTHON_COMMAND) scripts/stage_node_tool.py --source "$(OPENSPEC_TOOL_DIR)" --destination "$(OPENSPEC_RUNTIME_DIR)" --object-root "$(OBJ_DIR)"
	$(NPM) --prefix "$(OPENSPEC_RUNTIME_DIR)" ci
	@$(PYTHON_COMMAND) -c 'from pathlib import Path; Path(r"$(OPENSPEC_TOOL_STAMP)").touch()'

skill-evaluator-install: ## Install the pinned skill admission tool when authoring skills.
	$(UV) tool install --python 3.13 --force "$(SKILL_EVALUATOR_SOURCE)"

skills-check: python-version $(RUNTIME_PREREQUISITE) ## Evaluate only new or modified skills; otherwise skip.
	$(STEP) skills-check -- $(RUN_PYTHON_COMMAND) scripts/evaluate_changed_skills.py --evaluator "$(SKILL_EVALUATOR)"

repository-layout-check: python-version $(RUNTIME_PREREQUISITE) ## Reject generated dependencies/build state in Git and root Python modules.
	$(STEP) repository-layout-check -- $(PYTHON_COMMAND) scripts/check_repository_layout.py

public-export-check: python-version ## Reject tracked NVIDIA-internal references before public export.
	$(PYTHON_COMMAND) scripts/check_public_export.py .

python-version: ## Require a supported system Python from PATH.
	$(PYTHON_COMMAND) -c 'import sys; assert sys.version_info >= (3, 11), sys.version'

python-check: python-version $(DEV_PREREQUISITE) ## Validate the Python framework package.
	$(RUN_PYTHON_COMMAND) -X pycache_prefix="$(OBJ_DIR)/pycache" -m compileall -q src tests
	$(RUN_PYTHON_COMMAND) -m py_compile $(SAMPLE_PYTHON)
	$(RUN_PYTHON_COMMAND) scripts/check_host_path_policy.py .
	PYTHONPATH=src $(STEP) python-check -- $(RUN_PYTHON_COMMAND) scripts/run_checkpointed_unittests.py --state "$(PYTHON_TEST_CHECKPOINT)" --start-directory tests --pattern "$(PYTHON_TEST_PATTERN)" --verbosity "$(PYTHON_TEST_VERBOSITY)"

.PHONY: finite-acceptance-check
finite-acceptance-check: python-version $(RUNTIME_PREREQUISITE) ## Verify fractional application/library acceptance and identity compatibility.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.smoke.test_product_json_acceptance \
		tests.smoke.test_javascript_library_acceptance \
		tests.smoke.test_standard_local_command_adapter

.PHONY: repository-lifecycle-check
repository-lifecycle-check: python-version $(RUNTIME_PREREQUISITE) ## Qualify explicit retained-child lifecycle delegation.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest tests.e2e.test_repository_lifecycle

.PHONY: component-worker-check
component-worker-check: python-version $(RUNTIME_PREREQUISITE) ## Verify exact Component worker routing and predecessor byte custody.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.critical.test_component_workers \
		tests.smoke.test_standard_project_lifecycle \
		tests.critical.test_cas_read_bounds \
		tests.smoke.test_schema_catalog \
		tests.critical.test_wire_contract_versions

source-intelligence-check: python-version $(RUNTIME_PREREQUISITE) ## Verify opt-in project source-intelligence boundaries.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.smoke.test_project_source_index

provider-resolution-check: python-version $(RUNTIME_PREREQUISITE) ## Verify canonical capability-based provider resolution.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.smoke.test_provider_resolution \
		tests.smoke.test_schema_catalog \
		tests.critical.test_wire_contract_versions

source-admission-check: python-version $(RUNTIME_PREREQUISITE) ## Verify source admission, worker fanout, and receipt boundaries.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.critical.test_cli_generation \
		tests.e2e.test_cli_source_admission \
		tests.critical.test_source_verification_workspace \
		tests.smoke.test_standard_project_lifecycle \
		tests.critical.test_standard_source_admission \
		tests.smoke.test_standard_source_cache_roundtrip \
		tests.smoke.test_source_cache_publication

remote-evidence-check: python-version $(RUNTIME_PREREQUISITE) ## Verify remote evidence transfer, import, diagnostics, and cleanup.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.critical.test_execution_dispatch_contracts \
		tests.critical.test_remote_execution \
		tests.smoke.test_ssh_execution \
		tests.critical.test_ssh_transport

inherited-session-check: python-version $(RUNTIME_PREREQUISITE) ## Verify authenticated inherited-session handoff and source admission.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.critical.test_inherited_session_provider \
		tests.smoke.test_cursor_inherited_session \
		tests.critical.test_cached_coding_cli_source_generation_runner \
		tests.critical.test_coding_cli_generation.CodingCliSelectionTests \
		tests.critical.test_project_lifecycle_driver_adapter \
		tests.smoke.test_schema_catalog \
		tests.critical.test_standard_source_admission

target-matrix-check: python-version $(RUNTIME_PREREQUISITE) ## Verify concurrent target-matrix contracts, CLI, and scoped evidence.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.smoke.test_target_matrices \
		tests.smoke.test_target_matrix_cli \
		tests.critical.test_component_lock_store \
		tests.smoke.test_component_lock_planning \
		tests.critical.test_locked_generation_authority \
		tests.smoke.test_schema_catalog

native-toolchain-check: python-version $(RUNTIME_PREREQUISITE) ## Run explicitly authorized real-Bazel conformance (may resolve dependencies).
	LITERATE_AI_NATIVE_TOOLCHAIN_TESTS=1 PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m unittest \
		tests.conformance.test_sample_ladder.NeutralSampleLadderTests.test_service_stack_uses_standard_three_node_lifecycle_with_real_bazel \
		tests.conformance.test_sample_ladder.NeutralSampleLadderTests.test_service_stack_bazel_target_produces_bytecode_and_hits_cache

samples: python-version $(RUNTIME_PREREQUISITE) ## Run the live sample matrix directly; use litai rebuild for the pinned full-SDLC wrapper.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" $(STEP) samples -- $(RUN_PYTHON_COMMAND) scripts/run_samples.py --allow-host-execution $(SAMPLE_FLAVORS)

samples-packages: python-version $(RUNTIME_PREREQUISITE) ## Build selected samples end to end, then construct and verify their pip/Conan packages.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" $(RUN_PYTHON_COMMAND) scripts/run_samples.py --allow-host-execution --native-package $(PACKAGE_SAMPLE_PATTERNS) $(SAMPLE_FLAVORS)

roundtrip-host: python-version $(RUNTIME_PREREQUISITE) ## Forward/inverse all four languages under an exact installed wheel; set ROUNDTRIP_LANGUAGES to select a subset.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" $(PYTHON_COMMAND) scripts/installed_roundtrip.py --repository "$(CURDIR)" $(if $(ROUNDTRIP_LANGUAGES),--languages "$(ROUNDTRIP_LANGUAGES)",)

samples-platform-regression: python-version $(RUNTIME_PREREQUISITE) ## Fan the canonical sample concurrently to configured macOS, Linux, and Windows targets; override SAMPLE with a glob.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" $(RUN_PYTHON_COMMAND) scripts/fanout_samples.py $(if $(TEST_CONFIG),--config "$(TEST_CONFIG)",) $(if $(WORKER_CONFIG),--worker-config "$(WORKER_CONFIG)",) --sample "$(or $(SAMPLE),regenerative-roundtrip)" $(MATRIX_FLAVORS) $(SAMPLE_FLAVORS)

migration-baseline-refresh: python-version $(RUNTIME_PREREQUISITE) ## Refresh the current intentional wire-characterization fixture.
	PYTHONPATH=src:. $(RUN_PYTHON_COMMAND) scripts/refresh_migration_characterization.py

test-receipt-current: python-version $(RUNTIME_PREREQUISITE) ## Require the current local Git test assertion; this is not remote attestation.
	PYTHONPATH=src $(STEP) test-receipt-current -- $(RUN_PYTHON_COMMAND) -m literate_ai.cli project test-receipt require-current --project .

wheel-check: python-version $(RUNTIME_PREREQUISITE) ## Build and smoke-test the installed wheel outside this checkout.
	$(STEP) wheel-check -- $(RUN_PYTHON_COMMAND) scripts/wheel_smoke.py

format: $(DEV_PREREQUISITE) ## Format Python source and tests.
	$(RUFF) format src tests scripts $(SAMPLE_PYTHON)

format-check: $(DEV_PREREQUISITE) ## Check Python formatting without changing files.
	$(STEP) format-check -- $(RUFF) format --check src tests scripts $(SAMPLE_PYTHON)

lint: $(DEV_PREREQUISITE) ## Run strict Python lint checks.
	$(STEP) lint -- $(RUFF) check src tests scripts $(SAMPLE_PYTHON)

openspec-check: python-version $(RUNTIME_PREREQUISITE) $(OPENSPEC_TOOL_STAMP) ## Strictly validate all OpenSpec changes and specifications.
	$(STEP) openspec-check -- $(OPENSPEC) validate --all --strict --no-interactive

documentation-check: python-version $(RUNTIME_PREREQUISITE) $(OPENSPEC_TOOL_STAMP) ## Extract and render every Mermaid diagram in Markdown.
	$(STEP) documentation-check -- $(NPM) --prefix "$(OPENSPEC_RUNTIME_DIR)" run documentation:check -- "$(CURDIR)"

documentation-review: python-version $(RUNTIME_PREREQUISITE) ## Calculate the reviewed documentation authority marker.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m literate_ai.cli project documentation-review .

documentation-review-record: python-version $(RUNTIME_PREREQUISITE) ## Atomically record the validated documentation authority marker.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m literate_ai.cli project documentation-review . --record

doc-toolchain-bootstrap: python-version $(RUNTIME_PREREQUISITE) ## Install the pinned document-pair OOXML toolchain into ignored OBJ_DIR.
	$(RUN_PYTHON_COMMAND) scripts/bootstrap_doc_toolchain.py --install --allow-install

release-contributions: python-version $(RUNTIME_PREREQUISITE) ## Inventory current issues, reviews, branches, and worktrees without writes.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m literate_ai.cli release contributions sweep --project . $(if $(strip $(RELEASE_VERSION)),--version "$(RELEASE_VERSION)",) $(if $(strip $(RELEASE_MILESTONE)),--current-milestone "$(RELEASE_MILESTONE)",)

release-contributions-check: python-version $(RUNTIME_PREREQUISITE) ## Require every contribution disposition and all included work closed.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m literate_ai.cli release contributions sweep --project . $(if $(strip $(RELEASE_VERSION)),--version "$(RELEASE_VERSION)",) $(if $(strip $(RELEASE_MILESTONE)),--current-milestone "$(RELEASE_MILESTONE)",) --require-ready

release-collateral-regenerate: doc-toolchain-bootstrap ## Regenerate and independently verify the terminal document pair.
	OBJ_DIR="$(OBJ_DIR)" docs/presentations/literate-ai-manager-overview/regenerate_python.sh

release-collateral-preflight: python-version $(RUNTIME_PREREQUISITE) ## Check the explicit gcloud account and both stable resources without writes.
	OBJ_DIR="$(OBJ_DIR)" $(RUN_PYTHON_COMMAND) docs/presentations/literate-ai-manager-overview/publish_google_workspace.py --expected-account "$(RELEASE_ACCOUNT)" --preflight-only

release-collateral-publish: python-version $(RUNTIME_PREREQUISITE) ## Update both stable resources and retain export-back evidence.
	OBJ_DIR="$(OBJ_DIR)" $(RUN_PYTHON_COMMAND) docs/presentations/literate-ai-manager-overview/publish_google_workspace.py --expected-account "$(RELEASE_ACCOUNT)" $(if $(strip $(RELEASE_VERSION)),--release-version "$(RELEASE_VERSION)",) --authorize-external-write

driver-review: python-version $(RUNTIME_PREREQUISITE) ## Require a current lifecycle-driver TCB pin; names the drift when stale.
	$(STEP) driver-review -- $(RUN_PYTHON_COMMAND) scripts/review_lifecycle_driver.py

driver-review-record: python-version $(RUNTIME_PREREQUISITE) ## Re-pin the lifecycle-driver TCB; asserts you reviewed the reported drift.
	$(RUN_PYTHON_COMMAND) scripts/review_lifecycle_driver.py --record

runner-review: python-version $(RUNTIME_PREREQUISITE) ## Require a current sample test-runner source-closure pin.
	$(STEP) runner-review -- $(RUN_PYTHON_COMMAND) scripts/review_test_runner.py

runner-review-record: python-version $(RUNTIME_PREREQUISITE) ## Re-pin the reviewed sample test-runner source closure.
	$(RUN_PYTHON_COMMAND) scripts/review_test_runner.py --record

validate: repository-layout-check python-check lint format-check openspec-check documentation-check driver-review runner-review skills-check installed-e2e ## Run every current repository gate.

release: python-version $(RUNTIME_PREREQUISITE) ## Check the exact plan in RELEASE_PLAN; publication remains an explicit litai release publish command.
	PYTHONPATH=src $(RUN_PYTHON_COMMAND) -m literate_ai.cli release check "$(RELEASE_PLAN)" --project . --output "$(RELEASE_PREPARED)"

release-check: python-version $(RUNTIME_PREREQUISITE) ## Run gates fail-fast; resume passed gates after a repair and reset after success.
	BUILD_DIR="$(BUILD_DIR)" OBJ_DIR="$(OBJ_DIR)" $(RUN_PYTHON_COMMAND) scripts/run_checkpointed_gates.py --state "$(RELEASE_CHECKPOINT)" --make "$(MAKE)" $(RELEASE_GATES)

release-check-reset: python-version $(RUNTIME_PREREQUISITE) ## Discard the diagnostic release checkpoint so the next run starts at gate one.
	$(RUN_PYTHON_COMMAND) scripts/run_checkpointed_gates.py --state "$(RELEASE_CHECKPOINT)" --reset
	$(RUN_PYTHON_COMMAND) scripts/run_checkpointed_gates.py --state "$(PYTHON_TEST_CHECKPOINT)" --reset

.PHONY: release-artifacts
release-artifacts: python-version $(RUNTIME_PREREQUISITE) ## Qualify and retain exact wheel/plugin bytes for publication.
	PYTHONPATH=src:. $(RUN_PYTHON_COMMAND) scripts/qualify_release_files.py
