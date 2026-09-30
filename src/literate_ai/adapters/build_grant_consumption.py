"""Durable replay prevention in an operator-owned local SQLite store.

The trusted launcher owns this file and directory outside worker authority. This
adapter does not establish that separation. Administrators and storage are trusted;
rollback or replacement of the store requires revoking outstanding grants first.
No missing-store recovery, refund or deletion operation is supplied.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from literate_ai.contracts.identity import canonical_identity
from literate_ai.security.policy import (
    AuthorizationError,
    BuildAuthorization,
    BuildRequest,
)

_VERSION = 1


def _regular(path: Path) -> None:
    if (
        not path.is_absolute()
        or any(p.is_symlink() for p in (path, *path.parents))
        or not path.is_file()
    ):
        raise AuthorizationError("security.admission_store_unavailable")


def _connect(path: Path) -> sqlite3.Connection:
    _regular(path)
    # mode=rw is critical: deletion cannot silently create an empty replay ledger.
    connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        if connection.execute("PRAGMA user_version").fetchone() != (_VERSION,):
            raise AuthorizationError("security.admission_store_invalid")
        return connection
    except BaseException:
        connection.close()
        raise


@dataclass(frozen=True, slots=True)
class SQLiteBuildGrantConsumptionStore:
    path: Path

    @classmethod
    def initialize(cls, path: Path) -> SQLiteBuildGrantConsumptionStore:
        """Explicit provisioning only; refuse existing files and missing parents."""
        if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
            raise AuthorizationError("security.admission_store_unavailable")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(
                sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
            ) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                with connection:
                    connection.execute(
                        "CREATE TABLE spent ("
                        "authorization_key TEXT PRIMARY KEY NOT NULL, "
                        "grant_identity TEXT NOT NULL, "
                        "request_identity TEXT NOT NULL)"
                    )
                    connection.execute(f"PRAGMA user_version={_VERSION}")
            return cls(path)
        except (OSError, sqlite3.Error) as exc:
            # A failed initialization stays unusable; never erase partial state.
            raise AuthorizationError("security.admission_store_unavailable") from exc

    def consume(self, grant: BuildAuthorization, request: BuildRequest) -> None:
        request_digest = canonical_identity(request.to_dict()).uri
        if grant.request_digest != request_digest:
            raise AuthorizationError("security.admission_request_mismatch")
        key = hashlib.sha256(grant.authorization_id.encode("utf-8")).hexdigest()
        try:
            with closing(_connect(self.path)) as connection:
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    if connection.execute(
                        "SELECT 1 FROM spent WHERE authorization_key=?", (key,)
                    ).fetchone():
                        raise AuthorizationError(
                            "security.authorization_already_consumed"
                        )
                    connection.execute(
                        "INSERT INTO spent VALUES (?, ?, ?)",
                        (key, canonical_identity(grant.to_dict()).uri, request_digest),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise AuthorizationError("security.admission_store_unavailable") from exc
