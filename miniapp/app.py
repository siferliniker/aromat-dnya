"""HTTP shell and owner-only API. No Telegram sending or polling runs here."""

import asyncio
import json
from pathlib import Path

from aiohttp import web
from sqlalchemy.engine import Engine

from config import AppConfig
from miniapp.auth import AuthError, validate_init_data
from miniapp.repository import DEFAULT_CATALOG_PATH, IMAGE_ORIGINS, MiniAppRepository, REACTIONS


STATIC_PATH = Path(__file__).resolve().parent / "static"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' " + " ".join(IMAGE_ORIGINS) + "; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org https://*.web.telegram.org",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}


def create_app(*, engine: Engine, bot_token: str, owner_id: int,
               config: AppConfig | None = None, catalog_path: str | Path = DEFAULT_CATALOG_PATH) -> web.Application:
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(bot_token, str) or not bot_token:
        raise ValueError("Telegram owner credentials are required")
    repository = MiniAppRepository(engine, config, catalog_path)

    @web.middleware
    async def guard(request, handler):
        try:
            if request.path.startswith("/api/"):
                auth = request.headers.getall("Authorization", [])
                if len(auth) != 1 or not auth[0].startswith("tma "):
                    raise AuthError()
                validate_init_data(auth[0][4:], bot_token, owner_id)
            response = await handler(request)
        except AuthError as error:
            response = web.json_response({"error": "access_denied" if error.forbidden else "authorization_required"},
                                         status=403 if error.forbidden else 401)
        except web.HTTPException as error:
            response = web.json_response({"error": "request_too_large" if error.status == 413 else "not_found"
                                         if error.status == 404 else "invalid_request"}, status=error.status)
        except (ValueError, TypeError, json.JSONDecodeError):
            response = web.json_response({"error": "invalid_request"}, status=400)
        except Exception:
            # Exceptions can contain SQL parameters or Telegram initData. Never log them.
            response = web.json_response({"error": "temporarily_unavailable"}, status=503)
        response.headers.update(SECURITY_HEADERS)
        return response

    app = web.Application(middlewares=[guard], client_max_size=4096)

    async def shell(_request):
        return web.FileResponse(STATIC_PATH / "index.html")

    async def health(_request):
        return web.json_response({"status": "ok"})

    async def state(_request):
        return web.json_response(await asyncio.to_thread(repository.state, owner_id))

    async def library(request):
        if len(request.query.getall("kind", [])) != 1 or request.query.get("kind") not in ("wishlist", "known"):
            raise ValueError("invalid library")
        return web.json_response(await asyncio.to_thread(repository.library, owner_id, request.query["kind"]))

    async def react(request):
        if request.content_type != "application/json":
            raise ValueError("invalid content type")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"perfume_id", "revision", "reaction"}
                or type(body["perfume_id"]) is not int or body["perfume_id"] <= 0
                or type(body["revision"]) is not int or body["revision"] < 0
                or not isinstance(body["reaction"], str) or body["reaction"] not in REACTIONS):
            raise ValueError("invalid reaction")
        return web.json_response(await asyncio.to_thread(repository.react, owner_id,
            body["perfume_id"], body["revision"], body["reaction"]))

    app.router.add_get("/", shell)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/library", library)
    app.router.add_post("/api/react", react)
    app.router.add_static("/static/", STATIC_PATH, show_index=False, follow_symlinks=False)
    return app
