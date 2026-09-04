r"""M72 — 본문을 semantic embedding 으로 표현하면 M69 를 넘는가.

지시서(사용자, `model2_semantic_body_embedding_experiment_plan.md`):

    M69 는 본문 텍스트가 지원규모 신호를 가진다는 것을 보였고(0.4117 -> 0.3719),
    M70 은 TF-IDF 표현을 아무리 조정해도 추가 개선이 없다는 것을 보였다.
    그렇다면 본문의 **의미**를 sentence embedding 으로 표현하면 TF-IDF/SVD 보다
    지원규모 신호를 더 잘 잡는가. frozen embedding 으로 먼저 확인하고,
    개선이 있을 때만 fine-tuning 을 검토한다.

바꾸지 않는 것 — M69 와 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    모델       m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    구조화     M69 G 단계 feature 층 전체
    제목       제목 TF-IDF -> SVD64
    마스킹     금액 표현 -> [AMOUNT], 남은 숫자 -> '#'  (지시서 3장 고정 규칙)

바뀌는 것은 **본문을 어떤 벡터로 만드는가** 하나다.

## phase (지시서 9장)

    Phase 0  M69 baseline 재현 (실패하면 중단)
    Phase 1  semantic replacement — 본문 TF-IDF 를 빼고 embedding 만
    Phase 2  hybrid — 본문 TF-IDF + embedding 둘 다
    Phase 3  pooling 비교 — head / chunk_mean / section
    Phase 4  차원 비교 — raw 768 / SVD128 / SVD64

각 phase 는 직전 승자 위에서만 다음 축을 움직인다. 승자는 program_stem OOF MAE.

## 승자를 어떻게 읽어야 하는가

M70 에서 배운 것을 그대로 적용한다 — phase 마다 최소 MAE 를 뽑는 절차는
**차이가 없을 때도 반드시 승자를 만든다.** 그래서 모든 표에 95% CI 와 fold
승수를 같이 싣고, 후보 전체가 baseline 주변 어느 폭에 있는지(노이즈 대역)를
따로 계산한다. 판정 근거는 승자 이름이 아니라 그 두 칸이다.

## 누수 (지시서 2장)

    임베딩    frozen pretrained 인코더의 텍스트 함수다. y 를 본 적이 없다.
    차원축소  데이터에 적합하는 변환이라 **fold train 안에서만** fit 한다
    마스킹    M69 것을 그대로 받아 쓴다 — 여기서 다시 마스킹하지 않는다

산출
    ml/data/processed/m72_semantic_oof.parquet
    ml/reports/m72_m2_semantic_body.json / .md
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
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
# -------------------------------------------------------------------------

import os
import pickle
import re
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_semantic_body as SB
import m2_source_features as SF
import m45_m2_amount as M45

SRC = F6.OUT_V2
M69_OOF = os.path.join(C.PROC, "m69_source_oof.parquet")
OUT_OOF = os.path.join(C.PROC, "m72_semantic_oof.parquet")
MD = C.report_path("m72_m2_semantic_body.md")

M69_PUBLISHED = {"MAE_log10": 0.3719, "within_2x": 0.530, "within_3x": 0.723,
                 "strict_MAE": 0.3931, "taxonomy": 0.3292, "bizinfo": 0.4182}
BASELINE = "M69 baseline"
BODY_SVD = 64
GOAL_1, GOAL_2 = 0.35, 0.30
REPRO_TOL = 1e-9


# ------------------------------------------------------------ 규격
def spec(name, tfidf=True, semantic=None, pooling="chunk_mean",
         encoder=SB.PRIMARY, dim="raw"):
    """한 실험의 본문 표현 규격.

    tfidf     M69 의 본문 char TF-IDF -> SVD64 를 쓰는가
    semantic  embedding 을 쓰는가 (None 이면 안 씀)
    dim       'raw' | 128 | 64  — embedding 차원 축소 (fold train 에서만 fit)
    """
    return {"name": name, "tfidf": bool(tfidf), "semantic": bool(semantic),
            "pooling": pooling, "encoder": encoder, "dim": dim}


def spec_label(s):
    p = ["TF-IDF" if s["tfidf"] else "TF-IDF 없음"]
    if s["semantic"]:
        p.append("%s/%s/%s" % (s["encoder"].split("/")[-1], s["pooling"], s["dim"]))
    else:
        p.append("semantic 없음")
    return " + ".join(p)


def fit_body_tfidf(train_body, test_body):
    """M69 와 같은 규격 — char_wb 2~3gram, min_df 3, 30k, SVD64."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=3,
                        max_features=30000, sublinear_tf=True)
    A = v.fit_transform(train_body)
    svd = TruncatedSVD(n_components=BODY_SVD, random_state=F.PIPELINE_SEED)
    return svd.fit_transform(A), svd.transform(v.transform(test_body)), (v, svd)


def reduce_semantic(Etr, Ete, dim):
    """embedding 차원 축소. **fold train 에서만 fit** 한다 (지시서 8장)."""
    if dim == "raw":
        return Etr, Ete, None, None
    from sklearn.decomposition import TruncatedSVD

    k = int(min(int(dim), Etr.shape[1] - 1, Etr.shape[0] - 1))
    svd = TruncatedSVD(n_components=k, random_state=F.PIPELINE_SEED)
    return (svd.fit_transform(Etr), svd.transform(Ete), svd,
            float(svd.explained_variance_ratio_.sum()))


def assemble(s, Xs, titles, body, NB, E, tr, te):
    """열 순서는 M69 그대로 — [구조화+제목] + [NB] + [본문TF-IDF] + [semantic].

    M70 에서 이 순서를 바꿨다가 baseline 재현이 깨졌다(colsample_bytree=0.8 이라
    열 순서가 각 트리의 열 집합을 바꾼다). semantic 은 **맨 뒤에만** 붙인다.
    """
    cols = SF.columns_upto("G")
    a0, b0, _ = F.build_features(Xs, titles, tr, te, True, True)
    parts_tr = [a0.reset_index(drop=True), NB.iloc[tr][cols].reset_index(drop=True)]
    parts_te = [b0.reset_index(drop=True), NB.iloc[te][cols].reset_index(drop=True)]
    arts, evr = [], None
    if s["tfidf"]:
        ta, tb, art = fit_body_tfidf(body[tr], body[te])
        names = ["body_svd%02d" % i for i in range(ta.shape[1])]
        parts_tr.append(pd.DataFrame(ta, columns=names))
        parts_te.append(pd.DataFrame(tb, columns=names))
        arts.append(art)
    if s["semantic"]:
        ea, eb, red, evr = reduce_semantic(E[tr], E[te], s["dim"])
        names = ["sem%03d" % i for i in range(ea.shape[1])]
        parts_tr.append(pd.DataFrame(ea, columns=names))
        parts_te.append(pd.DataFrame(eb, columns=names))
        if red is not None:
            arts.append(red)
    return (pd.concat(parts_tr, axis=1), pd.concat(parts_te, axis=1), arts, evr)


def run_config(s, Xs, y, groups, titles, body, NB, embeddings):
    from sklearn.model_selection import GroupKFold

    E = embeddings[(s["encoder"], s["pooling"])] if s["semantic"] else None
    n = len(y)
    pred = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    per_fold, evrs, art_bytes, ncols = [], [], 0, 0
    t0 = time.time()
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fold_id[te] = i
        Xtr, Xte, arts, evr = assemble(s, Xs, titles, body, NB, E, tr, te)
        pred[te] = F.make_point_model().fit(Xtr, y[tr]).predict(Xte)
        per_fold.append(round(float(np.abs(pred[te] - y[te]).mean()), 4))
        if evr is not None:
            evrs.append(evr)
        if i == 0:
            art_bytes = len(pickle.dumps(arts, protocol=pickle.HIGHEST_PROTOCOL))
            ncols = int(Xtr.shape[1])
    return {"pred": pred, "fold_id": fold_id, "per_fold_MAE": per_fold,
            "seconds": round(time.time() - t0, 1),
            "n_features": ncols,
            "reducer_explained_variance": (round(float(np.mean(evrs)), 4) if evrs else None),
            "fitted_artifact_bytes": int(art_bytes)}


# ------------------------------------------------------------ 지표
def paired_test(y, p_new, p_old):
    from scipy import stats

    e_new, e_old = np.abs(p_new - y), np.abs(p_old - y)
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n_better": int((d < 0).sum()), "n_worse": int((d > 0).sum())}


BASE_FOLDS = []


def summarize(s, run, y, d, base_pred):
    met = M45.point_metrics(y, run["pred"])
    met["name"] = s["name"]
    met["spec"] = spec_label(s)
    met["per_fold_MAE"] = run["per_fold_MAE"]
    met["fold_std"] = round(float(np.std(run["per_fold_MAE"])), 4)
    met["cohort"] = {}
    for k in ("taxonomy", "bizinfo"):
        m = (d["cohort"] == k).to_numpy()
        met["cohort"][k] = {"n": int(m.sum()),
                            "MAE_log10": round(float(np.abs(run["pred"][m] - y[m]).mean()), 4)}
    for k in ("seconds", "n_features", "reducer_explained_variance",
              "fitted_artifact_bytes"):
        met[k] = run[k]
    if base_pred is not None:
        met["vs_baseline"] = paired_test(y, run["pred"], base_pred)
        met["fold_wins"] = int(sum(1 for a, b in zip(run["per_fold_MAE"], BASE_FOLDS)
                                   if a < b))
    return met


def noise_band(results, baseline_name):
    """후보 전체가 baseline 주변 어느 폭에 있는가 (M70 에서 세운 해석 규율)."""
    b = results[baseline_name]["MAE_log10"]
    deltas = {k: round(m["MAE_log10"] - b, 4)
              for k, m in results.items() if k != baseline_name}
    cross = {k: bool(m["vs_baseline"]["ci95"][0] < 0 < m["vs_baseline"]["ci95"][1])
             for k, m in results.items() if k != baseline_name}
    return {"baseline_MAE": b, "n_candidates": len(deltas),
            "delta_range": [min(deltas.values()), max(deltas.values())] if deltas else None,
            "n_with_ci_crossing_zero": int(sum(cross.values())),
            "n_significant": int(len(cross) - sum(cross.values())),
            "deltas": deltas}


def verdict_case(best_name, res, strict, baseline_name):
    """지시서 14장 Case A~D."""
    if best_name == baseline_name:
        return "D", ("semantic embedding 도 M69 를 넘지 못했다 — 모델 2 추가 표현 "
                     "실험 종료, M69 0.3719 를 최종 기준으로 확정")
    mae = res[best_name]["MAE_log10"]
    kept = strict[best_name]["MAE_log10"] < strict[baseline_name]["MAE_log10"]
    if mae < 0.35 and kept:
        return "A", "새 canonical 후보"
    if not kept:
        return "C", "program_stem 에서만 개선되고 strict split 에서 사라짐 — reject"
    if 0.35 <= mae <= 0.37:
        return "B", "통계적으로 일관되면 serving 비용 대비 승격 여부 검토"
    return "D", "semantic embedding 도 M69 를 의미 있게 넘지 못했다"


def main():
    t_all = time.time()
    print("== 데이터 — M69 와 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16],
                                      fp["rows_after_filters"]))
    NB, body, src = SF.build(d)

    # ---------------------------------------------- 임베딩 (지시서 2·3장)
    print("\n== 0. frozen embedding 생성 (지시서 6·7장)")
    embeddings, emb_meta = {}, {}
    for enc in SB.ENCODERS:
        for pool in SB.POOLINGS:
            V, info = SB.embed(body, enc, pool, verbose=False)
            embeddings[(enc, pool)] = V.astype(np.float32)
            emb_meta["%s|%s" % (enc.split("/")[-1], pool)] = {
                "dim": int(V.shape[1]), "cached": info["cached"],
                "generation_seconds": info["seconds"]}
            print("   %-34s %-11s dim=%d  생성 %.0f초%s"
                  % (enc.split("/")[-1], pool, V.shape[1], info["seconds"],
                     " (캐시)" if info["cached"] else ""))

    print("\n== 0-2. 누수 점검 (지시서 2장)")
    # 십진 숫자(정규식 \d)와 '숫자처럼 생긴 글자'(str.isdigit)를 나눠 센다.
    #
    # 처음에는 `any(ch.isdigit())` 하나로 셌더니 730/1877 이 나와 FAIL 이 떴다.
    # 전부 원문자 목록 표기(①②③⑤ …)였다 — `str.isdigit()` 은 유니코드 No 범주까지
    # True 를 돌려주기 때문이다. 원문자는 **자릿수 정보를 담지 않는** 문단 번호라
    # 누수가 아니다. M70 이 같은 자리를 정규식 `\d` 로 쟀고 0/1877 이었다.
    # 판정은 M70 과 같은 기준(십진 숫자)으로 하고, 원문자 수는 참고로 남긴다.
    decimal = sum(1 for b in body if re.search(r"\d", b))
    marker = sum(1 for b in body if any(ch.isdigit() for ch in b)) - decimal
    amounts = sum(1 for b in body if F.AMOUNT_IN_TITLE.search(b))
    leak = {
        "임베딩 입력": "M69 마스킹 본문 그대로 — 이 실험은 마스킹을 다시 하지 않는다",
        "마스킹 본문에 십진 숫자가 남은 행": "%d / %d — 0 이어야 한다" % (decimal, len(body)),
        "마스킹 본문에 금액 표현이 남은 행": "%d / %d — 0 이어야 한다" % (amounts, len(body)),
        "원문자(①②③) 만 있는 행": "%d / %d — 문단 번호라 자릿수 정보가 없다. 누수 아님"
                             % (marker, len(body)),
        "인코더": "frozen pretrained. y 를 본 적이 없다 (fine-tuning 없음)",
        "차원 축소기": "fold train 에서만 fit (지시서 8장)",
        "타깃을 임베딩 생성에 사용": "없음",
        "임베딩 생성 장치": "GPU(RTX 4090) forward. CPU 대비 최대 절대차 1.8e-06 · "
                     "코사인 유사도 최소 0.9999998 로 실측 — 값이 아니라 속도만 다르다",
        "XGBoost 장치": "CPU tree_method=hist 그대로. 트리를 GPU 로 옮기면 histogram "
                      "binning 이 달라져 M65 부터의 비교가 깨진다",
    }
    for k, v in leak.items():
        print("   %-34s %s" % (k, v))
    leak_pass = decimal == 0 and amounts == 0

    # ---------------------------------------------- Phase 0 재현
    print("\n== 1. M69 baseline 재현 (지시서 9장 Phase 0)")
    base_spec = spec(BASELINE, tfidf=True, semantic=False)
    run0 = run_config(base_spec, Xs, y, groups["program_stem"], titles, body, NB,
                      embeddings)
    global BASE_FOLDS
    BASE_FOLDS = run0["per_fold_MAE"]
    base = summarize(base_spec, run0, y, d, None)
    m69 = pd.read_parquet(M69_OOF)
    same = bool(np.allclose(run0["pred"], m69["pred_G"].to_numpy(), atol=REPRO_TOL))
    print("   MAE %.4f (기대 %.4f) · 2배내 %.1f%% · 3배내 %.1f%%"
          % (base["MAE_log10"], M69_PUBLISHED["MAE_log10"],
             100 * base["within_2x"], 100 * base["within_3x"]))
    print("   M69 저장 OOF 와 행 단위 일치: %s" % same)
    if not (same and abs(base["MAE_log10"] - M69_PUBLISHED["MAE_log10"]) < 5e-4):
        print("   재현 실패 — 지시서 9장에 따라 중단한다.")
        C.save_report("m72_m2_semantic_body.json",
                      {"verdict": "baseline 재현 실패 — 중단", "baseline": base,
                       "expected": M69_PUBLISHED, "row_level_match": same})
        return None
    print("   재현 통과 — semantic 축을 연다.")

    # ---------------------------------------------- phase 정의
    phases = [
        ("Phase 1 — semantic replacement", lambda w: [
            spec("A1 semantic만", tfidf=False, semantic=True,
                 pooling=w["pooling"], encoder=w["encoder"], dim=w["dim"])]),
        ("Phase 2 — hybrid", lambda w: [
            spec("B1 TF-IDF+semantic", tfidf=True, semantic=True,
                 pooling=w["pooling"], encoder=w["encoder"], dim=w["dim"])]),
        ("Phase 3 — pooling", lambda w: [
            spec("C1 head", tfidf=w["tfidf"], semantic=True, pooling="head",
                 encoder=w["encoder"], dim=w["dim"]),
            spec("C2 section", tfidf=w["tfidf"], semantic=True, pooling="section",
                 encoder=w["encoder"], dim=w["dim"])]),
        ("Phase 4 — embedding 차원", lambda w: [
            spec("D1 SVD128", tfidf=w["tfidf"], semantic=True, pooling=w["pooling"],
                 encoder=w["encoder"], dim=128),
            spec("D2 SVD64", tfidf=w["tfidf"], semantic=True, pooling=w["pooling"],
                 encoder=w["encoder"], dim=64)]),
    ]

    results = {BASELINE: base}
    preds = {BASELINE: run0["pred"]}
    specs = {BASELINE: base_spec}
    winner, wspec = BASELINE, dict(base_spec, semantic=True)   # phase1 이 켤 기본값
    phase_log = []
    for title, maker in phases:
        print("\n== %s  (직전 승자: %s)" % (title, winner))
        entries = maker(wspec)
        for s in entries:
            specs[s["name"]] = s
            run = run_config(s, Xs, y, groups["program_stem"], titles, body, NB,
                             embeddings)
            met = summarize(s, run, y, d, run0["pred"])
            results[s["name"]] = met
            preds[s["name"]] = run["pred"]
            v = met["vs_baseline"]
            print("   %-20s MAE %.4f (foldσ %.4f)  Δ%+0.4f  CI[%+0.4f,%+0.4f]  "
                  "p=%-9s fold승 %d/5  열 %d  %ds"
                  % (s["name"], met["MAE_log10"], met["fold_std"], v["delta_MAE"],
                     v["ci95"][0], v["ci95"][1], v["wilcoxon_p"], met["fold_wins"],
                     met["n_features"], met["seconds"]))
        pool = [winner] + [s["name"] for s in entries]
        new = min(pool, key=lambda k: results[k]["MAE_log10"])
        phase_log.append({"phase": title, "carried_in": winner,
                          "candidates": [s["name"] for s in entries], "winner": new,
                          "MAE": {k: results[k]["MAE_log10"] for k in pool},
                          "changed": bool(new != winner)})
        if new != winner:
            winner = new
            wspec = specs[new]
            print("   -> 승자 교체: %s" % winner)
        else:
            # baseline 을 못 넘었어도 다음 phase 는 semantic 축 위에서 계속한다.
            # 아니면 Phase 3~5 가 통째로 실행되지 않아 '어느 pooling·차원에서도
            # 안 되더라'를 말할 수 없게 된다.
            best_sem = min([s["name"] for s in entries],
                           key=lambda k: results[k]["MAE_log10"])
            wspec = specs[best_sem]
            print("   -> 유지: %s (후보가 baseline 을 못 넘음). "
                  "다음 phase 는 semantic 최고 %s 위에서 계속" % (winner, best_sem))

    # ---------------------------------------------- 보조 인코더 대조
    print("\n== 보조 인코더 대조 (지시서 6장 B) — 한 모델 탓이 아닌지 확인")
    alt = spec("E1 KoSimCSE", tfidf=wspec["tfidf"], semantic=True,
               pooling=wspec["pooling"], encoder=SB.SECONDARY, dim=wspec["dim"])
    specs[alt["name"]] = alt
    run = run_config(alt, Xs, y, groups["program_stem"], titles, body, NB, embeddings)
    results[alt["name"]] = summarize(alt, run, y, d, run0["pred"])
    preds[alt["name"]] = run["pred"]
    v = results[alt["name"]]["vs_baseline"]
    print("   %-20s MAE %.4f  Δ%+0.4f  CI[%+0.4f,%+0.4f]  fold승 %d/5"
          % (alt["name"], results[alt["name"]]["MAE_log10"], v["delta_MAE"],
             v["ci95"][0], v["ci95"][1], results[alt["name"]]["fold_wins"]))
    if results[alt["name"]]["MAE_log10"] < results[winner]["MAE_log10"]:
        winner, wspec = alt["name"], alt

    # ---------------------------------------------- 엄격 split
    strict_names = sorted({BASELINE, winner} | {p["winner"] for p in phase_log}
                          | {"B1 TF-IDF+semantic"}, key=list(results).index)
    print("\n== 엄격 split [normalized_title] — %s" % ", ".join(strict_names))
    strict = {}
    for name in strict_names:
        r = run_config(specs[name], Xs, y, groups["normalized_title"], titles, body,
                       NB, embeddings)
        strict[name] = {
            "MAE_log10": round(float(np.abs(r["pred"] - y).mean()), 4),
            "per_fold_MAE": r["per_fold_MAE"],
            "cohort": {k: round(float(np.abs(r["pred"][(d["cohort"] == k).to_numpy()]
                                             - y[(d["cohort"] == k).to_numpy()]).mean()), 4)
                       for k in ("taxonomy", "bizinfo")}}
        print("   %-20s strict MAE %.4f" % (name, strict[name]["MAE_log10"]))

    # ---------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 승자를 한 번 더")
    again = run_config(specs[winner], Xs, y, groups["program_stem"], titles, body,
                       NB, embeddings)
    repro = bool(np.allclose(again["pred"], preds[winner], atol=REPRO_TOL))
    print("   %s 재실행 OOF 일치: %s" % (winner, repro))

    # ---------------------------------------------- 서빙 비용 (지시서 10·11장)
    print("\n== 서빙 비용")
    enc_bytes = SB.encoder_artifact_bytes(specs[winner].get("encoder", SB.PRIMARY))
    lat = SB.inference_latency(specs[winner].get("encoder", SB.PRIMARY),
                               specs[winner].get("pooling", "chunk_mean"),
                               sample=list(body[:32]))
    serving = {
        "encoder_MB": round(enc_bytes / 1e6, 1) if enc_bytes > 0 else None,
        "fitted_artifact_MB": round(results[winner]["fitted_artifact_bytes"] / 1e6, 1),
        "baseline_fitted_artifact_MB": round(base["fitted_artifact_bytes"] / 1e6, 1),
        "latency": lat,
        "m69_needs_encoder": False,
    }
    print("   인코더 %s MB · 적합 artifact %.1f MB (baseline %.1f MB)"
          % (serving["encoder_MB"], serving["fitted_artifact_MB"],
             serving["baseline_fitted_artifact_MB"]))
    print("   인코딩 지연 %.1f ms/문서 (덩어리 %.1f ms) — CPU"
          % (lat["ms_per_doc"], lat["ms_per_chunk"]))

    # ---------------------------------------------- 판정
    nb = noise_band(results, BASELINE)
    W = results[winner]
    v = W.get("vs_baseline")
    checks = {
        "1. OOF MAE 가 0.3719 보다 감소": W["MAE_log10"] < base["MAE_log10"],
        "2. strict split 에서도 같은 방향":
            strict.get(winner, {}).get("MAE_log10", 9) < strict[BASELINE]["MAE_log10"],
        "3. 5개 fold 중 4개 이상 개선": W.get("fold_wins", 0) >= 4,
        "4. paired CI 가 0 아래": bool(v and v["ci95"][1] < 0),
        "5. taxonomy / bizinfo 양쪽 다 악화되지 않음": all(
            W["cohort"][k]["MAE_log10"] <= base["cohort"][k]["MAE_log10"] + 1e-4
            for k in ("taxonomy", "bizinfo")),
        "6. leakage audit PASS": bool(leak_pass),
        "7. reproducibility PASS": bool(repro),
        "8. serving latency / artifact 증가가 감당 가능": bool(
            serving["encoder_MB"] is None or serving["encoder_MB"] <= 500),
    }
    if winner == BASELINE:
        checks = {k: (False if k.startswith(("1.", "2.", "3.", "4.")) else v_)
                  for k, v_ in checks.items()}
    case, action = verdict_case(winner, results, strict, BASELINE)
    verdict = ("승격 후보 (M69 대체)" if (winner != BASELINE and all(checks.values()))
               else "현행 유지 (M69)")
    print("\n== 승격 점검표 (지시서 11장) — 대상: %s" % winner)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   Case %s — %s" % (case, action))
    print("   판정: %s" % verdict)
    print("   노이즈 대역: 후보 %d개 ΔMAE %+0.4f~%+0.4f · CI 가 0 을 가로지른 후보 %d/%d"
          % (nb["n_candidates"], nb["delta_range"][0], nb["delta_range"][1],
             nb["n_with_ci_crossing_zero"], nb["n_candidates"]))

    pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y,
                  "fold": run0["fold_id"], "cohort": d["cohort"].to_numpy(),
                  **{("pred_" + k.split()[0]): p for k, p in preds.items()}}
                 ).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "본문을 semantic embedding 으로 표현하면 M69 0.3719 을 넘는가",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"],
                      "rows": fp["rows_after_filters"], "model": F.XGB_POINT,
                      "structured_layer": "M69 G 단계 전체",
                      "masking": SF.manifest()["text_masking"]},
        "changed": "본문을 어떤 벡터로 만드는가 (TF-IDF/SVD vs sentence embedding)",
        "semantic_layer": SB.manifest(),
        "embeddings": emb_meta,
        "baseline_reproduction": {"MAE_log10": base["MAE_log10"],
                                  "expected": M69_PUBLISHED,
                                  "row_level_match_with_m69_oof": same},
        "leakage_audit": leak, "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "phases": phase_log,
        "results": results,
        "noise_band": nb,
        "strict_split": strict,
        "winner": winner, "winner_spec": specs[winner],
        "reproducibility": repro,
        "serving": serving,
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "case": case, "case_action": action, "verdict": verdict,
        "goals": {"first": GOAL_1, "final": GOAL_2,
                  "first_met": bool(W["MAE_log10"] < GOAL_1),
                  "final_met": bool(W["MAE_log10"] < GOAL_2)},
        "fine_tuning_decision": (
            "frozen embedding 에서 명확한 개선이 있을 때만 검토한다(지시서 12장). "
            "개선이 없으면 fine-tuning 으로 가지 않고 semantic 축을 종료한다."),
        "published_m69": M69_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t_all, 1),
    }
    C.save_report("m72_m2_semantic_body.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t_all))
    return payload


# ------------------------------------------------------------ md
def write_md(p):
    R = p["results"]
    L = []
    A = L.append
    A("# M72 — 본문 semantic embedding\n")
    A("> 질문: **M69 에서 확인된 본문 텍스트 신호를 TF-IDF/SVD 가 아니라 semantic")
    A("> sentence embedding 으로 표현하면, MAE 0.3719 을 의미 있게 낮출 수 있는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("고정      모델(XGB_POINT) · 구조화 feature(%s) · 제목 SVD64 · 마스킹 규칙"
      % u["structured_layer"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("## 1. baseline 재현 (지시서 9장 Phase 0)\n")
    b = p["baseline_reproduction"]
    A("```text")
    A("OOF MAE   %.4f   (기대 %.4f)" % (b["MAE_log10"], b["expected"]["MAE_log10"]))
    A("Within 2x %.1f%%   (기대 %.1f%%)" % (100 * R[BASELINE]["within_2x"],
                                          100 * b["expected"]["within_2x"]))
    A("Within 3x %.1f%%   (기대 %.1f%%)" % (100 * R[BASELINE]["within_3x"],
                                          100 * b["expected"]["within_3x"]))
    A("M69 저장 OOF 와 행 단위 일치: %s" % b["row_level_match_with_m69_oof"])
    A("```\n")
    A("## 2. embedding 생성 (지시서 6·7장)\n")
    A("| 인코더 / pooling | 차원 | 생성 시간(초) |")
    A("|---|---:|---:|")
    for k, v in p["embeddings"].items():
        A("| %s | %d | %.0f |" % (k, v["dim"], v["generation_seconds"]))
    A("")
    sl = p["semantic_layer"]
    A("> 인코더는 전부 **frozen** 입니다(지시서 12장). `%s` 의 `max_seq_length` 가"
      % sl["encoders"]["primary"].split("/")[-1])
    A("> 128 토큰이라 공고문 본문(중앙 2,594자)을 한 번에 넣으면 앞부분만 보고")
    A("> 잘립니다. 그래서 %s\n" % sl["chunk_aggregation"])
    A("## 3. 누수 점검 (지시서 2장)\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v))
    A("| **판정** | **%s** |" % p["leakage_verdict"])
    A("")
    A("## 4. 전체 결과\n")
    A("| 설정 | 규격 | MAE | fold σ | 2배 이내 | 3배 이내 | ΔMAE vs M69 | 95% CI | "
      "wilcoxon p | fold승 | taxonomy | bizinfo | 열 수 | 학습(초) |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, m in R.items():
        v = m.get("vs_baseline")
        A("| %s | %s | %.4f | %.4f | %.1f%% | %.1f%% | %s | %s | %s | %s | %.4f | %.4f | %d | %d |"
          % (k, m["spec"], m["MAE_log10"], m["fold_std"], 100 * m["within_2x"],
             100 * m["within_3x"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             (str(v["wilcoxon_p"]) if v else "—"),
             ("%d/5" % m["fold_wins"]) if v else "—",
             m["cohort"]["taxonomy"]["MAE_log10"], m["cohort"]["bizinfo"]["MAE_log10"],
             m["n_features"], m["seconds"]))
    A("")
    A("### fold별 MAE\n")
    A("| 설정 | " + " | ".join("fold %d" % i for i in range(5)) + " |")
    A("|---|" + "---:|" * 5)
    for k, m in R.items():
        A("| %s | " % k + " | ".join("%.4f" % x for x in m["per_fold_MAE"]) + " |")
    A("")
    nb = p["noise_band"]
    A("## 5. 노이즈 대역 — 승자를 어떻게 읽어야 하는가\n")
    A("```text")
    A("baseline MAE              %.4f" % nb["baseline_MAE"])
    A("후보 %d개의 ΔMAE 범위       %+0.4f ~ %+0.4f"
      % (nb["n_candidates"], nb["delta_range"][0], nb["delta_range"][1]))
    A("95%% CI 가 0 을 가로지른 후보  %d / %d"
      % (nb["n_with_ci_crossing_zero"], nb["n_candidates"]))
    A("0 을 넘지 않은 후보(유의)     %d / %d" % (nb["n_significant"], nb["n_candidates"]))
    A("```\n")
    A("## 6. phase 진행 (지시서 9장)\n")
    A("| phase | 물려받은 승자 | 후보 | 새 승자 | 교체 |")
    A("|---|---|---|---|---|")
    for ph in p["phases"]:
        A("| %s | %s | %s | %s | %s |"
          % (ph["phase"], ph["carried_in"], ", ".join(ph["candidates"]),
             ph["winner"], "예" if ph["changed"] else "아니오"))
    A("")
    A("## 7. 엄격 split (normalized_title)\n")
    A("| 설정 | strict MAE | taxonomy | bizinfo | fold별 |")
    A("|---|---:|---:|---:|---|")
    for k, v in p["strict_split"].items():
        A("| %s | %.4f | %.4f | %.4f | %s |"
          % (k, v["MAE_log10"], v["cohort"]["taxonomy"], v["cohort"]["bizinfo"],
             ", ".join("%.4f" % x for x in v["per_fold_MAE"])))
    A("")
    A("## 8. 서빙 비용 (지시서 10·11장)\n")
    s = p["serving"]
    A("```text")
    A("인코더 artifact       %s MB   (M69 는 인코더가 필요 없다)" % s["encoder_MB"])
    A("fold 적합 artifact    %.1f MB  (baseline %.1f MB)"
      % (s["fitted_artifact_MB"], s["baseline_fitted_artifact_MB"]))
    A("인코딩 지연           %.1f ms/문서 · %.1f ms/덩어리  (CPU)"
      % (s["latency"]["ms_per_doc"], s["latency"]["ms_per_chunk"]))
    A("```\n")
    A("## 9. 승격 점검표 (지시서 11장) — 대상: %s\n" % p["winner"])
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("| 같은 seed 재실행 OOF 일치 | %s |" % p["reproducibility"])
    A("")
    A("## 10. 최종 판정 (지시서 14장)\n")
    A("```text")
    A("Case %s — %s" % (p["case"], p["case_action"]))
    A("")
    A("M69 baseline   MAE %.4f" % R[BASELINE]["MAE_log10"])
    A("M72 최고       MAE %.4f   (%s)" % (R[p["winner"]]["MAE_log10"], p["winner"]))
    A("")
    A("목표  1차 MAE < %.2f -> %s / 최종 < %.2f -> %s"
      % (p["goals"]["first"], "달성" if p["goals"]["first_met"] else "미달",
         p["goals"]["final"], "달성" if p["goals"]["final_met"] else "미달"))
    A("판정: %s" % p["verdict"])
    A("```\n")
    A("### fine-tuning 으로 갈 것인가 (지시서 12장)\n")
    A("> %s\n" % p["fine_tuning_decision"])
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
