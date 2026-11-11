"""Short transactions for checked results in the encrypted application database."""

import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from llm.contracts import CardSummary, ReviewExtraction
from llm.errors import InvalidModelResponse
from storage.models import LLMCacheEntry


T = TypeVar("T", bound=BaseModel)


def require_cache_key(key: str) -> None:
    if not isinstance(key, str) or re.fullmatch(r"[a-f0-9]{64}", key) is None:
        raise ValueError("invalid cache key")


class SQLAlchemyLLMCache:
    def __init__(self, engine: Engine, *, allow_plaintext_for_tests: bool = False) -> None:
        if not allow_plaintext_for_tests:
            with engine.connect() as connection:
                result = connection.execute(text("PRAGMA cipher_version"))
                version = result.fetchone() if result.returns_rows else None
                if not version or not version[0]:
                    raise ValueError("LLM cache requires SQLCipher encryption")
        self.engine = engine

    def get(self, key: str, schema: type[T]) -> T | None:
        require_cache_key(key)
        with Session(self.engine) as session:
            value = session.scalar(select(LLMCacheEntry).where(LLMCacheEntry.cache_key == key))
            if value is None:
                return None
            output = value.output
        try:
            return schema.model_validate(output)
        except (ValidationError, TypeError):
            raise InvalidModelResponse() from None

    def put(self, key: str, task: str, model: str, checked: BaseModel) -> None:
        require_cache_key(key)
        expected = {"extraction": ReviewExtraction, "summary": CardSummary}.get(task)
        if expected is None or not isinstance(checked, expected):
            raise InvalidModelResponse()
        try:
            validated = expected.model_validate_json(checked.model_dump_json())
        except (ValidationError, TypeError):
            raise InvalidModelResponse() from None
        with Session(self.engine) as session, session.begin():
            if session.get(LLMCacheEntry, key) is None:
                session.add(LLMCacheEntry(cache_key=key, task=task, model=model,
                                          output=validated.model_dump(mode="json")))

    def purge(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(LLMCacheEntry))
