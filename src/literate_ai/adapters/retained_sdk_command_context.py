"""Portable historical retained launch facts, never a live SDK command scope."""

from pathlib import PurePosixPath, PureWindowsPath

from literate_ai.contracts import canonical_identity, canonical_json_bytes

CONTEXT_SCHEMA = "literate-ai/retained-sdk-command-context@1"
EXECUTION_SCHEMA = "literate-ai/native-sdk-command-execution@5"
_MAX_CONTEXT_BYTES = 1024**2
_BASE_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONNOUSERSITE": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def _require(condition):
    if not condition:
        raise ValueError("retained.sdk.historical-command-context-differs")


def verify_retained_sdk_command_context(
    context,
    *,
    plan,
    manifest,
    output_manifest,
    acceptance_policy,
    command_identity,
    phase,
    command_binding=None,
):
    """Reconstruct exact launch values using only portable recorded path strings.

    The worker's authenticated execution owner remains the authority for actual
    execution. This checker grants no admission and performs no filesystem access.
    """
    _require(
        type(context) is dict
        and set(context)
        == {
            "schema",
            "platform",
            "workspace_root",
            "argv",
            "cwd",
            "environment",
            "substitutions",
        }
        and context["schema"] == CONTEXT_SCHEMA
    )
    platform = context["platform"]
    _require(platform in {"freebsd", "linux", "macos", "windows"})
    path_type = PureWindowsPath if platform == "windows" else PurePosixPath
    separator = ";" if platform == "windows" else ":"

    def absolute(value):
        _require(type(value) is str and 0 < len(value) <= 65536 and "\x00" not in value)
        path = path_type(value)
        _require(path.is_absolute() and ".." not in path.parts and str(path) == value)
        return path

    workspace = absolute(context["workspace_root"])
    inputs, objects, outputs = (
        workspace / "snapshot" / "tree",
        workspace / "objects",
        workspace / "outputs",
    )
    argv, environment, substitutions = (
        context["argv"],
        context["environment"],
        context["substitutions"],
    )
    _require(type(argv) is list and 1 <= len(argv) <= 4096)
    _require(type(environment) is dict and len(environment) <= 32)
    _require(type(substitutions) is dict and len(substitutions) <= 1024)
    for value in argv:
        _require(type(value) is str and len(value) <= 65536 and "\x00" not in value)
    for mapping in (environment, substitutions):
        for key, value in mapping.items():
            _require(type(key) is str and 0 < len(key) <= 256 and "\x00" not in key)
            _require(type(value) is str and len(value) <= 65536 and "\x00" not in value)
    _require(
        sum(map(len, argv))
        + sum(
            len(k) + len(v)
            for mapping in (environment, substitutions)
            for k, v in mapping.items()
        )
        <= _MAX_CONTEXT_BYTES
    )
    _require(len(canonical_json_bytes(context)) <= _MAX_CONTEXT_BYTES)
    cwd = absolute(context["cwd"])
    expected_substitutions = {
        "{input_root}": str(inputs),
        "{object_root}": str(objects),
        "{output_root}": str(outputs),
    }
    secondary = plan.profile.secondary_access
    if secondary is not None:
        expected_substitutions["{secondary_account}"] = secondary.account.name
        view_roots = set()
        for role, names in (
            ("view", tuple(v.name for v in secondary.views)),
            ("fixture", secondary.fixtures),
        ):
            for name in names:
                key = "{secondary_" + role + ":" + name + "}"
                path = absolute(substitutions.get(key))
                _require(
                    path.name == name and path.parent.name.startswith("retained-views-")
                )
                view_roots.add(path.parent)
                expected_substitutions[key] = str(path)
        _require(len(view_roots) == 1)
    _require(substitutions == expected_substitutions)

    actions = [
        a
        for a in plan.profile.actions
        if canonical_identity(a.to_dict()) == command_identity
    ]
    cases = [
        case
        for case in (() if acceptance_policy is None else acceptance_policy.cases)
        if case.identity == command_identity
    ]
    _require(len(actions) + len(cases) == 1)
    expected_environment = dict(_BASE_ENVIRONMENT)
    home, temporary = workspace / "home", workspace / "tmp"
    if actions:
        action = actions[0]
        _require(action.stage == phase and cwd == objects)
        tool = next(
            (
                path
                for name, path, _identity in plan.profile.tools
                if name == action.tool
            ),
            None,
        )
        _require(tool is not None)
        arguments = []
        for argument in action.argv:
            for key, value in expected_substitutions.items():
                argument = argument.replace(key, value)
            _require("{secondary_" not in argument)
            arguments.append(argument)
        expected_argv = [str(inputs / tool), *arguments]
    else:
        case = cases[0]
        _require(phase == "accept")
        member = next(
            (m for m in output_manifest.members if m.path == case.executable), None
        )
        _require(member is not None and member.symlink_target is None)
        if case.filesystem is None:
            _require(cwd == outputs)
        else:
            _require(cwd.parent == objects and cwd.name.startswith("verifier-case-"))
            home = temporary = cwd
        arguments = [
            value.replace("{output_root}", str(outputs)) for value in case.argv
        ]
        if case.filesystem is not None:
            arguments = [value.replace("{case_root}", str(cwd)) for value in arguments]
        expected_argv = [str(outputs / case.executable), *arguments]
    _require(argv == expected_argv)
    expected_environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "TMP": str(temporary),
            "TEMP": str(temporary),
            "PATH": separator.join(
                [
                    *(
                        str(outputs / path)
                        for path in plan.profile.output_command_paths
                    ),
                    *sorted(
                        {
                            str((inputs / path).parent)
                            for _name, path, _identity in plan.profile.tools
                        }
                    ),
                ]
            ),
        }
    )
    if platform == "windows":
        expected_environment["SystemRoot"] = str(
            absolute(environment.get("SystemRoot"))
        )
    if plan.profile.runtime_library_paths:
        _require(platform != "windows")
        variable = "DYLD_LIBRARY_PATH" if platform == "macos" else "LD_LIBRARY_PATH"
        _require(
            all(
                path in manifest.directories
                for path in plan.profile.runtime_library_paths
            )
        )
        expected_environment[variable] = ":".join(
            str(inputs / path) for path in plan.profile.runtime_library_paths
        )
    # Exact keys reject credentials and arbitrary loader/compiler overrides before
    # anything is retained; redaction would destroy the exact environment binding.
    _require(environment == expected_environment)
    if command_binding is not None:
        for name, value in (
            ("argv", argv),
            ("cwd", str(cwd)),
            ("environment", environment),
        ):
            _require(
                command_binding[name + "_identity"]
                == canonical_identity(value).to_dict()
            )
        _require(
            command_binding["phase"] == phase
            and command_binding["command_identity"] == command_identity.to_dict()
            and command_binding["command_contract_identity"] == plan.identity.to_dict()
        )
    return context


def retained_sdk_command_context(
    *,
    platform,
    workspace_root,
    argv,
    cwd,
    environment,
    substitutions,
    plan,
    manifest,
    output_manifest,
    acceptance_policy,
    command_identity,
    phase,
):
    """Capture only the genuine retained launch's bounded, nonsecret projection."""
    _require(
        len(argv) <= 4096 and len(environment) <= 32 and len(substitutions) <= 1024
    )
    context = {
        "schema": CONTEXT_SCHEMA,
        "platform": platform,
        "workspace_root": str(workspace_root),
        "argv": list(argv),
        "cwd": str(cwd),
        "environment": dict(environment),
        "substitutions": dict(substitutions),
    }
    return verify_retained_sdk_command_context(
        context,
        plan=plan,
        manifest=manifest,
        output_manifest=output_manifest,
        acceptance_policy=acceptance_policy,
        command_identity=command_identity,
        phase=phase,
    )
