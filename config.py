from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Deepgram
    deepgram_api_key: str = ""

    # Groq
    groq_api_key: str = ""

    # ElevenLabs (Arabic TTS — Deepgram Aura has no Arabic voice)
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
