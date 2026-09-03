from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telnyx
    telnyx_api_key: str = ""
    telnyx_phone_number: str = ""
    # SIP Connection credential ID for browser-based WebRTC test calls
    # (Mission Control: API Keys -> Telephony Credentials). Read by
    # /telnyx-token; that endpoint returns a clear error when unset.
    telnyx_telephony_credential_id: str = ""

    # Deepgram
    deepgram_api_key: str = ""
    # English Aura-2 TTS voice (Deepgram). Used when TTS_PROVIDER_EN=deepgram
    # (Aura-2 has no Arabic voice — English only). English defaults to Groq
    # Orpheus (see TTS_PROVIDER_EN below).
    # NOTE: "orpheus" here is a real, separate Deepgram Aura-2 voice name —
    # confirmed against Deepgram's docs. It has NO relation to Groq/Canopy
    # Labs' "Orpheus" TTS model used elsewhere in this project. Coincidental
    # naming overlap only; do not "fix" this thinking it's a copy-paste bug.
    deepgram_tts_model_en: str = "aura-2-orpheus-en"
    # Arabic STT dialect code for Nova-3 (default "ar" = pan-Arab/MSA).
    # Bias recognition toward the caller's dialect with e.g. ar-EG, ar-SA,
    # ar-JO, ar-MA... (full list in app/core/language.py ARABIC_DIALECT_CODES).
    stt_language_ar: str = "ar"

    # Groq
    groq_api_key: str = ""

    # Arabic TTS provider: "groq" (Orpheus on Groq — default, uses the same
    # GROQ_API_KEY as the LLM, no extra account) or "elevenlabs".
    tts_provider_ar: str = "groq"
    # English TTS provider: "groq" (Orpheus English — default) or "deepgram"
    # (Aura-2, rollback path — see deepgram_tts.py). Mirrors TTS_PROVIDER_AR
    # so both languages have the same real runtime fallback, not just the
    # same failure detection (get_tts_provider() in app/core/language.py).
    tts_provider_en: str = "groq"
    # Orpheus Arabic voice: abdullah, fahad, sultan (male); lulwa, noura,
    # aisha (female). Listen before changing — run scripts/test_arabic_tts.py.
    groq_tts_voice_ar: str = "aisha"
    # Orpheus English voice for canopylabs/orpheus-v1-english (Groq docs
    # example voice is "austin"). English TTS now routes to Groq Orpheus —
    # one provider, one code path for both languages. Deepgram remains for
    # STT only.
    groq_tts_voice_en: str = "austin"
    groq_tts_model_en: str = "canopylabs/orpheus-v1-english"
    # Concurrent Orpheus TTS requests. Keep 1 on the Groq FREE tier (its
    # 1200 tokens/min budget 429-storms on bursts). After upgrading to the
    # Developer tier, 3 makes multi-sentence replies snappier.
    groq_tts_concurrency: int = 1
    # LLM reasoning effort for the gpt-oss-20b chat model. "low" = fastest,
    # occasionally trades grammatical care for speed (observed in Arabic
    # replies). "medium" tests whether grammar improves at a small TTFT cost.
    groq_reasoning_effort: str = "low"

    # ElevenLabs (Arabic TTS alternative — only used when TTS_PROVIDER_AR=elevenlabs.
    # NOTE: free-tier ElevenLabs keys cannot use *library* voices via API.)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id_ar: str = ""

    # Per-provider HTTP timeout (seconds), applied at client construction to
    # every REST provider (Groq TTS/LLM, Deepgram TTS, ElevenLabs). Passed to
    # httpx as a bare float, which sets connect/read/write/pool per-operation
    # and treats the read timeout as PER-CHUNK for streaming — so long LLM
    # streams survive as long as tokens keep arriving, but a genuinely hung
    # request dies instead of hanging the reply task forever. Deepgram STT is
    # excluded (WebSocket; idle covered by app-level keepalive/timeouts).
    provider_timeout_seconds: float = 60.0

    # /telnyx-token guard. This endpoint mints real, billable WebRTC call
    # tokens and was live on the public internet with zero auth for a
    # period after deployment. Two layers, neither sufficient alone:
    #   1. Shared secret (X-Najda-Demo-Token header). WEAK on its own:
    #      docs/app.js is a public static file (GitHub Pages) — the value
    #      configured there is readable by anyone who views the page
    #      source. This stops blind bots/scanners, not a targeted person.
    #   2. Per-IP rate limit — caps blast radius even if the secret leaks.
    #      Requires the reverse proxy to forward the real client IP and
    #      uvicorn to trust it (--proxy-headers) — verify this against
    #      real traffic, or every request shares one IP and this is a
    #      no-op. See app/routes/telnyx_token.py for the actual backstop
    #      (restricting the Telnyx Outbound Voice Profile's destination
    #      allowlist + a spend limit — independent of both layers here).
    demo_token_secret: str = ""
    telnyx_token_rate_limit_per_minute: int = 5

    # App
    app_env: str = "development"
    public_base_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def ws_base_url(self) -> str:
        url = self.public_base_url.rstrip("/")
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    def validate_required(self, keys: list[str]) -> None:
        missing = [k for k in keys if not getattr(self, k, "")]
        if missing:
            raise RuntimeError(
                f"Missing required settings: {', '.join(missing)}. "
                f"Check your .env file."
            )


settings = Settings()
