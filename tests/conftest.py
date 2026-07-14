from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test.db")
    return create_app(database_path=db_path)


@pytest.fixture
def client(app):
    return TestClient(app)
