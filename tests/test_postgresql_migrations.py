"""Regresiones del bootstrap del esquema PostgreSQL."""

import re

import pytest

import database.database as database


class _Result:
    def __init__(self, rows=()):
        self._rows = rows

    def fetchall(self):
        return [(name,) for name in self._rows]


class _FakeConnection:
    def __init__(self, *, tables=(), indexes=(), fail_on=None):
        self.tables = set(tables)
        self.indexes = set(indexes)
        self.fail_on = fail_on
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("simulated DDL failure")
        if "information_schema.tables" in sql:
            return _Result(self.tables)
        if "FROM pg_indexes" in sql:
            return _Result(self.indexes)
        return _Result()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def _baseline_objects():
    sql = (database.BASE_DIR / "migrations_pg" / "001_initial_schema.sql").read_text(
        encoding="utf-8"
    )
    tables = set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_]\w*)", sql, re.I))
    indexes = set(re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+([a-zA-Z_]\w*)", sql, re.I))
    return tables, indexes


def test_bootstrap_serializa_workers_y_aplica_el_baseline_en_una_transaccion(monkeypatch):
    connection = _FakeConnection()
    monkeypatch.setattr(database, "get_connection", lambda: connection)

    database._init_postgresql()

    assert connection.calls[0] == (
        "SELECT pg_advisory_xact_lock(?)",
        (database._PG_SCHEMA_BOOTSTRAP_LOCK,),
    )
    assert "information_schema.tables" in connection.calls[1][0]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1


def test_bootstrap_rechaza_schema_parcial_sin_ejecutar_ddl(monkeypatch):
    connection = _FakeConnection(tables={"businesses"})
    monkeypatch.setattr(database, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="Schema PostgreSQL parcial/incompleto"):
        database._init_postgresql()

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert not any(sql.lstrip().upper().startswith("CREATE TABLE") for sql, _ in connection.calls)
    assert connection.closed == 1


def test_bootstrap_revierte_todo_si_falla_un_ddl(monkeypatch):
    connection = _FakeConnection(fail_on="CREATE TABLE businesses")
    monkeypatch.setattr(database, "get_connection", lambda: connection)

    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        database._init_postgresql()

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed == 1


def test_bootstrap_omite_schema_completo_y_rechaza_indices_ausentes(monkeypatch):
    tables, indexes = _baseline_objects()
    complete = _FakeConnection(tables=tables, indexes=indexes)
    monkeypatch.setattr(database, "get_connection", lambda: complete)

    database._init_postgresql()

    assert complete.commits == 0
    assert complete.rollbacks == 1
    assert complete.closed == 1

    partial = _FakeConnection(tables=tables, indexes=indexes - {next(iter(indexes))})
    monkeypatch.setattr(database, "get_connection", lambda: partial)
    with pytest.raises(RuntimeError, match="índices faltantes"):
        database._init_postgresql()
    assert partial.commits == 0
    assert partial.rollbacks == 1
    assert partial.closed == 1
