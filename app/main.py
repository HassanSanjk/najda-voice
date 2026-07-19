import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.language import get_tts_provider
from app.core.logging_config import setup_logging
from app.core.voice import arabic_tts_configured, prewarm_greeting_cache
from app.routes.telnyx_token import router as telnyx_token_router
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

    if arabic_tts_configured():
        logger.info(f"Arabic TTS provider: {get_tts_provider('ar')}")
    else:
        logger.warning(
            f"Arabic TTS provider '{get_tts_provider('ar')}' is not configured — "
            f"Arabic TTS and Arabic language detection are DISABLED; calls will "
            f"run English-only until it is configured in .env"
        )

    # Pre-synthesize the fixed opening line(s) so the first call doesn't
    # pay a TTS round trip for a greeting that never changes. Non-fatal
    # if it fails — the greeting also lazily caches on first call.
    prewarm_task = asyncio.create_task(prewarm_greeting_cache())

    yield
    prewarm_task.cancel()
    logger.info("shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Najda Voice",
        description="Multilingual AI first aid voice call agent (demo project)",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(voice_router)
    # WebRTC browser-calling token endpoint — was defined but never
    # mounted, so /telnyx-token 404'd and the cheap browser-based test
    # path couldn't authenticate.
    app.include_router(telnyx_token_router)

    @app.get("/health")
    async def health_check():
        return {
            "status": "ok",
            "app": "najda-voice",
            "env": settings.app_env,
        }

    return app
