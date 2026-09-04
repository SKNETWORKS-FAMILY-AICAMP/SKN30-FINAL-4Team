r"""M70 — M69 의 본문 텍스트 표현만 튜닝해 MAE 0.35 아래로 갈 수 있는가.

지시서(사용자, `model2_m69_body_text_tuning_experiment_plan.md`):

    M69 는 본문 텍스트 feature 로 M65 를 유의하게 이겼다(0.4117 -> 0.3719).
    이번에는 **모델 구조와 구조화 feature 는 유지하고, 본문 텍스트 표현만**
    튜닝해 0.35 이하를 노린다. 한 번에 grid search 하지 말고 phase 별로
    best 만 다음 단계로 넘긴다.

바꾸지 않는 것 — M69 와 비교가 성립하려면 아래가 같아야 한다.

    데이터셋   design_features_v2.parquet, M45.prepare, 1,877행
    타깃       log10(per_recipient), basis=stated_cap
    split      GroupKFold(5), group=program_stem (엄격 기준 normalized_title)
    모델       m2_features.XGB_POINT 그대로 (새 튜닝 없음)
    구조화     M69 의 G 단계 feature 층 전체 (`SF.columns_upto("G")`)
    제목       m2_features 의 제목 SVD64 (D 단계에서만 대조군으로 건드린다)
    마스킹     금액 표현 -> [AMOUNT], 남은 숫자 -> '#'  (지시서 7장 고정 규칙)

바뀌는 것은 **본문 텍스트를 어떤 행렬로 만드는가** 하나다.

## phase (지시서 8장)

    Phase 1  SVD 차원        64 / 128 / 256
    Phase 2  char n-gram     2~3 / 2~4 / 3~5
    Phase 3  word TF-IDF     없음 / unigram / 1~2gram
    Phase 4  제목·본문 encoder 분리 여부
    Phase 5  본문 선택        전체 / 앞부분 / section 우선 / 중복 제거

각 phase 는 **직전 phase 의 승자 위에서만** 다음 축을 움직인다. 승자는
program_stem OOF MAE 로 고른다.

## Phase 4 에 대한 사실 정정

지시서 5장은 "제목과 본문을 각각 독립적으로 임베딩한다"를 새 실험으로 적었지만,
**M69 는 이미 그렇게 되어 있다** — 제목 TF-IDF->SVD64 와 본문 TF-IDF->SVD 가
따로 적합되어 concat 된다. 그래서 이 phase 는 '분리를 도입하는' 실험이 아니라
**분리가 맞았는지 확인하는** 실험으로 바꿔 잡았다. 대조군은 제목과 본문을 한
문자열로 이어 붙여 TF-IDF 를 하나만 적합하는 결합형이다. 결합형이 지면 현행
분리가 옳았다는 뜻이고, 이기면 그때 바꾸면 된다.

## 누수 (지시서 7장)

매 실험마다 두 칸을 다시 센다 — 본문 선택 방식을 바꾸면 마스킹을 통과하는
문자열도 바뀌기 때문이다.

    raw amount leakage          최종 텍스트에 금액 표현이 남은 행
    unmasked numeric target clue 최종 텍스트에 숫자가 남은 행

산출
    ml/data/processed/m70_body_tuning_oof.parquet
    ml/reports/m70_m2_body_text_tuning.json / .md
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import f06_design_features as F6
import m2_features as F
import m2_source_features as SF
import m45_m2_amount as M45

SRC = F6.OUT_V2
M69_OOF = os.path.join(C.PROC, "m69_source_oof.parquet")
OUT_OOF = os.path.join(C.PROC, "m70_body_tuning_oof.parquet")
MD = C.report_path("m70_m2_body_text_tuning.md")

# M69 공표치. 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M69_PUBLISHED = {"MAE_log10": 0.3719, "within_2x": 0.530, "within_3x": 0.723,
                 "strict_MAE": 0.3931}
BASELINE = "M69 baseline"
GOAL_1, GOAL_2 = 0.35, 0.30
REPRO_TOL = 1e-9

# 지시서 10장 8번 "serving 비용 증가가 감당 가능"의 판정선.
#
# 배수로 걸면 안 된다 — SVD 성분 행렬은 (성분수 x 어휘수) 라 차원을 2배로
# 올리면 용량도 정확히 2배가 되고, 그러면 이 칸은 '차원을 올렸는가'를 다시
# 묻는 칸이 되어 버린다(스모크 실측: SVD128 이 baseline 의 정확히 2.00배).
# 그래서 이 프로젝트가 **이미 서빙하는 것**을 기준으로 절대선을 잡는다.
#
#     모델 1 KLUE-BERT      423 MB
#     모델 2 현행 번들        11 MB
#
# 텍스트 인코더 100 MB 는 모델 1 의 1/4 이고 모델 2 번들과 합쳐도 모델 1 보다
# 작다. 배수와 절대값을 둘 다 리포트에 남기고, 판정은 절대선으로 한다.
ARTIFACT_CAP_MB = 100.0
SERVED_REFERENCE = {"model1_klue_bert_MB": 423, "model2_current_bundle_MB": 11}


# ------------------------------------------------------------ 본문 변형
# 지시서 6장 E2 — '사업개요/지원대상/지원내용' 계열 표제. F06 이 이미 쓰는
# 어휘를 그대로 가져온다(METHOD_CTX_RE + SCOPE_RE). 표제 어휘를 여기서 새로
# 지으면 F06 과 M70 이 다른 '본문'을 보게 된다.
SECTION_RE = re.compile(r"사업\s*개요|사업\s*목적|사업\s*내용|지원\s*대상|지원\s*내용|"
                        r"지원\s*분야|지원\s*사항|지원\s*자격|신청\s*자격|추진\s*내용|"
                        r"모집\s*분야|사업\s*설명")
SECTION_WINDOW = 600
SECTION_MAX = 4000
HEAD_CAP = 1000
_SENT = re.compile(r"[\n\r]+|(?<=[.。!?])\s+")


def body_full(raw):
    return SF.mask_text(raw)


def body_head(raw):
    """E1 앞부분 중심. 공고문은 앞쪽에 개요가 오고 뒤쪽은 서식·유의사항이다."""
    return SF.mask_text(raw, cap=HEAD_CAP)


def body_section(raw):
    """E2 표제 뒤 window 만 이어 붙인다. 표제가 없으면 전체로 후퇴한다.

    taxonomy 행의 raw 는 이미 '사업목적+사업내용+지원대상'이라 표제어가 없다.
    그때 빈 문자열을 돌려주면 이 실험이 '문서 코호트만 남기고 taxonomy 를
    지운 실험'이 되어 버린다 — 그래서 후퇴 경로를 둔다.
    """
    hits = [raw[m.start():m.start() + SECTION_WINDOW] for m in SECTION_RE.finditer(raw)]
    joined = " ".join(hits)[:SECTION_MAX]
    return SF.mask_text(joined if joined.strip() else raw)


def body_dedup(raw):
    """E3 중복 문장 제거. 공고문은 같은 문구가 표·본문·유의사항에 반복된다."""
    seen, out = set(), []
    for s in _SENT.split(SF.mask_text(raw)):
        k = s.strip()
        if len(k) < 4 or k in seen:
            continue
        seen.add(k)
        out.append(k)
    return " ".join(out)


BODY_VARIANTS = {"full": body_full, "head": body_head,
                 "section": body_section, "dedup": body_dedup}


# ------------------------------------------------------------ 텍스트 규격
def spec(name, body="full", char_ngram=(2, 3), char_svd=64,
         word_ngram=None, word_svd=32, join_title=False):
    return {"name": name, "body": body, "char_ngram": tuple(char_ngram),
            "char_svd": char_svd, "word_ngram": (tuple(word_ngram) if word_ngram else None),
            "word_svd": word_svd, "join_title": join_title}


def spec_label(s):
    p = ["body=%s" % s["body"], "char%d~%d" % s["char_ngram"], "SVD%d" % s["char_svd"]]
    if s["word_ngram"]:
        p.append("word%d~%d/SVD%d" % (s["word_ngram"] + (s["word_svd"],)))
    if s["join_title"]:
        p.append("제목결합")
    return " · ".join(p)


def _tfidf_svd(analyzer, ngram, n_comp, train_txt, test_txt, prefix):
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    v = TfidfVectorizer(analyzer=analyzer, ngram_range=ngram, min_df=3,
                        max_features=30000, sublinear_tf=True)
    A = v.fit_transform(train_txt)
    # 어휘가 적어 성분 수를 못 채우는 fold 가 있을 수 있다. 조용히 줄이지 않고
    # 실제 성분 수를 돌려줘 리포트에 남긴다.
    k = int(min(n_comp, max(1, A.shape[1] - 1)))
    svd = TruncatedSVD(n_components=k, random_state=F.PIPELINE_SEED)
    a = svd.fit_transform(A)
    b = svd.transform(v.transform(test_txt))
    cols = ["%s%02d" % (prefix, i) for i in range(k)]
    return (pd.DataFrame(a, columns=cols), pd.DataFrame(b, columns=cols),
            (v, svd), float(svd.explained_variance_ratio_.sum()))


def build_text(s, titles, body, tr, te, Xs):
    """규격 `s` 의 fold 단위 텍스트 블록.

    반환을 **앞쪽(구조화+제목)과 뒤쪽(본문)으로 나눠서** 돌려준다. 최종 열 순서를
    M69 와 똑같이 맞춰야 하기 때문이다 —

        M69 assemble:  [구조화 + 제목SVD] + [NB 구조화 층] + [본문SVD]

    `colsample_bytree=0.8` 이라 **열 순서가 바뀌면 각 트리가 보는 열 집합이
    바뀐다.** 처음 이 파일은 본문을 NB 앞에 붙였고, 그것만으로 baseline 이
    0.3719 대신 0.3706 으로 재현에 실패했다. 성능 차이가 아니라 순서 차이다.
    """
    evr, tail = {}, []
    if s["join_title"]:
        # 대조군 — 제목과 본문을 한 문자열로 붙여 TF-IDF 를 하나만 적합한다.
        joined = np.array([t + " " + b for t, b in zip(titles, body)])
        a0, b0, _ = F.build_features(Xs, titles, tr, te, True, False)
        ta, tb, art, e = _tfidf_svd("char_wb", s["char_ngram"], s["char_svd"],
                                    joined[tr], joined[te], "joint_svd")
        evr["joint_char"] = round(e, 4)
        arts = [art]
        tail.append((ta, tb))
    else:
        a0, b0, _ = F.build_features(Xs, titles, tr, te, True, True)
        ta, tb, art, e = _tfidf_svd("char_wb", s["char_ngram"], s["char_svd"],
                                    body[tr], body[te], "body_svd")
        evr["body_char"] = round(e, 4)
        arts = [art]
        tail.append((ta, tb))
        if s["word_ngram"]:
            wa, wb, wart, we = _tfidf_svd("word", s["word_ngram"], s["word_svd"],
                                          body[tr], body[te], "body_word")
            evr["body_word"] = round(we, 4)
            arts.append(wart)
            tail.append((wa, wb))
    head = (a0.reset_index(drop=True), b0.reset_index(drop=True))
    return head, tail, arts, evr


# ------------------------------------------------------------ 실행
def run_config(s, Xs, y, groups, titles, NB, body):
    """한 규격의 5-fold OOF. 구조화 층(G)은 항상 붙는다."""
    from sklearn.model_selection import GroupKFold

    cols = SF.columns_upto("G")
    n = len(y)
    pred = np.zeros(n)
    fold_id = np.zeros(n, dtype=int)
    per_fold, evrs, art_bytes = [], [], 0
    t0 = time.time()
    for i, (tr, te) in enumerate(GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups)):
        fold_id[te] = i
        head, tail, arts, evr = build_text(s, titles, body, tr, te, Xs)
        # 열 순서는 M69 그대로 — [구조화+제목] + [NB] + [본문]. build_text 참조.
        Xtr = pd.concat([head[0], NB.iloc[tr][cols].reset_index(drop=True)]
                        + [t[0] for t in tail], axis=1)
        Xte = pd.concat([head[1], NB.iloc[te][cols].reset_index(drop=True)]
                        + [t[1] for t in tail], axis=1)
        pred[te] = F.make_point_model().fit(Xtr, y[tr]).predict(Xte)
        per_fold.append(round(float(np.abs(pred[te] - y[te]).mean()), 4))
        evrs.append(evr)
        if i == 0:
            art_bytes = len(pickle.dumps(arts, protocol=pickle.HIGHEST_PROTOCOL))
    return {"pred": pred, "fold_id": fold_id, "per_fold_MAE": per_fold,
            "seconds": round(time.time() - t0, 1),
            "explained_variance": {k: round(float(np.mean([e[k] for e in evrs])), 4)
                                   for k in evrs[0]},
            "text_artifact_bytes": int(art_bytes)}


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


def subset_mae(y, p, d):
    out = {}
    for k, idx in d.groupby("cohort", observed=True).groups.items():
        i = d.index.get_indexer(idx)
        out[str(k)] = {"n": int(len(i)),
                       "MAE_log10": round(float(np.abs(p[i] - y[i]).mean()), 4)}
    return out


def summarize(s, run, y, d, base_pred):
    met = M45.point_metrics(y, run["pred"])
    met["name"] = s["name"]
    met["spec"] = spec_label(s)
    met["per_fold_MAE"] = run["per_fold_MAE"]
    met["fold_std"] = round(float(np.std(run["per_fold_MAE"])), 4)
    met["cohort"] = subset_mae(y, run["pred"], d)
    met["seconds"] = run["seconds"]
    met["explained_variance"] = run["explained_variance"]
    met["text_artifact_bytes"] = run["text_artifact_bytes"]
    if base_pred is not None:
        met["vs_baseline"] = paired_test(y, run["pred"], base_pred)
        met["fold_wins"] = int(sum(1 for a, b in zip(run["per_fold_MAE"],
                                                     BASE_FOLDS) if a < b))
    return met


def variant_overlap(bodies, d):
    """Phase 5 의 본문 변형이 **실제로 다른 문자열인가**를 근거원별로 센다.

    전체 중앙 길이만 보면 네 변형이 거의 같아 보인다(395 / 395 / 382 / 394).
    taxonomy 행의 본문이 239자라 자르고 말 것이 없기 때문이다. 그 숫자만 싣고
    '변형이 효과 없었다'고 쓰면 **실험을 안 한 것을 실험 결과로 읽는 것**이
    된다. 그래서 코호트별로 나눠, 긴 공고문(document 744행)에서 변형이 실제로
    몇 %의 행을 바꿨는지 함께 남긴다.
    """
    out = {}
    for src in pd.unique(d["evidence_source"].to_numpy()):
        m = (d["evidence_source"] == src).to_numpy()
        row = {"n": int(m.sum())}
        for k, v in bodies.items():
            row[k] = {
                "median_len": int(np.median([len(x) for x in v[m]])),
                "identical_to_full": round(float(np.mean(
                    [a == b for a, b in zip(v[m], bodies["full"][m])])), 3),
            }
        out[str(src)] = row
    return out


def noise_band(results, baseline_name):
    """모든 후보가 baseline 주변 몇 폭 안에 있는가 — 승자 선정의 해석 규율.

    phase 마다 최소 MAE 를 승자로 뽑는 절차는 **차이가 없을 때도 반드시 승자를
    만들어낸다.** 실제로 A2 는 baseline 을 0.0001 로 '이겼다'. 그래서 승자
    이름 옆에 항상 이 대역을 같이 둔다 — 대역 안이면 그 승자는 측정이 아니라
    표본 흔들림이다.
    """
    b = results[baseline_name]["MAE_log10"]
    deltas = {k: round(m["MAE_log10"] - b, 4)
              for k, m in results.items() if k != baseline_name}
    ci_crosses = {k: bool(m["vs_baseline"]["ci95"][0] < 0 < m["vs_baseline"]["ci95"][1])
                  for k, m in results.items() if k != baseline_name}
    return {
        "baseline_MAE": b,
        "delta_range": [min(deltas.values()), max(deltas.values())],
        "n_candidates": len(deltas),
        "n_with_ci_crossing_zero": int(sum(ci_crosses.values())),
        "n_significant": int(len(ci_crosses) - sum(ci_crosses.values())),
        "deltas": deltas,
    }


def text_leakage(body):
    """지시서 7장 — 실험마다 다시 센다. 본문 선택이 바뀌면 남는 문자열도 바뀐다."""
    digits = sum(1 for b in body if re.search(r"\d", b))
    amounts = sum(1 for b in body if F.AMOUNT_IN_TITLE.search(b))
    return {"unmasked_numeric_target_clue": "%d / %d" % (digits, len(body)),
            "raw_amount_leakage": "%d / %d" % (amounts, len(body)),
            "pass": bool(digits == 0 and amounts == 0)}


BASE_FOLDS = []


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
    print("   %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    NB, body69, src = SF.build(d)
    _, raw_body = SF.raw_bodies(d)
    bodies = {k: np.array([fn(t) for t in raw_body]) for k, fn in BODY_VARIANTS.items()}
    assert np.array_equal(bodies["full"], body69), "full 변형이 M69 본문과 달라졌다"
    print("   본문 변형 길이 중앙값: "
          + " ".join("%s=%d" % (k, int(np.median([len(x) for x in v])))
                     for k, v in bodies.items()))

    leak = {k: text_leakage(v) for k, v in bodies.items()}
    print("\n== 0. 누수 고정 규칙 (지시서 7장) — 본문 변형별")
    for k, v in leak.items():
        print("   %-8s 숫자 잔존 %s · 금액 표현 잔존 %s → %s"
              % (k, v["unmasked_numeric_target_clue"], v["raw_amount_leakage"],
                 "PASS" if v["pass"] else "FAIL"))

    # ------------------------------------------------------- Phase 0 재현
    print("\n== 1. M69 baseline 재현 (지시서 1장)")
    base_spec = spec(BASELINE)
    run0 = run_config(base_spec, Xs, y, groups["program_stem"], titles, NB,
                      bodies[base_spec["body"]])
    global BASE_FOLDS
    BASE_FOLDS = run0["per_fold_MAE"]
    base = summarize(base_spec, run0, y, d, None)
    m69 = pd.read_parquet(M69_OOF)
    same = bool(np.allclose(run0["pred"], m69["pred_G"].to_numpy(), atol=REPRO_TOL))
    print("   MAE %.4f (기대 %.4f) · 2배내 %.1f%% (기대 %.1f%%) · 3배내 %.1f%% (기대 %.1f%%)"
          % (base["MAE_log10"], M69_PUBLISHED["MAE_log10"],
             100 * base["within_2x"], 100 * M69_PUBLISHED["within_2x"],
             100 * base["within_3x"], 100 * M69_PUBLISHED["within_3x"]))
    print("   M69 저장 OOF 와 행 단위 일치: %s" % same)
    if not (same and abs(base["MAE_log10"] - M69_PUBLISHED["MAE_log10"]) < 5e-4):
        print("   재현 실패 — 지시서 1장에 따라 튜닝을 중단한다.")
        C.save_report("m70_m2_body_text_tuning.json",
                      {"verdict": "baseline 재현 실패 — 튜닝 중단",
                       "baseline": base, "expected": M69_PUBLISHED,
                       "row_level_match": same})
        return None
    print("   재현 통과 — 튜닝을 진행한다.")

    # ------------------------------------------------------- phase 정의
    phases = [
        ("Phase 1 — SVD 차원", lambda w: [
            spec("A1 SVD128", char_svd=128, **_carry(w, "char_svd")),
            spec("A2 SVD256", char_svd=256, **_carry(w, "char_svd"))]),
        ("Phase 2 — char n-gram", lambda w: [
            spec("B1 char2~4", char_ngram=(2, 4), **_carry(w, "char_ngram")),
            spec("B2 char3~5", char_ngram=(3, 5), **_carry(w, "char_ngram"))]),
        ("Phase 3 — word TF-IDF", lambda w: [
            spec("C1 +word1", word_ngram=(1, 1), **_carry(w, "word_ngram")),
            spec("C2 +word1~2", word_ngram=(1, 2), **_carry(w, "word_ngram"))]),
        ("Phase 4 — 제목/본문 encoder", lambda w: [
            spec("D1 제목결합(대조군)", join_title=True, **_carry(w, "join_title"))]),
        ("Phase 5 — 본문 선택", lambda w: [
            spec("E1 앞부분", body="head", **_carry(w, "body")),
            spec("E2 section 우선", body="section", **_carry(w, "body")),
            spec("E3 중복 제거", body="dedup", **_carry(w, "body"))]),
    ]

    results = {BASELINE: base}
    preds = {BASELINE: run0["pred"]}
    specs = {BASELINE: base_spec}
    winner, winner_spec = BASELINE, base_spec
    phase_log = []
    for title, maker in phases:
        print("\n== %s  (직전 승자: %s)" % (title, winner))
        entries = maker(winner_spec)
        for s in entries:
            specs[s["name"]] = s
            run = run_config(s, Xs, y, groups["program_stem"], titles, NB,
                             bodies[s["body"]])
            met = summarize(s, run, y, d, run0["pred"])
            results[s["name"]] = met
            preds[s["name"]] = run["pred"]
            v = met["vs_baseline"]
            print("   %-18s MAE %.4f (foldσ %.4f)  Δ%+0.4f  CI[%+0.4f,%+0.4f]  "
                  "p=%-9s fold승 %d/5  %ds  %.1fMB"
                  % (s["name"], met["MAE_log10"], met["fold_std"], v["delta_MAE"],
                     v["ci95"][0], v["ci95"][1], v["wilcoxon_p"], met["fold_wins"],
                     met["seconds"], met["text_artifact_bytes"] / 1e6))
        pool = [winner] + [s["name"] for s in entries]
        new = min(pool, key=lambda k: results[k]["MAE_log10"])
        phase_log.append({
            "phase": title, "candidates": [s["name"] for s in entries],
            "carried_in": winner, "winner": new,
            "MAE": {k: results[k]["MAE_log10"] for k in pool},
            "changed": bool(new != winner),
        })
        if new != winner:
            winner = new
            winner_spec = next(s for s in entries if s["name"] == new)
            print("   -> 승자 교체: %s" % winner)
        else:
            print("   -> 유지: %s (후보가 baseline 을 못 넘음)" % winner)

    # ------------------------------------------------------- 엄격 split
    strict_names = sorted({BASELINE, winner}
                          | {p["winner"] for p in phase_log}, key=list(results).index)
    print("\n== 엄격 split [normalized_title] — %s" % ", ".join(strict_names))
    strict = {}
    for name in strict_names:
        s = specs[name]
        run = run_config(s, Xs, y, groups["normalized_title"], titles, NB,
                         bodies[s["body"]])
        strict[name] = {"MAE_log10": round(float(np.abs(run["pred"] - y).mean()), 4),
                        "per_fold_MAE": run["per_fold_MAE"],
                        "cohort": subset_mae(y, run["pred"], d)}
        print("   %-18s strict MAE %.4f  (fold %s)"
              % (name, strict[name]["MAE_log10"], run["per_fold_MAE"]))

    # ------------------------------------------------------- 재현성
    print("\n== 재현성 — 같은 seed 로 승자를 한 번 더")
    rerun = run_config(winner_spec, Xs, y, groups["program_stem"], titles, NB,
                       bodies[winner_spec["body"]])
    repro = bool(np.allclose(rerun["pred"], preds[winner], atol=REPRO_TOL))
    print("   %s 재실행 OOF 일치: %s" % (winner, repro))

    # ------------------------------------------------------- 승격 판정
    W = results[winner]
    v = W.get("vs_baseline")
    sb, sw = strict[BASELINE]["MAE_log10"], strict[winner]["MAE_log10"]
    coh = {k: (W["cohort"][k]["MAE_log10"] < base["cohort"][k]["MAE_log10"])
           for k in W["cohort"]}
    checks = {
        "1. OOF MAE 가 0.3719 보다 감소": W["MAE_log10"] < base["MAE_log10"],
        "2. strict split 에서도 개선 유지": sw < sb,
        "3. 5개 fold 중 4개 이상 개선": (W.get("fold_wins", 0) >= 4),
        "4. paired 95% CI 가 0 아래": bool(v and v["ci95"][1] < 0),
        "5. leakage audit PASS": leak[winner_spec["body"]]["pass"],
        "6. reproducibility PASS": repro,
        "7. taxonomy / bizinfo 한쪽에만 의존하지 않음": all(coh.values()),
        "8. serving 비용 증가가 감당 가능 (텍스트 artifact <= %d MB)" % ARTIFACT_CAP_MB:
            W["text_artifact_bytes"] / 1e6 <= ARTIFACT_CAP_MB,
    }
    verdict = ("승격 후보 (M69 대체)" if (winner != BASELINE and all(checks.values()))
               else "현행 유지 (M69)")
    print("\n== 승격 점검표 (지시서 10장) — 대상: %s" % winner)
    if winner == BASELINE:
        print("   승자가 baseline 이다 — 어떤 변형도 M69 를 넘지 못했다.")
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)
    print("   목표 대비: 1차 %.2f %s / 최종 %.2f %s"
          % (GOAL_1, "달성" if W["MAE_log10"] < GOAL_1 else "미달",
             GOAL_2, "달성" if W["MAE_log10"] < GOAL_2 else "미달"))

    pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y,
                  "fold": run0["fold_id"], "cohort": d["cohort"].to_numpy(),
                  **{("pred_" + k.split()[0]): p for k, p in preds.items()}}
                 ).to_parquet(OUT_OOF, index=False)
    print("   [oof] %s" % OUT_OOF)

    payload = {
        "purpose": "본문 텍스트 표현만 튜닝해 M69 의 0.3719 을 0.35 아래로 낮출 수 있는가",
        "unchanged": {"dataset": fp["path"], "sha256": fp["sha256"],
                      "rows": fp["rows_after_filters"], "model": F.XGB_POINT,
                      "structured_layer": "M69 G 단계 전체",
                      "masking": SF.manifest()["text_masking"]},
        "changed": "본문 텍스트를 어떤 행렬로 만드는가 (SVD 차원 · n-gram · word · encoder 분리 · 본문 선택)",
        "baseline_reproduction": {"MAE_log10": base["MAE_log10"],
                                  "expected": M69_PUBLISHED,
                                  "row_level_match_with_m69_oof": same},
        "body_variants": {k: {"median_len": int(np.median([len(x) for x in v])),
                              "leakage": leak[k]} for k, v in bodies.items()},
        "variant_overlap": variant_overlap(bodies, d),
        "noise_band": noise_band(results, BASELINE),
        "phases": phase_log,
        "results": results,
        "strict_split": strict,
        "winner": winner, "winner_spec": winner_spec,
        "reproducibility": repro,
        "promotion_checks": {k: bool(x) for k, x in checks.items()},
        "verdict": verdict,
        "artifact_budget": {
            "cap_MB": ARTIFACT_CAP_MB, "already_served_MB": SERVED_REFERENCE,
            "winner_MB": round(W["text_artifact_bytes"] / 1e6, 1),
            "vs_baseline_x": round(W["text_artifact_bytes"]
                                   / max(base["text_artifact_bytes"], 1), 2),
            "note": "배수로 걸면 SVD 차원 증가를 기계적으로 탈락시킨다 — "
                    "이미 서빙 중인 모델 1(423MB)·모델 2(11MB) 기준의 절대선으로 판정한다",
        },
        "goals": {"first": GOAL_1, "final": GOAL_2,
                  "first_met": bool(W["MAE_log10"] < GOAL_1),
                  "final_met": bool(W["MAE_log10"] < GOAL_2)},
        "published_m69": M69_PUBLISHED,
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "seconds": round(time.time() - t_all, 1),
    }
    C.save_report("m70_m2_body_text_tuning.json", payload)
    write_md(payload)
    print("\n총 %.0f초" % (time.time() - t_all))
    return payload


def _carry(w, axis):
    """직전 승자의 규격에서 이번에 움직이는 축만 빼고 물려받는다."""
    keys = ("body", "char_ngram", "char_svd", "word_ngram", "word_svd", "join_title")
    return {k: w[k] for k in keys if k != axis}


# ------------------------------------------------------------ md
def write_md(p):
    R = p["results"]
    L = []
    A = L.append
    A("# M70 — M69 본문 텍스트 표현 튜닝\n")
    A("> 질문: **M69 에서 확인된 본문 텍스트 신호를 더 적절한 TF-IDF / SVD 표현으로")
    A("> 압축하면, MAE 0.3719 을 0.35 이하로 낮출 수 있는가?**\n")
    A("## 0. 같은 조건 / 바뀐 것\n")
    u = p["unchanged"]
    A("```text")
    A("dataset  %s  (%d행)" % (u["dataset"], u["rows"]))
    A("sha256   %s" % u["sha256"])
    A("고정      모델(XGB_POINT) · 구조화 feature(%s) · 마스킹 규칙" % u["structured_layer"])
    A("바뀐 것  %s" % p["changed"])
    A("```\n")
    A("## 1. baseline 재현 (지시서 1장)\n")
    b = p["baseline_reproduction"]
    A("```text")
    A("OOF MAE     %.4f   (기대 %.4f)" % (b["MAE_log10"], b["expected"]["MAE_log10"]))
    A("Within 2x   %.1f%%   (기대 %.1f%%)"
      % (100 * R[BASELINE]["within_2x"], 100 * b["expected"]["within_2x"]))
    A("Within 3x   %.1f%%   (기대 %.1f%%)"
      % (100 * R[BASELINE]["within_3x"], 100 * b["expected"]["within_3x"]))
    A("M69 저장 OOF 와 행 단위 일치: %s" % b["row_level_match_with_m69_oof"])
    A("```\n")
    A("## 2. 누수 고정 규칙 (지시서 7장) — 본문 변형별\n")
    A("| 본문 변형 | 길이 중앙값 | 숫자 잔존 | 금액 표현 잔존 | 판정 |")
    A("|---|---:|---:|---:|---|")
    for k, v in p["body_variants"].items():
        A("| %s | %d | %s | %s | %s |"
          % (k, v["median_len"], v["leakage"]["unmasked_numeric_target_clue"],
             v["leakage"]["raw_amount_leakage"], "PASS" if v["leakage"]["pass"] else "FAIL"))
    A("")
    vo = p.get("variant_overlap")
    if vo:
        A("### 2-2. 본문 변형이 실제로 다른 문자열인가 (근거원별)\n")
        A("| 근거원 | n | " + " | ".join("%s 길이 / full과 동일" % k
                                       for k in p["body_variants"]) + " |")
        A("|---|---:|" + "---|" * len(p["body_variants"]))
        for src, row in vo.items():
            A("| %s | %d | " % (src, row["n"])
              + " | ".join("%d / %.1f%%" % (row[k]["median_len"],
                                            100 * row[k]["identical_to_full"])
                           for k in p["body_variants"]) + " |")
        A("")
        A("> 전체 중앙 길이만 보면 네 변형이 거의 같아 보이지만(395/395/382/394), 그건")
        A("> taxonomy 행 본문이 239자라 **자르고 말 것이 없어서**입니다. 실제 검증 대상인")
        A("> 긴 공고문(`document` 744행)에서는 `head` 가 2,594자를 1,000자로 줄이고")
        A("> `dedup` 이 100% 의 행을 바꿉니다 — Phase 5 는 헛돈 것이 아니라, 제대로")
        A("> 바꾼 뒤에도 이기지 못한 것입니다.\n")
    nb = p.get("noise_band")
    if nb:
        A("## 2-3. 노이즈 대역 — 승자를 어떻게 읽어야 하는가\n")
        A("```text")
        A("baseline MAE            %.4f" % nb["baseline_MAE"])
        A("후보 %d개의 ΔMAE 범위    %+0.4f ~ %+0.4f"
          % (nb["n_candidates"], nb["delta_range"][0], nb["delta_range"][1]))
        A("95%% CI 가 0 을 가로지른 후보  %d / %d"
          % (nb["n_with_ci_crossing_zero"], nb["n_candidates"]))
        A("0 을 넘지 않은 후보(유의)      %d / %d"
          % (nb["n_significant"], nb["n_candidates"]))
        A("```\n")
        A("> phase 마다 최소 MAE 를 승자로 뽑는 절차는 **차이가 없을 때도 반드시")
        A("> 승자를 만들어냅니다.** 실제로 Phase 1 의 A2 는 baseline 을 0.0001 로")
        A("> '이겼습니다'. 아래 표의 승자 이름은 절차의 산물이지 측정 결과가")
        A("> 아닙니다 — 판정 근거는 CI 와 fold 승수입니다.\n")
    A("## 3. 전체 결과\n")
    A("| 설정 | 규격 | MAE | fold σ | 2배 이내 | 3배 이내 | ΔMAE vs M69 | 95% CI | "
      "wilcoxon p | fold승 | taxonomy | bizinfo | 설명분산 | 학습(초) | 텍스트 artifact |")
    A("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for k, m in R.items():
        v = m.get("vs_baseline")
        evr = " / ".join("%.3f" % x for x in m["explained_variance"].values())
        A("| %s | %s | %.4f | %.4f | %.1f%% | %.1f%% | %s | %s | %s | %s | %.4f | %.4f | "
          "%s | %d | %.1f MB |"
          % (k, m["spec"], m["MAE_log10"], m["fold_std"], 100 * m["within_2x"],
             100 * m["within_3x"],
             ("%+0.4f" % v["delta_MAE"]) if v else "—",
             ("[%+0.4f, %+0.4f]" % tuple(v["ci95"])) if v else "—",
             (str(v["wilcoxon_p"]) if v else "—"),
             ("%d/5" % m["fold_wins"]) if v else "—",
             m["cohort"]["taxonomy"]["MAE_log10"], m["cohort"]["bizinfo"]["MAE_log10"],
             evr, m["seconds"], m["text_artifact_bytes"] / 1e6))
    A("")
    A("### fold별 MAE\n")
    A("| 설정 | " + " | ".join("fold %d" % i for i in range(5)) + " |")
    A("|---|" + "---:|" * 5)
    for k, m in R.items():
        A("| %s | " % k + " | ".join("%.4f" % x for x in m["per_fold_MAE"]) + " |")
    A("")
    A("## 4. phase 진행 (지시서 8장 — 각 phase 의 best 만 다음으로)\n")
    A("| phase | 물려받은 승자 | 후보 | 새 승자 | 교체 |")
    A("|---|---|---|---|---|")
    for ph in p["phases"]:
        A("| %s | %s | %s | %s | %s |"
          % (ph["phase"], ph["carried_in"], ", ".join(ph["candidates"]),
             ph["winner"], "예" if ph["changed"] else "아니오"))
    A("")
    A("> Phase 4 에 대한 정정: 지시서 5장의 '제목/본문 encoder 분리'는 **M69 가 이미")
    A("> 그렇게 되어 있습니다** — 제목 SVD64 와 본문 SVD 를 따로 적합해 concat 합니다.")
    A("> 그래서 이 phase 는 분리를 도입하는 실험이 아니라, 둘을 한 문자열로 이어 붙인")
    A("> **결합형 대조군**을 세워 분리가 옳았는지 확인하는 실험으로 돌렸습니다.\n")
    A("## 5. 엄격 split (normalized_title)\n")
    A("| 설정 | strict MAE | fold별 |")
    A("|---|---:|---|")
    for k, v in p["strict_split"].items():
        A("| %s | %.4f | %s |" % (k, v["MAE_log10"],
                                  ", ".join("%.4f" % x for x in v["per_fold_MAE"])))
    A("")
    ab = p.get("artifact_budget")
    if ab:
        A("## 5-2. serving 비용 판정선 (지시서 10장 8번)\n")
        A("```text")
        A("이미 서빙 중  모델 1 KLUE-BERT %d MB · 모델 2 현행 번들 %d MB"
          % (ab["already_served_MB"]["model1_klue_bert_MB"],
             ab["already_served_MB"]["model2_current_bundle_MB"]))
        A("판정선        텍스트 인코더 %.0f MB 이하" % ab["cap_MB"])
        A("승자          %.1f MB  (baseline 의 %.2f배)" % (ab["winner_MB"], ab["vs_baseline_x"]))
        A("```\n")
        A("> %s\n" % ab["note"])
    A("## 6. 승격 점검표 (지시서 10장) — 대상: %s\n" % p["winner"])
    A("| 조건 | 결과 |")
    A("|---|---|")
    for k, ok in p["promotion_checks"].items():
        A("| %s | %s |" % (k, "통과" if ok else "미달"))
    A("| 같은 seed 재실행 OOF 일치 | %s |" % p["reproducibility"])
    A("")
    A("## 결론\n")
    W = R[p["winner"]]
    A("```text")
    A("M69 baseline   MAE %.4f" % R[BASELINE]["MAE_log10"])
    A("M70 승자       MAE %.4f   (%s)" % (W["MAE_log10"], p["winner"]))
    A("")
    A("목표  1차 MAE < %.2f  -> %s" % (p["goals"]["first"],
                                     "달성" if p["goals"]["first_met"] else "미달"))
    A("      최종 MAE < %.2f -> %s" % (p["goals"]["final"],
                                      "달성" if p["goals"]["final_met"] else "미달"))
    A("판정: %s" % p["verdict"])
    A("```")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
