"""인앱 결제(구독) 도메인 모델.

Google Play / App Store 의 자동 갱신 구독을 서버가 검증한 결과를 보관한다.
스토어가 **단일 진실 원천**이고 이 테이블은 그 스냅샷이다 — 클라이언트가 보낸
값을 그대로 믿지 않고, 항상 스토어 API 로 조회한 결과만 저장한다.

한 사용자가 여러 행을 가질 수 있다 (해지 후 재구독하면 안드로이드는 새
purchaseToken 이 발급된다). '현재 프리미엄인가'는 행 하나가 아니라
services/subscription.py 의 entitlement 규칙으로 판단한다.
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._common import (
    TZDateTime,
    created_at_column,
    pk_column,
    updated_at_column,
)

# 스토어 상태를 서비스 공통어로 정규화한 값
# active(정상) / grace(결제 실패 유예 — 권한 유지) / canceled(해지 예약 — 만료까지 유지)
# on_hold(결제 보류) / paused(일시정지) / expired(만료) / revoked(환불·회수) / pending(결제 대기)
SUBSCRIPTION_STATUSES = (
    "active",
    "grace",
    "canceled",
    "on_hold",
    "paused",
    "expired",
    "revoked",
    "pending",
)

# 권한(프리미엄)을 부여하는 상태 — 만료 시각 조건은 별도로 확인한다
ENTITLED_STATUSES = frozenset({"active", "grace", "canceled"})


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = pk_column()
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # android/ios
    product_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # 스토어 구매를 유일하게 식별하는 키.
    #   android: purchaseToken / ios: originalTransactionId
    # (platform, purchase_key) 유일 — 같은 영수증을 여러 계정이 등록하는 것을 막는다.
    purchase_key: Mapped[str] = mapped_column(String(512), nullable=False)
    # 최근 주문/트랜잭션 식별자 (스토어 콘솔 대조·CS 용)
    latest_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    is_auto_renewing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default="production", server_default="production"
    )
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True, index=True)
    canceled_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    # 스토어 응답 원문(JSON) — 상태 판정이 어긋났을 때 원인 추적용. 길이 상한을 둔다.
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 마지막으로 스토어 API 를 조회한 시각 (재조회 주기 판단)
    verified_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        UniqueConstraint("platform", "purchase_key", name="uq_subscriptions_platform_key"),
    )
