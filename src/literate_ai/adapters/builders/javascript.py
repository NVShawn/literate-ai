"""Authorization-gated JavaScript bundle builder using an exact Node.js runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from literate_ai.adapters.dependencies import (
    DependencyObservationError,
    NpmDependencyResolver,
    NpmDistributionAuthority,
    NpmLiteralDependencyResolver,
    observe_npm_distribution,
    resolve_windows_npm_cmd_target,
)
from literate_ai.bootstrap.host_install_requirements import version_satisfies
from literate_ai.contracts import SemanticVersion
from literate_ai.security import (
    BuildAuthorization,
    BuildAuthorizationVerifier,
    BuildRequest,
    FailClosedBuildAuthorizationVerifier,
)

from ._process import run_bounded_process as _run_bounded_process
from .python import (
    BuildError,
    _require_source_tree_unchanged,
    canonical_tree_digest,
    executable_file_digest,
    require_unsandboxed_host_build_authorization,
)

DEFAULT_NODE_VERSION_TIMEOUT_SECONDS = 15.0
DEFAULT_JAVASCRIPT_BUILD_TIMEOUT_SECONDS = 60.0
DEFAULT_NODE_STDOUT_LIMIT_BYTES = 1024 * 1024
DEFAULT_NODE_STDERR_LIMIT_BYTES = 1024 * 1024
DEFAULT_NODE_MINIMUM_VERSION = (20, 0, 0)
JAVASCRIPT_SOURCE_SUFFIXES = frozenset({".cjs", ".js", ".mjs"})
_NODE_PROBE_PREFIX = "literate-ai-node-toolchain-v1:"
_NODE_PROBE_SCRIPT = (
    "const record={exec_path:process.execPath,"
    "version:process.version,versions_node:process.versions.node};"
    "process.stdout.write('literate-ai-node-toolchain-v1:'"
    "+JSON.stringify(record));"
)
_NPM_RESOLUTION_PROBE_PREFIX = "literate-ai-npm-resolution-v1:"
_NPM_LITERAL_RESOLUTION_PROBE_PREFIX = "literate-ai-npm-literal-resolution-v1:"
_NPM_RESOLUTION_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_NPM_RESOLUTION_RECORD_LIMIT = 200_000
_NPM_RESOLUTION_PROBE_SCRIPT = r"""
const fs = require('fs');
const path = require('path');
const {builtinModules, createRequire} = require('module');
const root = fs.realpathSync(process.argv[1]);
const pending = [root];
const seen = new Set();
const records = [];
function packageRoot(entry) {
  let current = fs.statSync(entry).isDirectory() ? entry : path.dirname(entry);
  for (;;) {
    const manifest = path.join(current, 'package.json');
    if (fs.existsSync(manifest)) {
      const document = JSON.parse(fs.readFileSync(manifest, 'utf8'));
      if (typeof document.name === 'string' && document.name) {
        return fs.realpathSync(current);
      }
    }
    const parent = path.dirname(current);
    if (parent === current) throw new Error('resolved package root not found');
    current = parent;
  }
}
while (pending.length) {
  const packagePath = fs.realpathSync(pending.shift());
  if (seen.has(packagePath)) continue;
  seen.add(packagePath);
  const manifest = path.join(packagePath, 'package.json');
  const document = JSON.parse(fs.readFileSync(manifest, 'utf8'));
  const names = [...new Set(
    ['dependencies', 'optionalDependencies', 'peerDependencies']
      .flatMap(field => Object.keys(document[field] || {}))
  )].sort();
  const resolveFromPackage = createRequire(manifest);
  for (const dependency of names) {
    let resolvedRoot = null;
    try {
      let entry;
      try {
        entry = resolveFromPackage.resolve(`${dependency}/package.json`);
      } catch (error) {
        const fallbackCodes = ['ERR_PACKAGE_PATH_NOT_EXPORTED', 'MODULE_NOT_FOUND'];
        if (!error || !fallbackCodes.includes(error.code)) throw error;
        entry = resolveFromPackage.resolve(dependency);
      }
      resolvedRoot = packageRoot(fs.realpathSync(entry));
    } catch (error) {
      if (!error || error.code !== 'MODULE_NOT_FOUND') throw error;
    }
    records.push({
      package_root: packagePath,
      dependency,
      resolved_root: resolvedRoot,
    });
    if (resolvedRoot !== null && !seen.has(resolvedRoot)) pending.push(resolvedRoot);
  }
}
const evidence = {
  builtin_modules: [...new Set(
    builtinModules
      .filter(name => !name.startsWith('node:'))
      .map(name => name.split('/', 1)[0])
  )].sort(),
  records,
};
process.stdout.write('literate-ai-npm-resolution-v1:' + JSON.stringify(evidence));
""".strip()
_NPM_LITERAL_RESOLUTION_PROBE_SCRIPT = r"""
const fs = require('fs');
const path = require('path');
const {createRequire} = require('module');
const requests = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const records = [];
function packageRoot(entry) {
  let current = fs.statSync(entry).isDirectory() ? entry : path.dirname(entry);
  for (;;) {
    const manifest = path.join(current, 'package.json');
    if (fs.existsSync(manifest)) {
      const document = JSON.parse(fs.readFileSync(manifest, 'utf8'));
      if (typeof document.name === 'string' && document.name) {
        return fs.realpathSync(current);
      }
    }
    const parent = path.dirname(current);
    if (parent === current) throw new Error('resolved package root not found');
    current = parent;
  }
}
for (const request of requests) {
  const packagePath = fs.realpathSync(request.package_root);
  const resolveFromPackage = createRequire(path.join(packagePath, 'package.json'));
  let resolvedRoot = null;
  try {
    let entry;
    if (request.dependency.startsWith('#')) {
      entry = resolveFromPackage.resolve(request.dependency);
    } else {
      try {
        entry = resolveFromPackage.resolve(`${request.dependency}/package.json`);
      } catch (error) {
        const fallbackCodes = ['ERR_PACKAGE_PATH_NOT_EXPORTED', 'MODULE_NOT_FOUND'];
        if (!error || !fallbackCodes.includes(error.code)) throw error;
        entry = resolveFromPackage.resolve(request.dependency);
      }
    }
    resolvedRoot = packageRoot(fs.realpathSync(entry));
  } catch (error) {
    if (!error || error.code !== 'MODULE_NOT_FOUND') throw error;
  }
  records.push({
    package_root: packagePath,
    dependency: request.dependency,
    resolved_root: resolvedRoot,
  });
}
process.stdout.write(
  'literate-ai-npm-literal-resolution-v1:' + JSON.stringify(records)
);
""".strip()
_NPM_EXECUTION_GUARD_BODY = r"""
const fs = require('fs');
const path = require('path');
const Module = require('module');
const {builtinModules} = Module;
const cli = fs.realpathSync(process.argv[1]);
if (cli !== authority.cli_path) {
  throw new Error('npm CLI differs from its bound execution authority');
}
const npmRoot = fs.realpathSync(authority.npm_root);
if (npmRoot !== authority.npm_root) {
  throw new Error('npm package root differs from its bound execution authority');
}
const roots = authority.package_roots.map(root => {
  const exact = fs.realpathSync(root);
  if (exact !== root) {
    throw new Error('npm dependency root differs from its bound authority');
  }
  return exact;
});
roots.sort((left, right) => right.length - left.length);
const builtins = new Set(builtinModules);
const originalResolveFilename = Module._resolveFilename;
Module._resolveFilename = function(request, parent, isMain, options) {
  const resolved = originalResolveFilename.call(this, request, parent, isMain, options);
  if (builtins.has(request) || request.startsWith('node:')) return resolved;
  if (typeof resolved !== 'string' || !path.isAbsolute(resolved)) {
    throw new Error(`npm module resolution escaped authority: ${request}`);
  }
  const exact = fs.realpathSync(resolved);
  for (const root of roots) {
    const relative = path.relative(root, exact);
    const contained = relative === '' || (
      !relative.startsWith(`..${path.sep}`) &&
      relative !== '..' &&
      !path.isAbsolute(relative)
    );
    if (!contained) continue;
    const crossesPackageBoundary = relative
      .split(path.sep)
      .some(segment => segment.toLowerCase() === 'node_modules');
    if (!crossesPackageBoundary) {
      return resolved;
    }
  }
  throw new Error(`npm module resolution escaped authority: ${request}`);
};
require(cli);
""".strip()


def _npm_execution_guard_script(
    cli_path: str, npm_root: str, package_roots: tuple[str, ...]
) -> str:
    authority = json.dumps(
        {
            "cli_path": cli_path,
            "npm_root": npm_root,
            "package_roots": list(package_roots),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"const authority={authority};\n{_NPM_EXECUTION_GUARD_BODY}"


_SAFE_NODE_COMMAND_ARGUMENTS = frozenset(
    {
        "--no-deprecation",
        "--no-warnings",
        "--pending-deprecation",
        "--trace-deprecation",
        "--trace-warnings",
    }
)


def _node_toolchain_identity(
    *,
    command: tuple[str, ...],
    launcher_executable: str,
    launcher_digest: str,
    runtime_executable: str,
    runtime_digest: str,
    version: str,
) -> str:
    encoded = json.dumps(
        {
            "command": list(command),
            "launcher_digest": launcher_digest,
            "launcher_executable": launcher_executable,
            "runtime_digest": runtime_digest,
            "runtime_executable": runtime_executable,
            "version": version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class NodeToolchain:
    """One Node command with exact invocation, launcher, runtime, and version.

    A launcher may be a stable wrapper that selects another Node binary. The invocation
    path and resolved launcher bytes are therefore insufficient: discovery also binds
    the executable reported by ``process.execPath`` and its bytes. Both layers are
    re-probed around build and execution subprocesses. These temporal checks expose
    ordinary drift but cannot make filesystem lookup and process creation atomic.
    """

    command: tuple[str, ...]
    launcher_executable: str
    launcher_digest: str
    runtime_executable: str
    runtime_digest: str
    version: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or any(not isinstance(item, str) or not item for item in self.command)
            or any(
                not isinstance(item, str) or not item
                for item in (
                    self.launcher_executable,
                    self.runtime_executable,
                    self.version,
                )
            )
        ):
            raise ValueError("Node.js toolchain must identify a versioned command")
        if not self.version.startswith("v"):
            raise ValueError("Node.js toolchain version must be canonical SemVer")
        try:
            parsed_version = SemanticVersion.parse(self.version[1:])
        except ValueError as exc:
            raise ValueError(
                "Node.js toolchain version must be canonical SemVer"
            ) from exc
        if f"v{parsed_version}" != self.version:
            raise ValueError("Node.js toolchain version must be canonical SemVer")
        _require_safe_node_command_arguments(self.command[1:], error_type=ValueError)
        invocation = Path(self.command[0])
        if not invocation.is_absolute():
            raise ValueError("Node.js invocation must be an absolute path")
        try:
            resolved_launcher = invocation.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Node.js launcher must be available") from exc
        if (
            not Path(self.launcher_executable).is_absolute()
            or str(resolved_launcher) != self.launcher_executable
            or not resolved_launcher.is_file()
        ):
            raise ValueError(
                "Node.js invocation must resolve to the selected regular launcher"
            )
        try:
            runtime = Path(self.runtime_executable).resolve(strict=True)
        except OSError as exc:
            raise ValueError("Node.js runtime must be available") from exc
        if str(runtime) != self.runtime_executable or not runtime.is_file():
            raise ValueError("Node.js runtime must be one resolved regular file")
        _require_sha256(self.launcher_digest, "Node.js launcher identity")
        _require_sha256(self.runtime_digest, "Node.js runtime identity")
        object.__setattr__(
            self,
            "identity",
            _node_toolchain_identity(
                command=self.command,
                launcher_executable=self.launcher_executable,
                launcher_digest=self.launcher_digest,
                runtime_executable=self.runtime_executable,
                runtime_digest=self.runtime_digest,
                version=self.version,
            ),
        )

    @property
    def executable_digest(self) -> str:
        """Compatibility alias for the exact resolved launcher bytes."""

        return self.launcher_digest

    def _require_launcher_unchanged(self) -> None:
        invocation = Path(self.command[0])
        try:
            launcher = invocation.resolve(strict=True)
        except OSError as exc:
            raise BuildError(
                "builder.node_toolchain_changed",
                "Node.js became unavailable after toolchain selection",
            ) from exc
        if str(launcher) != self.launcher_executable or not launcher.is_file():
            raise BuildError(
                "builder.node_toolchain_changed",
                "Node.js executable path changed after toolchain selection",
            )
        if executable_file_digest(launcher) != self.launcher_digest:
            raise BuildError(
                "builder.node_toolchain_changed",
                "Node.js executable bytes changed after toolchain selection",
            )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_NODE_VERSION_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_NODE_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_NODE_STDERR_LIMIT_BYTES,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        self._require_launcher_unchanged()
        try:
            probe = _probe_node_command(
                self.command,
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
        except BuildError as exc:
            raise BuildError(
                "builder.node_toolchain_changed",
                "Node.js runtime could not be revalidated after toolchain selection",
            ) from exc
        self._require_launcher_unchanged()
        current = (
            probe.runtime_executable,
            probe.runtime_digest,
            probe.version,
        )
        expected = (
            self.runtime_executable,
            self.runtime_digest,
            self.version,
        )
        if current != expected:
            raise BuildError(
                "builder.node_toolchain_changed",
                "Node.js runtime identity changed after toolchain selection",
            )


@dataclass(frozen=True, slots=True)
class NpmToolchain:
    """Exact npm implementation executed by the selected Node.js runtime."""

    node: NodeToolchain
    cli_path: str
    cli_digest: str
    package_root: str
    package_roots: tuple[str, ...]
    distribution_identity: str
    version: str
    version_constraint: str
    constraint_identity: str
    identity: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.node, NodeToolchain):
            raise TypeError("npm toolchain requires the selected Node.js toolchain")
        try:
            cli = Path(self.cli_path).resolve(strict=True)
        except OSError as exc:
            raise ValueError("npm CLI must be available") from exc
        if (
            not Path(self.cli_path).is_absolute()
            or str(cli) != self.cli_path
            or not cli.is_file()
            or cli.name != "npm-cli.js"
        ):
            raise ValueError("npm CLI must identify one resolved npm-cli.js file")
        _require_sha256(self.cli_digest, "npm CLI identity")
        try:
            package_root = Path(self.package_root).resolve(strict=True)
        except OSError as exc:
            raise ValueError("npm package root must be available") from exc
        if (
            not Path(self.package_root).is_absolute()
            or str(package_root) != self.package_root
            or not package_root.is_dir()
            or package_root != cli.parent.parent
        ):
            raise ValueError("npm package root must contain the resolved npm CLI")
        if self.package_roots != tuple(sorted(set(self.package_roots))):
            raise ValueError("npm package roots must be sorted and unique")
        if self.package_root not in self.package_roots:
            raise ValueError("npm package roots must include the npm package root")
        for root in self.package_roots:
            try:
                resolved_root = Path(root).resolve(strict=True)
            except OSError as exc:
                raise ValueError("npm package roots must be available") from exc
            if not Path(root).is_absolute() or str(resolved_root) != root:
                raise ValueError("npm package roots must be resolved absolute paths")
        _require_sha256(self.distribution_identity, "npm distribution identity")
        try:
            parsed = SemanticVersion.parse(self.version)
        except ValueError as exc:
            raise ValueError("npm version must be canonical SemVer") from exc
        if str(parsed) != self.version:
            raise ValueError("npm version must be canonical SemVer")
        _require_sha256(self.constraint_identity, "npm constraint identity")
        if not self.version_constraint or not version_satisfies(
            self.version, self.version_constraint
        ):
            raise ValueError(
                "npm version must satisfy the selected Flavor constraint "
                f"{self.version_constraint}"
            )
        encoded = json.dumps(
            {
                "cli_digest": self.cli_digest,
                "cli_path": self.cli_path,
                "command": list(self.command),
                "constraint_identity": self.constraint_identity,
                "distribution_identity": self.distribution_identity,
                "node_toolchain_identity": self.node.identity,
                "package_root": self.package_root,
                "package_roots": list(self.package_roots),
                "version": self.version,
                "version_constraint": self.version_constraint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(
            self, "identity", f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        )

    @property
    def command(self) -> tuple[str, ...]:
        guard = _npm_execution_guard_script(
            self.cli_path, self.package_root, self.package_roots
        )
        return (
            self.node.runtime_executable,
            "--input-type=commonjs",
            "--eval",
            guard,
            "--",
            self.cli_path,
        )

    def _require_distribution_unchanged(
        self,
        environment: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> None:
        try:
            observed = _observe_npm_distribution_with_node(
                self.node,
                Path(self.cli_path),
                environment,
                timeout_seconds=timeout_seconds,
            )
        except (BuildError, DependencyObservationError, OSError) as exc:
            raise BuildError(
                "builder.npm_toolchain_changed",
                "npm distribution became unsafe after toolchain selection",
            ) from exc
        if (
            observed.package_root != self.package_root
            or observed.package_roots != self.package_roots
            or observed.version != self.version
            or observed.identity.uri != self.distribution_identity
        ):
            raise BuildError(
                "builder.npm_toolchain_changed",
                "npm distribution identity changed after toolchain selection",
            )

    def require_unchanged(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_NODE_VERSION_TIMEOUT_SECONDS,
    ) -> None:
        configured = dict(os.environ if environment is None else environment)
        self.node.require_unchanged(configured, timeout_seconds=timeout_seconds)
        self._require_distribution_unchanged(
            configured, timeout_seconds=timeout_seconds
        )
        current = _probe_npm_version(
            self.node,
            Path(self.cli_path),
            configured,
            package_root=self.package_root,
            package_roots=self.package_roots,
            timeout_seconds=timeout_seconds,
        )
        self._require_distribution_unchanged(
            configured, timeout_seconds=timeout_seconds
        )
        self.node.require_unchanged(configured, timeout_seconds=timeout_seconds)
        if current != self.version:
            raise BuildError(
                "builder.npm_toolchain_changed",
                "npm version changed after toolchain selection",
            )


def _require_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a sha256 digest")
    encoded = value.removeprefix("sha256:")
    if len(encoded) != 64 or encoded != encoded.casefold():
        raise ValueError(f"{label} must be a sha256 digest")
    try:
        int(encoded, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a sha256 digest") from exc


def _require_safe_node_command_arguments(
    arguments: Sequence[str], *, error_type: type[ValueError] | None = None
) -> None:
    """Reject command prefixes that can execute or load mutable external code.

    The command is later extended with framework-owned ``--eval`` and ``--check``
    operands. Only a deliberately small set of diagnostic flags is accepted before
    those operands. File-loading flags such as ``--require``, ``--import``, loaders,
    eval/print/check operands, and unknown future flags therefore fail closed instead
    of becoming unbound toolchain inputs.
    """

    if all(argument in _SAFE_NODE_COMMAND_ARGUMENTS for argument in arguments):
        return
    message = "Node.js command arguments are not in the audited non-loading allowlist"
    if error_type is ValueError:
        raise ValueError(message)
    raise BuildError("builder.node_command_arguments_unsafe", message)


def _parse_node_command(raw: str, *, label: str) -> tuple[str, ...]:
    try:
        parsed = tuple(shlex.split(raw, posix=os.name != "nt"))
    except ValueError as exc:
        raise BuildError(
            "builder.node_toolchain", f"{label} is not a valid command"
        ) from exc
    if os.name == "nt" and parsed:
        executable = parsed[0]
        if (
            len(executable) >= 2
            and executable[0] == executable[-1]
            and executable[0] in {'"', "'"}
        ):
            parsed = (executable[1:-1], *parsed[1:])
    if not parsed:
        raise BuildError("builder.node_toolchain", f"{label} is empty")
    return parsed


def _explicit_node_command(
    pinned_command: str | Sequence[str] | None,
    environment: Mapping[str, str],
) -> tuple[str, ...] | None:
    if pinned_command is not None:
        if isinstance(pinned_command, str):
            return _parse_node_command(pinned_command, label="pinned Node.js command")
        command = tuple(pinned_command)
        if not command or any(
            not isinstance(item, str) or not item for item in command
        ):
            raise BuildError(
                "builder.node_toolchain", "pinned Node.js command is invalid"
            )
        return command
    raw = environment.get("NODE", "").strip()
    return _parse_node_command(raw, label="NODE") if raw else None


def _node_candidates(
    environment: Mapping[str, str], explicit: tuple[str, ...] | None
) -> Iterator[tuple[str, ...]]:
    if explicit is not None:
        yield explicit
        return
    path_value = environment.get("PATH", os.defpath)
    seen: set[str] = set()
    for directory in path_value.split(os.pathsep):
        search_directory = directory or os.curdir
        for name in ("node", "nodejs"):
            try:
                found = shutil.which(name, path=search_directory)
            except (OSError, ValueError):
                continue
            if found is None:
                continue
            invocation = str(Path(os.path.abspath(found)))
            key = os.path.normcase(invocation)
            if key in seen:
                continue
            seen.add(key)
            yield (invocation,)


def _resolved_node_command(
    candidate: tuple[str, ...], environment: Mapping[str, str]
) -> tuple[tuple[str, ...], Path]:
    _require_safe_node_command_arguments(candidate[1:])
    try:
        found = shutil.which(candidate[0], path=environment.get("PATH", os.defpath))
    except (OSError, ValueError) as exc:
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js command could not be resolved",
        ) from exc
    if found is None:
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js command was not found on PATH",
        )
    invocation = Path(os.path.abspath(found))
    try:
        launcher = invocation.resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js launcher became unavailable during discovery",
        ) from exc
    if not launcher.is_file():
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js launcher is not a regular file",
        )
    return (str(invocation), *candidate[1:]), launcher


@dataclass(frozen=True, slots=True)
class _NodeProbe:
    runtime_executable: str
    runtime_digest: str
    version: str
    parsed_version: SemanticVersion


def _probe_node_command(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> _NodeProbe:
    completed = _run_bounded_process(
        [*command, "--input-type=commonjs", "--eval", _NODE_PROBE_SCRIPT],
        cwd=None,
        environment=controlled_node_environment(dict(environment)),
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=stdout_limit_bytes,
        stderr_limit_bytes=stderr_limit_bytes,
        error_prefix="builder.node_version",
    )
    if completed.returncode != 0 or completed.stderr:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate did not complete the isolated runtime probe",
        )
    try:
        output = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported a malformed runtime identity",
        ) from exc
    if output.endswith("\r\n"):
        output = output[:-2]
    elif output.endswith("\n"):
        output = output[:-1]
    if not output.startswith(_NODE_PROBE_PREFIX) or "\r" in output or "\n" in output:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported a malformed runtime identity",
        )
    try:
        raw = json.loads(output.removeprefix(_NODE_PROBE_PREFIX))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported an invalid runtime identity",
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {
        "exec_path",
        "version",
        "versions_node",
    }:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported an incomplete runtime identity",
        )
    if any(not isinstance(value, str) or not value for value in raw.values()):
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported empty runtime identity fields",
        )
    version = raw["version"]
    versions_node = raw["versions_node"]
    if not version.startswith("v") or version[1:] != versions_node:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported inconsistent runtime versions",
        )
    try:
        parsed = SemanticVersion.parse(versions_node)
    except ValueError as exc:
        raise BuildError(
            "builder.node_version_failed",
            "Node.js candidate reported a malformed semantic version",
        ) from exc
    try:
        runtime = Path(raw["exec_path"]).resolve(strict=True)
    except OSError as exc:
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js candidate's runtime executable is unavailable",
        ) from exc
    if not runtime.is_file():
        raise BuildError(
            "builder.node_toolchain_unavailable",
            "Node.js candidate's runtime executable is not a regular file",
        )
    return _NodeProbe(
        runtime_executable=str(runtime),
        runtime_digest=executable_file_digest(runtime),
        version=version,
        parsed_version=parsed,
    )


def _validated_node_version_prefix(
    value: tuple[int, ...], *, label: str
) -> tuple[int, ...]:
    if (
        not value
        or len(value) > 3
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise ValueError(
            f"{label} Node.js version must be a one-to-three-part version prefix"
        )
    return value


def controlled_node_environment(environment: dict[str, str]) -> dict[str, str]:
    """Remove ambient Node state and injection while retaining launch necessities."""

    blocked = {
        "node_compile_cache",
        "node_disable_compile_cache",
        "node_options",
        "node_path",
    }
    controlled = {
        name: value
        for name, value in environment.items()
        if name.casefold() not in blocked
    }
    controlled["NODE_DISABLE_COMPILE_CACHE"] = "1"
    return controlled


def discover_node_toolchain(
    environment: Mapping[str, str] | None = None,
    *,
    pinned_command: str | Sequence[str] | None = None,
    minimum_version: tuple[int, ...] = DEFAULT_NODE_MINIMUM_VERSION,
    required_version: tuple[int, ...] | None = None,
    timeout_seconds: float = DEFAULT_NODE_VERSION_TIMEOUT_SECONDS,
    stdout_limit_bytes: int = DEFAULT_NODE_STDOUT_LIMIT_BYTES,
    stderr_limit_bytes: int = DEFAULT_NODE_STDERR_LIMIT_BYTES,
) -> NodeToolchain:
    """Select an exact supported Node.js command from an explicit pin or ``PATH``.

    ``pinned_command`` is authoritative when supplied, followed by a non-empty
    operator-provided ``NODE`` command. Without either pin, candidates are probed in
    ``PATH`` directory order, trying ``node`` and then ``nodejs`` in each directory.
    Invalid unpinned candidates are skipped; an invalid explicit command fails closed.
    The invocation path, resolved launcher bytes, arguments, and exact SemVer output
    are all bound into the selected toolchain identity.
    """

    configured = dict(os.environ if environment is None else environment)
    explicit = _explicit_node_command(pinned_command, configured)
    minimum = _validated_node_version_prefix(minimum_version, label="minimum")
    required = (
        _validated_node_version_prefix(required_version, label="required")
        if required_version is not None
        else None
    )
    minimum_core = minimum + (0,) * (3 - len(minimum))
    minimum_semver = SemanticVersion(minimum_core[0], minimum_core[1], minimum_core[2])
    last_error: BuildError | None = None
    for candidate in _node_candidates(configured, explicit):
        try:
            command, launcher = _resolved_node_command(candidate, configured)
            launcher_digest = executable_file_digest(launcher)
            probe = _probe_node_command(
                command,
                configured,
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
            )
            if probe.parsed_version < minimum_semver:
                raise BuildError(
                    "builder.node_version_unsupported",
                    "Node.js candidate is older than the required minimum version",
                )
            parsed_core = (
                probe.parsed_version.major,
                probe.parsed_version.minor,
                probe.parsed_version.patch,
            )
            if required is not None and parsed_core[: len(required)] != required:
                raise BuildError(
                    "builder.node_version_unsupported",
                    "Node.js candidate does not match the required version",
                )
            try:
                current_launcher = Path(command[0]).resolve(strict=True)
            except OSError as exc:
                raise BuildError(
                    "builder.node_toolchain_changed",
                    "Node.js launcher became unavailable during discovery",
                ) from exc
            if (
                current_launcher != launcher
                or executable_file_digest(current_launcher) != launcher_digest
            ):
                raise BuildError(
                    "builder.node_toolchain_changed",
                    "Node.js launcher changed during toolchain discovery",
                )
            try:
                current_runtime = Path(probe.runtime_executable).resolve(strict=True)
            except OSError as exc:
                raise BuildError(
                    "builder.node_toolchain_changed",
                    "Node.js runtime became unavailable during toolchain discovery",
                ) from exc
            if (
                str(current_runtime) != probe.runtime_executable
                or not current_runtime.is_file()
                or executable_file_digest(current_runtime) != probe.runtime_digest
            ):
                raise BuildError(
                    "builder.node_toolchain_changed",
                    "Node.js runtime changed during toolchain discovery",
                )
            return NodeToolchain(
                command=command,
                launcher_executable=str(launcher),
                launcher_digest=launcher_digest,
                runtime_executable=probe.runtime_executable,
                runtime_digest=probe.runtime_digest,
                version=probe.version,
            )
        except BuildError as exc:
            if explicit is not None:
                raise
            last_error = exc
    error = BuildError(
        "builder.node_toolchain_unavailable",
        "no supported Node.js runtime was found through NODE or ordered PATH",
    )
    if last_error is not None:
        raise error from last_error
    raise error


def _npm_cli_candidates(
    node: NodeToolchain, environment: Mapping[str, str]
) -> Iterator[Path]:
    """Yield resolved npm implementations from PATH and common Node layouts."""

    seen: set[str] = set()
    launcher_names = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for launcher_name in launcher_names:
        try:
            launcher = shutil.which(
                launcher_name, path=environment.get("PATH", os.defpath)
            )
        except (OSError, ValueError):
            launcher = None
        if launcher is None:
            continue
        try:
            launcher_path = Path(launcher)
            resolved = (
                resolve_windows_npm_cmd_target(launcher_path)
                if launcher_path.suffix.casefold() == ".cmd"
                else launcher_path.resolve(strict=True)
            )
        except (DependencyObservationError, OSError):
            continue
        key = os.path.normcase(str(resolved))
        if resolved.is_file() and resolved.name == "npm-cli.js" and key not in seen:
            seen.add(key)
            yield resolved
    roots = {
        Path(node.command[0]).resolve().parent,
        Path(node.runtime_executable).resolve().parent,
    }
    for root in sorted(roots, key=str):
        for candidate in (
            root / "node_modules" / "npm" / "bin" / "npm-cli.js",
            root.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        ):
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if key not in seen and resolved.is_file() and resolved.name == "npm-cli.js":
                seen.add(key)
                yield resolved


def _selected_node_npm_dependency_resolver(
    node: NodeToolchain,
    package_root: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> tuple[NpmDependencyResolver, frozenset[str]]:
    """Observe package resolution exactly as the selected Node runtime performs it."""

    completed = _run_bounded_process(
        [
            node.runtime_executable,
            "--eval",
            _NPM_RESOLUTION_PROBE_SCRIPT,
            str(package_root),
        ],
        cwd=None,
        environment=controlled_node_environment(dict(environment)),
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=_NPM_RESOLUTION_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes=4096,
        error_prefix="builder.npm_dependency_resolution",
    )
    if completed.returncode != 0 or completed.stderr:
        raise BuildError(
            "builder.npm_dependency_resolution_failed",
            "selected Node did not complete the isolated npm dependency probe",
        )
    try:
        encoded = completed.stdout.decode("utf-8")
        if not encoded.startswith(_NPM_RESOLUTION_PROBE_PREFIX):
            raise ValueError("npm dependency probe output prefix is missing")
        evidence = json.loads(encoded.removeprefix(_NPM_RESOLUTION_PROBE_PREFIX))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise BuildError(
            "builder.npm_dependency_resolution_failed",
            "selected Node reported malformed npm dependency resolution evidence",
        ) from exc
    if not isinstance(evidence, dict) or set(evidence) != {
        "builtin_modules",
        "records",
    }:
        raise BuildError(
            "builder.npm_dependency_resolution_failed",
            "selected Node reported an invalid npm dependency resolution graph",
        )
    records = evidence["records"]
    raw_builtin_modules = evidence["builtin_modules"]
    if (
        not isinstance(records, list)
        or len(records) > _NPM_RESOLUTION_RECORD_LIMIT
        or not isinstance(raw_builtin_modules, list)
        or not raw_builtin_modules
        or any(not isinstance(item, str) or not item for item in raw_builtin_modules)
    ):
        raise BuildError(
            "builder.npm_dependency_resolution_failed",
            "selected Node reported invalid npm dependency resolution facts",
        )
    builtin_modules = frozenset(raw_builtin_modules)
    if len(builtin_modules) != len(raw_builtin_modules):
        raise BuildError(
            "builder.npm_dependency_resolution_failed",
            "selected Node reported duplicate builtin module evidence",
        )

    resolutions: dict[tuple[Path, str], Path | None] = {}
    for raw in records:
        if not isinstance(raw, dict) or set(raw) != {
            "package_root",
            "dependency",
            "resolved_root",
        }:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node reported an invalid npm dependency resolution record",
            )
        raw_source = raw["package_root"]
        dependency = raw["dependency"]
        raw_target = raw["resolved_root"]
        if (
            not isinstance(raw_source, str)
            or not Path(raw_source).is_absolute()
            or not isinstance(dependency, str)
            or not dependency
            or (raw_target is not None and not isinstance(raw_target, str))
            or (isinstance(raw_target, str) and not Path(raw_target).is_absolute())
        ):
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node reported an unsafe npm dependency resolution record",
            )
        try:
            source = Path(raw_source).resolve(strict=True)
            target = (
                None if raw_target is None else Path(raw_target).resolve(strict=True)
            )
        except OSError as exc:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node resolved an unavailable npm dependency package",
            ) from exc
        key = (source, dependency)
        if key in resolutions:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node reported duplicate npm dependency resolution evidence",
            )
        resolutions[key] = target

    def resolve(package: Path, dependency: str) -> Path | None:
        try:
            key = (package.resolve(strict=True), dependency)
        except OSError as exc:
            raise DependencyObservationError(
                "dependencies.npm-dependency-path-unsafe",
                "npm dependency source package became unavailable",
            ) from exc
        if key not in resolutions:
            raise DependencyObservationError(
                "dependencies.npm-dependency-resolution-incomplete",
                "selected Node did not report one declared npm dependency",
            )
        return resolutions[key]

    return resolve, builtin_modules


def _selected_node_npm_literal_dependency_resolver(
    node: NodeToolchain,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> NpmLiteralDependencyResolver:
    def resolve(
        requests: tuple[tuple[Path, str], ...],
    ) -> Mapping[tuple[Path, str], Path | None]:
        if not requests:
            return {}
        if len(requests) > _NPM_RESOLUTION_RECORD_LIMIT:
            raise DependencyObservationError(
                "dependencies.npm-literal-resolution-invalid",
                "npm literal dependency requests exceed the resolution limit",
            )
        payload = [
            {"package_root": str(package), "dependency": dependency}
            for package, dependency in requests
        ]
        with tempfile.TemporaryDirectory(prefix="litai-npm-resolution-") as directory:
            request_path = Path(directory) / "requests.json"
            request_path.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            completed = _run_bounded_process(
                [
                    node.runtime_executable,
                    "--eval",
                    _NPM_LITERAL_RESOLUTION_PROBE_SCRIPT,
                    str(request_path),
                ],
                cwd=None,
                environment=controlled_node_environment(dict(environment)),
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=_NPM_RESOLUTION_STDOUT_LIMIT_BYTES,
                stderr_limit_bytes=4096,
                error_prefix="builder.npm_literal_dependency_resolution",
            )
        if completed.returncode != 0 or completed.stderr:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node did not resolve npm literal package imports",
            )
        try:
            encoded = completed.stdout.decode("utf-8")
            if not encoded.startswith(_NPM_LITERAL_RESOLUTION_PROBE_PREFIX):
                raise ValueError("npm literal resolution output prefix is missing")
            raw_records = json.loads(
                encoded.removeprefix(_NPM_LITERAL_RESOLUTION_PROBE_PREFIX)
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node reported malformed npm literal resolution evidence",
            ) from exc
        if not isinstance(raw_records, list) or len(raw_records) != len(requests):
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node reported incomplete npm literal resolution evidence",
            )
        expected = set(requests)
        result: dict[tuple[Path, str], Path | None] = {}
        for raw in raw_records:
            if not isinstance(raw, dict) or set(raw) != {
                "package_root",
                "dependency",
                "resolved_root",
            }:
                raise BuildError(
                    "builder.npm_dependency_resolution_failed",
                    "selected Node reported an invalid npm literal resolution record",
                )
            raw_source = raw["package_root"]
            dependency = raw["dependency"]
            raw_target = raw["resolved_root"]
            if (
                not isinstance(raw_source, str)
                or not Path(raw_source).is_absolute()
                or not isinstance(dependency, str)
                or not dependency
                or (raw_target is not None and not isinstance(raw_target, str))
                or (isinstance(raw_target, str) and not Path(raw_target).is_absolute())
            ):
                raise BuildError(
                    "builder.npm_dependency_resolution_failed",
                    "selected Node reported unsafe npm literal resolution evidence",
                )
            try:
                source = Path(raw_source).resolve(strict=True)
                target = (
                    None
                    if raw_target is None
                    else Path(raw_target).resolve(strict=True)
                )
            except OSError as exc:
                raise BuildError(
                    "builder.npm_dependency_resolution_failed",
                    "selected Node resolved an unavailable npm literal package",
                ) from exc
            key = (source, dependency)
            if key not in expected or key in result:
                raise BuildError(
                    "builder.npm_dependency_resolution_failed",
                    "selected Node reported unexpected npm literal resolution evidence",
                )
            result[key] = target
        if set(result) != expected:
            raise BuildError(
                "builder.npm_dependency_resolution_failed",
                "selected Node omitted npm literal resolution evidence",
            )
        return result

    return resolve


def _observe_npm_distribution_with_node(
    node: NodeToolchain,
    cli: Path,
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
) -> NpmDistributionAuthority:
    package_root = cli.resolve(strict=True).parent.parent
    resolver, builtin_modules = _selected_node_npm_dependency_resolver(
        node,
        package_root,
        environment,
        timeout_seconds=timeout_seconds,
    )
    literal_resolver = _selected_node_npm_literal_dependency_resolver(
        node,
        environment,
        timeout_seconds=timeout_seconds,
    )
    return observe_npm_distribution(
        cli,
        dependency_resolver=resolver,
        literal_dependency_resolver=literal_resolver,
        builtin_modules=builtin_modules,
    )


def _probe_npm_version(
    node: NodeToolchain,
    cli: Path,
    environment: Mapping[str, str],
    *,
    package_root: str,
    package_roots: tuple[str, ...],
    timeout_seconds: float,
) -> str:
    guard = _npm_execution_guard_script(str(cli), package_root, package_roots)
    completed = _run_bounded_process(
        [
            node.runtime_executable,
            "--input-type=commonjs",
            "--eval",
            guard,
            "--",
            str(cli),
            "--version",
        ],
        cwd=None,
        environment=controlled_node_environment(dict(environment)),
        timeout_seconds=timeout_seconds,
        stdout_limit_bytes=4096,
        stderr_limit_bytes=4096,
        error_prefix="builder.npm_version",
    )
    if completed.returncode != 0 or completed.stderr:
        raise BuildError(
            "builder.npm_version_failed",
            "npm did not complete its isolated version probe",
        )
    try:
        version = completed.stdout.decode("utf-8").strip()
        parsed = SemanticVersion.parse(version)
    except (UnicodeError, ValueError) as exc:
        raise BuildError(
            "builder.npm_version_failed", "npm reported a malformed semantic version"
        ) from exc
    if str(parsed) != version:
        raise BuildError(
            "builder.npm_version_failed", "npm reported a noncanonical semantic version"
        )
    return version


def discover_npm_toolchain(
    node: NodeToolchain,
    environment: Mapping[str, str] | None = None,
    *,
    version_constraint: str,
    constraint_identity: str,
    timeout_seconds: float = DEFAULT_NODE_VERSION_TIMEOUT_SECONDS,
) -> NpmToolchain:
    """Find and bind exact npm implementation bytes for the selected Node runtime."""

    configured = dict(os.environ if environment is None else environment)
    for cli in _npm_cli_candidates(node, configured):
        digest = executable_file_digest(cli)
        try:
            before = _observe_npm_distribution_with_node(
                node, cli, configured, timeout_seconds=timeout_seconds
            )
            version = _probe_npm_version(
                node,
                cli,
                configured,
                package_root=before.package_root,
                package_roots=before.package_roots,
                timeout_seconds=timeout_seconds,
            )
            after = _observe_npm_distribution_with_node(
                node, cli, configured, timeout_seconds=timeout_seconds
            )
            if before.version != version:
                raise BuildError(
                    "builder.npm_version_failed",
                    "npm package version differs from npm --version",
                )
            selected = NpmToolchain(
                node=node,
                cli_path=str(cli),
                cli_digest=digest,
                package_root=before.package_root,
                package_roots=before.package_roots,
                distribution_identity=before.identity.uri,
                version=version,
                version_constraint=version_constraint,
                constraint_identity=constraint_identity,
            )
        except (BuildError, DependencyObservationError, ValueError):
            continue
        if before != after:
            raise BuildError(
                "builder.npm_toolchain_changed",
                "npm distribution changed during toolchain discovery",
            )
        if executable_file_digest(cli) != digest or selected.cli_digest != digest:
            raise BuildError(
                "builder.npm_toolchain_changed",
                "npm CLI changed during toolchain discovery",
            )
        return selected
    raise BuildError(
        "builder.npm_toolchain_unavailable",
        "a package-npm build requires an exact supported npm implementation "
        "executable by the selected Node runtime "
        f"({version_constraint})",
    )


@dataclass(frozen=True, slots=True)
class JavaScriptBuildArtifact:
    artifact_digest: str
    artifact_path: Path
    source_bundle_digest: str
    authorization_id: str
    entrypoint_file: str
    checked_files: tuple[str, ...]
    runtime_command: tuple[str, ...]
    toolchain_identity: str


class GuardedJavaScriptBuilder:
    """Syntax-check and seal generated JavaScript after exact authorization."""

    builder_id = "builder:javascript-node-check@1"
    entrypoint_file = "source/main.js"

    def __init__(
        self,
        toolchain: NodeToolchain,
        authorization_verifier: BuildAuthorizationVerifier | None = None,
        *,
        timeout_seconds: float = DEFAULT_JAVASCRIPT_BUILD_TIMEOUT_SECONDS,
        stdout_limit_bytes: int = DEFAULT_NODE_STDOUT_LIMIT_BYTES,
        stderr_limit_bytes: int = DEFAULT_NODE_STDERR_LIMIT_BYTES,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("JavaScript build timeout must be positive")
        if stdout_limit_bytes <= 0 or stderr_limit_bytes <= 0:
            raise ValueError("Node.js output limits must be positive")
        self.toolchain = toolchain
        self.authorization_verifier = (
            authorization_verifier or FailClosedBuildAuthorizationVerifier()
        )
        self.timeout_seconds = timeout_seconds
        self.stdout_limit_bytes = stdout_limit_bytes
        self.stderr_limit_bytes = stderr_limit_bytes

    def build(
        self,
        request: BuildRequest,
        authorization: BuildAuthorization,
        *,
        source_root: Path,
        artifact_store: Path,
        now: datetime,
    ) -> JavaScriptBuildArtifact:
        self.authorization_verifier.require_build_valid(authorization, request, now=now)
        require_unsandboxed_host_build_authorization(request, authorization)
        if request.builder_id != self.builder_id:
            raise BuildError(
                "builder.identity_mismatch", "Build request selected another builder"
            )
        if "javascript-checked-bundle" not in request.allowed_outputs:
            raise BuildError(
                "builder.output_not_authorized",
                "Checked JavaScript bundle output is not authorized",
            )
        if request.toolchain_digest != self.toolchain.identity:
            raise BuildError(
                "builder.toolchain_mismatch",
                "Build request selected another Node.js toolchain",
            )
        environment = controlled_node_environment(dict(os.environ))
        self.toolchain.require_unchanged(environment)
        resolved_source = source_root.resolve(strict=True)
        actual_source_digest = canonical_tree_digest(resolved_source)
        if actual_source_digest != request.source_bundle_digest:
            raise BuildError(
                "builder.source_digest_mismatch",
                "Source tree changed after authorization",
            )

        scripts = tuple(
            sorted(
                path
                for path in resolved_source.rglob("*")
                if path.is_file()
                and path.suffix.casefold() in JAVASCRIPT_SOURCE_SUFFIXES
            )
        )
        entrypoint = resolved_source.joinpath(*Path(self.entrypoint_file).parts)
        if not entrypoint.is_file() or entrypoint.is_symlink():
            raise BuildError(
                "builder.javascript_entrypoint_missing",
                f"Generated JavaScript source must contain {self.entrypoint_file}",
            )
        if not scripts:
            raise BuildError(
                "builder.javascript_entrypoint_missing",
                "Generated JavaScript source has no scripts",
            )
        relative_scripts = tuple(
            path.relative_to(resolved_source).as_posix() for path in scripts
        )

        destination_root = artifact_store.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix="javascript-build-", dir=destination_root)
        )
        try:
            for relative in relative_scripts:
                self.toolchain.require_unchanged(environment)
                try:
                    completed = _run_bounded_process(
                        [*self.toolchain.command, "--check", relative],
                        cwd=resolved_source,
                        environment=environment,
                        timeout_seconds=self.timeout_seconds,
                        stdout_limit_bytes=self.stdout_limit_bytes,
                        stderr_limit_bytes=self.stderr_limit_bytes,
                        error_prefix="builder.javascript_check",
                    )
                finally:
                    self.toolchain.require_unchanged(environment)
                if completed.returncode != 0:
                    detail = (completed.stdout + completed.stderr)[-4000:].decode(
                        "utf-8", errors="replace"
                    )
                    raise BuildError(
                        "builder.javascript_check_failed",
                        "JavaScript syntax check failed: " + detail,
                    )
            _require_source_tree_unchanged(resolved_source, actual_source_digest)

            file_records: list[dict[str, str]] = []
            for relative in relative_scripts:
                source = resolved_source / relative
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
                file_records.append(
                    {
                        "path": relative,
                        "digest": (
                            "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
                        ),
                    }
                )
            _require_source_tree_unchanged(resolved_source, actual_source_digest)
            try:
                self.toolchain.require_unchanged(environment)
            finally:
                _require_source_tree_unchanged(resolved_source, actual_source_digest)
            manifest = {
                "builder_id": self.builder_id,
                "source_bundle_digest": actual_source_digest,
                "authorization_id": authorization.authorization_id,
                "toolchain_identity": self.toolchain.identity,
                "runtime_command": list(self.toolchain.command),
                "entrypoint_file": self.entrypoint_file,
                "checked_files": list(relative_scripts),
                "files": file_records,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            artifact_digest = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
            (staging / "build-manifest.json").write_bytes(manifest_bytes)
            if not _cached_javascript_artifact_matches(
                staging, manifest=manifest, manifest_bytes=manifest_bytes
            ):
                raise BuildError(
                    "builder.artifact_invalid",
                    "Built artifact differs from its exact manifest",
                )
            final = destination_root / artifact_digest.removeprefix("sha256:")
            if final.exists():
                if not _cached_javascript_artifact_matches(
                    final, manifest=manifest, manifest_bytes=manifest_bytes
                ):
                    raise BuildError(
                        "builder.artifact_collision", "Existing artifact differs"
                    )
                shutil.rmtree(staging)
            else:
                os.replace(staging, final)
            return JavaScriptBuildArtifact(
                artifact_digest=artifact_digest,
                artifact_path=final,
                source_bundle_digest=actual_source_digest,
                authorization_id=authorization.authorization_id,
                entrypoint_file=self.entrypoint_file,
                checked_files=relative_scripts,
                runtime_command=self.toolchain.command,
                toolchain_identity=self.toolchain.identity,
            )
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise


class GuardedJavaScriptRoleBuilder(GuardedJavaScriptBuilder):
    """Build one role subtree whose authorization binds ``main.js`` at its root."""

    builder_id = "builder:javascript-node-role-check@1"
    entrypoint_file = "main.js"


def _cached_javascript_artifact_matches(
    root: Path, *, manifest: dict[str, object], manifest_bytes: bytes
) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return False
    expected = {
        str(item["path"]): str(item["digest"])
        for item in raw_files
        if isinstance(item, dict) and set(item) == {"path", "digest"}
    }
    if len(expected) != len(raw_files):
        return False
    actual: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            return False
        if path.is_dir():
            continue
        if not path.is_file():
            return False
        relative = path.relative_to(root).as_posix()
        if relative == "build-manifest.json":
            if path.read_bytes() != manifest_bytes:
                return False
            continue
        actual[relative] = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    return actual == expected and (root / "build-manifest.json").is_file()


__all__ = [
    "DEFAULT_JAVASCRIPT_BUILD_TIMEOUT_SECONDS",
    "DEFAULT_NODE_MINIMUM_VERSION",
    "JAVASCRIPT_SOURCE_SUFFIXES",
    "DEFAULT_NODE_STDERR_LIMIT_BYTES",
    "DEFAULT_NODE_STDOUT_LIMIT_BYTES",
    "DEFAULT_NODE_VERSION_TIMEOUT_SECONDS",
    "GuardedJavaScriptBuilder",
    "GuardedJavaScriptRoleBuilder",
    "JavaScriptBuildArtifact",
    "NpmToolchain",
    "NodeToolchain",
    "controlled_node_environment",
    "discover_node_toolchain",
    "discover_npm_toolchain",
]
