"""D02 — 목록 97,794건에서 층화 표본 5,000건 선정.

전량(약 19.6만 요청, 54시간)은 공공 서버 부담이 커서 표본만 받는다.
층은 분야(8) × 등록연도(13) = 최대 104셀. 셀별 균등 배분을 목표로 하되
작은 셀은 보유량만큼만 뽑고, 남는 몫은 큰 셀에 비례 재배분한다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

def _find_ml_root(_start):
    """`ml/` 를 위로 거슬러 찾는다. 파일이 몇 단계 아래로 옮겨져도 동작한다."""
    _p = _os.path.abspath(_start)
    while True:
        _p = _os.path.dirname(_p)
        if (_os.path.isdir(_os.path.join(_p, "pipelines"))
                and _os.path.isdir(_os.path.join(_p, "data"))):
            return _p
        if _p == _os.path.dirname(_p):
            raise RuntimeError("ml root not found from %s" % _start)


_ML = _find_ml_root(__file__)
for _d in ("pipelines", "evaluation", "experiments"):
    _base = _os.path.join(_ML, _d)
    if not _os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in _os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in _sys.path:
            _sys.path.insert(0, _dp)
# -------------------------------------------------------------------------

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROC, norm_category, pblanc_id, read_list, save_report

OUT = os.path.join(PROC, "list_sample.parquet")


def allocate(sizes, total):
    """셀별 균등 배분 후, 부족분을 여유 있는 셀에 비례 재배분."""
    cells = list(sizes.index)
    quota = pd.Series(0, index=cells, dtype=int)
    remaining = total
    active = set(cells)
    while remaining > 0 and active:
        share = max(remaining // len(active), 1)
        moved = 0
        for c in list(active):
            room = sizes[c] - quota[c]
            if room <= 0:
                active.discard(c)
                continue
            take = min(share, room, remaining - moved)
            if take <= 0:
                continue
            quota[c] += take
            moved += take
            if quota[c] >= sizes[c]:
                active.discard(c)
        if moved == 0:
            break
        remaining -= moved
    return quota


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-year", type=int, default=2019,
                    help="이 연도 미만은 제외. 실측상 2018년 이전 공고는 첨부파일이 없다")
    args = ap.parse_args()

    df = read_list()
    df["announcement_id"] = df["상세URL"].map(pblanc_id)
    df["category"] = df["분야"].map(norm_category)
    df["registered_date"] = pd.to_datetime(df["등록일자"], errors="coerce")
    df["year"] = df["registered_date"].dt.year

    before = len(df)
    df = df.dropna(subset=["announcement_id", "category", "year"]).copy()
    df = df.drop_duplicates(subset="announcement_id", keep="first")
    df["year"] = df["year"].astype(int)
    n_before_year = len(df)
    df = df[df["year"] >= args.min_year].copy()   # 2018년 이전은 첨부 0건(실측)
    df["cell"] = df["category"].astype(str) + "|" + df["year"].astype(str)

    sizes = df.groupby("cell").size().sort_values(ascending=False)
    quota = allocate(sizes, min(args.n, len(df)))

    rng = np.random.default_rng(args.seed)
    picks = []
    for cell, k in quota.items():
        if k <= 0:
            continue
        sub = df[df["cell"] == cell]
        idx = rng.choice(sub.index.values, size=int(k), replace=False)
        picks.append(sub.loc[idx])
    s = pd.concat(picks).sort_values(["year", "category"]).reset_index(drop=True)

    keep = ["announcement_id", "사업명", "분야", "category", "소관기관", "수행기관",
            "등록일자", "registered_date", "year", "신청시작일자", "신청종료일자",
            "상세URL", "cell"]
    s = s[[c for c in keep if c in s.columns]]
    s.to_parquet(OUT, index=False)

    save_report("d02_sample.json", {
        "source_rows": before,
        "min_year": args.min_year,
        "rows_before_year_filter": n_before_year,
        "eligible_rows": len(df),
        "year_filter_reason": "연도별 5건씩 탐침 결과 2013~2018 첨부 0/5, 2019~2025 5/5",
        "sampled": len(s),
        "seed": args.seed,
        "cells_total": int(sizes.size),
        "cells_used": int((quota > 0).sum()),
        "by_category": s["category"].value_counts().to_dict(),
        "by_year": s["year"].value_counts().sort_index().to_dict(),
        "smallest_cells": sizes.tail(5).to_dict(),
        "note_2025": "2025년은 3/27까지의 부분 연도 — 층 크기가 작아 배분량도 작다",
        "output": OUT,
    })
    print(f"표본 {len(s):,}건 → {OUT}")
    print("\n분야별:"); print(s["category"].value_counts().to_string())
    print("\n연도별:"); print(s["year"].value_counts().sort_index().to_string())


if __name__ == "__main__":
    main()
