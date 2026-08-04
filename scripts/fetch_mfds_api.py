"""공공데이터포털 식약처 영양성분 표준데이터 OpenAPI 전량 수집.

포털의 그리드 다운로드(CSV)는 **5만 건에서 잘린다**. 가공식품은 실제 59만 건이라
CSV로는 8%밖에 못 받는다. 이 스크립트는 OpenAPI 를 페이지 단위로 돌며 전량을 받아
JSONL 로 저장한다. 저장 결과는 `import_public_nutrition.py` 가 소비한다.

사용법:
    python -m scripts.fetch_mfds_api food        # 음식편
    python -m scripts.fetch_mfds_api processed   # 가공식품편
    python -m scripts.fetch_mfds_api processed --out /tmp/p.jsonl

인증키는 BE/.env 의 MFDS_API_KEY (공공데이터포털 계정 단위 — 승인받은 모든 API 공용).

설계 메모:
- **numOfRows 상한은 1000.** 5000 이상은 서버가 정상 응답을 주지 않는다(2026-08-04 확인).
- 응답을 메모리에 모으지 않고 **JSONL 로 스트리밍**한다 (가공식품 전량은 원본 약 650MB).
- **재개 가능**: 출력 파일이 있으면 이미 받은 줄 수로 시작 페이지를 계산해 이어받는다.
  중간에 끊겨도 처음부터 다시 받지 않는다.
- 실패한 페이지는 재시도(기본 3회, 지수 백오프) 후에도 실패하면 중단한다.
  일부만 저장된 상태로 적재하면 "삭제된 항목"과 구분이 안 되기 때문이다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE = "https://api.data.go.kr/openapi"

# 데이터셋 → 엔드포인트. 비밀값이 아니고 바뀔 일이 거의 없어 코드에 둔다
# (.env 에 두면 팀원마다 값이 갈리고, 어느 API 를 쓰는지 git 에 안 남는다).
DATASETS = {
    "food": f"{BASE}/tn_pubr_public_nutri_food_info_api",  # 전국통합식품영양성분정보(음식)
    "processed": f"{BASE}/tn_pubr_public_nutri_process_info_api",  # 〃 (가공식품)
}

PAGE_SIZE = 1000  # API 상한
MAX_RETRY = 3


def _service_key() -> str:
    """인증키를 **디코딩 형태로 정규화**해서 반환한다.

    포털은 Encoding/Decoding 두 형태를 주는데, 어느 쪽이 .env 에 들어올지 모른다.
    httpx 의 params= 가 인코딩을 한 번 해주므로 여기서는 반드시 디코딩 형태여야 한다
    (이미 인코딩된 키를 그대로 넘기면 %2B → %252B 로 이중 인코딩돼 인증이 깨진다).
    """
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    raw = (os.getenv("MFDS_API_KEY") or "").strip()
    if not raw:
        sys.exit("MFDS_API_KEY 가 BE/.env 에 없습니다.")
    return urllib.parse.unquote(raw) if "%" in raw else raw


def _get_page(client: httpx.Client, url: str, key: str, page: int) -> tuple[list[dict], int]:
    """한 페이지를 받아 (레코드, 전체건수) 반환. 재시도는 여기서 흡수한다."""
    last_err: Exception | None = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            res = client.get(
                url,
                params={
                    "serviceKey": key,
                    "pageNo": page,
                    "numOfRows": PAGE_SIZE,
                    "type": "json",
                },
            )
            body = res.json()["body"]
            items = (body.get("items") or {}).get("item") or []
            return items, int(body["totalCount"])
        except Exception as exc:  # noqa: BLE001 — 네트워크/JSON/스키마 무엇이든 재시도 대상
            last_err = exc
            if attempt < MAX_RETRY:
                time.sleep(2**attempt)
    raise RuntimeError(f"page {page} 실패 ({MAX_RETRY}회 재시도): {last_err}")


def fetch(dataset: str, out_path: Path) -> int:
    url = DATASETS[dataset]
    key = _service_key()

    # 재개: 이미 받은 줄 수 → 다음 페이지부터
    done = 0
    if out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            done = sum(1 for _ in f)
        if done % PAGE_SIZE:
            # 페이지 경계가 아니면 마지막 페이지가 덜 받아진 것 — 그 페이지부터 다시
            done -= done % PAGE_SIZE
            _truncate(out_path, done)
        if done:
            print(f"[fetch] 재개: 이미 {done}건 → {done // PAGE_SIZE + 1}페이지부터")

    mode = "a" if done else "w"
    page = done // PAGE_SIZE + 1
    total = -1

    with httpx.Client(timeout=120) as client, out_path.open(mode, encoding="utf-8") as out:
        while True:
            items, total = _get_page(client, url, key, page)
            if not items:
                break
            for row in items:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
            done += len(items)
            out.flush()
            if page % 25 == 0 or done >= total:
                pct = done / total * 100 if total else 0
                print(f"[fetch]   {done:>7,}/{total:,} ({pct:5.1f}%)", flush=True)
            if done >= total:
                break
            page += 1

    print(f"[fetch] 완료: {done:,}건 → {out_path}")
    return done


def _truncate(path: Path, keep_lines: int) -> None:
    """앞에서 keep_lines 줄만 남기고 자른다 (덜 받아진 마지막 페이지 제거)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with path.open(encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for i, line in enumerate(src):
            if i >= keep_lines:
                break
            dst.write(line)
    tmp.replace(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="식약처 영양성분 표준데이터 OpenAPI 전량 수집")
    ap.add_argument("dataset", choices=sorted(DATASETS), help="수집할 데이터셋")
    ap.add_argument("--out", type=Path, help="출력 JSONL 경로 (기본: ref/source/mfds_<dataset>.jsonl)")
    args = ap.parse_args()

    out = args.out or (
        Path(__file__).resolve().parents[3] / "ref" / "source" / f"mfds_{args.dataset}.jsonl"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fetch(args.dataset, out)


if __name__ == "__main__":
    main()
