from sqlite3 import Connection

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry

from storage.models import Base


def _enable_sqlite_foreign_keys(
    connection: Connection, _connection_record: ConnectionPoolEntry
) -> None:
    # With sqlite3's non-legacy transaction mode, the pragma needs autocommit.
    if hasattr(connection, "autocommit"):
        previous = connection.autocommit
        connection.autocommit = True
        try:
            connection.execute("PRAGMA foreign_keys=ON").close()
        finally:
            connection.autocommit = previous
    else:
        connection.execute("PRAGMA foreign_keys=ON").close()


def create_database_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, hide_parameters=True)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def initialize_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)
