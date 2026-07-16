import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging_config import setup_logging
from app.routes.voice import router as voice_router
from config import settings

setup_logging()

logger = logging.getLogger(__name__)

REQUIRED_FOR_VOICE = [
    "telnyx_api_key",
    "deepgram_api_key",
    "groq_api_key",
    "public_base_url",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"starting up in '{settings.app_env}' mode")

    missing = [k for k in REQUIRED_FOR_VOICE if not getattr(settings, k, "")]
    if missing:
        logger.warning(
            f"missing config for {', '.join(missing)}. "
            f"/health will work, but /voice will fail until these are set in .env"
        )
    else:
        logger.info("all required service keys present")

    yield
    logger.info("shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Najda Voice",
        description="Multilingual AI first aid voice call agent (demo project)",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(voice_router)

    @app.get("/health")
    async def health_check():
        return {
            "status": "ok",
            "app": "najda-voice",
            "env": settings.app_env,
        }

    return app
