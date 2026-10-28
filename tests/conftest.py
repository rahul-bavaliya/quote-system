import pytest

from quote_system.sample_data import generate


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """Synthetic exports -> full pipeline -> statistics db, in a temp folder."""
    root = tmp_path_factory.mktemp("qs")
    raw = root / "raw"
    generate(raw, n_wo=2500)
    mp = pytest.MonkeyPatch()
    mp.setenv("QS_RAW_DIR", str(raw))
    mp.setenv("QS_WORK_DIR", str(root / "work"))
    mp.setenv("QS_DB_PATH", str(root / "quote.duckdb"))
    mp.setenv("QS_QUOTES_DB", str(root / "quotes.sqlite"))
    from quote_system.config import get_settings
    from quote_system.pipeline import build_database

    s = get_settings()
    meta = build_database(s, s.db_path)
    yield {"settings": s, "meta": meta, "root": root}
    mp.undo()


@pytest.fixture(scope="session")
def client(built):
    from fastapi.testclient import TestClient

    from quote_system.api.main import app

    with TestClient(app) as c:
        yield c
