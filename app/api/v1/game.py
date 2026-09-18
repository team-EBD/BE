"""게이미피케이션 라우터 (로드맵 v3 — 함께 크는 펫).

기존 기록·영양 API 는 전혀 건드리지 않는 **추가 전용** 도메인이다. 이 라우터가
죽어도 식단 기록은 정상 동작해야 하므로, FE 도 게임 응답을 선택적으로 다룬다.

경로 선언 순서 주의: /pets/{code} 보다 정적 경로를 먼저 등록한다.
"""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import DB, CurrentUser
from app.core.errors import APIError
from app.core.timeutil import now_utc
from app.models import (
    CatalogItem,
    GameProfile,
    PetBond,
    RewardLedger,
    StagePlacement,
    User,
    UserItem,
    UserSkill,
)
from app.schemas.game import (
    ActivePet,
    ClaimableReward,
    CollectionItem,
    CollectionResponse,
    EquipSkillRequest,
    EquippedSkill,
    EventClaimRequest,
    EventClaimResponse,
    EventsResponse,
    FirstFriendOffer,
    FirstFriendRequest,
    FirstFriendResponse,
    GameHomeResponse,
    GameProfileBrief,
    GrowthStep,
    MissionClaimResponse,
    MissionOut,
    MissionsResponse,
    NextUnlock,
    PetDetailResponse,
    PetNicknameRequest,
    PurchaseRequest,
    PurchaseResponse,
    ShopResponse,
    SkillOut,
    SkillsResponse,
    StageOut,
    StagePlacementOut,
    StageSaveRequest,
)
from app.services import game_catalog as catalog
from app.services import game_events, game_missions
from app.services.game_profile import (
    ensure_catalog,
    ensure_game_profile,
    ensure_pet_bond,
    logical_today,
    owned_items,
    stage_placements,
)
from app.services.game_rewards import food_progress_by_code, next_unlock

router = APIRouter(prefix="/game", tags=["game"])


def _forget(db: Session, obj) -> None:
    """savepoint 롤백으로 이미 빠졌을 수도 있는 객체를 안전하게 세션에서 뗀다."""
    if obj in db:
        db.expunge(obj)


def _short_name(name: str) -> str:
    """'모카 고양이' → '모카'. 펫을 부르는 이름은 짧을수록 애착이 붙는다."""
    return name.split(" ")[0] if name else name


def _bonds(db: Session, user_id: int) -> dict[str, PetBond]:
    return {
        bond.pet_code: bond
        for bond in db.scalars(select(PetBond).where(PetBond.user_id == user_id))
    }


def _next_reward_label(pet_code: str, bond_level: int) -> str | None:
    """다음 유대 레벨에서 열리는 것 — '초록 스카프 + 쉼표 지킴이'."""
    layers = catalog.growth_layers(pet_code)
    next_level = bond_level + 1
    if next_level > len(catalog.bond_levels()):
        return None
    parts: list[str] = []
    if next_level - 1 < len(layers):
        parts.append(catalog.layer_label(layers[next_level - 1]))
    if next_level == catalog.skill_unlock_bond_level():
        skill = catalog.signature_skill(pet_code)
        if skill:
            parts.append(catalog.skill_name(skill))
    return " + ".join(parts) or None


def _active_pet(db: Session, profile: GameProfile) -> ActivePet | None:
    pet_code = profile.active_pet_code
    entry = catalog.get(pet_code) if pet_code else None
    if entry is None:
        return None
    bond = ensure_pet_bond(db, profile.user_id, pet_code)
    return ActivePet(
        item_code=pet_code,
        display_name=bond.nickname or _short_name(entry.name),
        name=entry.name,
        preview_key=entry.preview_key,
        bond_level=bond.bond_level,
        bond_level_name=catalog.bond_level_name(bond.bond_level),
        bond_days=bond.bond_days,
        next_level_days=catalog.next_bond_level_days(bond.bond_days),
        next_reward=_next_reward_label(pet_code, bond.bond_level),
        growth_layers=catalog.unlocked_growth_layers(pet_code, bond.bond_level),
    )


def _equipped_skill(db: Session, profile: GameProfile) -> EquippedSkill | None:
    code = profile.equipped_skill_code
    if not code:
        return None
    skill = db.scalar(
        select(UserSkill).where(
            UserSkill.user_id == profile.user_id, UserSkill.skill_code == code
        )
    )
    if skill is None:
        return None
    return EquippedSkill(
        code=code,
        name=catalog.skill_name(code),
        charges=skill.charge_count,
        max_charges=catalog.skill_max_charges(code),
        is_active=code in catalog.ACTIVE_SKILL_CODES,
    )


def _stage(db: Session, user_id: int) -> StageOut:
    placements = []
    for placement, code in stage_placements(db, user_id):
        entry = catalog.get(code)
        if entry is None:  # pragma: no cover — 카탈로그에서 사라진 코드는 무시
            continue
        placements.append(
            StagePlacementOut(
                slot_type=placement.slot_type,
                slot_index=placement.slot_index,
                item_code=code,
                preview_key=entry.preview_key,
                category=entry.category,
                transform=placement.transform,
            )
        )
    return StageOut(revision=0, placements=placements)


def _first_friend(profile: GameProfile) -> FirstFriendOffer:
    required = catalog.first_friend_required_days()
    return FirstFriendOffer(
        available=(
            profile.first_friend_claimed_at is None
            and profile.total_record_days >= required
        ),
        required_days=required,
        current_days=profile.total_record_days,
        choices=catalog.first_friend_choices(),
    )


def _profile_brief(profile: GameProfile) -> GameProfileBrief:
    return GameProfileBrief(
        points=profile.points,
        xp=profile.xp,
        level=profile.level,
        xp_to_next_level=catalog.xp_to_next_level(profile.level),
        current_streak=profile.current_streak,
        best_streak=profile.best_streak,
        total_record_days=profile.total_record_days,
        logical_date=logical_today().isoformat(),
    )


def build_home(db: Session, user: User) -> GameHomeResponse:
    profile = ensure_game_profile(db, user)
    stage = _stage(db, user.id)
    stage.revision = profile.stage_revision
    unlock = next_unlock(db, profile)
    today = logical_today()
    # 완료했지만 아직 안 받은 것 — 미션이 먼저, 이벤트가 뒤 (§6.3)
    claimable = [
        *game_missions.claimable(db, user.id, today),
        *game_events.claimable(db, user.id, today),
    ]
    return GameHomeResponse(
        profile=_profile_brief(profile),
        active_pet=_active_pet(db, profile),
        equipped_skill=_equipped_skill(db, profile),
        stage=stage,
        next_unlock=NextUnlock(**unlock) if unlock else None,
        first_friend=_first_friend(profile),
        claimable=[ClaimableReward(**item) for item in claimable],
    )


# --- 조회 ---

@router.get("/home", response_model=GameHomeResponse)
def game_home(user: CurrentUser, db: DB) -> GameHomeResponse:
    """홈 무대. 최초 호출 시 기본 펫·배경과 프로필을 idempotent 하게 지급한다."""
    home = build_home(db, user)
    db.commit()
    return home


def _unlock_progress(entry, rows: dict) -> dict | None:
    """음식 해금의 진행도 — {"current":3,"target":5,"unit":"day"|"menu"}.

    아직 진행이 없으면 0/target 으로 내려 카드가 목표를 보여줄 수 있게 한다.
    보유 중인 아이템은 호출부에서 None 으로 둔다.
    """
    if entry.unlock.get("type") != "food":
        return None
    target = catalog.food_unlock_target(entry)
    if target <= 0:
        return None
    row = rows.get(entry.code)
    return {
        "current": min(row.current_value, target) if row is not None else 0,
        "target": target,
        "unit": catalog.food_unlock_unit(entry),
    }


def _collection_items(
    db: Session, profile: GameProfile, *, shop_only: bool
) -> list[CollectionItem]:
    ensure_catalog(db)
    owned = owned_items(db, profile.user_id)
    active = {code for _placement, code in stage_placements(db, profile.user_id)}
    bonds = _bonds(db, profile.user_id)
    progress_rows = food_progress_by_code(db, profile.user_id)

    items: list[CollectionItem] = []
    for entry in catalog.entries():
        is_owned = entry.code in owned
        unlock_type = entry.unlock.get("type")
        for_sale = entry.price is not None and unlock_type in (
            "coin",
            "coin_or_first_friend",
        )
        if shop_only and (is_owned or not for_sale):
            continue

        locked_reason = None
        if not is_owned:
            if not for_sale:
                locked_reason = "NOT_FOR_SALE"
            elif entry.min_level and profile.level < entry.min_level:
                locked_reason = "LOCKED_LEVEL"
            elif profile.points < (entry.price or 0):
                locked_reason = "INSUFFICIENT_POINTS"

        bond = bonds.get(entry.code)
        skill = catalog.signature_skill(entry.code)
        progress = _unlock_progress(entry, progress_rows) if not is_owned else None
        items.append(
            CollectionItem(
                code=entry.code,
                name=entry.name,
                category=entry.category,
                preview_key=entry.preview_key,
                owned=is_owned,
                active=entry.code in active,
                price=entry.price,
                unlock=entry.unlock,
                bond_days=bond.bond_days if bond else None,
                bond_level=bond.bond_level if bond else None,
                signature_skill=skill,
                signature_skill_name=catalog.skill_name(skill) if skill else None,
                locked_reason=locked_reason,
                progress=progress,
            )
        )
    return items


@router.get("/collection", response_model=CollectionResponse)
def game_collection(user: CurrentUser, db: DB) -> CollectionResponse:
    profile = ensure_game_profile(db, user)
    items = _collection_items(db, profile, shop_only=False)
    db.commit()
    return CollectionResponse(
        slot_limits=catalog.slot_limits(), points=profile.points, items=items
    )


@router.get("/shop", response_model=ShopResponse)
def game_shop(user: CurrentUser, db: DB) -> ShopResponse:
    """코인으로 살 수 있고 아직 보유하지 않은 상품만."""
    profile = ensure_game_profile(db, user)
    items = _collection_items(db, profile, shop_only=True)
    db.commit()
    return ShopResponse(points=profile.points, items=items)


@router.get("/skills", response_model=SkillsResponse)
def game_skills(user: CurrentUser, db: DB) -> SkillsResponse:
    profile = ensure_game_profile(db, user)
    owned = {
        row.skill_code: row
        for row in db.scalars(select(UserSkill).where(UserSkill.user_id == user.id))
    }
    db.commit()
    out = []
    for code, spec in catalog.skills().items():
        row = owned.get(code)
        if row is None:
            continue
        out.append(
            SkillOut(
                code=code,
                name=catalog.skill_name(code),
                description=catalog.skill_description(code),
                source_pet_code=row.source_pet_code,
                unlock_bond_level=int(spec.get("unlock_bond_level", 3)),
                charges=row.charge_count,
                max_charges=catalog.skill_max_charges(code),
                is_active=code in catalog.ACTIVE_SKILL_CODES,
                equipped=profile.equipped_skill_code == code,
            )
        )
    return SkillsResponse(equipped_skill_code=profile.equipped_skill_code, skills=out)


# --- 미션 ---

@router.get("/missions", response_model=MissionsResponse)
def game_missions_list(user: CurrentUser, db: DB) -> MissionsResponse:
    """오늘의 일일 3개(티어당 1) + 이번 주 1개. 같은 날 다시 조회해도 같은 미션이다."""
    profile = ensure_game_profile(db, user)
    data = game_missions.list_missions(db, profile, logical_today())
    db.commit()
    return MissionsResponse(**data)


@router.post("/missions/{code}/claim", response_model=MissionClaimResponse)
def claim_mission(code: str, user: CurrentUser, db: DB) -> MissionClaimResponse:
    """완료된 미션의 보상을 수령한다 (자동 지급하지 않는다)."""
    profile = ensure_game_profile(db, user)
    result = game_missions.claim_mission(db, profile, code, logical_today())
    db.commit()
    return MissionClaimResponse(**result)


@router.post("/missions/{code}/swap", response_model=MissionOut)
def swap_mission(code: str, user: CurrentUser, db: DB) -> MissionOut:
    """'오늘의 바꾸기' — 시작 전 일일 미션 하나를 같은 티어 안에서 바꾼다 (일 1회)."""
    profile = ensure_game_profile(db, user)
    result = game_missions.swap_mission(db, profile, code, logical_today())
    db.commit()
    return MissionOut(**result)


# --- 이벤트 ---

@router.get("/events", response_model=EventsResponse)
def game_events_list(user: CurrentUser, db: DB) -> EventsResponse:
    """기간 중인 이벤트 + 내 진행도 + 보상 도달/수령 여부."""
    ensure_game_profile(db, user)
    events = game_events.list_events(db, user.id, logical_today())
    db.commit()
    return EventsResponse(events=events)


@router.post("/events/{code}/claim", response_model=EventClaimResponse)
def claim_event_reward(
    code: str, body: EventClaimRequest, user: CurrentUser, db: DB
) -> EventClaimResponse:
    """스탬프(또는 완료 미션 수) 보상 1건을 수령한다. 코인으로는 살 수 없다."""
    profile = ensure_game_profile(db, user)
    threshold = body.threshold()
    if threshold is None:
        raise APIError(409, "EVENT_REWARD_NOT_REACHED", "받을 보상을 지정해 주세요.")
    result = game_events.claim_reward(db, profile, code, threshold, logical_today())
    db.commit()
    return EventClaimResponse(**result)


# --- 무대 배치 ---

def _validate_placements(
    db: Session, profile: GameProfile, body: StageSaveRequest
) -> list[tuple[StagePlacement, CatalogItem]]:
    limits = catalog.slot_limits()
    owned = owned_items(db, profile.user_id)
    rows = {row.code: row for row in db.scalars(select(CatalogItem))}

    seen: set[tuple[str, int]] = set()
    per_type: dict[str, int] = {}
    resolved = []
    for item in body.placements:
        entry = catalog.get(item.item_code)
        if entry is None or item.item_code not in rows:
            raise APIError(404, "GAME_ITEM_NOT_FOUND", "존재하지 않는 아이템입니다.")
        if entry.category != item.slot_type:
            raise APIError(
                400, "ITEM_CATEGORY_MISMATCH", "아이템 종류와 슬롯이 맞지 않습니다."
            )
        if item.item_code not in owned:
            raise APIError(400, "ITEM_NOT_OWNED", "보유하지 않은 아이템입니다.")
        limit = limits.get(item.slot_type, 1)
        if item.slot_index >= limit:
            raise APIError(
                400, "STAGE_LIMIT_EXCEEDED", f"{item.slot_type} 슬롯은 {limit}개까지예요."
            )
        key = (item.slot_type, item.slot_index)
        if key in seen:
            raise APIError(400, "STAGE_LIMIT_EXCEEDED", "같은 슬롯을 두 번 채울 수 없어요.")
        seen.add(key)
        per_type[item.slot_type] = per_type.get(item.slot_type, 0) + 1
        if per_type[item.slot_type] > limit:
            raise APIError(
                400, "STAGE_LIMIT_EXCEEDED", f"{item.slot_type} 슬롯은 {limit}개까지예요."
            )
        resolved.append((item, rows[item.item_code]))
    return resolved


@router.put("/stage", response_model=StageOut)
def save_stage(body: StageSaveRequest, user: CurrentUser, db: DB) -> StageOut:
    """무대 배치 저장. revision 이 어긋나면 409 로 막아 마지막 쓰기를 지킨다."""
    profile = ensure_game_profile(db, user)
    if body.revision != profile.stage_revision:
        raise APIError(
            409, "STAGE_REVISION_CONFLICT", "다른 기기에서 무대가 먼저 바뀌었어요."
        )
    resolved = _validate_placements(db, profile, body)

    for old in db.scalars(
        select(StagePlacement).where(StagePlacement.user_id == user.id)
    ):
        db.delete(old)
    db.flush()

    for item, row in resolved:
        db.add(
            StagePlacement(
                user_id=user.id,
                slot_type=item.slot_type,
                slot_index=item.slot_index,
                catalog_item_id=row.id,
                transform=item.transform,
            )
        )
    # 무대의 펫이 곧 보상이 귀속되는 활성 펫이다
    pets = [item.item_code for item, _row in resolved if item.slot_type == "pet"]
    if pets:
        profile.active_pet_code = pets[0]
        ensure_pet_bond(db, user.id, pets[0])
    profile.stage_revision += 1
    db.flush()

    stage = _stage(db, user.id)
    stage.revision = profile.stage_revision
    db.commit()
    return stage


# --- 상점 구매 ---

@router.post("/shop/purchase", response_model=PurchaseResponse)
def purchase_item(body: PurchaseRequest, user: CurrentUser, db: DB) -> PurchaseResponse:
    """잎 코인으로 아이템을 산다. 같은 idempotency_key 는 두 번 차감하지 않는다."""
    profile = ensure_game_profile(db, user)
    items = ensure_catalog(db)

    entry = catalog.get(body.item_code)
    row = items.get(body.item_code)
    if entry is None or row is None:
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "존재하지 않는 아이템입니다.")

    key = f"purchase:{body.idempotency_key}"
    already = db.scalar(
        select(RewardLedger.id).where(
            RewardLedger.user_id == user.id, RewardLedger.idempotency_key == key
        )
    )
    if already is not None:
        db.commit()
        return PurchaseResponse(
            item_code=body.item_code, points=profile.points, already_purchased=True
        )

    owned = db.scalar(
        select(UserItem.id).where(
            UserItem.user_id == user.id, UserItem.catalog_item_id == row.id
        )
    )
    if owned is not None:
        raise APIError(409, "GAME_ITEM_ALREADY_OWNED", "이미 가지고 있어요.")
    if entry.price is None or entry.unlock.get("type") not in ("coin", "coin_or_first_friend"):
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "상점에서 판매하지 않는 아이템입니다.")
    if entry.min_level and profile.level < entry.min_level:
        raise APIError(409, "LEVEL_LOCKED", f"Lv.{entry.min_level}부터 입양할 수 있어요.")
    if profile.points < entry.price:
        raise APIError(409, "INSUFFICIENT_POINTS", "잎 코인이 부족해요.")

    ledger = RewardLedger(
        user_id=user.id,
        points_delta=-entry.price,
        reason="purchase",
        ref_type="catalog_item",
        ref_id=body.item_code,
        idempotency_key=key,
    )
    grant = UserItem(user_id=user.id, catalog_item_id=row.id, source="purchase")
    try:
        with db.begin_nested():
            db.add(ledger)
            db.add(grant)
            db.flush()
    except IntegrityError:
        # 같은 키/아이템으로 동시에 들어온 요청 — 차감 없이 현재 상태를 돌려준다
        _forget(db, ledger)
        _forget(db, grant)
        db.commit()
        return PurchaseResponse(
            item_code=body.item_code, points=profile.points, already_purchased=True
        )

    profile.points -= entry.price
    if entry.category == "pet":
        ensure_pet_bond(db, user.id, body.item_code)
    db.commit()
    return PurchaseResponse(item_code=body.item_code, points=profile.points)


# --- 첫 친구 무료 선택 ---

@router.post("/adoption/first-choice", response_model=FirstFriendResponse)
def claim_first_friend(
    body: FirstFriendRequest, user: CurrentUser, db: DB
) -> FirstFriendResponse:
    """서로 다른 3개 기록일을 채우면 강아지·토끼·여우 중 1마리를 무료로 받는다."""
    profile = ensure_game_profile(db, user)
    items = ensure_catalog(db)

    if body.item_code not in catalog.first_friend_choices():
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "첫 친구로 고를 수 없는 펫이에요.")
    if profile.first_friend_claimed_at is not None:
        db.commit()
        return FirstFriendResponse(item_code=body.item_code, already_claimed=True)
    if profile.total_record_days < catalog.first_friend_required_days():
        raise APIError(
            409,
            "FIRST_FRIEND_NOT_READY",
            f"서로 다른 {catalog.first_friend_required_days()}일을 기록하면 고를 수 있어요.",
        )

    row = items[body.item_code]
    grant = UserItem(user_id=user.id, catalog_item_id=row.id, source="first_friend")
    try:
        with db.begin_nested():
            db.add(grant)
            db.flush()
    except IntegrityError:
        _forget(db, grant)
    profile.first_friend_claimed_at = now_utc()
    ensure_pet_bond(db, user.id, body.item_code)
    db.commit()
    return FirstFriendResponse(item_code=body.item_code)


# --- 지원 스킬 장착 ---

@router.put("/skills/equipped", response_model=SkillsResponse)
def equip_skill(body: EquipSkillRequest, user: CurrentUser, db: DB) -> SkillsResponse:
    """계정 전체에서 지원 스킬은 한 번에 1개만 장착한다 (해제는 null)."""
    profile = ensure_game_profile(db, user)
    if body.skill_code is not None:
        owned = db.scalar(
            select(UserSkill.id).where(
                UserSkill.user_id == user.id, UserSkill.skill_code == body.skill_code
            )
        )
        if owned is None:
            raise APIError(
                400, "SKILL_NOT_UNLOCKED", "아직 배우지 않은 스킬이에요."
            )
    profile.equipped_skill_code = body.skill_code
    db.commit()
    return game_skills(user, db)


# --- 펫 상세 ---

@router.get("/pets/{code}", response_model=PetDetailResponse)
def pet_detail(code: str, user: CurrentUser, db: DB) -> PetDetailResponse:
    """0·3·7·14·30일 성장 타임라인을 처음부터 전부 공개한다 (v3 §3)."""
    profile = ensure_game_profile(db, user)
    entry = catalog.get(code)
    if entry is None or entry.category != "pet":
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "존재하지 않는 펫입니다.")

    owned = owned_items(db, user.id)
    bond = _bonds(db, user.id).get(code)
    bond_days = bond.bond_days if bond else 0
    bond_level = bond.bond_level if bond else 1
    skill = catalog.signature_skill(code)
    layers = catalog.growth_layers(code)

    growth = []
    for level, days, name in catalog.bond_levels():
        layer = layers[level - 1] if level - 1 < len(layers) else layers[-1]
        rewards = [catalog.layer_label(layer)]
        if level == catalog.skill_unlock_bond_level() and skill:
            rewards.append(catalog.skill_name(skill))
        growth.append(
            GrowthStep(
                level=level,
                days=days,
                name=name,
                layer=layer,
                layer_label=catalog.layer_label(layer),
                reward=" + ".join(rewards),
                reached=bond_days >= days and code in owned,
            )
        )

    db.commit()
    return PetDetailResponse(
        item_code=code,
        name=entry.name,
        display_name=(bond.nickname if bond and bond.nickname else _short_name(entry.name)),
        preview_key=entry.preview_key,
        owned=code in owned,
        active=profile.active_pet_code == code,
        bond_days=bond_days,
        bond_level=bond_level,
        bond_level_name=catalog.bond_level_name(bond_level),
        next_level_days=catalog.next_bond_level_days(bond_days),
        growth=growth,
        signature_skill=skill,
        signature_skill_name=catalog.skill_name(skill) if skill else None,
        price=entry.price,
        unlock=entry.unlock,
    )


@router.patch("/pets/{code}", response_model=PetDetailResponse)
def rename_pet(
    code: str, body: PetNicknameRequest, user: CurrentUser, db: DB
) -> PetDetailResponse:
    """펫 이름 지어주기 (빈 값이면 기본 이름으로 되돌린다)."""
    ensure_game_profile(db, user)
    if catalog.get(code) is None:
        raise APIError(404, "GAME_ITEM_NOT_FOUND", "존재하지 않는 펫입니다.")
    if code not in owned_items(db, user.id):
        raise APIError(400, "ITEM_NOT_OWNED", "아직 함께하지 않는 펫이에요.")
    bond = ensure_pet_bond(db, user.id, code)
    nickname = (body.nickname or "").strip()
    bond.nickname = nickname or None
    db.commit()
    return pet_detail(code, user, db)
