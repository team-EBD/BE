"""POST /meals/analyze — 사진과 함께 온 설명(text)이 AI 클라이언트로 전달되는지."""
from __future__ import annotations

from app.ai_client import get_ai_client
from app.ai_client.mock import MockAIClient
from app.main import app
from tests.test_images import upload


class RecordingAIClient(MockAIClient):
    """전달된 user_text 를 기록하는 mock."""

    def __init__(self):
        self.seen_user_text = "NOT_CALLED"

    def analyze(self, image_url, eating_habits=None, user_text=None):
        self.seen_user_text = user_text
        return super().analyze(image_url, eating_habits, user_text)


def _analyze(client, headers, **extra):
    image_id = upload(client, headers).json()["meal_image_id"]
    return client.post(
        "/v1/meals/analyze", headers=headers, json={"meal_image_id": image_id, **extra}
    )


def test_text_passed_to_ai_client(client, auth_headers):
    recorder = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recorder
    try:
        res = _analyze(client, auth_headers, text="김치찌개 반만 먹었어")
        assert res.status_code == 200
        assert recorder.seen_user_text == "김치찌개 반만 먹었어"
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_blank_text_normalized_to_none(client, auth_headers):
    recorder = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recorder
    try:
        _analyze(client, auth_headers, text="   ")
        assert recorder.seen_user_text is None
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_missing_text_backward_compatible(client, auth_headers):
    recorder = RecordingAIClient()
    app.dependency_overrides[get_ai_client] = lambda: recorder
    try:
        res = _analyze(client, auth_headers)
        assert res.status_code == 200
        assert recorder.seen_user_text is None
    finally:
        app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def test_text_over_200_rejected(client, auth_headers):
    res = _analyze(client, auth_headers, text="김" * 201)
    assert res.status_code == 400  # 검증 오류 → 명세서 1.4 에러 봉투
