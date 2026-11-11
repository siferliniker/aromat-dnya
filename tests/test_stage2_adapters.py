"""Tests-first offline adapters: all requests terminate in FakeTransport."""

import asyncio
from datetime import timedelta
import json
from time import monotonic

import pytest

from collectors.contracts import SourceBatch, collect_sources
from collectors.http import HttpRequest, HttpResponse
from collectors.opinion_versions import CollectedReviews, ReviewSnapshot
from collectors.permissions import AdmissionRegistry
from collectors.telegram import TelegramAdapter
from collectors.youtube import YouTubeAdapter
from test_stage2_data import APPROVED, NOW, OP_NAMES, admission, data_config


VIDEO = "qaVideo0001"
SECOND_VIDEO = "qaVideo0002"
WATCH = f"https://www.youtube.com/watch?v={VIDEO}"
SECOND_WATCH = f"https://www.youtube.com/watch?v={SECOND_VIDEO}"
CHANNEL = "UCqaSyntheticAuthor1"
TELEGRAM_CHANNEL = "-1001234567890"
TELEGRAM_URL = "https://t.me/qa_synthetic_channel/17"
QUOTE = "QA perfume 1 от QA house: нравится древесный аккорд"
SRT = f"1\n00:00:00,000 --> 00:00:03,500\n{QUOTE}\n\n".encode()
TOKEN = "qa-synthetic-oauth-token"
ROOT = "https://www.googleapis.com/youtube/v3"


def response(payload=None, *, status=200, headers=None, body=None):
    return HttpResponse(
        status_code=status, headers=headers or {},
        body=body if body is not None else json.dumps(payload).encode(),
    )


def videos(video_id=VIDEO, channel=CHANNEL):
    return response({"items": [{"id": video_id, "status": {"privacyStatus": "public"}, "snippet": {
        "channelId": channel, "publishedAt": "2026-11-07T08:00:00Z", "title": "Synthetic review",
    }}]})


def captions(video_id=VIDEO, track_id="qa-track-1"):
    return response({"items": [{"id": track_id, "snippet": {
        "videoId": video_id, "language": "ru", "trackKind": "standard", "isDraft": False,
    }}]})


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def request(self, request):
        assert isinstance(request, HttpRequest)
        self.requests.append(request)
        assert self.responses, "Unexpected source request beyond the configured fixture"
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def youtube_policy(**changes):
    return admission("youtube", **(dict(
        approved_urls=[WATCH, SECOND_WATCH],
        bloggers=[dict(external_id=CHANNEL, name="Synthetic Author",
                       channel_url=f"https://www.youtube.com/channel/{CHANNEL}", weight=0.75)],
    ) | changes))


def youtube(qa_config, transport, *, policies=None, video_ids=(VIDEO,), clock=lambda: NOW, sleep=None, **changes):
    config = data_config(qa_config)
    for key, value in changes.items():
        setattr(config.stage2.fetch, key, value)
    kwargs = dict(config=config, registry=AdmissionRegistry(policies or [youtube_policy()]),
                  transport=transport, oauth_access_token=TOKEN, video_ids=video_ids, clock=clock)
    if sleep is not None:
        kwargs["sleep"] = sleep
    return YouTubeAdapter(**kwargs)


def telegram(qa_config, *, policies=None, clock=lambda: NOW):
    policy = admission("telegram", approved_urls=[TELEGRAM_URL], bloggers=[dict(
        external_id=TELEGRAM_CHANNEL, name="Synthetic Channel",
        channel_url="https://t.me/qa_synthetic_channel", weight=0.75,
    )])
    return TelegramAdapter(config=data_config(qa_config), registry=AdmissionRegistry(policies or [policy]), clock=clock)


def update(*, field="channel_post", text=QUOTE, **post_changes):
    return {"update_id": 1, field: dict(
        message_id=17, date=int((NOW - timedelta(days=1)).timestamp()),
        chat={"id": int(TELEGRAM_CHANNEL), "type": "channel", "username": "qa_synthetic_channel"},
        text=text,
    ) | post_changes}


def test_youtube_official_oauth_creator_captions_produce_frozen_contract_and_fetched_time(qa_config):
    transport = FakeTransport(videos(), captions(), response(body=SRT))
    result = asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert isinstance(result, CollectedReviews)
    assert result.source_key == "youtube"
    assert len(result.snapshots) == 1
    snapshot = result.snapshots[0]
    assert isinstance(snapshot, ReviewSnapshot)
    assert snapshot.review.source_url == WATCH
    assert snapshot.review.blogger_external_id == CHANNEL
    assert snapshot.review.text == QUOTE
    assert snapshot.observed_at == NOW
    assert snapshot.review.published_at == NOW.replace(day=7, hour=8)
    assert snapshot.approval_ref == "qa-approval-youtube"
    assert snapshot.approved_at == APPROVED
    assert [item.url for item in transport.requests] == [f"{ROOT}/videos", f"{ROOT}/captions", f"{ROOT}/captions/qa-track-1"]
    for request in transport.requests:
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in request.url
        assert TOKEN not in json.dumps(dict(request.params))
        assert 0 < request.timeout_seconds <= 2.0
    assert set(transport.requests[0].params["part"].split(",")) == {"snippet", "status"}
    assert transport.requests[0].params["id"] == VIDEO
    assert transport.requests[1].params["videoId"] == VIDEO
    assert transport.requests[2].params["tfmt"] == "srt"


def test_youtube_collect_obeys_source_batch_without_fabricated_mentions(qa_config):
    transport = FakeTransport(videos(), captions(), response(body=SRT))
    batch = asyncio.run(youtube(qa_config, transport).collect())
    assert isinstance(batch, SourceBatch)
    assert batch.source_key == "youtube"
    assert len(batch.review_texts) == 1
    assert batch.review_texts[0].text == QUOTE
    assert batch.mentions == batch.ratings == batch.perfumes == batch.offers == []


@pytest.mark.parametrize("policy_changes", [
    {"enabled": False}, {"permission_status": "blocked"}, {"expires_at": NOW},
    {"operations": {name: name != "fetch" for name in OP_NAMES}},
    {"approved_urls": [WATCH + "&unapproved=1"]},
    {"platform": "manual"},
    {"scope": "manual_nonprivate"},
])
def test_youtube_denies_unapproved_fetch_before_any_transport_call(qa_config, policy_changes):
    transport = FakeTransport()
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport, policies=[youtube_policy(**policy_changes)]).collect_snapshots())
    assert transport.requests == []


def test_all_selected_youtube_urls_are_gated_before_first_network_request(qa_config):
    transport = FakeTransport()
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport, policies=[youtube_policy(approved_urls=[WATCH])],
                            video_ids=(VIDEO, SECOND_VIDEO)).collect_snapshots())
    assert transport.requests == []


def test_youtube_configuration_disabled_prevents_network_even_with_rights(qa_config):
    transport = FakeTransport()
    adapter = youtube(qa_config, transport)
    adapter.config.sources["youtube"].enabled = False
    with pytest.raises(ValueError):
        asyncio.run(adapter.collect_snapshots())
    assert transport.requests == []


@pytest.mark.parametrize("video_id", ["", "short", "../captions", VIDEO + "&key=unapproved", "https://example.test/video"])
def test_invalid_video_ids_never_become_request_parameters(qa_config, video_id):
    transport = FakeTransport()
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport, video_ids=(video_id,)).collect_snapshots())
    assert transport.requests == []


def test_noncreator_channel_metadata_never_triggers_caption_request(qa_config):
    transport = FakeTransport(videos(channel="UCnotAdmitted"))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 1


def test_mismatched_video_metadata_never_triggers_caption_request(qa_config):
    transport = FakeTransport(videos(video_id=SECOND_VIDEO))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 1


@pytest.mark.parametrize("privacy", ["private", "unlisted"])
def test_nonpublic_video_metadata_never_triggers_caption_request(qa_config, privacy):
    payload = {"items": [{"id": VIDEO, "status": {"privacyStatus": privacy}, "snippet": {"channelId": CHANNEL}}]}
    transport = FakeTransport(response(payload))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 1


def test_unknown_video_visibility_is_not_assumed_public(qa_config):
    transport = FakeTransport(response({"items": [{"id": VIDEO, "snippet": {"channelId": CHANNEL}}]}))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 1


def test_caption_track_for_another_video_cannot_be_downloaded(qa_config):
    transport = FakeTransport(videos(), captions(video_id=SECOND_VIDEO))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 2


@pytest.mark.parametrize("track_id", [".", ".."])
def test_caption_dot_segment_cannot_change_official_download_resource(qa_config, track_id):
    transport = FakeTransport(videos(), captions(track_id=track_id))
    with pytest.raises(ValueError):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 2


def test_no_captions_means_no_unofficial_transcript_fallback(qa_config):
    transport = FakeTransport(videos(), response({"items": []}))
    result = asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert result.snapshots == ()
    assert len(transport.requests) == 2


def test_definitive_permission_denial_is_not_retried_and_body_is_not_logged(qa_config, caplog):
    canary = "QA_PRIVATE_PLATFORM_ERROR_BODY"
    transport = FakeTransport(response(status=403, payload={"error": canary}))
    with pytest.raises(Exception):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 1
    assert canary not in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("first", [response(status=429, payload={}), response(status=503, payload={}), TimeoutError("Synthetic timeout")])
def test_retryable_failures_use_explicit_attempt_bound(qa_config, first):
    transport = FakeTransport(first, videos(), captions(), response(body=SRT))
    result = asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(result.snapshots) == 1
    assert len(transport.requests) == 4
    assert transport.requests[0].url == transport.requests[1].url


def test_youtube_attempt_limit_stops_permanent_transient_failure(qa_config):
    transport = FakeTransport(response(status=503), response(status=503))
    with pytest.raises(Exception):
        asyncio.run(youtube(qa_config, transport).collect_snapshots())
    assert len(transport.requests) == 2


def test_actual_request_timeout_cancels_nonresponsive_fake_transport(qa_config):
    requests = []
    class NeverResponds:
        async def request(self, request):
            requests.append(request)
            await asyncio.Event().wait()
    adapter = youtube(qa_config, NeverResponds(), request_timeout_seconds=0.02,
                      source_deadline_seconds=0.2, max_attempts=1, retry_backoff_seconds=[])
    started = monotonic()
    with pytest.raises(Exception):
        asyncio.run(asyncio.wait_for(adapter.collect_snapshots(), timeout=1.5))
    assert monotonic() - started < 0.5
    assert len(requests) == 1


def test_explicit_minimum_interval_applies_to_video_caption_and_download_requests(qa_config):
    waits = []
    async def sleep(delay):
        waits.append(delay)
    transport = FakeTransport(videos(), captions(), response(body=SRT))
    result = asyncio.run(youtube(qa_config, transport, sleep=sleep, min_request_interval_seconds=0.25).collect_snapshots())
    assert len(result.snapshots) == 1
    assert len(waits) >= 2
    assert sum(waits) >= 0.45


@pytest.mark.parametrize("policy_changes", [
    {"permission_status": "blocked"}, {"approved_urls": [SECOND_WATCH]}, {"bloggers": []},
])
def test_permission_is_rechecked_after_rate_limit_wait_before_network(qa_config, policy_changes):
    transport = FakeTransport(videos())
    adapter = None
    async def revoke_during_wait(delay):
        adapter.registry = AdmissionRegistry([youtube_policy(**policy_changes)])
    adapter = youtube(qa_config, transport, sleep=revoke_during_wait, min_request_interval_seconds=0.25)
    with pytest.raises(Exception):
        asyncio.run(adapter.collect_snapshots())
    assert len(transport.requests) == 1


def test_retry_after_beyond_source_deadline_stops_without_sleep_or_extra_request(qa_config):
    waits = []
    async def sleep(delay):
        waits.append(delay)
    transport = FakeTransport(response(status=429, headers={"Retry-After": "600"}))
    with pytest.raises(Exception):
        asyncio.run(youtube(qa_config, transport, sleep=sleep).collect_snapshots())
    assert len(transport.requests) == 1
    assert waits == []


def test_max_items_limits_youtube_review_count_and_network_requests(qa_config):
    transport = FakeTransport(videos(), captions(), response(body=SRT))
    result = asyncio.run(youtube(qa_config, transport, video_ids=(VIDEO, SECOND_VIDEO), max_items_per_source=1).collect_snapshots())
    assert len(result.snapshots) == 1
    assert len(transport.requests) == 3


@pytest.mark.parametrize("field", ["channel_post", "edited_channel_post"])
def test_telegram_consumes_only_explicit_supplied_channel_update(qa_config, field):
    snapshot = telegram(qa_config).consume(update(field=field))
    assert isinstance(snapshot, ReviewSnapshot)
    assert snapshot.review.source_url == TELEGRAM_URL
    assert snapshot.review.blogger_external_id == TELEGRAM_CHANNEL
    assert snapshot.review.text == QUOTE
    assert snapshot.observed_at == NOW
    assert snapshot.review.published_at == NOW - timedelta(days=1)
    assert snapshot.approved_at == APPROVED
    assert snapshot.approval_ref == "qa-approval-telegram"


def test_telegram_media_caption_is_admitted_as_text_without_fabricated_publication_time(qa_config):
    payload = update()
    payload["channel_post"].pop("text")
    payload["channel_post"]["caption"] = QUOTE
    payload["channel_post"].pop("date")
    snapshot = telegram(qa_config).consume(payload)
    assert snapshot.review.text == QUOTE
    assert snapshot.review.published_at is None


@pytest.mark.parametrize("payload", [
    {"update_id": 1}, update(field="message"), update(field="edited_message"),
    update(chat={"id": int(TELEGRAM_CHANNEL), "type": "private", "username": "qa_synthetic_channel"}),
    update(chat={"id": int(TELEGRAM_CHANNEL), "type": "supergroup", "username": "qa_synthetic_channel"}),
    update(chat={"id": int(TELEGRAM_CHANNEL), "type": "channel"}),
    update(chat={"id": -1009999999999, "type": "channel", "username": "qa_synthetic_channel"}),
    update(chat={"id": int(TELEGRAM_CHANNEL), "type": "channel", "username": "qa_synthetic_channel_lookalike"}),
    update(message_id=18), update(text="   "),
])
def test_telegram_ignores_private_untrusted_or_nonexact_updates(qa_config, payload):
    assert telegram(qa_config).consume(payload) is None


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"permission_status": "blocked"}, {"expires_at": NOW},
    {"operations": {name: name != "fetch" for name in OP_NAMES}},
    {"platform": "manual"}, {"scope": "manual_nonprivate"},
])
def test_telegram_rejects_missing_operation_or_mislabeled_platform_scope(qa_config, changes):
    policy = admission("telegram", **(dict(approved_urls=[TELEGRAM_URL], bloggers=[dict(
        external_id=TELEGRAM_CHANNEL, name="Synthetic Channel",
        channel_url="https://t.me/qa_synthetic_channel", weight=0.75,
    )]) | changes))
    assert telegram(qa_config, policies=[policy]).consume(update()) is None


def two_telegram_channel_policy():
    return admission("telegram", approved_urls=[TELEGRAM_URL, "https://t.me/qa_second_channel/17"], bloggers=[
        dict(external_id=TELEGRAM_CHANNEL, name="Synthetic First", channel_url="https://t.me/qa_synthetic_channel", weight=0.75),
        dict(external_id="-1001234567891", name="Synthetic Second", channel_url="https://t.me/qa_second_channel", weight=0.5),
    ])


def test_identical_message_numbers_in_distinct_channels_are_distinct_documents(qa_config):
    adapter = telegram(qa_config, policies=[two_telegram_channel_policy()])
    first = adapter.consume(update())
    second = adapter.consume(update(chat={"id": -1001234567891, "type": "channel", "username": "qa_second_channel"}))
    assert first is not None and second is not None
    assert first.review.external_id != second.review.external_id


def test_telegram_channel_identity_and_url_must_match_the_same_admitted_channel(qa_config):
    adapter = telegram(qa_config, policies=[two_telegram_channel_policy()])
    mismatched = update(chat={"id": -1001234567891, "type": "channel", "username": "qa_synthetic_channel"})
    assert adapter.consume(mismatched) is None


def test_failure_of_youtube_does_not_block_another_source(qa_config):
    transport = FakeTransport(response(status=403))
    class SyntheticHealthySource:
        source_key = "manual"
        async def collect(self):
            return SourceBatch(source_key="manual")
    results = asyncio.run(collect_sources([youtube(qa_config, transport), SyntheticHealthySource()]))
    assert [item.status for item in results] == ["failed", "ok"]
    assert [item.source_key for item in results] == ["youtube", "manual"]
