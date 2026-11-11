"""Validate Telegram initData before trusting its user identity.

HMAC algorithm: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


# A user can browse for one hour, then must reopen the app from Telegram.
INIT_DATA_TTL_SECONDS = 60 * 60
MAX_INIT_DATA_BYTES = 16 * 1024


class AuthError(ValueError):
    def __init__(self, *, forbidden: bool = False):
        super().__init__("Telegram authorization is required")
        self.forbidden = forbidden


def validate_init_data(raw: str, bot_token: str, owner_id: int, *, now: int | float | None = None) -> int:
    """Return only the verified owner ID, without retaining personal Telegram fields."""
    if (not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > MAX_INIT_DATA_BYTES
            or re.search(r"%(?![0-9a-fA-F]{2})", raw)):
        raise AuthError()
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=32)
    except (UnicodeError, ValueError):
        raise AuthError() from None
    values = dict(pairs)
    if (len(pairs) != len(values) or not all(key and "\n" not in key and "\r" not in key for key in values)
            or not {"hash", "auth_date", "user"}.issubset(values)
            or re.fullmatch(r"[0-9a-fA-F]{64}", values["hash"]) is None):
        raise AuthError()
    check = "\n".join(f"{key}={value}" for key, value in sorted(pairs) if key != "hash")
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, values["hash"].lower()):
        raise AuthError()
    try:
        if re.fullmatch(r"[0-9]{1,12}", values["auth_date"]) is None:
            raise ValueError()
        auth_date = int(values["auth_date"])
        timestamp = time.time() if now is None else now
        if auth_date > timestamp or timestamp - auth_date > INIT_DATA_TTL_SECONDS:
            raise ValueError()
        user = json.loads(values["user"])
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
            raise ValueError()
        if user.get("is_bot") is True:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise AuthError() from None
    if user["id"] != owner_id:
        raise AuthError(forbidden=True)
    return owner_id
