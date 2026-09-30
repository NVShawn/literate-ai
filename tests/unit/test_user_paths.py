from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest import mock

from literate_ai.adapters.user_paths import (
    HostInstallLayout,
    UserPathError,
    prepare_user_directory,
    resolve_host_paths,
    resolve_host_system_paths,
    resolve_user_home,
    resolve_user_paths,
)


class UserPathTests(unittest.TestCase):
    def test_user_home_uses_platform_native_environment_and_known_folder(self) -> None:
        self.assertEqual(
            resolve_user_home(
                platform_family="linux",
                environment={"HOME": "/users/operator"},
            ),
            PurePosixPath("/users/operator"),
        )
        self.assertEqual(
            resolve_user_home(
                platform_family="windows",
                environment={"USERPROFILE": "D:/Profiles/operator"},
            ),
            PureWindowsPath("D:/Profiles/operator"),
        )
        self.assertEqual(
            resolve_user_home(
                platform_family="windows",
                environment={},
                windows_known_folder=lambda kind: {
                    "profile": PureWindowsPath("C:/Users/operator")
                }[kind],
            ),
            PureWindowsPath("C:/Users/operator"),
        )

        with self.assertRaises(UserPathError) as caught:
            resolve_user_home(
                platform_family="macos",
                environment={},
                home=PurePosixPath("relative"),
            )
        self.assertEqual(caught.exception.code, "user_paths.home_relative")

    def test_install_layout_is_centralized_and_platform_native(self) -> None:
        posix = HostInstallLayout.for_prefix(
            PurePosixPath("/users/operator/.local"), platform_family="linux"
        )
        self.assertEqual(
            posix.environment,
            PurePosixPath("/users/operator/.local/share/literate-ai/venv"),
        )
        self.assertEqual(
            posix.launcher, PurePosixPath("/users/operator/.local/bin/litai")
        )

        windows = HostInstallLayout.for_prefix(
            PureWindowsPath("C:/Users/operator/AppData/Local/literate-ai"),
            platform_family="windows",
        )
        self.assertEqual(
            windows.environment,
            PureWindowsPath(
                "C:/Users/operator/AppData/Local/literate-ai/share/literate-ai/venv"
            ),
        )
        self.assertEqual(
            windows.launcher,
            PureWindowsPath(
                "C:/Users/operator/AppData/Local/literate-ai/bin/litai.cmd"
            ),
        )

    def test_system_path_policy_does_not_consult_user_configuration(self) -> None:
        with mock.patch(
            "literate_ai.adapters.user_paths.Path.home",
            side_effect=AssertionError("system paths must not consult home"),
        ):
            linux = resolve_host_system_paths(platform_family="linux")
        self.assertEqual(linux.os_release_file, PurePosixPath("/etc/os-release"))
        self.assertEqual(
            linux.linux_ldconfig_candidates,
            (PurePosixPath("/sbin/ldconfig"), PurePosixPath("/usr/sbin/ldconfig")),
        )
        self.assertEqual(linux.bubblewrap_proc_root, PurePosixPath("/proc"))
        self.assertEqual(linux.bubblewrap_device_root, PurePosixPath("/dev"))
        self.assertEqual(linux.bubblewrap_temporary_root, PurePosixPath("/tmp"))

    def test_linux_and_macos_share_the_posix_cli_configuration_root(self) -> None:
        for family in ("linux", "macos"):
            with self.subTest(platform=family):
                paths = resolve_user_paths(
                    platform_family=family,
                    environment={},
                    home=PurePosixPath("/users/operator"),
                )
                self.assertEqual(
                    paths.config_root,
                    PurePosixPath("/users/operator/.config/literate-ai"),
                )
                self.assertEqual(
                    paths.state_root,
                    PurePosixPath("/users/operator/.local/state/literate-ai"),
                )

    def test_absolute_xdg_and_litai_overrides_have_explicit_precedence(self) -> None:
        paths = resolve_user_paths(
            platform_family="linux",
            environment={
                "XDG_CONFIG_HOME": "/xdg/config",
                "XDG_STATE_HOME": "/xdg/state",
                "LITAI_CONFIG_DIR": "/explicit/config",
                "LITAI_STATE_DIR": "/explicit/state",
            },
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(paths.config_root, PurePosixPath("/explicit/config"))
        self.assertEqual(paths.state_root, PurePosixPath("/explicit/state"))

    def test_relative_overrides_and_xdg_roots_fail_closed(self) -> None:
        for name in (
            "LITAI_CONFIG_DIR",
            "LITAI_STATE_DIR",
            "XDG_CONFIG_HOME",
            "XDG_STATE_HOME",
        ):
            with self.subTest(environment=name):
                with self.assertRaises(UserPathError) as caught:
                    resolve_user_paths(
                        platform_family="macos",
                        environment={name: "relative"},
                        home=PurePosixPath("/users/operator"),
                    )
                self.assertEqual(caught.exception.code, "user_paths.override_relative")

    def test_windows_uses_roaming_config_and_local_state_known_folders(self) -> None:
        roots = {
            "roaming": PureWindowsPath("C:/Users/operator/AppData/Roaming"),
            "local": PureWindowsPath("C:/Users/operator/AppData/Local"),
        }
        paths = resolve_user_paths(
            platform_family="windows",
            environment={},
            windows_known_folder=roots.__getitem__,
        )
        self.assertEqual(
            paths.config_root,
            PureWindowsPath("C:/Users/operator/AppData/Roaming/literate-ai"),
        )
        self.assertEqual(
            paths.state_root,
            PureWindowsPath("C:/Users/operator/AppData/Local/literate-ai/state"),
        )

    def test_complete_host_policy_has_semantic_roots_on_every_platform(self) -> None:
        for family in ("linux", "macos"):
            with self.subTest(platform=family):
                paths = resolve_host_paths(
                    platform_family=family,
                    environment={},
                    home=PurePosixPath("/users/operator"),
                    temporary_root=PurePosixPath("/private/tmp/litai"),
                )
                self.assertEqual(
                    paths.data_root,
                    PurePosixPath("/users/operator/.local/share/literate-ai"),
                )
                self.assertEqual(
                    paths.cache_root,
                    PurePosixPath("/users/operator/.cache/literate-ai"),
                )
                self.assertEqual(paths.managed_tool_root, paths.data_root / "tools")
                self.assertEqual(
                    paths.install_root, PurePosixPath("/users/operator/.local")
                )
                self.assertEqual(
                    paths.short_staging_root,
                    PurePosixPath("/private/tmp/litai/stage"),
                )
                self.assertEqual(
                    paths.os_release_file,
                    PurePosixPath("/etc/os-release") if family == "linux" else None,
                )
                self.assertEqual(
                    paths.apparmor_user_namespace_restriction_file,
                    (
                        PurePosixPath(
                            "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
                        )
                        if family == "linux"
                        else None
                    ),
                )
                self.assertEqual(
                    paths.apparmor_bwrap_profile_file,
                    (
                        PurePosixPath("/etc/apparmor.d/bwrap-userns-restrict")
                        if family == "linux"
                        else None
                    ),
                )
                self.assertEqual(
                    paths.linux_ldconfig_candidates,
                    (
                        (
                            PurePosixPath("/sbin/ldconfig"),
                            PurePosixPath("/usr/sbin/ldconfig"),
                        )
                        if family == "linux"
                        else ()
                    ),
                )
                self.assertEqual(
                    paths.sandbox_runtime_roots,
                    (
                        (
                            PurePosixPath("/System"),
                            PurePosixPath("/usr"),
                            PurePosixPath("/bin"),
                            PurePosixPath("/dev"),
                        )
                        if family == "macos"
                        else (
                            PurePosixPath("/usr"),
                            PurePosixPath("/bin"),
                            PurePosixPath("/lib"),
                            PurePosixPath("/lib64"),
                        )
                    ),
                )

        roots = {
            "roaming": PureWindowsPath("D:/Profiles/operator/AppData/Roaming"),
            "local": PureWindowsPath("D:/Profiles/operator/AppData/Local"),
        }
        windows = resolve_host_paths(
            platform_family="windows",
            environment={"TEMP": "E:/Temporary"},
            windows_known_folder=roots.__getitem__,
        )
        self.assertEqual(
            windows.data_root,
            PureWindowsPath("D:/Profiles/operator/AppData/Local/literate-ai/data"),
        )
        self.assertEqual(
            windows.managed_tool_root,
            PureWindowsPath("D:/Profiles/operator/AppData/Local/literate-ai/tools"),
        )
        self.assertEqual(windows.temporary_root, PureWindowsPath("E:/Temporary"))
        self.assertEqual(windows.short_staging_root, PureWindowsPath("D:/litai-stage"))
        self.assertIsNone(windows.os_release_file)
        self.assertIsNone(windows.apparmor_user_namespace_restriction_file)
        self.assertIsNone(windows.apparmor_bwrap_profile_file)
        self.assertEqual(windows.linux_ldconfig_candidates, ())
        self.assertEqual(windows.sandbox_runtime_roots, ())
        self.assertIsNone(windows.bubblewrap_proc_root)
        self.assertIsNone(windows.bubblewrap_device_root)
        self.assertIsNone(windows.bubblewrap_temporary_root)

    def test_complete_host_policy_honors_absolute_overrides(self) -> None:
        paths = resolve_host_paths(
            platform_family="linux",
            environment={
                "LITAI_DATA_DIR": "/policy/data",
                "LITAI_CACHE_DIR": "/policy/cache",
                "LITAI_TOOL_DIR": "/policy/tools",
                "LITAI_INSTALL_DIR": "/policy/install",
                "LITAI_TEMP_DIR": "/policy/temp",
                "LITAI_STAGING_DIR": "/policy/stage",
            },
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(paths.data_root, PurePosixPath("/policy/data"))
        self.assertEqual(paths.cache_root, PurePosixPath("/policy/cache"))
        self.assertEqual(paths.managed_tool_root, PurePosixPath("/policy/tools"))
        self.assertEqual(paths.install_root, PurePosixPath("/policy/install"))
        self.assertEqual(paths.temporary_root, PurePosixPath("/policy/temp"))
        self.assertEqual(paths.short_staging_root, PurePosixPath("/policy/stage"))

    def test_missing_home_and_relative_known_folder_fail_closed(self) -> None:
        with mock.patch(
            "literate_ai.adapters.user_paths.Path.home",
            side_effect=RuntimeError("unavailable"),
        ):
            with self.assertRaises(UserPathError) as missing:
                resolve_user_paths(platform_family="linux", environment={})
        self.assertEqual(missing.exception.code, "user_paths.home_unavailable")

        with self.assertRaises(UserPathError) as relative:
            resolve_user_paths(
                platform_family="windows",
                environment={},
                windows_known_folder=lambda _kind: PureWindowsPath("relative"),
            )
        self.assertEqual(relative.exception.code, "user_paths.known_folder_invalid")

    def test_project_test_config_is_scoped_and_windows_portable(self) -> None:
        paths = resolve_user_paths(
            platform_family="linux",
            environment={},
            home=PurePosixPath("/users/operator"),
        )
        self.assertEqual(
            paths.project_test_config("literate-ai"),
            paths.config_root / "projects" / "literate-ai" / "test.json",
        )
        unsafe = paths.project_test_config("CON")
        self.assertEqual(unsafe.name, "test.json")
        self.assertTrue(unsafe.parent.name.startswith("sha256-"))

    def test_private_directory_preparation_enforces_custody_and_permissions(
        self,
    ) -> None:
        import os
        import stat
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            destination = root / "nested" / "events"

            prepare_user_directory(root, destination)

            self.assertTrue(destination.is_dir())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            with self.assertRaises(UserPathError):
                prepare_user_directory(root, Path(temporary) / "outside")

    def test_private_root_creation_handles_an_absent_config_parent(self) -> None:
        import os
        import stat
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            root = home / ".config" / "literate-ai"
            destination = root / "projects" / "alpha"

            prepare_user_directory(root, destination)

            self.assertTrue(destination.is_dir())
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
