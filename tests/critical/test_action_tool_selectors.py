"""Actual selector transport verifies private resolution and challenged authority."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from literate_ai.adapters.action_dispatch_wire import ActionWireError
from literate_ai.adapters.action_tool_selectors import (
    probe_command_tool_selectors,
)
from tests.support import fixtures_test_action_tool_observation as fixture


class ActionToolSelectorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ActionToolObservationTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.shadow = self.fixture.root / "shadow"
        self.shadow.mkdir()
        receiver = self.fixture.root / "receiver.py"
        receiver.write_text(
            receiver.read_text().replace(
                "environment=dict(os.environ)",
                "environment=dict(os.environ, PATH="
                + repr(str(self.shadow))
                + "+os.pathsep+os.path.dirname(sys.executable), "
                "PATHEXT=os.environ.get('PATHEXT','.EXE;.CMD'))",
            )
        )
        self.selector = {"python": (Path(sys.executable).name,)}

    def observed(self):
        return self.fixture.probe(self.fixture.admission())

    def probe(self, observed, selectors=None):
        return probe_command_tool_selectors(
            self.fixture.worker,
            observed,
            self.fixture.deadline,
            self.selector if selectors is None else selectors,
            cwd=self.fixture.root,
            environment=self.fixture.environment,
        )

    def test_new_path_shadow_refuses_without_changing_registered_inventory(self):
        observed = self.observed()
        self.probe(observed)
        shadow = self.shadow / Path(sys.executable).name
        shadow.write_bytes(b"must never execute this selector")
        shadow.chmod(0o755)
        self.assertEqual(self.observed().tools, observed.tools)
        with self.assertRaises(ActionWireError):
            self.probe(observed)
        with patch(
            "literate_ai.adapters.action_tool_selectors.run_command_observation"
        ) as transport:
            with self.assertRaises(ActionWireError):
                self.probe(observed, {"missing": ("tool",)})
            transport.assert_not_called()
