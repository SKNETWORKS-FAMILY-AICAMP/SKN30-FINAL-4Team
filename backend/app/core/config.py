from pathlib import Path

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
    cpl_prompt_version: str = Field(default="cpl-semantic-v0.2", min_length=1)
    cpl_ruleset_version: str = Field(default="cpl-alpha-v0.2", min_length=1)
    cpl_llm_timeout_seconds: int = Field(default=30, ge=1)
    cpl_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "cpl-v0.2.txt"
    )
    fit_ruleset_version: str = Field(default="fit-v0.2", min_length=1)
    fit_prompt_version: str = Field(default="fit-v0.2", min_length=1)
    fit_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    fit_scoring_path: Path = PROJECT_ROOT / "backend" / "config" / "fit_scoring.json"
    fit_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "fit-v0.2.txt"
    )

    @field_validator("database_url")
    @classmethod
    def require_psycopg_driver(cls, database_url: PostgresDsn) -> PostgresDsn:
        if database_url.scheme != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return database_url

    @field_validator("bizinfo_api_key", "openai_api_key", mode="before")
    @classmethod
    def empty_external_key_means_disabled(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator(
        "cpl_prompt_path",
        "fit_scoring_path",
        "fit_prompt_path",
        mode="before",
    )
    @classmethod
    def resolve_project_path(cls, value: object) -> object:
        path = Path(value) if isinstance(value, (str, Path)) else value
        if isinstance(path, Path) and not path.is_absolute():
            return PROJECT_ROOT / path
        return path
