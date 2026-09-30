"""Keep ELF inherited loader paths scoped to the executable being observed."""

import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from literate_ai.adapters.dependencies.observation import (
    LinuxElfDependencyObserver,
    _ElfImage,
    _inspect_elf,
)
from literate_ai.adapters.linux_loader_paths import LinuxLoaderPaths


class ElfInheritedPathTests(unittest.TestCase):
    def test_inspection_preserves_empty_runpath_presence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "app"
            binary.write_bytes(b"\x7fELFfixture")
            for dynamic, present in (("", False), ("0x (RUNPATH) path: []", True)):
                with self.subTest(present=present):
                    outputs = {
                        "-h": "Class: ELF64\nMachine: fixture\n",
                        "-d": dynamic,
                        "-l": "",
                        "-n": "",
                    }
                    with mock.patch(
                        "literate_ai.adapters.dependencies.observation._run_bounded_tool",
                        side_effect=lambda _tool, argv, outputs=outputs, **_kwargs: (
                            outputs[argv[0]]
                        ),
                    ):
                        image = _inspect_elf(root / "readelf", binary)
                    self.assertEqual(image.runpath_present, present)
                    self.assertEqual(image.search_paths, ())

    def observe(
        self,
        root,
        *,
        app_runpath=False,
        child_paths=(),
        child_runpath=False,
        inspector=False,
        loader_paths=None,
        dual_role=False,
    ):
        app = str(root / "app")
        child = str(root / "private" / "child.so")
        leaf = str(root / "private" / "leaf.so")
        cached_leaf = str(root / "cache" / "leaf.so")
        own_leaf = str(root / "child-own" / "leaf.so")

        def image(path, needed=(), paths=(), runpath=False):
            return _ElfImage(
                path,
                path,
                ("ELF64", "fixture"),
                needed,
                paths,
                None,
                runpath_present=runpath,
            )

        images = {
            app: image(app, ("child.so",), ("$ORIGIN/private",), app_runpath),
            child: image(child, ("leaf.so",), child_paths, child_runpath),
            leaf: image(leaf),
            cached_leaf: image(cached_leaf),
            own_leaf: image(own_leaf),
        }
        if loader_paths is not None:
            for directory in loader_paths.library:
                env_leaf = str(root / directory / "leaf.so")
                images[env_leaf] = image(env_leaf)
        seeds = {app: {"runtime"}}
        if dual_role:
            seeds[app].add("build")
        if inspector:
            tool = str(root / "inspector")
            images[tool] = image(tool, (child,))
            seeds[tool] = {"build", "toolchain"}

        def resolve(candidates, _architecture, _tool):
            for path in candidates:
                if path in images:
                    return path
            self.fail(f"No fixture matches candidates: {candidates}")

        with (
            mock.patch(
                "literate_ai.adapters.dependencies.observation._inspect_elf",
                side_effect=lambda _tool, path: images[str(path)],
            ),
            mock.patch(
                "literate_ai.adapters.dependencies.observation._resolve_exact_elf_candidate",
                side_effect=resolve,
            ),
        ):
            _, edges = LinuxElfDependencyObserver(loader_paths=loader_paths)._closure(
                root / "readelf",
                seeds,
                {"leaf.so": (cached_leaf,)},
            )
        return edges, child, leaf, cached_leaf, own_leaf

    def test_environment_follows_rpath_and_precedes_runpath(self):
        root = PurePosixPath("/fixture")
        paths = LinuxLoaderPaths(("/environment",))
        with mock.patch(
            "literate_ai.adapters.dependencies.observation.Path", PurePosixPath
        ):
            edges, child, leaf, _, _ = self.observe(root, loader_paths=paths)
            self.assertIn((child, leaf), edges)
            self.assertNotIn((child, "/environment/leaf.so"), edges)
            edges, child, leaf, _, own = self.observe(
                root,
                loader_paths=paths,
                child_paths=(str(root / "child-own"),),
                child_runpath=True,
            )
            self.assertIn((child, "/environment/leaf.so"), edges)
            self.assertNotIn((child, own), edges)
            self.assertNotIn((child, leaf), edges)

    def test_environment_does_not_leak_into_inspector_or_dual_role_seed(self):
        root = PurePosixPath("/fixture")
        paths = LinuxLoaderPaths(("/environment",))
        for arguments in ({"inspector": True}, {"dual_role": True}):
            with (
                self.subTest(arguments=arguments),
                mock.patch(
                    "literate_ai.adapters.dependencies.observation.Path", PurePosixPath
                ),
            ):
                edges, child, _, cached, _ = self.observe(
                    root, loader_paths=paths, app_runpath=True, **arguments
                )
                self.assertIn((child, "/environment/leaf.so"), edges)
                self.assertIn((child, cached), edges)

    def test_rpath_is_inherited_with_original_image_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            edges, child, leaf, cached, _ = self.observe(Path(temporary))
            self.assertIn((child, leaf), edges)
            self.assertNotIn((child, cached), edges)

    def test_runpath_is_not_inherited(self):
        with tempfile.TemporaryDirectory() as temporary:
            edges, child, leaf, cached, _ = self.observe(
                Path(temporary), app_runpath=True
            )
            self.assertIn((child, cached), edges)
            self.assertNotIn((child, leaf), edges)

    def test_child_empty_runpath_suppresses_ancestor_rpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            edges, child, leaf, cached, _ = self.observe(
                Path(temporary), child_runpath=True
            )
            self.assertIn((child, cached), edges)
            self.assertNotIn((child, leaf), edges)

    def test_child_rpath_precedes_ancestor_rpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edges, child, leaf, _, own = self.observe(
                root, child_paths=(str(root / "child-own"),)
            )
            self.assertIn((child, own), edges)
            self.assertNotIn((child, leaf), edges)

    def test_shared_image_keeps_each_seed_inheritance_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            edges, child, leaf, cached, _ = self.observe(
                Path(temporary), inspector=True
            )
            self.assertIn((child, leaf), edges)
            self.assertIn((child, cached), edges)
