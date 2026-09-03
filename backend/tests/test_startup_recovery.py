from fastapi.testclient import TestClient

import main
from app.core.config import Settings


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_interrupted_analysis_sweep_finishes_before_serving_requests(
    monkeypatch,
    tmp_path,
) -> None:
    engine = FakeEngine()
    swept = False

    def sweep(received_engine) -> int:
        nonlocal swept
        assert received_engine is engine
        swept = True
        return 0

    monkeypatch.setattr(main, "create_database_engine", lambda *_: engine)
    monkeypatch.setattr(main, "fail_interrupted_analyses", sweep)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+psycopg://test:test@localhost/test",
        jwt_secret="x" * 32,
        local_storage_root=tmp_path,
        sweep_interrupted_analyses_on_startup=True,
    )

    with TestClient(main.create_app(settings)) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert swept is True

    assert engine.disposed is True
