"""Lock and CycloneDX contracts for the ADR 0027 four-boundary portfolio."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path

from literate_ai.adapters.lifecycle.standard_local import LocalResolvedExecutionCommand
from literate_ai.application.standard_project_services import (
    StandardProjectApplicationService,
)
from tests.conformance.support.durable_split_service import (
    EDGE_COORDINATES,
    ROLE_COORDINATES,
    DurableSplitPortfolioError,
    _expire_lease,
    _finite_json_identity,
    _http_json,
    _require_collector_result,
    _require_durable_schema,
    _run_json,
    _service_stderr_excerpt,
    _UpstreamFixture,
    load_durable_split_portfolio,
    resolve_durable_split_portfolio,
)
from tests.conformance.support.sample_runner import (
    _locked_standard_sample_model_identities,
    _prepare_standard_sample_project,
    _StandardSampleAuthoritySnapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "samples" / "durable-split-service"
HARNESS = REPO_ROOT / "samples" / "_harness" / "durable-split-service"


class DurableSplitServicePortfolioTests(unittest.TestCase):
    _SCHEMA = """
        CREATE TABLE snapshot (
            snapshot_id INTEGER PRIMARY KEY,
            window TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            published_at TEXT
        );
        CREATE TABLE snapshot_metric (
            snapshot_id INTEGER NOT NULL REFERENCES snapshot(snapshot_id),
            metric TEXT NOT NULL,
            value NUMERIC NOT NULL,
            PRIMARY KEY (snapshot_id, metric)
        );
        CREATE TABLE current_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 0),
            snapshot_id INTEGER NOT NULL REFERENCES snapshot(snapshot_id)
        );
        CREATE TABLE collection_window (
            window TEXT PRIMARY KEY NOT NULL,
            selected_at TEXT NOT NULL,
            state TEXT NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            retry_count INTEGER NOT NULL,
            retry_limit INTEGER NOT NULL,
            last_error_class TEXT,
            started_at TEXT,
            completed_at TEXT
        );
    """

    def test_durable_schema_contract_accepts_the_exact_cache_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="litai-durable-schema-") as temporary:
            database = Path(temporary) / "cache.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(self._SCHEMA)

            _require_durable_schema(database)

    def test_durable_schema_contract_allows_column_declaration_order_to_vary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="litai-durable-schema-") as temporary:
            database = Path(temporary) / "cache.sqlite3"
            reordered = self._SCHEMA.replace(
                """        CREATE TABLE snapshot (
            snapshot_id INTEGER PRIMARY KEY,
            window TEXT NOT NULL,
            status TEXT NOT NULL,
""",
                """        CREATE TABLE snapshot (
            status TEXT NOT NULL,
            snapshot_id INTEGER PRIMARY KEY,
            window TEXT NOT NULL,
""",
            )
            self.assertNotEqual(reordered, self._SCHEMA)
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(reordered)

            _require_durable_schema(database)

    def test_durable_schema_contract_rejects_a_text_snapshot_key(self) -> None:
        with tempfile.TemporaryDirectory(prefix="litai-durable-schema-") as temporary:
            database = Path(temporary) / "cache.sqlite3"
            incompatible = self._SCHEMA.replace(
                "snapshot_id INTEGER", "snapshot_id TEXT"
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(incompatible)

            with self.assertRaisesRegex(
                DurableSplitPortfolioError,
                "snapshot.*public column contract",
            ):
                _require_durable_schema(database)

    def test_expired_lease_fixture_uses_the_public_running_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="litai-durable-schema-") as temporary:
            database = Path(temporary) / "cache.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.executescript(self._SCHEMA)

            _expire_lease(
                database,
                window="2026-01-04T00:00:00Z",
                now="2026-01-04T00:00:01Z",
            )

            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT state, lease_owner, lease_expires_at, retry_count "
                    "FROM collection_window WHERE window = ?",
                    ("2026-01-04T00:00:00Z",),
                ).fetchone()
            self.assertEqual(
                row,
                ("running", "expired-owner", "2026-01-01T00:00:00Z", 0),
            )

    def test_lease_held_result_owner_must_echo_the_invocation_owner(self) -> None:
        with self.assertRaisesRegex(
            DurableSplitPortfolioError,
            "owner did not echo the invocation owner",
        ):
            _require_collector_result(
                {
                    "status": "lease-held",
                    "window": "2026-01-03T00:00:00Z",
                    "owner": "current-holder",
                    "metric_count": 0,
                    "retry_count": 0,
                },
                status="lease-held",
                window="2026-01-03T00:00:00Z",
                owner="requesting-worker",
            )

    def test_finite_product_numbers_have_stable_canonical_evidence(self) -> None:
        first = _finite_json_identity({"z": 1.25, "a": [-0.0, 2.5]})
        reordered = _finite_json_identity({"a": [-0.0, 2.5], "z": 1.25})

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, _finite_json_identity({"z": 1.5, "a": [0, 2.5]}))

    def test_non_finite_product_number_cannot_enter_evidence(self) -> None:
        with self.assertRaisesRegex(
            DurableSplitPortfolioError, "contains a non-finite number"
        ):
            _finite_json_identity({"metric": float("nan")})

    def test_product_command_accepts_one_noncanonical_json_object(self) -> None:
        command = LocalResolvedExecutionCommand(
            argv=(
                sys.executable,
                "-c",
                'import sys; sys.stdout.write(\'{"z":1,"a":2}\\n\')',
            ),
            cwd=REPO_ROOT,
        )

        self.assertEqual(_run_json(command, {}), {"z": 1, "a": 2})

    def test_product_command_rejects_a_json_value_that_is_not_an_object(self) -> None:
        command = LocalResolvedExecutionCommand(
            argv=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('[1,2]\\n')",
            ),
            cwd=REPO_ROOT,
        )

        with self.assertRaisesRegex(
            DurableSplitPortfolioError, "result is not one JSON object"
        ):
            _run_json(command, {})

    def test_service_failure_excerpt_is_bounded_and_ignores_trace_separator(
        self,
    ) -> None:
        stderr = BytesIO(b"detail\n" + b"x" * 600 + b"\n----------------\n")

        self.assertEqual(_service_stderr_excerpt(stderr), "x" * 500)

    def test_upstream_fixture_serves_finite_json_numbers(self) -> None:
        metrics = {"cpu": 1.25, "memory": 2.5}

        with _UpstreamFixture(metrics) as upstream:
            self.assertEqual(_http_json(upstream.url), metrics)
            self.assertEqual(upstream.request_count, 1)

    def test_manifest_pins_two_roots_four_roles_and_verifier_fixture(self) -> None:
        metadata = json.loads((HARNESS / "sample.json").read_bytes())

        manifest = load_durable_split_portfolio(HARNESS, metadata)

        self.assertEqual(dict(manifest.roles), ROLE_COORDINATES)
        self.assertEqual(len(manifest.roots), 2)
        self.assertEqual(frozenset(manifest.edges), EDGE_COORDINATES)
        self.assertEqual(manifest.upstream_path.name, "upstream.json")
        self.assertEqual(
            {item.role for item in manifest.component_harnesses},
            set(ROLE_COORDINATES),
        )
        self.assertTrue(
            all(
                item.execution_path.is_file() and item.oracle_path.is_file()
                for item in manifest.component_harnesses
            )
        )

    def test_manifest_rejects_stale_fixture_identity(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="litai-durable-portfolio-"
        ) as temporary:
            harness = Path(temporary) / "harness"
            shutil.copytree(HARNESS, harness)
            metadata = json.loads((harness / "sample.json").read_bytes())
            (harness / "acceptance" / "upstream.json").write_text(
                "{}\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(
                DurableSplitPortfolioError, "identity is stale"
            ):
                load_durable_split_portfolio(harness, metadata)

    def test_real_locks_and_cyclonedx_preserve_exact_four_boundary_union(self) -> None:
        portfolio = resolve_durable_split_portfolio(SAMPLE, platform="macos")

        self.assertEqual(len(portfolio.frontend.lock.nodes), 3)
        self.assertEqual(len(portfolio.collector.lock.nodes), 2)
        self.assertEqual(
            {
                node.revision.coordinate.uri
                for lock in portfolio.locks
                for node in lock.nodes
            },
            set(ROLE_COORDINATES.values()),
        )
        cache_revisions = {
            node.revision.identity.uri
            for lock in portfolio.locks
            for node in lock.nodes
            if node.revision.coordinate.uri == ROLE_COORDINATES["cache"]
        }
        self.assertEqual(len(cache_revisions), 1)
        self.assertEqual(len(portfolio.managed_graphs), 2)
        self.assertEqual(len(portfolio.source_bom_identities), 2)

    def test_platform_selection_changes_target_without_changing_topology(self) -> None:
        linux = resolve_durable_split_portfolio(SAMPLE, platform="linux")
        windows = resolve_durable_split_portfolio(SAMPLE, platform="windows")

        self.assertNotEqual(linux.identity, windows.identity)
        for portfolio in (linux, windows):
            self.assertEqual(
                {
                    node.revision.coordinate.uri
                    for lock in portfolio.locks
                    for node in lock.nodes
                },
                set(ROLE_COORDINATES.values()),
            )

    def test_every_locked_node_closes_its_generation_skill_taxonomy(self) -> None:
        portfolio = resolve_durable_split_portfolio(SAMPLE, platform="macos")
        with tempfile.TemporaryDirectory(prefix="litai-durable-skills-") as temporary:
            for index, root in enumerate((portfolio.frontend, portfolio.collector)):
                snapshot = _StandardSampleAuthoritySnapshot(
                    root.authority, root.catalog
                )
                models = _locked_standard_sample_model_identities(
                    snapshot, coding_cli="codex", pipeline_model="gpt-5.4"
                )
                execution = StandardProjectApplicationService.plan(
                    root.lock, model_identities=models
                )
                prepared = _prepare_standard_sample_project(
                    snapshot=snapshot,
                    execution_plan=execution,
                    source_root=Path(temporary) / str(index),
                    coding_cli="codex",
                    pipeline_model="gpt-5.4",
                )
                self.assertEqual(len(prepared.nodes), len(root.lock.nodes))


if __name__ == "__main__":
    unittest.main()
