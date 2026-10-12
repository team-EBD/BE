"""다중 사진 AI 클라이언트 직렬화와 구 응답 호환."""
import pytest

from app.ai_client.base import AICandidate, parse_analyze
from app.ai_client.mock import MockAIClient
from app.ai_client.real import RealAIClient
from app.services.analyze import _grouped_candidates


@pytest.mark.parametrize('count', [1, 2, 3])
def test_real_sends_ordered_images_and_limit(monkeypatch, count):
    client = RealAIClient('http://ai', 1)
    calls = []
    def post(path, payload):
        calls.append((path, payload))
        return MockAIClient().analyze('http://old').model_dump(), None, 1
    monkeypatch.setattr(client, '_post', post)
    urls = [f'http://img/{i}' for i in range(count)]
    result = client.analyze('http://ignored', image_urls=urls, max_foods=count * 5)
    assert result.status == 'success'
    assert calls == [('/internal/analyze', {'image_url': urls[0], 'image_urls': urls, 'max_foods': count * 5})]


def test_parser_reads_image_fields_and_legacy_defaults():
    raw = MockAIClient().analyze('http://img').model_dump()
    raw['candidates'][0].pop('image_index')
    assert parse_analyze(raw).candidates[0].image_index == 0
    raw['candidates'][0]['image_index'] = 2
    raw['candidates'][0]['is_in_image'] = False
    parsed = parse_analyze(raw)
    assert parsed.candidates[0].image_index == 2
    assert parsed.candidates[0].is_in_image is False
    failure = MockAIClient(fail_image_index=1).analyze('http://img')
    assert parse_analyze(failure.model_dump()).failed_image_index == 1


def test_mock_assigns_rice_to_second_photo_only_for_multi_image():
    single = MockAIClient().analyze('http://img')
    multi = MockAIClient().analyze('http://img', image_urls=['http://img', 'http://img2'])
    assert [c.image_index for c in single.candidates] == [0,0,0,0]
    assert [c.image_index for c in multi.candidates] == [0,0,0,1]


def test_group_limit_accepts_fifteen_foods_and_keeps_prediction_cap():
    raw = [AICandidate(food_index=i, food_name=f'음식{i}', confidence=.9) for i in range(16) for _ in range(5)]
    assert len(_grouped_candidates(raw, 4, max_foods=15)) == 60
    assert len(_grouped_candidates(raw, 3)) == 15
