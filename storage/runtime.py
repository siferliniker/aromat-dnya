"""Encrypted live storage; synthetic tests use storage.database instead."""

import re

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url


def create_encrypted_engine(database_url: str, key: str | None) -> Engine:
    if not isinstance(key, str) or re.fullmatch(r"[0-9a-fA-F]{64}", key) is None:
        raise ValueError("DATABASE_ENCRYPTION_KEY must contain exactly 64 hexadecimal characters")
    try:
        url = make_url(database_url)
        if (url.drivername not in ("sqlite", "sqlite+pysqlcipher") or not url.database
                or url.database == ":memory:" or url.username or url.password or url.host or url.query):
            raise ValueError("unsupported encrypted database URL")
    except Exception:
        raise ValueError("a file SQLite URL without credentials or query parameters is required") from None
    try:
        from sqlcipher3 import dbapi2 as sqlcipher
    except ImportError:
        raise RuntimeError("SQLCipher driver is required; plaintext storage is disabled") from None

    # The pysqlcipher dialect reads its passphrase from URL.password. Use the same
    # SQLite protocol with an explicit SQLCipher module and keep the key out of URLs.
    engine = create_engine(url.set(drivername="sqlite+pysqlite"), module=sqlcipher,
                           hide_parameters=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure_connection(connection, _record):
        cursor = connection.cursor()
        try:
            cursor.execute(f'''PRAGMA key = "x'{key}'"''')
            cursor.execute("PRAGMA cipher_version")
            version = cursor.fetchone()
            if not version or not version[0]:
                raise RuntimeError("SQLCipher encryption is unavailable")
            cursor.execute("SELECT count(*) FROM sqlite_master")
            cursor.fetchone()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA secure_delete=ON")
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("SQLite foreign key protection is unavailable")
            cursor.execute("PRAGMA temp_store")
            if cursor.fetchone()[0] != 2:
                raise RuntimeError("SQLite temporary storage is not in memory")
        except Exception:
            raise RuntimeError("Encrypted database could not be opened safely") from None
        finally:
            cursor.close()

    try:
        with engine.connect():
            return engine
    except Exception:
        engine.dispose()
        raise RuntimeError("Encrypted database could not be opened safely") from None
