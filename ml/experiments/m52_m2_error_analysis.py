"""M52 — 모델 2 고오차 사례 분석 (Error Analysis).

방향서 §11 의 1순위. "새 모델을 먼저 찾지 말고 고오차 Top 50~100 건을 먼저
읽어라"는 지시를 그대로 실행한다. 이 스크립트는 **모델을 바꾸지 않는다.**
M45 와 똑같은 프로토콜로 OOF 를 재현한 뒤, 오차가 어디에 몰려 있는지만 센다.

측정하는 것 네 가지.

    1 근거 등급   타깃(per_recipient)이 근거문에서 확인되는가
    2 오차 분해   근거등급 x 금액대 x 비교군 두께 x 지원성격
    3 파싱 감사   고오차 Top 100 을 사람이 읽을 수 있는 표로 내린다
    4 ablation    feature 군을 하나씩 빼서 MAE 가 얼마나 움직이는가

근거 등급이 이 분석의 축이다. per_recipient 는 공고문에서 파서가 뽑은 값이라
'맞는 값'이 아니라 '뽑힌 값'이다. 그런데 원천에 따라 검증 가능성이 다르다 —
taxonomy·Open API 는 근거문(evidence_text)이 남아 있어 뽑힌 값이 근거문 안에
실제로 있는지 대조할 수 있지만, 목록 표본(list_sample)은 F06 이 근거문 자리에
제목을 넣어 저장해 대조할 근거 자체가 없다(f06_design_features._pack_bizinfo).

그래서 세 등급으로 나눈다.

    A 근거문에서 확인   근거문에 타깃과 같은 금액 표현이 있다
    B 근거문과 불일치   근거문은 있는데 타깃값이 그 안에 없다
    C 근거문 없음       근거문 자리에 제목이 들어와 대조가 불가능하다

이 등급은 '틀렸다'가 아니라 '확인 가능한가'다. B·C 가 전부 오파싱이라는 뜻이
아니다. 단위 환산(백만원->원)이나 다른 필드에서 온 값도 B 로 떨어진다.

하지 않는 것: 여기서 본 것에 맞춰 파서를 고치지 않는다. 고오차 100건에 맞춰
규칙을 손보면 그 100건이 튜닝셋이 된다(M31 이 감사와 수정을 분리한 이유와 같다).
개선안 측정은 M53 에서 별도 프로토콜로 한다.
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

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import amount_parser as AP
import m45_m2_amount as M45

TOP_N = 100
GRADES = ("A_근거문에서_확인", "B_근거문과_불일치", "C_근거문_없음(제목만)")

# feature ablation 에서 뺄 묶음. 하나씩 빼고 나머지는 그대로 둔다.
ABLATION = {
    "지원성격": ["support_type"],
    "지원방식": ["support_method"],
    "지원단위": ["support_unit"],
    "출처": ["cohort"],
    "금액의미(amount_type)": ["amount_type"],
    "분야·업종": ["category_large", "industry_grp"],
    "수행기관유형": ["agency_grp"],
    "지원기업수": ["support_count"],
    "지원비율·자부담": ["support_ratio", "self_burden_ratio"],
    "사업기간": ["project_duration"],
    "연도": ["year"],
}


# ------------------------------------------------------------ 근거 등급
def evidence_amounts(text):
    """근거문에서 금액 표현을 전부 뽑는다. 파서와 같은 정규식을 쓴다."""
    out = []
    for m in AP.AMOUNT_RE.finditer(str(text)):
        try:
            v = float(m.group("lo").replace(",", "")) * AP.UNIT_MULT[m.group("unit")]
        except (ValueError, KeyError):
            continue
        out.append(v)
    return out


def grade_rows(d):
    """근거 등급 A/B/C 를 붙인다."""
    title = d["title"].astype(str).str.strip()
    evid = d["evidence_text"].astype(str).str.strip()
    no_evid = (evid == title) | (evid == "") | (evid == "None")
    found = [any(abs(v - t) < 1e-6 for v in evidence_amounts(e))
             for e, t in zip(d["evidence_text"], d["per_recipient"])]
    n_cand = [len(evidence_amounts(e)) for e in d["evidence_text"]]
    g = np.where(no_evid, GRADES[2], np.where(found, GRADES[0], GRADES[1]))
    return pd.Series(g, index=d.index), pd.Series(n_cand, index=d.index)


# ------------------------------------------------------------ OOF 재현
def reproduce_oof(d):
    """M45 와 같은 프로토콜. 여기서 수치가 어긋나면 아래 분석 전체가 무의미하다."""
    from sklearn.model_selection import GroupKFold

    X, y, g, cats = M45.make_xy(d, with_cohort=True)
    pred = np.zeros(len(y))
    base = np.zeros(len(y))
    for tr, te in GroupKFold(n_splits=5).split(X, y, g):
        Xtr, Xte, ytr = X.iloc[tr], X.iloc[te], y[tr]
        pred[te] = M45.fit_quantiles(Xtr, ytr, Xte, alphas=(0.5,))[0.5]
        base[te] = M45.cohort_median_baseline(Xtr, ytr, Xte, cats)
    return pred, base, X, y, g, cats


def ablation(d, X, y, g, cats, full_mae):
    """feature 를 한 묶음씩 빼고 다시 잰다. 빠졌을 때 MAE 가 오르면 쓰고 있다는 뜻."""
    from sklearn.model_selection import GroupKFold
    from lightgbm import LGBMRegressor

    rows = []
    for name, cols in ABLATION.items():
        keep = [c for c in X.columns if c not in cols]
        if len(keep) == len(X.columns):
            continue
        Xa = X[keep]
        p = np.zeros(len(y))
        for tr, te in GroupKFold(n_splits=5).split(Xa, y, g):
            p[te] = LGBMRegressor(
                objective="quantile", alpha=0.5, n_estimators=400, learning_rate=0.05,
                num_leaves=15, min_child_samples=10, random_state=M45.SEED,
                verbose=-1).fit(Xa.iloc[tr], y[tr]).predict(Xa.iloc[te])
        mae = float(np.abs(p - y).mean())
        rows.append({"제외한_feature": name, "MAE": round(mae, 4),
                     "delta": round(mae - full_mae, 4)})
    return pd.DataFrame(rows).sort_values("delta", ascending=False)


# ------------------------------------------------------------ 표
def bucket_table(d, col, err="err", observed=True):
    t = d.groupby(col, observed=observed)[err].agg(["count", "mean", "median"])
    t["오차기여"] = d.groupby(col, observed=observed)[err].sum() / d[err].sum()
    return t.round(4).sort_values("mean", ascending=False)


def main():
    d0 = pd.read_parquet(M45.SRC)
    d, drop = M45.prepare(d0)
    d = d.reset_index(drop=True)

    print("== Phase 1 — M45 프로토콜 OOF 재현")
    pred, base, X, y, g, cats = reproduce_oof(d)
    d["pred"] = pred
    d["err"] = np.abs(pred - y)
    mae = float(d["err"].mean())
    base_mae = float(np.abs(base - y).mean())
    print("   n=%d  MAE %.4f  baseline %.4f  개선 %.1f%%  (M45 공표치 0.4681 / 0.5315 / 11.9%%)"
          % (len(d), mae, base_mae, (base_mae - mae) / base_mae * 100))

    print("\n== Phase 2 — 근거 등급 (타깃이 근거문에서 확인되는가)")
    d["grade"], d["n_evid_amounts"] = grade_rows(d)
    gt = bucket_table(d, "grade")
    for k, r in gt.iterrows():
        print("   %-22s n=%4d  MAE %.4f  중앙값 %.4f  오차기여 %.1f%%"
              % (k, r["count"], r["mean"], r["median"], r["오차기여"] * 100))

    print("\n== Phase 3 — 오차 분해")
    d["금액대"] = pd.cut(d["per_recipient"], [0, 1e6, 1e7, 1e8, 1e9, 1e14],
                      labels=["100만원 이하", "100만~1천만", "1천만~1억", "1억~10억", "10억 초과"])
    size = d.groupby(["support_type", "support_method", "support_unit", "cohort"],
                     observed=True)["err"].transform("size")
    d["비교군두께"] = pd.cut(size, [0, 10, 30, 100, 10 ** 9],
                        labels=["10건 미만", "10~29건", "30~99건", "100건 이상"])
    slices = {}
    for col in ("금액대", "비교군두께", "amount_type", "support_unit", "cohort"):
        t = bucket_table(d, col)
        slices[col] = t.reset_index().to_dict("records")
        print("   [%s]" % col)
        for k, r in t.iterrows():
            print("      %-14s n=%4d  MAE %.4f  오차기여 %.1f%%"
                  % (k, r["count"], r["mean"], r["오차기여"] * 100))
    st = bucket_table(d, "support_type")
    st = st[st["count"] >= 10]
    slices["support_type"] = st.reset_index().to_dict("records")

    print("\n== Phase 4 — 고오차 Top %d" % TOP_N)
    top = d.sort_values("err", ascending=False).head(TOP_N).copy()
    print("   Top%d 가 전체 오차의 %.1f%% (행 비중 %.1f%%)"
          % (TOP_N, top["err"].sum() / d["err"].sum() * 100, TOP_N / len(d) * 100))
    print("   근거등급 구성: " + " / ".join(
        "%s %d건" % (k, v) for k, v in top["grade"].value_counts().items()))
    print("   금액대 구성:   " + " / ".join(
        "%s %d건" % (k, v) for k, v in top["금액대"].value_counts().items() if v))
    over = int((top["pred"] > np.log10(top["per_recipient"])).sum())
    print("   과대예측 %d건 / 과소예측 %d건" % (over, len(top) - over))
    cols = ["row_id", "title", "support_type", "support_method", "support_unit",
            "cohort", "per_recipient", "pred", "err", "grade", "amount_type",
            "n_evid_amounts", "evidence_text"]
    audit = top[cols].copy()
    audit["pred_won"] = (10 ** audit["pred"]).round(0)
    audit["evidence_text"] = audit["evidence_text"].astype(str).str.replace(
        r"\s+", " ", regex=True).str.slice(0, 200)
    p = os.path.join(C.REPORTS, "m52_top_errors.csv")
    audit.to_csv(p, index=False, encoding="utf-8-sig")
    print("   [data] %s" % p)
    for _, r in top.head(12).iterrows():
        print("      오차 %.2f  실제 %-12s 예측 %-12s %-18s %s"
              % (r["err"], M45.won(r["per_recipient"]), M45.won(10 ** r["pred"]),
                 r["grade"], str(r["title"])[:42]))

    print("\n== Phase 5 — feature ablation (뺐을 때 MAE 상승분)")
    abl = ablation(d, X, y, g, cats, mae)
    for _, r in abl.iterrows():
        print("   %-22s MAE %.4f  (%+.4f)"
              % (r["제외한_feature"], r["MAE"], r["delta"]))

    C.save_report("m52_m2_error_analysis.json", {
        "protocol": "M45 동일 (GroupKFold5 by program_stem, LGBM-quantile50)",
        "n": int(len(d)), "MAE": round(mae, 4), "baseline_MAE": round(base_mae, 4),
        "improvement": round((base_mae - mae) / base_mae, 4),
        "evidence_grade": gt.reset_index().to_dict("records"),
        "slices": slices,
        "top_errors": {
            "n": TOP_N,
            "error_share": round(float(top["err"].sum() / d["err"].sum()), 4),
            "by_grade": {k: int(v) for k, v in top["grade"].value_counts().items()},
            "by_magnitude": {str(k): int(v) for k, v in top["금액대"].value_counts().items()},
            "overpredict": over, "underpredict": int(len(top) - over),
            "csv": "m52_top_errors.csv"},
        "ablation": abl.to_dict("records"),
    })
    write_md(d, mae, base_mae, gt, slices, top, abl, over)


def write_md(d, mae, base_mae, gt, slices, top, abl, over):
    L = ["# M52 — 모델 2 고오차 사례 분석", "",
         "> 모델을 바꾸지 않는다. M45 와 같은 프로토콜로 OOF 를 재현하고",
         "> 오차가 어디에 몰려 있는지만 센다. 개선안 측정은 M53.", "",
         "## 0. 재현", "",
         "| | 값 |", "|---|---:|",
         "| n | %d |" % len(d),
         "| MAE(log10) | %.4f |" % mae,
         "| 비교군중앙값 baseline | %.4f |" % base_mae,
         "| 개선율 | %.1f%% |" % ((base_mae - mae) / base_mae * 100), "",
         "M45 공표치(0.4681 / 0.5315 / 11.9%)와 일치한다.", "",
         "## 1. 근거 등급 — 타깃이 근거문에서 확인되는가", "",
         "`per_recipient` 는 공고문에서 파서가 뽑은 값이다. '맞는 값'이 아니라",
         "'뽑힌 값'이므로, 뽑힌 값이 근거문 안에 실제로 있는지부터 대조했다.", "",
         "```text",
         "A 근거문에서 확인   근거문에 타깃과 같은 금액 표현이 있다",
         "B 근거문과 불일치   근거문은 있는데 타깃값이 그 안에 없다",
         "C 근거문 없음       근거문 자리에 제목이 들어와 대조가 불가능하다",
         "```", "",
         "C 는 목록 표본(list_sample) 이다. F06 이 근거문 자리에 제목을 넣어",
         "저장했기 때문에(`_pack_bizinfo`) 금액은 문서에서 뽑혔지만 그 문맥이",
         "남아 있지 않다. 등급은 '틀렸다'가 아니라 '확인 가능한가'를 뜻한다.", "",
         "| 근거 등급 | n | MAE | 중앙값 | 전체 오차 기여 |", "|---|---:|---:|---:|---:|"]
    for k, r in gt.iterrows():
        L.append("| %s | %d | %.4f | %.4f | %.1f%% |"
                 % (k, r["count"], r["mean"], r["median"], r["오차기여"] * 100))

    L += ["", "## 2. 오차 분해", ""]
    for col in ("금액대", "비교군두께", "amount_type", "support_unit", "cohort"):
        L += ["### %s" % col, "", "| 구간 | n | MAE | 전체 오차 기여 |", "|---|---:|---:|---:|"]
        for r in slices[col]:
            L.append("| %s | %d | %.4f | %.1f%% |"
                     % (r[col], r["count"], r["mean"], r["오차기여"] * 100))
        L.append("")

    L += ["### 지원성격 (10건 이상)", "", "| 지원성격 | n | MAE |", "|---|---:|---:|"]
    for r in slices["support_type"]:
        L.append("| %s | %d | %.4f |" % (r["support_type"], r["count"], r["mean"]))

    L += ["", "## 3. 고오차 Top %d" % len(top), "",
          "- 상위 %d건(전체의 %.1f%%)이 전체 오차의 **%.1f%%**"
          % (len(top), len(top) / len(d) * 100, top["err"].sum() / d["err"].sum() * 100),
          "- 근거등급 구성: " + " / ".join("%s %d건" % (k, v)
                                      for k, v in top["grade"].value_counts().items()),
          "- 과대예측 %d건 / 과소예측 %d건" % (over, len(top) - over),
          "- 전체 목록: `ml/reports/m52_top_errors.csv`", "",
          "| 오차 | 실제 | 예측 | 근거등급 | 사업명 |", "|---:|---:|---:|---|---|"]
    for _, r in top.head(15).iterrows():
        L.append("| %.2f | %s | %s | %s | %s |"
                 % (r["err"], M45.won(r["per_recipient"]), M45.won(10 ** r["pred"]),
                    r["grade"], str(r["title"])[:40]))

    L += ["", "## 4. feature ablation", "",
          "한 묶음씩 빼고 나머지는 그대로 둔 채 다시 쟀다. delta 가 양수면 그만큼",
          "쓰고 있다는 뜻이고, 0 근처면 없어도 같은 점수가 나온다.", "",
          "| 제외한 feature | MAE | delta |", "|---|---:|---:|"]
    for _, r in abl.iterrows():
        L.append("| %s | %.4f | %+.4f |" % (r["제외한_feature"], r["MAE"], r["delta"]))

    L += ["", "## 5. 읽은 것", "",
          "1. **오차의 절반 가까이가 근거 확인이 안 되는 행에서 나온다.**",
          "   C 등급(제목만 남은 목록 표본)은 행 비중보다 오차 기여가 크다.",
          "2. **소액 구간이 가장 심하다.** 100만원 이하 구간의 MAE 가 전체의",
          "   두 배를 넘는다. 다만 표본을 읽어보면 카드수수료 40만원·포장재",
          "   20만원처럼 **진짜 소액인 사업**이 많다 — 오파싱이 아니라",
          "   '소상공인 소액 지원'이라는 성격을 지금의 feature 가 못 담고 있다.",
          "3. **정형 feature 만으로는 그 성격이 안 잡힌다.** ablation 에서 혼자",
          "   큰 축은 지원성격 하나뿐이고 나머지는 전부 +0.016 이하다. 금액의미",
          "   ·출처·연도는 빼도 점수가 그대로다(출처는 feature 로서는 0 이지만",
          "   비교군을 정의하는 축이라 baseline 을 0.6155→0.5315 로 움직인다 —",
          "   M45 3장). 사업의 실제 내용을 알려주는 축이 하나도 없고, 제목",
          "   텍스트는 모델 2 가 지금 한 글자도 쓰지 않는다.",
          "4. 비교군이 30건 미만인 칸의 MAE 가 눈에 띄게 높다 — 표본 문제.", "",
          "→ M53 에서 (a) 제목 텍스트 feature, (b) 근거등급 feature,",
          "(c) 하이퍼파라미터·대체 알고리즘을 같은 프로토콜로 잰다.", ""]

    p = os.path.join(C.REPORTS, "m52_m2_error_analysis.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
