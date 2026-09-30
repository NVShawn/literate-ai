"""Stdio JSON-RPC MCP adapter over in-process litai CLI. No extra MCP dependency."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentParser
from io import StringIO
from pathlib import Path
from typing import Any

from literate_ai.application.mcp_resources import (
    McpResourceError,
    project_mcp_resources,
    read_project_mcp_resource,
)
from literate_ai.cli.dispatch import main as litai_main
from literate_ai.projects import discover_project
from literate_ai.version import DISTRIBUTION_VERSION

PROTOCOL_VERSION = "2025-03-26"
_TOOLS = (
    {
        "name": "verify",
        "description": "Run litai verify on a project",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "lock",
        "description": "Run litai lock on a Component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "project": {"type": "string", "default": "."},
            },
            "required": ["component"],
        },
    },
    {
        "name": "plan",
        "description": "Run litai plan on a Component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "project": {"type": "string", "default": "."},
            },
            "required": ["component"],
        },
    },
    {
        "name": "rebuild",
        "description": "Run litai rebuild on a Component",
        "inputSchema": {
            "type": "object",
            "properties": {
                "component": {"type": "string"},
                "project": {"type": "string", "default": "."},
                "allow_host_execution": {"type": "boolean", "default": False},
            },
            "required": ["component"],
        },
    },
    {
        "name": "project_validate",
        "description": "Run litai project validate",
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string", "default": "."}},
        },
    },
    {
        "name": "catalog_copy",
        "description": "Run litai catalog copy",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": "."},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["source", "items"],
        },
    },
    {
        "name": "operator_mcp_show",
        "description": "Read the typed operator-local MCP catalog",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "operator_mcp_write",
        "description": "Atomically replace the typed operator-local MCP catalog",
        "inputSchema": {
            "type": "object",
            "properties": {"servers": {"type": "array", "items": {"type": "string"}}},
            "required": ["servers"],
        },
    },
    {
        "name": "work_record",
        "description": "Record one typed active-roadmap work item",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"},
                "title": {"type": "string"},
                "priority": {"type": "string"},
                "owner": {"type": "string"},
                "direction": {"type": "string"},
                "conclusion": {"type": "string"},
                "dependencies": {"type": "array", "items": {"type": "string"}},
                "implementation": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "project": {"type": "string", "default": "."},
            },
            "required": [
                "work_id",
                "title",
                "priority",
                "owner",
                "direction",
                "conclusion",
                "implementation",
                "evidence",
            ],
        },
    },
    {
        "name": "work_close",
        "description": "Close a roadmap item after every checklist is complete",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"},
                "project": {"type": "string", "default": "."},
            },
            "required": ["work_id"],
        },
    },
    {
        "name": "resolve_nvidia_stack",
        "description": "Select an exact NVIDIA stack from retained compatibility data",
        "inputSchema": {
            "type": "object",
            "properties": {
                "compatibility": {"type": "string"},
                "worker_id": {"type": "string"},
                "python_abi": {"type": "string"},
                "observations": {"type": "string"},
                "toolkit": {"type": "string"},
                "packages": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["compatibility", "worker_id", "python_abi"],
        },
    },
    {
        "name": "document_verify",
        "description": "Run the independent deterministic document-pair verifier",
        "inputSchema": {
            "type": "object",
            "properties": {
                "manifest": {"type": "string"},
                "component": {"type": "string"},
            },
            "required": ["manifest", "component"],
        },
    },
)

_MUTATING_TOOLS = frozenset(
    {
        "lock",
        "rebuild",
        "catalog_copy",
        "operator_mcp_write",
        "work_record",
        "work_close",
    }
)
for _tool in _TOOLS:
    _mutating = _tool["name"] in _MUTATING_TOOLS
    _tool["annotations"] = {
        "readOnlyHint": not _mutating,
        "destructiveHint": _mutating,
        "openWorldHint": True,
    }
    _tool["inputSchema"]["additionalProperties"] = False
    if _mutating:
        _tool["inputSchema"]["properties"]["acknowledge"] = {"type": "boolean"}
        _tool["inputSchema"].setdefault("required", []).append("acknowledge")


def _scoped_arguments(
    name: str, arguments: object, root: Path, *, allow_operator_config: bool
) -> dict[str, Any]:
    tool = next((item for item in _TOOLS if item["name"] == name), None)
    if tool is None:
        raise KeyError(name)
    if not isinstance(arguments, dict):
        raise ValueError("mcp.arguments_invalid: arguments must be an object")
    schema = tool["inputSchema"]
    properties = schema["properties"]
    if set(arguments) - set(properties) or set(schema.get("required", ())) - set(
        arguments
    ):
        raise ValueError("mcp.arguments_invalid: missing or unknown argument")
    for key, value in arguments.items():
        kind = properties[key]["type"]
        valid = (
            (
                kind == "string"
                and isinstance(value, str)
                and bool(value)
                and "\x00" not in value
                and not value.startswith("-")
            )
            or (kind == "boolean" and isinstance(value, bool))
            or (
                kind == "array"
                and isinstance(value, list)
                and all(
                    isinstance(item, str)
                    and item
                    and "\x00" not in item
                    and not item.startswith("-")
                    for item in value
                )
            )
        )
        if not valid:
            raise ValueError(f"mcp.arguments_invalid: invalid {key}")
    if name in _MUTATING_TOOLS and arguments.get("acknowledge") is not True:
        raise ValueError(
            "mcp.acknowledgement_required: mutation requires acknowledge=true"
        )
    if name == "operator_mcp_write" and not allow_operator_config:
        raise ValueError(
            "mcp.operator_config_disabled: start with --allow-operator-config to enable"
        )
    result = dict(arguments)

    def scoped(value: str, base: Path) -> str:
        path = Path(value).expanduser()
        resolved = (base / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(
                "mcp.path_outside_root: path escapes the configured project root"
            )
        return str(resolved)

    if "project" in properties:
        result["project"] = scoped(arguments.get("project", "."), root)
        _require_discovery_scope(Path(result["project"]), root)
    project = Path(result.get("project", root))
    for key in ("component", "manifest", "compatibility", "observations"):
        if key in result:
            result[key] = scoped(result[key], project)
    if name == "catalog_copy":
        source = result["source"]
        if "://" not in source and not source.startswith("git@"):
            result["source"] = scoped(source, root)
    return result


def _require_discovery_scope(selected: Path, root: Path) -> None:
    project = discover_project(selected)
    if project is not None and not project.root.resolve().is_relative_to(root):
        raise ValueError(
            "mcp.path_outside_root: project discovery escapes the session root"
        )


def _run_cli(argv: list[str]) -> dict[str, Any]:
    stdout = StringIO()
    stderr = StringIO()
    status = litai_main(["--json", *argv], stdout=stdout, stderr=stderr)
    payload = stdout.getvalue() or stderr.getvalue()
    try:
        envelope = json.loads(payload)
    except json.JSONDecodeError:
        envelope = {
            "schema": "literate-ai/cli-error@1",
            "ok": False,
            "command": argv[0] if argv else "unknown",
            "error": {"code": "mcp.cli_output_invalid", "message": payload[:512]},
        }
    return {"status": status, "envelope": envelope}


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    project = str(arguments.get("project") or ".")
    if name == "verify":
        return _run_cli(["verify", project])
    if name == "lock":
        return _run_cli(["lock", str(arguments["component"]), "--project", project])
    if name == "plan":
        return _run_cli(["plan", str(arguments["component"]), "--project", project])
    if name == "rebuild":
        argv = ["rebuild", str(arguments["component"]), "--project", project]
        if arguments.get("allow_host_execution"):
            argv.append("--allow-host-execution")
        return _run_cli(argv)
    if name == "project_validate":
        return _run_cli(["project", "validate", "--project", project])
    if name == "catalog_copy":
        argv = ["catalog", "copy", str(arguments["source"]), *arguments["items"]]
        argv.extend(["--project", project])
        if arguments.get("overwrite"):
            argv.append("--overwrite")
        return _run_cli(argv)
    if name == "operator_mcp_show":
        return _run_cli(["config", "mcp", "show"])
    if name == "operator_mcp_write":
        argv = ["config", "mcp", "write"]
        for server in arguments["servers"]:
            argv.extend(("--server", str(server)))
        return _run_cli(argv)
    if name == "work_record":
        argv = [
            "work",
            "record",
            str(arguments["work_id"]),
            "--title",
            str(arguments["title"]),
            "--priority",
            str(arguments["priority"]),
            "--owner",
            str(arguments["owner"]),
            "--direction",
            str(arguments["direction"]),
            "--conclusion",
            str(arguments["conclusion"]),
            "--project",
            project,
        ]
        for key, option in (
            ("dependencies", "--depends-on"),
            ("implementation", "--implementation"),
            ("evidence", "--evidence"),
        ):
            for item in arguments.get(key, []):
                argv.extend((option, str(item)))
        return _run_cli(argv)
    if name == "work_close":
        return _run_cli(
            ["work", "close", str(arguments["work_id"]), "--project", project]
        )
    if name == "resolve_nvidia_stack":
        argv = [
            "worker",
            "resolve-nvidia",
            "--compatibility",
            str(arguments["compatibility"]),
            "--worker-id",
            str(arguments["worker_id"]),
            "--python-abi",
            str(arguments["python_abi"]),
        ]
        for key, option in (
            ("observations", "--observations"),
            ("toolkit", "--toolkit"),
        ):
            if arguments.get(key):
                argv.extend((option, str(arguments[key])))
        for package in arguments.get("packages", []):
            argv.extend(("--package", str(package)))
        return _run_cli(argv)
    if name == "document_verify":
        return _run_cli(
            [
                "document",
                "verify",
                "--manifest",
                str(arguments["manifest"]),
                "--component",
                str(arguments["component"]),
            ]
        )
    raise KeyError(name)


def _handle(
    message: dict[str, Any],
    *,
    project_root: Path | None = None,
    allow_operator_config: bool = False,
) -> dict[str, Any] | None:
    root = (project_root or Path.cwd()).resolve()
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "literate-ai", "version": DISTRIBUTION_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": list(_TOOLS)},
        }
    if method == "resources/list":
        try:
            _require_discovery_scope(root, root)
            resources = project_mcp_resources(root)
        except (McpResourceError, OSError, ValueError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32002, "message": str(exc)},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"resources": [item.metadata() for item in resources]},
        }
    if method == "resources/read":
        uri = str((message.get("params") or {}).get("uri") or "")
        try:
            _require_discovery_scope(root, root)
            resource = read_project_mcp_resource(root, uri)
            text = resource.content.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32003, "message": "resource is not UTF-8 text"},
            }
        except (McpResourceError, OSError, ValueError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32002, "message": str(exc)},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "contents": [
                    {"uri": resource.uri, "mimeType": resource.mime_type, "text": text}
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "params must be an object"},
            }
        name = params.get("name")
        arguments = params.get("arguments", {})
        try:
            arguments = _scoped_arguments(
                str(name), arguments, root, allow_operator_config=allow_operator_config
            )
            result = _call_tool(str(name), arguments)
        except (ValueError, OSError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            }
        except KeyError:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown tool {name!r}"},
            }
        text = json.dumps(result["envelope"], ensure_ascii=False)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": result["status"] != 0
                or not bool(result["envelope"].get("ok", True)),
            },
        }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"unknown method {method!r}"},
    }


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--allow-operator-config", action="store_true")
    args = parser.parse_args()
    root = args.project_root.resolve(strict=True)
    if not root.is_dir():
        parser.error("--project-root must be a directory")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        response = _handle(
            message, project_root=root, allow_operator_config=args.allow_operator_config
        )
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
