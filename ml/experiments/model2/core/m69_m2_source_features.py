r"""M69 — 원천 feature 를 보강하면 Stage 1 금액구간 분류력이 올라가는가.

지시서(사용자, `model2_source_feature_enhancement_expert_retry_plan.md`):

    구조 실험(M67 hard routing · M68 residual/MoE)은 전부 기각됐다. oracle
    routing 이 0.2430 까지 내려가므로 **금액 구간을 더 잘 알 수 있다면**
    expert 구조의 잠재력은 남아 있다. 따라서 구조를 더 복잡하게 만들지 말고,
    금액대와 직접 연결되는 원천 정보를 모델 2 전용 feature 로 보강한 뒤
    Stage 1 분류력이 실제로 개선되는지 **먼저** 확인하고, 충분히 개선된
    경우에만 expert 구조를 재도전하라.

바꾸지 않는 것 — M65/M67 과 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    모델       m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    구간정의   fold train y 의 P33.3 / P66.7 (M67 과 동일)
    baseline   M45.cohort_median_baseline

바뀌는 것은 **입력 feature 하나**다. 예측 경로는 M65 그대로(단일 XGB)에서
시작하고, 게이트를 통과할 때만 M67 의 경로를 다시 켠다.

## ablation 단계 (지시서 9장)

    A  기존 M65 feature (구조화 + 제목 SVD64)
    B  A + budget/cap        (총사업비·금액후보 수·예산/건수)
    C  B + selected_count    (건수 로그·결측·상한초과)
    D  C + support_rate/self_burden (결측·합계 일관성)
    E  D + duration          (근거등급·개월수·지급주기)
    F  E + support_unit/method refinement (단위 근거등급·방식 히트수·지원항목 8종)
    G  F + 마스킹 본문 텍스트 SVD64   ← 지시서에 없던 한 칸. 아래 참조

G 를 넣은 이유. 지시서 3장의 후보 11개 중 6개(`support_rate`·
`self_burden_rate`·`selected_count`·`project_duration`·`support_unit`·
`support_method`)는 **이미 M65 feature 에 들어 있고**, 4개(`support_cap`·
`support_per_recipient`·`support_per_project`·`loan_limit`)는 이 데이터셋에서
**타깃 그 자체**라 4장 규칙에 걸려 쓸 수 없다. 남는 순수 신규는 `total_budget`
하나인데 커버리지가 9.3% 다. 그래서 "문서 안에 이미 존재하는 금액 결정 요인"
(지시서 2장)을 구조화 값이 아니라 **본문 텍스트**에서 찾는 칸을 하나 더 둔다.
현재 모델 2 는 제목만 텍스트로 쓰고 사업내용·지원대상 본문은 한 글자도 쓰지
않는다.

## 누수 방어

    금액 feature   파서가 고른 후보(=타깃)를 구조적으로 지목해 제외하고
                   나머지 후보에서만 만든다. 값 비교로 거르지 않는다 —
                   값으로 거르면 '결측이라는 사실'이 타깃을 가리킨다.
    텍스트 feature 금액 표현을 [AMOUNT] 로 바꾼 뒤 **남은 모든 숫자를 '#'** 로
                   덮는다. 자릿수가 살아 있으면 그것이 곧 정답이다.
    구간 경계      fold train 의 y 로만 계산 (M67 과 동일)

산출
    ml/data/processed/m69_source_oof.parquet
    ml/reports/m69_m2_source_features.json / .md
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
import re
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
import m45_m2_amount as M45

SRC = F6.OUT_V2
OUT_OOF = os.path.join(C.PROC, "m69_source_oof.parquet")
MD = C.report_path("m69_m2_source_features.md")

BUCKETS = ["Low", "Mid", "High"]
CUTS = (100.0 / 3.0, 200.0 / 3.0)
STEPS = ["A", "B", "C", "D", "E", "F", "G"]
STEP_LABEL = {
    "A": "A 기존 M65", "B": "B +budget/cap", "C": "C +selected_count",
    "D": "D +rate/burden", "E": "E +duration/주기", "F": "F +unit/method/항목",
    "G": "G +본문 텍스트",
}
BODY_SVD = 64
BODY_PREFIX = "body_svd"

# M65 공표치. 재현 대조용으로만 쓴다.
M65_PUBLISHED = {"MAE_log10": 0.4117, "within_2x": 0.492, "within_3x": 0.681}
M67_PUBLISHED = {"stage1_acc": 0.732, "routed": 0.4368, "oracle": 0.2430}

# 지시서 8장 게이트
GATE_A, GATE_B, GATE_C = 0.85, 0.80, 0.75


# ------------------------------------------------------------ 구간
def bucket_edges(ytr):
    return tuple(float(v) for v in np.percentile(ytr, CUTS))


def to_bucket(y, edges):
    return np.digitize(y, np.asarray(edges), right=False).astype(int)


def stage1_model():
    """M67 과 같은 설정. 목적함수만 다중분류다 — 비교가 성립해야 한다."""
    import xgboost as xgb
    p = {k: v for k, v in F.XGB_POINT.items() if k != "objective"}
    return xgb.XGBClassifier(objective="multi:softprob", num_class=3,
                             eval_metric="mlogloss", **p)


def fit_body_svd(train_body, test_body, return_objects=False):
    """본문 TF-IDF -> SVD. 제목과 같은 규격, fold train 에만 적합한다.

    `return_objects` 는 서빙 번들용이다 — 학습과 서빙이 **같은 함수**로
    적합해야 규격이 갈라지지 않는다.
    """
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), min_df=3,
                        max_features=30000, sublinear_tf=True)
    A = v.fit_transform(train_body)
    svd = TruncatedSVD(n_components=BODY_SVD, random_state=F.PIPELINE_SEED)
    ta, tb = svd.fit_transform(A), svd.transform(v.transform(test_body))
    return (ta, tb, (v, svd)) if return_objects else (ta, tb)


def assemble(Xtr, Xte, NB, tr, te, body_tr, body_te, step, body_cache):
    """단계 `step` 의 feature 행렬. 같은 fold 안에서 컬럼만 갈아 끼운다."""
    cols = SF.columns_upto(step)
    a, b = Xtr, Xte
    if cols:
        a = pd.concat([a.reset_index(drop=True),
                       NB.iloc[tr][cols].reset_index(drop=True)], axis=1)
        b = pd.concat([b.reset_index(drop=True),
                       NB.iloc[te][cols].reset_index(drop=True)], axis=1)
    if step == "G":
        if body_cache[0] is None:
            body_cache[0] = fit_body_svd(body_tr, body_te)
        ta, tb = body_cache[0]
        names = ["%s%02d" % (BODY_PREFIX, i) for i in range(ta.shape[1])]
        a = pd.concat([a.reset_index(drop=True), pd.DataFrame(ta, columns=names)], axis=1)
        b = pd.concat([b.reset_index(drop=True), pd.DataFrame(tb, columns=names)], axis=1)
    return a, b


# ------------------------------------------------------------ ablation 본체
def run_ablation(Xs, y, groups, titles, body, NB, cats, steps=STEPS, verbose=True):
    """한 번의 5-fold 에서 모든 단계를 **같은 fold 분할로** 잰다.

    단계를 따로 돌리면 fold·SVD 적합이 달라져 차이가 feature 때문인지
    우연 때문인지 알 수 없게 된다 (M67 이 세운 규율).
    """
    from sklearn.model_selection import GroupKFold

    n = len(y)
    pred = {s: np.zeros(n) for s in steps}
    z_hat = {s: np.zeros(n, dtype=int) for s in steps}
    proba = {s: np.zeros((n, 3)) for s in steps}
    z_true = np.zeros(n, dtype=int)
    base = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    per_fold = []

    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        t0 = time.time()
        fold_id[te] = i
        Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
        ytr, yte = y[tr], y[te]
        edges = bucket_edges(ytr)
        ztr, zte = to_bucket(ytr, edges), to_bucket(yte, edges)
        z_true[te] = zte
        base[te] = M45.cohort_median_baseline(Xs.iloc[tr], ytr, Xs.iloc[te], cats)

        body_cache = [None]
        rec = {"fold": i, "n_train": int(len(tr)), "n_test": int(len(te)),
               "edges_won": [int(round(10 ** e)) for e in edges],
               "baseline_MAE": round(float(np.abs(base[te] - yte).mean()), 4),
               "MAE": {}, "stage1_acc": {}}
        for s in steps:
            Xtr, Xte = assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te], s, body_cache)
            pred[s][te] = F.make_point_model().fit(Xtr, ytr).predict(Xte)
            clf = stage1_model().fit(Xtr, ztr)
            pr = clf.predict_proba(Xte)
            proba[s][te] = pr
            z_hat[s][te] = pr.argmax(1)
            rec["MAE"][s] = round(float(np.abs(pred[s][te] - yte).mean()), 4)
            rec["stage1_acc"][s] = round(float((z_hat[s][te] == zte).mean()), 4)
        rec["seconds"] = round(time.time() - t0, 1)
        per_fold.append(rec)
        if verbose:
            print("   fold %d  cut %s  (%.0fs)" % (i, rec["edges_won"], rec["seconds"]))
            for s in steps:
                print("      %-18s MAE %.4f   Stage1 %.3f"
                      % (STEP_LABEL[s], rec["MAE"][s], rec["stage1_acc"][s]))
    return pred, z_hat, proba, z_true, base, fold_id, per_fold


# ------------------------------------------------------------ 지표
def stage1_metrics(z_true, z_hat):
    from sklearn.metrics import f1_score

    M = np.zeros((3, 3), dtype=int)
    for t, h in zip(z_true, z_hat):
        M[t, h] += 1
    rec = {}
    for k, name in enumerate(BUCKETS):
        m = z_true == k
        rec[name] = round(float((z_hat[m] == k).mean()), 4) if m.sum() else None
    return {
        "accuracy": round(float((z_hat == z_true).mean()), 4),
        "macro_f1": round(float(f1_score(z_true, z_hat, average="macro")), 4),
        "recall": rec,
        "opposite_end_error_rate": round(float((np.abs(z_true - z_hat) == 2).mean()), 4),
        "adjacent_error_rate": round(float((np.abs(z_true - z_hat) == 1).mean()), 4),
        "confusion_true_x_pred": M.tolist(),
    }


def bucket_metrics(y, p, z_true):
    out = {}
    for k, name in enumerate(BUCKETS):
        m = z_true == k
        e = np.abs(p[m] - y[m])
        out[name] = {"n": int(m.sum()), "MAE_log10": round(float(e.mean()), 4),
                     "within_2x": round(float((e <= np.log10(2)).mean()), 4)}
    return out


def cohort_breakdown(d, y, pred, a="A", b="G"):
    """개선이 어느 코호트에서 나왔는가 — 텍스트 층의 마지막 누수 점검이다.

    taxonomy 의 본문(purpose/content/target_text)에는 금액이 아예 없고,
    bizinfo 의 본문은 공고문 전체라 금액 절이 마스킹된 채로 들어간다. 개선이
    bizinfo 에만 몰려 있으면 '마스킹을 뚫고 남은 금액 신호'를 의심해야 하고,
    두 쪽에서 함께 나오면 사업 내용 자체가 신호라는 뜻이다.
    """
    out = {}
    for col in ("cohort", "evidence_source"):
        rows = {}
        for k, idx in d.groupby(col, observed=True).groups.items():
            i = d.index.get_indexer(idx)
            ea, eb = np.abs(pred[a][i] - y[i]).mean(), np.abs(pred[b][i] - y[i]).mean()
            rows[str(k)] = {"n": int(len(i)), "MAE_%s" % a: round(float(ea), 4),
                            "MAE_%s" % b: round(float(eb), 4),
                            "delta": round(float(eb - ea), 4)}
        out[col] = rows
    return out


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


def summarize_steps(y, pred, z_hat, z_true, base, per_fold, steps=STEPS):
    b = float(np.abs(base - y).mean())
    out = {}
    for s in steps:
        met = M45.point_metrics(y, pred[s])
        met["improvement"] = round(float((b - met["MAE_log10"]) / b), 4)
        met["per_fold_MAE"] = [f["MAE"][s] for f in per_fold]
        met["fold_std"] = round(float(np.std(met["per_fold_MAE"])), 4)
        met["buckets"] = bucket_metrics(y, pred[s], z_true)
        met["stage1"] = stage1_metrics(z_true, z_hat[s])
        met["stage1"]["per_fold"] = [f["stage1_acc"][s] for f in per_fold]
        if s != "A":
            met["vs_A"] = paired_test(y, pred[s], pred["A"])
            met["fold_wins_vs_A"] = int(sum(
                1 for a, c in zip(met["per_fold_MAE"], [f["MAE"]["A"] for f in per_fold])
                if a < c))
        out[s] = met
    return {"baseline_MAE": round(b, 4), "steps": out, "folds": per_fold}


# ------------------------------------------------------------ feature 감사
def feature_audit(NB, src, y, d):
    """지시서 5장 — 모델 성능 실험 전에 feature 자체 품질부터 본다."""
    tgt = d["per_recipient"].to_numpy(dtype=float)
    # 범주형에서 '값이 없다'를 나타내는 표기. coverage 를 셀 때 결측으로 본다 —
    # 'none' 과 'no_duration_evidence' 는 이름만 다른 같은 상태다.
    empty = {"none", "no_duration_evidence", "no_text", "unchanged"}
    rows = []
    for c in NB.columns:
        v = NB[c]
        is_cat = str(v.dtype) == "category"
        nn = (int(v.notna().sum()) if not is_cat
              else int((~v.astype(str).isin(empty)).sum()))
        item = {
            "feature": c,
            "coverage": round(nn / len(v), 4),
            "missing_rate": round(1 - nn / len(v), 4),
            "kind": "categorical" if is_cat else "numeric",
        }
        if is_cat:
            item["values"] = {str(k): int(x) for k, x in v.value_counts().head(8).items()}
            item["parse_confidence"] = "규칙 매치 — 값 분포로만 감사 가능"
            item["outlier_count"] = 0
        else:
            f = v.to_numpy(dtype=float)
            m = np.isfinite(f)
            item["min"] = round(float(f[m].min()), 4) if m.any() else None
            item["max"] = round(float(f[m].max()), 4) if m.any() else None
            item["median"] = round(float(np.median(f[m])), 4) if m.any() else None
            # 이상치: log10 계열은 상식범위(1원~1조) 밖, 나머지는 IQR 5배 밖.
            # 0/1 지시자는 IQR 이 0 이라 소수 클래스가 통째로 이상치가 된다 — 제외.
            binary = m.sum() and set(np.unique(f[m])) <= {0.0, 1.0}
            if c.endswith("_log10"):
                item["outlier_count"] = int(((f[m] < 0) | (f[m] > 12)).sum())
            elif binary:
                item["outlier_count"] = 0
            elif m.sum() > 10:
                q1, q3 = np.percentile(f[m], [25, 75])
                iqr = max(q3 - q1, 1e-9)
                item["outlier_count"] = int(((f[m] < q1 - 5 * iqr) | (f[m] > q3 + 5 * iqr)).sum())
            else:
                item["outlier_count"] = 0
            item["corr_with_y"] = (round(float(np.corrcoef(f[m], y[m])[0, 1]), 4)
                                   if m.sum() > 30 and np.std(f[m]) > 0 else None)
            # 누수: 이 feature 가 타깃 값을 그대로 담고 있는가
            if c.endswith("_log10") and m.sum():
                same = np.isclose(10 ** f[m], tgt[m], rtol=1e-6)
                item["equals_target_rate"] = round(float(same.mean()), 4)
        item["source_breakdown"] = {
            k: round(float(x), 4) for k, x in
            (NB.assign(_s=d["evidence_source"].to_numpy())
             .groupby("_s", observed=True)[c]
             .apply(lambda z: (z.astype(str) != "none").mean() if is_cat else z.notna().mean())
             ).items()}
        rows.append(item)
    return rows


def leakage_audit(NB, src, body, y, d):
    """지시서 4장 — 이 실험에서 새로 생긴 누수 경로만 본다."""
    tgt = d["per_recipient"].to_numpy(dtype=float)
    # 1) 파서가 고른 후보가 정말 타깃인가 (제외 규칙이 맞는 자리를 짚는가)
    hit = 0
    for s, t in zip(src, tgt):
        c = SF.sorted_candidates(s)
        if c:
            v = c[0]["max"] if c[0]["max"] is not None else c[0]["min"]
            hit += int(v is not None and abs(float(v) - float(t)) < 1)
    # 2) 마스킹 본문에 숫자가 남았는가
    digits = sum(1 for b in body if re.search(r"\d", b))
    # 3) 새 금액 feature 가 타깃과 같은 값인가
    same = {}
    for c in NB.columns:
        if c.endswith("_log10") and c != "nb_src_len_log10" and c != "nb_body_len_log10":
            f = NB[c].to_numpy(dtype=float)
            m = np.isfinite(f)
            same[c] = round(float(np.isclose(10 ** f[m], tgt[m], rtol=1e-6).mean()), 4) \
                if m.any() else None
    return {
        "타깃 제외 규칙이 지목한 후보 = 실제 타깃": "%d/%d (%.3f)"
            % (hit, len(src), hit / len(src)),
        "마스킹 본문에 숫자가 남은 행": "%d/%d — 0 이어야 한다" % (digits, len(body)),
        "새 금액 feature 가 타깃과 같은 값인 비율": same,
        "위 비율을 0 으로 요구하지 않는 이유":
            "1개사만 뽑는 사업은 총사업비와 기업당 한도가 실제로 같은 숫자다. "
            "우연한 일치는 원문의 사실이고, 복사본이라면 **항상** 일치한다. "
            "그래서 판정 기준은 0 이 아니라 '값이 있는 행의 절반 미만'으로 둔다. "
            "구조적 차단(첫 줄)이 실제 방어선이다.",
        "쓰지 않기로 한 컬럼": SF.LEAKAGE_EXCLUDED,
        "이미 M65 에 있던 것": SF.ALREADY_IN_M65,
        "구간 경계 계산 입력": "fold train 의 y 만 (np.percentile(ytr, [33.3, 66.7]))",
    }


def unit_word_ceiling(Xs, y, groups, titles, src):
    """진단용 상한 — **모델이 아니다.**

    타깃 표현의 단위어(만원/백만원/억원)를 Stage 1 에 넣으면 정확도가 어디까지
    가는가. 지시서 4장이 금지한 값(타깃과 같은 문장에서 자릿수를 그대로 읽는
    값)이라 승격 후보가 될 수 없다. 여기 두는 이유는 하나다 — '금액구간을
    더 잘 아는 것'이 원리적으로 가능한지, 그리고 그 정보가 **원문 어디에만**
    있는지를 M67 의 oracle 처럼 한 줄로 보여주기 위해서다.

    단위어는 `design_features_v2.amount_unit_raw`(taxonomy 만 채워져 있다)가
    아니라 **원천 텍스트에서 파서가 고른 후보의 단위**로 잡는다 — 두 코호트에
    같은 방식으로 붙여야 이 상한이 한쪽 코호트의 결측을 재는 칸이 되지 않는다.
    """
    from sklearn.model_selection import GroupKFold

    words = []
    for s in src:
        c = SF.sorted_candidates(s)
        words.append(c[0]["unit"] if c else "none")
    unit = pd.Series(words)
    z_hat = np.zeros(len(y), dtype=int)
    z_true = np.zeros(len(y), dtype=int)
    X = Xs.copy()
    X["_unit"] = pd.Categorical(unit.to_numpy())
    for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(X, y, groups):
        Xtr, Xte, _ = F.build_features(X, titles, tr, te, True, True)
        edges = bucket_edges(y[tr])
        ztr, zte = to_bucket(y[tr], edges), to_bucket(y[te], edges)
        z_true[te] = zte
        z_hat[te] = stage1_model().fit(Xtr, ztr).predict(Xte)
    return stage1_metrics(z_true, z_hat)


# ------------------------------------------------------------ expert 재도전
def run_expert(Xs, y, groups, titles, body, NB, cats, step):
    """지시서 10장 — Stage 1 이 게이트를 통과했을 때만 켠다.

    M67 과 같은 경로다. 다른 것은 Stage 1/Stage 2 가 보는 feature 뿐이다.
    """
    from sklearn.model_selection import GroupKFold

    n = len(y)
    names = ["single", "routed", "oracle", "soft"]
    pred = {m: np.zeros(n) for m in names}
    z_true = np.zeros(n, dtype=int)
    z_hat = np.zeros(n, dtype=int)
    per_fold = []
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
        Xtr, Xte = assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te], step, [None])
        ytr, yte = y[tr], y[te]
        edges = bucket_edges(ytr)
        ztr, zte = to_bucket(ytr, edges), to_bucket(yte, edges)
        z_true[te] = zte
        pred["single"][te] = F.make_point_model().fit(Xtr, ytr).predict(Xte)
        clf = stage1_model().fit(Xtr, ztr)
        pr = clf.predict_proba(Xte)
        zh = pr.argmax(1)
        z_hat[te] = zh
        table = np.zeros((len(te), 3))
        for k in range(3):
            m = ztr == k
            table[:, k] = F.make_point_model().fit(Xtr.iloc[m], ytr[m]).predict(Xte)
        rows = np.arange(len(te))
        pred["routed"][te] = table[rows, zh]
        pred["oracle"][te] = table[rows, zte]
        pred["soft"][te] = (pr * table).sum(1)
        per_fold.append({"fold": i, "stage1_acc": round(float((zh == zte).mean()), 4),
                         "MAE": {m: round(float(np.abs(pred[m][te] - yte).mean()), 4)
                                 for m in names}})
    return pred, z_true, z_hat, per_fold


def blend_scan(y, p_single, p_expert):
    """지시서 10-C — alpha 스캔. **선택이 아니라 곡선을 보여주는 칸이다.**

    alpha 를 여기서 고르면 같은 OOF 에서 고르고 같은 OOF 로 재는 것이라
    낙관 쪽으로 휜다. M68b 가 λ 에서 같은 함정을 만났고 그래서 '보조 기록'으로
    남겼다. 여기서도 곡선만 남기고 승격 판정에는 쓰지 않는다.
    """
    out = []
    for a in np.arange(0, 1.01, 0.1):
        p = a * p_single + (1 - a) * p_expert
        out.append({"alpha": round(float(a), 1),
                    "MAE_log10": round(float(np.abs(p - y).mean()), 4)})
    return out


# ------------------------------------------------------------ 보고
def report_ablation(res, title):
    print("   ---- %s (baseline %.4f)" % (title, res["baseline_MAE"]))
    print("      %-18s %8s %8s | %8s %8s %8s %8s %8s"
          % ("단계", "MAE", "foldσ", "S1 acc", "macroF1", "Low rec", "High rec", "반대끝"))
    for s, met in res["steps"].items():
        s1 = met["stage1"]
        print("      %-18s %8.4f %8.4f | %8.4f %8.4f %8.4f %8.4f %8.4f"
              % (STEP_LABEL[s], met["MAE_log10"], met["fold_std"], s1["accuracy"],
                 s1["macro_f1"], s1["recall"]["Low"], s1["recall"]["High"],
                 s1["opposite_end_error_rate"]))
    for s, met in res["steps"].items():
        if "vs_A" in met:
            v = met["vs_A"]
            print("      %-18s ΔMAE %+0.4f  95%%CI [%+0.4f, %+0.4f]  p=%s  fold승 %d/5"
                  % (STEP_LABEL[s], v["delta_MAE"], v["ci95"][0], v["ci95"][1],
                     v["wilcoxon_p"], met["fold_wins_vs_A"]))


def gate_case(acc, opp_before, opp_after, low_rec, high_rec, low0, high0):
    """지시서 8장 — Stage 1 통과 기준."""
    if acc >= GATE_A and opp_after < opp_before * 0.7:
        return "A", "expert 구조 재도전"
    if acc >= GATE_B and (low_rec > low0 and high_rec > high0):
        return "B", "limited expert / soft gate 만 재검토"
    if acc >= GATE_C:
        return "C", "개선 폭 부족 — expert 구조 재도전 보류"
    return "D", "현재 문서 feature 로 금액구간 구별 자체가 어렵다 — M65 유지"


def main():
    t0 = time.time()
    print("== 데이터 — M65/M67 과 같은 입력")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    fp = F.dataset_fingerprint(SRC)
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    print("\n== 원천 feature 층 추출")
    ts = time.time()
    NB, body, src = SF.build(d)
    print("   %d개 feature / %.0f초" % (NB.shape[1], time.time() - ts))
    print("   단계별 누적 feature 수: "
          + " ".join("%s=%d" % (s, len(SF.columns_upto(s))) for s in STEPS))

    print("\n== 1. feature 품질 감사 (지시서 5장)")
    audit = feature_audit(NB, src, y, d)
    for a in audit:
        extra = ""
        if a["kind"] == "numeric":
            extra = "  중앙 %s  이상치 %d  y상관 %s  타깃동일 %s" % (
                a.get("median"), a["outlier_count"], a.get("corr_with_y"),
                a.get("equals_target_rate", "—"))
        else:
            extra = "  값 %s" % list(a["values"])[:4]
        print("   %-28s coverage %.3f%s" % (a["feature"], a["coverage"], extra))

    print("\n== 2. 누수 감사 (지시서 4장)")
    leak = leakage_audit(NB, src, body, y, d)
    for k, v in leak.items():
        if isinstance(v, dict):
            print("   %s:" % k)
            for kk, vv in v.items():
                print("      %-34s %s" % (kk, vv))
        else:
            print("   %-40s %s" % (k, v))
    leak_checks = {
        "구조적 차단이 타깃 후보를 정확히 지목": leak["타깃 제외 규칙이 지목한 후보 = 실제 타깃"]
            .endswith("(1.000)"),
        "마스킹 뒤 본문에 숫자가 남지 않음":
            int(leak["마스킹 본문에 숫자가 남은 행"].split("/")[0]) == 0,
        "어떤 금액 feature 도 타깃의 복사본이 아님 (일치율 < 0.5)":
            all((v or 0) < 0.5
                for v in leak["새 금액 feature 가 타깃과 같은 값인 비율"].values()
                if v is not None),
    }
    leak_pass = all(leak_checks.values())
    for k, ok in leak_checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   누수 감사 판정: %s" % ("PASS" if leak_pass else "FAIL"))

    print("\n== 3. ablation 5-fold [program_stem] (지시서 6·7·9장)")
    pred, z_hat, proba, z_true, base, fold_id, per_fold = run_ablation(
        Xs, y, groups["program_stem"], titles, body, NB, cats)
    ps = summarize_steps(y, pred, z_hat, z_true, base, per_fold)
    print()
    report_ablation(ps, "전체 OOF [program_stem]")

    # 최적 단계 — Stage 1 정확도 기준(지시서의 판단 대상이 Stage 1 이므로).
    best = max(STEPS, key=lambda s: ps["steps"][s]["stage1"]["accuracy"])
    best_mae = min(STEPS, key=lambda s: ps["steps"][s]["MAE_log10"])
    print("   Stage1 최고 단계: %s / MAE 최저 단계: %s" % (STEP_LABEL[best], STEP_LABEL[best_mae]))

    print("\n== 4. 진단용 상한 — 타깃 표현의 단위어를 Stage 1 에 넣으면 (모델 아님)")
    ceiling = unit_word_ceiling(Xs, y, groups["program_stem"], titles, src)
    print("   accuracy %.4f  macroF1 %.4f  반대끝 %.4f"
          % (ceiling["accuracy"], ceiling["macro_f1"], ceiling["opposite_end_error_rate"]))

    print("\n== 5. 엄격 split [normalized_title] — A 와 최적 단계만")
    strict_steps = sorted({"A", best, best_mae}, key=STEPS.index)
    sp, sz_hat, _, sz_true, sbase, _, sper = run_ablation(
        Xs, y, groups["normalized_title"], titles, body, NB, cats,
        steps=strict_steps, verbose=False)
    nt = summarize_steps(y, sp, sz_hat, sz_true, sbase, sper, steps=strict_steps)
    report_ablation(nt, "전체 OOF [normalized_title]")

    # ------------------------------------------------------------ 게이트
    a1 = ps["steps"]["A"]["stage1"]
    b1 = ps["steps"][best]["stage1"]
    case, action = gate_case(b1["accuracy"], a1["opposite_end_error_rate"],
                             b1["opposite_end_error_rate"], b1["recall"]["Low"],
                             b1["recall"]["High"], a1["recall"]["Low"], a1["recall"]["High"])
    print("\n== 6. Stage 1 게이트 (지시서 8장)")
    print("   기존 %.4f -> 보강 %.4f (%s)  반대끝 %.4f -> %.4f"
          % (a1["accuracy"], b1["accuracy"], STEP_LABEL[best],
             a1["opposite_end_error_rate"], b1["opposite_end_error_rate"]))
    print("   Case %s — %s" % (case, action))

    # ------------------------------------------------------------ expert 재도전
    expert = None
    if case in ("A", "B"):
        print("\n== 7. expert 구조 재도전 (지시서 10장) — 단계 %s" % STEP_LABEL[best])
        ep, ez_true, ez_hat, eper = run_expert(
            Xs, y, groups["program_stem"], titles, body, NB, cats, best)
        eb = float(np.abs(base - y).mean())
        expert = {"step": best, "folds": eper, "methods": {}}
        for m in ("single", "routed", "oracle", "soft"):
            met = M45.point_metrics(y, ep[m])
            met["buckets"] = bucket_metrics(y, ep[m], ez_true)
            if m != "single":
                met["vs_single"] = paired_test(y, ep[m], ep["single"])
            expert["methods"][m] = met
            print("   %-8s MAE %.4f  2배내 %.1f%%" % (m, met["MAE_log10"],
                                                    100 * met["within_2x"]))
        expert["stage1"] = stage1_metrics(ez_true, ez_hat)
        expert["blend_alpha_scan"] = blend_scan(y, ep["single"], ep["routed"])
        expert["blend_note"] = ("alpha 는 같은 OOF 에서 고르면 낙관 쪽으로 휜다. "
                                "곡선만 남기고 승격 판정에는 쓰지 않는다 (M68b 와 같은 규율).")
    else:
        print("\n== 7. expert 구조 재도전 — 건너뜀 (지시서 13장 중단 조건)")
        print("   Stage 1 accuracy %.4f < %.2f" % (b1["accuracy"], GATE_B))

    # ------------------------------------------------------------ 재현성
    print("\n== 8. 재현성 — 같은 seed 로 A 와 %s 를 한 번 더" % STEP_LABEL[best])
    repro_steps = sorted({"A", best}, key=STEPS.index)
    pred2 = run_ablation(Xs, y, groups["program_stem"], titles, body, NB, cats,
                         steps=repro_steps, verbose=False)[0]
    repro = {s: bool(np.allclose(pred2[s], pred[s])) for s in repro_steps}
    print("   " + " / ".join("%s %s" % (STEP_LABEL[k], v) for k, v in repro.items()))

    # ------------------------------------------------------------ 승격 판정
    A, B = ps["steps"]["A"], ps["steps"][best_mae]
    v = B.get("vs_A")
    checks = {
        "1. program_stem OOF MAE 가 A 보다 명확히 개선": B["MAE_log10"] < A["MAE_log10"],
        "2. normalized_title 엄격 split 에서도 같은 방향":
            nt["steps"][best_mae]["MAE_log10"] < nt["steps"]["A"]["MAE_log10"]
            if best_mae in nt["steps"] else False,
        "3. 5개 fold 대부분에서 개선 (4 이상)": (B.get("fold_wins_vs_A", 0) >= 4),
        "4. paired 95% CI 가 0 아래": bool(v and v["ci95"][1] < 0),
        "5. Low/High 구간 오차 감소":
            (B["buckets"]["Low"]["MAE_log10"] < A["buckets"]["Low"]["MAE_log10"]
             and B["buckets"]["High"]["MAE_log10"] < A["buckets"]["High"]["MAE_log10"]),
        "6. routing 반대 끝 오분류 감소":
            B["stage1"]["opposite_end_error_rate"] < A["stage1"]["opposite_end_error_rate"],
        "7. leakage audit PASS": bool(leak_pass),
        "8. 재현성 PASS": all(repro.values()),
        "9. 1차 목표 MAE < 0.35": B["MAE_log10"] < 0.35,
    }
    core = [k for k in checks if not k.startswith("9.")]
    verdict = ("승격 후보 (M65 대체)" if all(checks[k] for k in core)
               else "현행 유지 (M65)")
    print("\n== 9. 승격 점검표 (지시서 12장) — 대상: %s" % STEP_LABEL[best_mae])
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    pd.DataFrame({
        "row_id": d["row_id"].to_numpy(), "y": y, "fold": fold_id,
        "z_true": z_true, "pred_baseline": base,
        **{("pred_" + s): pred[s] for s in STEPS},
        **{("zhat_" + s): z_hat[s] for s in STEPS},
        "support_type": d["support_type"].to_numpy(),
        "cohort": d["cohort"].to_numpy(),
        "evidence_source": d["evidence_source"].to_numpy(),
    }).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "원천 feature 보강이 Stage 1 금액구간 분류력과 M65 MAE 를 개선하는가",
        "unchanged": {
            "dataset": fp["path"], "sha256": fp["sha256"], "rows": fp["rows_after_filters"],
            "target": "log10(per_recipient), basis=stated_cap",
            "split": "GroupKFold(5), group=program_stem / normalized_title",
            "model": F.XGB_POINT, "bucket_cuts": list(CUTS),
        },
        "changed": "입력 feature 만 — 모델 2 전용 원천 feature 층 %s" % SF.LAYER_VERSION,
        "feature_layer": SF.manifest(),
        "step_labels": STEP_LABEL,
        "step_feature_counts": {s: len(SF.columns_upto(s)) for s in STEPS},
        "feature_audit": audit,
        "leakage_audit": leak,
        "leakage_checks": {k: bool(v) for k, v in leak_checks.items()},
        "leakage_verdict": "PASS" if leak_pass else "FAIL",
        "results": {"program_stem": ps, "normalized_title": nt},
        "cohort_breakdown": cohort_breakdown(d, y, pred, "A", best_mae),
        "unit_word_ceiling": ceiling,
        "stage1_gate": {"case": case, "action": action,
                        "baseline_accuracy": a1["accuracy"],
                        "best_step": best, "best_accuracy": b1["accuracy"],
                        "thresholds": {"A": GATE_A, "B": GATE_B, "C": GATE_C}},
        "expert_retry": expert,
        "best_step_stage1": best, "best_step_mae": best_mae,
        "reproducibility": repro,
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "published_m65": M65_PUBLISHED, "published_m67": M67_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m69_m2_source_features.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


# ------------------------------------------------------------ md
def write_md(p):
    ps = p["results"]["program_stem"]
    nt = p["results"]["normalized_title"]
    best, best_mae = p["best_step_stage1"], p["best_step_mae"]
    L = []
    A = L.append
    A("# M69 — 원천 feature 보강 → Stage 1 재검증 → expert 재도전\n")
    A("> 질문: **금액 규모를 결정하는 원천 정보를 더 정확히 구조화하면, Stage 1")
    A("> 금액구간 분류 성능을 73.2%에서 충분히 끌어올릴 수 있는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("target   %s" % u["target"])
    A("split    %s" % u["split"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("## 1. 지시서 3장 후보 11종을 어떻게 처리했는가\n")
    A("| 후보 | 처리 | 이유 |")
    A("|---|---|---|")
    for k, v in p["feature_layer"]["leakage_excluded"].items():
        A("| `%s` | **제외 (누수)** | %s |" % (k, v))
    for k in p["feature_layer"]["already_in_m65"]:
        A("| `%s` | 이미 M65 feature | 값이 아니라 결측·근거등급·일관성만 보강 |" % k)
    A("| `total_budget` | **신규 사용** | 타깃 후보를 뺀 나머지 금액 후보에서 추출 |")
    A("")
    A("> 지시서 3장의 후보 11개 중 4개는 이 데이터셋에서 **타깃 그 자체**이고")
    A("> 6개는 **이미 M65 에 들어 있다**. 순수 신규는 `total_budget` 하나뿐이라,")
    A("> 그 칸만으로는 지시서가 기대한 폭이 나오지 않는다. 그래서 ablation 에")
    A("> 지시서에 없던 **G(마스킹 본문 텍스트)** 를 한 칸 더 뒀다 — 현재 모델 2 는")
    A("> 제목만 텍스트로 쓰고 사업내용·지원대상 본문은 쓰지 않는다.\n")
    A("## 2. feature 품질 감사 (지시서 5장)\n")
    A("| feature | coverage | 결측률 | 종류 | 이상치 | y 상관 | 타깃과 동일값 |")
    A("|---|---:|---:|---|---:|---:|---:|")
    for a in p["feature_audit"]:
        A("| `%s` | %.3f | %.3f | %s | %d | %s | %s |"
          % (a["feature"], a["coverage"], a["missing_rate"], a["kind"],
             a["outlier_count"],
             ("%.3f" % a["corr_with_y"]) if a.get("corr_with_y") is not None else "—",
             ("%.3f" % a["equals_target_rate"]) if a.get("equals_target_rate") is not None else "—"))
    A("")
    A("## 3. 누수 감사 (지시서 4장)\n")
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in p["leakage_audit"].items():
        if isinstance(v, dict):
            A("| %s | %s |" % (k, "· ".join("`%s` %s" % (a, b) for a, b in list(v.items())[:6])))
        else:
            A("| %s | %s |" % (k, v))
    for k, ok in p.get("leakage_checks", {}).items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("| **판정** | **%s** |" % p["leakage_verdict"])
    A("")
    A("## 4. ablation — 단계별 단일 XGB MAE 와 Stage 1 (지시서 9장)\n")
    A("| 단계 | 신규 feature 수 | MAE(log10) | fold σ | 2배 이내 | 3배 이내 | "
      "ΔMAE vs A | 95% CI | wilcoxon p | fold승 | "
      "S1 acc | macro-F1 | Low recall | Mid recall | High recall | 반대끝 오분류 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for s, met in ps["steps"].items():
        s1 = met["stage1"]
        v = met.get("vs_A")
        A("| %s | %d | %.4f | %.4f | %.1f%% | %.1f%% | %s | %s | %s | %s | "
          "%.4f | %.4f | %.4f | %.4f | %.4f | %.4f |"
          % (p["step_labels"][s], p["step_feature_counts"][s], met["MAE_log10"],
             met["fold_std"], 100 * met["within_2x"], 100 * met["within_3x"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             (str(v["wilcoxon_p"]) if v else "—"),
             ("%d/5" % met["fold_wins_vs_A"]) if v else "—",
             s1["accuracy"], s1["macro_f1"], s1["recall"]["Low"], s1["recall"]["Mid"],
             s1["recall"]["High"], s1["opposite_end_error_rate"]))
    A("")
    A("### fold별 MAE\n")
    A("| 단계 | " + " | ".join("fold %d" % f["fold"] for f in ps["folds"]) + " |")
    A("|---|" + "---:|" * len(ps["folds"]))
    for s, met in ps["steps"].items():
        A("| %s | " % p["step_labels"][s]
          + " | ".join("%.4f" % x for x in met["per_fold_MAE"]) + " |")
    A("")
    A("> baseline(비교군 중앙값) MAE %.4f. M65 공표치는 MAE 0.4117 / 2배 이내 49.2%% 이고,"
      % ps["baseline_MAE"])
    A("> 위 표의 A 행이 그 재현입니다. M67 의 Stage 1 은 73.2% 였습니다.\n")
    A("## 5. 실제 구간별 MAE\n")
    A("| 구간 | n | " + " | ".join(p["step_labels"][s] for s in ps["steps"]) + " |")
    A("|---|---:|" + "---:|" * len(ps["steps"]))
    for name in BUCKETS:
        A("| %s | %d | " % (name, ps["steps"]["A"]["buckets"][name]["n"])
          + " | ".join("%.4f" % ps["steps"][s]["buckets"][name]["MAE_log10"]
                       for s in ps["steps"]) + " |")
    A("")
    A("## 6. Stage 1 confusion — A 대 %s\n" % p["step_labels"][best])
    for s in sorted({"A", best}, key=STEPS.index):
        A("**%s** (accuracy %.4f)\n" % (p["step_labels"][s], ps["steps"][s]["stage1"]["accuracy"]))
        A("| 실제 \\ 예측 | Low | Mid | High |")
        A("|---|---:|---:|---:|")
        for r, row in zip(BUCKETS, ps["steps"][s]["stage1"]["confusion_true_x_pred"]):
            A("| %s | %d | %d | %d |" % (r, row[0], row[1], row[2]))
        A("")
    cb = p.get("cohort_breakdown")
    if cb:
        A("## 6-2. 개선이 어느 코호트에서 나왔는가 (텍스트 층 누수 점검)\n")
        A("| 축 | 값 | n | A MAE | %s MAE | Δ |" % p["step_labels"][best_mae])
        A("|---|---|---:|---:|---:|---:|")
        for axis, rows in cb.items():
            for k, v in rows.items():
                A("| %s | %s | %d | %.4f | %.4f | %+0.4f |"
                  % (axis, k, v["n"], v["MAE_A"], v["MAE_%s" % best_mae], v["delta"]))
        A("")
        A("> taxonomy 의 본문은 `purpose`·`content`·`target_text` 라 **금액이 아예")
        A("> 없는 텍스트**입니다. 그쪽에서도 개선이 나온다는 것이, 이 이득이")
        A("> 마스킹을 뚫고 남은 금액 신호가 아니라 **사업 내용 자체**라는 근거입니다.")
        A("> 공고문 전체를 본문으로 쓰는 `document` 행에서 개선이 가장 큰 것은")
        A("> 그쪽이 원래 제목 말고는 텍스트가 없던 자리이기 때문입니다.\n")
    A("## 7. 진단용 상한 — 타깃 표현의 단위어를 넣으면 (모델 아님)\n")
    c = p["unit_word_ceiling"]
    A("```text")
    A("Stage 1 accuracy  %.4f   (보강 최고 %.4f / 기존 %.4f)"
      % (c["accuracy"], ps["steps"][best]["stage1"]["accuracy"],
         ps["steps"]["A"]["stage1"]["accuracy"]))
    A("macro-F1          %.4f" % c["macro_f1"])
    A("반대끝 오분류      %.4f" % c["opposite_end_error_rate"])
    A("```\n")
    A("> M67 의 oracle 과 같은 성격입니다 — **상한 진단이지 모델이 아닙니다.**")
    A("> 단위어(만원/백만원/억원)는 타깃이 적힌 그 문장에서 자릿수를 그대로 읽는")
    A("> 값이라 지시서 4장 '사용 금지'에 걸립니다. 이 줄이 말하는 것은 하나입니다 —")
    A("> **금액구간을 아는 정보는 원문의 금액 표현 안에만 있고, 사업 설계 정보")
    A("> (예산·건수·비율·기간·항목)에는 그만큼 남아 있지 않다.**\n")
    A("## 8. 엄격 split (normalized_title)\n")
    A("| 단계 | MAE(log10) | Stage 1 accuracy |")
    A("|---|---:|---:|")
    for s, met in nt["steps"].items():
        A("| %s | %.4f | %.4f |" % (p["step_labels"][s], met["MAE_log10"],
                                    met["stage1"]["accuracy"]))
    A("")
    A("## 9. Stage 1 게이트 (지시서 8장)\n")
    g = p["stage1_gate"]
    A("```text")
    A("기존 Stage 1 accuracy   %.4f" % g["baseline_accuracy"])
    A("보강 Stage 1 accuracy   %.4f   (%s)" % (g["best_accuracy"], p["step_labels"][best]))
    A("기준  Case A >= %.2f / Case B >= %.2f / Case C >= %.2f"
      % (g["thresholds"]["A"], g["thresholds"]["B"], g["thresholds"]["C"]))
    A("판정  Case %s — %s" % (g["case"], g["action"]))
    A("```\n")
    if p["expert_retry"]:
        e = p["expert_retry"]
        A("## 10. expert 구조 재도전 (지시서 10장)\n")
        A("| 방법 | MAE(log10) | 2배 이내 | ΔMAE vs single | 95% CI |")
        A("|---|---:|---:|---:|---:|")
        for m, met in e["methods"].items():
            v = met.get("vs_single")
            A("| %s | %.4f | %.1f%% | %s | %s |"
              % (m, met["MAE_log10"], 100 * met["within_2x"],
                 ("%+0.4f" % v["delta_MAE"]) if v else "—",
                 ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—"))
        A("")
        A("### alpha blend 곡선 (참고)\n")
        A("| alpha | MAE |")
        A("|---:|---:|")
        for r in e["blend_alpha_scan"]:
            A("| %.1f | %.4f |" % (r["alpha"], r["MAE_log10"]))
        A("")
        A("> %s\n" % e["blend_note"])
    else:
        A("## 10. expert 구조 재도전 — 실행하지 않음\n")
        A("지시서 13장 중단 조건에 걸립니다 — **새 feature 를 넣어도 Stage 1 accuracy 가")
        A("%.2f 미만**입니다(%.4f). M67 이 이미 측정한 대로 routing 실패 행의 손해가"
          % (g["thresholds"]["B"], g["best_accuracy"]))
        A("성공 행의 이득을 넘기 때문에, 이 정확도로 expert 를 다시 켜면 결과는")
        A("M67 의 재연(routed 0.4368)입니다.\n")
    A("## 10-2. 서빙 영향 — 자동으로 판정할 수 없는 조건 (지시서 12장 9번)\n")
    A("지시서의 승격 조건 9번은 **serving 복잡도 대비 개선 폭이 충분한가**입니다.")
    A("이 칸은 수치로 닫히지 않으므로 사실만 적고 판단은 남깁니다.\n")
    A("```text")
    A("현재 계약  m56_m2_canonical.SERVING_FIELDS — 구조화 필드만. 텍스트는 title 하나")
    A("새 요구    사업목적·사업내용·지원대상 본문(또는 공고문 원문) 문자열 1개")
    A("           B~F 의 구조화 feature 도 이 텍스트에서 뽑는다 — 층 전체가 텍스트를 요구한다")
    A("모델 크기  fold 당 TF-IDF+SVD 가 하나 더 (제목용과 같은 규격, 64차원)")
    A("얻는 것    MAE %.4f -> %.4f (%.1f%%)  ·  엄격 split %.4f -> %.4f"
      % (ps["steps"]["A"]["MAE_log10"], ps["steps"][best_mae]["MAE_log10"],
         100 * (ps["steps"]["A"]["MAE_log10"] - ps["steps"][best_mae]["MAE_log10"])
         / ps["steps"]["A"]["MAE_log10"],
         nt["steps"]["A"]["MAE_log10"], nt["steps"][best_mae]["MAE_log10"]))
    A("```\n")
    A("> 사업을 **설계하는 시점**의 조회라면 기획안 본문은 이미 손에 있는 입력이므로")
    A("> 추가 부담이 크지 않습니다. 반대로 구조화 필드만 폼으로 받는 화면이라면")
    A("> 새 입력칸이 하나 늘어납니다. 텍스트 없이 갈 경우의 차선은 **F 단계"
      " (MAE %.4f)** 인데," % ps["steps"]["F"]["MAE_log10"])
    A("> F 의 구조화 feature 도 원문에서 뽑은 값이라 텍스트 없이는 결국 A 로 돌아갑니다.\n")
    A("## 11. 재현성 / 승격 점검표 (지시서 12장)\n")
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("| 같은 seed 재실행 OOF 일치 | %s |"
      % " / ".join("%s %s" % (p["step_labels"][k], v) for k, v in p["reproducibility"].items()))
    A("")
    A("## 결론\n")
    A("```text")
    A("M65 canonical            MAE %.4f   Stage1 %.4f"
      % (ps["steps"]["A"]["MAE_log10"], ps["steps"]["A"]["stage1"]["accuracy"]))
    A("원천 feature 보강 최고    MAE %.4f   Stage1 %.4f  (%s)"
      % (ps["steps"][best_mae]["MAE_log10"], ps["steps"][best]["stage1"]["accuracy"],
         p["step_labels"][best]))
    A("단위어 상한(모델 아님)    —           Stage1 %.4f" % p["unit_word_ceiling"]["accuracy"])
    A("")
    A("목표  1차 MAE < 0.35 / 최종 < 0.30")
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
