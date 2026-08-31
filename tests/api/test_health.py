from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health() -> None:
    """헬스 체크 엔드포인트가 정상 상태를 반환하는지 검증합니다."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
