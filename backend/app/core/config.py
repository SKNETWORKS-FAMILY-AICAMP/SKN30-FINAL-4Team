from pathlib import Path
from urllib.parse import urlsplit
from functools import cached_property

from app.services.sim.sim_engine import load_sim_scoring, load_sim_prompt
from app.services.fit.fit_engine import load_fit_scoring, load_fit_prompt

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr, field_validator
from app.schemas.sim import SimScoringPolicy
from app.schemas.fit import FitScoringPolicy
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
    jwt_access_token_expire_minutes: int = Field(default=60, ge=1)
    # --- Supabase Auth -------------------------------------------------------
    # 로그인은 Supabase 가 맡고 FastAPI 는 그 토큰을 검증한다. 서명 방식은
    # 프로젝트 설정에 따라 다르므로 둘 다 받아 두고 토큰의 alg 로 고른다.
    #   공유 비밀(HS256)  -> SUPABASE_JWT_SECRET
    #   비대칭 키(ES256…) -> SUPABASE_JWKS_URL, 없으면 SUPABASE_URL 에서 유도
    # 둘 다 비어 있으면 Supabase 인증을 끈 것이고, 자체 발급 JWT 만 받는다.
    supabase_url: AnyHttpUrl | None = None
    supabase_jwt_secret: SecretStr | None = None
    supabase_jwks_url: AnyHttpUrl | None = None
    supabase_jwt_audience: str = Field(default="authenticated", min_length=1)
    # Supabase 계정과 아직 연결되지 않은 기존 사용자를 첫 로그인 때 이메일로
    # 이어 붙일지. 두 시스템의 사용자 명부가 같다는 전제가 있어야 켠다.
    supabase_link_existing_user_by_email: bool = True
    # 마이그레이션 기간 동안 자체 발급 JWT(POST /api/v1/auth/login)도 계속
    # 받는다. Supabase 하나로 확정되면 끈다.
    allow_internal_jwt: bool = True
    # 기동할 때 끊긴 분석을 실패로 정리할지. 분석이 프로세스 안의 태스크로만
    # 돌기 때문에 단일 프로세스에서는 켜 두는 것이 맞다. 서버를 여러 개
    # 띄우면 새로 뜬 쪽이 다른 쪽에서 처리 중인 건을 죽이므로 꺼야 한다.
    sweep_interrupted_analyses_on_startup: bool = True
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
    fit_prompt_version: str = Field(default="fit-v0.5", min_length=1)
    fit_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    fit_scoring_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "fit_scoring_v0.2.json"
    )
    fit_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "fit-v0.5.txt"
    )
    sim_ruleset_version: str = Field(default="sim-v0.2", min_length=1)
    sim_prompt_version: str = Field(default="sim-v0.3", min_length=1)
    sim_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    sim_scoring_path: Path = PROJECT_ROOT / "backend" / "config" / "sim_scoring.json"
    sim_prompt_path: Path = (
        PROJECT_ROOT / "backend" / "config" / "prompts" / "sim-v0.3.txt"
    )
    # 세 모델(Model 1/2/3)을 분석 파이프라인에서 돌릴지. 기본은 꺼 둔다 —
    # backend/requirements.txt 에 torch·transformers 가 없고, Model 1 이
    # 442MB 가중치를 올린다. 켜는 것은 배포의 결정이지 코드의 기본값이 아니다.
    ml_models_enabled: bool = False
    # Model 2 비교군 참조표의 '출처'. ml_orchestrator.COHORTS 중 하나여야 하고,
    # 벗어나면 회귀 예측은 그대로 나오되 percentile 만 비고 이유가 metadata 에
    # 남는다. 이 backend 가 동기화하는 공고가 BIZINFO 라 그쪽을 기본으로 둔다 —
    # 사전협의요청서에 맞는 모집단이 어느 쪽인지는 아직 평가된 바 없다.
    ml_model2_cohort: str = Field(default="bizinfo", min_length=1)
    chat_model_profile: str = Field(default="gpt-4o-mini", min_length=1)
    # 챗봇 프롬프트는 설정이 아니라 코드다 — chatmessage/prompt.py 가 갖고
    # 버전은 chatmessage.PROMPT_VERSION 이다. cpl/fit/sim 처럼 txt 파일로 빼지
    # 않는 이유는, 프롬프트가 Context 구조(context_scope·evidence_ids)와 한 몸이라
    # 따로 고치면 곧바로 어긋나기 때문이다.

    @cached_property
    def supabase_token_verifier(self) -> "SupabaseTokenVerifier":
        """토큰 검증기 하나를 앱 수명 동안 재사용한다.

        JWKS 클라이언트가 키를 캐시하기 때문에 매 요청 새로 만들면 로그인마다
        원격 조회가 돈다.
        """
        from app.core.supabase_auth import SupabaseTokenVerifier

        return SupabaseTokenVerifier(
            jwt_secret=(
                self.supabase_jwt_secret.get_secret_value()
                if self.supabase_jwt_secret
                else None
            ),
            jwks_url=self.resolved_supabase_jwks_url,
            issuer=self.resolved_supabase_issuer,
            audience=self.supabase_jwt_audience,
        )

    @property
    def resolved_supabase_jwks_url(self) -> str | None:
        """명시 설정이 우선. 없으면 프로젝트 URL 에서 표준 경로를 만든다."""
        if self.supabase_jwks_url is not None:
            return str(self.supabase_jwks_url)
        if self.supabase_url is None:
            return None
        return f"{str(self.supabase_url).rstrip('/')}/auth/v1/.well-known/jwks.json"

    @property
    def resolved_supabase_issuer(self) -> str | None:
        """프로젝트 URL 을 모르면 issuer 검증을 하지 않는다.

        검증할 값이 없는데 아무 issuer 나 통과시키지 않고, 검증 항목에서 아예
        빼는 쪽을 고른다 — 통과 조건을 조용히 느슨하게 만들지 않기 위해서다.
        """
        if self.supabase_url is None:
            return None
        return f"{str(self.supabase_url).rstrip('/')}/auth/v1"

    @cached_property
    def sim_scoring(self) -> SimScoringPolicy:
        return load_sim_scoring(self.sim_scoring_path)

    @cached_property
    def sim_prompt(self) -> str:
        return load_sim_prompt(self.sim_prompt_path)

    @cached_property
    def fit_scoring(self) -> FitScoringPolicy:
        return load_fit_scoring(self.fit_scoring_path)

    @cached_property
    def fit_prompt(self) -> str:
        return load_fit_prompt(self.fit_prompt_path)

    @field_validator("database_url")
    @classmethod
    def require_psycopg_driver(cls, database_url: PostgresDsn) -> PostgresDsn:
        if database_url.scheme != "postgresql+psycopg":
            raise ValueError("DATABASE_URL must use postgresql+psycopg")
        return database_url

    @field_validator(
        "bizinfo_api_key", "openai_api_key", "smtp_host", "smtp_username",
        "smtp_password", "smtp_from_email", "password_reset_url",
        "supabase_url", "supabase_jwt_secret", "supabase_jwks_url", mode="before",
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
            or "?" in value
            or "#" in value
            or any(
                character.isspace()
                or ord(character) < 0x20
                or ord(character) == 0x7F
                for character in value
            )
            or not (
                parsed.scheme == "https"
                or (
                    parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
        ):
            raise ValueError(
                "PASSWORD_RESET_URL must be a trusted HTTPS frontend URL "
                "(HTTP allowed only on loopback), without query or fragment"
            )
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
        mode="before",
    )
    @classmethod
    def resolve_project_path(cls, value: object) -> object:
        path = Path(value) if isinstance(value, (str, Path)) else value
        if isinstance(path, Path) and not path.is_absolute():
            return PROJECT_ROOT / path
        return path
