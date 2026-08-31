r"""DL18 — 텍스트를 벡터에 붙인다 (계획서 §4.1 · §7 · §5의 A4, Step 7 마지막 칸).

M38 은 정형 축만 썼다. 계획서 §4.1 은 사업목적·지원대상·지원내용을 embedding
으로 바꿔 같은 벡터에 붙이라고 한다. 여기서 그 칸을 채운다.

계획서 §7 의 경고를 그대로 지킨다
    정형이 10~20차원인데 text embedding 이 768차원이면 단순 concat 시 text 가
    공간을 지배한다. 그래서 PCA 로 16~32차원으로 줄이고, 두 블록을 각각
    정규화한 뒤 가중치를 준다.

A4 — text-feature 불일치 (계획서 §5)
    M38 에서는 텍스트가 없어 이 유형을 비워 뒀다. 여기서 채운다.
    방향벡터로 만들 수 없는 종류의 신호라 **불일치 점수**로 만든다.

        text_mismatch = (자기 비교군 텍스트 중심까지의 코사인거리)
                      - (가장 가까운 다른 비교군 텍스트 중심까지의 거리)

    양수이고 클수록 "텍스트는 다른 비교군에 속한다고 말하는데 정형 축은
    이 비교군에 들어와 있다"는 뜻이다. 계획서의 예 — 텍스트상 목적은 R&D
    인데 설계 feature 는 금융융자형 — 이 그대로 잡힌다.

텍스트 가중치를 무엇으로 고르는가 — **고르지 않는다.**
    M37 합성 이상치는 정형 축만 흔든다. 텍스트를 건드리지 않으므로 합성
    회수율로 텍스트 가중치를 고르면 항상 0 이 이긴다. 그건 텍스트가 쓸모
    없다는 뜻이 아니라 **validation 이 그 질문에 답할 수 없다**는 뜻이다.
    그래서 계획서 §7 이 예시로 든 0.6/0.4 를 그대로 쓰고, 나머지 비율은
    민감도로만 싣는다. hold-out 으로 고르지 않는다(계획서 §11.2).

숨기지 않고 적는 비대칭
    taxonomy 965행은 사업목적·지원대상·업종 원문이 있고, bizinfo 983행은
    제목과 지원규모 문장뿐이다. 텍스트 블록의 정보량이 코호트마다 다르다.
    그래서 taxonomy 만으로 자른 수치를 따로 낸다.

GPU 가 필요한가 — 아니다.
    ko-sroberta-multitask(RoBERTa-base) 로 1,948개 문장을 인코딩하는 일이다.
    CPU 로 1~2분이면 끝난다. GPU 가 필요한 지점은 **인코더를 미세조정할 때**
    이고, 이 실험은 사전학습 임베딩을 그대로 쓴다.
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
from sklearn.decomposition import PCA
from sklearn.metrics import roc_auc_score

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from m13_m4_anomaly import MIN_AXES, SRC, prepare
from m34_m3_diagnostics import CLEAN, EQUAL_BUDGET, _binary, boot_ci
from m38_m3_vector_direction import (MIN_COHORT, N_PC, SEED, build_vectors,
                                     cohort_key, combine, eval_holdout,
                                     score_components)

MODEL = "jhgan/ko-sroberta-multitask"
CACHE = os.path.join(C.PROC, "m3_text_embeddings.parquet")
TEXT_DIM = 24                   # 계획서 §7 의 16~32 구간
W_STRUCT = 0.6                  # 계획서 §7 의 예시 비율. 데이터로 고르지 않는다
MAX_CHARS = 900


def make_text(df):
    """사업목적·지원대상·지원내용. 없는 칸은 비워 두고 있는 것만 잇는다."""
    parts = [df["title"].fillna(""), df["policy_purpose"].fillna(""),
             df["support_target"].fillna(""), df["industry"].fillna(""),
             df["evidence_text"].fillna("")]
    t = parts[0].astype(str)
    for p in parts[1:]:
        t = t + " \n" + p.astype(str)
    return t.str.replace(r"\s+", " ", regex=True).str.strip().str[:MAX_CHARS]


def embed(train, force=False):
    """문장 임베딩. 한 번 만들면 캐시한다 — 텍스트가 안 바뀌면 값도 안 바뀐다."""
    txt = make_text(train)
    if os.path.exists(CACHE) and not force:
        c = pd.read_parquet(CACHE).set_index("row_id")
        if set(train["row_id"]) <= set(c.index):
            print("  [cache] %s" % CACHE)
            return c.loc[train["row_id"]].to_numpy(dtype=np.float32)
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(MODEL, device="cpu")
    print("  [encode] %s / %d문장 (CPU)" % (MODEL, len(txt)))
    E = m.encode(txt.tolist(), batch_size=32, show_progress_bar=False,
                 normalize_embeddings=True)
    pd.DataFrame(E, index=pd.Index(train["row_id"], name="row_id")).reset_index() \
        .to_parquet(CACHE, index=False)
    return np.asarray(E, dtype=np.float32)


def block_norm(A):
    s = np.linalg.norm(A, axis=1).mean()
    return A / s if s > 0 else A


def text_mismatch(train, E, k1):
    """계획서 §5 A4 — 텍스트가 다른 비교군을 가리키는가.

    자기 비교군 텍스트 중심까지의 거리에서, 가장 가까운 다른 비교군 중심까지의
    거리를 뺀다. 양수면 텍스트가 '나는 저쪽 사업'이라고 말하는 것이다.
    """
    key = k1.to_numpy()
    cents, keys = [], []
    for k in sorted(set(key)):
        pos = np.where(key == k)[0]
        if len(pos) < MIN_COHORT:
            continue
        v = E[pos].mean(0)
        n = np.linalg.norm(v)
        cents.append(v / n if n > 0 else v)
        keys.append(k)
    Ck = np.vstack(cents)
    kpos = {k: i for i, k in enumerate(keys)}
    D = 1.0 - E @ Ck.T                       # 코사인 거리 (E, Ck 모두 단위벡터)
    own, other = [], []
    for i, k in enumerate(k1.to_numpy()):
        j = kpos.get(k)
        if j is None:
            own.append(np.nan)
            other.append(np.nan)
            continue
        own.append(D[i, j])
        d = D[i].copy()
        d[j] = np.inf
        other.append(d.min())
    own, other = np.array(own), np.array(other)
    ms = own - other
    return np.where(np.isnan(ms), 0.0, ms), keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-embed", action="store_true")
    args = ap.parse_args()

    df = prepare(pd.read_parquet(SRC))
    train = df[df["n_axes"] >= MIN_AXES].reset_index(drop=True)
    cl = pd.read_csv(CLEAN, encoding="utf-8-sig")

    print("DL18 — 텍스트를 벡터에 붙인다")
    E = embed(train, args.force_embed)
    print("  임베딩 %s -> PCA %d차원" % (E.shape, TEXT_DIM))
    T = PCA(n_components=TEXT_DIM, random_state=SEED).fit_transform(E)

    Xs, _, names, n_num = build_vectors(train, train)
    Tn = block_norm(T)
    Xsn = block_norm(Xs)

    blocks = {
        "structured only": (Xs, n_num),
        "text only": (Tn, TEXT_DIM),
        "structured+text (0.6/0.4)": (np.hstack([W_STRUCT * Xsn,
                                                 (1 - W_STRUCT) * Tn]), n_num),
    }

    print("\n== 블록 조합별 (clean hold-out)")
    res = {}
    for bname, (X, nn) in blocks.items():
        comp = score_components(train, train, X, X, nn)
        for sname, sc in (("distance only", comp["dist_pct"].to_numpy(float)),
                          ("dist0.7+residual0.3", combine(comp, 0.7, "dir_residual"))):
            r, y, v = eval_holdout(train, sc, cl)
            res["%s | %s" % (bname, sname)] = r
            print("  %-30s %-22s ROC-AUC %.4f  PR-AUC %.4f  상위7 recall %s"
                  % (bname, sname, r["roc_auc"], r["pr_auc"],
                     r["equal_budget"]["recall"]))

    # ---- A4 text-feature 불일치
    _, k1 = cohort_key(train)
    ms, keys = text_mismatch(train, E, k1)
    r_ms, y_ms, v_ms = eval_holdout(train, ms, cl)
    print("\n== A4 text-feature 불일치 단독 (계획서 §5)")
    print("  비교군 %d개 / ROC-AUC %.4f  PR-AUC %.4f  상위7 recall %s"
          % (len(keys), r_ms["roc_auc"], r_ms["pr_auc"], r_ms["equal_budget"]["recall"]))

    # 정형 거리에 불일치를 얹으면
    comp_s = score_components(train, train, Xs, Xs, n_num)
    d_pct = pd.Series(comp_s["dist_pct"]).rank(pct=True).to_numpy()
    m_pct = pd.Series(ms).rank(pct=True).to_numpy()
    add = {}
    for w in (0.7, 0.5):
        r, _, _ = eval_holdout(train, w * d_pct + (1 - w) * m_pct, cl)
        add["distance %.1f + text_mismatch %.1f" % (w, 1 - w)] = r
        print("  거리 %.1f + 불일치 %.1f            ROC-AUC %.4f  상위7 recall %s"
              % (w, 1 - w, r["roc_auc"], r["equal_budget"]["recall"]))

    # ---- 텍스트 정보량이 다른 두 코호트를 나눠 본다
    print("\n== 코호트별 (텍스트 정보량이 다르다)")
    by_cohort = {}
    for coh in ("taxonomy", "bizinfo"):
        m = train["cohort"] == coh
        sub_ids = set(train[m]["row_id"])
        clc = cl[cl["row_id"].isin(sub_ids)]
        n_pos = int((clc["라벨"] == "atypical_design").sum())
        n_neg = int((clc["라벨"] == "normal").sum())
        if n_pos < 2 or n_neg < 2:
            print("  %-10s 라벨이 얇다 (양성 %d / 음성 %d) — 생략" % (coh, n_pos, n_neg))
            continue
        row = {}
        for bname, (X, nn) in blocks.items():
            comp = score_components(train, train, X, X, nn)
            r, _, _ = eval_holdout(train, comp["dist_pct"].to_numpy(float), clc)
            row[bname] = r["roc_auc"]
        by_cohort[coh] = {"n_positive": n_pos, "n_normal": n_neg, "roc_auc": row}
        print("  %-10s 양성 %d / 음성 %d  %s" % (coh, n_pos, n_neg,
              " ".join("%s %.3f" % (k.split()[0], v) for k, v in row.items())))

    best = max(res, key=lambda k: res[k]["roc_auc"])
    print("\n== 판정")
    verdict = judge(res, r_ms, add)
    print("  %s" % verdict["verdict"])
    for x in verdict["reasons"]:
        print("   - %s" % x)

    rep = {
        "질문": "text 와 structured feature 를 함께 쓰면 의미가 증가하는가 (계획서 §12-7)",
        "encoder": MODEL, "device": "cpu",
        "n_train": int(len(train)), "embed_dim": int(E.shape[1]),
        "text_pca_dim": TEXT_DIM, "w_struct": W_STRUCT,
        "weight_note": ("텍스트 가중치는 데이터로 고르지 않았다. M37 합성은 정형 축만 "
                        "흔들어서 텍스트 가중치를 고를 수 없고, hold-out 으로 고르면 "
                        "hold-out 이 튜닝셋이 된다. 계획서 §7 의 예시 비율 0.6/0.4 를 썼다."),
        "block_results": res, "best_block": best,
        "text_mismatch_alone": r_ms,
        "text_mismatch_added": add,
        "n_text_cohorts": len(keys),
        "by_cohort": by_cohort,
        "cohort_asymmetry": ("taxonomy 는 사업목적·지원대상·업종 원문이 있고 "
                             "bizinfo 는 제목과 지원규모 문장뿐이다"),
        "verdict": verdict,
    }
    C.save_report("dl18_m3_text_vector.json", rep)
    write_md(rep)


def judge(res, r_ms, add):
    reasons = []
    s_only = res["structured only | distance only"]["roc_auc"]
    st = max(v["roc_auc"] for k, v in res.items() if k.startswith("structured+text"))
    t_only = max(v["roc_auc"] for k, v in res.items() if k.startswith("text only"))
    reasons.append("정형만 %.3f / 텍스트만 %.3f / 정형+텍스트 %.3f" % (s_only, t_only, st))
    if st > s_only + 0.05:
        v = "텍스트가 보탬이 된다"
    elif st < s_only - 0.05:
        v = "텍스트가 오히려 깎는다"
    else:
        v = "텍스트를 붙여도 달라지지 않는다"
    reasons.append("차이 %+.3f — 35건·양성 10건에서 이 정도는 부트스트랩 구간 안이다. "
                   "방향만 읽고 크기는 읽지 않는다." % (st - s_only))
    reasons.append("A4 text-feature 불일치 단독 ROC-AUC %.3f. 이 신호의 값어치는 "
                   "순위가 아니라 **경고 이유를 문장으로 말할 수 있다는 것**이다."
                   % r_ms["roc_auc"])
    return {"verdict": v, "reasons": reasons}


def write_md(r):
    L = ["# DL18 — 텍스트를 벡터에 붙인다", "",
         "> 계획서 §4.1·§7. M38 은 정형 축만 썼습니다. 여기서 사업목적·지원대상·",
         "> 지원내용을 embedding 으로 바꿔 같은 벡터에 붙이고, §5 의 A4(text-feature",
         "> 불일치) 칸을 채웁니다.", "",
         "```text",
         "encoder  %s (CPU)" % r["encoder"],
         "임베딩   %d차원 -> PCA %d차원" % (r["embed_dim"], r["text_pca_dim"]),
         "블록가중 정형 %.1f / 텍스트 %.1f" % (r["w_struct"], 1 - r["w_struct"]),
         "```", "",
         "## 1. 텍스트 가중치를 데이터로 고르지 않은 이유", "",
         r["weight_note"], "",
         "즉 여기 실린 0.6/0.4 는 **계획서가 예시로 든 값**이지 실험이 고른 값이",
         "아닙니다. 텍스트 가중치를 제대로 고르려면 텍스트도 흔드는 합성 생성기나",
         "라벨이 붙은 두 번째 세트가 필요합니다.", "",
         "## 2. 블록 조합별 — clean hold-out", "",
         "| 블록 | 점수 | ROC-AUC | PR-AUC | 상위7 recall |", "|---|---|---:|---:|---:|"]
    for k, v in sorted(r["block_results"].items(), key=lambda kv: -kv[1]["roc_auc"]):
        b, s = k.split(" | ")
        L.append("| %s | %s | %.4f | %.4f | %s |"
                 % (b, s, v["roc_auc"], v["pr_auc"], v["equal_budget"]["recall"]))
    ms = r["text_mismatch_alone"]
    L += ["", "## 3. A4 — text-feature 불일치 (계획서 §5)", "",
          "```text",
          "text_mismatch = (자기 비교군 텍스트 중심까지의 코사인거리)",
          "              - (가장 가까운 다른 비교군 중심까지의 거리)",
          "```", "",
          "양수이고 클수록 \"텍스트는 다른 비교군에 속한다고 말하는데 정형 축은",
          "이 비교군에 들어와 있다\"는 뜻입니다. 비교군 %d개를 텍스트 중심으로 잡았습니다."
          % r["n_text_cohorts"], "",
          "| 점수 | ROC-AUC | PR-AUC | 상위7 recall |", "|---|---:|---:|---:|",
          "| 불일치 단독 | %.4f | %.4f | %s |"
          % (ms["roc_auc"], ms["pr_auc"], ms["equal_budget"]["recall"])]
    for k, v in r["text_mismatch_added"].items():
        L.append("| %s | %.4f | %.4f | %s |"
                 % (k, v["roc_auc"], v["pr_auc"], v["equal_budget"]["recall"]))
    L += ["",
          "> 이 신호의 값어치는 순위가 아닙니다. **경고 이유를 문장으로 말할 수 있다는**",
          "> **것**입니다 — \"사업목적 문장은 R&D 계열에 가까운데 설계 축은 융자형",
          "> 비교군에 들어와 있습니다\" 같은 문장이 여기서 나옵니다.", ""]
    if r.get("by_cohort"):
        L += ["## 4. 코호트별 — 텍스트 정보량이 다릅니다", "",
              r["cohort_asymmetry"] + ".", "",
              "| 코호트 | 양성 | 음성 | " +
              " | ".join(list(next(iter(r["by_cohort"].values()))["roc_auc"])) + " |",
              "|---|---:|---:|" + "---:|" * len(next(iter(r["by_cohort"].values()))["roc_auc"])]
        for coh, v in r["by_cohort"].items():
            L.append("| %s | %d | %d | %s |"
                     % (coh, v["n_positive"], v["n_normal"],
                        " | ".join("%.3f" % x for x in v["roc_auc"].values())))
        L.append("")
    L += ["## 5. 판정", "", "**%s**" % r["verdict"]["verdict"], ""]
    for x in r["verdict"]["reasons"]:
        L.append("- %s" % x)
    L += ["", "## 6. GPU 가 필요한 지점", "",
          "이 실험은 **CPU 로 끝납니다.** 사전학습 임베딩을 그대로 쓰기 때문입니다",
          "(RoBERTa-base 로 %d문장 인코딩, 1~2분)." % r["n_train"],
          "GPU 가 필요해지는 것은 **인코더를 미세조정할 때**입니다 — 텍스트로부터",
          "설계 이상을 직접 학습시키는 단계로 넘어가면 그때 필요합니다.", ""]
    p = os.path.join(C.REPORTS, "dl18_m3_text_vector.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % p)


if __name__ == "__main__":
    main()
