"""pytest configuration for the unit test suite."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_operator_mcp_catalog(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Keep operator MCP fan-out out of the developer's user config/state."""

    isolated = tmp_path_factory.getbasetemp() / "litai-config"
    isolated.mkdir(exist_ok=True)
    os.environ.setdefault("LITAI_CONFIG_DIR", str(isolated))
    os.environ.setdefault("LITAI_STATE_DIR", str(isolated / "state"))
    os.environ.setdefault("LITAI_MCP_TRANSPORT", "off")


@pytest.fixture(scope="session", autouse=True)
def _isolate_git_cache(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Give each xdist worker its own OBJ_DIR so concurrent git fetches don't collide.

    Without this, all workers share the same _build/repository-lineage cache and
    ``git fetch`` calls from different workers collide on the same .lock files.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    obj = tmp_path_factory.getbasetemp() / f"obj-{worker}"
    obj.mkdir(exist_ok=True)
    old = os.environ.get("OBJ_DIR")
    os.environ["OBJ_DIR"] = str(obj)
    yield
    if old is None:
        os.environ.pop("OBJ_DIR", None)
    else:
        os.environ["OBJ_DIR"] = old
