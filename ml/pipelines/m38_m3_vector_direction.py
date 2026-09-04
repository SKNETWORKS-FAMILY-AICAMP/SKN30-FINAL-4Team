r"""M38 — Vector Direction: 거리만이 아니라 '어느 방향으로 달라졌는가' (계획서 §4~§7, Step 7).

한 사업을 벡터 하나로 만들고, 비교군 대표벡터 C 에서 얼마나·**어느 쪽으로**
움직였는지를 본다.

    D = X - C          평균적인 유사사업에서 이 사업으로 가는 이동벡터

거리 이상도 (계획서 §4.2)
    ||D|| 를 비교군 안에서 percentile 로 환산한다. 비교군마다 퍼진 정도가
    다르므로 절대 거리를 그대로 쓰면 큰 비교군만 계속 걸린다.

방향 이상도 — 두 가지를 따로 만든다

    (1) residual  비교군이 평소 변하는 방향(비교군 자체 주성분 상위 k개)에서
                  얼마나 벗어났는가. D 를 그 부분공간에 사영하고 남은 에너지
                  비율이다. "이 비교군에서는 원래 그 축으로 잘 안 움직인다"
                  를 재는 값이라 사람 정의가 필요 없다.

    (2) typed     계획서 §5 가 지시한 대로 **사람이 검토가 필요하다고 정의한
                  방향**과의 cosine similarity. 경고 이유를 문장으로 말할 수
                  있다는 것이 이 방식의 유일한 존재 이유다.

                  A1 고액·소수기업형   지원액 ↑↑ / 기업수 ↓↓
                  A2 고액·초단기형     지원액 ↑↑ / 사업기간 ↓↓
                  A3 고지원율형        지원비율 ↑↑
                  A4 소액·다수기업형   지원액 ↓↓ / 기업수 ↑↑

                  계획서의 A4(text-feature 불일치)는 텍스트가 있어야 성립해서
                  DL18 로 넘긴다. 대신 라벨에서 실제로 관찰된 소액·다수형을
                  여기 A4 자리에 둔다 (수출보험료 400개사·디딤돌 900과제).

거리와 방향을 섞는 가중치 (계획서 §6)
    0.7/0.3, 0.5/0.5, 0.3/0.7 을 후보로 두고 **M37 합성 이상치에서 고른다.**
    계획서 §6 의 "human hold-out 으로 weight 를 선택하면 안 된다"를 지키는
    유일한 방법이다. 고른 뒤 clean hold-out 에서 한 번만 확인한다.
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
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, _binary, boot_ci
from m37_m3_synthetic import build as build_synthetic

SEED = 42
MIN_COHORT = 20         # 이보다 작으면 상위 단계 비교군으로 물러난다
N_PC = 3                # 비교군이 '평소 변하는 방향' 으로 볼 주성분 개수
NUM = ["log_per_recipient", "log_support_count", "project_duration", "support_ratio"]
CAT = ["support_method", "amount_type", "support_unit"]
WEIGHTS = [0.7, 0.5, 0.3]       # 거리에 주는 비중. 나머지가 방향 몫이다

# 계획서 §5 — 사람이 정의한 '검토가 필요한 설계 방향'. 축 순서는 NUM 과 같다.
TYPED = {
    "A1 고액·소수기업형": {"log_per_recipient": +1, "log_support_count": -1},
    "A2 고액·초단기형": {"log_per_recipient": +1, "project_duration": -1},
    "A3 고지원율형": {"support_ratio": +1},
    "A4 소액·다수기업형": {"log_per_recipient": -1, "log_support_count": +1},
}
TYPED_TEXT = {
    "A1 고액·소수기업형": "동일 비교군 대비 기업당 지원액은 크고 지원 기업 수는 적은 방향",
    "A2 고액·초단기형": "동일 비교군 대비 기업당 지원액은 크고 사업기간은 짧은 방향",
    "A3 고지원율형": "동일 비교군 대비 지원비율이 높은 방향",
    "A4 소액·다수기업형": "동일 비교군 대비 기업당 지원액은 작고 지원 기업 수는 많은 방향",
}


def build_vectors(train, apply_df):
    """정형 벡터. 수치 블록과 범주 블록의 크기를 맞춘 뒤 붙인다.

    맞추지 않으면 범주 one-hot 축 개수만큼 범주 블록이 공간을 지배한다.
    계획서 §7 이 text embedding 에 대해 지적한 것과 같은 문제다.
    """
    num_tr, num_ap, names = [], [], []
    for f in NUM:
        med = train[f].median()
        med = 0.0 if pd.isna(med) else med
        num_tr.append(train[f].fillna(med).to_numpy(dtype=float))
        num_ap.append(apply_df[f].fillna(med).to_numpy(dtype=float))
        names.append(f)
    Ntr, Nap = np.column_stack(num_tr), np.column_stack(num_ap)
    sc = StandardScaler().fit(Ntr)
    Ntr, Nap = sc.transform(Ntr), sc.transform(Nap)

    cat_tr, cat_ap, cnames = [], [], []
    for f in CAT:
        levels = [v for v in train[f].dropna().unique()]
        for v in levels:
            cat_tr.append((train[f] == v).to_numpy(float))
            cat_ap.append((apply_df[f] == v).to_numpy(float))
            cnames.append("%s=%s" % (f, v))
    Ctr = np.column_stack(cat_tr) if cat_tr else np.zeros((len(train), 0))
    Cap = np.column_stack(cat_ap) if cat_ap else np.zeros((len(apply_df), 0))

    def blk(A, ref):
        s = np.linalg.norm(ref, axis=1).mean()
        return A / s if s > 0 else A
    Ntr_n, Nap_n = blk(Ntr, Ntr), blk(Nap, Ntr)
    Ctr_n, Cap_n = blk(Ctr, Ctr), blk(Cap, Ctr)
    return (np.hstack([Ntr_n, Ctr_n]), np.hstack([Nap_n, Cap_n]),
            names + cnames, len(names))


def cohort_key(df):
    """지원성격 x 지원방식. 얇으면 지원성격만, 그것도 얇으면 전체."""
    return (df["support_type"].astype(str) + "|" + df["support_method"].astype(str),
            df["support_type"].astype(str))


def score_components(train, apply_df, Xtr, Xap, n_num):
    """비교군별 C 를 만들고 거리·방향 세 성분을 낸다."""
    k2_tr, k1_tr = cohort_key(train)
    k2_ap, k1_ap = cohort_key(apply_df)
    n2 = k2_tr.value_counts()
    n1 = k1_tr.value_counts()

    def resolve(k2, k1):
        if n2.get(k2, 0) >= MIN_COHORT:
            return ("2", k2)
        if n1.get(k1, 0) >= MIN_COHORT:
            return ("1", k1)
        return ("0", "ALL")

    # 적용 행이 학습에 없는 비교군으로 떨어질 수 있다 (합성이 지원방식을 바꾼 경우).
    # 그래서 학습·적용 양쪽에서 나온 키를 모두 만들어 둔다.
    groups = {}
    need = ({resolve(a, b) for a, b in zip(k2_tr, k1_tr)} |
            {resolve(a, b) for a, b in zip(k2_ap, k1_ap)})
    for lvl, key in need:
        if lvl == "2":
            mask = (k2_tr == key).to_numpy()
        elif lvl == "1":
            mask = (k1_tr == key).to_numpy()
        else:
            mask = np.ones(len(train), bool)
        M = Xtr[mask]
        c = M.mean(0)
        Dm = M - c
        # 비교군이 '평소 변하는 방향' = 자체 주성분 상위 N_PC 개
        if len(M) > N_PC:
            _, _, Vt = np.linalg.svd(Dm, full_matrices=False)
            V = Vt[:N_PC]
        else:
            V = np.zeros((0, Xtr.shape[1]))
        groups[(lvl, key)] = {"c": c, "V": V, "n": int(mask.sum()),
                              "dist": np.linalg.norm(Dm, axis=1)}

    dist_pct, resid, typed_cos, typed_name, cohort_n = [], [], [], [], []
    T = np.zeros((len(TYPED), Xtr.shape[1]))
    for i, (_, spec) in enumerate(TYPED.items()):
        for f, sgn in spec.items():
            T[i, NUM.index(f)] = sgn
    T = T / np.linalg.norm(T, axis=1, keepdims=True)
    tnames = list(TYPED)

    for i in range(len(apply_df)):
        g = groups[resolve(k2_ap.iloc[i], k1_ap.iloc[i])]
        d = Xap[i] - g["c"]
        nd = np.linalg.norm(d)
        dist_pct.append(float((g["dist"] <= nd).mean()) * 100)
        cohort_n.append(g["n"])
        if nd < 1e-12 or len(g["V"]) == 0:
            resid.append(0.0)
            typed_cos.append(0.0)
            typed_name.append(None)
            continue
        proj = g["V"] @ d
        resid.append(float(1.0 - (proj @ proj) / (nd * nd)))
        cs = (T @ d) / nd
        j = int(np.argmax(cs))
        typed_cos.append(float(cs[j]))
        typed_name.append(tnames[j])

    return pd.DataFrame({
        "dist_pct": dist_pct, "dir_residual": resid,
        "dir_typed_cos": typed_cos, "dir_typed_name": typed_name,
        "cohort_n": cohort_n,
    })


def combine(comp, w_dist, direction="dir_residual"):
    """거리와 방향을 percentile 로 맞춘 뒤 섞는다.

    한쪽은 0~100, 다른 쪽은 0~1 인 채로 더하면 가중치가 의미를 잃는다.
    """
    d = pd.Series(comp["dist_pct"]).rank(pct=True).to_numpy()
    a = pd.Series(comp[direction]).rank(pct=True).to_numpy()
    return w_dist * d + (1 - w_dist) * a


def eval_holdout(apply_df, scores, cl):
    s = pd.Series(scores, index=apply_df["row_id"].to_numpy())
    sub = cl[cl["라벨"].isin(["normal", "atypical_design"]) & cl["row_id"].isin(s.index)]
    y = (sub["라벨"] == "atypical_design").to_numpy(int)
    sc = s.loc[sub["row_id"]].to_numpy(float)
    eb = min(EQUAL_BUDGET, len(sc))
    return {"n": int(len(y)), "n_positive": int(y.sum()),
            "roc_auc": round(float(roc_auc_score(y, sc)), 4),
            "pr_auc": round(float(average_precision_score(y, sc)), 4),
            "equal_budget": {"top_n": eb, **_binary(y, sc >= np.sort(sc)[::-1][eb - 1])}}, y, sc


def synthetic_recall(train, syn, w, direction):
    """가중치 선택용 validation. **hold-out 을 쓰지 않는다** (계획서 §6)."""
    mixed = pd.concat([train.assign(__synthetic=False), syn], ignore_index=True)
    Xtr, Xap, _, n_num = build_vectors(train, mixed)
    comp = score_components(train, mixed, Xtr, Xap, n_num)
    s = combine(comp, w, direction)
    is_syn = mixed["__synthetic"].fillna(False).to_numpy(bool)
    k = int(is_syn.sum())
    return float(is_syn[np.argsort(-s)[:k]].mean())


def resample_stability(train, w, direction, n_iter=10, frac=0.8, top_n=30):
    """비교군 대표벡터 C 를 80% 표본에서 다시 만들어도 상위 목록이 유지되는가.

    Deep SVDD 가 무너진 자리다(시드 간 상위30 겹침 0.38). 같은 잣대로 잰다.
    이 방식에는 난수 초기값이 없으므로 흔들리는 원인은 오직 표본뿐이다.
    """
    rng = np.random.default_rng(SEED)
    Xtr, Xap, _, n_num = build_vectors(train, train)
    base_comp = score_components(train, train, Xtr, Xap, n_num)
    base = set(train.iloc[np.argsort(-combine(base_comp, w, direction))[:top_n]]["row_id"])
    ov = []
    for _ in range(n_iter):
        sub = train.sample(frac=frac, random_state=int(rng.integers(1e6)))
        Xs, Xa, _, nn = build_vectors(sub, train)
        c = score_components(sub, train, Xs, Xa, nn)
        top = set(train.iloc[np.argsort(-combine(c, w, direction))[:top_n]]["row_id"])
        ov.append(len(top & base) / top_n)
    return {"n_iter": n_iter, "frac": frac, "top_n": top_n,
            "overlap_mean": round(float(np.mean(ov)), 4),
            "overlap_min": round(float(np.min(ov)), 4)}


def main():
    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")
    syn = build_synthetic(train, 120, SEED)

    Xtr, Xap, names, n_num = build_vectors(train, train)
    comp = score_components(train, train, Xtr, Xap, n_num)
    print("M38 — Vector Direction")
    print("  학습·적용 %d행 / 벡터 %d차원 (수치 %d + 범주 %d)"
          % (len(train), Xtr.shape[1], n_num, Xtr.shape[1] - n_num))
    print("  비교군 크기 중앙값 %d / 최소 %d"
          % (int(np.median(comp["cohort_n"])), int(comp["cohort_n"].min())))

    # ---- 1. 성분 하나씩 (계획서 Step 7: distance only / direction only)
    singles = {
        "distance only": comp["dist_pct"].to_numpy(float),
        "direction only (residual)": comp["dir_residual"].to_numpy(float),
        "direction only (typed cos)": comp["dir_typed_cos"].to_numpy(float),
    }
    print("\n== 성분 하나씩 (clean hold-out)")
    single_res = {}
    for k, v in singles.items():
        r, _, _ = eval_holdout(train, v, cl)
        single_res[k] = r
        print("  %-28s ROC-AUC %.4f  PR-AUC %.4f  상위7 recall %s"
              % (k, r["roc_auc"], r["pr_auc"], r["equal_budget"]["recall"]))

    # ---- 2. 가중치는 합성에서 고른다 (계획서 §6)
    print("\n== 가중치 선택 — M37 합성 이상치 회수율 기준 (hold-out 미사용)")
    sel = {}
    for direction in ("dir_residual", "dir_typed_cos"):
        for w in WEIGHTS:
            rec = synthetic_recall(train, syn, w, direction)
            sel["%s w_dist=%.1f" % (direction, w)] = round(rec, 4)
            print("  %-28s 합성 회수율 %.4f" % ("%s w=%.1f" % (direction, w), rec))
    best_key = max(sel, key=sel.get)
    best_dir = best_key.split(" ")[0]
    best_w = float(best_key.split("=")[1])
    print("  -> 선택: %s (합성 회수율 %.4f)" % (best_key, sel[best_key]))

    # ---- 3. 고른 설정을 clean hold-out 에서 한 번만 확인
    combos = {}
    for direction in ("dir_residual", "dir_typed_cos"):
        for w in WEIGHTS:
            r, y, sc = eval_holdout(train, combine(comp, w, direction), cl)
            combos["%s w_dist=%.1f" % (direction, w)] = r
    chosen = combos[best_key]
    r_chosen, y, sc = eval_holdout(train, combine(comp, best_w, best_dir), cl)
    ci = boot_ci(y, sc)
    print("\n== clean hold-out (선택한 설정만이 판정 근거다)")
    print("  %s  ROC-AUC %.4f 95%%CI [%.3f, %.3f]  PR-AUC %.4f  상위7 recall %s precision %s"
          % (best_key, r_chosen["roc_auc"], ci[0], ci[1], r_chosen["pr_auc"],
             r_chosen["equal_budget"]["recall"], r_chosen["equal_budget"]["precision"]))
    print("\n  (참고) 고르지 않은 조합들")
    for k, v in combos.items():
        if k != best_key:
            print("    %-32s ROC-AUC %.4f" % (k, v["roc_auc"]))

    # ---- 4. 경고 이유를 문장으로 — 이 방식의 존재 이유
    out = train[["row_id", "title", "support_type", "support_method"]].copy()
    out = pd.concat([out, comp], axis=1)
    out["score"] = combine(comp, best_w, best_dir)
    top = out.sort_values("score", ascending=False).head(10)
    print("\n== 상위 10건과 경고 이유 (계획서 §5)")
    reasons = []
    for _, r in top.iterrows():
        txt = ("%s — %s (cosine %.2f, 비교군 %d건, 거리 P%.0f)"
               % (r["dir_typed_name"] or "유형 없음",
                  TYPED_TEXT.get(r["dir_typed_name"], "비교군이 평소 변하지 않는 방향"),
                  r["dir_typed_cos"], r["cohort_n"], r["dist_pct"]))
        reasons.append({"row_id": r["row_id"], "title": str(r["title"])[:60],
                        "score": round(float(r["score"]), 4), "reason": txt})
        print("  [%.3f] %-42s %s" % (r["score"], str(r["title"])[:42], txt[:70]))

    dist_typed = out["dir_typed_name"].value_counts(dropna=False)
    print("\n== 전체에서 가장 가까운 방향 유형 분포")
    print("  %s" % {str(k): int(v) for k, v in dist_typed.items()})

    # ---- 5. 안정성. Deep SVDD 가 무너진 자리라 같은 잣대로 잰다.
    print("\n== 재학습 안정성 (80% 재표집 10회, 상위 30건 유지율)")
    stab = {"chosen (%s)" % best_key: resample_stability(train, best_w, best_dir),
            "distance only": resample_stability(train, 1.0, "dir_residual")}
    for k, v in stab.items():
        print("  %-34s 평균 %.3f / 최저 %.3f" % (k, v["overlap_mean"], v["overlap_min"]))

    syn_single = {"distance only": synthetic_recall(train, syn, 1.0, "dir_residual"),
                  "direction only (residual)": synthetic_recall(train, syn, 0.0, "dir_residual"),
                  "direction only (typed cos)": synthetic_recall(train, syn, 0.0, "dir_typed_cos")}
    print("\n== 성분별 합성 회수율 (참고)")
    for k, v in syn_single.items():
        print("  %-30s %.4f" % (k, v))

    ml = {}
    p36 = os.path.join(C.REPORTS, "m36_m3_oneclass.json")
    if os.path.exists(p36):
        import json
        j = json.load(open(p36, encoding="utf-8"))
        ml = {n: v["views"]["주 평가(atypical_design)"]["roc_auc"]
              for n, v in j["per_model"].items()}

    rep = {
        "질문": "거리보다 방향 차이가 사람 판단과 더 잘 맞는가 (계획서 §12-6)",
        "n_train": int(len(train)), "n_dims": int(Xtr.shape[1]),
        "n_numeric_dims": int(n_num), "cohort_rule": "지원성격x지원방식 -> 지원성격 -> 전체",
        "min_cohort": MIN_COHORT, "n_pc": N_PC,
        "typed_directions": {k: TYPED_TEXT[k] for k in TYPED},
        "singles": single_res,
        "weight_selection_on_synthetic": sel,
        "chosen": best_key, "chosen_holdout": r_chosen, "chosen_roc_auc_ci95": ci,
        "all_combos_holdout": combos,
        "top_cases": reasons,
        "typed_distribution": {str(k): int(v) for k, v in dist_typed.items()},
        "stability": stab, "synthetic_recall_singles":
            {k: round(v, 4) for k, v in syn_single.items()},
        "ml_same_holdout": ml,
        "note": "가중치는 M37 합성에서 골랐다. clean hold-out 은 확인에만 썼다 (계획서 §6).",
    }
    C.save_report("m38_m3_vector_direction.json", rep)
    out.to_parquet(os.path.join(C.PROC, "m38_vector_direction.parquet"), index=False)
    write_md(rep)


def write_md(r):
    L = ["# M38 — Vector Direction: 거리만이 아니라 어느 방향으로 달라졌는가", "",
         "> 계획서 §4~§7. 비교군 대표벡터 `C` 에서 이 사업까지의 이동벡터 `D = X - C` 를",
         "> 만들고, **얼마나** 움직였는지(거리)와 **어느 쪽으로** 움직였는지(방향)를",
         "> 따로 잰 뒤 섞습니다.", "",
         "```text",
         "벡터 %d차원 (수치 %d + 범주 one-hot %d) / 학습·적용 %d행"
         % (r["n_dims"], r["n_numeric_dims"], r["n_dims"] - r["n_numeric_dims"],
            r["n_train"]),
         "비교군: %s (최소 %d건)" % (r["cohort_rule"], r["min_cohort"]),
         "```", "",
         "## 1. 방향을 두 가지로 나눈 이유", "",
         "| | 무엇을 재는가 | 사람 정의 필요 | 경고 이유를 말할 수 있는가 |",
         "|---|---|---|---|",
         "| `residual` | 비교군이 평소 변하는 방향(자체 주성분 상위 %d개)에서 벗어난 정도 | 불필요 | 못 함 |" % r["n_pc"],
         "| `typed cos` | 사람이 정의한 검토 방향과의 cosine | 필요 | **함** |", "",
         "사람이 정의한 방향 (계획서 §5)", "",
         "| 유형 | 방향 |", "|---|---|"]
    for k, v in r["typed_directions"].items():
        L.append("| `%s` | %s |" % (k, v))
    L += ["",
          "> 계획서의 A4(text-feature 불일치)는 텍스트가 있어야 성립해서 DL18 로",
          "> 넘겼습니다. 대신 라벨에서 실제로 관찰된 소액·다수형을 A4 자리에 뒀습니다",
          "> (수출보험료 400개사·디딤돌 900과제).", "",
          "## 2. 성분 하나씩 — clean hold-out", "",
          "| 성분 | ROC-AUC | PR-AUC | 상위7 recall |", "|---|---:|---:|---:|"]
    for k, v in r["singles"].items():
        L.append("| %s | %.4f | %.4f | %s |"
                 % (k, v["roc_auc"], v["pr_auc"], v["equal_budget"]["recall"]))
    L += ["", "## 3. 가중치는 합성 이상치에서 골랐습니다", "",
          "계획서 §6: **\"human hold-out 으로 weight 를 선택하면 안 된다\"**.",
          "M37 의 합성 이상치 회수율만 보고 골랐습니다.", "",
          "| 설정 | 합성 회수율 |", "|---|---:|"]
    for k, v in sorted(r["weight_selection_on_synthetic"].items(), key=lambda kv: -kv[1]):
        mark = " **← 선택**" if k == r["chosen"] else ""
        L.append("| %s | %.4f%s |" % (k, v, mark))
    ch = r["chosen_holdout"]
    L += ["", "## 4. 고른 설정을 clean hold-out 에서 한 번만 확인", "",
          "```text",
          "%s" % r["chosen"],
          "ROC-AUC %.4f  95%% 부트스트랩 구간 %.3f ~ %.3f"
          % (ch["roc_auc"], r["chosen_roc_auc_ci95"][0], r["chosen_roc_auc_ci95"][1]),
          "PR-AUC  %.4f" % ch["pr_auc"],
          "상위 %d건 recall %s / precision %s"
          % (ch["equal_budget"]["top_n"], ch["equal_budget"]["recall"],
             ch["equal_budget"]["precision"]),
          "```", "",
          "고르지 않은 조합들 (참고용 — 판정 근거가 아닙니다)", "",
          "| 설정 | ROC-AUC |", "|---|---:|"]
    for k, v in sorted(r["all_combos_holdout"].items(), key=lambda kv: -kv[1]["roc_auc"]):
        L.append("| %s | %.4f |" % (k, v["roc_auc"]))
    if r.get("ml_same_holdout"):
        L += ["", "같은 hold-out 의 기존 모델 (M36)", "", "| 모델 | ROC-AUC |", "|---|---:|"]
        for k, v in sorted(r["ml_same_holdout"].items(), key=lambda kv: -kv[1]):
            L.append("| %s | %.4f |" % (k, v))
    L += ["", "### 여기서 한 번 멈추고 읽어야 하는 것", "",
          "고르지 않은 조합 중 `dir_typed_cos w_dist=0.7` 의 hold-out ROC-AUC 가",
          "**%.4f** 로 선택한 설정(%.4f)보다 높습니다. 그 값을 보고 설정을 바꾸면"
          % (max(v["roc_auc"] for k, v in r["all_combos_holdout"].items()
                 if k != r["chosen"]), ch["roc_auc"]),
          "**hold-out 이 튜닝셋이 됩니다**(계획서 §11.2). 바꾸지 않았습니다.", "",
          "동시에 이것은 선택 근거였던 합성 이상치가 이 방식에 대해 **약한**",
          "**validation** 이라는 뜻이기도 합니다 — 합성 회수율이 0.08~0.22 로 전부",
          "낮아서 조합 간 차이를 가릴 힘이 부족합니다. 방향 성분을 제대로 고르려면",
          "합성이 아니라 **라벨이 붙은 두 번째 세트**가 필요합니다. 그것이 이",
          "실험이 남긴 가장 실질적인 요구사항입니다.", "",
          "## 5. 안정성 — 재학습하면 같은 사업을 경고하는가", "",
          "| 설정 | 상위 30건 유지율 (평균 / 최저) |", "|---|---|"]
    for k, v in (r.get("stability") or {}).items():
        L.append("| %s | %.0f%% / %.0f%% |" % (k, v["overlap_mean"] * 100,
                                               v["overlap_min"] * 100))
    L += ["",
          "> 이 방식에는 난수 초기값이 없습니다. 비교군 대표벡터 `C` 는 평균이라",
          "> 흔들리는 원인이 표본뿐입니다. Deep SVDD 가 시드만 바꿔도 상위30 의",
          "> 62%가 바뀐 것(DL17)과 같은 잣대로 잰 값입니다.", "",
          "## 6. 경고 이유를 문장으로 — 이 방식의 존재 이유", "",
          "거리 기반 모델은 \"멀다\"까지만 말합니다. 방향 유형을 붙이면 **왜** 검토가",
          "필요한지 문장이 나옵니다.", "",
          "| 점수 | 사업 | 경고 이유 |", "|---:|---|---|"]
    for c in r["top_cases"]:
        L.append("| %.3f | %s | %s |" % (c["score"], c["title"][:40], c["reason"]))
    L += ["", "전체에서 가장 가까운 방향 유형 분포: `%s`" % r["typed_distribution"], "",
          "> 출력 문구는 M13 의 허용 표현 안에서만 씁니다 — '드문 설계 조합, 확인 필요'",
          "> 이지 '잘못 설계됨'이 아닙니다.", ""]
    p = os.path.join(C.REPORTS, "m38_m3_vector_direction.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
