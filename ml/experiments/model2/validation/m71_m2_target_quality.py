r"""M71 — M69 의 남은 오차가 모델 한계인가, bizinfo 금액 타깃의 파싱 오류인가.

지시서(사용자, `model2_target_quality_bizinfo_audit_experiment_plan.md`):

    M69 는 코호트별로 taxonomy 0.3292 / bizinfo 0.4182 다. 구조·텍스트 표현을
    더 바꾸지 말고 **bizinfo 금액 타깃이 원문과 일치하는지 점검하고**, 잘못된
    타깃을 수정하거나 불확실한 타깃을 분리한 뒤 M69 를 재학습하라.

## 이 실험에서 제일 조심해야 하는 것 — MAE 를 비교할 수 없게 된다

지시서 9장은 `M69 original 0.3719` 과 `M69 + corrected ?` 를 나란히 놓으라고
적었다. **그대로 하면 안 된다.** 타깃을 고치면 y 가 바뀌고, 바뀐 y 로 잰 MAE 는
원래 y 로 잰 MAE 와 **같은 자를 쓴 값이 아니다.** 타깃을 더 일관되게 만들기만
해도 MAE 는 기계적으로 내려간다 — 모델이 좋아진 것이 하나도 없어도 그렇다.
"오차가 큰 행의 타깃을 예측값 쪽으로 옮기면 MAE 가 준다"는 것은 측정이 아니라
정의다.

그래서 이 스크립트는 비교를 두 겹으로 나눈다.

    고정 평가셋 (판정 근거)
        감사 라벨이 CORRECT 라 **y 가 한 글자도 바뀌지 않은 행**만 모은다
        (taxonomy 969 + bizinfo 809). 네 버전 모두 이 행들에서는 같은 y 를
        같은 fold 로 예측하므로 MAE 가 곧바로 비교된다. 여기서 묻는 것은
        "학습 라벨을 청소하면 **깨끗한 행**을 더 잘 맞히는가"이고, 그것이
        타깃 품질 개선이 모델에 주는 진짜 이득이다.

    전체 셋 (참고 기록)
        지시서 9장이 요구한 표. 다만 V2 는 y 자체가 다르므로 **0.3719 과
        직접 비교할 수 없다**고 표에 적어 둔다.

같은 이유로 V3(필터)·V4(가중)는 **학습에서만** 라벨 품질을 쓰고 평가 행과
평가 타깃은 건드리지 않는다. 그래야 0.3719 과 같은 자로 잰 값이 된다.

## 버전 (지시서 8장)

    V1 baseline    M69 그대로 (재현 게이트)
    V2 corrected   FIXABLE 행의 타깃을 원문 근거로 교정. 학습·평가 모두 y2
    V3 filtered    AMBIGUOUS / NO_EVIDENCE / SEMANTIC_MISMATCH 를 **학습에서** 제외
    V4 weighted    같은 라벨에 가중치 (CORRECT/FIXABLE 1.0, AMBIGUOUS 0.4, 나머지 0)

## 누수 (지시서 11장)

    감사·교정 층(`m2_target_audit`)은 y·예측·오차를 **읽지 않는다.** 규칙을
    발견한 표본은 지시서 3장이 지정한 고오차 행이지만, 규칙 자체는 전부
    텍스트 문맥이다. 그래서 옳고 그름은 '오차가 줄었는가'가 아니라 근거
    문자열로 판정하고, 교정 전/후 근거를 전부 CSV 로 남긴다.

    `amount_parser` 는 고치지 않는다 — 모델 3 이 같은 파서를 쓴다(지시서 1장).

산출
    ml/data/processed/m71_target_audit.parquet      행별 감사 라벨
    ml/reports/m71_target_corrections.csv           교정 전/후 + 근거 (사람 검증용)
    ml/data/processed/m71_target_quality_oof.parquet
    ml/reports/m71_m2_target_quality.json / .md
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
import sys
import time
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_source_features as SF
import m2_target_audit as TA
import m45_m2_amount as M45

SRC = F6.OUT_V2
M69_OOF = os.path.join(C.PROC, "m69_source_oof.parquet")
OUT_AUDIT = os.path.join(C.PROC, "m71_target_audit.parquet")
OUT_OOF = os.path.join(C.PROC, "m71_target_quality_oof.parquet")
OUT_DIFF = C.report_path("m71_target_corrections.csv")
MD = C.report_path("m71_m2_target_quality.md")

M69_PUBLISHED = {"MAE_log10": 0.3719, "within_2x": 0.530, "within_3x": 0.723,
                 "taxonomy": 0.3292, "bizinfo": 0.4182, "strict_MAE": 0.3931}
V1, V2, V3, V4 = "V1 baseline", "V2 corrected", "V3 filtered", "V4 weighted"
VERSIONS = [V1, V2, V3, V4]
BODY_SVD = 64
GOAL_1, GOAL_2 = 0.35, 0.30
REPRO_TOL = 1e-9
TOP_N = (50, 100, 200)


# ------------------------------------------------------------ feature (M69 G)
def fit_body_svd(train_body, test_body):
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=3,
                        max_features=30000, sublinear_tf=True)
    A = v.fit_transform(train_body)
    svd = TruncatedSVD(n_components=BODY_SVD, random_state=F.PIPELINE_SEED)
    return svd.fit_transform(A), svd.transform(v.transform(test_body))


def assemble(Xs, titles, body, NB, tr, te):
    """M69 G 단계와 **같은 열 순서** — [구조화+제목] + [NB] + [본문SVD].

    M70 에서 이 순서를 바꿨다가 baseline 재현이 깨졌다(colsample_bytree=0.8 이라
    열 순서가 각 트리의 열 집합을 바꾼다). 여기서도 순서가 곧 재현 조건이다.
    """
    cols = SF.columns_upto("G")
    a0, b0, _ = F.build_features(Xs, titles, tr, te, True, True)
    ta, tb = fit_body_svd(body[tr], body[te])
    names = ["body_svd%02d" % i for i in range(ta.shape[1])]
    Xtr = pd.concat([a0.reset_index(drop=True),
                     NB.iloc[tr][cols].reset_index(drop=True),
                     pd.DataFrame(ta, columns=names)], axis=1)
    Xte = pd.concat([b0.reset_index(drop=True),
                     NB.iloc[te][cols].reset_index(drop=True),
                     pd.DataFrame(tb, columns=names)], axis=1)
    return Xtr, Xte


def run_version(name, Xs, y_train, y_eval, groups, titles, body, NB,
                keep=None, weight=None):
    """한 버전의 5-fold OOF.

    `y_train` 과 `y_eval` 을 나눈 것이 이 함수의 요점이다. V3/V4 는 학습에서만
    라벨 품질을 쓰고 평가 타깃은 V1 것을 그대로 쓴다 — 그래야 0.3719 과 같은
    자로 잰 값이 된다. `keep` 은 **학습 행만** 걸러낸다(평가 행은 늘 전부).
    """
    from sklearn.model_selection import GroupKFold

    n = len(y_eval)
    pred = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    per_fold, n_train_used = [], []
    t0 = time.time()
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y_eval, groups)):
        fold_id[te] = i
        Xtr, Xte = assemble(Xs, titles, body, NB, tr, te)
        ytr = y_train[tr]
        w = None if weight is None else weight[tr]
        if keep is not None:
            m = keep[tr]
            Xtr, ytr = Xtr[m].reset_index(drop=True), ytr[m]
            w = None if w is None else w[m]
        if w is not None:                      # 가중치 0 인 행은 학습에서 빠진다
            nz = w > 0
            Xtr, ytr, w = Xtr[nz].reset_index(drop=True), ytr[nz], w[nz]
        n_train_used.append(int(len(ytr)))
        model = F.make_point_model()
        model.fit(Xtr, ytr, sample_weight=w) if w is not None else model.fit(Xtr, ytr)
        pred[te] = model.predict(Xte)
        per_fold.append(round(float(np.abs(pred[te] - y_eval[te]).mean()), 4))
    return {"name": name, "pred": pred, "fold_id": fold_id,
            "per_fold_MAE": per_fold, "n_train_per_fold": n_train_used,
            "seconds": round(time.time() - t0, 1)}


# ------------------------------------------------------------ 지표
def paired_test(y, p_new, p_old, mask=None):
    from scipy import stats

    m = np.ones(len(y), bool) if mask is None else mask
    e_new, e_old = np.abs(p_new[m] - y[m]), np.abs(p_old[m] - y[m])
    d = e_new - e_old
    w = None if np.allclose(d, 0) else stats.wilcoxon(e_new, e_old)
    rng = np.random.default_rng(F.PIPELINE_SEED)
    boot = np.array([d[rng.integers(0, len(d), len(d))].mean() for _ in range(2000)])
    return {"delta_MAE": round(float(d.mean()), 4),
            "ci95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
            "wilcoxon_p": (None if w is None else float("%.3g" % w.pvalue)),
            "n": int(m.sum()), "n_better": int((d < 0).sum()),
            "n_worse": int((d > 0).sum())}


def block(y, pred, d, A, per_fold, base_pred=None, mask=None, base_folds=None):
    m = np.ones(len(y), bool) if mask is None else mask
    met = M45.point_metrics(y[m], pred[m])
    met["n"] = int(m.sum())
    met["per_fold_MAE"] = per_fold
    met["fold_std"] = round(float(np.std(per_fold)), 4)
    met["cohort"] = {}
    for k in ("taxonomy", "bizinfo"):
        s = m & (d["cohort"].to_numpy() == k)
        met["cohort"][k] = {"n": int(s.sum()),
                            "MAE_log10": round(float(np.abs(pred[s] - y[s]).mean()), 4)}
    met["by_label"] = {}
    for lab in TA.LABELS:
        s = m & (A["label"].to_numpy() == lab)
        if s.sum():
            met["by_label"][lab] = {"n": int(s.sum()),
                                    "MAE_log10": round(float(np.abs(pred[s] - y[s]).mean()), 4)}
    if base_pred is not None:
        met["vs_V1"] = paired_test(y, pred, base_pred, m)
        if base_folds:
            met["fold_wins"] = int(sum(1 for a, b in zip(per_fold, base_folds) if a < b))
    return met


def main():
    t_all = time.time()
    print("== 데이터 — M69 와 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y1, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16],
                                      fp["rows_after_filters"]))
    # `src` = 타깃이 파싱된 그 텍스트(taxonomy=scale_text, bizinfo=공고문 원문).
    # `body` = 본문 텍스트 feature 용 마스킹본으로, taxonomy 쪽에는 사업목적·
    # 사업내용이 앞에 붙어 있다. 감사는 **파서가 본 것과 같은 텍스트**를 봐야
    # 한다 — body 를 넘기면 taxonomy 행의 후보 순서가 달라져 멀쩡한 행이
    # NO_EVIDENCE 로 떨어진다(스모크 실측: 7행 -> 41행).
    NB, body, src = SF.build(d)

    m69 = pd.read_parquet(M69_OOF)
    assert (m69["row_id"].to_numpy() == d["row_id"].to_numpy()).all()
    err69 = np.abs(m69["pred_G"].to_numpy() - y1)

    # ---------------------------------------------- 1. 우선순위 샘플링 (지시서 3장)
    print("\n== 1. bizinfo 고오차 행 (지시서 3장 우선 1)")
    is_biz = (d["cohort"] == "bizinfo").to_numpy()
    order = np.argsort(-np.where(is_biz, err69, -1))
    prio = {}
    for n in TOP_N:
        idx = order[:n]
        prio["Top%d" % n] = {
            "n": int(n),
            "mean_abs_err": round(float(err69[idx].mean()), 4),
            "share_of_bizinfo_total_error": round(
                float(err69[idx].sum() / err69[is_biz].sum()), 4)}
        print("   Top%-4d 평균오차 %.3f · bizinfo 총오차의 %.1f%%"
              % (n, prio["Top%d" % n]["mean_abs_err"],
                 100 * prio["Top%d" % n]["share_of_bizinfo_total_error"]))

    # ---------------------------------------------- 2. 감사 (지시서 4·5·6장)
    print("\n== 2. 원문 감사 — 라벨 (지시서 5장)")
    ts = time.time()
    A = TA.audit(d, src)
    A.to_parquet(OUT_AUDIT, index=False)
    print("   %d행 감사 / %.0f초  [%s]" % (len(A), time.time() - ts, OUT_AUDIT))
    xt = pd.crosstab(A["cohort"], A["label"])
    print(xt.to_string())
    # 현행 타깃 재현 — 감사층이 파서와 같은 후보를 짚는지
    same_as_parser = float(np.isclose(A["target_current"].fillna(-1),
                                      d["per_recipient"].fillna(-1)).mean())
    print("   감사층이 지목한 현행 타깃 = 데이터셋 타깃: %.4f" % same_as_parser)

    print("\n   라벨별 n / 평균 OOF 오차 (지시서 10장 품질별)")
    label_err = {}
    for lab in TA.LABELS:
        for coh in ("bizinfo", "taxonomy"):
            s = (A["label"] == lab).to_numpy() & (d["cohort"] == coh).to_numpy()
            if s.sum():
                label_err["%s/%s" % (coh, lab)] = {
                    "n": int(s.sum()), "mean_abs_err": round(float(err69[s].mean()), 4)}
    for k, v in label_err.items():
        print("      %-28s n=%4d  평균오차 %.3f" % (k, v["n"], v["mean_abs_err"]))

    print("\n   오류유형별 (지시서 6장)")
    etypes = {}
    for et in sorted(A["error_type"].dropna().unique()):
        s = (A["error_type"] == et).to_numpy()
        etypes[str(et)] = {
            "n": int(s.sum()), "share": round(float(s.mean()), 4),
            "mean_abs_err": round(float(err69[s].mean()), 4),
            "fixable": int(((A["error_type"] == et) & (A["label"] == "FIXABLE")).sum())}
        print("      %-24s n=%3d (%.1f%%)  평균오차 %.3f  교정가능 %d"
              % (et, etypes[str(et)]["n"], 100 * etypes[str(et)]["share"],
                 etypes[str(et)]["mean_abs_err"], etypes[str(et)]["fixable"]))

    # ---------------------------------------------- 3. 교정 diff (지시서 11장)
    fix = A["label"].to_numpy() == "FIXABLE"
    y2 = y1.copy()
    y2[fix] = np.log10(A["target_corrected"].to_numpy()[fix])
    diff = pd.DataFrame({
        "row_id": A["row_id"][fix], "cohort": A["cohort"][fix],
        "title": d["title"].to_numpy()[fix],
        "error_type": A["error_type"][fix],
        "target_before_won": A["target_current"][fix].astype("Int64"),
        "target_after_won": A["target_corrected"][fix].astype("Int64"),
        "delta_log10": np.round(y2[fix] - y1[fix], 4),
        "oof_abs_err_before": np.round(err69[fix], 4),
        "evidence_before": A["evidence_before"][fix],
        "evidence_after": A["evidence_after"][fix],
    })
    diff.to_csv(OUT_DIFF, index=False, encoding="utf-8-sig")
    print("\n== 3. 교정 diff — %d행 (지시서 11장, 사람 검증용)" % fix.sum())
    print("   교정 폭 |Δlog10| 중앙 %.3f · 최대 %.3f"
          % (np.median(np.abs(diff.delta_log10)), np.abs(diff.delta_log10).max()))
    print("   방향: 낮춤 %d행 / 높임 %d행"
          % (int((diff.delta_log10 < 0).sum()), int((diff.delta_log10 > 0).sum())))
    print("   [diff] %s" % OUT_DIFF)

    # ---------------------------------------------- 누수 점검 (지시서 11장)
    print("\n== 4. 누수 점검 (지시서 11장)")
    # NB feature 가 타깃과 같은 값을 담은 칸 수를 **y1 과 y2 둘 다** 센다.
    #
    # 이 숫자를 0 으로 요구하면 안 된다 — 1개사만 뽑는 사업은 총사업비와 기업당
    # 한도가 실제로 같은 숫자라, 교정 전에도 우연한 일치가 있다(M69 가 이미
    # 측정: total_budget 6.4%). 물어야 할 것은 **교정이 그 일치를 늘렸는가**다.
    # 늘었다면 교정된 타깃이 feature 안으로 들어온 것이고, 그대로면 원래 있던
    # 우연이다.
    def _target_hits(tgt):
        n = 0
        for c in NB.columns:
            if not c.endswith("_log10") or c in ("nb_src_len_log10", "nb_body_len_log10"):
                continue
            f = NB[c].to_numpy(dtype=float)
            m = np.isfinite(f)
            n += int(np.isclose(10 ** f[m], tgt[m], rtol=1e-6).sum())
        return n

    hit1, hit2 = _target_hits(10 ** y1), _target_hits(10 ** y2)
    leak = {
        "감사층이 읽은 것": TA.manifest()["inputs_seen"],
        "감사층이 읽지 않은 것": TA.manifest()["inputs_not_seen"],
        "amount_parser 수정 여부": "수정하지 않음 — 모델 3 이 같은 파서를 쓴다",
        "NB feature 가 타깃과 같은 값인 칸 수 (교정 전 y1)": "%d — M69 가 이미 재던 우연 일치" % hit1,
        "NB feature 가 타깃과 같은 값인 칸 수 (교정 후 y2)": "%d — y1 보다 늘지 않아야 한다" % hit2,
        "교정이 새로 만든 일치": "%+d" % (hit2 - hit1),
        "구간/가중치 계산에 y 사용": "없음 — 라벨은 텍스트 문맥에서만 나온다",
    }
    for k, v in leak.items():
        print("   %-46s %s" % (k, v))
    leak_pass = hit2 <= hit1

    # ---------------------------------------------- 5. 재학습 (지시서 9장)
    keep = A["keep"].to_numpy()
    weight = A["weight"].to_numpy()
    fixed_eval = (A["label"].to_numpy() == "CORRECT")   # y 가 바뀌지 않은 행
    print("\n== 5. 재학습 — 고정 평가셋 %d행 / 전체 %d행" % (fixed_eval.sum(), len(y1)))
    print("   V3 학습 유지 %d행 · V4 가중 0 행 %d"
          % (int(keep.sum()), int((weight == 0).sum())))

    runs, results = {}, {}
    for gname in ("program_stem", "normalized_title"):
        g = groups[gname]
        print("\n   -- split [%s]" % gname)
        r = {}
        r[V1] = run_version(V1, Xs, y1, y1, g, titles, body, NB)
        r[V2] = run_version(V2, Xs, y2, y2, g, titles, body, NB)
        r[V3] = run_version(V3, Xs, y2, y1, g, titles, body, NB, keep=keep)
        r[V4] = run_version(V4, Xs, y2, y1, g, titles, body, NB, weight=weight)
        runs[gname] = r
        yv = {V1: y1, V2: y2, V3: y1, V4: y1}
        res = {"fixed_eval": {}, "full": {}}
        for v in VERSIONS:
            res["fixed_eval"][v] = block(
                yv[v], r[v]["pred"], d, A,
                [round(float(np.abs(r[v]["pred"][(r[v]["fold_id"] == i) & fixed_eval]
                                    - yv[v][(r[v]["fold_id"] == i) & fixed_eval]).mean()), 4)
                 for i in range(F.N_SPLITS)],
                base_pred=(None if v == V1 else r[V1]["pred"]), mask=fixed_eval,
                base_folds=(None if v == V1 else res["fixed_eval"][V1]["per_fold_MAE"]))
            res["full"][v] = block(yv[v], r[v]["pred"], d, A, r[v]["per_fold_MAE"],
                                   base_pred=(None if v == V1 else r[V1]["pred"]),
                                   base_folds=(None if v == V1
                                               else r[V1]["per_fold_MAE"]))
            res["full"][v]["comparable_to_V1"] = bool(v != V2)
            res["full"][v]["seconds"] = r[v]["seconds"]
        results[gname] = res
        for scope in ("fixed_eval", "full"):
            print("      [%s]" % ("고정 평가셋" if scope == "fixed_eval" else "전체 셋"))
            for v in VERSIONS:
                b = res[scope][v]
                vs = b.get("vs_V1")
                flag = "" if b.get("comparable_to_V1", True) else "  ※ y 다름 — 직접비교 불가"
                print("         %-14s MAE %.4f  tax %.4f  biz %.4f  %s%s"
                      % (v, b["MAE_log10"], b["cohort"]["taxonomy"]["MAE_log10"],
                         b["cohort"]["bizinfo"]["MAE_log10"],
                         ("Δ%+0.4f CI[%+0.4f,%+0.4f] p=%s" %
                          (vs["delta_MAE"], vs["ci95"][0], vs["ci95"][1], vs["wilcoxon_p"]))
                         if vs else "", flag))

    # ---------------------------------------------- 재현성
    print("\n== 6. 재현성 — 같은 seed 로 V1·V3 을 한 번 더")
    repro = {}
    for v, kw in ((V1, {}), (V3, {"keep": keep})):
        yt = y1 if v == V1 else y2
        again = run_version(v, Xs, yt, y1, groups["program_stem"], titles, body, NB, **kw)
        repro[v] = bool(np.allclose(again["pred"], runs["program_stem"][v]["pred"],
                                    atol=REPRO_TOL))
    print("   " + " / ".join("%s %s" % (k, v) for k, v in repro.items()))
    m69_match = bool(np.allclose(runs["program_stem"][V1]["pred"],
                                 m69["pred_G"].to_numpy(), atol=REPRO_TOL))
    print("   V1 이 M69 저장 OOF 와 행 단위 일치: %s" % m69_match)

    # ---------------------------------------------- 승격 판정 (지시서 12장)
    ps, nt = results["program_stem"], results["normalized_title"]
    best = min([V2, V3, V4], key=lambda v: ps["fixed_eval"][v]["MAE_log10"])
    B, base = ps["fixed_eval"][best], ps["fixed_eval"][V1]
    v = B.get("vs_V1")
    checks = {
        "1. 고정 평가셋 OOF MAE 가 V1 보다 개선": B["MAE_log10"] < base["MAE_log10"],
        "2. bizinfo MAE 가 V1 보다 개선":
            B["cohort"]["bizinfo"]["MAE_log10"] < base["cohort"]["bizinfo"]["MAE_log10"],
        "3. taxonomy 성능이 악화되지 않음":
            B["cohort"]["taxonomy"]["MAE_log10"] <= base["cohort"]["taxonomy"]["MAE_log10"] + 1e-4,
        "4. strict split 에서도 개선 유지":
            nt["fixed_eval"][best]["MAE_log10"] < nt["fixed_eval"][V1]["MAE_log10"],
        "5. 5개 fold 대부분에서 개선 (4 이상)": B.get("fold_wins", 0) >= 4,
        "6. paired CI 가 0 아래": bool(v and v["ci95"][1] < 0),
        "7. leakage audit PASS": bool(leak_pass),
        "8. reproducibility PASS": bool(all(repro.values()) and m69_match),
    }
    verdict = "승격 후보 (M69 대체)" if all(checks.values()) else "현행 유지 (M69)"
    print("\n== 7. 승격 점검표 (지시서 12장) — 대상: %s (고정 평가셋 기준)" % best)
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    pd.DataFrame({
        "row_id": d["row_id"].to_numpy(), "y1": y1, "y2": y2,
        "fold": runs["program_stem"][V1]["fold_id"],
        "cohort": d["cohort"].to_numpy(), "label": A["label"].to_numpy(),
        "fixed_eval": fixed_eval,
        **{("pred_" + v.split()[0]): runs["program_stem"][v]["pred"] for v in VERSIONS},
    }).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "M69 의 남은 오차가 모델 한계인가 bizinfo 타깃 파싱 오류인가",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"],
                      "rows": fp["rows_after_filters"], "model": F.XGB_POINT,
                      "features": "M69 G 단계 그대로"},
        "changed": "타깃 품질 — 원문 근거로 교정 / 라벨로 학습 필터·가중",
        "comparability_note": (
            "타깃을 고치면 y 가 바뀌어 MAE 를 0.3719 과 직접 비교할 수 없다. "
            "판정은 y 가 바뀌지 않은 CORRECT 행(고정 평가셋)에서만 한다. "
            "V3/V4 는 학습에서만 라벨을 쓰고 평가 타깃은 V1 것을 그대로 쓴다."),
        "audit_layer": TA.manifest(),
        "priority_sampling": prio,
        "label_distribution": {str(k): {str(kk): int(vv) for kk, vv in row.items()}
                               for k, row in xt.iterrows()},
        "audit_matches_parser": round(same_as_parser, 4),
        "label_error": label_err,
        "error_types": etypes,
        "corrections": {
            "n": int(fix.sum()),
            "median_abs_delta_log10": round(float(np.median(np.abs(diff.delta_log10))), 4),
            "max_abs_delta_log10": round(float(np.abs(diff.delta_log10).max()), 4),
            "n_lowered": int((diff.delta_log10 < 0).sum()),
            "n_raised": int((diff.delta_log10 > 0).sum()),
            "diff_csv": os.path.relpath(OUT_DIFF, C.ROOT),
        },
        "leakage_audit": leak, "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "fixed_eval_n": int(fixed_eval.sum()),
        "train_rows_kept": int(keep.sum()),
        "results": results,
        "best_version": best,
        "reproducibility": {**repro, "V1 == M69 저장 OOF": m69_match},
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "goals": {"first": GOAL_1, "final": GOAL_2},
        "published_m69": M69_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t_all, 1),
    }
    C.save_report("m71_m2_target_quality.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t_all))
    return payload


# ------------------------------------------------------------ md
def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    best = p["best_version"]
    L = []
    A = L.append
    A("# M71 — bizinfo 금액 타깃 품질 감사와 재학습\n")
    A("> 질문: **M69 의 남은 오차가 모델 한계가 아니라 bizinfo 금액 타깃의")
    A("> 파싱/관측 오류에서 오는 것인가?**\n")
    A("## 0. 이 리포트를 읽는 법 — MAE 두 개를 구분해야 한다\n")
    A("```text")
    A(p["comparability_note"])
    A("```\n")
    A("| | 평가 행 | 평가 타깃 | 0.3719 과 비교 |")
    A("|---|---|---|---|")
    A("| 고정 평가셋 (판정 근거) | 감사 라벨 CORRECT %d행 | y 변경 없음 | **가능** |"
      % p["fixed_eval_n"])
    A("| 전체 셋 (참고) | 1,877행 | V2 는 y2 | V2 는 **불가** |")
    A("")
    A("## 1. 우선순위 샘플링 (지시서 3장)\n")
    A("| 구간 | n | 평균 절대오차 | bizinfo 총오차 중 비중 |")
    A("|---|---:|---:|---:|")
    for k, v in p["priority_sampling"].items():
        A("| %s | %d | %.3f | %.1f%% |"
          % (k, v["n"], v["mean_abs_err"], 100 * v["share_of_bizinfo_total_error"]))
    A("")
    A("## 2. 감사 라벨 (지시서 5장)\n")
    A("| 코호트 | " + " | ".join(TA.LABELS) + " |")
    A("|---|" + "---:|" * len(TA.LABELS))
    for coh, row in p["label_distribution"].items():
        A("| %s | " % coh + " | ".join(str(row.get(l, 0)) for l in TA.LABELS) + " |")
    A("")
    A("> 감사층이 지목한 '현행 타깃'이 데이터셋 타깃과 일치한 비율 **%.4f** —"
      % p["audit_matches_parser"])
    A("> 감사층이 파서와 같은 자리를 보고 있다는 확인입니다.\n")
    A("### 라벨별 n / 평균 OOF 오차\n")
    A("| 코호트 / 라벨 | n | 평균 절대오차 |")
    A("|---|---:|---:|")
    for k, v in p["label_error"].items():
        A("| %s | %d | %.3f |" % (k, v["n"], v["mean_abs_err"]))
    A("")
    A("## 3. 오류 유형 (지시서 6장)\n")
    A("| 유형 | n | 비율 | 평균 OOF 오차 | 교정 가능 |")
    A("|---|---:|---:|---:|---:|")
    for k, v in p["error_types"].items():
        A("| %s | %d | %.1f%% | %.3f | %d |"
          % (k, v["n"], 100 * v["share"], v["mean_abs_err"], v["fixable"]))
    A("")
    A("### 교정하지 않기로 한 것\n")
    A("| 플래그 | 이유 |")
    A("|---|---|")
    for k, v in p["audit_layer"]["not_corrected_flags"].items():
        A("| `%s` | %s |" % (k, v))
    A("")
    A("## 4. 교정 diff (지시서 11장)\n")
    c = p["corrections"]
    A("```text")
    A("교정 행수        %d" % c["n"])
    A("|Δlog10| 중앙    %.3f   (최대 %.3f)" % (c["median_abs_delta_log10"],
                                            c["max_abs_delta_log10"]))
    A("방향             낮춤 %d행 / 높임 %d행" % (c["n_lowered"], c["n_raised"]))
    A("전/후 근거       %s" % c["diff_csv"])
    A("```\n")
    A("> 교정이 옳은지는 오차가 줄었는가로 판정할 수 없습니다 — 그건 순환입니다.")
    A("> 위 CSV 에 교정 전/후 값과 **원문 근거 문자열**을 나란히 남겼으니 그것으로")
    A("> 판정해 주십시오.\n")
    A("## 5. 누수 점검 (지시서 11장)\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        A("| %s | %s |" % (k, v if not isinstance(v, list) else ", ".join(map(str, v))))
    A("| **판정** | **%s** |" % p["leakage_verdict"])
    A("")
    for scope, title in (("fixed_eval", "6. 고정 평가셋 — 판정 근거"),
                         ("full", "7. 전체 셋 — 지시서 9장 표 (참고)")):
        A("## %s\n" % title)
        A("| 버전 | n | MAE | fold σ | 2배 이내 | 3배 이내 | ΔMAE vs V1 | 95% CI | "
          "wilcoxon p | fold승 | taxonomy | bizinfo |")
        A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for v in VERSIONS:
            b = ps[scope][v]
            vs = b.get("vs_V1")
            note = "" if b.get("comparable_to_V1", True) else " ※"
            A("| %s%s | %d | %.4f | %.4f | %.1f%% | %.1f%% | %s | %s | %s | %s | %.4f | %.4f |"
              % (v, note, b["n"], b["MAE_log10"], b["fold_std"], 100 * b["within_2x"],
                 100 * b["within_3x"],
                 ("%+0.4f" % vs["delta_MAE"]) if vs else "—",
                 ("[%+0.4f, %+0.4f]" % tuple(vs["ci95"])) if vs else "—",
                 (str(vs["wilcoxon_p"]) if vs else "—"),
                 ("%d/5" % b["fold_wins"]) if vs and "fold_wins" in b else "—",
                 b["cohort"]["taxonomy"]["MAE_log10"], b["cohort"]["bizinfo"]["MAE_log10"]))
        A("")
        if scope == "full":
            A("> ※ V2 는 타깃 자체가 다릅니다. 이 행의 MAE 를 M69 의 0.3719 과 나란히")
            A("> 놓고 '좋아졌다'고 읽으면 안 됩니다 — 자가 다릅니다.\n")
    A("### 라벨별 MAE (지시서 10장 품질별, 전체 셋)\n")
    A("| 버전 | " + " | ".join(TA.LABELS) + " |")
    A("|---|" + "---:|" * len(TA.LABELS))
    for v in VERSIONS:
        row = ps["full"][v]["by_label"]
        A("| %s | " % v + " | ".join(
            ("%.4f (n=%d)" % (row[l]["MAE_log10"], row[l]["n"])) if l in row else "—"
            for l in TA.LABELS) + " |")
    A("")
    A("## 8. 엄격 split (normalized_title) — 고정 평가셋\n")
    A("| 버전 | MAE | taxonomy | bizinfo |")
    A("|---|---:|---:|---:|")
    for v in VERSIONS:
        b = nt["fixed_eval"][v]
        A("| %s | %.4f | %.4f | %.4f |" % (v, b["MAE_log10"],
                                           b["cohort"]["taxonomy"]["MAE_log10"],
                                           b["cohort"]["bizinfo"]["MAE_log10"]))
    A("")
    A("## 9. 승격 점검표 (지시서 12장) — 대상: %s\n" % best)
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (k, v) for k, v in p["reproducibility"].items()))
    A("")
    A("## 결론\n")
    A("```text")
    A("V1 baseline (고정 평가셋)  MAE %.4f   tax %.4f / biz %.4f"
      % (ps["fixed_eval"][V1]["MAE_log10"],
         ps["fixed_eval"][V1]["cohort"]["taxonomy"]["MAE_log10"],
         ps["fixed_eval"][V1]["cohort"]["bizinfo"]["MAE_log10"]))
    A("%s (고정 평가셋)         MAE %.4f   tax %.4f / biz %.4f"
      % (best, ps["fixed_eval"][best]["MAE_log10"],
         ps["fixed_eval"][best]["cohort"]["taxonomy"]["MAE_log10"],
         ps["fixed_eval"][best]["cohort"]["bizinfo"]["MAE_log10"]))
    A("")
    A("감사 결과  bizinfo 파싱 오류로 교정 가능한 행 %d / 901" % p["corrections"]["n"])
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
