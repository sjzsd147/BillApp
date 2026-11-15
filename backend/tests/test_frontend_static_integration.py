"""Integration tests for frontend static files mounting and SPA fallback routing."""

from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from billapp.app import create_app


@pytest.fixture
def static_client(tmp_path: Path):
    db_file = tmp_path / "test_static.db"
    app = create_app(db_path=str(db_file))
    with TestClient(app) as client:
        yield client


def test_serve_frontend_index(static_client: TestClient):
    """Test root / returns frontend index.html."""
    resp = static_client.get("/")
    assert resp.status_code == 200
    assert "账单助手" in resp.text
    assert "<div id=\"app\"></div>" in resp.text


def test_serve_frontend_spa_fallback(static_client: TestClient):
    """Test client-side routes fallback to index.html without 404."""
    for path in ["/dashboard", "/import", "/review", "/transactions", "/rules", "/accounts", "/settings"]:
        resp = static_client.get(path)
        assert resp.status_code == 200
        assert "账单助手" in resp.text


def test_api_route_priority_and_404_json(static_client: TestClient):
    """Test nonexistent /api/* routes strictly return 404 JSON, NEVER HTML."""
    resp = static_client.get("/api/nonexistent_test_route")
    assert resp.status_code == 404
    assert resp.headers.get("content-type", "").startswith("application/json")
    data = resp.json()
    assert data["code"] == "NOT_FOUND"


def test_api_health_works_with_static_mount(static_client: TestClient):
    """Test standard API endpoints work untouched when static distribution is mounted."""
    resp = static_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "1.0.0"}
