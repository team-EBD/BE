"""기록 습관 라이프사이클 푸시 (2026-10-15 리텐션).

목표: 광고로 들어온 사용자가 첫 기록을 남기고, 끊기더라도 다시 돌아오게 한다
(D1 29% / D3 5% / D7 4%, 가입자 절반이 첫 기록 없음).

근거
- Headspace: '첫 세션'을 겨냥한 CRM 이 1주 리텐션 +109% → first_log
- Milkman 'don't miss twice': 한 번 빠진 사람이 바로 돌아오게 하는 것이 효과가 크다(+27%) → missed_one
- Duolingo: 같은 문구 반복을 피한다 → 최근 7일에 받은 템플릿은 다시 고르지 않는다
- Braze: 수신 거부 1·2위는 '나와 상관없음'과 '너무 잦음' → 세그먼트별 문구, 하루 최대 1건,
  기기 로컬 식사 알림을 이미 켠 사용자는 제외
- Yale 연구: '먹기 전에 찍어야 한다'는 인식이 사진 기록을 끊는다 → 먹은 뒤 갤러리·글 기록도 된다고 말한다

법적 제약(정보통신망법): 정보성 알림으로만 쓴다 — 혜택·할인·'무료' 같은 광고성 문구 금지,
21:00~08:00(KST)에는 보내지 않는다.

규칙
- 사용자당 논리 날짜(KST, day_start_hour 경계) 하루 최대 1건. push_send_logs 의
  (user_id, channel='lifecycle', logical_date) 유일 제약으로 보장한다 — 발송 전에 행을 커밋.
- 대상: 푸시 토큰이 있고 전체 알림이 켜진(설정 행 없음 = 켬) 사용자. 단 로컬 식사 알림
  (lunch_time/dinner_time)을 설정한 사용자는 기기가 이미 알려 주므로 제외 — 시각이 그 끼니로
  그럴듯할 때만(점심 9~15시, 저녁 15~23시) 설정된 것으로 본다. 테스트 기기 계정 제외.
- 오늘(논리 날짜) 먹은 기록(건너뜀 제외)이 있으면 보내지 않는다.
- 세그먼트 (먹은 기록이 있는 논리 날짜 기준)
  - first_log : 기록 없음 + 가입 7일 이내(가입 2시간 이내는 제외) — 점심 슬롯
  - missed_one: 마지막 기록이 이틀 전(어제 빠짐) — 점심 슬롯
  - keep_going: 마지막 기록이 어제, 오늘 아직 — 저녁 슬롯 (연속 일수 언급)
  - lapsed    : 마지막 기록이 3~13일 전 — 점심 슬롯, 3일에 한 번까지
  - 그 외(14일 이상 공백, 가입 7일 넘은 무기록): 보내지 않는다
"""
from __future__ import annotations

import logging
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import from_db, kst_date_of, now_utc, to_kst
from app.models import MealRecord, NotificationSetting, PushToken, User
from app.models.push import PUSH_CHANNEL_LIFECYCLE
from app.push_client.base import PushClient
from app.services.game_profile import _streak_from_days
from app.services.push_send_log import claim_push_send, sent_logs_since

logger = logging.getLogger("eatlog.lifecycle_push")

# FE 수신 핸들러(src/notifications/fcm.js ROUTE_BY_TYPE)의 라우팅 키.
# 현재 FE 는 weekly_report 만 매핑돼 있어 이 값은 탭 이동 없이 앱만 연다 —
# FE 에 meal_reminder → HOME_TAB 매핑을 추가하면 홈(기록 진입점)으로 이동한다.
PUSH_DATA = {"type": "meal_reminder"}

SLOT_LUNCH = "lunch"
SLOT_DINNER = "dinner"

# 정보통신망법 야간 전송 제한 (KST). QUIET_START 시 이후 ~ QUIET_END 시 전에는 보내지 않는다.
QUIET_START_HOUR = 21
QUIET_END_HOUR = 8

FIRST_LOG_WINDOW_DAYS = 7  # 가입 후 이 일수 안에서만 first_log
FIRST_LOG_MIN_AGE = timedelta(hours=2)  # 막 가입한 사람에게 바로 보내지 않는다
LAPSED_MAX_GAP_DAYS = 13
LAPSED_INTERVAL_DAYS = 3  # lapsed 는 이 간격에 한 번까지
TEMPLATE_COOLDOWN_DAYS = 7  # 최근 이 일수 안에 받은 템플릿은 피한다
HISTORY_LOOKBACK_DAYS = 30  # 연속 일수·마지막 기록 계산에 보는 범위

# 슬롯 마감(KST, 이 시각부터는 그 슬롯을 시작하지 않는다)
SLOT_END = {SLOT_LUNCH: (15, 0), SLOT_DINNER: (20, 30)}

SEGMENT_SLOT = {
    "first_log": SLOT_LUNCH,
    "missed_one": SLOT_LUNCH,
    "keep_going": SLOT_DINNER,
    "lapsed": SLOT_LUNCH,
}


@dataclass(frozen=True)
class Template:
    template_id: str
    title: str
    body: str  # keep_going 은 {streak} 자리표시자(오늘 기록하면 이어질 연속 일수)를 쓸 수 있다


# 잇싹(그릇 속 새싹 펫)의 목소리, 해요체, 탓하지 않는 말투. 이모지는 템플릿당 🌱 하나까지.
# 음식 이름·체중·칼로리·혜택/무료 문구 금지. 제목 20자 안, 본문 핵심은 앞 37자 안.
TEMPLATES: dict[str, tuple[Template, ...]] = {
    "first_log": (
        Template(
            "first_log_after_meal",
            "잇싹이 첫 기록을 기다려요",
            "다 먹은 뒤에 기록해도 괜찮아요. 갤러리 사진이나 글로 한 끼만 남겨 볼까요?",
        ),
        Template(
            "first_log_gallery",
            "이미 먹었어도 괜찮아요",
            "갤러리에 있는 식사 사진으로도 기록할 수 있어요. 잇싹과 첫 기록을 시작해요 🌱",
        ),
        Template(
            "first_log_text",
            "사진이 없어도 기록돼요",
            "먹은 걸 글로 적기만 해도 기록이 돼요. 잇싹이 첫 한 끼를 기다리고 있어요.",
        ),
    ),
    "missed_one": (
        Template(
            "missed_one_one_meal",
            "오늘 한 끼면 충분해요",
            "어제는 쉬어 갔으니 오늘 한 끼만 남겨요. 두 번은 놓치지 않기로 해요 🌱",
        ),
        Template(
            "missed_one_after_meal",
            "잇싹이 오늘을 기다려요",
            "오늘 한 끼만 기록하면 흐름이 다시 이어져요. 먹고 나서 남겨도 괜찮아요.",
        ),
        Template(
            "missed_one_light",
            "가볍게 다시 이어가요",
            "하루 쉬어도 괜찮아요. 오늘 한 끼만 기록하면 잇싹과 다시 함께예요.",
        ),
    ),
    "keep_going": (
        Template(
            "keep_going_streak",
            "오늘도 한 끼 남겨요",
            "오늘 한 끼면 {streak}일 연속이에요. 잇싹이 저녁 기록을 기다려요 🌱",
        ),
        Template(
            "keep_going_evening",
            "오늘 기록, 잊지 않았죠?",
            "오늘 한 끼면 {streak}일 연속 기록이에요. 어제처럼 가볍게 남겨 봐요.",
        ),
        Template(
            "keep_going_grow",
            "잇싹이 쑥쑥 크고 있어요",
            "오늘 한 끼를 남기면 {streak}일 연속이에요. 먹고 나서 기록해도 돼요.",
        ),
    ),
    "lapsed": (
        Template(
            "lapsed_hello",
            "잇싹이 안부를 물어요",
            "오늘 한 끼만 남겨 볼까요? 언제 와도 잇싹은 반가워요 🌱",
        ),
        Template(
            "lapsed_easy",
            "한 끼부터 가볍게 해요",
            "사진이 없으면 글로 적어도 돼요. 오늘 한 끼만 가볍게 남겨요.",
        ),
        Template(
            "lapsed_here",
            "잇싹이 여기 있어요",
            "부담 없이 오늘 한 끼만 기록해요. 먹고 나서 남겨도 괜찮아요.",
        ),
    ),
}


@dataclass
class LifecycleRunSummary:
    slot: str
    sent: Counter = field(default_factory=Counter)  # 세그먼트별 발송 사용자 수
    skipped: Counter = field(default_factory=Counter)  # 건너뛴 이유별 사용자 수
    success: int = 0  # 토큰 단위 성공
    failure: int = 0


# --- 시각 판단 ---

def in_quiet_hours(now: datetime) -> bool:
    """야간 전송 제한 시간(21:00~08:00 KST)인지."""
    hour = to_kst(now).hour
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def lifecycle_slot_due(now: datetime, at: str, slot: str | None = None) -> bool:
    """슬롯 시각(HH:mm KST)을 지났고, 그 슬롯의 마감 전이며, 야간 제한 전인지. 잘못된 설정이면 False.

    마감(SLOT_END)이 없으면 재기동이 저녁 늦게 일어났을 때 점심 문구가 밤 9시 직전에
    한꺼번에 나가고, 발송 루프가 21시를 넘길 수 있다.
    """
    try:
        hour, minute = (int(part) for part in at.split(":"))
    except (AttributeError, ValueError):
        logger.warning("라이프사이클 푸시 슬롯 시각 설정이 올바르지 않습니다: %r", at)
        return False
    if in_quiet_hours(now):
        return False
    now_kst = to_kst(now)
    current = (now_kst.hour, now_kst.minute)
    end = SLOT_END.get(slot)
    if end is not None and current >= end:
        return False
    return current >= (hour, minute)


def lifecycle_slots() -> list[tuple[str, str]]:
    return [
        (SLOT_LUNCH, settings.lifecycle_push_lunch_time),
        (SLOT_DINNER, settings.lifecycle_push_dinner_time),
    ]


# --- 세그먼트 ---

def classify_segment(
    record_days: set[date],
    today: date,
    *,
    has_any_record: bool,
    signed_up_at: datetime,
    now: datetime,
) -> str | None:
    """사용자의 오늘 세그먼트. 보낼 대상이 아니면 None.

    record_days 는 최근 기록이 있는 논리 날짜, has_any_record 는 전체 기간 기준 기록 여부.
    """
    if today in record_days:
        return None
    if not has_any_record:
        signup_day = kst_date_of(signed_up_at, settings.day_start_hour)
        if (today - signup_day).days >= FIRST_LOG_WINDOW_DAYS:
            return None
        if now - signed_up_at < FIRST_LOG_MIN_AGE:
            return None
        return "first_log"
    past = [day for day in record_days if day < today]
    if not past:
        return None  # 오래전 기록만 있거나 미래 날짜 기록만 있음
    gap = (today - max(past)).days
    if gap == 1:
        return "keep_going"
    if gap == 2:
        return "missed_one"
    if 3 <= gap <= LAPSED_MAX_GAP_DAYS:
        return "lapsed"
    return None


def streak_if_logged_today(record_days: set[date], today: date) -> int:
    """오늘 한 끼를 기록하면 이어질 연속 일수 (game_profile 스트릭과 같은 규칙)."""
    return _streak_from_days(sorted(record_days), today) + (0 if today in record_days else 1)


def choose_template(
    segment: str, user_id: int, today: date, recent_template_ids: set[str]
) -> Template:
    """최근 7일에 받지 않은 템플릿 중 사용자·날짜로 정해지는 하나. 다 받았으면 전체에서 고른다."""
    pool = TEMPLATES[segment]
    candidates = [t for t in pool if t.template_id not in recent_template_ids] or list(pool)
    seed = zlib.crc32(f"{user_id}:{today.isoformat()}:{segment}".encode())
    return candidates[seed % len(candidates)]


def render(template: Template, streak: int) -> tuple[str, str]:
    return template.title, template.body.format(streak=streak)


# --- 조회 ---

# 로컬 식사 알림 시각이 '그 끼니로 그럴듯한' 범위(시, 양끝 포함). 운영 데이터에 24시간 피커 실수로
# 저녁 00:00~06:30, 점심 01:00 같은 값이 있다 — 그런 알림은 사실상 식사 알림 역할을 못 하므로
# 설정된 것으로 치지 않는다(그 사용자도 라이프사이클 푸시를 받는다).
LOCAL_REMINDER_PLAUSIBLE_HOURS = {"lunch": (9, 15), "dinner": (15, 23)}


def _plausible_reminder(value: str | None, meal: str) -> bool:
    if not value:
        return False
    try:
        hour = int(value.split(":")[0])
    except ValueError:
        return False
    low, high = LOCAL_REMINDER_PLAUSIBLE_HOURS[meal]
    return low <= hour <= high


def has_local_meal_reminder(setting: NotificationSetting) -> bool:
    """기기 로컬 식사 알림이 실제로 식사 시간에 울리도록 설정돼 있는지."""
    return _plausible_reminder(setting.lunch_time, "lunch") or _plausible_reminder(
        setting.dinner_time, "dinner"
    )


def lifecycle_push_recipients(db: Session) -> dict[int, list[str]]:
    """user_id → 토큰들. 전체 알림 켬(미설정=켬) + 그럴듯한 로컬 식사 알림이 없는 사용자만."""
    rows = db.execute(
        select(PushToken, NotificationSetting).outerjoin(
            NotificationSetting, NotificationSetting.user_id == PushToken.user_id
        )
    ).all()
    recipients: dict[int, list[str]] = {}
    for token, setting in rows:
        if setting is not None and (not setting.is_enabled or has_local_meal_reminder(setting)):
            continue
        recipients.setdefault(token.user_id, []).append(token.push_token)
    return recipients


def _record_days_by_user(
    db: Session, user_ids: list[int], now: datetime
) -> dict[int, set[date]]:
    since = now - timedelta(days=HISTORY_LOOKBACK_DAYS + 1)
    rows = db.execute(
        select(MealRecord.user_id, MealRecord.eaten_at).where(
            MealRecord.user_id.in_(user_ids),
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= since,
        )
    ).all()
    days: dict[int, set[date]] = {}
    for user_id, eaten_at in rows:
        days.setdefault(user_id, set()).add(kst_date_of(eaten_at, settings.day_start_hour))
    return days


def _users_with_any_record(db: Session, user_ids: list[int]) -> set[int]:
    return set(
        db.scalars(
            select(MealRecord.user_id)
            .where(
                MealRecord.user_id.in_(user_ids),
                MealRecord.deleted_at.is_(None),
                MealRecord.is_skipped.is_(False),
            )
            .distinct()
        ).all()
    )


# --- 발송 ---

def send_lifecycle_push(
    db: Session, client: PushClient, slot: str, now: datetime | None = None
) -> LifecycleRunSummary:
    """해당 슬롯(lunch/dinner) 세그먼트 사용자에게 하루 최대 1건 발송한다."""
    now = now or now_utc()
    summary = LifecycleRunSummary(slot=slot)
    if in_quiet_hours(now):
        logger.info("라이프사이클 푸시: 야간 전송 제한 시간이라 건너뜀 (slot=%s)", slot)
        return summary

    recipients = lifecycle_push_recipients(db)
    if not recipients:
        logger.info("라이프사이클 푸시 대상 없음 (slot=%s)", slot)
        return summary

    today = kst_date_of(now, settings.day_start_hour)
    user_ids = list(recipients)
    users = {
        user.id: user
        for user in db.scalars(select(User).where(User.id.in_(user_ids))).all()
    }
    record_days = _record_days_by_user(db, user_ids, now)
    has_record = _users_with_any_record(db, user_ids)

    recent_logs = sent_logs_since(
        db, user_ids, PUSH_CHANNEL_LIFECYCLE, today - timedelta(days=TEMPLATE_COOLDOWN_DAYS - 1)
    )
    recent_templates: dict[int, set[str]] = {}
    sent_today: set[int] = set()
    last_lapsed: dict[int, date] = {}
    for log in recent_logs:
        recent_templates.setdefault(log.user_id, set()).add(log.template_id)
        if log.logical_date == today:
            sent_today.add(log.user_id)
        if log.kind == "lapsed" and log.logical_date > last_lapsed.get(log.user_id, date.min):
            last_lapsed[log.user_id] = log.logical_date

    invalid_tokens: list[str] = []
    started = time.monotonic()
    for user_id, tokens in recipients.items():
        # 발송은 한 명씩 막히는 호출이라 루프가 길어질 수 있다 — 흐른 시간을 더해 다시 보고 21시가 되면 멈춘다
        if in_quiet_hours(now + timedelta(seconds=time.monotonic() - started)):
            summary.skipped["quiet_hours_reached"] += 1
            break
        user = users.get(user_id)
        if user is None:
            continue
        if user.is_test_device:
            summary.skipped["test_device"] += 1
            continue
        days = record_days.get(user_id, set())
        if today in days:
            summary.skipped["logged_today"] += 1
            continue
        segment = classify_segment(
            days,
            today,
            has_any_record=user_id in has_record,
            signed_up_at=from_db(user.created_at),
            now=now,
        )
        if segment is None:
            summary.skipped["no_segment"] += 1
            continue
        if SEGMENT_SLOT[segment] != slot:
            summary.skipped["other_slot"] += 1
            continue
        if user_id in sent_today:
            summary.skipped["already_sent"] += 1
            continue
        if segment == "lapsed":
            last = last_lapsed.get(user_id)
            if last is not None and (today - last).days < LAPSED_INTERVAL_DAYS:
                summary.skipped["lapsed_cooldown"] += 1
                continue

        template = choose_template(segment, user_id, today, recent_templates.get(user_id, set()))
        if not claim_push_send(
            db,
            user_id=user_id,
            channel=PUSH_CHANNEL_LIFECYCLE,
            kind=segment,
            template_id=template.template_id,
            logical_date=today,
            now=now,
        ):
            summary.skipped["already_sent"] += 1  # 다른 인스턴스가 먼저 보냄
            continue
        title, body = render(template, streak_if_logged_today(days, today))
        try:
            report = client.send(tokens, title, body, PUSH_DATA)
        except Exception:  # noqa: BLE001 — 한 사용자 발송 실패로 나머지·토큰 정리를 버리지 않는다
            logger.exception("라이프사이클 푸시 발송 실패 (user_id=%s)", user_id)
            summary.failure += len(tokens)
            continue
        summary.sent[segment] += 1
        summary.success += report.success_count
        summary.failure += report.failure_count
        invalid_tokens.extend(report.invalid_tokens)

    if invalid_tokens:
        stale = db.scalars(
            select(PushToken).where(PushToken.push_token.in_(invalid_tokens))
        ).all()
        for row in stale:
            db.delete(row)
        db.commit()
        logger.info("등록 해제된 푸시 토큰 %d건 정리", len(stale))

    logger.info(
        "라이프사이클 푸시 발송(slot=%s, %s): 세그먼트별 %s / 건너뜀 %s / 토큰 성공 %d 실패 %d (대상 %d명)",
        slot,
        today.isoformat(),
        dict(summary.sent),
        dict(summary.skipped),
        summary.success,
        summary.failure,
        len(recipients),
    )
    return summary
