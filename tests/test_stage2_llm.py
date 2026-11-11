"""Independent stage-two checks. All SDK traffic terminates in MockTransport."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import logging

import httpx2
from openai import AsyncOpenAI
import pytest
from sqlalchemy import select

from collectors.contracts import ReviewTextRecord
from llm.cache import SQLAlchemyLLMCache
from llm.claims import FileClaimStore
from llm.client import OpenAIResponsesClient
from llm.contracts import CardSummary, ReviewExtraction, build_cache_key
from llm.errors import (
    AdmissionDenied, DuplicateClaim, IncompleteResponse, InvalidEvidence,
    InvalidModelResponse, LLMError, ModelRefusal, ProcessingDisabled, RequestRejected, UncertainOutcome,
)
from llm.policy import SourceApproval
from llm.service import ReviewAnalyzer
from llm.validation import (
    CatalogPerfume, SummaryEvidence, render_summary, validate_extraction, validate_summary,
)
from storage.models import LLMCacheEntry


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
URL = "https://example.test/reviews/1"
QUOTE = "QA Cedar от QA House: нравится древесный аккорд"
TEXT = f"Мой отзыв: {QUOTE}."


def review(text=TEXT):
    return ReviewTextRecord(external_id="review-1", source_url=URL, text=text, published_at=NOW)


def mention(**changes):
    return {
        "perfume_name": "QA Cedar", "perfume_house": "QA House", "sentiment": "positive",
        "evidence_quote": QUOTE, "confidence": 0.9,
    } | changes


def extraction(*items):
    return ReviewExtraction.model_validate({"mentions": list(items or [mention()])})


def catalog():
    return [CatalogPerfume(id=17, name="QA Cedar", house="QA House", source_key="dataset", external_id="qa-cedar")]


def approval(**changes):
    values = dict(
        source_key="otzovik", approval_id="qa-approval-1", enabled=True,
        permission_status="approved", allow_llm=True, scope="public_review",
        allow_quotation=True, allow_retention=True,
        approved_urls=(URL,), obtained_at=NOW, expires_at=NOW + timedelta(days=1),
    )
    values.update(changes)
    return SourceApproval(**values)


def evidence(identifier=7, **changes):
    values = dict(
        mention_id=identifier, perfume_id=17, source_key="otzovik", source_url=URL,
        sentiment="positive", quote=QUOTE, published_at=NOW, observed_at=NOW, approval_id="qa-approval-1",
    )
    values.update(changes)
    return SummaryEvidence(**values)


def response_payload(text=None, *, status="completed", refusal=False):
    content = ({"type": "refusal", "refusal": "SYNTHETIC_PRIVATE_REFUSAL_BODY"} if refusal else {
        "type": "output_text", "text": text if text is not None else extraction().model_dump_json(),
        "annotations": [],
    })
    return {
        "id": "resp_qa_fixture", "object": "response", "created_at": 1794052800,
        "status": status, "model": "qa-offline-model", "error": None,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [{"type": "message", "id": "msg_qa_fixture", "status": "completed",
                    "role": "assistant", "content": [content]}],
    }


async def run_sdk(payload, requests):
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, json=payload)
    async with AsyncOpenAI(
        api_key="qa-synthetic-api-key", max_retries=0,
        base_url="https://example.test/v1", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
    ) as sdk:
        return await OpenAIResponsesClient(sdk).parse(
            model="qa-offline-model", input_text=TEXT, instructions="Extract only stated opinions.",
            schema=ReviewExtraction, max_output_tokens=400, timeout_seconds=3.0,
        )


def test_actual_sdk_serializes_store_false_and_strict_schema_without_private_metadata():
    requests = []
    result = asyncio.run(run_sdk(response_payload(), requests))
    assert result == extraction()
    assert len(requests) == 1
    wire = requests[0]
    assert wire["store"] is False
    assert wire["model"] == "qa-offline-model"
    assert wire["max_output_tokens"] == 400
    text_format = wire["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["strict"] is True
    assert text_format["schema"]["additionalProperties"] is False
    encoded = json.dumps(wire, ensure_ascii=False)
    assert TEXT in encoded
    for absent in ("user_id", "chat_id", "approval_id", "qa-synthetic-api-key", URL):
        assert absent not in encoded


@pytest.mark.parametrize(
    "payload,error",
    [
        (response_payload(status="incomplete"), IncompleteResponse),
        (response_payload(refusal=True), ModelRefusal),
        (response_payload(text="not json"), InvalidModelResponse),
        (response_payload(text='{"mentions":[],"invented":true}'), InvalidModelResponse),
        (response_payload(text='{"mentions":[{"perfume_name":"QA Cedar"}]}'), InvalidModelResponse),
    ],
)
def test_sdk_failure_paths_reject_untrusted_outputs_without_echoing_response(payload, error, caplog):
    requests = []
    with caplog.at_level(logging.INFO), pytest.raises(error) as raised:
        asyncio.run(run_sdk(payload, requests))
    assert len(requests) == 1
    for private in (TEXT, QUOTE, "SYNTHETIC_PRIVATE_REFUSAL_BODY", "qa-synthetic-api-key"):
        assert private not in caplog.text
        assert private not in str(raised.value)


def test_exact_catalog_match_resolves_without_creating_a_new_identity():
    result = validate_extraction(extraction(), review=review(), catalog=catalog())
    assert len(result.mentions) == 1
    resolved = result.mentions[0]
    assert (resolved.perfume_id, resolved.catalog_source_key, resolved.perfume_external_id) == (17, "dataset", "qa-cedar")
    assert resolved.mention.evidence_quote == QUOTE
    assert result.quarantine == ()


@pytest.mark.parametrize("name", ["QA Ceder", "Completely invented perfume"])
def test_fuzzy_or_unknown_names_are_quarantined(name):
    result = validate_extraction(extraction(mention(perfume_name=name)), review=review(), catalog=catalog())
    assert result.mentions == ()
    assert len(result.quarantine) == 1
    assert result.quarantine[0].reason == "unknown_perfume"


def test_name_without_house_must_be_unique_in_catalog():
    ambiguous = catalog() + [CatalogPerfume(id=18, name="QA Cedar", house="Other House", source_key="dataset", external_id="other")]
    result = validate_extraction(extraction(mention(perfume_house=None)), review=review(), catalog=ambiguous)
    assert result.mentions == ()
    assert result.quarantine[0].reason == "ambiguous_perfume"


def test_mismatching_house_is_never_ignored_when_resolving_names():
    result = validate_extraction(extraction(mention(perfume_house="Invented House")), review=review(), catalog=catalog())
    assert result.mentions == ()
    assert result.quarantine[0].reason == "unknown_perfume"


@pytest.mark.parametrize("quote", ["Нравится древесный аккорд", "нравится  древесный аккорд", "invented praise"])
def test_quote_must_be_an_exact_substring_of_accepted_input(quote):
    result = validate_extraction(extraction(mention(evidence_quote=quote)), review=review(), catalog=catalog())
    assert result.mentions == ()
    assert result.quarantine[0].reason == "invalid_quote"


def test_invalid_mention_does_not_discard_an_independent_valid_mention():
    result = validate_extraction(
        extraction(mention(), mention(perfume_name="Unknown")), review=review(), catalog=catalog()
    )
    assert len(result.mentions) == len(result.quarantine) == 1


def test_valid_catalog_name_cannot_claim_another_perfumes_verbatim_quote():
    text = "QA Rose от QA House: нравится розовый аккорд."
    result = validate_extraction(
        extraction(mention(evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert result.mentions == ()
    assert len(result.quarantine) == 1


@pytest.mark.parametrize("quoted_name", ["qa cedar", "ＱＡ Ｃｅｄａｒ", "QA   Cedar"])
def test_catalog_name_phrase_uses_nfkc_casefold_and_normalized_spacing(quoted_name):
    text = f"{quoted_name} от QA House: нравится древесный аккорд."
    result = validate_extraction(
        extraction(mention(evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert len(result.mentions) == 1
    assert result.quarantine == ()


@pytest.mark.parametrize("quoted_name", ["XQA Cedar", "QA Cedarwood", "QA Cedar_edition"])
def test_catalog_name_phrase_cannot_match_part_of_a_larger_word(quoted_name):
    text = f"{quoted_name} от QA House: нравится древесный аккорд."
    result = validate_extraction(
        extraction(mention(evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert result.mentions == ()
    assert len(result.quarantine) == 1


@pytest.mark.parametrize("model_name", ["QA Cedar", "Кью Эй Кедр"])
def test_explicit_trusted_catalog_alias_can_ground_and_resolve_a_mention(model_name):
    text = "Кью Эй Кедр от QA House: нравится древесный аккорд."
    entry = CatalogPerfume(
        id=17, name="QA Cedar", house="QA House", source_key="dataset", external_id="qa-cedar",
        aliases=("Кью Эй Кедр",),
    )
    result = validate_extraction(
        extraction(mention(perfume_name=model_name, evidence_quote=text)), review=review(text), catalog=[entry],
    )
    assert len(result.mentions) == 1
    assert result.mentions[0].perfume_id == 17
    assert result.quarantine == ()


def test_transliteration_is_not_generated_as_an_implicit_alias():
    text = "Кью Эй Кедр от QA House: нравится древесный аккорд."
    result = validate_extraction(
        extraction(mention(evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert result.mentions == ()
    assert len(result.quarantine) == 1


def test_supplied_house_requires_the_house_to_be_present_in_review_context():
    text = "QA Cedar: нравится древесный аккорд."
    result = validate_extraction(
        extraction(mention(evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert result.mentions == ()
    assert len(result.quarantine) == 1


def test_unique_catalog_name_can_resolve_when_model_does_not_supply_a_house():
    text = "QA Cedar: нравится древесный аккорд."
    result = validate_extraction(
        extraction(mention(perfume_house=None, evidence_quote=text)), review=review(text), catalog=catalog(),
    )
    assert len(result.mentions) == 1
    assert result.quarantine == ()


def test_identical_document_mentions_do_not_inflate_independent_review_count():
    result = validate_extraction(
        extraction(mention(), mention(confidence=0.1)), review=review(), catalog=catalog(),
    )
    assert len(result.mentions) == 1


def test_conflicting_document_sentiments_quarantine_only_the_conflicted_perfume():
    rose_quote = "QA Rose от QA House: нравится розовый аккорд"
    text = f"{TEXT} {rose_quote}."
    entries = catalog() + [CatalogPerfume(
        id=18, name="QA Rose", house="QA House", source_key="dataset", external_id="qa-rose",
    )]
    result = validate_extraction(
        extraction(mention(), mention(sentiment="negative"), mention(perfume_name="QA Rose", evidence_quote=rose_quote)),
        review=review(text), catalog=entries,
    )
    assert [item.perfume_id for item in result.mentions] == [18]
    assert result.quarantine


def checked_summary(items):
    return CardSummary(text=render_summary(items), evidence_mention_ids=[item.mention_id for item in items])


def validate_card(result, items, *, approvals=None, budget=4000):
    return validate_summary(
        result, perfume_id=17, evidence=items, approvals=approvals or {"otzovik": approval()},
        now=NOW, max_utf16_units=budget, freshness_seconds={"otzovik": 3600},
    )


def test_summary_is_exact_server_rendering_of_one_or_two_approved_quotes():
    items = [evidence(), evidence(8, sentiment="negative", quote="резкий старт")]
    result = checked_summary(items)
    assert result.text == f"Подтверждённые мнения:\nПоложительно: «{QUOTE}» [m7]\nОтрицательно: «резкий старт» [m8]"
    assert validate_card(result, items) == result


@pytest.mark.parametrize("ids", [[99], [7, 7], [7, 8, 9]])
def test_summary_rejects_unknown_duplicate_or_excess_evidence_ids(ids):
    items = [evidence(), evidence(8), evidence(9)]
    with pytest.raises(InvalidEvidence):
        validate_card(CardSummary(text="arbitrary", evidence_mention_ids=ids), items)


def test_valid_evidence_id_cannot_authorize_invented_facts():
    result = CardSummary(text="Выпущен в 2020 году, стоит 12345 рублей, ноты уд и жасмин.", evidence_mention_ids=[7])
    with pytest.raises(InvalidEvidence):
        validate_card(result, [evidence()])


@pytest.mark.parametrize(
    "changes",
    [{"perfume_id": 18}, {"approval_id": "wrong-approval"}, {"source_url": "https://example.test/unapproved"},
     {"observed_at": NOW - timedelta(hours=2)}, {"observed_at": NOW + timedelta(seconds=1)}],
)
def test_summary_rejects_wrong_perfume_approval_url_or_stale_evidence(changes):
    items = [evidence(**changes)]
    with pytest.raises((InvalidEvidence, AdmissionDenied)):
        validate_card(checked_summary(items), items)


def test_summary_rejects_revoked_approval_and_utf16_budget_overflow():
    items = [evidence(quote="🌲" * 10)]
    result = checked_summary(items)
    with pytest.raises((InvalidEvidence, AdmissionDenied)):
        validate_card(result, items, approvals={"otzovik": approval(enabled=False)})
    budget = len(result.text.encode("utf-16-le")) // 2 - 1
    with pytest.raises(InvalidEvidence):
        validate_card(result, items, budget=budget)


def test_unknown_publication_date_is_allowed_when_observation_is_fresh():
    items = [evidence(published_at=None)]
    assert validate_card(checked_summary(items), items).evidence_mention_ids == [7]


def test_summary_cannot_cite_evidence_without_quotation_rights():
    items = [evidence()]
    with pytest.raises((InvalidEvidence, AdmissionDenied)):
        validate_card(checked_summary(items), items, approvals={"otzovik": approval(allow_quotation=False)})


def test_cache_requires_explicit_test_opt_in_for_plain_sqlite(qa_engine):
    with pytest.raises(ValueError):
        SQLAlchemyLLMCache(qa_engine)


def cache_key(**changes):
    values = dict(task="extraction", model="qa-offline-model", prompt_version="1", schema_version="1", input_text=TEXT)
    values.update(changes)
    return build_cache_key(**values)


def test_cache_roundtrips_only_validated_structured_output_and_can_be_purged(qa_engine):
    cache = SQLAlchemyLLMCache(qa_engine, allow_plaintext_for_tests=True)
    assert cache.get(cache_key(), ReviewExtraction) is None
    cache.put(cache_key(), "extraction", "qa-offline-model", extraction())
    assert cache.get(cache_key(), ReviewExtraction) == extraction()
    cache.purge()
    assert cache.get(cache_key(), ReviewExtraction) is None


def test_persistent_claim_prevents_duplicate_calls_even_across_instances(tmp_path):
    first, second = FileClaimStore(tmp_path / "claims"), FileClaimStore(tmp_path / "claims")
    first.acquire(cache_key())
    with pytest.raises(DuplicateClaim):
        second.acquire(cache_key())
    first.mark(cache_key(), "succeeded")
    with pytest.raises(DuplicateClaim):
        second.acquire(cache_key())
    second.purge()
    second.acquire(cache_key())


def test_claim_files_contain_only_hash_and_status_metadata(tmp_path):
    claims_path = tmp_path / "claims"
    store = FileClaimStore(claims_path)
    store.acquire(cache_key())
    files = [path for path in claims_path.iterdir() if path.is_file()]
    assert files
    for path in files:
        content = path.read_text(encoding="utf-8")
        assert TEXT not in content
        assert QUOTE not in content
        assert URL not in content


def service_config(qa_config):
    values = qa_config.model_dump(mode="python")
    values["llm"] = {"provider": "openai", "enabled": True, "model": "qa-offline-model"}
    values["sources"]["otzovik"]["enabled"] = True
    values["stage2"] = {
        "enabled": True, "approvals_path": "qa-approvals.json", "claims_path": "qa-claims",
        "freshness_seconds": {"otzovik": 3600},
        "fetch": {"request_timeout_seconds": 2.0, "source_deadline_seconds": 4.0, "max_attempts": 1,
                  "retry_backoff_seconds": [], "min_request_interval_seconds": 0.0, "max_items_per_source": 5},
        "llm": {"request_timeout_seconds": 2.0, "max_attempts": 1, "retry_backoff_seconds": [],
                "max_input_chars": 4000, "max_output_tokens": 400, "max_requests_per_run": 10,
                "max_concurrent_requests": 2},
    }
    return type(qa_config).model_validate(values)


def test_service_cache_hit_avoids_second_sdk_call_and_revocation_blocks_reuse(qa_engine, qa_config, tmp_path):
    requests = []
    approvals = {"otzovik": approval()}
    async def exercise():
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json=response_payload())
        async with AsyncOpenAI(api_key="qa-synthetic-key", max_retries=0, base_url="https://example.test/v1",
                               http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))) as sdk:
            analyzer = ReviewAnalyzer(
                client=OpenAIResponsesClient(sdk), cache=SQLAlchemyLLMCache(qa_engine, allow_plaintext_for_tests=True),
                claims=FileClaimStore(tmp_path / "claims"), config=service_config(qa_config),
                approvals=approvals, clock=lambda: NOW, live_admitted=True,
            )
            first = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            second = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert first == second
            assert len(first.mentions) == 1
            approvals["otzovik"] = approval(enabled=False)
            with pytest.raises(AdmissionDenied):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
    asyncio.run(exercise())
    assert len(requests) == 1


def test_unadmitted_live_service_never_calls_sdk_even_with_enabled_config(qa_engine, qa_config, tmp_path):
    class UnexpectedClient:
        async def parse(self, **kwargs):
            raise AssertionError("SDK call before live admission")
    analyzer = ReviewAnalyzer(
        client=UnexpectedClient(), cache=SQLAlchemyLLMCache(qa_engine, allow_plaintext_for_tests=True),
        claims=FileClaimStore(tmp_path / "claims"), config=service_config(qa_config),
        approvals={"otzovik": approval()}, clock=lambda: NOW,
    )
    with pytest.raises((ProcessingDisabled, AdmissionDenied)):
        asyncio.run(analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW))


@asynccontextmanager
async def analyzer_environment(qa_engine, qa_config, tmp_path, *, handler=None, approvals=None, config=None):
    requests = []
    async def handle(request):
        requests.append(json.loads(request.content))
        if handler is not None:
            return await handler(request)
        return httpx2.Response(200, json=response_payload())
    async with AsyncOpenAI(api_key="qa-synthetic-key", max_retries=0, base_url="https://example.test/v1",
                           http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle))) as sdk:
        cache = SQLAlchemyLLMCache(qa_engine, allow_plaintext_for_tests=True)
        analyzer = ReviewAnalyzer(
            client=OpenAIResponsesClient(sdk), cache=cache, claims=FileClaimStore(tmp_path / "claims"),
            config=config or service_config(qa_config), approvals=approvals or {"otzovik": approval()},
            clock=lambda: NOW, live_admitted=True,
        )
        yield analyzer, cache, requests


def cache_rows(engine):
    with engine.connect() as connection:
        return connection.execute(select(LLMCacheEntry)).all()


@pytest.mark.parametrize(
    "changes",
    [{"enabled": False}, {"permission_status": "blocked"}, {"allow_llm": False},
     {"allow_retention": False}, {"approved_urls": ()}, {"expires_at": NOW - timedelta(seconds=1)}],
)
def test_source_gate_rejects_before_sdk_or_cache_write(qa_engine, qa_config, tmp_path, changes):
    async def exercise():
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, approvals={"otzovik": approval(**changes)}
        ) as (analyzer, cache, requests):
            with pytest.raises(AdmissionDenied):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert requests == []
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_same_input_concurrency_sends_at_most_one_billable_request(qa_engine, qa_config, tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            await release.wait()
            return httpx2.Response(200, json=response_payload())
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            first = asyncio.create_task(analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW))
            await asyncio.wait_for(entered.wait(), timeout=3)
            followers = [asyncio.create_task(analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)) for _ in range(3)]
            release.set()
            outcomes = await asyncio.gather(first, *followers, return_exceptions=True)
            assert len(requests) == 1
            assert any(not isinstance(value, Exception) for value in outcomes)
            assert all(not isinstance(value, Exception) or isinstance(value, DuplicateClaim) for value in outcomes)
    asyncio.run(exercise())
    assert len(cache_rows(qa_engine)) == 1


@pytest.mark.parametrize("kind", ["refusal", "incomplete", "invalid_json"])
def test_paid_failures_never_become_valid_cache_entries(qa_engine, qa_config, tmp_path, kind):
    async def exercise():
        payload = (response_payload(refusal=True) if kind == "refusal" else
                   response_payload(status="incomplete") if kind == "incomplete" else response_payload(text="invalid"))
        async def handle(request):
            return httpx2.Response(200, json=payload)
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            with pytest.raises((ModelRefusal, IncompleteResponse, InvalidModelResponse)):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            with pytest.raises(DuplicateClaim):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_invalid_quote_quarantine_is_safe_and_never_cached(qa_engine, qa_config, tmp_path):
    private_invalid_quote = "SYNTHETIC_NOT_IN_INPUT_PRIVATE_TEXT"
    async def exercise():
        payload = response_payload(text=extraction(mention(evidence_quote=private_invalid_quote)).model_dump_json())
        async def handle(request):
            return httpx2.Response(200, json=payload)
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            result = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert result.mentions == ()
            assert result.quarantine[0].reason == "invalid_quote"
            assert private_invalid_quote not in repr(result.quarantine)
            with pytest.raises(DuplicateClaim):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_network_timeout_is_uncertain_and_does_not_retry_or_cache(qa_engine, qa_config, tmp_path):
    async def exercise():
        async def handle(request):
            raise httpx2.ReadTimeout("SYNTHETIC_PRIVATE_TIMEOUT_BODY", request=request)
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            with pytest.raises(UncertainOutcome):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            with pytest.raises(DuplicateClaim):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_permission_revoked_during_provider_call_prevents_output_and_cache(qa_engine, qa_config, tmp_path):
    approvals = {"otzovik": approval()}
    async def exercise():
        async def handle(request):
            approvals["otzovik"] = approval(enabled=False)
            return httpx2.Response(200, json=response_payload())
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, handler=handle, approvals=approvals
        ) as (analyzer, cache, requests):
            with pytest.raises(AdmissionDenied):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_empty_summary_uses_no_sdk_request_or_cache_entry(qa_engine, qa_config, tmp_path):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            assert await analyzer.summarize(perfume_id=17, evidence=[], max_utf16_units=4000) is None
            assert requests == []
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_oversized_input_and_run_budget_are_enforced_before_an_additional_call(qa_engine, qa_config, tmp_path):
    config = service_config(qa_config)
    config.stage2.llm.max_requests_per_run = 1
    async def exercise():
        async with analyzer_environment(qa_engine, config, tmp_path, config=config) as (analyzer, cache, requests):
            with pytest.raises(LLMError):
                await analyzer.extract(source_key="otzovik", review=review("x" * 4001), catalog=catalog(), observed_at=NOW)
            assert requests == []
            await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            with pytest.raises(LLMError):
                await analyzer.extract(source_key="otzovik", review=review("Другой текст. " + TEXT), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())


def test_cache_hit_revalidates_against_current_catalog_instead_of_reusing_an_old_id(qa_engine, qa_config, tmp_path):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            first = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            revised_catalog = [CatalogPerfume(id=99, name="QA Cedar", house="QA House", source_key="dataset", external_id="new-id")]
            second = await analyzer.extract(source_key="otzovik", review=review(), catalog=revised_catalog, observed_at=NOW)
            assert first.mentions[0].perfume_id == 17
            assert second.mentions[0].perfume_id == 99
            assert len(requests) == 1
    asyncio.run(exercise())


def test_extract_observed_at_is_a_required_keyword():
    import inspect
    parameter = inspect.signature(ReviewAnalyzer.extract).parameters.get("observed_at")
    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("observed_at", [None, NOW.replace(tzinfo=None), NOW + timedelta(seconds=1), NOW - timedelta(seconds=3601)])
def test_invalid_observation_is_rejected_before_sdk_and_cache(qa_engine, qa_config, tmp_path, observed_at):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            with pytest.raises((InvalidEvidence, AdmissionDenied)):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=observed_at)
            assert requests == []
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


@pytest.mark.parametrize("published_at", [None, NOW - timedelta(days=3650)])
def test_fresh_fetch_is_not_replaced_by_publication_or_approval_time(qa_engine, qa_config, tmp_path, published_at):
    old_approval = approval(obtained_at=NOW - timedelta(days=365))
    record = review().model_copy(update={"published_at": published_at})
    async def exercise():
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, approvals={"otzovik": old_approval},
        ) as (analyzer, cache, requests):
            result = await analyzer.extract(source_key="otzovik", review=record, catalog=catalog(), observed_at=NOW)
            assert len(result.mentions) == 1
            assert len(requests) == 1
    asyncio.run(exercise())


@pytest.mark.parametrize("config_ttl,approval_ttl", [(3600, 30), (30, 3600)])
def test_observation_ttl_uses_the_stricter_configuration_and_approval_cap(
    qa_engine, qa_config, tmp_path, config_ttl, approval_ttl,
):
    config = service_config(qa_config)
    config.stage2.freshness_seconds["otzovik"] = config_ttl
    async def exercise():
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, config=config,
            approvals={"otzovik": approval(max_age_seconds=approval_ttl)},
        ) as (analyzer, cache, requests):
            with pytest.raises((InvalidEvidence, AdmissionDenied)):
                await analyzer.extract(
                    source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW - timedelta(seconds=31),
                )
            assert requests == []
    asyncio.run(exercise())


def test_cache_hit_rechecks_current_observation_freshness(qa_engine, qa_config, tmp_path):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            with pytest.raises((InvalidEvidence, AdmissionDenied)):
                await analyzer.extract(
                    source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW - timedelta(seconds=3601),
                )
            assert len(requests) == 1
    asyncio.run(exercise())


def test_observation_expiring_during_provider_call_cannot_enter_cache(qa_engine, qa_config, tmp_path):
    clock = [NOW]
    async def exercise():
        async def handle(request):
            clock[0] = NOW + timedelta(seconds=3601)
            return httpx2.Response(200, json=response_payload())
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            analyzer.clock = lambda: clock[0]
            with pytest.raises((InvalidEvidence, AdmissionDenied)):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


def test_source_rights_default_to_no_quotation_and_no_retention():
    policy = SourceApproval(
        source_key="otzovik", approval_id="qa-default-deny", enabled=True,
        permission_status="approved", allow_llm=True, scope="public_review",
    )
    assert policy.allow_quotation is False
    assert policy.allow_retention is False


def test_summary_cache_hit_rechecks_revoked_quotation_rights(qa_engine, qa_config, tmp_path):
    approvals = {"otzovik": approval()}
    items = [evidence()]
    async def exercise():
        async def handle(request):
            return httpx2.Response(200, json=response_payload(text=checked_summary(items).model_dump_json()))
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, handler=handle, approvals=approvals,
        ) as (analyzer, cache, requests):
            assert await analyzer.summarize(perfume_id=17, evidence=items, max_utf16_units=4000) == checked_summary(items)
            approvals["otzovik"] = approval(allow_quotation=False)
            with pytest.raises(AdmissionDenied):
                await analyzer.summarize(perfume_id=17, evidence=items, max_utf16_units=4000)
            assert len(requests) == 1
    asyncio.run(exercise())


def test_summary_revocation_inflight_prevents_a_valid_response_from_repopulating_cache(qa_engine, qa_config, tmp_path):
    approvals = {"otzovik": approval()}
    items = [evidence()]
    async def exercise():
        async def handle(request):
            approvals["otzovik"] = approval(allow_retention=False)
            return httpx2.Response(200, json=response_payload(text=checked_summary(items).model_dump_json()))
        async with analyzer_environment(
            qa_engine, qa_config, tmp_path, handler=handle, approvals=approvals,
        ) as (analyzer, cache, requests):
            with pytest.raises(AdmissionDenied):
                await analyzer.summarize(perfume_id=17, evidence=items, max_utf16_units=4000)
            assert len(requests) == 1
    asyncio.run(exercise())
    assert cache_rows(qa_engine) == []


@pytest.mark.parametrize("error_code", ["credit_balance_exhausted", "insufficient_quota"])
def test_confirmed_credit_rejection_has_no_backoff_and_releases_claim(
    qa_engine, qa_config, tmp_path, monkeypatch, error_code,
):
    config = service_config(qa_config)
    config.stage2.llm.max_attempts = 3
    config.stage2.llm.retry_backoff_seconds = [0.01, 0.01]
    async def unexpected_sleep(delay):
        raise AssertionError("Credit exhaustion must not trigger retry backoff")
    monkeypatch.setattr("llm.service.asyncio.sleep", unexpected_sleep)
    async def exercise():
        calls = 0
        async def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx2.Response(429, json={"error": {
                    "message": "SYNTHETIC_PRIVATE_PROVIDER_ERROR", "type": error_code, "code": error_code,
                }})
            return httpx2.Response(200, json=response_payload())
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle, config=config) as (analyzer, cache, requests):
            with pytest.raises(RequestRejected) as rejected:
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert rejected.value.retryable is False
            assert "SYNTHETIC_PRIVATE_PROVIDER_ERROR" not in str(rejected.value)
            assert len(requests) == 1
            assert cache_rows(qa_engine) == []
            result = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(result.mentions) == 1
            assert len(requests) == 2
    asyncio.run(exercise())


def test_local_purge_suspends_processing_and_removes_cache_and_claims(qa_engine, qa_config, tmp_path):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert cache_rows(qa_engine)
            assert list(analyzer.claims.directory.iterdir())
            analyzer.purge_local_llm_state()
            assert analyzer.live_admitted is False
            assert cache_rows(qa_engine) == []
            assert list(analyzer.claims.directory.iterdir()) == []
            with pytest.raises(AdmissionDenied):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())


def test_inflight_and_queued_calls_cannot_restore_purged_state_after_readmission(qa_engine, qa_config, tmp_path):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def handle(request):
            entered.set()
            await release.wait()
            return httpx2.Response(200, json=response_payload())
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            running = asyncio.create_task(analyzer.extract(
                source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW,
            ))
            await asyncio.wait_for(entered.wait(), timeout=3)
            queued = asyncio.create_task(analyzer.extract(
                source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW,
            ))
            await asyncio.sleep(0)
            analyzer.purge_local_llm_state()
            analyzer.live_admitted = True
            release.set()
            outcomes = await asyncio.gather(running, queued, return_exceptions=True)
            assert all(isinstance(outcome, AdmissionDenied) for outcome in outcomes)
            assert len(requests) == 1
            assert cache_rows(qa_engine) == []
            assert list(analyzer.claims.directory.iterdir()) == []
    asyncio.run(exercise())


def test_exact_unknown_quote_can_be_resolved_from_cache_when_catalog_is_expanded(qa_engine, qa_config, tmp_path):
    async def exercise():
        async with analyzer_environment(qa_engine, qa_config, tmp_path) as (analyzer, cache, requests):
            first = await analyzer.extract(source_key="otzovik", review=review(), catalog=[], observed_at=NOW)
            assert first.mentions == ()
            assert first.quarantine[0].reason == "unknown_perfume"
            second = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(second.mentions) == 1
            assert len(requests) == 1
    asyncio.run(exercise())


def test_shared_alias_without_house_is_quarantined_as_ambiguous():
    text = "Shared trusted name: нравится древесный аккорд."
    entries = [CatalogPerfume(
        id=index, name=f"QA Perfume {index}", house=f"QA House {index}", source_key="dataset",
        external_id=f"qa-{index}", aliases=("Shared trusted name",),
    ) for index in (17, 18)]
    result = validate_extraction(
        extraction(mention(perfume_name="Shared trusted name", perfume_house=None, evidence_quote=text)),
        review=review(text), catalog=entries,
    )
    assert result.mentions == ()
    assert len(result.quarantine) == 1
    assert result.quarantine[0].reason == "ambiguous_perfume"


def test_supplied_house_can_be_grounded_by_context_outside_the_exact_quote():
    quote = "QA Cedar: нравится древесный аккорд"
    text = f"Производитель QA House. {quote}."
    result = validate_extraction(
        extraction(mention(evidence_quote=quote)), review=review(text), catalog=catalog(),
    )
    assert len(result.mentions) == 1
    assert result.quarantine == ()


def test_unsupported_unknown_model_name_cannot_be_preserved_in_checked_cache(qa_engine, qa_config, tmp_path):
    async def exercise():
        async def handle(request):
            invented = extraction(mention(perfume_name="SYNTHETIC Invented Aroma"))
            return httpx2.Response(200, json=response_payload(text=invented.model_dump_json()))
        async with analyzer_environment(qa_engine, qa_config, tmp_path, handler=handle) as (analyzer, cache, requests):
            result = await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert result.mentions == ()
            assert result.quarantine[0].reason == "unknown_perfume"
            assert cache_rows(qa_engine) == []
            with pytest.raises(DuplicateClaim):
                await analyzer.extract(source_key="otzovik", review=review(), catalog=catalog(), observed_at=NOW)
            assert len(requests) == 1
    asyncio.run(exercise())
