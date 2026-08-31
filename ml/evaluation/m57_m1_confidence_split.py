r"""M57 — 모델 1 마감 검증: 라벨 확신도별 성능 분해 + 사업화 쏠림 해부.

지시서(Part A)를 그대로 실행한다. **새 모델을 찾지 않는다.**

    금지: 새 PLM 탐색 / 새 하이퍼파라미터 탐색 / external 을 보고 재학습 /
          class weight 를 test 결과에 맞춰 튜닝 / 라벨 기준을 예측에 맞춰 수정

그래서 이 스크립트는 아무것도 학습하지 않는다. 이미 저장된 산출물만 읽는다.

    reports/dl12_m1_candidates_dl.json    외부 131건 정답 + DL 후보 3종의 예측
    reports/dl12_m1_candidates_ml.json    같은 131건 + ML 후보 3종의 예측
    reports/dl16_m1_abstention.json       판단보류 커버리지 곡선 (KLUE-BERT)
    models/m1_dl_bundle/*.parquet 학습 1,404건 / 외부 131건(라벨 확신도)

M54 가 라벨 확신도별 **정확도**까지는 냈다. 여기서는 지시서가 요구한
나머지를 채운다 — 부분집합별 Macro-F1 · 클래스별 P/R/F1 · confusion,
그리고 `사업화` 쏠림이 어디서 오는지.

두 개의 '확신도'를 절대 섞지 않는다
    라벨 확신도   라벨러가 정답을 붙이며 남긴 등급(높음/보통/중간/낮음).
                 **정답셋 쪽** 불확실성이다. 이 문서의 분해 축이다.
    모델 확신도   softmax 최고확률. **모델 쪽** 불확실성이고 판단보류의
                 기준이다(dl16). 운영에서 쓸 수 있는 것은 이쪽뿐이다 —
                 라벨 확신도는 서비스 시점에 존재하지 않는다.

한계 두 가지를 먼저 적는다
    1 dl12 의 external_pred 는 **시드 하나**의 예측이다. 공표 정확도
      0.8422 ± 0.0072 는 3시드 평균이고, 이 시드는 0.8321 이다. 부분집합을
      더 쪼개면 흔들림은 더 커진다. 그래서 점추정마다 부트스트랩 구간을 붙인다.
    2 외부 131건에 없는 클래스가 4종이라 그쪽 성능은 측정되지 않는다.
      Macro-F1 은 **그 부분집합의 정답에 등장한 클래스**에 대해서만 낸다
      (dl12 의 macro_f1_present 와 같은 규약).
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

import json
import os
import sys
from collections import Counter, OrderedDict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

BUNDLE = os.path.join(C.MODELS, "m1_dl_bundle")
ADOPTED = "KLUE-BERT"
HIGH = "높음"                    # 라벨러가 '높음'을 준 건만 high-confidence 로 본다
DRIFT_CLASS = "사업화"           # 지시서 Q2 가 지목한 쏠림 대상
SEED = 42
N_BOOT = 4000


# ------------------------------------------------------------------ 지표
def macro_f1_present(gold, pred):
    """정답에 등장한 클래스에 대해서만 macro-F1. dl12 의 규약과 같다.

    19클래스 전체로 매크로를 잡으면 외부셋에 아예 없는 4종이 F1=0 으로
    들어와 부분집합끼리 비교가 안 된다.
    """
    f1s = []
    for c in sorted(set(gold)):
        tp = sum(1 for g, p in zip(gold, pred) if g == c and p == c)
        fp = sum(1 for g, p in zip(gold, pred) if g != c and p == c)
        fn = sum(1 for g, p in zip(gold, pred) if g == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return float(np.mean(f1s)) if f1s else 0.0


def weighted_f1(gold, pred):
    tot = 0.0
    for c, w in Counter(gold).items():
        tp = sum(1 for g, p in zip(gold, pred) if g == c and p == c)
        fp = sum(1 for g, p in zip(gold, pred) if g != c and p == c)
        fn = sum(1 for g, p in zip(gold, pred) if g == c and p != c)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        tot += f1 * w
    return tot / len(gold) if gold else 0.0


def accuracy(gold, pred):
    return float(np.mean([g == p for g, p in zip(gold, pred)])) if gold else 0.0


def per_class(gold, pred, train_counts):
    """부분집합 안에서의 클래스별 P/R/F1. 예측만 있고 정답이 없는 클래스도 남긴다
    — `사업화` 처럼 '정답보다 예측이 많은' 클래스가 바로 그 자리에 드러난다."""
    rows = []
    seen = sorted(set(gold) | set(pred), key=lambda c: (-Counter(gold)[c], c))
    for c in seen:
        n_gold = sum(1 for g in gold if g == c)
        n_pred = sum(1 for p in pred if p == c)
        tp = sum(1 for g, p in zip(gold, pred) if g == c and p == c)
        rec = tp / n_gold if n_gold else None
        prec = tp / n_pred if n_pred else None
        f1 = (2 * prec * rec / (prec + rec)) if (prec and rec) else 0.0
        rows.append({"클래스": c, "학습표본": int(train_counts.get(c, 0)),
                     "정답수": n_gold, "예측수": n_pred, "TP": tp,
                     "재현율": None if rec is None else round(rec, 4),
                     "정밀도": None if prec is None else round(prec, 4),
                     "F1": round(f1, 4)})
    return rows


def confusion(gold, pred):
    """0 이 아닌 칸만. 19x19 격자를 통째로 찍으면 아무도 읽지 않는다."""
    c = Counter(zip(gold, pred))
    return [{"정답": g, "예측": p, "건수": n, "정답여부": g == p}
            for (g, p), n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]


def boot_metric(gold, pred, fn, n=N_BOOT, seed=SEED):
    """부분집합 지표의 부트스트랩 95% 구간. n=52 짜리 macro-F1 을 점추정만으로
    읽으면 안 된다는 것을 숫자로 보이기 위해 붙인다."""
    rng = np.random.default_rng(seed)
    g, p = np.array(gold, dtype=object), np.array(pred, dtype=object)
    vals = []
    for _ in range(n):
        i = rng.integers(0, len(g), len(g))
        vals.append(fn(list(g[i]), list(p[i])))
    return [round(float(np.percentile(vals, 2.5)), 4),
            round(float(np.percentile(vals, 97.5)), 4)]


def boot_gap(gold, pred, mask_a, mask_b, fn, n=N_BOOT, seed=SEED):
    """두 부분집합의 지표 차이. **각 부분집합 안에서 따로 리샘플**한다(층화).
    전체를 리샘플하면 부분집합 크기까지 같이 흔들려 차이가 아니라 구성비
    변동을 재게 된다."""
    rng = np.random.default_rng(seed)
    g, p = np.array(gold, dtype=object), np.array(pred, dtype=object)
    ia, ib = np.where(mask_a)[0], np.where(mask_b)[0]
    out = []
    for _ in range(n):
        sa, sb = rng.choice(ia, len(ia)), rng.choice(ib, len(ib))
        out.append(fn(list(g[sa]), list(p[sa])) - fn(list(g[sb]), list(p[sb])))
    out = np.array(out)
    return {"gap_mean": round(float(out.mean()), 4),
            "ci95": [round(float(np.percentile(out, 2.5)), 4),
                     round(float(np.percentile(out, 97.5)), 4)],
            "p_gap_gt_0": round(float((out > 0).mean()), 4)}


def subset_block(name, gold, pred, train_counts):
    return {
        "이름": name, "N": len(gold),
        "정확도": round(accuracy(gold, pred), 4),
        "정확도_ci95": boot_metric(gold, pred, accuracy),
        "macro_f1_present": round(macro_f1_present(gold, pred), 4),
        "macro_f1_ci95": boot_metric(gold, pred, macro_f1_present),
        "weighted_f1": round(weighted_f1(gold, pred), 4),
        "정답클래스수": len(set(gold)), "예측클래스수": len(set(pred)),
        "오류수": int(sum(1 for g, p in zip(gold, pred) if g != p)),
        "per_class": per_class(gold, pred, train_counts),
        "confusion": confusion(gold, pred),
    }


# ------------------------------------------------------- 사업화 쏠림 해부
def drift_analysis(ex, gold, pred, all_preds):
    """지시서 Q2 — `사업화` 쏠림이 어디서 발생하는가.

    (1) 어떤 정답 클래스가 흡수되는가
    (2) 그 사례들이 low-confidence 에 몰려 있는가
    (3) 같은 오답을 **다른 모델 계열도** 내는가
        여섯 후보(DL 3 + ML 3)가 모두 같은 곳에서 흡수하면 그것은 KLUE-BERT
        의 버릇이 아니라 **클래스 경계 자체가 겹친다**는 뜻이다. 라벨 누수
        (M05)를 제거한 뒤에도 남는 구조적 겹침인지 여기서 갈린다.
    """
    conf = ex["confidence"].fillna("미기재").to_numpy()
    title = ex["title"].to_numpy()
    text = ex["text"].to_numpy()

    absorbed = [i for i in range(len(gold))
                if pred[i] == DRIFT_CLASS and gold[i] != DRIFT_CLASS]
    by_true = Counter(gold[i] for i in absorbed)
    by_conf = Counter(conf[i] for i in absorbed)

    cases = []
    for i in absorbed:
        agree = [m for m, pr in all_preds.items() if pr[i] == DRIFT_CLASS]
        cases.append({
            "정답": gold[i], "라벨확신도": str(conf[i]),
            "title": str(title[i])[:60],
            "동일오답_모델수": len(agree), "동일오답_모델": agree,
            "요약머리": str(text[i]).replace("\n", " ")[:90],
        })
    cases.sort(key=lambda r: (-r["동일오답_모델수"], r["정답"]))

    prec_by_subset = {}
    for nm, m in (("전체", np.ones(len(gold), bool)),
                  ("high", conf == HIGH), ("non-high", conf != HIGH)):
        idx = np.where(m)[0]
        n_pred = sum(1 for i in idx if pred[i] == DRIFT_CLASS)
        tp = sum(1 for i in idx if pred[i] == DRIFT_CLASS and gold[i] == DRIFT_CLASS)
        n_gold = sum(1 for i in idx if gold[i] == DRIFT_CLASS)
        prec_by_subset[nm] = {"정답수": n_gold, "예측수": n_pred, "TP": tp,
                              "정밀도": round(tp / n_pred, 4) if n_pred else None,
                              "과예측": n_pred - n_gold}

    by_model = {}
    for m, pr in all_preds.items():
        n_pred = sum(1 for p in pr if p == DRIFT_CLASS)
        tp = sum(1 for g, p in zip(gold, pr) if g == p == DRIFT_CLASS)
        by_model[m] = {"예측수": n_pred, "TP": tp,
                       "정밀도": round(tp / n_pred, 4) if n_pred else None,
                       "정확도": round(accuracy(gold, pr), 4)}

    n_abs = len(absorbed)
    n_low = sum(1 for i in absorbed if conf[i] != HIGH)
    return {
        "설명": "예측이 %s 인데 정답이 아닌 건" % DRIFT_CLASS,
        "정답수": int(sum(1 for g in gold if g == DRIFT_CLASS)),
        "예측수": int(sum(1 for p in pred if p == DRIFT_CLASS)),
        "흡수건수": n_abs,
        "유입_클래스": [{"정답": k, "건수": v} for k, v in by_true.most_common()],
        "유입_라벨확신도": [{"라벨확신도": str(k), "건수": v} for k, v in by_conf.most_common()],
        "non_high_비중": round(n_low / n_abs, 4) if n_abs else None,
        "non_high_건수": n_low,
        "부분집합별_정밀도": prec_by_subset,
        "모델계열별": by_model,
        "사례": cases,
    }


# ---------------------------------------------------------------- 본체
def main():
    with open(os.path.join(C.REPORTS, "dl12_m1_candidates_dl.json"), encoding="utf-8") as f:
        dl12 = json.load(f)
    with open(os.path.join(C.REPORTS, "dl12_m1_candidates_ml.json"), encoding="utf-8") as f:
        ml12 = json.load(f)
    with open(os.path.join(C.REPORTS, "dl16_m1_abstention.json"), encoding="utf-8") as f:
        dl16 = json.load(f)
    tr = pd.read_parquet(os.path.join(BUNDLE, "train.parquet"))
    ex = pd.read_parquet(os.path.join(BUNDLE, "external.parquet"))

    gold = dl12["gold"]
    assert gold == ex["gold"].tolist(), "dl12 의 gold 순서가 external.parquet 과 다르다"
    assert gold == ml12["gold"], "DL/ML 후보의 gold 순서가 다르다"
    all_preds = OrderedDict()
    for src in (dl12, ml12):
        for k, v in src["results"].items():
            all_preds[k] = v["external_pred"]
    pred = all_preds[ADOPTED]
    train_counts = tr["label"].value_counts().to_dict()
    conf = ex["confidence"].fillna("미기재").to_numpy()

    print("M57 — 모델 1 마감 검증 (라벨 확신도 분해 · 사업화 쏠림)")
    print("  학습 없음. 저장된 산출물만 재집계한다.")
    print("  채택 모델 %s / 외부 %d건 / 학습 %d건 %d클래스"
          % (ADOPTED, len(gold), len(tr), tr["label"].nunique()))
    print("  이 예측의 정확도 %.4f (시드 1개) · 공표 0.8422 ± 0.0072 (3시드 평균)"
          % accuracy(gold, pred))

    # ---- 1. 세 부분집합
    masks = OrderedDict([
        ("전체 external", np.ones(len(gold), bool)),
        ("High-confidence", conf == HIGH),
        ("Non-high-confidence", conf != HIGH),
    ])
    blocks = OrderedDict()
    for nm, m in masks.items():
        g = [gold[i] for i in range(len(gold)) if m[i]]
        p = [pred[i] for i in range(len(gold)) if m[i]]
        blocks[nm] = subset_block(nm, g, p, train_counts)

    print("\n== 1. 라벨 확신도로 나눈 성능 (지시서 Part A 2절)")
    print("  %-22s %5s %9s %-18s %9s %-18s %6s"
          % ("부분집합", "N", "정확도", "95% CI", "Macro-F1", "95% CI", "오류"))
    for nm, b in blocks.items():
        print("  %-22s %5d %9.4f  [%.3f, %.3f] %9.4f  [%.3f, %.3f] %6d"
              % (nm, b["N"], b["정확도"], b["정확도_ci95"][0], b["정확도_ci95"][1],
                 b["macro_f1_present"], b["macro_f1_ci95"][0], b["macro_f1_ci95"][1],
                 b["오류수"]))

    hi, lo = masks["High-confidence"], masks["Non-high-confidence"]
    gaps = {"accuracy": boot_gap(gold, pred, hi, lo, accuracy),
            "macro_f1_present": boot_gap(gold, pred, hi, lo, macro_f1_present)}
    print("\n  High - Non-high 격차 (각 부분집합 안에서 층화 리샘플)")
    for k, v in gaps.items():
        print("    %-18s %+.4f  95%% [%+.4f, %+.4f]  P(격차>0)=%.3f"
              % (k, v["gap_mean"], v["ci95"][0], v["ci95"][1], v["p_gap_gt_0"]))

    grade = []
    for c in ["높음", "보통", "중간", "낮음"]:
        m = conf == c
        if not m.sum():
            continue
        g = [gold[i] for i in range(len(gold)) if m[i]]
        p = [pred[i] for i in range(len(gold)) if m[i]]
        grade.append({"라벨확신도": c, "n": int(m.sum()),
                      "정확도": round(accuracy(g, p), 4),
                      "macro_f1_present": round(macro_f1_present(g, p), 4),
                      "정답클래스수": len(set(g))})
    print("\n  등급 4단계")
    for r in grade:
        print("    %-4s n=%3d  정확도 %.4f  Macro-F1 %.4f (정답 %d클래스)"
              % (r["라벨확신도"], r["n"], r["정확도"], r["macro_f1_present"],
                 r["정답클래스수"]))

    # ---- 2. 클래스별 표
    print("\n== 2. 클래스별 (정답/예측 · F1) — 전체 vs High vs Non-high")
    idx = {nm: {x["클래스"]: x for x in blocks[nm]["per_class"]} for nm in blocks}
    print("  %-10s %6s | %-14s | %-14s | %-14s"
          % ("클래스", "학습", "전체", "High", "Non-high"))
    for x in blocks["전체 external"]["per_class"]:
        c = x["클래스"]
        cells = []
        for nm in blocks:
            y = idx[nm].get(c)
            cells.append("     -        " if y is None
                         else "%2d/%2d  F1 %.2f" % (y["정답수"], y["예측수"], y["F1"]))
        print("  %-10s %6d | %-14s | %-14s | %-14s"
              % (c, train_counts.get(c, 0), cells[0], cells[1], cells[2]))

    # ---- 3. 사업화 쏠림
    drift = drift_analysis(ex, gold, pred, all_preds)
    print("\n== 3. `%s` 쏠림 (지시서 Q2)" % DRIFT_CLASS)
    print("  정답 %d건 / 예측 %d건 / 흡수 %d건 / 그중 non-high %d건 (%.0f%%)"
          % (drift["정답수"], drift["예측수"], drift["흡수건수"],
             drift["non_high_건수"], (drift["non_high_비중"] or 0) * 100))
    print("  유입 클래스: %s"
          % ", ".join("%s %d" % (r["정답"], r["건수"]) for r in drift["유입_클래스"]))
    print("  유입 라벨확신도: %s"
          % ", ".join("%s %d" % (r["라벨확신도"], r["건수"]) for r in drift["유입_라벨확신도"]))
    print("\n  부분집합별 %s 정밀도" % DRIFT_CLASS)
    for k, v in drift["부분집합별_정밀도"].items():
        print("    %-9s 정답 %2d / 예측 %2d / 과예측 %+d / 정밀도 %s"
              % (k, v["정답수"], v["예측수"], v["과예측"],
                 "-" if v["정밀도"] is None else "%.4f" % v["정밀도"]))
    print("\n  여섯 후보가 모두 같은 쏠림을 보이는가 (구조적 겹침 판별)")
    print("    %-22s %6s %5s %9s %9s" % ("모델", "예측수", "TP", "정밀도", "정확도"))
    for k, v in drift["모델계열별"].items():
        print("    %-22s %6d %5d %9s %9.4f"
              % (k, v["예측수"], v["TP"],
                 "-" if v["정밀도"] is None else "%.4f" % v["정밀도"], v["정확도"]))
    print("\n  흡수된 건 (동일 오답을 낸 모델 수 순)")
    for r in drift["사례"]:
        print("    [%s/%s] %d/6모델  %s"
              % (r["정답"], r["라벨확신도"], r["동일오답_모델수"], r["title"][:52]))

    # ---- 4. 운영 판단보류
    cov = dl16.get("mean_by_coverage", {}).get("max_proba", [])
    print("\n== 4. 운영 판단보류 검토 (지시서 Part A 4절)")
    for r in cov:
        if r["coverage"] in (1.0, 0.9, 0.8, 0.7, 0.6):
            print("   커버리지 %3.0f%%  n=%3d  정확도 %.4f +- %.4f  (임계 %.3f)"
                  % (r["coverage"] * 100, r["n"], r["accuracy_mean"],
                     r["accuracy_std"], r["threshold_mean"]))
    print("   * 임계값을 이 외부셋에서 고르지 않는다. 커버리지 목표를 먼저 정하고")
    print("     학습셋 OOF 에서 잡는다 (dl16 단서 그대로).")

    rep = {
        "note": "학습 없음. dl12(DL/ML)·dl16·m1_dl_bundle 재집계",
        "adopted": ADOPTED,
        "seed_note": "external_pred 는 시드 1개. 공표 정확도 0.8422 +- 0.0072 는 3시드 평균",
        "high_confidence_정의": "라벨러 등급 '%s' 만 high, 나머지(보통·중간·낮음) 는 non-high" % HIGH,
        "macro_f1_규약": "그 부분집합의 정답에 등장한 클래스만 (dl12 macro_f1_present 와 동일)",
        "subsets": list(blocks.values()),
        "high_vs_nonhigh_gap": gaps,
        "by_grade": grade,
        "사업화_쏠림": drift,
        "coverage_curve": cov,
        "limits": ["시드 1개 예측", "외부에 없는 클래스 4종 미측정",
                   "non-high 52건의 Macro-F1 은 구간이 넓다"],
    }
    C.save_report("m57_m1_confidence_split.json", rep)
    write_md(rep, blocks, train_counts)


def write_md(r, blocks, train_counts):
    b_all = blocks["전체 external"]
    b_hi = blocks["High-confidence"]
    b_lo = blocks["Non-high-confidence"]
    d = r["사업화_쏠림"]
    g_acc = r["high_vs_nonhigh_gap"]["accuracy"]
    g_f1 = r["high_vs_nonhigh_gap"]["macro_f1_present"]

    L = ["# M57 — 모델 1 마감 검증 (라벨 확신도 분해 · `사업화` 쏠림)", "",
         "> 새 모델을 찾지 않습니다. 학습도 하지 않습니다. dl12(DL 3종 + ML 3종)·",
         "> dl16·학습/외부 bundle 을 다시 집계했을 뿐입니다.", "",
         "```text",
         "채택 모델  %s" % r["adopted"],
         "외부       131건 (라벨러 확신도 동봉) / 학습 1,404건 19클래스",
         "예측       %s" % r["seed_note"],
         "Macro-F1   %s" % r["macro_f1_규약"],
         "```", "",
         "## 0. 두 개의 '확신도'를 섞지 않습니다", "",
         "| | 무엇의 불확실성인가 | 서비스 시점에 존재하는가 |",
         "|---|---|---|",
         "| **라벨 확신도** (높음/보통/중간/낮음) | 정답셋 쪽 | **없음** — 라벨러가 사후에 붙인 등급 |",
         "| **모델 확신도** (softmax 최고확률) | 모델 쪽 | 있음 — 판단보류의 기준 |", "",
         "1~3장은 **라벨 확신도**로 나눕니다. 정확도 0.8422 안에 정답셋의 모호성이",
         "얼마나 섞여 있는지 보려는 것이지 운영 규칙을 만드는 것이 아닙니다.",
         "운영 규칙은 4장에서 **모델 확신도**로만 다룹니다.", "",
         "## 1. 라벨 확신도별 성능", "",
         "| 부분집합 | N | Accuracy | 95% CI | Macro-F1 | 95% CI | 오류 | 정답 클래스 |",
         "|---|---:|---:|---|---:|---|---:|---:|"]
    for nm in blocks:
        b = blocks[nm]
        L.append("| %s | %d | **%.4f** | %.3f ~ %.3f | **%.4f** | %.3f ~ %.3f | %d | %d |"
                 % (nm, b["N"], b["정확도"], b["정확도_ci95"][0], b["정확도_ci95"][1],
                    b["macro_f1_present"], b["macro_f1_ci95"][0], b["macro_f1_ci95"][1],
                    b["오류수"], b["정답클래스수"]))
    L += ["",
          "**High − Non-high 격차** — 각 부분집합 **안에서** 층화 리샘플했습니다.",
          "전체를 리샘플하면 부분집합 크기까지 흔들려 격차가 아니라 구성비 변동을",
          "재게 됩니다.", "",
          "| 지표 | 격차 | 95% CI | P(격차>0) |", "|---|---:|---|---:|",
          "| Accuracy | **%+.4f** | %+.4f ~ %+.4f | %.3f |"
          % (g_acc["gap_mean"], g_acc["ci95"][0], g_acc["ci95"][1], g_acc["p_gap_gt_0"]),
          "| Macro-F1 | **%+.4f** | %+.4f ~ %+.4f | %.3f |"
          % (g_f1["gap_mean"], g_f1["ci95"][0], g_f1["ci95"][1], g_f1["p_gap_gt_0"]), "",
          "등급 4단계 (M54 재확인 + Macro-F1)", "",
          "| 라벨 확신도 | n | Accuracy | Macro-F1 | 정답 클래스 수 |",
          "|---|---:|---:|---:|---:|"]
    for x in r["by_grade"]:
        L.append("| %s | %d | %.4f | %.4f | %d |"
                 % (x["라벨확신도"], x["n"], x["정확도"], x["macro_f1_present"],
                    x["정답클래스수"]))
    L += ["",
          "> Non-high %d건의 Macro-F1 은 클래스당 정답이 1~2건인 것들을 포함합니다."
          % b_lo["N"],
          "> 구간이 넓은 것은 모델이 불안정해서가 아니라 **표본이 그만큼 작기",
          "> 때문**입니다. 점추정만 떼어 읽지 마십시오.", "",
          "## 2. 클래스별 P/R/F1", "",
          "| 클래스 | 학습표본 | 전체 정답/예측 | 전체 F1 | High 정답/예측 | High F1 | Non-high 정답/예측 | Non-high F1 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    idx = {nm: {x["클래스"]: x for x in blocks[nm]["per_class"]} for nm in blocks}
    for x in b_all["per_class"]:
        c = x["클래스"]
        cells = []
        for nm in blocks:
            y = idx[nm].get(c)
            cells += ["—", "—"] if y is None else [
                "%d/%d" % (y["정답수"], y["예측수"]), "%.2f" % y["F1"]]
        L.append("| %s | %d | %s |" % (c, train_counts.get(c, 0), " | ".join(cells)))

    L += ["", "### Confusion — 오답 칸만 (0 인 칸은 싣지 않습니다)", ""]
    for nm in blocks:
        wrong = [x for x in blocks[nm]["confusion"] if not x["정답여부"]]
        L += ["**%s** (오류 %d건 / %d건 중)" % (nm, blocks[nm]["오류수"], blocks[nm]["N"]), "",
              "| 정답 | 예측 | 건수 |", "|---|---|---:|"]
        for x in wrong:
            L.append("| %s | %s | %d |" % (x["정답"], x["예측"], x["건수"]))
        L.append("")

    L += ["## 3. `사업화` 쏠림은 어디서 오는가", "",
          "```text",
          "정답 %d건  ·  예측 %d건  ·  흡수(예측=사업화, 정답!=사업화) %d건"
          % (d["정답수"], d["예측수"], d["흡수건수"]),
          "그중 non-high 라벨 %d건 (%.0f%%)"
          % (d["non_high_건수"], (d["non_high_비중"] or 0) * 100),
          "```", "",
          "**어떤 정답 클래스가 흡수되는가**", "",
          "| 정답 클래스 | → `사업화` 건수 |", "|---|---:|"]
    for x in d["유입_클래스"]:
        L.append("| %s | %d |" % (x["정답"], x["건수"]))
    L += ["", "**부분집합별 `사업화` 정밀도**", "",
          "| 부분집합 | 정답 | 예측 | 과예측 | 정밀도 |", "|---|---:|---:|---:|---:|"]
    for k, v in d["부분집합별_정밀도"].items():
        L.append("| %s | %d | %d | %+d | %s |"
                 % (k, v["정답수"], v["예측수"], v["과예측"],
                    "—" if v["정밀도"] is None else "%.4f" % v["정밀도"]))
    L += ["", "**KLUE-BERT 의 버릇인가, 클래스 경계 자체가 겹치는가**", "",
          "같은 131건에 대해 여섯 후보(DL 3 + ML 3)의 `사업화` 과예측을 나란히",
          "둡니다. 한 모델만 그렇다면 백본 문제이고, 전부 그렇다면 **경계가**",
          "**구조적으로 겹치는 것**입니다 — 라벨 누수 제거(M05) 뒤에도 남는",
          "성질입니다.", "",
          "| 모델 | `사업화` 예측수 | TP | 정밀도 | 전체 정확도 |",
          "|---|---:|---:|---:|---:|"]
    for k, v in d["모델계열별"].items():
        mark = " **(채택)**" if k == r["adopted"] else ""
        L.append("| %s%s | %d | %d | %s | %.4f |"
                 % (k, mark, v["예측수"], v["TP"],
                    "—" if v["정밀도"] is None else "%.4f" % v["정밀도"], v["정확도"]))
    L += ["", "**흡수된 건 하나하나** — `동일오답` 은 여섯 후보 중 몇 개가 같은 오답을",
          "냈는지입니다. 6/6 이면 그 건은 어느 모델로 바꿔도 같은 곳으로 갑니다.", "",
          "| 정답 | 라벨확신도 | 동일오답 | 공고명 |", "|---|---|---:|---|"]
    for x in d["사례"]:
        L.append("| %s | %s | %d/6 | %s |"
                 % (x["정답"], x["라벨확신도"], x["동일오답_모델수"], x["title"]))

    L += ["", "## 4. 운영 판단보류 — 라벨 확신도가 아니라 모델 확신도로", "",
          "1~3장의 라벨 확신도는 **서비스 시점에 없습니다.** 운영에서 쓸 수 있는",
          "축은 모델의 softmax 최고확률뿐이고, 그 곡선은 dl16 이 이미 냈습니다.", "",
          "| 커버리지 | n | 정확도 | 표준편차 | 임계값 |", "|---:|---:|---:|---:|---:|"]
    for x in r["coverage_curve"]:
        L.append("| %.0f%% | %d | %.4f | %.4f | %.3f |"
                 % (x["coverage"] * 100, x["n"], x["accuracy_mean"],
                    x["accuracy_std"], x["threshold_mean"]))
    L += ["",
          "> **임계값을 이 표에서 고르지 않습니다.** 외부 131건에서 임계값을 고르면",
          "> 그 순간 외부셋은 검증셋이 아닙니다. 커버리지 목표(예: 70%)를 먼저",
          "> 정하고 임계값은 **학습셋 OOF** 에서 잡습니다.", "",
          "## 5. 읽은 것", ""]

    n_over = sum(1 for v in d["모델계열별"].values() if v["예측수"] > d["정답수"])
    n_model = len(d["모델계열별"])
    med_agree = float(np.median([x["동일오답_모델수"] for x in d["사례"]])) if d["사례"] else 0
    hi_prec = d["부분집합별_정밀도"]["high"]
    train_total = max(1, sum(train_counts.values()))
    L += ["**(1) 라벨 확신도가 높은 쪽에서 성능이 확실히 올라갑니다.** High %d건에서"
          % b_hi["N"],
          "정확도 %.4f / Macro-F1 %.4f, Non-high %d건에서 %.4f / %.4f 입니다."
          % (b_hi["정확도"], b_hi["macro_f1_present"], b_lo["N"],
             b_lo["정확도"], b_lo["macro_f1_present"]),
          "정확도 격차의 95%% 구간(%+.4f ~ %+.4f)이 0 을 품지 %s."
          % (g_acc["ci95"][0], g_acc["ci95"][1],
             "않습니다" if g_acc["ci95"][0] > 0 else "습니다"),
          "그래서 전체 정확도 %.4f 는 **모델의 한계와 정답셋의 모호성이 섞인**"
          % b_all["정확도"],
          "**숫자**이며, 뒤쪽은 모델을 바꿔도 줄지 않습니다.", "",
          "**(2) `사업화` 쏠림은 어느 한 백본의 버릇이 아닙니다.** 후보 %d종 중 %d종이"
          % (n_model, n_over),
          "`사업화` 를 정답수(%d건)보다 많이 예측합니다 — TF-IDF+LinearSVM 처럼 계열이"
          % d["정답수"],
          "완전히 다른 모델까지 포함해서입니다. 백본을 바꿔서 없어지는 현상이",
          "아닙니다. 원인은 **학습 분포**입니다 — 사업화가 학습 %d건 중 %d건(%.0f%%)로"
          % (train_total, train_counts.get("사업화", 0),
             train_counts.get("사업화", 0) / train_total * 100),
          "가장 크고, 애매한 입력에서 각 모델이 그쪽으로 밀립니다.", "",
          "**다만 '같은 공고가 모든 모델에 사업화로 보인다'는 아닙니다.** 흡수 %d건의"
          % d["흡수건수"],
          "모델 합의 수는 중앙값 %.0f/%d 입니다. 특정 공고가 구조적으로 `사업화`"
          % (med_agree, n_model),
          "쪽인 것이 아니라, 각 모델이 **서로 다른 애매한 건에서 각자 다수 클래스로**",
          "**밀리는** 모습입니다. 유입 클래스가 컨설팅·판로·융자·설비·연구개발 등으로",
          "넓게 퍼져 있는 것도 같은 이야기입니다 — 인접 클래스 한 쌍의 경계 문제가",
          "아니라 다수 클래스 쪽으로 향한 전반적인 사전확률 압력입니다.", "",
          "**(3) 쏠림이 정답셋 모호성만으로 설명되지도 않습니다.** 흡수 %d건 중 %d건"
          % (d["흡수건수"], d["non_high_건수"]),
          "(%.0f%%)이 non-high 라벨이지만, High 부분집합 안에서도 `사업화` 는 정답"
          % ((d["non_high_비중"] or 0) * 100),
          "%d건에 예측 %d건(정밀도 %.2f)으로 여전히 과예측입니다. 즉 (2)의 분포"
          % (hi_prec["정답수"], hi_prec["예측수"], hi_prec["정밀도"] or 0),
          "압력과 (1)의 라벨 모호성이 **겹쳐서** 나오는 오차입니다.", "",
          "**(4) 그래서 마감 결론은 Freeze 입니다.** 지시서가 금지한 것(새 PLM 탐색·",
          "재학습·test 에 맞춘 튜닝)을 하지 않고도 남은 오차의 위치가 확인됐습니다 —",
          "정답셋 모호성과 다수 클래스 사전확률. 둘 다 백본 교체로 줄지 않습니다.",
          "남은 개선 여지는 모델이 아니라 **라벨 정의와 학습 표본 구성** 쪽입니다.", "",
          "**(5) 운영은 판단보류를 켜는 쪽이 낫습니다.** 커버리지를 줄이면 정확도가",
          "단조롭게 오르고(4장), 모델 1의 오류는 하류(모델 2·3)에서 **비교군을**",
          "**통째로 잘못 잡는 것**으로 번집니다. 다만 임계값은 이 외부셋이 아니라",
          "학습셋 OOF 에서 잡습니다.", ""]

    p = os.path.join(C.REPORTS, "m57_m1_confidence_split.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
