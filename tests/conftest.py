"""pytest configuration for the test suite."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_operator_mcp_catalog(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Keep operator MCP fan-out out of the developer's user config/state."""

    isolated = tmp_path_factory.getbasetemp() / "litai-config"
    isolated.mkdir(exist_ok=True)
    values = {
        "LITAI_CONFIG_DIR": str(isolated),
        "LITAI_STATE_DIR": str(isolated / "state"),
        "LITAI_MCP_TRANSPORT": "off",
    }
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    yield
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session", autouse=True)
def _isolate_git_cache(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Give each xdist worker its own OBJ_DIR so concurrent git fetches don't collide.

    Without this, all workers share the same _build/repository-lineage cache and
    ``git fetch`` calls from different workers collide on the same .lock files.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    obj = tmp_path_factory.getbasetemp() / f"obj-{worker}"
    obj.mkdir(exist_ok=True)
    temporary = tmp_path_factory.getbasetemp() / "tmp"
    temporary.mkdir(exist_ok=True)
    build = tmp_path_factory.getbasetemp() / "build"
    build.mkdir(exist_ok=True)
    values = {
        "OBJ_DIR": str(obj),
        "BUILD_DIR": str(build),
        "TMPDIR": str(temporary),
        "TEMP": str(temporary),
        "TMP": str(temporary),
        "LITAI_TEST_WORKER": worker,
        "PYTHON_TEST_WORKERS": "1",
    }
    old = {key: os.environ.get(key) for key in values}
    previous_tempdir = tempfile.tempdir
    os.environ.update(values)
    tempfile.tempdir = str(temporary)
    yield
    tempfile.tempdir = previous_tempdir
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
