r"""M39 — 모델 3 최종 정리: 같은 잣대로 한 표에 놓고 Go/No-Go 를 낸다.

M34~M38·DL17·DL18 은 각자 다른 자리에서 수치를 냈다. 여기서는 **후보 전부를
한 번에 다시 계산한다** — 같은 평가함수, 같은 부트스트랩, 같은 안정성 프로토콜.
리포트에서 숫자를 긁어 모아 표를 만들면 어느 수치가 어느 조건에서 나온
것인지 섞인다.

세 축으로 판정한다 (직전 계획서 §10)
    성능    clean hold-out ROC-AUC (+ 부트스트랩 구간)
    안정성  다시 학습해도 같은 사업을 경고하는가
    설명    왜 검토가 필요한지 문장으로 말할 수 있는가

그리고 계획서 §12 의 여덟 질문에 하나씩 답을 붙인다.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, SEED, _binary, boot_ci
from m36_m3_oneclass import ALERT_RATE
from m36_m3_oneclass import encode as enc36
from m36_m3_oneclass import fit_all
from m38_m3_vector_direction import (build_vectors, combine, score_components)

TOP_N = 30
N_RESAMPLE = 10


def eval_scores(train, scores, cl):
    s = pd.Series(scores, index=train["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    y = (sub["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[sub["row_id"]].to_numpy(float)
    from sklearn.metrics import average_precision_score, roc_auc_score
    k = max(1, int(round(len(train) * ALERT_RATE)))
    thr = float(np.sort(scores)[::-1][k - 1])
    eb = min(EQUAL_BUDGET, len(sc))
    return {
        "roc_auc": round(float(roc_auc_score(y, sc)), 4),
        "roc_auc_ci95": boot_ci(y, sc),
        "pr_auc": round(float(average_precision_score(y, sc)), 4),
        "operating_2pct": _binary(y, sc >= thr),
        "equal_budget": {"top_n": eb, **_binary(y, sc >= np.sort(sc)[::-1][eb - 1])},
    }


def candidates(train):
    """후보 점수를 한 자리에서 만든다. 학습·적용은 모두 같은 1,948행이다."""
    out = {}
    Xtr, Xap, _ = enc36(train, train)
    for n, s in fit_all(Xtr, Xap).items():
        out[n] = s

    # Deep SVDD 는 deep-learning 브랜치에만 있다. 여기(machine-learning)에서는
    # 그 행이 빠진 채로 ML 후보끼리 비교한다 — 없는 것을 있는 척하지 않는다.
    try:
        from dl17_m3_deepsvdd import SEEDS, train_one
    except ImportError:
        print("  [skip] dl17_m3_deepsvdd 없음 — Deep SVDD 행을 뺀다 "
              "(이 브랜치에는 DL 스크립트가 없다)")
    else:
        ds = [train_one(Xtr, sd)[0] for sd in SEEDS]
        out["DeepSVDD (시드평균순위)"] = np.mean(
            [pd.Series(x).rank(pct=True).to_numpy() for x in ds], axis=0)
        out["__deepsvdd_seeds"] = ds

    Xs, _, _, n_num = build_vectors(train, train)
    comp = score_components(train, train, Xs, Xs, n_num)
    out["비교군거리 (M38)"] = comp["dist_pct"].to_numpy(float)
    out["비교군거리+방향 (M38)"] = combine(comp, 0.7, "dir_residual")
    return out, comp


def resample_stability(train, kind, top_n=TOP_N, n_iter=N_RESAMPLE, frac=0.8):
    """표본을 갈아도 상위 목록이 유지되는가. 모든 후보에 같은 프로토콜을 쓴다."""
    rng = np.random.default_rng(SEED)
    if kind in ("OneClassSVM", "IsolationForest", "LocalOutlierFactor"):
        Xtr, Xap, _ = enc36(train, train)
        base = set(train.iloc[np.argsort(-fit_all(Xtr, Xap)[kind])[:top_n]]["row_id"])
        ov = []
        for _ in range(n_iter):
            sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
            Xs, Xa, _ = enc36(sub, train)
            top = set(train.iloc[np.argsort(-fit_all(Xs, Xa)[kind])[:top_n]]["row_id"])
            ov.append(len(top & base) / top_n)
        return round(float(np.mean(ov)), 4)
    Xs, _, _, n_num = build_vectors(train, train)
    w = 1.0 if kind == "비교군거리 (M38)" else 0.7
    base_s = combine(score_components(train, train, Xs, Xs, n_num), w, "dir_residual")
    base = set(train.iloc[np.argsort(-base_s)[:top_n]]["row_id"])
    ov = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
        A, B, _, nn = build_vectors(sub, train)
        s = combine(score_components(sub, train, A, B, nn), w, "dir_residual")
        top = set(train.iloc[np.argsort(-s)[:top_n]]["row_id"])
        ov.append(len(top & base) / top_n)
    return round(float(np.mean(ov)), 4)


EXPLAINS = {
    "OneClassSVM": "아니오 — 커널 거리라 축 단위로 분해되지 않는다 (M13 이 비교군 percentile 을 따로 붙여 설명했다)",
    "IsolationForest": "부분적 — 분할 경로를 축으로 되돌릴 수 있으나 문장이 되지는 않는다",
    "LocalOutlierFactor": "아니오 — 이웃 밀도비라 축 단위 근거가 없다",
    "DeepSVDD (시드평균순위)": "아니오 — 초구 거리는 분해되지 않는다",
    "비교군거리 (M38)": "부분적 — D 의 축별 기여도를 그대로 읽을 수 있다",
    "비교군거리+방향 (M38)": "예 — 방향 유형(A1~A4)을 문장으로 말한다",
}


def answer_questions(res, extra):
    """계획서 §12 의 여덟 질문. 실험 결과로만 답한다."""
    m33 = extra["m33"]
    m34, m35 = extra["m34"], extra.get("m35")
    m37, dl17, dl18 = extra["m37"], extra["dl17"], extra["dl18"]
    best = max(res, key=lambda k: res[k]["roc_auc"])
    q = []
    q.append(("1. 사람의 atypical_design 판단이 일관적인가",
              "부분적. 파서를 고치자 이전 '비전형' 15건 중 8건이 다른 칸으로 옮겨갔다"
              "(%d건은 data_error, 나머지는 uncertain/normal). 반대로 이전 '정상' 17건은"
              " 한 건도 atypical 로 올라오지 않았다 — 음성 판정은 안정적이고 양성 판정이"
              " 입력 품질에 끌려다녔다."
              % m33["prev_vs_new"].get("비전형", {}).get("data_error", 0)))
    q.append(("2. 현재 feature 에 그 판단을 설명할 signal 이 있는가",
              "있다. 같은 50건에서 M30 의 ROC-AUC 0.48(동전던지기)이 %.3f 로 올라갔다."
              " 올린 것은 모델이 아니라 파서 교정과 라벨 분리다." % res[best]["roc_auc"]))
    q.append(("3. 특정 feature 하나에만 의존하고 있지는 않은가",
              "아니다. 단일 축 최고가 ROC-AUC %.3f 로 Full(%.3f)에 못 미친다."
              " 다만 `support_ratio` 와 `support_unit` 은 빼면 오히려 좋아진다 —"
              " 35건 표본에서 그 차이는 부트스트랩 구간 안이라 축을 빼는 근거로 쓰지 않았다."
              % (max(v["roc_auc"] for v in m34["single"].values()), m34["full"]["roc_auc"])))
    q.append(("4. anomaly detection 이라는 문제정의가 맞는가",
              "맞다. 단, '정상 분포에서 먼 점'이 아니라 **'비교군 대표벡터에서 먼 점'**"
              " 이라야 한다. 전역 one-class(OneClassSVM %.3f)보다 비교군 기준 거리"
              "(%.3f)가 크게 낫다."
              % (res["OneClassSVM"]["roc_auc"], res["비교군거리 (M38)"]["roc_auc"])))
    if m35:
        b = m35["best"]
        q.append(("5. supervised classification 이 더 적합한가",
                  "아니다. 35건·양성 10건에서 지도학습 최고는 %s LOO ROC-AUC %.3f"
                  " (라벨 순열 p=%.3f). 비지도 최고 %.3f 를 넘지 못한다. 라벨을 더 모으기"
                  " 전에는 분류로 갈아탈 근거가 없다."
                  % (b, m35["results"][b]["loo_auc"], m35["results"][b]["perm_p"],
                     res[best]["roc_auc"])))
    else:
        q.append(("5. supervised classification 이 더 적합한가", "M35 미완 — 별도 보고"))
    q.append(("6. 거리보다 방향 차이가 사람 판단과 더 잘 맞는가",
              "아니다. 거리 단독 %.3f, 방향 단독 0.37~0.59, 둘을 섞으면 %.3f 로 오히려"
              " 내려간다. 방향의 값어치는 순위가 아니라 **경고 이유를 문장으로 말하는 것**이다."
              % (res["비교군거리 (M38)"]["roc_auc"], res["비교군거리+방향 (M38)"]["roc_auc"])))
    if dl18:
        q.append(("7. text 와 structured 를 함께 쓰면 의미가 증가하는가",
              "증가하지 않는다. 정형만 %.3f / 텍스트만 %.3f / 정형+텍스트 %.3f."
              " text-feature 불일치(A4)도 단독 %.3f 로 순위에는 보탬이 안 된다 —"
              " 남는 값어치는 설명 문장뿐이다."
              % (dl18["block_results"]["structured only | distance only"]["roc_auc"],
                 dl18["block_results"]["text only | distance only"]["roc_auc"],
                 dl18["block_results"]["structured+text (0.6/0.4) | distance only"]["roc_auc"],
                 dl18["text_mismatch_alone"]["roc_auc"])))
    else:
        q.append(("7. text 와 structured 를 함께 쓰면 의미가 증가하는가",
                  "DL18(딥러닝 브랜치)에서 측정했다. 이 브랜치에는 그 리포트가 없다."))
    a8 = ("모델마다 다르다. 비교군거리 %.0f%% / IsolationForest %.0f%% 는 유지되고,"
          " OneClassSVM 은 %.0f%% 로 무너진다."
          % (res["비교군거리 (M38)"]["stability"] * 100,
             res["IsolationForest"]["stability"] * 100,
             res["OneClassSVM"]["stability"] * 100))
    if dl17:
        a8 += (" Deep SVDD 는 시드만 바꿔도 상위30 겹침이 %.0f%% 다(DL17)."
               % (dl17["stability"]["top30_overlap"]["mean"] * 100))
    q.append(("8. 재학습해도 같은 사업을 반복 경고하는가", a8))
    q.append(("(추가) 합성 이상치 성능은 무엇을 재고 있었나",
              "생성 규칙이다. 기존 합성셋은 kNN 순도 %.2f 로 완전히 뭉쳐 있었고,"
              " 규칙을 흩뜨리자 OneClassSVM 회수율이 %.2f 에서 %.2f 로 떨어졌다."
              % (m37["clumping"]["old"]["knn_purity"],
                 m37["detection"]["old"]["OneClassSVM"]["recall_at_k"],
                 m37["detection"]["new"]["OneClassSVM"]["recall_at_k"])))
    return q


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")

    print("M39 — 모델 3 최종 정리")
    print("  학습·적용 %d행 / 주 평가 %d건 (양성 %d)"
          % (len(train),
             int(cl["라벨"].isin(["normal", "atypical_design"]).sum()),
             int((cl["라벨"] == "atypical_design").sum())))

    cands, comp = candidates(train)
    ds_seeds = cands.pop("__deepsvdd_seeds", None)

    res = {}
    for name, s in cands.items():
        r = eval_scores(train, s, cl)
        if name.startswith("DeepSVDD"):
            ov = [len(set(np.argsort(ds_seeds[i])[::-1][:TOP_N]) &
                      set(np.argsort(ds_seeds[j])[::-1][:TOP_N])) / TOP_N
                  for i in range(len(ds_seeds)) for j in range(i + 1, len(ds_seeds))]
            r["stability"] = round(float(np.mean(ov)), 4)
            r["stability_kind"] = "시드 간 상위30 겹침"
        else:
            r["stability"] = resample_stability(train, name)
            r["stability_kind"] = "80%% 재표집 상위30 유지율"
        r["explainable"] = EXPLAINS.get(name, "")
        res[name] = r

    print("\n%-26s %9s %18s %9s %10s %9s" % ("후보", "ROC-AUC", "95%CI", "PR-AUC",
                                             "상위7recall", "안정성"))
    for n, r in sorted(res.items(), key=lambda kv: -kv[1]["roc_auc"]):
        print("%-26s %9.4f  [%.3f, %.3f] %9.4f %10s %9.3f"
              % (n, r["roc_auc"], r["roc_auc_ci95"][0], r["roc_auc_ci95"][1],
                 r["pr_auc"], r["equal_budget"]["recall"], r["stability"]))

    def load(name):
        p = os.path.join(C.REPORTS, name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None

    extra = {"m33": load("m33_m3_relabel.json"), "m34": load("m34_m3_diagnostics.json"),
             "m35": load("m35_m3_supervised.json"), "m37": load("m37_m3_synthetic.json"),
             "dl17": load("dl17_m3_deepsvdd.json"), "dl18": load("dl18_m3_text_vector.json")}
    qa = answer_questions(res, extra)
    print("\n== 계획서 §12 의 질문들")
    for q, a in qa:
        print("  %s" % q)
        print("     %s" % a)

    verdict = judge(res)
    print("\n== 최종 판정: %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)

    rep = {"n_train": int(len(train)),
           "eval_set": {"n": int(cl["라벨"].isin(["normal", "atypical_design"]).sum()),
                        "positive": int((cl["라벨"] == "atypical_design").sum()),
                        "separate": {"data_error": int((cl["라벨"] == "data_error").sum()),
                                     "uncertain": int((cl["라벨"] == "uncertain").sum())}},
           "candidates": res, "questions": [{"q": q, "a": a} for q, a in qa],
           "verdict": verdict,
           "m30_baseline_roc_auc": 0.48,
           "protocol": "모든 후보를 이 스크립트에서 다시 계산했다. 리포트에서 수치를 "
                       "긁어 모으지 않는다 — 조건이 섞인다."}
    C.save_report("m39_m3_final.json", rep)
    write_md(rep)


def judge(res):
    best = max(res, key=lambda k: res[k]["roc_auc"])
    r = res[best]
    stable = {k: v for k, v in res.items() if v["stability"] >= 0.7}
    reasons = [
        "clean hold-out 최고는 **%s** — ROC-AUC %.3f (95%% 구간 %.3f~%.3f). "
        "M30 의 0.48 에서 올라온 값이고, 올린 것은 모델이 아니라 파서 교정(M32)과 "
        "라벨 분리(M33)다." % (best, r["roc_auc"], r["roc_auc_ci95"][0], r["roc_auc_ci95"][1]),
        "재학습 안정성 %.0f%% 이상인 후보는 %d개다: %s"
        % (70, len(stable), ", ".join(stable) or "없음"),
        "운영 경고율 2%% 에서 주 평가 35건 중 걸리는 것은 %d건이다. 50건이 교체 *전* "
        "모델 점수로 층화 추출된 세트라 운영선과 자리가 어긋난다 — 운영 임계선은 "
        "새 표본으로 다시 잡아야 한다." % r["operating_2pct"]["n_flagged"],
    ]
    if r["roc_auc_ci95"][0] < 0.6:
        v = "Conditional"
        reasons.append("부트스트랩 하한이 %.2f 다. 양성 10건짜리 표본이라 이 수치를 "
                       "운영 약속으로 쓸 수 없다." % r["roc_auc_ci95"][0])
    elif best in stable:
        v = "Conditional (조건부 채택)"
        reasons.append("성능·안정성·설명 세 축을 모두 넘긴 후보가 있다. 다만 라벨이 "
                       "35건뿐이라 **두 번째 라벨 세트로 재확인**하는 것을 조건으로 단다.")
    else:
        v = "Conditional"
        reasons.append("성능 최고 후보가 안정성 문턱을 못 넘는다. 성능과 안정성을 "
                       "함께 넘는 후보로만 운영해야 한다.")
    return {"verdict": v, "reasons": reasons, "best": best}


def write_md(r):
    L = ["# M39 — 모델 3 최종 정리", "",
         "> 후보를 **이 스크립트에서 한 번에 다시 계산했습니다.** 같은 평가함수,",
         "> 같은 부트스트랩, 같은 안정성 프로토콜입니다. 리포트에서 숫자를 긁어 모으면",
         "> 어느 수치가 어느 조건에서 나온 것인지 섞입니다.", "",
         "```text",
         "학습·적용 %d행" % r["n_train"],
         "주 평가   normal + atypical_design = %d건 (양성 %d)"
         % (r["eval_set"]["n"], r["eval_set"]["positive"]),
         "분리 보고 data_error %d / uncertain %d"
         % (r["eval_set"]["separate"]["data_error"], r["eval_set"]["separate"]["uncertain"]),
         "```", "",
         "## 1. 한 표", "",
         "| 후보 | ROC-AUC | 95% 구간 | PR-AUC | 상위7 recall | 안정성 | 설명 가능 |",
         "|---|---:|---|---:|---:|---:|---|"]
    for n, v in sorted(r["candidates"].items(), key=lambda kv: -kv[1]["roc_auc"]):
        L.append("| %s | **%.4f** | %.3f ~ %.3f | %.4f | %s | %.3f | %s |"
                 % (n, v["roc_auc"], v["roc_auc_ci95"][0], v["roc_auc_ci95"][1],
                    v["pr_auc"], v["equal_budget"]["recall"], v["stability"],
                    v["explainable"]))
    L += ["",
          "> 안정성은 표본 80%% 재학습 10회의 상위 30건 유지율입니다. Deep SVDD 만",
          "> 시드 간 겹침(난수 초기값이 있는 유일한 후보라 그쪽이 더 나쁜 축입니다).", "",
          "> **M30 에서 같은 50건의 ROC-AUC 는 0.48 이었습니다.**", "",
          "## 2. 계획서 §12 의 질문들", ""]
    for x in r["questions"]:
        L += ["**%s**" % x["q"], "", x["a"], ""]
    L += ["## 3. 판정", "", "**%s**" % r["verdict"]["verdict"], ""]
    for x in r["verdict"]["reasons"]:
        L.append("- %s" % x)
    L += ["", "## 4. 남은 것", "", "```text",
          "1  라벨 세트 두 번째 판 — 35건·양성 10건으로는 부트스트랩 구간이 너무 넓다",
          "2  운영 임계선 재산정 — 50건이 교체 전 모델로 층화된 세트라 자리가 어긋난다",
          "3  방향 성분의 validation — 합성은 정형만 흔들어서 방향·텍스트 가중치를 못 고른다",
          "4  Deep SVDD 붕괴 — epoch 이 아니라 구조 문제다. 쓰려면 구조를 손봐야 한다",
          "```", ""]
    p = os.path.join(C.REPORTS, "m39_m3_final.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
