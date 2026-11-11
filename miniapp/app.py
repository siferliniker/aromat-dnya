"""HTTP shell and owner-only API. No Telegram sending or polling runs here."""

import asyncio
import json
from pathlib import Path

from aiohttp import web
from sqlalchemy.engine import Engine

from config import AppConfig
from miniapp.auth import AuthError, validate_init_data
from miniapp.performance import FragellaPerformance
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
               config: AppConfig | None = None, catalog_path: str | Path = DEFAULT_CATALOG_PATH,
               performance: FragellaPerformance | None = None) -> web.Application:
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(bot_token, str) or not bot_token:
        raise ValueError("Telegram owner credentials are required")
    repository = MiniAppRepository(engine, config, catalog_path)
    performance = performance or FragellaPerformance("", engine)

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
        return await card_response(await asyncio.to_thread(repository.state, owner_id))

    async def card_response(result):
        if result.get("current"):
            try:
                result["current"] = await performance.enrich(result["current"])
            except Exception:
                # Optional provider data must never turn a saved reaction into an apparent failure.
                pass
        return web.json_response(result)

    def cached_response(result):
        result["items"] = [performance.cached(card) for card in result.get("items", [])]
        return web.json_response(result)

    async def library(request):
        if len(request.query.getall("kind", [])) != 1 or request.query.get("kind") not in ("wishlist", "known"):
            raise ValueError("invalid library")
        return cached_response(await asyncio.to_thread(repository.library, owner_id, request.query["kind"]))

    async def react(request):
        if request.content_type != "application/json":
            raise ValueError("invalid content type")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"perfume_id", "revision", "reaction"}
                or type(body["perfume_id"]) is not int or body["perfume_id"] <= 0
                or type(body["revision"]) is not int or body["revision"] < 0
                or not isinstance(body["reaction"], str) or body["reaction"] not in REACTIONS):
            raise ValueError("invalid reaction")
        return await card_response(await asyncio.to_thread(repository.react, owner_id,
            body["perfume_id"], body["revision"], body["reaction"]))

    async def catalogue(request):
        permitted = {"q", "family", "season", "note", "time", "offset", "limit"}
        if (set(request.query) - permitted
                or any(len(request.query.getall(key)) != 1 for key in request.query)):
            raise ValueError("invalid query")
        values = dict(request.query)
        for key, default in (("offset", 0), ("limit", 30)):
            raw = values.get(key, str(default))
            if not raw.isascii() or not raw.isdigit() or len(raw) > 5:
                raise ValueError("invalid pagination")
            values[key] = int(raw)
        return cached_response(await asyncio.to_thread(repository.catalogue, owner_id, **values))

    async def selection(request):
        if request.content_type != "application/json":
            raise ValueError("invalid content type")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"perfume_id", "revision"}
                or type(body["perfume_id"]) is not int or body["perfume_id"] <= 0
                or type(body["revision"]) is not int or body["revision"] < 0):
            raise ValueError("invalid selection")
        return await card_response(await asyncio.to_thread(repository.select_perfume, owner_id,
            body["perfume_id"], body["revision"]))

    async def undo(request):
        if request.content_type != "application/json":
            raise ValueError("invalid content type")
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"revision"}
                or type(body["revision"]) is not int or body["revision"] < 0):
            raise ValueError("invalid undo")
        return await card_response(await asyncio.to_thread(repository.undo, owner_id, body["revision"]))

    async def performance_detail(request):
        raw = request.match_info["perfume_id"]
        if not raw.isascii() or not raw.isdigit() or len(raw) > 10 or int(raw) <= 0:
            raise ValueError("invalid perfume")
        card = await asyncio.to_thread(repository.detail, int(raw))
        if card is None:
            raise web.HTTPNotFound()
        try:
            card = await performance.enrich(card)
        except Exception:
            pass
        return web.json_response({"performance": card.get("performance")})

    app.router.add_get("/", shell)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/library", library)
    app.router.add_get("/api/catalog", catalogue)
    app.router.add_get("/api/performance/{perfume_id}", performance_detail)
    app.router.add_post("/api/react", react)
    app.router.add_post("/api/select", selection)
    app.router.add_post("/api/undo", undo)
    app.router.add_static("/static/", STATIC_PATH, show_index=False, follow_symlinks=False)
    return app
