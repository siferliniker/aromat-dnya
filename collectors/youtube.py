"""Creator-authorized official captions for exactly admitted public videos."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.parse import quote

from collectors.contracts import ReviewTextRecord, SourceBatch
from collectors.http import HttpRequest, HttpResponse, HttpTransport
from collectors.opinion_versions import CollectedReviews, ReviewSnapshot
from collectors.permissions import AdmissionRegistry, PermissionDenied
from config import AppConfig
from llm.policy import aware, require_nonprivate_text


_ROOT = "https://www.googleapis.com/youtube/v3"
RequestGate = Callable[[str, str | None], None]


class SourceRequestError(ValueError):
    """The official service could not supply this admitted observation."""


def _json(response: HttpResponse) -> dict:
    try:
        value = json.loads(response.body)
    except (ValueError, TypeError):
        raise SourceRequestError("invalid official source response") from None
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise SourceRequestError("invalid official source response")
    return value


def _published(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceRequestError("invalid publication timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SourceRequestError("invalid publication timestamp") from None
    if not aware(result):
        raise SourceRequestError("invalid publication timestamp")
    return result


def _srt(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise SourceRequestError("invalid official subtitle encoding") from None
    collected = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines or not re.fullmatch(
            r"\d{2,}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2,}:\d{2}:\d{2},\d{3}(?:\s+.*)?", lines[0],
        ):
            raise SourceRequestError("invalid official subtitle format")
        collected.extend(line.strip() for line in lines[1:] if line.strip())
    result = " ".join(collected)
    if not result:
        raise SourceRequestError("empty official subtitle text")
    return result


class YouTubeAdapter:
    source_key = "youtube"

    def __init__(
        self, config: AppConfig, registry: AdmissionRegistry, transport: HttpTransport,
        oauth_access_token: str, video_ids: Sequence[str], clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.config, self.registry, self.transport = config, registry, transport
        self._token, self.video_ids, self.clock, self.sleep = oauth_access_token, video_ids, clock, sleep

    def _admit(self, source_url: str | None = None, blogger_external_id: str | None = None):
        configured = self.config.sources.get(self.source_key)
        if (self.config.stage2 is None or not self.config.stage2.enabled or self.config.stage2.fetch is None
                or configured is None or not configured.enabled or configured.kind != "blog"):
            raise PermissionDenied("YouTube opinion source is disabled")
        now = self.clock()
        admission = self.registry.require(self.source_key, "fetch", now=now,
            source_url=source_url, blogger_external_id=blogger_external_id)
        if admission.platform != "youtube" or admission.kind != "blog" or admission.scope != "public_review":
            raise PermissionDenied("YouTube source has not been admitted for public creator content")
        return admission

    def _retry_after(self, response: HttpResponse) -> float:
        value = next((value for name, value in response.headers.items() if name.lower() == "retry-after"), None)
        if value is None:
            return 0.0
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                deadline = parsedate_to_datetime(value)
                seconds = (deadline - self.clock()).total_seconds() if aware(deadline) else math.inf
            except (ValueError, TypeError, OverflowError):
                seconds = math.inf
        return max(0.0, seconds) if math.isfinite(seconds) else math.inf

    async def _request(
        self, path: str, params: dict[str, str], *, deadline: float, last_request: list,
        source_url: str, request_gate: RequestGate | None, blogger_external_id: str | None = None,
    ) -> HttpResponse:
        policy = self.config.stage2.fetch
        loop = asyncio.get_running_loop()
        for attempt in range(policy.max_attempts):
            self._admit(source_url, blogger_external_id)
            delay = max(0.0, policy.min_request_interval_seconds - (loop.time() - last_request[0])) if last_request else 0.0
            if delay:
                if loop.time() + delay >= deadline:
                    raise SourceRequestError("source deadline exhausted")
                await self.sleep(delay)
            self._admit(source_url, blogger_external_id)
            if request_gate is not None:
                request_gate(source_url, blogger_external_id)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SourceRequestError("source deadline exhausted")
            timeout = min(policy.request_timeout_seconds, remaining)
            request = HttpRequest("GET", f"{_ROOT}/{path}", params,
                                  {"Authorization": f"Bearer {self._token}"}, timeout)
            last_request[:] = [loop.time()]
            retry_after = 0.0
            try:
                # Enter the transport in the same task as the gate check.
                async with asyncio.timeout(timeout):
                    response = await self.transport.request(request)
            except (TimeoutError, ConnectionError, OSError):
                response = None
            if response is not None:
                if type(response.status_code) is not int:
                    raise SourceRequestError("invalid official source status")
                if response.status_code == 200:
                    return response
                if response.status_code != 429 and not 500 <= response.status_code <= 599:
                    raise SourceRequestError("official source request was denied")
                retry_after = self._retry_after(response)
            if attempt + 1 >= policy.max_attempts:
                raise SourceRequestError("source request attempts exhausted")
            delay = max(policy.retry_backoff_seconds[attempt], retry_after)
            if not math.isfinite(delay) or loop.time() + delay >= deadline:
                raise SourceRequestError("source retry exceeds its deadline")
            if delay:
                await self.sleep(delay)
        raise SourceRequestError("source request attempts exhausted")

    async def collect_snapshots(self, *, request_gate: RequestGate | None = None) -> CollectedReviews:
        self._admit()
        if (not isinstance(self._token, str) or not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", self._token)
                or not isinstance(self.video_ids, Sequence) or isinstance(self.video_ids, (str, bytes))
                or any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is None
                       for value in self.video_ids)):
            raise PermissionDenied("valid creator OAuth and explicit video identifiers are required")
        videos = tuple(dict.fromkeys(self.video_ids))
        for video_id in videos:
            self._admit(f"https://www.youtube.com/watch?v={video_id}")
        policy = self.config.stage2.fetch
        deadline, last_request = asyncio.get_running_loop().time() + policy.source_deadline_seconds, []
        snapshots = []
        async with asyncio.timeout(policy.source_deadline_seconds):
            for video_id in videos[:policy.max_items_per_source]:
                source_url = f"https://www.youtube.com/watch?v={video_id}"
                payload = _json(await self._request("videos", {"part": "snippet,status", "id": video_id},
                    deadline=deadline, last_request=last_request, source_url=source_url, request_gate=request_gate))
                if len(payload["items"]) != 1 or not isinstance(payload["items"][0], dict):
                    raise SourceRequestError("selected video metadata is absent")
                video = payload["items"][0]
                snippet, status = video.get("snippet"), video.get("status")
                if (video.get("id") != video_id or not isinstance(snippet, dict) or not isinstance(status, dict)
                        or status.get("privacyStatus") != "public"):
                    raise PermissionDenied("selected video must be the admitted public resource")
                channel_id = snippet.get("channelId")
                if not isinstance(channel_id, str):
                    raise PermissionDenied("video channel identity is absent")
                self._admit(source_url, channel_id)
                publication = _published(snippet.get("publishedAt"))
                tracks = _json(await self._request("captions", {"part": "snippet", "videoId": video_id},
                    deadline=deadline, last_request=last_request, source_url=source_url,
                    request_gate=request_gate, blogger_external_id=channel_id))["items"]
                allowed_tracks = []
                for track in tracks:
                    details = track.get("snippet") if isinstance(track, dict) else None
                    if not isinstance(details, dict) or details.get("videoId") != video_id:
                        raise SourceRequestError("caption metadata does not match the selected video")
                    if details.get("isDraft") is not False or details.get("status") == "failed":
                        continue
                    track_id = track.get("id")
                    if (not isinstance(track_id, str) or not track_id.strip() or track_id in (".", "..")
                            or any(ord(char) < 32 or ord(char) == 127 for char in track_id)):
                        raise SourceRequestError("invalid caption track identity")
                    allowed_tracks.append(track)
                if not allowed_tracks:
                    continue
                selected = min(allowed_tracks, key=lambda value: value["id"])
                self._admit(source_url, channel_id)
                response = await self._request(f"captions/{quote(selected['id'], safe='')}", {"tfmt": "srt"},
                    deadline=deadline, last_request=last_request, source_url=source_url,
                    request_gate=request_gate, blogger_external_id=channel_id)
                text = _srt(response.body)
                require_nonprivate_text(text)
                if self.config.stage2.llm is None or len(text) > self.config.stage2.llm.max_input_chars:
                    raise SourceRequestError("caption text exceeds the configured processing bound")
                observed_at = self.clock()
                admission = self._admit(source_url, channel_id)
                review = ReviewTextRecord(external_id=video_id, source_url=source_url,
                    blogger_external_id=channel_id, published_at=publication, text=text)
                snapshots.append(ReviewSnapshot(review, observed_at, admission.approval_ref, admission.approved_at))
        return CollectedReviews(self.source_key, tuple(snapshots))

    async def collect(self) -> SourceBatch:
        result = await self.collect_snapshots()
        return SourceBatch(source_key=self.source_key, review_texts=[item.review for item in result.snapshots])
