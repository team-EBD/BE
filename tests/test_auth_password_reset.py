"""비밀번호 재설정 플로우 (/auth/password/forgot, /auth/password/reset)."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.v1.auth import get_mailer
from app.core.config import settings
from app.core.timeutil import now_utc
from app.mail_client import MailSendError, MockMailClient
from app.main import app
from app.models import PasswordResetCode, RefreshToken, User
from tests.conftest import login as social_login
from tests.test_auth_email import SIGNUP_BODY, signup

EMAIL = SIGNUP_BODY["email"]  # alice@example.com


@pytest.fixture()
def mailer(client):
    """테스트용 mock 메일 클라이언트 — 발송 내용을 기억한다."""
    mock = MockMailClient()
    app.dependency_overrides[get_mailer] = lambda: mock
    yield mock
    app.dependency_overrides.pop(get_mailer, None)


def request_code(client) -> dict:
    return client.post("/v1/auth/password/forgot", json={"email": EMAIL})


def sent_code(mailer: MockMailClient) -> str:
    """마지막 발송 메일 본문에서 6자리 코드를 추출한다."""
    match = re.search(r"인증코드: (\d{6})", mailer.sent[-1]["body"])
    assert match, mailer.sent[-1]["body"]
    return match.group(1)


def reset(client, code: str, new_password: str = "newpass12", email: str = EMAIL):
    return client.post(
        "/v1/auth/password/reset",
        json={"email": email, "code": code, "new_password": new_password},
    )


def test_forgot_sends_code_mail(client, mailer, db_factory):
    signup(client)
    res = request_code(client)
    assert res.status_code == 200, res.text
    assert "인증코드" in res.json()["message"]

    assert len(mailer.sent) == 1
    assert mailer.sent[0]["to"] == EMAIL
    assert re.fullmatch(r"\d{6}", sent_code(mailer))

    # DB 에는 코드 원문이 아닌 해시만 저장된다
    with db_factory() as db:
        row = db.scalar(select(PasswordResetCode))
        assert row is not None
        assert sent_code(mailer) not in row.code_hash


def test_forgot_unknown_email_404(client, mailer):
    res = request_code(client)
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert mailer.sent == []


def test_forgot_social_only_account_409(client, mailer):
    social_login(client, "alice")  # email: alice@example.com (google)
    res = request_code(client)
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CONFLICT"
    assert "소셜" in res.json()["error"]["message"]
    assert mailer.sent == []


def test_forgot_mail_failure_500(client):
    signup(client)

    class FailingMailer:
        def send(self, to: str, subject: str, body: str) -> None:
            raise MailSendError("boom")

    app.dependency_overrides[get_mailer] = lambda: FailingMailer()
    try:
        res = request_code(client)
    finally:
        app.dependency_overrides.pop(get_mailer, None)
    assert res.status_code == 500
    assert res.json()["error"]["code"] == "INTERNAL_ERROR"


def test_reset_success_changes_password_and_revokes_sessions(client, mailer, db_factory):
    tokens = signup(client).json()
    request_code(client)

    res = reset(client, sent_code(mailer))
    assert res.status_code == 200, res.text

    # 재설정 시점까지 발급된 refresh 토큰은 전부 철회 상태
    with db_factory() as db:
        user = db.scalar(select(User).where(User.social_id == EMAIL))
        issued = db.scalars(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        ).all()
        assert issued and all(t.revoked_at is not None for t in issued)

    # 기존 비밀번호 로그인 불가, 새 비밀번호 로그인 가능
    old = client.post(
        "/v1/auth/login", json={"email": EMAIL, "password": SIGNUP_BODY["password"]}
    )
    assert old.status_code == 401
    new = client.post("/v1/auth/login", json={"email": EMAIL, "password": "newpass12"})
    assert new.status_code == 200

    # 기존 refresh 토큰은 철회되어 재사용 불가
    refreshed = client.post(
        "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


def test_reset_wrong_code_400_and_attempt_limit(client, mailer):
    signup(client)
    request_code(client)
    code = sent_code(mailer)
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(settings.password_reset_code_max_attempts):
        res = reset(client, wrong)
        assert res.status_code == 400
        assert res.json()["error"]["message"] == "인증코드가 올바르지 않습니다."

    # 시도 상한 초과 후에는 올바른 코드도 거부된다
    res = reset(client, code)
    assert res.status_code == 400


def test_reset_expired_code_400(client, mailer, db_factory):
    signup(client)
    request_code(client)
    code = sent_code(mailer)

    with db_factory() as db:
        row = db.scalar(select(PasswordResetCode))
        row.expires_at = now_utc() - timedelta(minutes=1)
        db.commit()

    res = reset(client, code)
    assert res.status_code == 400
    assert "만료" in res.json()["error"]["message"]


def test_reset_code_single_use(client, mailer):
    signup(client)
    request_code(client)
    code = sent_code(mailer)

    assert reset(client, code).status_code == 200
    res = reset(client, code, new_password="another12")
    assert res.status_code == 400


def test_reissue_invalidates_previous_code(client, mailer):
    signup(client)
    request_code(client)
    first = sent_code(mailer)
    request_code(client)
    second = sent_code(mailer)

    if first != second:  # 극히 드물게 같은 코드가 나올 수 있다
        assert reset(client, first).status_code == 400
    assert reset(client, second).status_code == 200


def test_reset_weak_new_password_400(client, mailer):
    signup(client)
    request_code(client)
    res = reset(client, sent_code(mailer), new_password="short")
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_reset_unknown_email_400(client, mailer):
    res = reset(client, "123456", email="ghost@example.com")
    assert res.status_code == 400
    assert res.json()["error"]["message"] == "인증코드가 올바르지 않습니다."
