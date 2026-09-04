"""D02B — 지원성격 부족 셀을 겨냥한 보강 표본.

왜 필요한가
    A10(지원규모 연도별 추이)에서 19개 지원성격 중 대부분이 표본 부족으로
    유의한 추세를 볼 수 없었다. 무작정 표본을 더 늘리기보다, 어떤 지원성격이
    부족한지부터 보고 그것부터 채운다.

겨냥이 가능한 이유 — 대분류가 다운로드 전에 이미 알려진 사전 신호다
    지원성격은 원문을 읽어야 나오지만, 대분류(경영/기술/수출…)는 목록 CSV에
    이미 있다. 현재 라벨(M14, 임계값 0.20)로 대분류→지원성격 구성비를 재보면
    상관이 뚜렷하다.
        금융 -> 융자 80.1%   인력 -> 고용보조 72.7%   창업 -> 사업화 75.3%
        경영 -> 설비 11.1%   인력 -> 교육훈련 11.1%
    이걸 이용해 "설비를 채우려면 경영에서, 교육훈련을 채우려면 인력에서 더
    뽑는다"는 식으로 다운로드 전에 표본을 편향시킬 수 있다.
    (이건 제목만으로 라벨을 정하는 것과 다르다 — 여기서는 어디서 더 뽑을지만
    정하고, 실제 라벨은 다운로드 후 원문 기반 모델 1로 정식 분류한다.)

목표와 현실성 검토
    유형당 7년 합계 140건(연 20건)을 이상적 목표로 두고 역산했더니, 19종 중
    사업화·고용보조·융자·판로·연구개발·컨설팅 6종은 이미 충분했고, 설비·
    교육훈련 2종은 raw 1,000~2,000건으로 도달 가능했다. 나머지(성능인증·
    해외인증·해외수주·실증·수출물류·수출통관·SW·솔루션·기술·IP평가)는
    최적 대분류로 겨냥해도 raw 수만~9만 건이 필요해 모집단(62,659건) 규모를
    넘거나 육박한다 — 이 방식으로는 못 채운다. 그래서 1단계로 설비·교육훈련
    2종만 겨냥한다.

배분 — 연도별 부족분에 비례
    설비는 경영 층에서, 교육훈련은 인력 층에서, 그 지원성격의 연도별 부족분
    (20건 목표 대비) 비율대로 raw 표본을 나눠 뽑는다. 균등 배분이 아니라
    부족한 연도에 더 많이 배분한다.

기존 표본과 안 겹치게 한다
    list_sample.parquet(D02, 5,000건)에 이미 뽑힌 건은 제외한다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m4_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
import os as _os
import sys as _sys

_ML = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments"):
    _p = _os.path.join(_ML, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# -------------------------------------------------------------------------

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROC, norm_category, pblanc_id, read_list, save_report

OUT = os.path.join(PROC, "list_sample_targeted.parquet")
EXISTING = os.path.join(PROC, "list_sample.parquet")

# 연도별 raw 배분. M15/M14(임계값 0.20) 라벨 기준 부족분을 대분류별 원문->라벨
# 수율(설비 0.0538, 교육훈련 0.0521)로 역산해 미리 계산해 둔 값이다.
# 계산 근거는 세션 기록 및 위 docstring 참고. 값이 바뀌면(재분류 등) 다시 계산해야 한다.
TARGETS = {
    ("설비", "경영"): {2019: 130, 2020: 93, 2021: 167, 2022: 186,
                      2023: 204, 2024: 149, 2025: 223},
    ("교육훈련", "인력"): {2019: 230, 2020: 269, 2021: 192, 2022: 250,
                        2023: 307, 2024: 346, 2025: 326},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-year", type=int, default=2019)
    args = ap.parse_args()

    df = read_list()
    df["announcement_id"] = df["상세URL"].map(pblanc_id)
    df["category"] = df["분야"].map(norm_category)
    df["registered_date"] = pd.to_datetime(df["등록일자"], errors="coerce")
    df["year"] = df["registered_date"].dt.year
    df = df.dropna(subset=["announcement_id", "category", "year"]).copy()
    df = df.drop_duplicates(subset="announcement_id", keep="first")
    df["year"] = df["year"].astype(int)
    df = df[df["year"] >= args.min_year].copy()

    existing_ids = set()
    if os.path.exists(EXISTING):
        prev = pd.read_parquet(EXISTING)
        existing_ids = set(prev["announcement_id"].astype(str))
    df = df[~df["announcement_id"].astype(str).isin(existing_ids)]
    print("기존 표본 %d건 제외 후 후보 %d건" % (len(existing_ids), len(df)))

    rng = np.random.default_rng(args.seed)
    picks, summary = [], {}
    for (support_type, cat), by_year in TARGETS.items():
        got = {}
        for y, k in by_year.items():
            pool = df[(df["category"] == cat) & (df["year"] == y)]
            take = min(k, len(pool))
            if take < k:
                print("[주의] %s %d년 후보 부족: 요청 %d / 가용 %d" % (cat, y, k, len(pool)))
            if take <= 0:
                continue
            idx = rng.choice(pool.index.values, size=take, replace=False)
            sel = df.loc[idx].copy()
            sel["target_support_type"] = support_type
            sel["target_category"] = cat
            picks.append(sel)
            df = df.drop(idx)   # 같은 후보가 두 유형에 중복 배정되지 않게 즉시 제거
            got[y] = take
        summary[support_type] = got
        print("%-8s(대분류=%s) 배분 완료: 합계 %d건" % (support_type, cat, sum(got.values())))

    if not picks:
        print("배분된 표본이 없다")
        return
    s = pd.concat(picks).sort_values(["target_support_type", "year"]).reset_index(drop=True)

    keep = ["announcement_id", "사업명", "분야", "category", "소관기관", "수행기관",
            "등록일자", "registered_date", "year", "신청시작일자", "신청종료일자",
            "상세URL", "target_support_type", "target_category"]
    s = s[[c for c in keep if c in s.columns]]
    s.to_parquet(OUT, index=False)

    print()
    print("겨냥 표본 %d건 -> %s" % (len(s), OUT))
    print(s["target_support_type"].value_counts().to_string())

    save_report("d02b_targeted_sample.json", {
        "purpose": "지원성격 19종 중 표본 부족 유형(설비·교육훈련)을 대분류 사전신호로 겨냥 보강",
        "excluded_existing": len(existing_ids),
        "targets": {"%s|%s" % (t, c): by_year for (t, c), by_year in TARGETS.items()},
        "sampled": len(s),
        "by_type": s["target_support_type"].value_counts().to_dict(),
        "note": ("여기서 target_support_type 은 다운로드 우선순위를 정하려는 겨냥일 뿐 "
                 "확정 라벨이 아니다. 실제 라벨은 다운로드 후 M14 로 원문 기반 재분류한다. "
                 "제외된 유형(성능인증 등 7종)은 최적 대분류로 겨냥해도 raw 수만 건이 "
                 "필요해(모집단 62,659건 규모를 넘거나 육박) 이 방식으로 못 채운다."),
        "output": OUT,
    })


if __name__ == "__main__":
    main()
