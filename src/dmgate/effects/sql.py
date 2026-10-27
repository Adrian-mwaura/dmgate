"""SQL effect resolver (DESIGN.md §6.2): statements -> effects on tables, columns and rows.

sqlglot parses each statement. Columns come from the statement itself (`*` expanded from the
snapshot's schema). Rows come from a dry-run: the statement's FROM/JOIN/WHERE becomes a SELECT of
primary keys, run against the snapshot (`dmgate.effects.database`). Effects, per table:

| Statement      | Effects                                                                      |
|----------------|------------------------------------------------------------------------------|
| SELECT         | read column {returned columns}; read row {rows read} (row selection)         |
| UPDATE         | write row {rows changed} (row selection); write column {SET columns}         |
| DELETE         | delete row {rows deleted}                                                    |
| INSERT         | write table {t}; write column {inserted columns}; INSERT ... SELECT reads    |
| DROP TABLE     | delete table {t}                                                             |
| CREATE, ALTER  | write table {t} (schema)                                                     |
| anything else  | opaque (PRAGMA, ATTACH, ...)                                                 |

Rows and columns are separate effects so the rules can narrow either one: fewer rows, or fewer
columns. A SELECT's or UPDATE's row effect only identifies records; their data is in the column
effect. Whatever cannot be resolved stays unresolved, so the effect is never exact: no snapshot,
an unknown table or column, a dry-run error, a set operation or CTE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import exp

from dmgate.effects.database import Snapshot, Table, find_snapshot
from dmgate.effects.model import LISTING, ROW_SELECTION, Effect, Resolution, Verb

_IGNORED = (exp.Transaction, exp.Commit, exp.Rollback)


def resolve_sql(sql: str, environment: Mapping[str, Any] | None = None, database: str | None = None) -> Resolution:
    env = environment or {}
    snapshot = find_snapshot(env, database)
    ctx = _SqlCtx(snapshot, sql)
    try:
        statements = [s for s in sqlglot.parse(sql, read=snapshot.dialect if snapshot else "sqlite") if s is not None]
    except sqlglot.errors.SqlglotError:
        return Resolution((_opaque("the SQL did not parse"),))
    for statement in statements:
        ctx.statement(statement)
    return Resolution(tuple(ctx.effects), tuple(ctx.flows))


def _opaque(note: str) -> Effect:
    return Effect(Verb.EXEC, "process", frozenset({"sql"}), opaque=True, command="sql", note=note)


class _SqlCtx:
    def __init__(self, snapshot: Snapshot | None, source: str):
        self.snapshot = snapshot
        self.source = source
        self.effects: list[Effect] = []
        self.flows: list[tuple[int, int]] = []

    def add(self, effect: Effect) -> int:
        self.effects.append(effect)
        return len(self.effects) - 1

    def statement(self, s: exp.Expression) -> None:
        if isinstance(s, _IGNORED):
            return
        if isinstance(s, (exp.Select, exp.SetOperation)):
            self.select(s)
        elif isinstance(s, exp.Update):
            self.update(s)
        elif isinstance(s, exp.Delete):
            self.delete(s)
        elif isinstance(s, exp.Insert):
            self.insert(s)
        elif isinstance(s, exp.Drop):
            self.drop(s)
        elif isinstance(s, (exp.Create, exp.Alter)):
            name = _table_name(s.this)
            if name:
                self.add(self._table_effect(Verb.WRITE, name, "schema"))
            else:
                self.add(_opaque("schema statement on an unknown object"))
        else:
            self.add(_opaque(f"unsupported SQL statement: {s.key}"))

    # -- schema --------------------------------------------------------------------------------

    def _table(self, name: str) -> Table | None:
        return self.snapshot.table(name) if self.snapshot else None

    def _table_effect(self, verb: Verb, name: str, note: str = "") -> Effect:
        table = self._table(name)
        if self.snapshot is not None and table is None:
            return Effect(verb, "table", unresolved=(name,), command="sql", note=note)
        return Effect(verb, "table", frozenset({table.name if table else name}), command="sql", note=note)

    def _columns(self, table_name: str, names: list[str]) -> tuple[frozenset[str], tuple[str, ...]]:
        """Column ids, and the names the schema does not know (the statement would fail)."""
        table = self._table(table_name)
        if table is None:
            if self.snapshot is not None:
                return frozenset(), tuple(f"{table_name}.{n}" for n in names)
            return frozenset(f"{table_name}.{n}" for n in names), ()
        known = {c.lower(): c for c in table.columns}
        ids = {f"{table.name}.{known[n.lower()]}" for n in names if n.lower() in known}
        missing = tuple(f"{table.name}.{n}" for n in names if n.lower() not in known)
        return frozenset(ids), missing

    # -- dry-run -------------------------------------------------------------------------------

    def _dry_run(self, query: exp.Select, tables: list[tuple[str, Table]]) -> list[set[str]] | None:
        """Row ids per (alias, table) in `tables`, or None if the query cannot run here."""
        if self.snapshot is None:
            return None
        try:
            rows = self.snapshot.query(query.sql(dialect="sqlite"))
        except Exception:  # noqa: BLE001 - any database error means the selector is unresolved
            return None
        out: list[set[str]] = [set() for _ in tables]
        for row in rows:
            for i, (_, table) in enumerate(tables):
                if row[i] is not None:
                    out[i].add(table.row_id(row[i]))
        return out

    def _where_rows(self, table_expr: exp.Table, where: exp.Where | None, name: str) -> tuple[frozenset[str], tuple[str, ...]]:
        """Rows an UPDATE or DELETE touches: SELECT pk FROM t WHERE <same predicate>."""
        table = self._table(name)
        selector = where.this.sql() if where is not None else "all rows"
        if table is None:
            return frozenset(), (f"{name}: {selector}",)
        query = sqlglot.select(exp.column(table.primary_key, table=table_expr.alias_or_name)).from_(table_expr.copy())
        if where is not None:
            query = query.where(where.this.copy())
        found = self._dry_run(query, [(table_expr.alias_or_name, table)])
        if found is None:
            return frozenset(), (f"{name}: {selector}",)
        return frozenset(found[0]), ()

    # -- statements ----------------------------------------------------------------------------

    def select(self, s: exp.Expression) -> list[int]:
        simple = isinstance(s, exp.Select) and not s.args.get("with_") and all(
            isinstance(source, exp.Table) for source in _sources(s))
        if not simple:
            return self._conservative_read(s)
        if not _sources(s):  # SELECT 1, or SELECT (SELECT ssn FROM t): reads only what its subqueries read
            return self._conservative_read(s) if s.find(exp.Table) else []
        aliases = {source.alias_or_name: source for source in _sources(s)}
        tables = {alias: self._table(source.name) for alias, source in aliases.items()}

        columns: dict[str, set[str]] = {alias: set() for alias in aliases}
        unresolved: dict[str, list[str]] = {alias: [] for alias in aliases}
        first = next(iter(aliases))
        aggregated = bool(s.args.get("group"))
        for projection in s.expressions:
            target = projection.unalias()
            if isinstance(target, exp.Star):
                for alias in aliases:
                    self._all_columns(alias, aliases, tables, columns, unresolved)
                continue
            if isinstance(target, exp.Column) and isinstance(target.this, exp.Star):
                self._all_columns(target.table or first, aliases, tables, columns, unresolved)
                continue
            if target.find(exp.AggFunc):
                aggregated = True
            for col in target.find_all(exp.Column):
                if isinstance(col.this, exp.Star):
                    continue
                alias = col.table or self._owner(col.name, aliases, tables) or first
                if alias not in aliases:
                    unresolved[first].append(col.sql())
                    continue
                ids, missing = self._columns(aliases[alias].name, [col.name])
                columns[alias] |= ids
                unresolved[alias] += missing

        rows: list[set[str]] | None = None
        known = [(alias, table) for alias, table in tables.items() if table is not None]
        if len(known) == len(tables):
            dry = s.copy()
            dry.set("expressions", [exp.column(t.primary_key, table=alias) for alias, t in known])
            for key in ("group", "having", "distinct", "qualify"):
                dry.set(key, None)
            if aggregated:
                for key in ("limit", "offset", "order"):
                    dry.set(key, None)
            rows = self._dry_run(dry, known)

        indices = []
        where = s.args.get("where")
        selector = where.this.sql() if where is not None else "all rows"
        for i, (alias, source) in enumerate(aliases.items()):
            name = tables[alias].name if tables[alias] else source.name
            if columns[alias] or unresolved[alias]:
                indices.append(self.add(Effect(Verb.READ, "column", frozenset(columns[alias]), tuple(unresolved[alias]),
                                               command="sql", note="returned")))
            row_targets = frozenset(rows[i]) if rows is not None else frozenset()
            row_unresolved = () if rows is not None else (f"{name}: {selector}",)
            indices.append(self.add(Effect(Verb.READ, "row", row_targets, row_unresolved, command="sql",
                                           note=ROW_SELECTION)))
        return indices

    def _all_columns(self, alias: str, aliases: Mapping[str, exp.Table], tables: Mapping[str, Table | None],
                     columns: dict[str, set[str]], unresolved: dict[str, list[str]]) -> None:
        if alias not in aliases:
            unresolved[next(iter(aliases))].append(f"{alias}.*")
            return
        table = tables[alias]
        if table is None:
            unresolved[alias].append(f"{aliases[alias].name}.*")
        else:
            columns[alias] |= {f"{table.name}.{c}" for c in table.columns}

    def _owner(self, column: str, aliases: Mapping[str, exp.Table], tables: Mapping[str, Table | None]) -> str | None:
        """The alias of the only FROM table that has this column."""
        owners = [a for a, t in tables.items() if t is not None and column.lower() in (c.lower() for c in t.columns)]
        return owners[0] if len(owners) == 1 else None

    def _conservative_read(self, s: exp.Expression) -> list[int]:
        """Set operations, CTEs, subqueries in FROM: every column of every table read, rows unknown."""
        indices = []
        cte_names = {cte.alias_or_name for cte in s.find_all(exp.CTE)}
        for name in sorted({t.name for t in s.find_all(exp.Table) if t.name not in cte_names}):
            table = self._table(name)
            if table is not None:
                cols = frozenset(f"{table.name}.{c}" for c in table.columns)
                indices.append(self.add(Effect(Verb.READ, "column", cols, command="sql", note="returned")))
            else:
                indices.append(self.add(Effect(Verb.READ, "column", unresolved=(f"{name}.*",), command="sql")))
            indices.append(self.add(Effect(Verb.READ, "row", unresolved=(f"{name}: complex query",), command="sql",
                                           note=ROW_SELECTION)))
        return indices

    def update(self, s: exp.Update) -> None:
        name = _table_name(s.this)
        if not name:
            self.add(_opaque("UPDATE of an unknown object"))
            return
        rows, row_unresolved = self._where_rows(s.this, s.args.get("where"), name)
        table = self._table(name)
        self.add(Effect(Verb.WRITE, "row", rows, row_unresolved, command="sql", note=ROW_SELECTION))
        set_columns = [eq.this.name for eq in s.expressions if isinstance(eq, exp.EQ) and isinstance(eq.this, exp.Column)]
        ids, missing = self._columns(table.name if table else name, set_columns)
        self.add(Effect(Verb.WRITE, "column", ids, missing, command="sql", note="set"))

    def delete(self, s: exp.Delete) -> None:
        name = _table_name(s.this)
        if not name:
            self.add(_opaque("DELETE from an unknown object"))
            return
        rows, row_unresolved = self._where_rows(s.this, s.args.get("where"), name)
        self.add(Effect(Verb.DELETE, "row", rows, row_unresolved, command="sql"))

    def insert(self, s: exp.Insert) -> None:
        target = s.this
        name = _table_name(target.this if isinstance(target, exp.Schema) else target)
        if not name:
            self.add(_opaque("INSERT into an unknown object"))
            return
        reads = self.select(s.expression) if isinstance(s.expression, (exp.Select, exp.SetOperation)) else []
        table = self._table(name)
        if isinstance(target, exp.Schema) and target.expressions:
            names = [c.name for c in target.expressions]
        elif table is not None:
            names = list(table.columns)
        else:
            names = []
        written = [self.add(self._table_effect(Verb.WRITE, name, "insert"))]
        ids, missing = self._columns(table.name if table else name, names)
        if ids or missing or not names:
            written.append(self.add(Effect(Verb.WRITE, "column", ids, missing or (() if names else (f"{name}.*",)),
                                           command="sql", note="insert")))
        self.flows += [(r, w) for r in reads if self.effects[r].note != ROW_SELECTION for w in written]

    def drop(self, s: exp.Drop) -> None:
        kind = (s.args.get("kind") or "").upper()
        objects = s.args.get("tables") or ([s.this] if s.this is not None else [])
        if kind != "TABLE" or not objects:
            self.add(_opaque(f"DROP {kind or 'object'}"))
            return
        for obj in objects:
            name = _table_name(obj)
            self.add(self._table_effect(Verb.DELETE, name, "drop") if name else _opaque("DROP of an unknown table"))


def _sources(s: exp.Select) -> list[exp.Expression]:
    from_ = s.args.get("from_")
    out = [from_.this] if from_ is not None else []
    out += [join.this for join in s.args.get("joins") or []]
    return out


def _table_name(node: exp.Expression | None) -> str | None:
    if isinstance(node, exp.Table) and node.name:
        return node.name
    return None


def listing_effect(snapshot: Snapshot | None) -> Effect:
    """`.tables` / `.schema` in the sqlite3 shell: table names and their definitions."""
    tables = frozenset(snapshot.tables) if snapshot else frozenset()
    return Effect(Verb.READ, "table", tables, () if snapshot else ("tables",), command="sqlite3", note=LISTING)
