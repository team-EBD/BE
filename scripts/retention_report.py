"""리텐션 D1/D7 베이스라인 — 읽기 전용 CLI 리포트.

게이미피케이션이 리텐션을 올렸는지 판단할 기준선이 없어서 만든다. **엔드포인트가
아니다** — 사용자 대상 기능이 아니고 인증·권한 설계가 따로 필요하다.

    .\\venv\\Scripts\\python.exe -m scripts.retention_report
    .\\venv\\Scripts\\python.exe -m scripts.retention_report --days 60
    .\\venv\\Scripts\\python.exe -m scripts.retention_report --since 2026-09-01 --until 2026-09-14
    .\\venv\\Scripts\\python.exe -m scripts.retention_report --csv > retention.csv

정의
----
- **코호트**: `users.created_at` 을 논리 날짜(KST 06:00 경계)로 옮긴 가입일.
  경계는 `settings.day_start_hour` 와 `core.timeutil.kst_date_of` 를 그대로 쓴다.
- **활성**: 그 논리 날짜에 삭제되지 않은 `meal_records` 가 1건 이상이고
  `is_skipped = false`. 생략 기록은 활동으로 세지 않는다(보상도 안 주는 기준과 동일).
  기록의 논리 날짜는 코드 전반과 같이 `eaten_at` 기준이다 — 기록 날짜 수정 기능이
  있으므로 "언제 앱을 켰나"가 아니라 "언제의 식사를 남겼나"를 센다.
- **D1 / D7**: 가입 논리 날짜 +1 일 / +7 일에 활성.
- **D1-D7 누적**: +1 ~ +7 중 하루라도 활성. 이탈 판단에는 이쪽이 더 유용하다.

쓰기는 하지 않는다 (SELECT 전용, commit 없음).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.timeutil import kst_date_of, kst_day_bounds, now_utc
from app.models import ClientEvent, MealRecord, User

# D1-D7 누적이 보는 창
WINDOW_DAYS = 7

HEADER_NOTE = (
    "※ 탈퇴한 사용자는 users 에서 사라지므로(client_events.user_id 는 SET NULL) "
    "이 표는 '남아 있는 사용자' 기준이다 — 실제 코호트보다 작게 잡히고 "
    "리텐션은 낙관적으로 나온다."
)


@dataclass(frozen=True)
class CohortRow:
    cohort_date: date
    signups: int
    d1: int
    d7: int
    d1_d7: int

    def rate(self, hits: int) -> float | None:
        """코호트가 비면 None — 0 으로 나누지 않는다."""
        if self.signups <= 0:
            return None
        return hits * 100.0 / self.signups


def _logical_date(dt) -> date:
    return kst_date_of(dt, settings.day_start_hour)


def _cohorts(db: Session, since: date, until: date) -> dict[int, date]:
    """{user_id: 가입 논리 날짜} — [since, until] 논리 날짜 범위 안의 가입자만."""
    start, _ = kst_day_bounds(since, settings.day_start_hour)
    _, end = kst_day_bounds(until, settings.day_start_hour)
    rows = db.execute(
        select(User.id, User.created_at).where(
            User.created_at >= start, User.created_at < end
        )
    ).all()
    return {user_id: _logical_date(created_at) for user_id, created_at in rows}


def _active_days(db: Session, user_ids: set[int], since: date, until: date) -> dict[int, set[date]]:
    """{user_id: 활성 논리 날짜 집합}. 생략·삭제 기록은 제외한다."""
    if not user_ids:
        return {}
    start, _ = kst_day_bounds(since, settings.day_start_hour)
    _, end = kst_day_bounds(until + timedelta(days=WINDOW_DAYS), settings.day_start_hour)
    rows = db.execute(
        select(MealRecord.user_id, MealRecord.eaten_at).where(
            MealRecord.user_id.in_(user_ids),
            MealRecord.deleted_at.is_(None),
            MealRecord.is_skipped.is_(False),
            MealRecord.eaten_at >= start,
            MealRecord.eaten_at < end,
        )
    ).all()
    out: dict[int, set[date]] = defaultdict(set)
    for user_id, eaten_at in rows:
        out[user_id].add(_logical_date(eaten_at))
    return out


def build_rows(db: Session, since: date, until: date) -> list[CohortRow]:
    """코호트일별 D1/D7/D1-D7. 가입자가 없는 날도 빈 행으로 남긴다."""
    cohorts = _cohorts(db, since, until)
    active = _active_days(db, set(cohorts), since, until)

    signups: dict[date, int] = defaultdict(int)
    d1: dict[date, int] = defaultdict(int)
    d7: dict[date, int] = defaultdict(int)
    d1_d7: dict[date, int] = defaultdict(int)

    for user_id, joined in cohorts.items():
        signups[joined] += 1
        days = active.get(user_id, set())
        if joined + timedelta(days=1) in days:
            d1[joined] += 1
        if joined + timedelta(days=WINDOW_DAYS) in days:
            d7[joined] += 1
        if any(joined + timedelta(days=n) in days for n in range(1, WINDOW_DAYS + 1)):
            d1_d7[joined] += 1

    rows: list[CohortRow] = []
    cursor = since
    while cursor <= until:
        rows.append(
            CohortRow(cursor, signups[cursor], d1[cursor], d7[cursor], d1_d7[cursor])
        )
        cursor += timedelta(days=1)
    return rows


def totals(rows: list[CohortRow]) -> CohortRow:
    """합계 행. 코호트일이 아니므로 날짜는 첫 코호트일을 그대로 둔다(표기는 '합계')."""
    return CohortRow(
        cohort_date=rows[0].cohort_date if rows else date.min,
        signups=sum(r.signups for r in rows),
        d1=sum(r.d1 for r in rows),
        d7=sum(r.d7 for r in rows),
        d1_d7=sum(r.d1_d7 for r in rows),
    )


def game_event_rows(db: Session, since: date, until: date) -> list[tuple[date, str, int]]:
    """(논리 날짜, event_type, 유니크 사용자 수) — `game_*` 이벤트만.

    개인정보 주의: `meta` 는 읽지도 출력하지도 않는다.
    """
    start, _ = kst_day_bounds(since, settings.day_start_hour)
    _, end = kst_day_bounds(until, settings.day_start_hour)
    rows = db.execute(
        select(ClientEvent.event_type, ClientEvent.user_id, ClientEvent.created_at).where(
            ClientEvent.event_type.like("game_%"),
            ClientEvent.created_at >= start,
            ClientEvent.created_at < end,
        )
    ).all()
    buckets: dict[tuple[date, str], set[int | None]] = defaultdict(set)
    for event_type, user_id, created_at in rows:
        buckets[(_logical_date(created_at), event_type)].add(user_id)
    return sorted(
        ((day, event_type, len(users)) for (day, event_type), users in buckets.items()),
        key=lambda r: (r[0], r[1]),
    )


# --- 출력 ---

def _cell(hits: int, rate: float | None) -> str:
    return "-" if rate is None else f"{hits} ({rate:.1f}%)"


def render_table(rows: list[CohortRow], events: list[tuple[date, str, int]]) -> str:
    out = [HEADER_NOTE, ""]
    out.append(f"{'코호트일':<14}{'가입':>5}  {'D1':<12}{'D7':<12}{'D1-D7':<12}")
    for row in rows:
        out.append(
            f"{row.cohort_date.isoformat():<14}{row.signups:>5}  "
            f"{_cell(row.d1, row.rate(row.d1)):<12}"
            f"{_cell(row.d7, row.rate(row.d7)):<12}"
            f"{_cell(row.d1_d7, row.rate(row.d1_d7)):<12}"
        )
    total = totals(rows)
    out.append(
        f"{'합계':<13}{total.signups:>5}  "
        f"{_cell(total.d1, total.rate(total.d1)):<12}"
        f"{_cell(total.d7, total.rate(total.d7)):<12}"
        f"{_cell(total.d1_d7, total.rate(total.d1_d7)):<12}"
    )
    out.append("")
    out.append("게임 이벤트 (일자별 유니크 사용자 수)")
    if not events:
        out.append("  (없음)")
    for day, event_type, users in events:
        out.append(f"  {day.isoformat()}  {event_type:<32}{users:>6}")
    return "\n".join(out)


def write_csv(rows: list[CohortRow], events: list[tuple[date, str, int]], stream) -> None:
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        ["section", "cohort_date", "signups", "d1", "d1_rate", "d7", "d7_rate", "d1_d7", "d1_d7_rate"]
    )

    def pct(value: float | None) -> str:
        return "" if value is None else f"{value:.1f}"

    for row in rows:
        writer.writerow([
            "cohort", row.cohort_date.isoformat(), row.signups,
            row.d1, pct(row.rate(row.d1)),
            row.d7, pct(row.rate(row.d7)),
            row.d1_d7, pct(row.rate(row.d1_d7)),
        ])
    total = totals(rows)
    writer.writerow([
        "total", "", total.signups,
        total.d1, pct(total.rate(total.d1)),
        total.d7, pct(total.rate(total.d7)),
        total.d1_d7, pct(total.rate(total.d1_d7)),
    ])
    writer.writerow([])
    writer.writerow(["section", "date", "event_type", "unique_users"])
    for day, event_type, users in events:
        writer.writerow(["game_event", day.isoformat(), event_type, users])


def resolve_range(
    days: int, since: str | None, until: str | None, today: date | None = None
) -> tuple[date, date]:
    """옵션 → [since, until] 코호트일 범위. `--since/--until` 이 `--days` 보다 우선한다."""
    today = today or kst_date_of(now_utc(), settings.day_start_hour)
    end = date.fromisoformat(until) if until else today
    if since:
        start = date.fromisoformat(since)
    else:
        start = end - timedelta(days=max(days, 1) - 1)
    if start > end:
        raise SystemExit("--since 가 --until 보다 뒤입니다.")
    return start, end


def report(
    session_factory=SessionLocal,
    *,
    days: int = 30,
    since: str | None = None,
    until: str | None = None,
    as_csv: bool = False,
    stream=None,
) -> None:
    start, end = resolve_range(days, since, until)
    stream = stream or sys.stdout
    with session_factory() as db:
        rows = build_rows(db, start, end)
        events = game_event_rows(db, start, end)
    if as_csv:
        # CSV 본문에 섞으면 열이 밀린다 — 한계 고지는 stderr 로 (리다이렉트해도 보인다)
        print(HEADER_NOTE, file=sys.stderr)
        write_csv(rows, events, stream)
    else:
        print(render_table(rows, events), file=stream)


def main() -> None:  # pragma: no cover — 인자 파싱만
    parser = argparse.ArgumentParser(description="리텐션 D1/D7 베이스라인 (읽기 전용)")
    parser.add_argument("--days", type=int, default=30, help="최근 N 개 코호트일 (기본 30)")
    parser.add_argument("--since", help="시작 코호트일 YYYY-MM-DD")
    parser.add_argument("--until", help="끝 코호트일 YYYY-MM-DD")
    parser.add_argument("--csv", action="store_true", help="표 대신 CSV 로 출력")
    args = parser.parse_args()
    report(days=args.days, since=args.since, until=args.until, as_csv=args.csv)


if __name__ == "__main__":  # pragma: no cover
    main()
