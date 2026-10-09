"""Mix literal graph, acquired archive checksum and source admission checks."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import unittest
from dataclasses import replace

from literate_ai.adapters.dependencies.acquisition import _generated_lock_projection
from literate_ai.adapters.dependencies.admission import reconcile_generated_dependencies
from literate_ai.adapters.dependencies.hex_archive import verify_hex_archive
from literate_ai.adapters.dependencies.mix_lock import MixLock, MixLockedPackage
from literate_ai.adapters.dependencies.types import DependencyObservationError
from literate_ai.contracts.mix_projects import MixDependencyIntent, MixProjectIntent


def _project():
    return MixProjectIntent(
        "fixture",
        "1.0.0",
        "Mix dependency fixture",
        ("MIT",),
        (MixDependencyIntent("decimal", "== 2.3.0"),),
        links=(("Source", "https://example.invalid/fixture"),),
    )


def _entry(name="decimal", deps="[]", manager="mix", repo="hexpm"):
    return (
        f'"{name}": {{:hex, :{name}, "2.3.0", "' + "a" * 64 + '", '
        f'[:{manager}], {deps}, "{repo}", "' + "b" * 64 + '"}'
    )


def _lock(*entries):
    return ("%{\n" + ",\n".join(entries) + ",\n}\n").encode()


class MixLockTests(unittest.TestCase):
    def parse(self, content):
        return MixLock.from_bytes(content, project=_project())

    def test_native_registry_tuple_and_root_graph(self):
        lock = self.parse(_lock(_entry()))
        self.assertEqual(lock.edges, (("@root", "decimal"),))
        self.assertEqual(lock.packages[0].inner_sha256, "a" * 64)
        self.assertEqual(lock.packages[0].outer_sha256, "b" * 64)

    def test_complete_transitive_graph_and_unselected_optional_dependency(self):
        deps = (
            '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: false]}, '
            '{:absent, "~> 1.0", [hex: :absent, repo: "hexpm", optional: true]}]'
        )
        lock = self.parse(_lock(_entry(deps=deps), _entry("child")))
        self.assertEqual(lock.edges, (("@root", "decimal"), ("decimal", "child")))

    def test_selected_optional_dependency_is_in_the_complete_graph(self):
        deps = '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: true]}]'
        lock = self.parse(_lock(_entry(deps=deps), _entry("child")))
        self.assertIn(("decimal", "child"), lock.edges)

    def test_missing_required_transitive_and_root_dependencies_fail(self):
        deps = '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: false]}]'
        for content in (b"%{}", _lock(_entry(deps=deps))):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.parse(content)

    def test_executable_terms_interpolation_and_trailing_code_fail(self):
        for content in (
            b'%{"decimal" => File.read!("anything")}',
            _lock(_entry()).replace(b'"2.3.0"', b'"#{System.cmd("id", [])}"'),
            _lock(_entry()) + b'System.cmd("id", [])',
            b'Code.eval_string("%{}")',
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.parse(content)

    def test_duplicate_keys_packages_and_dependency_options_fail(self):
        dup_option = (
            '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", '
            "optional: true, optional: false]}]"
        )
        dup_dep = (
            '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: true]}, '
            '{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: true]}]'
        )
        for content in (
            _lock(_entry(), _entry()),
            _lock(_entry(deps=dup_option)),
            _lock(_entry(deps=dup_dep)),
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.parse(content)

    def test_git_path_private_registry_aliases_and_nonmix_managers_fail(self):
        for content in (
            b'%{"decimal" => {:git, "https://example.invalid/repo", "abc", []}}',
            _lock(_entry(manager="rebar3")),
            _lock(_entry(repo="private")),
            _lock(_entry()).replace(b":decimal", b":different"),
            _lock(_entry()).replace(b'"' + b"b" * 64 + b'"', b'"no checksum"'),
        ):
            with (
                self.subTest(content=content),
                self.assertRaises(DependencyObservationError),
            ):
                self.parse(content)

    def test_unreachable_packages_are_not_acquisition_authority(self):
        with self.assertRaisesRegex(DependencyObservationError, "outside the root"):
            self.parse(_lock(_entry(), _entry("unreachable")))

    def test_comments_and_whitespace_are_inert(self):
        self.assertEqual(
            self.parse(b"# native Mix lock\n" + _lock(_entry())),
            self.parse(_lock(_entry())),
        )

    def test_native_atom_key_forms_have_the_same_checked_identity(self):
        quoted = _lock(_entry())
        unquoted = quoted.replace(b'"decimal":', b"decimal:")
        explicit_atom = quoted.replace(b'"decimal":', b":decimal =>")
        self.assertEqual(self.parse(quoted), self.parse(unquoted))
        self.assertEqual(self.parse(quoted), self.parse(explicit_atom))
        with self.assertRaises(DependencyObservationError):
            self.parse(quoted.replace(b'"decimal":', b'"decimal" =>'))

    def test_nonliteral_optional_flag_is_rejected_with_typed_error(self):
        deps = '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: []]}]'
        with self.assertRaises(DependencyObservationError):
            self.parse(_lock(_entry(deps=deps)))

    def test_encoding_resource_and_nesting_budgets_fail_closed(self):
        for content in (
            b"\xff",
            b" " * (2 * 1024 * 1024 + 1),
            b'%{"x" => ' + b"[" * 18 + b"]" * 18 + b"}",
        ):
            with (
                self.subTest(size=len(content)),
                self.assertRaises(DependencyObservationError),
            ):
                self.parse(content)


def _archive(entries=None):
    files = {
        "VERSION": b"3",
        "metadata.config": b"native metadata",
        "contents.tar.gz": b"compressed package bytes",
    }
    inner = hashlib.sha256(b"".join(files.values())).hexdigest()
    files["CHECKSUM"] = inner.upper().encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tar:
        for name, content in entries if entries is not None else files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    content = stream.getvalue()
    package = MixLockedPackage(
        "decimal", "2.3.0", inner, hashlib.sha256(content).hexdigest(), ()
    )
    return content, package, files


class HexArchiveTests(unittest.TestCase):
    def test_actual_bytes_are_checked_against_both_native_checksums(self):
        content, package, _ = _archive()
        evidence = verify_hex_archive(content, package=package)
        self.assertEqual(evidence.outer_sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(evidence.inner_sha256, package.inner_sha256)
        self.assertTrue(evidence.identity.startswith("sha256:"))

    def test_changed_bytes_and_changed_inner_lock_checksum_fail(self):
        content, package, _ = _archive()
        with self.assertRaises(DependencyObservationError):
            verify_hex_archive(content + b"changed", package=package)
        with self.assertRaises(DependencyObservationError):
            verify_hex_archive(content, package=replace(package, inner_sha256="c" * 64))

    def test_duplicate_missing_extra_and_escaping_archive_members_fail(self):
        _, _, files = _archive()
        entries = list(files.items())
        for bad in (
            entries + entries[:1],
            entries[:-1],
            entries + [("../outside", b"escape")],
            entries + [("extra", b"extra")],
        ):
            content, package, _ = _archive(bad)
            with (
                self.subTest(entries=bad),
                self.assertRaises(DependencyObservationError),
            ):
                verify_hex_archive(content, package=package)

    def test_symlink_archive_entry_is_not_acquired_file_evidence(self):
        content, package, _ = _archive()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as tar:
            info = tarfile.TarInfo("metadata.config")
            info.type = tarfile.SYMTYPE
            info.linkname = "outside"
            tar.addfile(info)
        content = stream.getvalue()
        with self.assertRaises(DependencyObservationError):
            verify_hex_archive(
                content,
                package=replace(
                    package, outer_sha256=hashlib.sha256(content).hexdigest()
                ),
            )


class MixDependencyAdmissionTests(unittest.TestCase):
    def test_declared_hex_dependency_must_be_present_in_source_bom(self):
        files = {"source/mix-project.json": json.dumps(_project().to_dict())}
        with self.assertRaisesRegex(
            DependencyObservationError, "absent from the source BOM"
        ):
            reconcile_generated_dependencies(files, b'{"components": []}')
        reconcile_generated_dependencies(files, b'{"components": [{"name":"decimal"}]}')

    def test_derived_lock_transitives_must_be_in_source_bom(self):
        deps = '[{:child, "~> 2.0", [hex: :child, repo: "hexpm", optional: false]}]'
        files = {
            "source/mix-project.json": json.dumps(_project().to_dict()),
            "source/mix.lock": _lock(_entry(deps=deps), _entry("child")).decode(),
        }
        with self.assertRaisesRegex(
            DependencyObservationError, "absent from the source BOM"
        ):
            reconcile_generated_dependencies(
                files, b'{"components": [{"name":"decimal"}]}'
            )

    def test_raw_native_manifest_is_not_admitted_as_data(self):
        with self.assertRaisesRegex(DependencyObservationError, "declarative"):
            reconcile_generated_dependencies(
                {"source/mix.exs": 'File.write!("outside", "executed")'}, b"{}"
            )

    def test_parsed_lock_and_intent_do_not_replace_verified_acquisition(self):
        for name, content in (
            ("mix-project.json", json.dumps(_project().to_dict())),
            ("mix.lock", _lock(_entry()).decode()),
            ("mix.exs", "Mix.Project"),
        ):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(DependencyObservationError, "lifecycle-owned"),
            ):
                _generated_lock_projection({"source/" + name: content})


if __name__ == "__main__":
    unittest.main()
