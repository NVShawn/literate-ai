from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.check_host_path_policy import violations


class HostPathPolicyTests(unittest.TestCase):
    def test_absolute_literals_and_ambient_home_fail_outside_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "src" / "example.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                "from pathlib import Path\n"
                'SYSTEM = Path("/etc/example")\n'
                "HOME = Path.home()\n",
                encoding="utf-8",
            )

            findings = violations(repository)

        self.assertEqual(len(findings), 2)
        self.assertTrue(any("absolute Path literal" in item for item in findings))
        self.assertTrue(any("Path.home()" in item for item in findings))

    def test_relative_paths_and_the_central_owner_are_admitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            source = repository / "src" / "relative.py"
            source.parent.mkdir(parents=True)
            source.write_text(
                'from pathlib import Path\nRESOURCE = Path("assets/example")\n',
                encoding="utf-8",
            )
            owner = repository / "src" / "literate_ai" / "adapters" / "user_paths.py"
            owner.parent.mkdir(parents=True)
            owner.write_text(
                "from pathlib import Path\n"
                'SYSTEM = Path("/etc/example")\n'
                "HOME = Path.home()\n",
                encoding="utf-8",
            )

            self.assertEqual(violations(repository), ())


if __name__ == "__main__":
    unittest.main()
