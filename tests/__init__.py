"""Literate-AI test suite."""

from __future__ import annotations

import os
from tempfile import TemporaryDirectory

# Mutagenic CLI verbs fan-out through the operator MCP catalog. Isolate that
# catalog so unittest and pytest never spawn user-configured MCPs or write
# skip lines onto stderr that CLI tests treat as failure.
_ISOLATED_OPERATOR_CONFIG = TemporaryDirectory(prefix="litai-test-config-")
os.environ.setdefault("LITAI_CONFIG_DIR", _ISOLATED_OPERATOR_CONFIG.name)
os.environ.setdefault("LITAI_MCP_TRANSPORT", "off")
