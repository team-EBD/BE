"""테스트 기기(로봇) 표시 기록 — 분석 지표에서 실제 사용자와 구분하기 위한 것.

구글 플레이는 새 빌드를 Firebase Test Lab 기기에서 로봇으로 자동 점검하고, 그 로봇이 구글 로그인까지 해서
계정이 생긴다(2026-09-21 기준 86개 계정 중 39개 추정). 그 기기는 안드로이드 시스템 설정
`firebase.test.lab = "true"` 를 갖고 있어, 앱이 이를 읽어 로그인·가입 요청의 `is_test_device` 로 보낸다.

보안 경계가 아니다: 앱이 보내는 값이라 위조할 수 있지만, 효과는 '분석에서 빠진다' 뿐이다.
권한·과금 등 다른 판단에 이 값을 쓰지 않는다.
"""
from __future__ import annotations

from app.models import User


def record_test_device(user: User, reported: bool | None) -> None:
    """앱이 보고한 값을 users.is_test_device 에 반영한다 (커밋은 호출자가 한다).

    - None(구버전 앱 — 보고 없음): 기존 값을 건드리지 않는다.
    - True: 한 번 찍히면 유지한다. 같은 계정이 나중에 일반 기기에서 로그인해도 되돌리지 않는다
      (로봇 계정이 '깨끗한' 계정으로 바뀌는 것보다, 테스트에 쓰인 계정이 계속 제외되는 편이 안전하다).
    - False: 아직 True 가 아닐 때만 기록한다.
    """
    if reported is None or user.is_test_device is True:
        return
    user.is_test_device = reported
