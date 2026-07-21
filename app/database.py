from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, CursorResult, Engine, RowMapping
from sqlalchemy.pool import NullPool


def normalize_database_url(value: str | Path) -> str:
    raw = str(value)
    if "://" in raw:
        return raw
    return f"sqlite+pysqlite:///{Path(raw).expanduser().resolve()}"


class QueryResult:
    def __init__(self, result: CursorResult[Any]) -> None:
        self.result = result

    @property
    def rowcount(self) -> int:
        return self.result.rowcount

    @property
    def lastrowid(self) -> int | None:
        value = self.result.lastrowid
        return int(value) if value is not None else None

    def fetchone(self) -> RowMapping | None:
        row = self.result.mappings().fetchone()
        return row

    def fetchall(self) -> Sequence[RowMapping]:
        return self.result.mappings().fetchall()


class DatabaseSession:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def execute(
        self,
        statement: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> QueryResult:
        sql, values = self._bind(statement, parameters)
        return QueryResult(self.connection.execute(text(sql), values))

    @staticmethod
    def _bind(
        statement: str,
        parameters: Sequence[Any] | Mapping[str, Any] | None,
    ) -> tuple[str, Mapping[str, Any]]:
        if parameters is None:
            return statement, {}
        if isinstance(parameters, Mapping):
            return statement, parameters
        parts = statement.split("?")
        expected = len(parts) - 1
        if expected != len(parameters):
            raise ValueError(f"SQL expected {expected} parameters, received {len(parameters)}")
        values = {f"p{index}": value for index, value in enumerate(parameters)}
        sql = "".join(
            part + (f":p{index}" if index < expected else "") for index, part in enumerate(parts)
        )
        return sql, values


class Database:
    def __init__(self, url: str | Path) -> None:
        self.url = normalize_database_url(url)
        connect_args = {"check_same_thread": False} if self.url.startswith("sqlite") else {}
        poolclass = NullPool if self.url.startswith("sqlite") else None
        self.engine: Engine = create_engine(
            self.url,
            pool_pre_ping=True,
            connect_args=connect_args,
            poolclass=poolclass,
        )

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    @contextmanager
    def connect(self) -> Iterator[DatabaseSession]:
        with self.engine.begin() as connection:
            if self.dialect == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            yield DatabaseSession(connection)

    def dispose(self) -> None:
        self.engine.dispose()
