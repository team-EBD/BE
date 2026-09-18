"""'발견 돋보기'(`food_clarifier`) — 분석 요청의 candidate_depth 스위치.

SKILL.md §6.5: feature flag + 스킬 장착 + 충전(7일마다 1회)이 **모두** 참일 때만
`candidate_depth="clarifier"` 로 보낸다. 자동 확정은 하지 않는다 — 후보만 1개 더.
"""
from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.ai_client import get_ai_client
from app.ai_client.mock import MockAIClient
from app.ai_client.real import RealAIClient
from app.core.config import settings
from app.core.timeutil import kst_date_of, now_utc
from app.main import app
from app.models import AiCallLog, GameProfile, SkillUsageLedger, UserSkill
from app.services import game_catalog as catalog
from app.services import game_skills
from tests.test_images import upload

SKILL = game_skills.CLARIFIER_SKILL_CODE


def _logical_today() -> date:
    return kst_date_of(now_utc(), settings.day_start_hour)


class RecordingAIClient(MockAIClient):
    """넘어온 candidate_depth 를 그대로 기록하는 더블 (신 시그니처)."""

    def __init__(self) -> None:
        self.depths: list[str | None] = []

    def analyze(self, image_url, eating_habits=None, user_text=None, candidate_depth=None):
        self.depths.append(candidate_depth)
        return super().analyze(image_url, eating_habits, user_text, candidate_depth)


class LegacyAIClient(MockAIClient):
    """candidate_depth 를 **모르는** 구버전 시그니처. 넘기면 TypeError 가 난다."""

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, image_url, eating_habits=None, user_text=None):
        self.calls += 1
        return MockAIClient.analyze(self, image_url, eating_habits, user_text)


@pytest.fixture()
def clarifier_on():
    before = settings.game_food_clarifier
    settings.game_food_clarifier = True
    yield
    settings.game_food_clarifier = before


def use_client(ai) -> None:
    app.dependency_overrides[get_ai_client] = lambda: ai


@pytest.fixture(autouse=True)
def _restore_ai_client():
    yield
    app.dependency_overrides[get_ai_client] = lambda: MockAIClient()


def analyze(client, headers) -> httpx.Response:
    image_id = upload(client, headers).json()["meal_image_id"]
    return client.post(
        "/v1/meals/analyze", headers=headers, json={"meal_image_id": image_id}
    )


def equip_clarifier(client, headers, db_factory) -> int:
    """유대 Lv.3 을 기다리지 않고 '발견 돋보기'를 해금·장착시킨다. user_id 반환."""
    client.get("/v1/game/home", headers=headers)  # 프로필 생성
    db = db_factory()
    try:
        profile = db.scalar(select(GameProfile))
        db.add(
            UserSkill(
                user_id=profile.user_id,
                skill_code=SKILL,
                source_pet_code="pet_cat",
                charge_count=1,
            )
        )
        profile.equipped_skill_code = SKILL
        db.commit()
        return profile.user_id
    finally:
        db.close()


def record_past_use(db_factory, user_id: int, day: date) -> None:
    """과거에 돋보기를 쓴 것으로 원장에 남긴다 (충전 주기 검증용)."""
    db = db_factory()
    try:
        db.add(
            SkillUsageLedger(
                user_id=user_id,
                skill_code=SKILL,
                logical_date=day,
                reason="extra_candidate",
                idempotency_key=f"food-clarifier:{day.isoformat()}",
            )
        )
        db.commit()
    finally:
        db.close()


def usage_dates(db_factory, user_id: int) -> list[date]:
    db = db_factory()
    try:
        return sorted(
            db.scalars(
                select(SkillUsageLedger.logical_date).where(
                    SkillUsageLedger.user_id == user_id,
                    SkillUsageLedger.skill_code == SKILL,
                )
            ).all()
        )
    finally:
        db.close()


def task_types(db_factory) -> list[str]:
    db = db_factory()
    try:
        return list(db.scalars(select(AiCallLog.task_type).order_by(AiCallLog.id)).all())
    finally:
        db.close()


# --- 발동 판정 ---

def test_flag_off_never_sends_candidate_depth(client, auth_headers, db_factory):
    """기본(off)에서는 필드를 **아예 넘기지 않는다** — 구 시그니처도 그대로 돈다."""
    equip_clarifier(client, auth_headers, db_factory)
    legacy = LegacyAIClient()
    use_client(legacy)

    res = analyze(client, auth_headers)

    assert res.status_code == 200, res.text
    assert legacy.calls == 1
    assert usage_dates(db_factory, 1) == []  # 충전도 소모하지 않는다


def test_flag_on_without_equipped_skill_stays_standard(client, auth_headers, clarifier_on):
    recorder = RecordingAIClient()
    use_client(recorder)

    assert analyze(client, auth_headers).status_code == 200
    assert recorder.depths == [None]


def test_unlocked_but_not_equipped_stays_standard(client, auth_headers, db_factory, clarifier_on):
    """해금만 해 놓고 다른 스킬을 장착했으면 발동하지 않는다."""
    equip_clarifier(client, auth_headers, db_factory)
    db = db_factory()
    try:
        db.scalar(select(GameProfile)).equipped_skill_code = "streak_pause"
        db.commit()
    finally:
        db.close()
    recorder = RecordingAIClient()
    use_client(recorder)

    analyze(client, auth_headers)

    assert recorder.depths == [None]


def test_equipped_with_charge_sends_clarifier(client, auth_headers, db_factory, clarifier_on):
    user_id = equip_clarifier(client, auth_headers, db_factory)
    recorder = RecordingAIClient()
    use_client(recorder)

    res = analyze(client, auth_headers)

    assert res.status_code == 200, res.text
    assert recorder.depths == ["clarifier"]
    assert usage_dates(db_factory, user_id) == [_logical_today()]


def test_second_request_in_same_charge_cycle_is_standard(
    client, auth_headers, db_factory, clarifier_on
):
    user_id = equip_clarifier(client, auth_headers, db_factory)
    recorder = RecordingAIClient()
    use_client(recorder)

    analyze(client, auth_headers)
    analyze(client, auth_headers)

    assert recorder.depths == ["clarifier", None]
    assert usage_dates(db_factory, user_id) == [_logical_today()]  # 충전은 1회만


def test_recharges_after_seven_days(client, auth_headers, db_factory, clarifier_on):
    user_id = equip_clarifier(client, auth_headers, db_factory)
    today = _logical_today()
    record_past_use(db_factory, user_id, today - timedelta(days=7))
    recorder = RecordingAIClient()
    use_client(recorder)

    analyze(client, auth_headers)

    assert recorder.depths == ["clarifier"]
    assert usage_dates(db_factory, user_id) == [today - timedelta(days=7), today]


def test_still_on_cooldown_before_seven_days(client, auth_headers, db_factory, clarifier_on):
    user_id = equip_clarifier(client, auth_headers, db_factory)
    record_past_use(db_factory, user_id, _logical_today() - timedelta(days=6))
    recorder = RecordingAIClient()
    use_client(recorder)

    analyze(client, auth_headers)

    assert recorder.depths == [None]
    assert len(usage_dates(db_factory, user_id)) == 1  # 새로 소모하지 않았다


def test_charge_check_failure_does_not_block_analysis(
    client, auth_headers, db_factory, clarifier_on, monkeypatch
):
    """스킬 판정이 터져도 기록 흐름은 멈추지 않는다 (불변 조건)."""
    equip_clarifier(client, auth_headers, db_factory)

    def boom(*_args, **_kwargs):
        raise RuntimeError("스킬 판정 폭발")

    monkeypatch.setattr(game_skills, "_equipped_and_unlocked", boom)
    recorder = RecordingAIClient()
    use_client(recorder)

    res = analyze(client, auth_headers)

    assert res.status_code == 200, res.text
    assert res.json()["candidates"]  # 분석 결과는 정상 저장됐다
    assert recorder.depths == [None]


# --- 계측 / 후보 개수 ---

def test_clarifier_call_is_logged_with_its_own_task_type(
    client, auth_headers, db_factory, clarifier_on
):
    """비용 분리 집계의 근거 — `analyze_clarifier` 로 남아야 한다."""
    equip_clarifier(client, auth_headers, db_factory)
    use_client(MockAIClient())

    assert analyze(client, auth_headers).status_code == 200
    assert analyze(client, auth_headers).status_code == 200  # 충전 소진 → standard

    assert task_types(db_factory) == ["analyze_clarifier", "analyze"]


def test_clarifier_keeps_the_extra_candidate(client, auth_headers, db_factory, clarifier_on):
    """음식당 대체 후보가 1개 더 살아남는다 (BE 방어 컷에 잘리면 스킬이 무효)."""
    equip_clarifier(client, auth_headers, db_factory)
    use_client(MockAIClient())

    clarifier = analyze(client, auth_headers).json()["candidates"]
    standard = analyze(client, auth_headers).json()["candidates"]

    def per_food(candidates, index):
        return [c for c in candidates if c["food_index"] == index]

    assert len(per_food(clarifier, 0)) == 4
    assert len(per_food(standard, 0)) == 3


def test_clarifier_analysis_still_counts_against_the_daily_limit(
    client, auth_headers, db_factory, clarifier_on, monkeypatch
):
    """돋보기 호출이 task_type 이 다르다는 이유로 한도를 우회하면 안 된다."""
    monkeypatch.setattr(settings, "analyze_daily_limit", 1)
    equip_clarifier(client, auth_headers, db_factory)
    use_client(MockAIClient())

    assert analyze(client, auth_headers).status_code == 200  # analyze_clarifier 1건
    res = analyze(client, auth_headers)

    assert res.status_code == 429
    assert res.json()["error"]["code"] == "TOO_MANY_REQUESTS"


# --- 노출(FE '준비 중' 판단) ---

def test_skill_is_inactive_while_the_flag_is_off():
    assert SKILL in catalog.ACTIVE_SKILL_CODES
    assert catalog.is_active_skill(SKILL) is False
    assert catalog.is_active_skill("streak_pause") is True


def test_skill_becomes_active_with_the_flag(clarifier_on):
    assert catalog.is_active_skill(SKILL) is True


def test_skills_api_reports_charges_from_the_ledger(
    client, auth_headers, db_factory, clarifier_on
):
    """달력 기준 충전이라 user_skills.charge_count 가 아니라 원장이 단일 원천이다."""
    user_id = equip_clarifier(client, auth_headers, db_factory)

    def clarifier_row() -> dict:
        skills = client.get("/v1/game/skills", headers=auth_headers).json()["skills"]
        return next(s for s in skills if s["code"] == SKILL)

    fresh = clarifier_row()
    assert (fresh["charges"], fresh["max_charges"], fresh["is_active"]) == (1, 1, True)

    record_past_use(db_factory, user_id, _logical_today())
    assert clarifier_row()["charges"] == 0
    assert client.get("/v1/game/home", headers=auth_headers).json()["equipped_skill"][
        "charges"
    ] == 0


# --- RealAIClient 직렬화 ---

def _capture(calls: list):
    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)

        class _Res:
            status_code = 200
            text = "{}"

            def json(self):
                return {
                    "status": "success",
                    "candidates": [],
                    "ai_call_log": {"task_type": "analyze", "status": "success"},
                }

        return _Res()

    return fake_post


def test_real_client_omits_candidate_depth_by_default(monkeypatch):
    """구버전 AI 서버가 모르는 키를 기본 요청에 섞지 않는다."""
    calls: list = []
    monkeypatch.setattr(httpx, "post", _capture(calls))

    RealAIClient("http://ai", timeout=1.0).analyze("http://img/1.jpg")
    RealAIClient("http://ai", timeout=1.0).analyze("http://img/1.jpg", candidate_depth="standard")

    assert all("candidate_depth" not in payload for payload in calls)


def test_real_client_sends_candidate_depth_when_clarifier(monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", _capture(calls))

    RealAIClient("http://ai", timeout=1.0).analyze(
        "http://img/1.jpg", candidate_depth="clarifier"
    )

    assert calls[0]["candidate_depth"] == "clarifier"
