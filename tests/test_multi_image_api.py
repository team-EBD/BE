"""사진 최대 세 장: 분석부터 저장·조회·수정·보존까지 API 계약 검증."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.ai_client import get_ai_client
from app.ai_client.base import AICandidate, failed_analyze
from app.ai_client.mock import MockAIClient
from app.core.config import settings
from app.main import app
from app.models import AiCallLog, AiCallLogImage, FoodCandidate, MealImage, MealItem, MealRecord, MealRecordImage
from app.services.image_retention import retention_cutoff_utc
from tests.conftest import login
from tests.test_images import upload
from tests.test_meals import MEAL_PAYLOAD, create_meal
from tests.test_meal_bbox_integrity import BOX_A, BOX_B


def photos(client, headers, count=2):
    return [upload(client, headers).json()['meal_image_id'] for _ in range(count)]


def analyze(client, headers, ids):
    res = client.post('/v1/meals/analyze', headers=headers, json={'meal_image_ids': ids})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize('count', [1, 2, 3])
def test_analysis_uses_one_call_and_one_credit(client, auth_headers, db_factory, monkeypatch, count):
    monkeypatch.setattr(settings, 'ai_premium_gate', True)
    ids = photos(client, auth_headers, count)
    calls, checks = [], []
    class Recorder(MockAIClient):
        def analyze(self, image_url, eating_habits=None, **kwargs):
            calls.append((image_url, kwargs))
            return super().analyze(image_url, eating_habits, **kwargs)
    app.dependency_overrides[get_ai_client] = lambda: Recorder()
    from app.api.v1 import meals
    original = meals.enforce_daily_limit
    def enforce(*args):
        checks.append(1)
        return original(*args)
    monkeypatch.setattr(meals, 'enforce_daily_limit', enforce)
    before = client.get('/v1/usage/daily', headers=auth_headers).json()
    body = analyze(client, auth_headers, ids)
    assert body['image_ids'] == ids
    assert len(calls) == len(checks) == 1
    assert calls[0][1]['max_foods'] == 5 * count
    assert len(calls[0][1]['image_urls']) == count
    assert calls[0][0] == calls[0][1]['image_urls'][0]
    assert [(c['image_index'], c['meal_image_id']) for c in body['candidates']] == [
        (0,ids[0]), (0,ids[0]), (0,ids[0]), (min(count-1,1),ids[min(count-1,1)]),
    ]
    after = client.get('/v1/usage/daily', headers=auth_headers).json()
    assert after['analyze']['used'] == before['analyze']['used'] + 1
    assert after['free_credits']['remaining'] == before['free_credits']['remaining'] - 1
    with db_factory() as db:
        assert db.scalar(select(func.count(AiCallLog.id))) == 1
        log = db.get(AiCallLog, body['ai_call_log_id'])
        assert (log.meal_image_id, log.input_type, log.input_image_count) == (ids[0], 'photo', count)
        links = db.scalars(select(AiCallLogImage).order_by(AiCallLogImage.position)).all()
        assert [(r.meal_image_id, r.position, r.status, r.fail_reason) for r in links] == [(iid, i, 'ok', None) for i, iid in enumerate(ids)]
        for c in body['candidates']:
            assert db.get(FoodCandidate, c['food_candidate_id']).meal_image_id == c['meal_image_id']


@pytest.mark.parametrize('endpoint', ['/v1/meals/analyze', '/v1/meals'])
@pytest.mark.parametrize('case', ['four', 'duplicate', 'mismatch', 'empty_conflict', 'foreign', 'missing'])
def test_invalid_image_sets_never_write(client, auth_headers, db_factory, endpoint, case):
    ids = photos(client, auth_headers, 4)
    payload = {} if endpoint.endswith('analyze') else dict(MEAL_PAYLOAD)
    expected = 400
    if case == 'four':
        payload['meal_image_ids'] = ids
    elif case == 'duplicate':
        payload['meal_image_ids'] = [ids[0], ids[0]]
    elif case == 'mismatch':
        payload.update(meal_image_id=ids[1], meal_image_ids=ids[:2])
    elif case == 'empty_conflict':
        payload.update(meal_image_id=ids[0], meal_image_ids=[])
    elif case == 'foreign':
        other = login(client, 'other')
        other_headers = {'Authorization': f"Bearer {other['access_token']}"}
        payload['meal_image_ids'] = [ids[0], photos(client, other_headers, 1)[0]]
        expected = 403
    else:
        payload['meal_image_ids'] = [ids[0], 99999]
        expected = 404
    res = client.post(endpoint, headers=auth_headers, json=payload)
    assert res.status_code == expected, res.text
    if expected == 400:
        assert res.json()['error']['code'] == 'VALIDATION_ERROR'
    with db_factory() as db:
        for model in (AiCallLog, AiCallLogImage, MealRecord, MealRecordImage):
            assert db.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize('payload', [{}, {'meal_image_ids': []}, {'meal_image_id': None}])
def test_analysis_requires_photo(client, auth_headers, payload):
    assert client.post('/v1/meals/analyze', headers=auth_headers, json=payload).status_code == 400


@pytest.mark.parametrize('failed_index', [0, 1, 2, None, 99])
def test_failure_is_atomic_and_records_failed_position(client, auth_headers, db_factory, failed_index):
    ids = photos(client, auth_headers, 3)
    class Failure(MockAIClient):
        def analyze(self, *args, **kwargs):
            if failed_index is None:
                return failed_analyze('ai_timeout')
            return MockAIClient(fail_image_index=failed_index).analyze(*args, **kwargs)
    app.dependency_overrides[get_ai_client] = lambda: Failure()
    body = analyze(client, auth_headers, ids)
    valid = failed_index if failed_index in (0, 1, 2) else None
    assert body['status'] == 'failed'
    assert body['failed_image_index'] == valid
    assert body['failed_meal_image_id'] == (ids[valid] if valid is not None else None)
    assert client.get('/v1/usage/daily', headers=auth_headers).json()['analyze']['used'] == 0
    with db_factory() as db:
        assert db.scalar(select(func.count(FoodCandidate.id))) == 0
        assert db.get(AiCallLog, body['ai_call_log_id']).input_image_count == 3
        rows = db.scalars(select(AiCallLogImage).order_by(AiCallLogImage.position)).all()
        assert [r.status for r in rows] == ['failed' if i == valid else 'ok' for i in range(3)]
        assert [r.fail_reason for r in rows] == [body['reason'] if i == valid else None for i in range(3)]


def test_outside_photo_food_and_bad_index(client, auth_headers, db_factory, caplog):
    ids = photos(client, auth_headers)
    class Outside(MockAIClient):
        def analyze(self, *args, **kwargs):
            result = super().analyze(*args, **kwargs)
            result.candidates[0].image_index = 99
            result.candidates[-1].is_in_image = False
            return result
    app.dependency_overrides[get_ai_client] = lambda: Outside()
    body = analyze(client, auth_headers, ids)
    assert body['candidates'][0]['image_index'] == 0
    assert [c['food_index'] for c in body['candidates'][:3]] == [0, 0, 0]
    assert body['candidates'][0]['meal_image_id'] == ids[0]
    assert body['candidates'][-1]['meal_image_id'] is None
    assert body['candidates'][-1]['bbox'] is None
    assert 'image_index 범위 보정' in caplog.text
    with db_factory() as db:
        assert db.get(FoodCandidate, body['candidates'][-1]['food_candidate_id']).meal_image_id is None


@pytest.mark.parametrize('count', [1, 2, 3])
def test_food_cap_scales_by_image_count(client, auth_headers, count):
    ids = photos(client, auth_headers, count)
    class Many(MockAIClient):
        def analyze(self, *args, **kwargs):
            result = super().analyze(*args, **kwargs)
            result.candidates = [AICandidate(food_index=i, food_name=f'음식{i}', confidence=.9) for i in range(20)]
            return result
    app.dependency_overrides[get_ai_client] = lambda: Many()
    assert len(analyze(client, auth_headers, ids)['candidates']) == 5 * count


def test_save_order_bbox_rules_and_list(client, auth_headers, db_factory):
    ids = photos(client, auth_headers)
    draft = analyze(client, auth_headers, ids)
    rice = draft['candidates'][-1]
    ordered = list(reversed(ids))
    items = [
        {**MEAL_PAYLOAD['items'][1], 'food_candidate_id': rice['food_candidate_id'], 'bbox': BOX_A},
        {**MEAL_PAYLOAD['items'][0], 'bbox': BOX_B},
        {**MEAL_PAYLOAD['items'][0], 'food_name': '명시', 'bbox_image_id': ids[0]},
        {**MEAL_PAYLOAD['items'][0], 'food_name': '명시 NULL', 'bbox_image_id': None},
    ]
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ordered, 'items': items})
    assert [(i['meal_image_id'], i['position']) for i in body['images']] == [(ordered[0],0), (ordered[1],1)]
    detail = client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers).json()
    assert detail['images'] == body['images']
    assert detail['image_url'] == body['images'][0]['image_url']
    assert [i['bbox_image_id'] for i in detail['items']] == [ids[1], None, ids[0], None]
    with db_factory() as db:
        assert db.get(MealRecord, body['meal_id']).meal_image_id == ordered[0]
        assert db.scalar(select(func.count()).select_from(MealRecordImage)) == 2
    entry = client.get('/v1/meals?date=2026-06-27', headers=auth_headers).json()['meals'][0]
    assert entry['image_count'] == 2
    assert entry['image_url'] == detail['image_url']


@pytest.mark.parametrize('count', [0, 1, 2])
def test_bbox_default_without_candidate(client, auth_headers, count):
    ids = photos(client, auth_headers, count)
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids})
    detail = client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers).json()
    assert [i['bbox_image_id'] for i in detail['items']] == [ids[0] if count == 1 else None] * 2


@pytest.mark.parametrize('single', [False, True])
def test_skip_cannot_have_photos(client, auth_headers, single):
    ids = photos(client, auth_headers)
    payload = {**MEAL_PAYLOAD, 'is_skipped': True, 'items': []}
    payload.update({'meal_image_id': ids[0]} if single else {'meal_image_ids': ids})
    assert client.post('/v1/meals', headers=auth_headers, json=payload).status_code == 400


def test_bbox_explicit_outside_photo_is_rejected_on_create_and_patch(client, auth_headers):
    ids = photos(client, auth_headers, 3)
    items = [{**MEAL_PAYLOAD['items'][0], 'bbox_image_id': ids[2]}]
    assert client.post('/v1/meals', headers=auth_headers, json={**MEAL_PAYLOAD, 'meal_image_ids': ids[:2], 'items': items}).status_code == 400
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids[:2]})
    assert client.patch(f"/v1/meals/{body['meal_id']}", headers=auth_headers, json={'items': items}).status_code == 400
    assert client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers).json()['total_calories'] == 524


def test_patch_preserves_bbox_image_and_candidate_takes_priority(client, auth_headers):
    ids = photos(client, auth_headers)
    items = [{**MEAL_PAYLOAD['items'][0], 'bbox': BOX_A, 'bbox_image_id': ids[1]}]
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids, 'items': items})
    path = f"/v1/meals/{body['meal_id']}"
    omitted = MEAL_PAYLOAD['items'][0]
    for _ in range(2):
        res = client.patch(path, headers=auth_headers, json={'items': [omitted], 'meal_image_ids': list(reversed(ids))})
        assert res.status_code == 200, res.text
        assert res.json()['items'][0]['bbox'] == BOX_A
        assert res.json()['items'][0]['bbox_image_id'] == ids[1]
        assert [i['meal_image_id'] for i in res.json()['images']] == ids
    candidate = analyze(client, auth_headers, ids)['candidates'][0]
    res = client.patch(path, headers=auth_headers, json={'items': [{**omitted, 'food_candidate_id': candidate['food_candidate_id']}]})
    assert res.json()['items'][0]['bbox_image_id'] == ids[0]
    res = client.patch(path, headers=auth_headers, json={'items': [{**omitted, 'bbox': None, 'bbox_image_id': None}]})
    assert res.json()['items'][0]['bbox'] is None
    assert res.json()['items'][0]['bbox_image_id'] is None


def test_retention_excludes_expired_image_without_renumbering(client, auth_headers, db_factory):
    ids = photos(client, auth_headers)
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids})
    with db_factory() as db:
        db.get(MealImage, ids[0]).uploaded_at = retention_cutoff_utc() - timedelta(days=1)
        db.commit()
    detail = client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers).json()
    assert detail['image_url'] is None
    assert [(i['meal_image_id'], i['position']) for i in detail['images']] == [(ids[1],1)]
    entry = client.get('/v1/meals?date=2026-06-27', headers=auth_headers).json()['meals'][0]
    assert entry['image_count'] == 1
    assert entry['image_url'] is None


def test_photo_deletion_keeps_call_snapshot(client, auth_headers, db_factory):
    ids = photos(client, auth_headers)
    draft = analyze(client, auth_headers, ids)
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids,
        'items': [{**MEAL_PAYLOAD['items'][0], 'bbox': BOX_A, 'bbox_image_id': ids[1]}]})
    with db_factory() as db:
        db.delete(db.get(MealImage, ids[1]))
        db.commit()
    with db_factory() as db:
        assert db.get(AiCallLog, draft['ai_call_log_id']).input_image_count == 2
        rows = db.scalars(select(AiCallLogImage).order_by(AiCallLogImage.position)).all()
        assert [r.meal_image_id for r in rows] == [ids[0],None]
        assert db.scalar(select(MealItem)).bbox_image_id is None
        assert db.scalar(select(func.count()).select_from(MealRecordImage)) == 1


def test_single_aliases_return_identical_json(client, auth_headers, db_factory, monkeypatch):
    ids = photos(client, auth_headers, 1)
    import app.services.analyze as service
    monkeypatch.setattr(service.time, 'perf_counter', lambda: 1)
    legacy = client.post('/v1/meals/analyze', headers=auth_headers, json={'meal_image_id': ids[0]})
    # 같은 DB 시작 상태로 되돌려 자동 발급 ID 차이까지 제거한다.
    with db_factory() as db:
        for row in db.scalars(select(FoodCandidate)).all():
            db.delete(row)
        for row in db.scalars(select(AiCallLog)).all():
            db.delete(row)
        db.commit()
    modern = client.post('/v1/meals/analyze', headers=auth_headers, json={'meal_image_ids': ids})
    assert legacy.content == modern.content


def test_three_images_pay_one_photo_meal_reward(client, auth_headers, db_factory):
    from app.models import RewardLedger
    from app.services import game_catalog
    ids = photos(client, auth_headers, 3)
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids})
    with db_factory() as db:
        rewards = db.scalars(select(RewardLedger).where(RewardLedger.reason == 'meal')).all()
        assert len(rewards) == 1
        assert rewards[0].idempotency_key == f"meal:{body['meal_id']}"
        assert rewards[0].xp_delta == game_catalog.XP_PER_PHOTO_MEAL


def test_reward_failure_recreates_all_images_atomically(client, auth_headers, db_factory, monkeypatch):
    def fail(*args):
        raise RuntimeError('보상 실패')
    monkeypatch.setattr('app.services.meals.apply_meal_rewards', fail)
    ids = photos(client, auth_headers)
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids})
    assert body['rewards'] is None
    assert [i['meal_image_id'] for i in body['images']] == ids
    with db_factory() as db:
        assert db.scalar(select(func.count(MealRecord.id))) == 1
        assert db.scalar(select(func.count()).select_from(MealRecordImage)) == 2


def test_unowned_candidate_cannot_supply_bbox_image(client, auth_headers):
    ids = photos(client, auth_headers)
    other = login(client, 'other-bbox')
    other_headers = {'Authorization': f"Bearer {other['access_token']}"}
    foreign = analyze(client, other_headers, photos(client, other_headers, 1))['candidates'][0]
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids,
        'items': [{**MEAL_PAYLOAD['items'][0], 'food_candidate_id': foreign['food_candidate_id']}]})
    detail = client.get(f"/v1/meals/{body['meal_id']}", headers=auth_headers).json()
    assert detail['items'][0]['bbox_image_id'] is None


def test_single_photo_both_aliases_match_and_create_link(client, auth_headers, db_factory):
    ids = photos(client, auth_headers, 1)
    res = client.post('/v1/meals/analyze', headers=auth_headers, json={'meal_image_id': ids[0], 'meal_image_ids': ids})
    assert res.status_code == 200
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_id': ids[0], 'meal_image_ids': ids})
    with db_factory() as db:
        assert db.get(MealRecordImage, (body['meal_id'], ids[0])).position == 0


def test_inferred_candidate_photo_follows_rule_one_even_outside_record(client, auth_headers):
    # 계약은 명시한 bbox_image_id 에만 기록 사진 집합 검증을 요구한다.
    ids = photos(client, auth_headers, 3)
    candidate = analyze(client, auth_headers, ids[2:])['candidates'][0]
    item = {**MEAL_PAYLOAD['items'][0], 'food_candidate_id': candidate['food_candidate_id'], 'bbox': BOX_A}
    body = create_meal(client, auth_headers, {**MEAL_PAYLOAD, 'meal_image_ids': ids[:2], 'items': [item]})
    path = f"/v1/meals/{body['meal_id']}"
    assert client.get(path, headers=auth_headers).json()['items'][0]['bbox_image_id'] == ids[2]
    res = client.patch(path, headers=auth_headers, json={'items': [MEAL_PAYLOAD['items'][0]]})
    assert res.status_code == 200, res.text
    assert res.json()['items'][0]['bbox_image_id'] == ids[2]
