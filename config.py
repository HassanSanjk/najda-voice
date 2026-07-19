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
    # Arabic STT dialect code for Nova-3 (default "ar" = pan-Arab/MSA).
    # Bias recognition toward the caller's dialect with e.g. ar-EG, ar-SA,
    # ar-JO, ar-MA... (full list in app/core/language.py ARABIC_DIALECT_CODES).
    stt_language_ar: str = "ar"

    # Groq
    groq_api_key: str = ""

    # Arabic TTS provider: "groq" (Orpheus on Groq — default, uses the same
    # GROQ_API_KEY as the LLM, no extra account) or "elevenlabs".
    tts_provider_ar: str = "groq"
    # Orpheus Arabic voice: abdullah, fahad, sultan (male); lulwa, noura,
    # aisha (female). Listen before changing — run scripts/test_arabic_tts.py.
    groq_tts_voice_ar: str = "aisha"
    # Concurrent Orpheus TTS requests. Keep 1 on the Groq FREE tier (its
    # 1200 tokens/min budget 429-storms on bursts). After upgrading to the
    # Developer tier, 3 makes multi-sentence replies snappier.
    groq_tts_concurrency: int = 1

    # ElevenLabs (Arabic TTS alternative — only used when TTS_PROVIDER_AR=elevenlabs.
    # NOTE: free-tier ElevenLabs keys cannot use *library* voices via API.)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id_ar: str = ""

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
