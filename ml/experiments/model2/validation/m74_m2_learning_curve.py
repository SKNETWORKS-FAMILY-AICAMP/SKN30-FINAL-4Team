r"""M74 — 관측 수를 늘리면 모델 2 가 실제로 좋아지는가 (수집 없이 재는 법).

지시서(사용자, `model2_new_bizinfo_observation_expansion_plan.md`)는 신규
Bizinfo 를 +300/+600/+1,000 모아 learning curve 를 그리라고 했다. 그런데
실측 수율이 **공고 1건당 학습행 0.054** 라, +300 을 얻으려면 약 5,500건을
받아야 한다(요청 11,000회, 약 5시간). 그 비용을 치르기 전에 답해야 할 질문이
하나 있다.

    지금 곡선의 기울기가, 관측을 더 모을 값어치가 있을 만큼 가파른가?

이 스크립트는 **이미 가진 1,877행을 잘라서** 그 기울기를 잰다. 그룹 단위로
40/55/70/85/100% 를 뽑아 M69 의 G 단계를 그대로 돌리고, 나온 점들에
MAE = c * n^(-a) 를 맞춘다. a 를 알면 '+N행이면 MAE 가 얼마나 내려가는가'를
외삽할 수 있고, 그 값을 M73 의 paired CI 폭(±0.007)과 비교하면 **몇 행부터
검출 가능한 개선인지** 나온다.

바꾸지 않는 것 — M69 와 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem
    feature    M69 의 G 단계 (구조화 + 제목 SVD64 + 원천 feature + 본문 SVD64)
    회귀모델   m2_features.XGB_POINT 그대로 (새 튜닝 없음)

바뀌는 것은 **학습에 쓰는 행 수** 하나다.

주의 — 그룹 단위로 자른다. 행 단위로 자르면 같은 program_stem 이 쪼개져
GroupKFold 의 전제가 깨지고, 남은 행이 서로의 답을 알려주게 된다.

비율마다 seed 를 여러 개 두는 이유: 어느 그룹이 빠지느냐에 따라 MAE 가
출렁인다. 그 출렁임을 평균으로 눌러야 기울기가 보인다.

산출
    ml/reports/m74_m2_learning_curve.{json,md}
"""
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

import os
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
import common as C
import f06_design_features as F6
import m2_features as F
import m2_source_features as SF
import m45_m2_amount as M45
import m69_m2_source_features as M69

STEP = "G"
FRACTIONS = (0.40, 0.55, 0.70, 0.85, 1.00)
SEEDS = (0, 1)
CI_HALFWIDTH = 0.007          # M73 실측 paired 95% CI 반폭
M73_MAE = 0.3563              # 현재 최고 (soft/ordinal_xgb)
M69_MAE = 0.3719              # global baseline
YIELD = 0.0544                # 실측: 공고 1건당 학습행


def subsample(groups, frac, seed):
    """그룹 단위로 frac 만큼 남긴 행 인덱스."""
    if frac >= 1.0:
        return np.arange(len(groups))
    uniq = pd.unique(groups)
    rng = np.random.RandomState(seed)
    keep = set(rng.choice(uniq, size=int(round(len(uniq) * frac)), replace=False))
    return np.where(pd.Series(groups).isin(keep).to_numpy())[0]


def run_one(Xs, y, groups, titles, body, NB, cats, idx):
    """부분집합으로 M69 G 단계 5-fold OOF 를 돌리고 MAE·Stage1 을 낸다."""
    sXs = Xs.iloc[idx].reset_index(drop=True)
    sNB = NB.iloc[idx].reset_index(drop=True)
    sy, sg, st, sb = y[idx], groups[idx], titles[idx], body[idx]
    pred, z_hat, _, z_true, base, _fid, per_fold = M69.run_ablation(
        sXs, sy, sg, st, sb, sNB, cats, steps=[STEP], verbose=False)
    p = pred[STEP]
    mae = float(np.abs(p - sy).mean())
    ratio = 10.0 ** (p - sy)
    return {
        "n": int(len(idx)),
        "n_groups": int(len(pd.unique(sg))),
        "MAE_log10": round(mae, 4),
        "within_2x": round(float(((ratio >= 0.5) & (ratio <= 2)).mean()), 4),
        "within_3x": round(float(((ratio >= 1 / 3) & (ratio <= 3)).mean()), 4),
        "baseline_MAE": round(float(np.abs(base - sy).mean()), 4),
        "stage1_acc": round(float((z_hat[STEP] == z_true).mean()), 4),
        "fold_MAE": [r["MAE"][STEP] for r in per_fold],
    }


def fit_power(ns, maes):
    """log MAE = log c - a * log n 를 최소제곱으로 맞춘다."""
    x = np.log(np.asarray(ns, dtype=float))
    yv = np.log(np.asarray(maes, dtype=float))
    a_neg, logc = np.polyfit(x, yv, 1)
    a = -float(a_neg)
    predv = logc + a_neg * x
    ss_res = float(((yv - predv) ** 2).sum())
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return a, float(np.exp(logc)), float(r2)


def main():
    t0 = time.time()
    print("== 데이터 — M69 와 같은 입력")
    raw = pd.read_parquet(F6.OUT_V2)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = F.group_key(d, "program_stem")
    print("   %d행 / 그룹 %d" % (len(y), len(pd.unique(groups))))

    print("== 원천 feature 층 (M69 G 단계)")
    NB, body, _src = SF.build(d)

    rows = []
    for frac in FRACTIONS:
        seeds = (0,) if frac >= 1.0 else SEEDS
        for sd in seeds:
            idx = subsample(groups, frac, sd)
            ts = time.time()
            r = run_one(Xs, y, groups, titles, body, NB, cats, idx)
            r.update(frac=frac, seed=sd, seconds=round(time.time() - ts, 1))
            rows.append(r)
            print("   frac %.2f seed %d  n=%4d  MAE %.4f  Stage1 %.3f  (%.0fs)"
                  % (frac, sd, r["n"], r["MAE_log10"], r["stage1_acc"],
                     r["seconds"]), flush=True)

    df = pd.DataFrame(rows)
    agg = (df.groupby("frac")
             .agg(n=("n", "mean"), MAE=("MAE_log10", "mean"),
                  MAE_sd=("MAE_log10", "std"), acc=("stage1_acc", "mean"))
             .reset_index())
    agg["n"] = agg["n"].round(0).astype(int)

    a, c, r2 = fit_power(agg["n"].tolist(), agg["MAE"].tolist())
    n0 = int(agg["n"].iloc[-1])
    mae0 = float(agg["MAE"].iloc[-1])

    proj = []
    for add in (27, 100, 200, 300, 600, 1000, 2000):
        d_mae = mae0 * (1 - ((n0 + add) / n0) ** (-a))
        docs = int(add / YIELD)
        proj.append({"add_rows": add, "total_n": n0 + add,
                     "expected_delta_MAE": round(float(d_mae), 5),
                     "detectable": bool(d_mae >= CI_HALFWIDTH),
                     "collect_docs": docs, "requests": docs * 2})
    need = next((p for p in proj if p["detectable"]), None)

    payload = {
        "purpose": "관측 수 확대의 기대 효과를 수집 없이 추정한다 "
                   "(지시서 12장 learning curve 의 대체 측정)",
        "unchanged": {"dataset": os.path.basename(F6.OUT_V2), "rows": int(len(y)),
                      "split": "GroupKFold(5), group=program_stem",
                      "feature_step": STEP, "model": "m2_features.XGB_POINT"},
        "changed": "학습에 쓰는 행 수만 (그룹 단위 subsample)",
        "runs": rows,
        "curve": agg.to_dict("records"),
        "power_fit": {"form": "MAE = c * n^(-a)", "a": round(a, 4),
                      "c": round(c, 4), "r2": round(r2, 4)},
        "projection": proj,
        "first_detectable": need,
        "reference": {"M69_global": M69_MAE, "M73_soft_ordinal": M73_MAE,
                      "paired_ci_halfwidth": CI_HALFWIDTH,
                      "yield_rows_per_doc": YIELD},
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m74_m2_learning_curve.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


def write_md(p):
    L = []
    A = L.append
    A("# M74 — 관측 수를 늘리면 정말 좋아지는가 (수집 없이 잰 학습곡선)\n")
    A("> 질문: **신규 Bizinfo 를 수천 건 받기 전에, 지금 곡선의 기울기가**")
    A("> **그 비용을 정당화할 만큼 가파른지 먼저 확인한다.**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    A("```text")
    for k, v in p["unchanged"].items():
        A("%-14s %s" % (k, v))
    A("%-14s %s" % ("바뀐 것", p["changed"]))
    A("```\n")
    A("## 1. 학습곡선\n")
    A("| 비율 | 학습 n | MAE(log10) | 표준편차 | Stage1 acc |")
    A("|---:|---:|---:|---:|---:|")
    for r in p["curve"]:
        sd = r.get("MAE_sd")
        sd_s = "—" if sd is None or pd.isna(sd) else "%.4f" % sd
        A("| %.0f%% | %d | %.4f | %s | %.3f |"
          % (100 * r["frac"], r["n"], r["MAE"], sd_s, r["acc"]))
    A("")
    f = p["power_fit"]
    A("## 2. 기울기\n")
    A("```text")
    A("%s" % f["form"])
    A("a   = %.4f   (클수록 데이터가 잘 듣는다)" % f["a"])
    A("R^2 = %.4f" % f["r2"])
    A("```\n")
    A("## 3. 외삽 — 몇 행을 더 모으면 무엇이 보이는가\n")
    A("검출 기준은 M73 의 paired 95%% CI 반폭 **%.3f** 이다. 기대 개선폭이 "
      "이보다 작으면 재도 보이지 않는다.\n" % p["reference"]["paired_ci_halfwidth"])
    A("| 추가 학습행 | 총 n | 기대 ΔMAE | 검출 | 수집 공고 | 요청 수 |")
    A("|---:|---:|---:|:---:|---:|---:|")
    for r in p["projection"]:
        A("| +%d | %d | %.5f | %s | %s | %s |"
          % (r["add_rows"], r["total_n"], r["expected_delta_MAE"],
             "가능" if r["detectable"] else "불가",
             format(r["collect_docs"], ","), format(r["requests"], ",")))
    A("")
    A("## 결론\n")
    A("```text")
    nd = p["first_detectable"]
    if nd:
        A("검출 가능해지는 최소 지점   +%d행" % nd["add_rows"])
        A("  필요한 수집 공고          %s건 (요청 %s회)"
          % (format(nd["collect_docs"], ","), format(nd["requests"], ",")))
    else:
        A("+2,000행까지 어떤 규모로도 검출 가능한 개선이 나오지 않는다.")
    A("파일럿 500건이 주는 것      약 %d행 -> 기대 ΔMAE %.5f (검출 불가)"
      % (p["projection"][0]["add_rows"], p["projection"][0]["expected_delta_MAE"]))
    A("```")
    with open(C.report_path("m74_m2_learning_curve.md"), "w",
              encoding="utf-8") as fo:
        fo.write("\n".join(L))
    print("[report] m74_m2_learning_curve.md")


if __name__ == "__main__":
    main()
