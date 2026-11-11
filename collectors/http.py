"""Injectable HTTPS transport with explicit request timeouts and no redirects."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    params: Mapping[str, str]
    headers: Mapping[str, str] = field(repr=False)
    timeout_seconds: float


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


class HttpTransport(Protocol):
    request: Callable[[HttpRequest], Awaitable[HttpResponse]]


class TransportUnavailable(ConnectionError):
    """An HTTPS connection failed without exposing its request credentials."""


class AiohttpTransport:
    async def request(self, request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        if (request.method != "GET" or parsed.scheme != "https" or parsed.hostname != "www.googleapis.com"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not parsed.path.startswith("/youtube/v3/") or request.timeout_seconds <= 0):
            raise ValueError("transport only supports the admitted official HTTPS service")
        try:
            timeout = aiohttp.ClientTimeout(total=request.timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(request.url, params=request.params, headers=request.headers,
                                       allow_redirects=False) as response:
                    return HttpResponse(response.status, dict(response.headers), await response.read())
        except aiohttp.ClientError:
            raise TransportUnavailable("source connection failed") from None
