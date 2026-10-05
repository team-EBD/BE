"""GET /v1/promotions/active — 설정값으로 만드는 프로모션 이미지."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.core.timeutil import now_utc
from app.models import User
from app.models.billing import Subscription
from app.services import promotion as promotion_service

URL = "/v1/promotions/active"
IMAGE = "https://cdn.example.com/promo/launch.png"


@pytest.fixture(autouse=True)
def promo_defaults(monkeypatch):
    """로컬 .env 와 무관하게 계약의 기본값에서 출발한다 (이미지 주소만 비어 있음)."""
    defaults = {
        "promo_enabled": True,
        "promo_id": "launch-100-free-2026-10",
        "promo_image_url": "",
        "promo_image_width": 1080,
        "promo_image_height": 1620,
        "promo_tutorial_image_url": "",
        "promo_tutorial_image_width": 1536,
        "promo_tutorial_image_height": 1024,
        "promo_alt_text": "Eat로그 출시 기념 — AI 분석 100회 무료",
        "promo_action": "subscription",
        "promo_action_url": "",
        "promo_placements": "launch,tutorial",
        "promo_show_to_premium": False,
    }
    for name, value in defaults.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def with_image(monkeypatch):
    monkeypatch.setattr(settings, "promo_image_url", IMAGE)


def _add_subscription(db_factory, *, status: str, expires_in_days: int) -> None:
    with db_factory() as db:
        db.add(
            Subscription(
                user_id=db.scalar(select(User.id)),
                platform="android",
                product_id="eatlog_premium_monthly",
                purchase_key=f"promo-test-{status}",
                status=status,
                is_auto_renewing=status == "active",
                verified_at=now_utc(),
                expires_at=now_utc() + timedelta(days=expires_in_days),
            )
        )
        db.commit()


@pytest.fixture
def make_premium(db_factory, auth_headers):
    return lambda: _add_subscription(db_factory, status="active", expires_in_days=30)


def _get(client, headers, **params):
    return client.get(URL, headers=headers, params=params)


def test_code_defaults_match_the_contract():
    """env 없이 본 코드 기본값 — 계약 표와 같아야 한다."""
    fields = Settings.model_fields
    assert fields["promo_enabled"].default is True
    assert fields["promo_id"].default == "launch-100-free-2026-10"
    assert fields["promo_image_width"].default == 1024
    assert fields["promo_image_height"].default == 1536
    # 기본 이미지는 공개 HTTPS 주소여야 한다 (배포 직후 Parameter Store 설정 없이도 보이도록)
    assert fields["promo_image_url"].default.startswith("https://")
    assert fields["promo_alt_text"].default == "Eat로그 출시 기념 — AI 분석 100회 무료"
    assert fields["promo_action"].default == "subscription"
    assert fields["promo_action_url"].default == ""
    assert fields["promo_placements"].default == "launch,tutorial"
    assert fields["promo_show_to_premium"].default is False


def test_requires_auth(client):
    res = client.get(URL)
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_empty_image_url_means_no_promotion(client, auth_headers):
    res = _get(client, auth_headers)
    assert res.status_code == 200
    assert res.json() == {"promotion": None}


def test_whitespace_image_url_means_no_promotion(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "promo_image_url", "   ")
    assert _get(client, auth_headers).json() == {"promotion": None}


def test_payload_matches_contract(client, auth_headers, with_image):
    res = _get(client, auth_headers)
    assert res.status_code == 200
    assert res.json() == {
        "promotion": {
            "id": "launch-100-free-2026-10",
            "image_url": IMAGE,
            "image_width": 1080,
            "image_height": 1620,
            "alt_text": "Eat로그 출시 기념 — AI 분석 100회 무료",
            "action": "subscription",
            "action_url": None,
            "placements": ["launch", "tutorial"],
        }
    }


def test_placement_defaults_to_launch(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_placements", "tutorial")
    assert _get(client, auth_headers).json() == {"promotion": None}
    promo = _get(client, auth_headers, placement="tutorial").json()["promotion"]
    assert promo["placements"] == ["tutorial"]


def test_placement_filter(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_placements", " Launch , ,launch")
    launch = _get(client, auth_headers, placement="launch").json()["promotion"]
    assert launch["placements"] == ["launch"]
    assert _get(client, auth_headers, placement="tutorial").json() == {"promotion": None}


def test_no_placements_configured(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_placements", "")
    for placement in ("launch", "tutorial"):
        assert _get(client, auth_headers, placement=placement).json() == {"promotion": None}


def test_unknown_placement_is_a_validation_error(client, auth_headers, with_image):
    res = _get(client, auth_headers, placement="banner")
    # 프로젝트 규약: 요청 검증 실패는 400 VALIDATION_ERROR 봉투
    assert res.status_code == 400
    error = res.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["field"] == "query.placement"


def test_disabled_flag(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_enabled", False)
    for placement in ("launch", "tutorial"):
        assert _get(client, auth_headers, placement=placement).json() == {"promotion": None}


def test_premium_user_gets_null(client, auth_headers, with_image, make_premium):
    assert _get(client, auth_headers).json()["promotion"] is not None
    make_premium()
    assert _get(client, auth_headers).json() == {"promotion": None}
    assert _get(client, auth_headers, placement="tutorial").json() == {"promotion": None}


def test_premium_user_sees_it_when_allowed(
    client, auth_headers, with_image, make_premium, monkeypatch
):
    monkeypatch.setattr(settings, "promo_show_to_premium", True)
    make_premium()
    assert _get(client, auth_headers).json()["promotion"]["id"] == "launch-100-free-2026-10"


def test_expired_subscriber_sees_promotion(client, auth_headers, with_image, db_factory):
    _add_subscription(db_factory, status="expired", expires_in_days=-1)
    assert _get(client, auth_headers).json()["promotion"] is not None


def test_premium_check_failure_is_treated_as_free(client, auth_headers, with_image, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(promotion_service, "is_premium", boom)
    res = _get(client, auth_headers)
    assert res.status_code == 200
    assert res.json()["promotion"]["image_url"] == IMAGE


def test_premium_check_is_skipped_when_shown_to_premium(
    client, auth_headers, with_image, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise AssertionError("프리미엄 판정을 부를 필요가 없다")

    monkeypatch.setattr(settings, "promo_show_to_premium", True)
    monkeypatch.setattr(promotion_service, "is_premium", boom)
    assert _get(client, auth_headers).json()["promotion"] is not None


@pytest.mark.parametrize("configured", ["purchase", "", "  ", "SUBSCRIBE!"])
def test_malformed_action_falls_back_to_none(
    client, auth_headers, with_image, monkeypatch, configured
):
    monkeypatch.setattr(settings, "promo_action", configured)
    monkeypatch.setattr(settings, "promo_action_url", "https://example.com/event")
    promo = _get(client, auth_headers).json()["promotion"]
    assert promo["action"] == "none"
    assert promo["action_url"] is None


def test_action_is_normalized(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_action", " Subscription ")
    assert _get(client, auth_headers).json()["promotion"]["action"] == "subscription"


def test_action_url(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_action", "url")
    monkeypatch.setattr(settings, "promo_action_url", " https://example.com/event ")
    promo = _get(client, auth_headers).json()["promotion"]
    assert promo["action"] == "url"
    assert promo["action_url"] == "https://example.com/event"


def test_action_url_without_url_falls_back_to_none(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_action", "url")
    monkeypatch.setattr(settings, "promo_action_url", "")
    promo = _get(client, auth_headers).json()["promotion"]
    assert promo["action"] == "none"
    assert promo["action_url"] is None


def test_action_none_ignores_action_url(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_action", "none")
    monkeypatch.setattr(settings, "promo_action_url", "https://example.com/event")
    promo = _get(client, auth_headers).json()["promotion"]
    assert promo["action"] == "none"
    assert promo["action_url"] is None


def test_config_changes_are_read_per_request(client, auth_headers, with_image, monkeypatch):
    """이미지·id 를 바꾸면 다음 요청부터 바로 반영된다 (모듈 상수로 굳지 않는다)."""
    monkeypatch.setattr(settings, "promo_id", "autumn-2026-11")
    monkeypatch.setattr(settings, "promo_image_url", "https://cdn.example.com/promo/autumn.png")
    monkeypatch.setattr(settings, "promo_image_width", 1200)
    monkeypatch.setattr(settings, "promo_image_height", 1500)
    monkeypatch.setattr(settings, "promo_alt_text", "가을 이벤트")
    promo = _get(client, auth_headers).json()["promotion"]
    assert promo["id"] == "autumn-2026-11"
    assert promo["image_url"] == "https://cdn.example.com/promo/autumn.png"
    assert (promo["image_width"], promo["image_height"]) == (1200, 1500)
    assert promo["alt_text"] == "가을 이벤트"


def test_non_positive_image_size_falls_back(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_image_width", 0)
    promo = _get(client, auth_headers).json()["promotion"]
    assert (promo["image_width"], promo["image_height"]) == (1080, 1620)


def test_env_names_map_to_settings(monkeypatch):
    """Parameter Store 키 이름(대문자)이 설정 필드로 들어온다."""
    env = {
        "PROMO_IMAGE_URL": "https://cdn.example.com/x.png",
        "PROMO_ID": "x-1",
        "PROMO_ENABLED": "false",
        "PROMO_IMAGE_WIDTH": "900",
        "PROMO_IMAGE_HEIGHT": "1200",
        "PROMO_ALT_TEXT": "대체 문구",
        "PROMO_ACTION": "url",
        "PROMO_ACTION_URL": "https://example.com",
        "PROMO_PLACEMENTS": "launch",
        "PROMO_SHOW_TO_PREMIUM": "true",
        "FREE_CREDIT_LIMIT": "30",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fresh = Settings(_env_file=None)
    assert fresh.promo_image_url == "https://cdn.example.com/x.png"
    assert fresh.promo_id == "x-1"
    assert fresh.promo_enabled is False
    assert (fresh.promo_image_width, fresh.promo_image_height) == (900, 1200)
    assert fresh.promo_alt_text == "대체 문구"
    assert fresh.promo_action == "url"
    assert fresh.promo_action_url == "https://example.com"
    assert fresh.promo_placements == "launch"
    assert fresh.promo_show_to_premium is True
    assert fresh.free_credit_limit == 30


TUTORIAL_IMAGE = "https://cdn.example.com/promo/tutorial-wide.png"


def test_tutorial_placement_uses_its_own_image(client, auth_headers, with_image, monkeypatch):
    """튜토리얼 페이월은 가로형 전용 이미지를 받는다 — 팝업(launch)은 기본 이미지 그대로."""
    monkeypatch.setattr(settings, "promo_tutorial_image_url", TUTORIAL_IMAGE)
    tutorial = _get(client, auth_headers, placement="tutorial").json()["promotion"]
    assert tutorial["image_url"] == TUTORIAL_IMAGE
    assert (tutorial["image_width"], tutorial["image_height"]) == (1536, 1024)
    # 같은 프로모션이다 — id·동작·대체 문구는 공유한다
    assert tutorial["id"] == "launch-100-free-2026-10"
    assert tutorial["action"] == "subscription"

    launch = _get(client, auth_headers, placement="launch").json()["promotion"]
    assert launch["image_url"] == IMAGE
    assert (launch["image_width"], launch["image_height"]) == (1080, 1620)


def test_tutorial_placement_falls_back_to_main_image(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_tutorial_image_url", "   ")
    tutorial = _get(client, auth_headers, placement="tutorial").json()["promotion"]
    assert tutorial["image_url"] == IMAGE
    assert (tutorial["image_width"], tutorial["image_height"]) == (1080, 1620)


def test_tutorial_image_alone_does_not_enable_promotion(client, auth_headers, monkeypatch):
    """기본 이미지가 비어 있으면(프로모션 없음) 튜토리얼 이미지만으로는 켜지지 않는다."""
    monkeypatch.setattr(settings, "promo_tutorial_image_url", TUTORIAL_IMAGE)
    assert _get(client, auth_headers, placement="tutorial").json() == {"promotion": None}


def test_tutorial_image_bad_size_falls_back(client, auth_headers, with_image, monkeypatch):
    monkeypatch.setattr(settings, "promo_tutorial_image_url", TUTORIAL_IMAGE)
    monkeypatch.setattr(settings, "promo_tutorial_image_width", 0)
    tutorial = _get(client, auth_headers, placement="tutorial").json()["promotion"]
    # 가로형 기본 비율로 떨어진다 (세로 포스터 비율이 아니다)
    assert (tutorial["image_width"], tutorial["image_height"]) == (1536, 1024)
