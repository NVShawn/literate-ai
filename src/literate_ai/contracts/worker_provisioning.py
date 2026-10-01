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

    SCHEMA = "urn:literate-ai:schema:v1:worker-provisioner"

    def __post_init__(self):
        command = string_tuple(self.command, "WorkerProvisioner.command")
        if not command or len(command) > 64 or any("\0" in arg for arg in command):
            fail("WorkerProvisioner.command", "requires 1..64 non-NUL arguments")
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
        return {
            "schema": self.SCHEMA,
            "command": list(self.command),
            "enabled": self.enabled,
            "environment": [v.to_dict() for v in self.environment],
            "help_argument": self.help_argument,
            "timeout_seconds": self.timeout_seconds,
        }

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
        )
