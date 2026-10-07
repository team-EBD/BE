"""스크립트 모듈이 컨테이너(/app/scripts) 레이아웃에서도 import 되는지.

운영 컨테이너에서 `python -m scripts.import_mfds_material` 이 import 단계에서 IndexError 로 죽었다(2026-10-07):
저장소 밖 작업공간 `ref/source/` 경로를 `Path(__file__).parents[3]` 로 모듈 로드 시점에 계산했는데
/app/scripts/… 는 위로 세 단계가 없다.
"""
from pathlib import Path

from scripts.import_public_nutrition import workspace_ref


def test_workspace_ref_local_layout_points_outside_repo():
    here = Path("/Users/me/Documents/Software maestro/EBD/BE/scripts/import_public_nutrition.py")
    assert workspace_ref("mfds_processed.jsonl", here=here) == Path(
        "/Users/me/Documents/Software maestro/ref/source/mfds_processed.jsonl"
    )


def test_workspace_ref_container_layout_does_not_raise():
    here = Path("/app/scripts/import_public_nutrition.py")
    path = workspace_ref("macro_ratios_computed.json", here=here)
    assert path == Path("/ref/source/macro_ratios_computed.json")
    assert not path.exists()  # 호출부(MacroEstimator.from_artifact)가 FileNotFoundError 로 None 처리


def test_script_modules_import_without_workspace():
    # import_mfds_material → import_mfds_api → import_public_nutrition 체인이 모듈 로드만으로 실패하지 않는다
    import scripts.import_mfds_api  # noqa: F401
    import scripts.import_mfds_material  # noqa: F401
