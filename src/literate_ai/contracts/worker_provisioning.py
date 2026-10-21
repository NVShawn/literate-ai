"""Provider-neutral, user-local dynamic worker configuration and wire protocol."""

from __future__ import annotations

from dataclasses import dataclass

from ._validation import bool_value, contract_fields, fail, int_value, string_tuple
from .execution_dispatch import ExecutionWorkerEnvironment


@dataclass(frozen=True, slots=True)
class WorkerProvisioner:
    command: tuple[str, ...]
    enabled: bool = False
    environment: tuple[ExecutionWorkerEnvironment, ...] = ()
    help_argument: str = "--help"
    timeout_seconds: int = 300
    inspection_command: tuple[str, ...] = ()

    SCHEMA = "urn:literate-ai:schema:v1:worker-provisioner"

    def __post_init__(self):
        command = string_tuple(self.command, "WorkerProvisioner.command")
        if not command or len(command) > 64 or any("\0" in arg for arg in command):
            fail("WorkerProvisioner.command", "requires 1..64 non-NUL arguments")
        if type(self.inspection_command) is not tuple:
            fail("WorkerProvisioner.inspection_command", "requires an immutable tuple")
        inspection = string_tuple(
            self.inspection_command, "WorkerProvisioner.inspection_command"
        )
        if len(inspection) > 64 or any("\0" in arg for arg in inspection):
            fail(
                "WorkerProvisioner.inspection_command",
                "requires at most 64 non-NUL arguments",
            )
        bool_value(self.enabled, "WorkerProvisioner.enabled")
        int_value(
            self.timeout_seconds,
            "WorkerProvisioner.timeout_seconds",
            minimum=1,
            maximum=3600,
        )
        if self.help_argument not in ("--help", "help"):
            fail("WorkerProvisioner.help_argument", "must be --help or help")
        names = [binding.name for binding in self.environment]
        if len(names) > 64 or len(set(names)) != len(names):
            fail("WorkerProvisioner.environment", "requires at most 64 unique bindings")

    def to_dict(self):
        value = {
            "schema": self.SCHEMA,
            "command": list(self.command),
            "enabled": self.enabled,
            "environment": [v.to_dict() for v in self.environment],
            "help_argument": self.help_argument,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.inspection_command:
            value["inspection_command"] = list(self.inspection_command)
        return value

    @classmethod
    def from_dict(cls, value):
        from ._validation import list_value

        data = contract_fields(
            value,
            path="WorkerProvisioner",
            schema_uri=cls.SCHEMA,
            required=frozenset(
                {
                    "command",
                    "enabled",
                    "environment",
                    "help_argument",
                    "timeout_seconds",
                }
            ),
            optional=frozenset({"inspection_command"}),
        )
        if "inspection_command" in data and (
            not isinstance(data["inspection_command"], list)
            or not data["inspection_command"]
        ):
            fail(
                "WorkerProvisioner.inspection_command",
                "explicit inspection command must be nonempty",
            )
        return cls(
            string_tuple(data["command"], "WorkerProvisioner.command"),
            data["enabled"],
            tuple(
                ExecutionWorkerEnvironment.from_dict(v)
                for v in list_value(
                    data["environment"], "WorkerProvisioner.environment"
                )
            ),
            data["help_argument"],
            data["timeout_seconds"],
            string_tuple(
                data.get("inspection_command", ()),
                "WorkerProvisioner.inspection_command",
            ),
        )
