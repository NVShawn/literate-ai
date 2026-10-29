"""Exact installed-wheel authority for the Standard lifecycle driver."""

from __future__ import annotations

import csv
import errno
import io
import json
import os
import tempfile
import unittest
from functools import partial
from importlib.metadata import PathDistribution
from pathlib import Path, PurePosixPath
from unittest import mock

from literate_ai.adapters.standard_lifecycle_binding import (
    InstalledFrameworkDistribution,
    StandardLifecycleBindingError,
    observe_installed_framework_distribution,
)
from literate_ai.contracts import (
    load_current_standard_lifecycle_policy,
)


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str = "literate-ai",
        version: str = "0.0.0",
        entries: dict[str, bytes] | None = None,
        direct_url: object | None = None,
    ) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.metadata = {"Name": name}
        self.version = version
        self._direct_url = direct_url
        payload = entries or {
            "literate_ai/__init__.py": b'"""framework"""\n',
            "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}\n",
            "../../../share/literate-ai/schemas/v2/projects.json": b"{}\n",
            "literate_ai-0.0.0.dist-info/METADATA": (
                b"Metadata-Version: 2.4\nName: literate-ai\nVersion: 0.0.0\n"
            ),
            "literate_ai-0.0.0.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
            # Mutable installation records and generated launchers are excluded.
            "literate_ai-0.0.0.dist-info/RECORD": b"host-specific\n",
            "../../../bin/litai": b"generated launcher\n",
            "../../../bin/litai-mcp": b"generated mcp launcher\n",
        }
        self.files = tuple(PurePosixPath(path) for path in payload)
        self.locations: dict[str, Path] = {}
        for index, (entry, content) in enumerate(payload.items()):
            location = root / f"payload-{index}"
            location.write_bytes(content)
            self.locations[entry] = location

    def locate_file(self, path: object) -> Path:
        return self.locations[str(path)]

    def read_text(self, filename: str) -> str | None:
        if filename != "direct_url.json" or self._direct_url is None:
            return None
        if isinstance(self._direct_url, str):
            return self._direct_url
        return json.dumps(self._direct_url)


def observe(distribution: FakeDistribution) -> InstalledFrameworkDistribution:
    return observe_installed_framework_distribution(
        distribution_finder=lambda _name: (distribution,)
    )


class StandardLifecycleBindingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.distribution = FakeDistribution(self.root)
        self.observation = observe(self.distribution)
        self.policy = load_current_standard_lifecycle_policy()

    def recorded_distribution(self):
        root = self.root / "recorded"
        metadata = root / "literate_ai-0.0.0.dist-info"
        payload = {
            "literate_ai/__init__.py": b"framework\n",
            "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}\n",
            "literate_ai/ordinary.py": b"original\n",
            "share/literate-ai/schemas/v2/projects.json": b"{}\n",
            "literate_ai-0.0.0.dist-info/METADATA": (
                b"Name: literate-ai\nVersion: 0.0.0\n"
            ),
        }
        for name, data in payload.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        for name in (*payload, "literate_ai/missing.pyc"):
            writer.writerow((name, "", ""))
        record = metadata / "RECORD"
        record.write_bytes(stream.getvalue().encode("utf-8"))
        return root, record, PathDistribution(metadata)

    def test_record_inventory_skips_prefilter_but_reobserves_actual_bytes(self):
        root, _record, distribution = self.recorded_distribution()
        with mock.patch.object(
            PathDistribution,
            "files",
            new_callable=mock.PropertyMock,
            side_effect=AssertionError("existence-prefilter must not run"),
        ):
            first = observe(distribution)
            (root / "literate_ai/ordinary.py").write_bytes(b"modified\n")
            second = observe(distribution)
        self.assertNotEqual(first.identity, second.identity)
        self.assertEqual(len(first.members), 5)
        self.assertFalse(any(member.path.endswith(".pyc") for member in first.members))
        # The new enumeration preserves the existing canonical observation for
        # an intact wheel; only preliminary existence filtering is removed.
        legacy_files = tuple(distribution.files)
        read_text = distribution.read_text
        with (
            mock.patch.object(
                distribution,
                "read_text",
                side_effect=lambda name: None if name == "RECORD" else read_text(name),
            ),
            mock.patch.object(
                PathDistribution,
                "files",
                new_callable=mock.PropertyMock,
                return_value=legacy_files,
            ),
        ):
            self.assertEqual(second, observe(distribution))

    def test_missing_recorded_nonmandatory_member_refuses(self):
        root, _record, distribution = self.recorded_distribution()
        (root / "literate_ai/ordinary.py").unlink()
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(distribution)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_unavailable"
        )

    def test_malformed_and_excessive_record_inventories_refuse(self):
        _root, record, distribution = self.recorded_distribution()
        original = record.read_text()
        for value in ("", "one-column\n", '"unterminated', ",,\n"):
            with self.subTest(value=value):
                record.write_text(value)
                with self.assertRaises(StandardLifecycleBindingError) as caught:
                    observe(distribution)
                self.assertEqual(
                    caught.exception.code,
                    "standard_binding.distribution_payload_invalid",
                )
        record.write_text(original)
        for field in ("_MAXIMUM_RECORD_BYTES", "_MAXIMUM_PAYLOAD_FILES"):
            with (
                self.subTest(field=field),
                mock.patch(
                    "literate_ai.adapters.standard_lifecycle_binding." + field, 1
                ),
            ):
                with self.assertRaises(StandardLifecycleBindingError) as caught:
                    observe(distribution)
                self.assertEqual(
                    caught.exception.code,
                    "standard_binding.distribution_payload_invalid",
                )

    def test_editable_installations_are_explicitly_rejected(self) -> None:
        editable = FakeDistribution(
            self.root / "editable",
            direct_url={"url": "file:///source", "dir_info": {"editable": True}},
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(editable)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_editable"
        )

        pth = FakeDistribution(
            self.root / "pth",
            entries={
                "__editable__.literate_ai-0.0.0.pth": b"/source\n",
                "literate_ai/__init__.py": b"",
                "literate_ai/standard_policies/standard-lifecycle-v1.json": b"{}",
                "../../../share/literate-ai/schemas/v2/projects.json": b"{}",
            },
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(pth)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_editable"
        )

    def test_missing_symlinked_and_incomplete_payload_members_are_rejected(
        self,
    ) -> None:
        missing = FakeDistribution(self.root / "missing")
        missing.locations["literate_ai/__init__.py"].unlink()
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(missing)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_unavailable"
        )

        symlinked = FakeDistribution(self.root / "symlinked")
        member = symlinked.locations["literate_ai/__init__.py"]
        target = symlinked.root / "target"
        target.write_bytes(b"framework")
        member.unlink()
        member.symlink_to(target)
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(symlinked)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_unsafe"
        )

        incomplete = FakeDistribution(
            self.root / "incomplete",
            entries={
                "literate_ai/__init__.py": b"",
                "literate_ai-0.0.0.dist-info/METADATA": b"Name: literate-ai\n",
            },
        )
        with self.assertRaises(StandardLifecycleBindingError) as caught:
            observe(incomplete)
        self.assertEqual(
            caught.exception.code, "standard_binding.distribution_payload_incomplete"
        )

    def test_open_error_reobserves_prior_member_without_nofollow(self) -> None:
        cases = {
            "symlink": "standard_binding.distribution_payload_unsafe",
            "replacement": "standard_binding.distribution_changed",
            "directory": "standard_binding.distribution_changed",
            "unchanged": "standard_binding.distribution_payload_unavailable",
            "missing": "standard_binding.distribution_payload_unavailable",
        }
        for change, expected in cases.items():
            with self.subTest(change=change):
                distribution = FakeDistribution(self.root / f"open-refusal-{change}")
                member = distribution.locations["literate_ai/__init__.py"]
                backing = member.with_name("saved-member.py")
                original_open = os.open

                def refused(
                    file,
                    *args,
                    member=member,
                    backing=backing,
                    change=change,
                    original_open=original_open,
                    **kwargs,
                ):
                    if isinstance(file, (str, os.PathLike)) and Path(file) == member:
                        if change != "unchanged":
                            member.rename(backing)
                        if change == "symlink":
                            member.symlink_to(backing.name)
                        elif change == "replacement":
                            member.write_bytes(b"replacement payload")
                        elif change == "directory":
                            member.mkdir()
                        raise PermissionError(errno.EACCES, "forced open-time refusal")
                    return original_open(file, *args, **kwargs)

                with (
                    mock.patch.object(os, "O_NOFOLLOW", 0, create=True),
                    mock.patch.object(os, "open", side_effect=refused),
                ):
                    with self.assertRaises(StandardLifecycleBindingError) as caught:
                        observe(distribution)
                self.assertEqual(caught.exception.code, expected)
                self.assertIsInstance(caught.exception.__cause__, PermissionError)

    def test_member_redirected_to_its_original_inode_at_open_refuses(self) -> None:
        for nofollow in sorted({0, getattr(os, "O_NOFOLLOW", 0)}):
            with self.subTest(nofollow=nofollow):
                distribution = FakeDistribution(self.root / f"open-race-{nofollow}")
                member = distribution.locations["literate_ai/__init__.py"]
                backing = member.with_name("saved-member.py")
                original_io_open = io.open
                original_os_open = os.open

                def redirected(
                    open_function, file, *args, member=member, backing=backing, **kwargs
                ):
                    if (
                        isinstance(file, (str, os.PathLike))
                        and Path(file) == member
                        and not backing.exists()
                    ):
                        member.rename(backing)
                        member.symlink_to(backing.name)
                    return open_function(file, *args, **kwargs)

                with (
                    mock.patch.object(os, "O_NOFOLLOW", nofollow, create=True),
                    mock.patch.object(
                        io,
                        "open",
                        side_effect=partial(redirected, original_io_open),
                    ),
                    mock.patch.object(
                        os,
                        "open",
                        side_effect=partial(redirected, original_os_open),
                    ),
                ):
                    with self.assertRaises(StandardLifecycleBindingError) as caught:
                        observe(distribution)
                self.assertIn(
                    caught.exception.code,
                    {
                        "standard_binding.distribution_payload_unsafe",
                        "standard_binding.distribution_changed",
                    },
                )


if __name__ == "__main__":
    unittest.main()
