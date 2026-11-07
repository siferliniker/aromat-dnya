import importlib.util
import sqlite3

import pytest
from sqlalchemy import text

from storage.runtime import create_encrypted_engine


@pytest.mark.parametrize("key", [None, "", " ", "a" * 63, "a" * 65, "g" * 64, "secret"])
def test_encrypted_runtime_rejects_missing_or_invalid_key_without_creating_database(tmp_path, key):
    database = tmp_path / "private.sqlite3"
    with pytest.raises((ValueError, RuntimeError)):
        engine = create_encrypted_engine(f"sqlite:///{database.as_posix()}", key)
        try:
            with engine.connect():
                raise AssertionError("Invalid encryption key was accepted")
        finally:
            engine.dispose()
    assert not database.exists()


def test_runtime_fails_closed_when_sqlcipher_driver_is_unavailable(tmp_path):
    if importlib.util.find_spec("sqlcipher3") is not None:
        pytest.skip("SQLCipher driver is installed; Linux ciphertext checks cover the real driver")
    database = tmp_path / "must-not-be-plaintext.sqlite3"
    with pytest.raises((RuntimeError, ImportError)):
        engine = create_encrypted_engine(f"sqlite:///{database.as_posix()}", "ab" * 32)
        try:
            with engine.connect():
                raise AssertionError("Runtime silently substituted unencrypted SQLite")
        finally:
            engine.dispose()
    assert not database.exists()


def test_actual_sqlcipher_encrypts_database_and_wal_rejects_wrong_key_and_reopens(tmp_path, caplog):
    if importlib.util.find_spec("sqlcipher3") is None:
        pytest.skip("Run this acceptance check with the clean SQLCipher-enabled venv")
    database = tmp_path / "actual-private.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    key, wrong_key = "ab" * 32, "cd" * 32
    marker = "QA_PRIVATE_PAYLOAD_54a383ff_ea30_4134"
    engine = create_encrypted_engine(url, key)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA cipher_version").scalar_one()
            assert connection.exec_driver_sql("PRAGMA temp_store").scalar_one() == 2
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one() == "wal"
            connection.commit()
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE qa_private_payload(id INTEGER PRIMARY KEY,payload TEXT)")
            connection.execute(text("INSERT INTO qa_private_payload(payload) VALUES (:payload)"), {"payload": marker})
        wal = database.with_name(database.name + "-wal")
        assert wal.is_file() and wal.stat().st_size > 0
        assert marker.encode() not in database.read_bytes()
        assert marker.encode() not in wal.read_bytes()
        assert not database.read_bytes().startswith(b"SQLite format 3")
        with sqlite3.connect(database) as plaintext:
            with pytest.raises(sqlite3.DatabaseError):
                plaintext.execute("SELECT * FROM sqlite_master").fetchall()
        with pytest.raises(RuntimeError) as failure:
            create_encrypted_engine(url, wrong_key)
        assert key not in str(failure.value) and wrong_key not in str(failure.value)
    finally:
        engine.dispose()
    reopened = create_encrypted_engine(url, key)
    try:
        with reopened.connect() as connection:
            assert connection.execute(text("SELECT payload FROM qa_private_payload")).scalar_one() == marker
    finally:
        reopened.dispose()
    assert marker not in caplog.text and key not in caplog.text and wrong_key not in caplog.text
