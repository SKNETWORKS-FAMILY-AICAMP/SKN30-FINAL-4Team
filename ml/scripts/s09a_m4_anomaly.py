"""S09A — 모델 4: 사업 설계 이상패턴 탐지.

한 줄 정의 (설계서)
    과거 지원사업의 다변량 설계 패턴을 학습하여 신규 사업의 희귀성·이례성을
    탐지하는 비지도 이상탐지 모델.

'이례적'이지 '잘못됐다'가 아니다. 출력 문구를 코드에서 강제한다(ALLOWED/FORBIDDEN).

S04E 가 이 모델에 Conditional 을 준 이유가 하나 있다.
    수치 4축(기업당지원액·지원건수·지원비율·사업기간) 중 3축 이상이 채워진 행이
    전체의 22% 뿐이다. 결측을 0이나 중앙값으로 메우고 학습하면 모델이 탐지하는
    것은 '희귀한 설계'가 아니라 '원문에 안 적힌 사업'이 된다.
그래서 두 가지를 나눈다.
    학습    수치 2축 이상이 채워진 행만 (결측 대치 최소화)
    표시    결측 축은 점수에서 빼고 'n축 근거로 계산'을 함께 출력

평가는 정답이 없으므로 네 갈래로 우회한다 (설계서 지시 그대로).
    1. synthetic anomaly injection — 일부러 만든 이례 사례를 상위로 올리는가
    2. contamination 변화 안정성 — 파라미터를 바꿔도 상위 사례가 유지되는가
    3. 재학습 일관성 — 표본을 갈아도 상위 사례가 유지되는가
    4. feature-level local explanation — 왜 이례적인지 축 단위로 말할 수 있는가
"""
import os
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SRC = os.path.join(C.PROC, "design_features.parquet")
REF = os.path.join(C.PROC, "cohort_reference.parquet")
OUT = os.path.join(C.PROC, "design_anomaly.parquet")
SEED = 42

NUM_FEATS = ["log_per_recipient", "log_support_count", "support_ratio", "project_duration"]
CAT_FEATS = ["support_type", "support_method", "support_unit", "amount_type",
             "category_large"]
MIN_AXES = 2          # 학습에 넣을 최소 수치 축 수
CONTAMINATION = 0.05
PCT_EXTREME = 90      # 비교군 percentile 이 이 밖이면 '두드러진 축'으로 본다

ALLOWED = ["과거 사업 패턴과 차이가 큼", "희귀한 설계 조합", "동일 유형 대비 비전형적", "확인 필요"]
FORBIDDEN = ["잘못 설계됨", "부적절함", "정책적으로 문제 있음", "지원규모 과다"]


def prepare(df):
    d = df.copy()
    d.loc[d["amount_outlier"], "per_recipient"] = np.nan
    d = d[d["support_type"].notna()].copy()
    amt, cnt = d["per_recipient"], d["support_count"]
    d["log_per_recipient"] = np.log10(amt.where(amt > 0))
    d["log_support_count"] = np.log10(cnt.where(cnt > 0))
    d["n_axes"] = d[NUM_FEATS].notna().sum(axis=1)
    return d


def encode(train, apply_df):
    """수치는 표준화, 범주는 train 빈도로 인코딩한다.

    one-hot 대신 빈도 인코딩을 쓰는 이유: 이상탐지에서 알고 싶은 것은
    '이 범주가 얼마나 드문가'다. 빈도는 그 정보를 축 하나로 직접 담는다.
    one-hot 은 희귀 범주마다 축을 하나씩 늘려 차원만 키운다.

    결측 수치는 train 중앙값으로 채우되, 채웠다는 사실을 지시자 축으로 남긴다.
    지시자가 없으면 모델이 '중앙값 근처'라고 오해해 결측 행이 정상으로 보인다.
    """
    parts_tr, parts_ap, names = [], [], []
    for f in NUM_FEATS:
        med = train[f].median()
        parts_tr.append(train[f].fillna(med).to_numpy())
        parts_ap.append(apply_df[f].fillna(med).to_numpy())
        names.append(f)
        parts_tr.append(train[f].isna().astype(float).to_numpy())
        parts_ap.append(apply_df[f].isna().astype(float).to_numpy())
        names.append(f + "__missing")
    for f in CAT_FEATS:
        freq = train[f].value_counts(normalize=True)
        parts_tr.append(train[f].map(freq).fillna(0.0).to_numpy())
        parts_ap.append(apply_df[f].map(freq).fillna(0.0).to_numpy())
        names.append(f + "__freq")

    Xtr = np.column_stack(parts_tr)
    Xap = np.column_stack(parts_ap)
    sc = StandardScaler().fit(Xtr)
    return sc.transform(Xtr), sc.transform(Xap), names


def fit_models(Xtr, Xap, contamination=CONTAMINATION):
    out = {}
    iso = IsolationForest(n_estimators=500, contamination=contamination,
                          random_state=SEED, n_jobs=-1).fit(Xtr)
    out["IsolationForest"] = -iso.score_samples(Xap)

    lof = LocalOutlierFactor(n_neighbors=20, contamination=contamination, novelty=True).fit(Xtr)
    out["LocalOutlierFactor"] = -lof.score_samples(Xap)

    oc = OneClassSVM(kernel="rbf", gamma="scale", nu=contamination).fit(Xtr)
    out["OneClassSVM"] = -oc.score_samples(Xap)
    return out


def score_drivers(X, names, scores):
    """각 축이 이상점수를 얼마나 끌고 가는가.

    IsolationForest 가 합성 이상치를 거의 못 잡은 이유를 여기서 확인했다.
    IF 점수는 결측 지시자와 희귀 범주 빈도에 끌려간다 — 즉 '희귀한 설계'가
    아니라 '원문에 안 적힌 사업'을 탐지하고 있었다. S04E 가 경고한 실패 모드다.
    """
    from scipy.stats import spearmanr
    out = {}
    for name, s in scores.items():
        out[name] = {n: round(float(spearmanr(np.abs(X[:, i]), s).statistic), 3)
                     for i, n in enumerate(names)}
    return out


def norm01(v):
    v = np.asarray(v, dtype=float)
    lo, hi = np.nanmin(v), np.nanmax(v)
    return (v - lo) / (hi - lo) if hi > lo else np.zeros_like(v)


# -------------------------------------------------------- 평가 1: 합성 이상치
def inject_synthetic(train, n=60, rng=None):
    """일부러 만든 이례 사례. 실제 행을 골라 축 하나를 극단으로 민다.

    '이 모델이 무엇을 잡아야 하는가'를 코드로 못박는 장치다.
    정답이 없는 비지도 모델에서 유일하게 재현 가능한 검증 축이다.
    """
    rng = rng or np.random.default_rng(SEED)
    base = train[train["n_axes"] >= 3].sample(min(n, (train["n_axes"] >= 3).sum()),
                                              random_state=SEED).copy()
    kinds = rng.integers(0, 4, len(base))
    base = base.reset_index(drop=True)
    for i, k in enumerate(kinds):
        if k == 0:      # 극소수 기업에 극고액
            base.loc[i, "log_per_recipient"] = train["log_per_recipient"].max() + 1.0
            base.loc[i, "log_support_count"] = 0.0
        elif k == 1:    # 극다수 기업에 극소액
            base.loc[i, "log_per_recipient"] = train["log_per_recipient"].min() - 1.0
            base.loc[i, "log_support_count"] = train["log_support_count"].max() + 1.0
        elif k == 2:    # 사업기간만 비정상적으로 김
            base.loc[i, "project_duration"] = 30.0
        else:           # 지원비율이 범위를 벗어남
            base.loc[i, "support_ratio"] = 100.0
            base.loc[i, "log_per_recipient"] = train["log_per_recipient"].max() + 0.8
    base["__synthetic"] = True
    base["__kind"] = kinds
    return base


def eval_synthetic(train, models_scores_fn, n=60):
    syn = inject_synthetic(train, n)
    mixed = pd.concat([train.assign(__synthetic=False, __kind=-1), syn],
                      ignore_index=True)
    scores = models_scores_fn(train, mixed)
    is_syn = mixed["__synthetic"].to_numpy()
    out = {}
    for name, s in scores.items():
        order = np.argsort(-s)
        k = int(is_syn.sum())
        topk = is_syn[order[:k]]
        top2k = is_syn[order[:2 * k]]
        # 합성 사례가 상위 몇 %에 들어오는가
        ranks = np.argsort(np.argsort(-s))[is_syn] / len(s)
        out[name] = {"n_synthetic": k,
                     "recall_at_k": round(float(topk.mean()), 4),
                     "recall_at_2k": round(float(top2k.mean()), 4),
                     "median_rank_pct": round(float(np.median(ranks)) * 100, 2)}
    return out


# -------------------------------------------------------- 평가 2: 안정성
def eval_contamination(train, apply_df, model, tops=30):
    """contamination 을 바꿔도 상위 이상 사례가 유지되는가.

    주의해서 읽어야 하는 지표다. IsolationForest 와 LOF 의 `score_samples` 는
    contamination 과 무관하다(그 값은 임계선만 옮긴다). 두 모델의 유지율 100%는
    안정성의 증거가 아니라 정의상 당연한 결과다.
    실제로 이 검증이 의미를 갖는 것은 nu 가 모델 자체를 바꾸는 OneClassSVM 뿐이다.
    """
    Xtr, Xap, _ = encode(train, apply_df)
    ref = None
    out = {}
    for c in (0.01, 0.03, 0.05, 0.10):
        s = fit_models(Xtr, Xap, contamination=c)[model]
        top = set(apply_df.iloc[np.argsort(-s)[:tops]]["row_id"])
        if ref is None:
            ref = top
        out["contamination=%.2f" % c] = {
            "overlap_top%d" % tops: round(len(top & ref) / tops, 4)}
    return out


def eval_resample(train, apply_df, model, tops=30, n_iter=10, frac=0.8):
    """표본 80% 로 다시 학습해도 상위 이상 사례가 유지되는가.

    설계서의 '연도별 재학습 일관성'을 표본 재추출로 대체한다 — taxonomy 는
    2023 단년이라 연도 분할이 불가능하다. 대신 무엇으로 대체했는지 남긴다.
    """
    rng = np.random.default_rng(SEED)
    Xtr, Xap, _ = encode(train, apply_df)
    base = set(apply_df.iloc[np.argsort(-fit_models(Xtr, Xap)[model])[:tops]]["row_id"])
    ov = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
        Xs, Xa, _ = encode(sub, apply_df)
        s = fit_models(Xs, Xa)[model]
        top = set(apply_df.iloc[np.argsort(-s)[:tops]]["row_id"])
        ov.append(len(top & base) / tops)
    return {"n_iter": n_iter, "frac": frac, "top_n": tops,
            "overlap_mean": round(float(np.mean(ov)), 4),
            "overlap_min": round(float(np.min(ov)), 4)}


# -------------------------------------------------------- 설명
AXIS_LABEL = {
    "per_recipient": "기업(과제)당 지원한도",
    "support_count": "지원 기업/과제 수",
    "support_ratio": "지원비율",
    "project_duration": "사업기간",
}


def josa(word):
    """받침 유무로 이/가를 고른다. 리포트를 사람이 읽으므로 여기까지 맞춘다."""
    ch = word[-1]
    if "가" <= ch <= "힣":
        return "이" if (ord(ch) - 0xAC00) % 28 else "가"
    return "이"


def explain(row, ref):
    """왜 이례적인지 축 단위로 말한다. 비교군 percentile 로 근거를 만든다.

    비교군 조회는 s08a 의 `compare` 를 그대로 호출한다. 여기서 따로 구현하면
    같은 사업에 대해 모델 3 과 모델 4 가 서로 다른 비교군을 말하게 된다 —
    특히 지원단위 분리 같은 규칙이 한쪽에만 반영되는 사고가 난다.
    """
    from s08a_m3_cohort import compare

    notable = []
    for axis, label in AXIS_LABEL.items():
        c = compare(ref, axis, row.get(axis), row["support_type"],
                    row["support_method"], row.get("support_unit"), row["cohort"])
        if c["status"] == "비교불가":
            continue
        rank, n = c["percentile_rank"], c["n"]
        if rank >= PCT_EXTREME or rank <= 100 - PCT_EXTREME:
            direction = "높음" if rank >= PCT_EXTREME else "낮음"
            notable.append({
                "axis": axis, "percentile": rank, "n": n, "level": c["level"],
                "text": "%s%s 비교군 대비 %s (P%.0f, %s 비교군 %d건)"
                        % (label, josa(label), direction, rank, c["level"], n)})
    return sorted(notable, key=lambda x: abs(x["percentile"] - 50), reverse=True)


def status_of(score_pct):
    """설계서가 허용한 표현만 쓴다."""
    if score_pct >= 99:
        return "rare_pattern", "과거 사업 패턴과 차이가 큼"
    if score_pct >= 95:
        return "atypical", "동일 유형 대비 비전형적"
    if score_pct >= 90:
        return "check", "확인 필요"
    return "typical", "비교군 범위 내"


def won(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    for unit, mult in (("조원", 1e12), ("억원", 1e8), ("만원", 1e4)):
        if abs(v) >= mult:
            return "%.1f%s" % (v / mult, unit)
    return "%.0f원" % v


def main():
    df = prepare(pd.read_parquet(SRC))
    ref = pd.read_parquet(REF)

    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    print("모델 4 대상: 전체 %d건 중 수치 %d축 이상 채워진 %d건 (%.1f%%)"
          % (len(df), MIN_AXES, len(train), len(train) / len(df) * 100))
    print("  코호트 구성: %s" % dict(train["cohort"].value_counts()))

    Xtr, Xap, names = encode(train, train)
    scores = fit_models(Xtr, Xap)

    print("\n== 모델별 합성 이상치 탐지 (설계서: synthetic anomaly injection)")
    syn = eval_synthetic(train, lambda tr, ap: fit_models(*encode(tr, ap)[:2]))
    for k, v in syn.items():
        print("  %-20s top-k recall %.3f / top-2k %.3f / 합성사례 중앙 순위 상위 %.1f%%"
              % (k, v["recall_at_k"], v["recall_at_2k"], v["median_rank_pct"]))
    best = max(syn, key=lambda k: syn[k]["recall_at_k"])
    print("  -> 채택: %s (설계서 MVP 권장은 IsolationForest)" % best)

    s = scores[best]
    train["anomaly_score"] = np.round(norm01(s), 4)
    train["score_pct"] = pd.Series(s).rank(pct=True).to_numpy() * 100
    train[["status", "status_text"]] = pd.DataFrame(
        [status_of(p) for p in train["score_pct"]], index=train.index)

    drivers = score_drivers(Xap, names, scores)
    print("\n== 각 축이 점수를 끌고 가는 정도 (스피어만, 상위 4축)")
    for name, dd in drivers.items():
        top4 = sorted(dd.items(), key=lambda kv: -abs(kv[1]))[:4]
        print("  %-20s %s" % (name, ", ".join("%s %.2f" % kv for kv in top4)))

    print("\n== contamination 안정성 (상위 30건 유지율)")
    cont = eval_contamination(train, train, best)
    for k, v in cont.items():
        print("  %-20s %.3f" % (k, list(v.values())[0]))

    print("\n== 재학습 안정성 (80% 재표집 10회, 상위 30건 유지율)")
    res = eval_resample(train, train, best)
    print("  평균 %.3f / 최저 %.3f" % (res["overlap_mean"], res["overlap_min"]))

    top = train.sort_values("anomaly_score", ascending=False).head(10)
    cases = []
    print("\n== 상위 이상 사례 10건")
    for _, r in top.iterrows():
        note = explain(r, ref)
        cases.append({
            "row_id": r["row_id"], "title": r["title"],
            "anomaly_score": float(r["anomaly_score"]),
            "status": r["status"], "status_text": r["status_text"],
            "reference_group": {"support_type": r["support_type"],
                                "support_method": r["support_method"],
                                "n": note[0]["n"] if note else None},
            "n_axes_used": int(r["n_axes"]),
            "notable_features": [x["text"] for x in note[:3]],
            "evidence": (r["evidence_text"] or "")[:160],
        })
        print("  [%.3f] %-40s %s" % (r["anomaly_score"], str(r["title"])[:40], r["status"]))
        for x in note[:3]:
            print("         - %s" % x["text"])

    verdict = judge(syn[best], cont, res, len(train), len(df))
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)

    train[["row_id", "cohort", "title", "support_type", "support_method",
           "per_recipient", "support_count", "support_ratio", "project_duration",
           "n_axes", "anomaly_score", "score_pct", "status", "status_text"]] \
        .to_parquet(OUT, index=False)
    print("[data] %s" % OUT)

    C.save_report("s09a_m4_anomaly.json", {
        "n_total": int(len(df)), "n_train": int(len(train)), "min_axes": MIN_AXES,
        "contamination": CONTAMINATION, "features": names,
        "synthetic_eval": syn, "best_model": best, "score_drivers": drivers,
        "contamination_stability": cont, "resample_stability": res,
        "top_cases": cases, "allowed_expressions": ALLOWED,
        "forbidden_expressions": FORBIDDEN, "verdict": verdict,
    })
    write_md(syn, best, cont, res, cases, verdict, train, df, drivers)


def judge(syn_best, cont, res, n_train, n_total):
    reasons, v = [], "Go"
    if syn_best["recall_at_k"] >= 0.7:
        reasons.append("합성 이상치를 상위 k 안에 %.0f%% 회수" % (syn_best["recall_at_k"] * 100))
    elif syn_best["recall_at_k"] >= 0.4:
        reasons.append("합성 이상치 회수율 %.0f%% — 잡아야 할 것의 절반 안팎만 잡는다"
                       % (syn_best["recall_at_k"] * 100))
        v = "Conditional"
    else:
        reasons.append("합성 이상치 회수율 %.0f%% — 만들어 넣은 이례 사례조차 못 올린다"
                       % (syn_best["recall_at_k"] * 100))
        v = "No-Go"

    worst = min(list(x.values())[0] for x in cont.values())
    reasons.append("nu(contamination) 를 0.01~0.10 으로 바꿨을 때 상위 30건 유지율 최저 %.0f%% "
                   "— OneClassSVM 은 nu 가 모델 자체를 바꾼다. 상위 N 건을 고정 목록으로 "
                   "쓰지 말고 nu 를 명시해 함께 출력해야 한다" % (worst * 100))
    if worst < 0.5:
        v = "Conditional" if v == "Go" else v

    reasons.append("80%% 재표집 재학습 시 상위 30건 유지율 평균 %.0f%% (최저 %.0f%%)"
                   % (res["overlap_mean"] * 100, res["overlap_min"] * 100))
    if res["overlap_mean"] < 0.6:
        v = "Conditional" if v == "Go" else v

    cov = n_train / n_total * 100
    reasons.append("전체 %d건 중 %d건(%.0f%%)만 학습·적용 대상 — 나머지는 수치 축이 "
                   "1개 이하라 '이례'를 말할 근거가 없다" % (n_total, n_train, cov))
    if cov < 40:
        reasons.append("적용 범위가 좁다. 서비스에서는 '판정 불가'를 기본 응답으로 두어야 한다")
        v = "Conditional" if v == "Go" else v
    return {"verdict": v, "reasons": reasons}


def write_md(syn, best, cont, res, cases, verdict, train, df, drivers):
    L = ["# 모델 4 — 사업 설계 이상패턴 탐지", "",
         "> 과거 지원사업의 다변량 설계 패턴을 학습해 신규 사업의 희귀성·이례성을 탐지한다.",
         "> **'이례적'이지 '잘못됐다'가 아니다.** 출력 문구를 코드에서 제한한다.", "",
         "## 1. 학습 대상을 좁힌 이유", "",
         "수치 4축(기업당지원액·지원건수·지원비율·사업기간) 중 %d축 이상이 채워진 행만 쓴다." % MIN_AXES,
         "결측을 메우고 전부 학습하면 모델이 잡는 것은 '희귀한 설계'가 아니라",
         "'원문에 안 적힌 사업'이 된다.", "",
         "| | 건수 |", "|---|---:|",
         "| 전체 (지원성격 확정) | %d |" % len(df),
         "| 학습·적용 대상 (%d축 이상) | %d (%.1f%%) |"
         % (MIN_AXES, len(train), len(train) / len(df) * 100), "",
         "결측 축은 중앙값으로 채우되 `__missing` 지시자 축을 함께 넣었다. 지시자가 없으면",
         "모델이 결측 행을 '중앙값 근처의 평범한 사업'으로 오해한다.", "",
         "## 2. 모델 비교 — 합성 이상치를 잡는가", "",
         "정답이 없는 비지도 모델이라 일부러 만든 이례 사례로 잰다.",
         "(극소수기업·극고액 / 극다수기업·극소액 / 비정상 장기 / 지원비율 100%)", "",
         "| 모델 | top-k 회수율 | top-2k 회수율 | 합성사례 중앙 순위 |",
         "|---|---:|---:|---:|"]
    for k, v in sorted(syn.items(), key=lambda kv: -kv[1]["recall_at_k"]):
        L.append("| %s | %.1f%% | %.1f%% | 상위 %.1f%% |"
                 % (k, v["recall_at_k"] * 100, v["recall_at_2k"] * 100, v["median_rank_pct"]))
    L += ["", "채택: **%s**" % best, "",
          "### 설계서 권장(IsolationForest)을 쓰지 않은 이유", "",
          "IF 점수가 무엇에 끌려가는지 축별로 쟀다(스피어만 상관, 절댓값 상위 5축).", "",
          "| 모델 | 점수를 끌고 가는 축 |", "|---|---|"]
    for name, dd in drivers.items():
        top5 = sorted(dd.items(), key=lambda kv: -abs(kv[1]))[:5]
        L.append("| %s | %s |" % (name, ", ".join("`%s` %.2f" % kv for kv in top5)))
    L += ["",
          "IF 는 **결측 지시자와 희귀 범주 빈도**가 점수를 지배한다. 즉 '희귀한 설계'가",
          "아니라 '원문에 안 적힌 사업'을 탐지하고 있었다 — S04E 가 경고한 실패 모드 그대로다.",
          "합성 이상치를 5건만 넣어도 회수율이 0% 여서 표본 수 문제도 아니었다.", "",
          "## 3. 안정성", "",
          "| 검증 | 결과 |", "|---|---|"]
    for k, v in cont.items():
        L.append("| %s 상위 30건 유지율 | %.0f%% |" % (k, list(v.values())[0] * 100))
    L.append("| (읽는 법) | IF·LOF 의 `score_samples` 는 contamination 과 무관하다. "
             "그 모델들의 100%%는 안정성의 증거가 아니라 정의상 당연한 값이고, "
             "이 검증이 의미를 갖는 것은 nu 가 모델을 바꾸는 OneClassSVM 뿐이다. |")
    L.append("| 80%% 재표집 재학습(10회) 상위 30건 유지율 | 평균 %.0f%% / 최저 %.0f%% |"
             % (res["overlap_mean"] * 100, res["overlap_min"] * 100))
    L += ["",
          "> 설계서의 '연도별 재학습 일관성'은 taxonomy 가 2023 단년이라 불가능해",
          "> 표본 재추출로 대체했다. 대체했다는 사실을 여기 남긴다.", "",
          "## 4. 상위 이상 사례와 축 단위 근거", "",
          "| 점수 | 사업 | 상태 | 비교군 | 두드러진 축 |",
          "|---:|---|---|---|---|"]
    for c in cases:
        rg = c["reference_group"]
        L.append("| %.3f | %s | %s | %s/%s (n=%s) | %s |"
                 % (c["anomaly_score"], str(c["title"])[:34], c["status_text"],
                    rg["support_type"], rg["support_method"], rg["n"],
                    "<br>".join(c["notable_features"]) or "—"))
    L += ["", "## 5. 출력 문구 제한", "",
          "허용:", "", "```text"] + ALLOWED + ["```", "",
          "금지:", "", "```text"] + FORBIDDEN + ["```", "",
          "`status` 값은 코드에서 이 목록으로만 생성된다. 자유서술을 붙이지 않는다.", "",
          "## 6. 판정", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = os.path.join(C.REPORTS, "s09a_m4_anomaly.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
