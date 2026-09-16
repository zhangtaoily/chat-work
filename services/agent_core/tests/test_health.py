"""健康检查接口测试。"""

from fastapi.testclient import TestClient

from agent_core.api.main import app

client = TestClient(app)


def test_health() -> None:
    """GET /health 返回 200 与版本号。"""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "0.1.0"}
