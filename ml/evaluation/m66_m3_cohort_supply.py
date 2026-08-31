r"""M66 — 얇은 비교군 표본을 먼저 채우고 현행 모델 3 을 재평가.

M64 는 얇은 비교군 문제의 해법이 모델이 아니라 **표본 확대**이고, 그 표본의
절반은 새 수집이 아니라 **이미 가진 문서의 추출 개선**이라고 끝났다. 여기서
그 추출을 하고, 같은 구조로 다시 잰다.

**구조는 한 줄도 건드리지 않는다.** 거리 기반 이례성 점수 · 비교군 사다리
(성격x방식) · mean 대표벡터 · standard scaling · Euclidean · `MIN_COHORT=20`
전부 M44 Freeze 그대로다. 바뀌는 것은 입력 데이터 하나뿐이다.

    1 얇은 비교군 보강    F06 --supply 가 이미 가진 문서에서 세 축을 더 뽑는다
    2 결측 축소           지원단위 · 지원비율
    3 현행 모델 3 재평가   아래 다섯 지표만 본다

평가 기준 (지시서가 정한 다섯. ROC 는 이번에 보지 않는다)

    resampling Spearman             m3_lab.resample_stability
    Top-K overlap                   m3_lab.resample_stability
    synthetic perturbation 방향 일관성  m3_lab.synthetic_stress
    attribution 안정성                m3_lab.attribution_stability
    fallback 비율                     m3_lab.cohort_profile

세 갈래로 나눠 잰다. pool 이 2,451 -> 2,626 으로 늘기 때문이다. 크기가 다른 두
집합의 지표를 그냥 나란히 놓으면 '데이터가 좋아졌다'와 '대상이 달라졌다'가
섞인다 (M64 와 같은 설계).

    A v2 pool (2,451)         현행
    B 교집합 pool (v2∩v3)      같은 행에서 입력 품질만 바꾼 순수 대조
    C v3 pool (2,626)         보강 후 실제 운영 상태

**돌리지 않는 것** — 지시서가 반복하지 말라고 지정했거나 후속으로 미룬 것들.
    multi-prototype (M59 reject) · shrinkage (M50 reject) ·
    robust/quantile scaling (M61 reject) · cohort 구조 변경 (M58 reject) ·
    Mahalanobis 유사 거리 (M61·M64 가 후속 후보로만 남김)
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

import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warnings

warnings.filterwarnings("ignore")
import common as C
import m3_lab as L
import f06_design_features as F6
from m13_m4_anomaly import MIN_AXES, prepare

V2, V3 = F6.OUT_V2, F6.OUT_V3
THIN_NEAR = (10, 20)      # '문턱 근처 얇은 칸' — MIN_COHORT 바로 아래 구간
AUDIT = os.path.join(C.REPORTS, "m66_supply_audit.csv")


# ------------------------------------------------------------------ 1 공급
def cohort_counts(pool, cols=("support_type", "support_method")):
    return L.level_keys(pool, list(cols)).value_counts()


def supply_table(p2, p3):
    """문턱 근처 얇은 칸이 실제로 얼마나 찼는가. 칸 목록은 **v2 기준으로 고정**
    한다 — 보강 후에 다시 고르면 '늘어난 칸만 골랐다'가 된다."""
    k2, k3 = cohort_counts(p2), cohort_counts(p3)
    lo, hi = THIN_NEAR
    rows = []
    for key, n in k2[(k2 >= lo) & (k2 < hi)].sort_values(ascending=False).items():
        after = int(k3.get(key, 0))
        rows.append({"cohort": str(key), "v2": int(n), "v3": after,
                     "delta": after - int(n), "reached_20": after >= L.MIN_COHORT})
    return rows, k2, k3


def missingness(pool):
    out = {}
    for c in ("support_unit", "support_ratio", "log_per_recipient",
              "log_support_count", "project_duration", "agency_type"):
        out[c] = round(float(pool[c].isna().mean()) * 100, 1)
    return out


# ------------------------------------------------------------------ 2 평가
def evaluate(train, tag, t0):
    """지시서의 다섯 지표. 하나도 빼지 않고, 요구하지 않은 것은 넣지 않는다."""
    res = L.score_pool(train, train)
    prof = L.cohort_profile(res)
    stab = L.resample_stability(train)
    syn = L.synthetic_stress(train)
    attr = L.attribution_stability(train)
    vol = L.rank_volatility(train)
    f = stab["frac_0.8"]
    print("   [%s] %ds  fallback %d (%.2f%%)  얇은비교군 %d  "
          "Spearman(0.8) %.3f  Top30 %.3f  방향일관성 %.2f  attribution %.3f"
          % (tag, time.time() - t0, prof["n_global_fallback"],
             100 * prof["n_global_fallback"] / len(train), prof["n_thin"],
             f["spearman_mean"], f["top30_mean"], syn["min_positive_rate"],
             attr["top1_axis_agreement_mean"]))
    return {"n": len(train), "cohort_profile": prof, "resample": stab,
            "synthetic": syn, "attribution": attr, "rank_volatility": vol,
            "fallback_rate": round(prof["n_global_fallback"] / len(train), 4)}


def verdict(base, var):
    """m3_lab.verdict 의 문턱을 그대로 쓰되 **지시서가 지정한 다섯 지표에만**
    적용한다. 문턱은 결과를 보기 전에 고정된 값이다 (m3_lab.KEEP_*)."""
    fails = []
    b, v = base["resample"]["frac_0.8"], var["resample"]["frac_0.8"]
    if v["spearman_mean"] < b["spearman_mean"] - 0.01:
        fails.append("재표집 순위상관 악화 (%.3f -> %.3f)"
                     % (b["spearman_mean"], v["spearman_mean"]))
    if v["top30_mean"] < b["top30_mean"] - 0.05:
        fails.append("재표집 Top30 악화 (%.3f -> %.3f)"
                     % (b["top30_mean"], v["top30_mean"]))
    if (var["synthetic"]["min_positive_rate"]
            < min(1.0, base["synthetic"]["min_positive_rate"]) - 0.02):
        fails.append("synthetic 방향 일관성 악화")
    if (var["attribution"]["top1_axis_agreement_mean"]
            < base["attribution"]["top1_axis_agreement_mean"] - 0.05):
        fails.append("attribution 흔들림 증가 (%.3f -> %.3f)"
                     % (base["attribution"]["top1_axis_agreement_mean"],
                        var["attribution"]["top1_axis_agreement_mean"]))
    if var["fallback_rate"] > base["fallback_rate"] + 0.02:
        fails.append("전역 fallback 비율 비정상 증가 (%.3f -> %.3f)"
                     % (base["fallback_rate"], var["fallback_rate"]))
    return fails


# ------------------------------------------------------------------ 3 감사
def write_audit(v3, v2):
    """새로 채운 값을 근거창과 함께 남긴다. 규칙으로 채운 값은 '맞는 값'이
    아니라 '뽑힌 값'이라, 근거가 없으면 감사가 불가능하다 (M62 와 같은 규율).
    근거등급별로 나눠 뽑는다 — 등급마다 정확도가 다를 수 있고 한 덩이로 뽑으면
    그 차이가 평균에 묻힌다."""
    a, b = v2.set_index("row_id"), v3.set_index("row_id")
    rows = []
    # 바뀐 행을 값이 아니라 **근거등급 컬럼**으로 고릅니다. `amount_type` 은
    # v2 에서 결측이 아니라 `unknown` 이라 값으로 고르면 한 건도 안 걸립니다.
    for col, basis_col, tag in (
            ("support_unit", "support_unit_basis", "count_expression"),
            ("support_ratio", "support_ratio_basis", None),
            ("amount_type", "amount_type_basis", None)):
        d = b[b[basis_col].notna() & (b[basis_col] != "none")]
        if tag:
            d = d[d[basis_col] == tag]      # S1 이 채운 것만 (기존 등급 제외)
        d = d[d.index.isin(a.index)]
        for basis, g in d.groupby(d[basis_col].fillna("(없음)")):
            take = g.sample(n=min(20, len(g)), random_state=L.SEED)
            for rid, r in take.iterrows():
                rows.append({"row_id": rid, "필드": col, "새값": r[col],
                             "근거등급": basis,
                             "근거창": str(r.get("support_unit_window") or "")[:160]
                             if col == "support_unit" else "",
                             "제목": str(r["title"])[:70]})
    df = pd.DataFrame(rows)
    df.to_csv(AUDIT, index=False, encoding="utf-8-sig")
    return len(df)


# ------------------------------------------------------------------ main
def main():
    t0 = time.time()
    if not os.path.exists(V3):
        raise FileNotFoundError(
            "design_features_v3.parquet 이 없다. 먼저 만들어야 한다:\n"
            "  python scripts/f06_design_features.py --supply")

    v2raw, v3raw = pd.read_parquet(V2), pd.read_parquet(V3)
    p2, p3 = L.load_pool(V2), L.load_pool(V3)
    inter = sorted(set(p2["row_id"]) & set(p3["row_id"]))
    b2 = p2[p2["row_id"].isin(inter)].reset_index(drop=True)
    b3 = p3[p3["row_id"].isin(inter)].reset_index(drop=True)

    print("== 1 공급 보강 (F06 --supply · 이미 가진 문서만 다시 읽음)")
    print("   pool  v2 %d -> v3 %d  (신규 진입 %d · 교집합 %d)"
          % (len(p2), len(p3), len(p3) - len(p2), len(inter)))
    m2, m3 = missingness(p2), missingness(p3)
    for k in m2:
        print("   %-20s 결측 %5.1f%% -> %5.1f%%" % (k, m2[k], m3[k]))

    rows, k2, k3 = supply_table(p2, p3)
    print("\n== 2 문턱 근처 얇은 칸 (v2 에서 %d <= n < %d 인 L1 칸)" % THIN_NEAR)
    for r in rows:
        print("   %-24s %2d -> %2d  %s" % (r["cohort"], r["v2"], r["v3"],
                                           "20 도달" if r["reached_20"] else ""))
    reached = sum(r["reached_20"] for r in rows)
    print("   -> %d / %d 칸이 MIN_COHORT=20 을 넘었다" % (reached, len(rows)))

    n_audit = write_audit(v3raw, v2raw)
    print("   감사표본 %d행 -> %s" % (n_audit, os.path.relpath(AUDIT, C.ROOT)))

    print("\n== 3 현행 모델 3 재평가 (구조 불변 · 지시서 지표 5종만)")
    ev = {}
    ev["A v2 pool"] = evaluate(p2, "A v2 pool", t0)
    ev["B 교집합 v2"] = evaluate(b2, "B∩v2", t0)
    ev["B 교집합 v3"] = evaluate(b3, "B∩v3", t0)
    ev["C v3 pool"] = evaluate(p3, "C v3 pool", t0)

    print("\n== 4 판정 (문턱은 m3_lab 이 결과 보기 전에 고정한 값)")
    fails_b = verdict(ev["B 교집합 v2"], ev["B 교집합 v3"])
    fails_c = verdict(ev["A v2 pool"], ev["C v3 pool"])
    print("   B 교집합 (같은 행·입력만 다름): %s"
          % ("유지" if not fails_b else "; ".join(fails_b)))
    print("   C 운영상태 (pool 이 달라 참고): %s"
          % ("유지" if not fails_c else "; ".join(fails_c)))

    C.save_report("m66_m3_cohort_supply.json", {
        "structure": "M44 Freeze 그대로 — 거리·사다리(성격x방식)·mean·standard·"
                     "Euclidean·MIN_COHORT=20. 바뀐 것은 입력 데이터뿐",
        "datasets": {"v2": os.path.relpath(V2, C.ROOT),
                     "v3": os.path.relpath(V3, C.ROOT)},
        "pool_rows": {"v2": len(p2), "v3": len(p3), "intersection": len(inter)},
        "missingness_pct": {"v2": m2, "v3": m3},
        "thin_cohorts_near_threshold": rows,
        "reached_20": reached,
        "n_thin_cohorts_examined": len(rows),
        "sub_threshold_rows": {"v2": int(k2[k2 < L.MIN_COHORT].sum()),
                               "v3": int(k3[k3 < L.MIN_COHORT].sum())},
        "evaluation": ev,
        "verdict": {"B_intersection": fails_b or ["유지"],
                    "C_operational": fails_c or ["유지"]},
        "not_run": ["multi-prototype (M59 reject)", "shrinkage (M50 reject)",
                    "robust/quantile scaling (M61 reject)",
                    "cohort 구조 변경 (M58 reject)",
                    "Mahalanobis 유사 거리 (후속 후보)"],
        "elapsed_sec": round(time.time() - t0, 1),
    })
    print("\n[%ds] 완료" % (time.time() - t0))
    return ev


if __name__ == "__main__":
    main()
