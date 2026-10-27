"""Database snapshots: the platform's view of a database, for SQL dry-runs (DESIGN.md §6.2).

`environment["databases"]` maps a database path (or name) to a snapshot spec:

    {"name": "app", "dialect": "sqlite", "snapshot_id": "fsdb-staging-v0",
     "tables": {"customers": {
         "primary_key": "id", "name_column": "account", "protected": false,
         "columns": {"id": {}, "email": {"sensitivity": "S2"}, "role": {"security": "permissions"}},
         "rows": [{"id": 1, "email": "...", "_protected": true}, ...]}}}

Keys starting with "_" in a row are metadata, not columns. A dry-run turns a statement's selector
(its WHERE clause) into the concrete rows it touches by running a SELECT of primary keys against an
in-memory copy of the snapshot, opened read-only. Nothing runs against the real database; in
production the same query goes to a read-only replica.

Resource ids: tables "customers", columns "customers.email", rows "customers/42".
"""

from __future__ import annotations

import posixpath
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from dmgate.effects.registry import Resource

_LEVELS = ("S0", "S1", "S2", "S3")


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    primary_key: str
    column_attrs: Mapping[str, Mapping[str, Any]]
    rows: tuple[Mapping[str, Any], ...]
    protected: bool = False
    name_column: str | None = None
    sensitivity: str | None = None  # explicit, or the highest level among the columns

    def row_id(self, key: Any) -> str:
        return f"{self.name}/{key}"


class Snapshot:
    def __init__(self, path: str, spec: Mapping[str, Any]):
        self.path = path
        self.name = spec.get("name", posixpath.basename(path))
        self.dialect = spec.get("dialect", "sqlite")
        self.spec = spec
        self.tables: dict[str, Table] = {}
        for name, t in spec.get("tables", {}).items():
            columns = tuple(t["columns"])
            levels = [a["sensitivity"] for a in t["columns"].values() if a.get("sensitivity") in _LEVELS]
            self.tables[name] = Table(
                name=name,
                columns=columns,
                primary_key=t.get("primary_key", columns[0]),
                column_attrs={c: dict(a) for c, a in t["columns"].items()},
                rows=tuple(t.get("rows", ())),
                protected=bool(t.get("protected", False)),
                name_column=t.get("name_column"),
                sensitivity=t.get("sensitivity") or (max(levels, key=_LEVELS.index) if levels else None),
            )
        self._connection: sqlite3.Connection | None = None

    def table(self, name: str) -> Table | None:
        return self.tables.get(name) or next((t for n, t in self.tables.items() if n.lower() == name.lower()), None)

    def connection(self) -> sqlite3.Connection:
        """An in-memory copy, built once and then opened read-only (PRAGMA query_only)."""
        if self._connection is None:
            db = sqlite3.connect(":memory:", check_same_thread=False)
            for t in self.tables.values():
                cols = ", ".join(f'"{c}"' for c in t.columns)
                db.execute(f'CREATE TABLE "{t.name}" ({cols})')
                marks = ", ".join("?" for _ in t.columns)
                db.executemany(f'INSERT INTO "{t.name}" VALUES ({marks})',
                               [tuple(row.get(c) for c in t.columns) for row in t.rows])
            db.commit()
            db.execute("PRAGMA query_only = ON")
            self._connection = db
        return self._connection

    def query(self, sql: str) -> list[tuple[Any, ...]]:
        return self.connection().execute(sql).fetchall()

    def resources(self) -> list[Resource]:
        out = []
        for t in self.tables.values():
            out.append(Resource(t.name, "table", {"name": t.name, "protected": t.protected, "database": self.path,
                                                  **({"sensitivity": t.sensitivity} if t.sensitivity else {})}))
            for c in t.columns:
                out.append(Resource(f"{t.name}.{c}", "column", {"name": c, **t.column_attrs[c]}))
            for row in t.rows:
                attrs: dict[str, Any] = {
                    "table": t.name,
                    "values": {c: row.get(c) for c in t.columns},
                    "protected": bool(row.get("_protected", False)),
                }
                if t.name_column and row.get(t.name_column) is not None:
                    attrs["name"] = str(row[t.name_column])
                if t.sensitivity:
                    attrs["sensitivity"] = t.sensitivity
                out.append(Resource(t.row_id(row[t.primary_key]), "row", attrs))
        return out


_CACHE: dict[tuple[str, Any], tuple[Mapping[str, Any], Snapshot]] = {}


def _snapshot(path: str, spec: Mapping[str, Any]) -> Snapshot:
    key = (path, spec.get("snapshot_id") or id(spec))
    cached = _CACHE.get(key)
    if cached is None:
        cached = _CACHE[key] = (spec, Snapshot(path, spec))  # keep spec alive so its id stays unique
    return cached[1]


def snapshots(environment: Mapping[str, Any]) -> list[Snapshot]:
    return [_snapshot(path, spec) for path, spec in environment.get("databases", {}).items()]


def find_snapshot(environment: Mapping[str, Any], database: str | None) -> Snapshot | None:
    """The snapshot a command refers to: by path (relative paths from the cwd) or by name. With no
    database named, the only snapshot if there is exactly one."""
    available = snapshots(environment)
    if database is None:
        return available[0] if len(available) == 1 else None
    names = {database}
    cwd = environment.get("cwd")
    if cwd and not database.startswith("/"):
        names.add(posixpath.normpath(posixpath.join(cwd, database)))
    return next((s for s in available if s.path in names or s.name in names), None)
