"""항목 전체 교체 시 위치 스냅샷의 보존·제거 규칙."""
import pytest

from tests.test_meals import MEAL_PAYLOAD, create_meal

BOX_A = {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4}
BOX_B = {"x": 0.5, "y": 0.1, "width": 0.2, "height": 0.3}


def _create(client, headers, items=None):
    items = items or [
        {**MEAL_PAYLOAD["items"][0], "bbox": BOX_A},
        {**MEAL_PAYLOAD["items"][1], "bbox": BOX_B},
    ]
    return create_meal(client, headers, {**MEAL_PAYLOAD, "items": items})["meal_id"]


def _patch(client, headers, meal_id, items):
    res = client.patch(f"/v1/meals/{meal_id}", headers=headers, json={"items": items})
    assert res.status_code == 200, res.text
    detail = client.get(f"/v1/meals/{meal_id}", headers=headers)
    assert detail.json()["items"] == res.json()["items"]
    return res.json()["items"]


@pytest.mark.parametrize("identity", ["original", "missing", "null"])
def test_legacy_quantity_edit_and_reorder_preserve_boxes(client, auth_headers, identity):
    meal_id = _create(client, auth_headers)
    items = [{**item, "serving_amount": 0.5, "calories": 100}
             for item in reversed(MEAL_PAYLOAD["items"])]
    for item in items:
        if identity == "missing":
            item.pop("nutrition_item_id")
        elif identity == "null":
            item["nutrition_item_id"] = None
    for _ in range(2):  # 재생성된 항목을 다시 편집해도 유지
        updated = _patch(client, auth_headers, meal_id, items)
        assert [item["bbox"] for item in updated] == [BOX_B, BOX_A]


def test_replacement_deletion_and_addition_do_not_borrow_boxes(client, auth_headers):
    meal_id = _create(client, auth_headers)
    updated = _patch(client, auth_headers, meal_id, [
        {**MEAL_PAYLOAD["items"][1], "serving_amount": 0.5},
        {**MEAL_PAYLOAD["items"][0], "food_name": "새 음식"},
    ])
    assert [item["bbox"] for item in updated] == [BOX_B, None]
    updated = _patch(client, auth_headers, meal_id, [
        MEAL_PAYLOAD["items"][1], MEAL_PAYLOAD["items"][0],
    ])
    assert [item["bbox"] for item in updated] == [BOX_B, None]  # 삭제됐던 음식 재추가


@pytest.mark.parametrize("bbox", [None, BOX_B])
def test_explicit_bbox_overrides_legacy_preservation(client, auth_headers, bbox):
    meal_id = _create(client, auth_headers)
    updated = _patch(client, auth_headers, meal_id, [
        {**MEAL_PAYLOAD["items"][0], "bbox": bbox},
    ])
    assert updated[0]["bbox"] == bbox


def test_same_name_with_changed_nutrition_identity_clears_bbox(client, auth_headers):
    meal_id = _create(client, auth_headers)
    updated = _patch(client, auth_headers, meal_id, [
        {**MEAL_PAYLOAD["items"][0], "nutrition_item_id": 8},
    ])
    assert updated[0]["bbox"] is None


@pytest.mark.parametrize("old_duplicate", [False, True])
def test_ambiguous_duplicate_names_do_not_copy_bbox(client, auth_headers, old_duplicate):
    item = MEAL_PAYLOAD["items"][0]
    old_items = [{**item, "bbox": BOX_A}]
    if old_duplicate:
        old_items.append({**item, "bbox": BOX_B})
    meal_id = _create(client, auth_headers, old_items)
    new_items = [item] if old_duplicate else [item, item]
    updated = _patch(client, auth_headers, meal_id, new_items)
    assert all(row["bbox"] is None for row in updated)


def test_explicit_boxes_disambiguate_identical_names(client, auth_headers):
    item = MEAL_PAYLOAD["items"][0]
    meal_id = _create(client, auth_headers, [{**item, "bbox": BOX_A}, {**item, "bbox": BOX_B}])
    updated = _patch(client, auth_headers, meal_id, [
        {**item, "serving_amount": 0.5, "bbox": BOX_B},
        {**item, "serving_amount": 0.5, "bbox": BOX_A},
    ])
    assert [row["bbox"] for row in updated] == [BOX_B, BOX_A]
