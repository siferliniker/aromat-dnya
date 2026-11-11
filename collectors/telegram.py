"""Consume already delivered public channel updates, without another poller."""

from collections.abc import Callable
from datetime import datetime, timezone
import re

from collectors.contracts import ReviewTextRecord
from collectors.opinion_versions import ReviewSnapshot
from collectors.permissions import AdmissionRegistry, PermissionDenied
from config import AppConfig
from llm.policy import require_nonprivate_text


class TelegramAdapter:
    source_key = "telegram"

    def __init__(self, config: AppConfig, registry: AdmissionRegistry, clock: Callable[[], datetime]):
        self.config, self.registry, self.clock = config, registry, clock

    def consume(self, update: object) -> ReviewSnapshot | None:
        configured = self.config.sources.get(self.source_key)
        if (self.config.stage2 is None or not self.config.stage2.enabled or configured is None
                or not configured.enabled or configured.kind != "blog"):
            return None
        if hasattr(update, "model_dump"):
            update = update.model_dump(mode="python", exclude_none=True)
        if not isinstance(update, dict):
            return None
        post = update.get("channel_post") or update.get("edited_channel_post")
        if not isinstance(post, dict):
            return None
        chat, message_id = post.get("chat"), post.get("message_id")
        if (not isinstance(chat, dict) or chat.get("type") != "channel"
                or type(chat.get("id")) is not int or type(message_id) is not int or message_id <= 0
                or not isinstance(chat.get("username"), str)
                or re.fullmatch(r"[A-Za-z0-9_]{5,32}", chat["username"]) is None):
            return None
        text = post.get("text", post.get("caption"))
        if not isinstance(text, str) or not text.strip():
            return None
        source_url = f"https://t.me/{chat['username']}/{message_id}"
        now = self.clock()
        try:
            admission = self.registry.require(self.source_key, "fetch", now=now,
                source_url=source_url, blogger_external_id=str(chat["id"]))
            if admission.platform != "telegram" or admission.kind != "blog" or admission.scope != "public_review":
                return None
            blogger = next((item for item in admission.bloggers if item.external_id == str(chat["id"])), None)
            if blogger is None or blogger.channel_url != f"https://t.me/{chat['username']}":
                return None
            require_nonprivate_text(text)
        except (PermissionDenied, ValueError):
            return None
        if self.config.stage2.llm is None or len(text.strip()) > self.config.stage2.llm.max_input_chars:
            return None
        published_at = post.get("date")
        if type(published_at) is int:
            try:
                published_at = datetime.fromtimestamp(published_at, timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
        elif published_at is not None and (not isinstance(published_at, datetime) or published_at.utcoffset() is None):
            return None
        review = ReviewTextRecord(external_id=f"{chat['id']}:{message_id}", source_url=source_url,
            blogger_external_id=str(chat["id"]), published_at=published_at, text=text)
        return ReviewSnapshot(review, now, admission.approval_ref, admission.approved_at)
