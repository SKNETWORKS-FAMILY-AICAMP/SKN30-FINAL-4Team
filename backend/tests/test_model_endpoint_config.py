from fastapi.testclient import TestClient

from app.core.config import Settings
import main


DATABASE_URL = "postgresql+psycopg://test:test@127.0.0.1:1/sims"
JWT_SECRET = "test-secret-that-is-at-least-32-bytes"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "database_url": DATABASE_URL,
        "jwt_secret": JWT_SECRET,
        "openai_api_key": "test-key",
        "sweep_interrupted_analyses_on_startup": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_embedding_endpoint_defaults_to_legacy_openai_endpoint() -> None:
    settings = _settings(openai_base_url="https://llm.example.test/v1")

    assert str(settings.embedding_base_url) == "https://llm.example.test/v1"


def test_embedding_endpoint_can_be_configured_separately() -> None:
    settings = _settings(
        openai_base_url="https://llm.example.test/v1",
        embedding_base_url="https://embedding.example.test/v1",
    )

    assert str(settings.openai_base_url) == "https://llm.example.test/v1"
    assert str(settings.embedding_base_url) == "https://embedding.example.test/v1"


def test_embedding_endpoint_environment_override_and_blank_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.example.test/v1")
    separate = Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        jwt_secret=JWT_SECRET,
    )
    assert str(separate.embedding_base_url) == "https://embedding.example.test/v1"

    monkeypatch.setenv("EMBEDDING_BASE_URL", "")
    fallback = Settings(
        _env_file=None,
        database_url=DATABASE_URL,
        jwt_secret=JWT_SECRET,
    )
    assert str(fallback.embedding_base_url) == "https://llm.example.test/v1"


def test_app_constructs_llm_and_embedding_clients_with_distinct_endpoints(
    monkeypatch,
) -> None:
    captured: dict[str, dict[str, object]] = {}

    class FakeLLM:
        def __init__(self, **kwargs: object) -> None:
            captured["llm"] = kwargs

    class FakeEmbedding:
        def __init__(self, **kwargs: object) -> None:
            captured["embedding"] = kwargs

    monkeypatch.setattr(main, "OpenAILLMClient", FakeLLM)
    monkeypatch.setattr(main, "OpenAIEmbeddingClient", FakeEmbedding)

    with TestClient(
        main.create_app(
            _settings(
                openai_base_url="https://llm.example.test/v1",
                embedding_base_url="https://embedding.example.test/v1",
            )
        )
    ):
        pass

    assert captured["llm"]["base_url"] == "https://llm.example.test/v1"
    assert captured["embedding"]["base_url"] == "https://embedding.example.test/v1"
    assert captured["llm"]["api_key"] == captured["embedding"]["api_key"]
