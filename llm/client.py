"""Minimal Responses adapter; no provider bodies escape this boundary."""

import asyncio
from typing import TypeVar

from openai import APIConnectionError, APIResponseValidationError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from llm.errors import (
    IncompleteResponse, InvalidModelResponse, ModelRefusal, RequestRejected, UncertainOutcome,
)


T = TypeVar("T", bound=BaseModel)


class OpenAIResponsesClient:
    def __init__(self, async_openai: AsyncOpenAI) -> None:
        # SDK retries bypass durable claim decisions. Only the orchestrator may retry.
        self.client = async_openai.with_options(max_retries=0)

    async def parse(
        self, *, model: str, input_text: str, instructions: str, schema: type[T],
        max_output_tokens: int, timeout_seconds: float,
    ) -> T:
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await self.client.responses.parse(
                    model=model, input=input_text, instructions=instructions,
                    text_format=schema, store=False, background=False,
                    max_output_tokens=max_output_tokens, timeout=timeout_seconds,
                )
        except APIStatusError as error:
            if error.status_code in (400, 401, 403, 404, 405, 413, 415, 422, 429):
                exhausted = error.code in ("credit_balance_exhausted", "insufficient_quota")
                raise RequestRejected(retryable=error.status_code == 429 and not exhausted) from None
            raise UncertainOutcome() from None
        except (APIConnectionError, TimeoutError):
            raise UncertainOutcome() from None
        except (ValidationError, APIResponseValidationError, ValueError, TypeError):
            raise InvalidModelResponse() from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise UncertainOutcome() from None
        if response.status == "incomplete":
            raise IncompleteResponse()
        if response.status != "completed" or response.error is not None:
            raise InvalidModelResponse()
        texts = 0
        for output in response.output:
            if getattr(output, "type", None) != "message":
                continue
            if getattr(output, "status", None) != "completed":
                raise IncompleteResponse()
            for content in output.content:
                if content.type == "refusal":
                    raise ModelRefusal()
                if content.type == "output_text":
                    texts += 1
        parsed = response.output_parsed
        if texts != 1 or not isinstance(parsed, schema):
            raise InvalidModelResponse()
        try:
            return schema.model_validate_json(parsed.model_dump_json())
        except (ValidationError, ValueError, TypeError):
            raise InvalidModelResponse() from None
