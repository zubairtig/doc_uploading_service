import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi import _rate_limit_exceeded_handler

from app.api.routes import categorisation, document_requests, documents, health
from app.config import settings
from app.core.logging import configure_logging
from app.core.rate_limit import limiter
from app.db.session import engine
from app.services import categorisation_service
from app.web.routes import router as web_router

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Bank-statement categorisation runs interrupted by a restart carry on.
    try:
        await categorisation_service.resume_unfinished()
    except Exception:  # noqa: BLE001 — never block start-up on this
        logging.getLogger(__name__).exception("could not resume categorisation runs")
    yield
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    debug=settings.app_debug,
    lifespan=lifespan,
)

# CORS — lets integrator frontends call the API / upload directly from a
# browser. Origins configurable via CORS_ALLOW_ORIGINS (comma-separated).
_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (spec §15.4 — see app/core/rate_limit.py for defaults).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Health endpoints at root for operations probes.
app.include_router(health.router, tags=["health"])

# v1 surface defined in spec §6. The documents router carries its own /v1 prefix.
app.include_router(documents.router)
app.include_router(document_requests.router)
app.include_router(categorisation.router)

# Collection portal: staff pages, client upload pages, and local file serving.
# Same app, same port — no separate frontend server.
app.include_router(web_router)
