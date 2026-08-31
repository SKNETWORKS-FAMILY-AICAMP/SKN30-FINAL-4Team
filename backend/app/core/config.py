from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: PostgresDsn
    database_connect_timeout_seconds: int = Field(default=3, ge=1)
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_access_token_expire_minutes: int = Field(default=30, ge=1)
    smtp_host: str | None = None
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_email: str | None = None
    password_reset_url: str | None = None
    # 3000 은 Next.js, 5173 은 Vite 의 기본 개발 포트다. 프론트가 어느 쪽을
    # 쓰든 첫 호출이 CORS 로 막히지 않게 둘 다 기본으로 연다.
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ]
    )
    local_storage_root: Path = PROJECT_ROOT / "backend" / "storage"
    bizinfo_api_key: SecretStr | None = None
    bizinfo_api_url: AnyHttpUrl = (
        "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"
    )
    bizinfo_timeout_seconds: int = Field(default=30, ge=1)
    openai_api_key: SecretStr | None = None
    openai_base_url: AnyHttpUrl = "https://api.openai.com/v1"
    embedding_model_name: str = Field(
        default="text-embedding-3-small", min_length=1
    )
    embedding_profile_name: str = Field(default="bizinfo-summary", min_length=1)
    embedding_profile_version: int = Field(default=1, ge=1)
    embedding_preprocessing_version: str = Field(
        default="detail-ref-v1", min_length=1
    )
    embedding_timeout_seconds: int = Field(default=30, ge=1)
    embedding_batch_size: int = Field(default=100, ge=1, le=2048)
    cpl_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    cpl_prompt_version: str = Field(default="cpl-semantic-v0.9", min_length=1)
    cpl_ruleset_version: str = Field(default="cpl-alpha-v0.3", min_length=1)
    cpl_llm_timeout_seconds: int = Field(default=30, ge=1)
    cpl_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "cpl-v0.9.txt"
    )
    fit_ruleset_version: str = Field(default="fit-v0.3", min_length=1)
    fit_prompt_version: str = Field(default="fit-v0.4", min_length=1)
    fit_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    fit_scoring_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "fit_scoring_v0.2.json"
    )
    fit_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "fit-v0.4.txt"
    )
    sim_ruleset_version: str = Field(default="sim-v0.2", min_length=1)
    sim_prompt_version: str = Field(default="sim-v0.2", min_length=1)
    sim_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    sim_scoring_path: Path = PROJECT_ROOT / "backend" / "config" / "sim_scoring.json"
    sim_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "sim-v0.2.txt"
    )
    chat_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    chat_prompt_version: str = Field(default="chat-v0.1", min_length=1)
    chat_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "chat-v0.1.txt"
    )

    @field_validator("database_url")
    @classmethod
    def require_psycopg_driver(cls, database_url: PostgresDsn) -> PostgresDsn:
        if database_url.scheme != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return database_url

    @field_validator(
        "bizinfo_api_key", "openai_api_key", "smtp_host", "smtp_username",
        "smtp_password", "smtp_from_email", "password_reset_url", mode="before",
    )
    @classmethod
    def empty_external_key_means_disabled(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("password_reset_url")
    @classmethod
    def validate_password_reset_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value or "#" in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ValueError("PASSWORD_RESET_URL must be a trusted HTTPS frontend URL (HTTP allowed only on loopback), without query or fragment")
        parsed.port
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_allowed_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            if not isinstance(origin, str) or not origin or any(
                character.isspace()
                or ord(character) < 0x20
                or ord(character) == 0x7F
                for character in origin
            ):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                )
            try:
                parsed = urlsplit(origin)
                hostname = parsed.hostname
                parsed.port
            except ValueError as error:
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                ) from error
            if (
                parsed.scheme not in {"http", "https"}
                or hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or "?" in origin
                or "#" in origin
                or "*" in origin
            ):
                raise ValueError(
                    "CORS_ALLOWED_ORIGINS must contain exact HTTP(S) origins"
                )
        return origins

    @field_validator(
        "cpl_prompt_path",
        "fit_scoring_path",
        "fit_prompt_path",
        "sim_scoring_path",
        "sim_prompt_path",
        "chat_prompt_path",
        mode="before",
    )
    @classmethod
    def resolve_project_path(cls, value: object) -> object:
        path = Path(value) if isinstance(value, (str, Path)) else value
        if isinstance(path, Path) and not path.is_absolute():
            return PROJECT_ROOT / path
        return path
