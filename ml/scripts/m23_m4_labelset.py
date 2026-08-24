"""M23 — 모델 4 실제 검증 준비: 라벨링 세트 생성 + 운영 정책 확정.

최종정리 문서 4.5절이 "반드시 남은 검증"으로 지목한 것.

    실제 사업 30~50건을 사람이 직접 검토해 정상 / 비전형 라벨을 만든다.
    threshold tuning 에 쓰지 않고 최종 hold-out 으로만 쓴다.

라벨링 자체는 사람 몫이라 여기서 못 한다. 대신 **사람이 바로 채울 수 있는
세트를 만든다.** 그냥 상위 50건을 주면 안 된다 — 전부 경고된 사업만 보게 되어
"놓친 것"을 잴 수 없고, 라벨러가 모델 순위를 알면 판단이 끌려간다.

세트 설계
    층화 추출   경고 구간 / 경계 구간 / 정상 구간에서 나눠 뽑는다
                경고만 주면 recall 은 재도 precision 밖에 못 잰다
    순서 섞기   점수 순서를 감추고 무작위 순서로 낸다
    점수 숨김   anomaly_score 는 정답지(_key)에만 두고 라벨링 시트엔 안 넣는다
    근거 동봉   비교군 percentile 을 붙여 라벨러가 맨눈으로 판단할 재료를 준다

운영 정책 (문서 6절 모델 4의 4번)
    M20 이 경고율 2% 를 권했다. 그 값을 운영 규칙 형태로 확정하고,
    비교군 표본이 얇아 percentile 을 못 내는 경우의 기본 응답까지 적는다.
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import AXIS_LABEL, MIN_AXES, REF, SRC, explain, prepare
from m16_m4_tuning import EXPERIMENTS, encode

OUT_SHEET = os.path.join(C.REPORTS, "m23_labelset.csv")
OUT_KEY = os.path.join(C.REPORTS, "m23_labelset_key.csv")
SEED = 42
N_TOTAL = 50
# 층화 구간 — 경고 / 경계 / 정상. 경고만 주면 놓친 것을 못 잰다.
STRATA = [("경고구간", 98.0, 100.0, 20),
          ("경계구간", 90.0, 98.0, 15),
          ("정상구간", 0.0, 90.0, 15)]


def build_sheet(train, scores, ref):
    rng = np.random.default_rng(SEED)
    t = train.assign(anomaly_score=scores,
                     score_pct=pd.Series(scores).rank(pct=True).to_numpy() * 100)
    rows = []
    for name, lo, hi, k in STRATA:
        pool = t[(t["score_pct"] >= lo) & (t["score_pct"] < hi if hi < 100
                                           else t["score_pct"] <= hi)]
        if not len(pool):
            continue
        take = pool.sample(min(k, len(pool)), random_state=SEED)
        for _, r in take.iterrows():
            note = explain(r, ref)
            rows.append({
                "row_id": r["row_id"],
                "사업명": str(r["title"])[:60],
                "지원성격": r["support_type"],
                "지원방식": r["support_method"],
                "기업당지원한도": r.get("per_recipient"),
                "지원기업수": r.get("support_count"),
                "지원비율": r.get("support_ratio"),
                "사업기간": r.get("project_duration"),
                "비교군_근거": " / ".join(x["text"] for x in note[:3]) or "비교군 부족",
                "__stratum": name,
                "__score": round(float(r["anomaly_score"]), 4),
                "__pct": round(float(r["score_pct"]), 2),
            })
    df = pd.DataFrame(rows)
    # 라벨러가 순위를 눈치채지 못하도록 섞는다
    return df.sample(frac=1.0, random_state=int(rng.integers(1e6))).reset_index(drop=True)


def operating_policy(m20):
    """문서 6절 모델 4의 4번 — 최종 threshold 운영 정책."""
    rec = m20.get("recommended") or {}
    sweep = {int(r["alert_rate_target"] * 100): r for r in m20["sweep"]}
    return {
        "alert_budget": rec.get("alert_rate_target"),
        "rule": [
            "전체 사업의 상위 %.0f%% 를 '확인 필요'로 표시한다."
            % ((rec.get("alert_rate_target") or 0.02) * 100),
            "표시는 경고가 아니라 검토 대상 지정이다. 문구는 '과거 사업 패턴과 "
            "차이가 큼 / 확인 필요' 로 고정한다.",
            "수치 축이 2개 미만인 사업은 점수를 내지 않고 '판정 불가'로 둔다. "
            "근거 없이 정상이라고 말하지 않는다.",
            "비교군이 30건 미만이면 percentile 근거를 붙이지 않고 '비교군 부족'을 "
            "함께 표시한다.",
            "경고 예산을 바꾸려면 아래 표의 recall/precision 교환비를 근거로 "
            "명시하고 바꾼다.",
        ],
        "tradeoff": {str(k): {"recall": v["recall"], "precision": v["precision"],
                              "false_alerts": v["fp_mean"]}
                     for k, v in sweep.items()},
        "why_this_budget": (
            "recall 75%% 하한을 지키는 가장 작은 값이다. 3%% 로 올리면 recall 이 "
            "%.1f%% 까지 오르지만 헛경고가 %.0f건 생긴다."
            % (sweep.get(3, {}).get("recall", 0) * 100,
               sweep.get(3, {}).get("fp_mean", 0))),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=N_TOTAL)
    a = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    ref = pd.read_parquet(REF)
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    with open(os.path.join(C.REPORTS, "m16_m4_tuning.json"), encoding="utf-8") as f:
        m16 = json.load(f)
    with open(os.path.join(C.REPORTS, "m20_m4_threshold.json"), encoding="utf-8") as f:
        m20 = json.load(f)

    ch = m16["chosen"]
    gamma = ch["gamma"] if ch["gamma"] == "scale" else float(ch["gamma"])
    feats = EXPERIMENTS[ch["features"]]
    from sklearn.svm import OneClassSVM
    Xtr, Xap, _ = encode(train, train, feats["num"], feats["cat"], ch["scaler"])
    s = -OneClassSVM(kernel="rbf", gamma=gamma, nu=float(ch["nu"])).fit(Xtr) \
        .score_samples(Xap)

    print("모델 4 실제 검증 준비: 적용 대상 %d행" % len(train))
    t0 = time.time()

    sheet = build_sheet(train, s, ref)
    key = sheet[["row_id", "__stratum", "__score", "__pct"]].copy()
    blank = sheet.drop(columns=["__stratum", "__score", "__pct"]).copy()
    blank.insert(len(blank.columns), "판단(정상/비전형)", "")
    blank.insert(len(blank.columns), "판단이유", "")
    blank.to_csv(OUT_SHEET, index=False, encoding="utf-8-sig")
    key.to_csv(OUT_KEY, index=False, encoding="utf-8-sig")

    print("\n== 라벨링 세트 %d건" % len(sheet))
    print("   층화 구성: %s" % dict(sheet["__stratum"].value_counts()))
    print("   [sheet] %s   <- 사람이 채울 시트 (점수 없음)" % OUT_SHEET)
    print("   [key]   %s   <- 정답 대조용 (점수·구간)" % OUT_KEY)
    print("\n   미리보기")
    for _, r in sheet.head(4).iterrows():
        print("     %s | %s / %s" % (str(r["사업명"])[:34], r["지원성격"], r["지원방식"]))
        print("        %s" % r["비교군_근거"][:88])

    pol = operating_policy(m20)
    print("\n== 운영 정책 (경고 예산 %.0f%%)" % ((pol["alert_budget"] or 0) * 100))
    for i, r in enumerate(pol["rule"], 1):
        print("   %d. %s" % (i, r))

    verdict = {
        "verdict": "준비 완료 — 라벨링 대기",
        "reasons": [
            "층화 %d건 세트 생성 (경고 %d / 경계 %d / 정상 %d)"
            % (len(sheet), *[int((sheet["__stratum"] == n).sum())
                             for n, _, _, _ in STRATA]),
            "점수를 시트에서 뺐다 — 라벨러가 모델 순위를 알면 판단이 끌려간다",
            "경고 구간만 주지 않았다 — 그러면 precision 만 재고 recall 은 못 잰다",
            "이 세트는 threshold 튜닝에 쓰지 않는다. 최종 hold-out 전용이다",
            "라벨이 채워지면 M20 과 같은 경고율에서 recall/precision 을 다시 재면 된다",
        ]}
    print("\n== 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)
    print("  소요 %.1f분" % ((time.time() - t0) / 60))

    C.save_report("m23_m4_labelset.json", {
        "n_pool": int(len(train)), "n_sheet": int(len(sheet)),
        "strata": [{"name": n, "pct_lo": lo, "pct_hi": hi, "target": k}
                   for n, lo, hi, k in STRATA],
        "strata_actual": {k: int(v) for k, v in sheet["__stratum"].value_counts().items()},
        "sheet_path": OUT_SHEET, "key_path": OUT_KEY,
        "operating_policy": pol, "verdict": verdict,
        "runtime_min": round((time.time() - t0) / 60, 2)})
    write_md(sheet, pol, verdict)


def write_md(sheet, pol, verdict):
    L = ["# 모델 4 실제 검증 준비 — 라벨링 세트 + 운영 정책", "",
         "> 최종정리 문서 4.5절: \"실제 사업 30~50건을 사람이 직접 검토해 정상 /",
         "> 비전형 라벨을 만든다. threshold tuning 에 쓰지 않고 최종 hold-out 으로만.\"", "",
         "라벨링 자체는 사람 몫이라 여기서 못 합니다. **바로 채울 수 있는 세트를**",
         "**만들었습니다.**", "",
         "## 1. 왜 상위 50건을 그냥 주면 안 되는가", "",
         "| 문제 | 대응 |", "|---|---|",
         "| 경고된 사업만 보면 '놓친 것'을 못 잰다 (recall 측정 불가) | 경고·경계·정상 구간에서 **층화 추출** |",
         "| 라벨러가 모델 순위를 알면 판단이 끌려간다 | **점수를 시트에서 빼고** 무작위 순서로 배치 |",
         "| 맨눈으로 판단할 재료가 없다 | 비교군 percentile 근거를 함께 제공 |", "",
         "## 2. 세트 구성", "",
         "| 구간 | 점수 percentile | 건수 |", "|---|---|---:|"]
    for n, lo, hi, _ in STRATA:
        L.append("| %s | P%.0f~P%.0f | %d |"
                 % (n, lo, hi, int((sheet["__stratum"] == n).sum())))
    L += ["| **합계** | | **%d** |" % len(sheet), "",
          "```text",
          "%s          사람이 채울 시트 (점수 없음)" % os.path.basename(OUT_SHEET),
          "%s      정답 대조용 (점수·구간)" % os.path.basename(OUT_KEY),
          "```", "",
          "시트 열: 사업명 / 지원성격 / 지원방식 / 기업당지원한도 / 지원기업수 /",
          "지원비율 / 사업기간 / 비교군_근거 / **판단(정상/비전형)** / **판단이유**", "",
          "## 3. 라벨링 후 할 일", "", "```text",
          "1. 판단 열을 정상 / 비전형 으로 채운다",
          "2. m23_labelset_key.csv 와 row_id 로 붙인다",
          "3. M20 과 같은 경고율(2%)에서 recall / precision 을 다시 잰다",
          "4. 합성 이상치 기준 수치와 나란히 보고한다 — 대체가 아니라 병기다",
          "```", "",
          "> 이 세트는 **threshold 튜닝에 쓰지 않습니다.** 튜닝에 쓰면 최종 검증이",
          "> 아니라 학습 데이터가 됩니다.", "",
          "## 4. 운영 정책 (문서 6절 모델 4의 4번)", "",
          "**경고 예산 %.0f%%**" % ((pol["alert_budget"] or 0) * 100), ""]
    for i, r in enumerate(pol["rule"], 1):
        L.append("%d. %s" % (i, r))
    L += ["", "### 경고 예산을 바꿀 때의 교환비", "",
          "| 경고율 | recall | precision | 헛경고 |", "|---:|---:|---:|---:|"]
    for k, v in sorted(pol["tradeoff"].items(), key=lambda kv: int(kv[0])):
        L.append("| %s%% | %.1f%% | %.1f%% | %.0f건 |"
                 % (k, v["recall"] * 100, v["precision"] * 100, v["false_alerts"]))
    L += ["", "> %s" % pol["why_this_budget"], "",
          "## 5. 상태", "", "**%s**" % verdict["verdict"], ""]
    for r in verdict["reasons"]:
        L.append("- %s" % r)
    L.append("")
    p = os.path.join(C.REPORTS, "m23_m4_labelset.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
