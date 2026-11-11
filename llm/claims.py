"""Durable hash-only claims prevent repeated charges after uncertain outcomes."""

import json
import os
from pathlib import Path
import re
from uuid import uuid4

from llm.cache import require_cache_key
from llm.errors import DuplicateClaim


_STATUSES = frozenset(("pending", "paid_failure", "uncertain", "succeeded"))


class FileClaimStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._owned: set[str] = set()

    def _path(self, key: str) -> Path:
        require_cache_key(key)
        return self.directory / f"{key}.json"

    def _sync_directory(self) -> None:
        if os.name != "nt":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write(descriptor: int, key: str, status: str) -> None:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"cache_key": key, "status": status}, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())

    def acquire(self, key: str) -> None:
        try:
            descriptor = os.open(self._path(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise DuplicateClaim() from None
        # An interrupted write deliberately leaves a blocking claim for an operator.
        self._write(descriptor, key, "pending")
        self._sync_directory()
        self._owned.add(key)

    def mark(self, key: str, status: str) -> None:
        target = self._path(key)
        if status not in _STATUSES or key not in self._owned or not target.is_file():
            raise ValueError("claim is not owned or status is invalid")
        temporary = self.directory / f".{key}.{uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            self._write(descriptor, key, status)
            os.replace(temporary, target)
            self._sync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def release(self, key: str) -> None:
        if key not in self._owned:
            raise ValueError("claim is not owned")
        self._path(key).unlink()
        self._owned.discard(key)
        self._sync_directory()

    def purge(self) -> None:
        """Full purge; caller must first suspend processing and issuance."""
        for path in self.directory.iterdir():
            claim_file = re.fullmatch(r"[a-f0-9]{64}\.json", path.name)
            interrupted_update = re.fullmatch(r"\.[a-f0-9]{64}\.[a-f0-9]{32}\.tmp", path.name)
            if (claim_file or interrupted_update) and not path.is_dir():
                path.unlink()
        self._owned.clear()
        self._sync_directory()
