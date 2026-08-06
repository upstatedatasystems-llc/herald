from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Environment & System
    HERALD_ENV: str = "production"
    LOG_LEVEL: str = "INFO"
    TZ: str = "America/New_York"
    HERALD_API_PORT: int = 8000
    HERALD_API_KEY: str = ""
    HERALD_MAX_SOURCE_CHARS: int = 100000
    HERALD_WORK_DIR: str = "/data/herald"

    # Database
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "herald"
    POSTGRES_USER: str = "herald"
    POSTGRES_PASSWORD: str = "herald_secure_password"
    DATABASE_URL: str = ""

    # Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-1.5-flash"
    GEMINI_TEMPERATURE: float = 0.3
    GEMINI_MAX_OUTPUT_TOKENS: int = 4096
    GEMINI_TIMEOUT_SECONDS: int = 60
    GEMINI_RETRY_COUNT: int = 3

    # Kokoro TTS
    KOKORO_BASE_URL: str = "http://kokoro:8880/v1"
    KOKORO_VOICE: str = "af_heart"
    KOKORO_SPEED: float = 1.0
    KOKORO_MODEL_PATH: str = "/opt/herald/models/kokoro"
    MAX_ACTIVE_TTS_JOBS: int = 1
    TTS_MAX_CHUNK_CHARS: int = 500

    # Directives Bounds
    ALLOWED_VOICES: str = "af_heart,af_bella,af_sarah,am_adam,am_michael"
    MIN_SPEED: float = 0.8
    MAX_SPEED: float = 1.2

    # Audio & FFmpeg
    AUDIO_OUTPUT_BITRATE: str = "64k"
    AUDIO_SAMPLE_RATE: int = 24000
    AUDIO_CHANNELS: int = 1
    LOUDNORM_TARGET_I: float = -16.0
    LOUDNORM_TARGET_TP: float = -1.5
    LOUDNORM_TARGET_LRA: float = 11.0

    # Google Drive Delivery
    GOOGLE_DRIVE_FOLDER_ID: str = ""

    # Gmail Intake & Allowed Senders
    EMAIL_ALLOWED_SENDERS: str = ""
    EMAIL_ATTACHMENT_MAX_MB: int = 18
    LOCAL_COMPLETE_RETENTION_HOURS: int = 48

    def get_database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    def get_allowed_senders_list(self) -> list[str]:
        if not self.EMAIL_ALLOWED_SENDERS:
            return []
        return [email.strip().lower() for email in self.EMAIL_ALLOWED_SENDERS.split(",") if email.strip()]

    def get_allowed_voices_list(self) -> list[str]:
        if not self.ALLOWED_VOICES:
            return ["af_heart"]
        return [v.strip().lower() for v in self.ALLOWED_VOICES.split(",") if v.strip()]

    def is_production_valid(self) -> bool:
        if self.HERALD_ENV.lower() == "production":
            if not self.HERALD_API_KEY or self.HERALD_API_KEY == "default-insecure-api-key":
                return False
            if not self.EMAIL_ALLOWED_SENDERS.strip():
                return False
        return True


settings = Settings()
