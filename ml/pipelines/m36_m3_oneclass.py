r"""M36 — one-class 기준선을 clean hold-out 에서 다시 잰다 (계획서 Step 5).

M30 과 무엇이 같고 무엇이 다른가
    같다   모델 설정(A_설계핵심 / StandardScaler / nu 0.02 / gamma 0.5),
           경고선을 hold-out 이 아니라 전체 분포에서 긋는 것, 같은 50건
    다르다 입력이 M32 로 교정된 값이고, 정답이 M33 의 네 칸 라벨이다

M30 은 `비전형` 15건을 positive 로 뒀다. 그 중 8건이 파서 오류였으므로
그 수치는 이상탐지 성능이 아니라 파서 고장률이었다. 여기서는
`atypical_design` 10건만 positive 로 두고, `data_error` 와 `uncertain` 은
같은 표에 **따로** 싣는다 — 빼는 게 아니라 분리해서 보고한다.

세 축을 함께 낸다 (직전 계획서 §10 의 Go/No-Go 기준)
    성능   ROC-AUC / PR-AUC / 경고예산별 recall·precision
    안정성 표본 80% 재학습 10회의 상위 30건 유지율
    민감도 nu 를 바꿨을 때 순위가 얼마나 흔들리는가
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
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.neighbors import LocalOutlierFactor
from sklearn.preprocessing import StandardScaler
from sklearn.svm import OneClassSVM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, SEED, _binary, boot_ci

# M16/M30 최종 설정. clean hold-out 으로 바꾸지 않는다.
NUM = ["log_per_recipient", "log_support_count", "project_duration"]
CAT = ["support_method", "amount_type"]
NU, GAMMA = 0.02, 0.5
ALERT_RATE = 0.02
TOP_N = 30


def encode(train, apply_df):
    parts_tr, parts_ap, names = [], [], []
    for f in NUM:
        med = train[f].median()
        med = 0.0 if pd.isna(med) else med
        parts_tr += [train[f].fillna(med).to_numpy(), train[f].isna().astype(float).to_numpy()]
        parts_ap += [apply_df[f].fillna(med).to_numpy(), apply_df[f].isna().astype(float).to_numpy()]
        names += [f, f + "__missing"]
    for f in CAT:
        freq = train[f].value_counts(normalize=True)
        parts_tr.append(train[f].map(freq).fillna(0.0).to_numpy())
        parts_ap.append(apply_df[f].map(freq).fillna(0.0).to_numpy())
        names.append(f + "__freq")
    sc = StandardScaler().fit(np.column_stack(parts_tr))
    return sc.transform(np.column_stack(parts_tr)), sc.transform(np.column_stack(parts_ap)), names


def fit_all(Xtr, Xap, nu=NU):
    return {
        "OneClassSVM": -OneClassSVM(kernel="rbf", gamma=GAMMA, nu=nu)
                       .fit(Xtr).score_samples(Xap),
        "IsolationForest": -IsolationForest(n_estimators=500, contamination=nu,
                                            random_state=SEED, n_jobs=-1)
                           .fit(Xtr).score_samples(Xap),
        "LocalOutlierFactor": -LocalOutlierFactor(n_neighbors=20, novelty=True)
                              .fit(Xtr).score_samples(Xap),
    }


def views(train, scores, cl):
    """정답 정의 세 가지를 같은 점수로 잰다.

    주 평가        normal vs atypical_design            <- 판정의 근거
    데이터오류 포함  normal vs atypical_design+data_error <- M30 과 비교 가능한 값
    이전 라벨       정상 vs 비전형 (M30 정의 그대로)      <- 무엇이 달라졌는지 보이는 값
    """
    s = pd.Series(scores, index=train["row_id"].to_numpy())
    have = cl[cl["row_id"].isin(s.index)].copy()
    have["score"] = s.loc[have["row_id"]].to_numpy()

    k = max(1, int(round(len(train) * ALERT_RATE)))
    thr = float(np.sort(scores)[::-1][k - 1])

    defs = {
        "주 평가(atypical_design)": (have["라벨"].isin(["normal", "atypical_design"]),
                                 have["라벨"] == "atypical_design"),
        "데이터오류 포함": (have["라벨"].isin(["normal", "atypical_design", "data_error"]),
                     have["라벨"].isin(["atypical_design", "data_error"])),
        "이전 라벨(M30 정의)": (have["이전라벨"].isin(["정상", "비전형"]),
                          have["이전라벨"] == "비전형"),
    }
    out = {}
    for name, (keep, pos) in defs.items():
        sub = have[keep.to_numpy()]
        y = pos[keep].to_numpy(int)
        sc = sub["score"].to_numpy(float)
        if len(np.unique(y)) < 2:
            continue
        eb = min(EQUAL_BUDGET, len(sc))
        out[name] = {
            "n": int(len(y)), "n_positive": int(y.sum()),
            "roc_auc": round(float(roc_auc_score(y, sc)), 4),
            "pr_auc": round(float(average_precision_score(y, sc)), 4),
            "operating_2pct": _binary(y, sc >= thr),
            "equal_budget": {"top_n": eb,
                             **_binary(y, sc >= np.sort(sc)[::-1][eb - 1])},
        }
    return out, have, thr


def stability(train, model, n_iter=10, frac=0.8, top_n=TOP_N):
    """표본 80% 로 다시 학습해도 상위 목록이 유지되는가."""
    rng = np.random.default_rng(SEED)
    Xtr, Xap, _ = encode(train, train)
    base = set(train.iloc[np.argsort(-fit_all(Xtr, Xap)[model])[:top_n]]["row_id"])
    ov = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
        Xs, Xa, _ = encode(sub, train)
        top = set(train.iloc[np.argsort(-fit_all(Xs, Xa)[model])[:top_n]]["row_id"])
        ov.append(len(top & base) / top_n)
    return {"n_iter": n_iter, "frac": frac, "top_n": top_n,
            "overlap_mean": round(float(np.mean(ov)), 4),
            "overlap_min": round(float(np.min(ov)), 4)}


def nu_sensitivity(train, cl, top_n=TOP_N):
    """nu 는 OneClassSVM 의 모델 자체를 바꾼다. 순위가 얼마나 흔들리는지 본다."""
    Xtr, Xap, _ = encode(train, train)
    ref_top, out = None, {}
    for nu in (0.01, 0.02, 0.05, 0.10):
        s = fit_all(Xtr, Xap, nu=nu)["OneClassSVM"]
        v, _, _ = views(train, s, cl)
        top = set(train.iloc[np.argsort(-s)[:top_n]]["row_id"])
        ref_top = ref_top if ref_top is not None else top
        main = v.get("주 평가(atypical_design)", {})
        out["nu=%.2f" % nu] = {
            "roc_auc": main.get("roc_auc"),
            "overlap_top%d" % top_n: round(len(top & ref_top) / top_n, 4),
        }
    return out


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    Xtr, Xap, names = encode(train, train)
    scores = fit_all(Xtr, Xap)

    print("M36 — one-class 기준선 재평가")
    print("  학습 %d행 x %d축 / 경고율 %.0f%%" % (len(train), Xtr.shape[1],
                                            ALERT_RATE * 100))

    per_model, best_have = {}, None
    for name, s in scores.items():
        v, have, thr = views(train, s, cl)
        per_model[name] = {"threshold": round(thr, 6), "views": v}
        m = v["주 평가(atypical_design)"]
        print("\n  %-20s ROC-AUC %.4f  PR-AUC %.4f" % (name, m["roc_auc"], m["pr_auc"]))
        print("    운영 2%%      경고 %d (TP %d FP %d FN %d) recall %s precision %s"
              % (m["operating_2pct"]["n_flagged"], m["operating_2pct"]["TP"],
                 m["operating_2pct"]["FP"], m["operating_2pct"]["FN"],
                 m["operating_2pct"]["recall"], m["operating_2pct"]["precision"]))
        eb = m["equal_budget"]
        print("    같은예산 상위%d TP %d FP %d recall %s precision %s"
              % (eb["top_n"], eb["TP"], eb["FP"], eb["recall"], eb["precision"]))
        if name == "OneClassSVM":
            best_have = have

    # 주 평가 기준 최고 모델의 부트스트랩 구간
    best = max(per_model, key=lambda k: per_model[k]["views"]["주 평가(atypical_design)"]["roc_auc"])
    s = pd.Series(scores[best], index=train["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    ci = boot_ci((sub["라벨"] == "atypical_design").to_numpy(int),
                 s.loc[sub["row_id"]].to_numpy(float))
    print("\n  최고 %s ROC-AUC 95%%CI [%.3f, %.3f]" % (best, ci[0], ci[1]))

    stab = {n: stability(train, n) for n in scores}
    print("\n== 재학습 안정성 (80%% 재표집 10회, 상위 %d건 유지율)" % TOP_N)
    for n, v in stab.items():
        print("  %-20s 평균 %.3f / 최저 %.3f" % (n, v["overlap_mean"], v["overlap_min"]))

    nus = nu_sensitivity(train, cl)
    print("\n== nu 민감도 (OneClassSVM)")
    for k, v in nus.items():
        print("  %-10s ROC-AUC %s / 상위%d 유지율 %s"
              % (k, v["roc_auc"], TOP_N, v["overlap_top%d" % TOP_N]))

    rep = {
        "model_config": "features=A_설계핵심 / StandardScaler / nu=%.2f gamma=%.1f" % (NU, GAMMA),
        "n_train": int(len(train)), "n_features": int(Xtr.shape[1]),
        "feature_names": names, "alert_rate": ALERT_RATE,
        "per_model": per_model, "best_by_main_view": best, "best_roc_auc_ci95": ci,
        "stability": stab, "nu_sensitivity": nus,
        "note": "M30 과 같은 설정·같은 50건. 다른 것은 교정된 입력과 네 칸 라벨뿐이다.",
    }
    C.save_report("m36_m3_oneclass.json", rep)
    write_md(rep, best_have)


def write_md(r, have):
    L = ["# M36 — one-class 기준선을 clean hold-out 에서 다시 잰다", "",
         "> M30 과 **모델 설정·경고선·50건이 모두 같습니다.** 다른 것은 두 가지뿐입니다 —",
         "> 입력이 M32 로 교정된 값이고, 정답이 M33 의 네 칸 라벨입니다.", "",
         "```text", r["model_config"],
         "학습 %d행 x %d축 / 경고선은 전체 분포의 상위 %.0f%%"
         % (r["n_train"], r["n_features"], r["alert_rate"] * 100),
         "```", "",
         "## 1. 주 평가 — `normal` vs `atypical_design`", "",
         "| 모델 | ROC-AUC | PR-AUC | 운영 2% recall | 같은예산 상위7 recall | precision |",
         "|---|---:|---:|---:|---:|---:|"]
    for n, v in sorted(r["per_model"].items(),
                       key=lambda kv: -kv[1]["views"]["주 평가(atypical_design)"]["roc_auc"]):
        m = v["views"]["주 평가(atypical_design)"]
        eb = m["equal_budget"]
        L.append("| %s | **%.4f** | %.4f | %s | %s | %s |"
                 % (n, m["roc_auc"], m["pr_auc"], m["operating_2pct"]["recall"],
                    eb["recall"], eb["precision"]))
    L += ["",
          "최고 모델 **%s** 의 ROC-AUC 95%% 부트스트랩 구간: **%.3f ~ %.3f**"
          % (r["best_by_main_view"], r["best_roc_auc_ci95"][0], r["best_roc_auc_ci95"][1]),
          "",
          "> M30 에서 같은 50건의 OneClassSVM ROC-AUC 는 **0.48** 이었습니다.", "",
          "## 2. 정답 정의를 바꾸면", "",
          "`data_error` 를 positive 에 넣으면 수치가 올라갑니다. 그건 이상탐지가",
          "잘해서가 아니라 **파서가 만든 드문 값을 잡았기 때문**입니다. 두 값을",
          "나란히 두는 이유가 그것입니다.", "",
          "| 모델 | 정답 정의 | n | 양성 | ROC-AUC | PR-AUC |",
          "|---|---|---:|---:|---:|---:|"]
    for n, v in r["per_model"].items():
        for dname, m in v["views"].items():
            L.append("| %s | %s | %d | %d | %.4f | %.4f |"
                     % (n, dname, m["n"], m["n_positive"], m["roc_auc"], m["pr_auc"]))
    L += ["", "## 3. 안정성 — 표본 80%% 재학습 10회", "",
          "| 모델 | 상위 %d건 유지율 (평균 / 최저) |" % TOP_N, "|---|---|"]
    for n, v in r["stability"].items():
        L.append("| %s | %.0f%% / %.0f%% |" % (n, v["overlap_mean"] * 100,
                                               v["overlap_min"] * 100))
    L += ["",
          "> `IsolationForest` 와 `LocalOutlierFactor` 의 `score_samples` 는",
          "> contamination 과 무관합니다. 그 모델들의 높은 유지율은 안정성의 증거가",
          "> 아니라 정의상 당연한 값입니다.", "",
          "## 4. nu 민감도 (OneClassSVM)", "",
          "nu 는 임계선만 옮기는 값이 아니라 **모델 자체를 바꿉니다.**", "",
          "| nu | 주 평가 ROC-AUC | nu=0.01 대비 상위 %d 유지율 |" % TOP_N,
          "|---|---:|---:|"]
    for k, v in r["nu_sensitivity"].items():
        L.append("| %s | %s | %.0f%% |" % (k, v["roc_auc"],
                                           v["overlap_top%d" % TOP_N] * 100))
    if have is not None:
        fn = have[(have["라벨"] == "atypical_design")].sort_values("score", ascending=False)
        L += ["", "## 5. OneClassSVM 이 `atypical_design` 10건을 어떻게 세웠나", "",
              "| 사업 | 점수 순위(주 평가 35건 중) | 라벨근거 |", "|---|---:|---|"]
        sub = have[have["라벨"].isin(["normal", "atypical_design"])].copy()
        sub["rank"] = sub["score"].rank(ascending=False).astype(int)
        for _, x in sub[sub["라벨"] == "atypical_design"].sort_values("rank").iterrows():
            L.append("| %s | %d / %d | %s |" % (str(x["사업명"])[:40], x["rank"],
                                                len(sub), str(x["라벨근거"])[:70]))
    L.append("")
    p = os.path.join(C.REPORTS, "m36_m3_oneclass.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
