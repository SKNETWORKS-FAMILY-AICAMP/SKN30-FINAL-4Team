r"""M82-C — P3 End-to-End Serving Smoke.

지시서(사용자, `m82_final_result_and_next_experiments.md` 7장 B):

    신규 문서 -> 본문 추출 -> masking -> proximity regex extraction ->
    explicit proximity feature -> masked proximity TF-IDF/SVD ->
    기존 structured + title/body features -> M73 ordinal soft routing ->
    최종 expected amount

이 스크립트가 확인하는 것은 **학습이 아니라 추론 경로**다.

    1. 전체 1,877행으로 P3 파이프라인을 한 번 적합한다(fold 없음)
    2. '신규 문서' 두 종류를 그 파이프라인에 통과시킨다
         (a) 데이터셋에 있는 실제 행 하나를 빼고 학습해 그 행을 신규처럼 추론
             -> 예측이 실제 값 근처인지, OOF 와 같은 크기인지
         (b) 데이터셋에 없는 **합성 공고문 텍스트** — 처음 보는 문장
             -> 크래시 없이 흐르는지, 금액이 상식 범위인지
    3. 학습 때 만든 feature 차원·순서가 추론 때와 정확히 같은지

## 무엇을 덮고 무엇을 안 덮는가

    덮는다     masking -> proximity regex -> explicit feature -> masked
               proximity TF-IDF/SVD -> structured/title/body -> ordinal
               soft routing -> 금액
    안 덮는다  HWP/PDF 원문 -> 텍스트 추출 -> F06 스키마 (D04·E01·F04·F05·F06).
               M82 가 건드리지 않은 기존 경로라 여기서 다시 재지 않는다.

산출
    ml/reports/m82c_serving_smoke.json / .md
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
import m45_m2_amount as M45
import m69_m2_source_features as M69
import m73_m2_routing_improvement as M73
import m82_m2_proximity_features as M82

SRC = F6.OUT_V2
MD = C.report_path("m82c_serving_smoke.md")

# 처음 보는 합성 공고문. 실제 공고 문체를 흉내내되 데이터셋에 없는 문장이다.
SYNTHETIC_DOC = """□ 사업목적 : 지역 중소제조기업의 스마트공장 고도화 지원
□ 지원대상 : 업력 3년 이상 도내 중소제조기업 (신청자격 : 최근 연 매출 30억원 이하)
□ 지원내용 : 스마트공장 솔루션 구축비 및 자동화 설비 도입비 지원
   - 정부지원 비율은 총 사업비의 60% 이내이며, 자부담 40% 이상이어야 함
□ 지원규모 : 기업당 최대 4천만원 이내 (총 25개사 내외 선정)
□ 사업기간 : 협약체결일로부터 10개월
□ 지원조건 : 보조금 방식이며 융자는 해당하지 않음
□ 신청방법 : 온라인 접수 후 서류 제출
"""
SYNTHETIC_TITLE = "2026년 지역 중소제조기업 스마트공장 고도화 지원사업 모집 공고"


def fit_p3(Xs, y, titles, body, NB, P, tr, te):
    """P3 파이프라인 하나를 적합하고 te 행을 추론한다. 학습과 같은 변환 순서."""
    Xtr0, Xte0, _ = F.build_features(Xs, titles, tr, te, True, True)
    Xb_tr, Xb_te = M69.assemble(Xtr0, Xte0, NB, tr, te, body[tr], body[te],
                                M82.STEP, [None])
    p_tr = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[tr].reset_index(drop=True)
    p_te = P[M82.NUMERIC_COLS + M82.FLAG_COLS].iloc[te].reset_index(drop=True)
    a, b = M82.augment(Xb_tr, Xb_te, p_tr, p_te)
    sv_tr, sv_te = M82.fit_prox_svd(P["prox_context_text"].to_numpy()[tr],
                                    P["prox_context_text"].to_numpy()[te])
    nm = ["proxsvd%02d" % j for j in range(sv_tr.shape[1])]
    A, B = M82.augment(a, b, pd.DataFrame(sv_tr, columns=nm),
                       pd.DataFrame(sv_te, columns=nm))
    ytr = y[tr]

    # M73 soft/ordinal_xgb — 학습 때와 같은 블록
    edges = M73.bucket_edges(ytr)
    ztr = M73.to_bucket(ytr, edges)
    tab = np.zeros((len(B), 3))
    for k in range(3):
        m = ztr == k
        tab[:, k] = F.make_point_model().fit(A.iloc[m], ytr[m]).predict(B)
    pr = M73.stage1_proba("ordinal_xgb", A, ztr, B)
    pred = M73.route_soft(tab, pr)
    return {"pred": pred, "proba": pr, "table": tab, "edges": edges,
            "cols_train": list(map(str, A.columns)),
            "cols_infer": list(map(str, B.columns))}


def main():
    t0 = time.time()
    print("== 데이터")
    raw = pd.read_parquet(SRC)
    d, _ = M45.prepare(raw)
    d = d.reset_index(drop=True)
    fp = F.dataset_fingerprint(SRC)
    n = len(d)
    print("   %s / 행 %d" % (fp["path"], n))

    # ---------------------------------------------- (b) 합성 문서 행 붙이기
    # 구조화 필드는 실제 행 하나의 스키마를 그대로 쓰되 **텍스트만 새 것**으로
    # 바꾼다. 스키마를 새로 만들면 F06 계약이 깨져 무엇을 재는지 흐려진다.
    donor = int(np.argmax((d["cohort"] == "bizinfo").to_numpy()))
    syn = d.iloc[[donor]].copy()
    syn["row_id"] = "SMOKE_SYNTHETIC_0001"
    syn["title"] = SYNTHETIC_TITLE
    if "normalized_title" in syn.columns:
        syn["normalized_title"] = SYNTHETIC_TITLE
    if "program_stem" in syn.columns:
        syn["program_stem"] = "SMOKE_SYNTHETIC"
    syn["evidence_text"] = SYNTHETIC_DOC
    syn["evidence_source"] = "document"
    d2 = pd.concat([d, syn], ignore_index=True)
    print("   합성 신규 문서 1행 추가 -> %d행 (donor row %s)"
          % (len(d2), d.iloc[donor]["row_id"]))

    Xs, y, _, cats = M45.make_xy(d2, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d2)
    NB, body, src = SF.build(d2)
    P = M82.build_proximity(src, M82.WINDOW_PRIMARY)

    # 합성 문서에서 proximity 가 실제로 잡혔는지 (추출 단계 자체 확인)
    syn_p = P.iloc[len(d2) - 1]
    syn_prox = {k: (None if pd.isna(syn_p[k]) else float(syn_p[k]))
                for k in ("prox_support_rate", "prox_self_burden_rate",
                          "prox_selected_count", "prox_duration_months")}
    syn_ctx_digits = bool(pd.Series([syn_p["prox_context_text"]])
                          .str.contains(r"\d", regex=True).iloc[0])
    print("\n== 1. 합성 문서 proximity 추출")
    print("   %s" % syn_prox)
    print("   masking 확인 — 문맥에 숫자 잔존: %s (False 여야 함)" % syn_ctx_digits)
    print("   문맥(앞 100자): %s" % syn_p["prox_context_text"][:100])

    # ---------------------------------------------- 추론 (a) 실제 행 + (b) 합성
    real_i = int(np.argsort(-(d["cohort"] == "bizinfo").to_numpy().astype(int)
                            * np.arange(len(d)))[0])   # bizinfo 중 뒤쪽 행 하나
    te = np.array([real_i, len(d2) - 1])
    tr = np.array([i for i in range(len(d2)) if i not in set(te.tolist())])
    print("\n== 2. 전체 적합 후 추론 (train %d행 -> test %d행)" % (len(tr), len(te)))
    out = fit_p3(Xs, y, titles, body, NB, P, tr, te)

    pred_real, pred_syn = float(out["pred"][0]), float(out["pred"][1])
    true_real = float(y[real_i])
    won_real, won_syn = 10 ** pred_real, 10 ** pred_syn
    print("   (a) 실제 행 %s" % d.iloc[real_i]["row_id"])
    print("       예측 %.4f (%,.0f원) / 실제 %.4f (%,.0f원) / |오차| %.4f"
          .replace(",", "") % (pred_real, won_real, true_real, 10 ** true_real,
                               abs(pred_real - true_real)))
    print("   (b) 합성 신규 문서")
    print("       예측 %.4f (약 %s원)" % (pred_syn, format(int(won_syn), ",")))
    print("       구간확률 Low/Mid/High = %.3f / %.3f / %.3f"
          % tuple(out["proba"][1]))
    print("       구간 경계(원) %s" % [int(round(10 ** e)) for e in out["edges"]])

    # ---------------------------------------------- 3. 차원·순서
    dim_ok = len(out["cols_train"]) == len(out["cols_infer"])
    order_ok = out["cols_train"] == out["cols_infer"]
    print("\n== 3. feature 차원/순서")
    print("   train %d열 / infer %d열 — 차원 일치 %s · 순서 일치 %s"
          % (len(out["cols_train"]), len(out["cols_infer"]), dim_ok, order_ok))

    # ---------------------------------------------- 판정
    # 합성 문서의 '상식 범위' 는 학습 타깃의 1~99 분위로 잡는다 — 임의 상수를
    # 쓰면 그 상수가 판정을 만든다.
    lo, hi = float(np.percentile(y, 1)), float(np.percentile(y, 99))
    checks = {
        "1. 신규 문서가 크래시 없이 예측까지 도달": np.isfinite(pred_syn),
        "2. 합성 문서 proximity 가 실제로 추출됨":
            sum(1 for v in syn_prox.values() if v is not None) >= 2,
        "3. masking 후 문맥에 숫자 잔존 없음": not syn_ctx_digits,
        "4. 합성 예측이 학습 타깃 1~99 분위 안": lo <= pred_syn <= hi,
        "5. 실제 행 예측 오차가 OOF MAE 의 3배 이내(0.3518×3)":
            abs(pred_real - true_real) <= 3 * 0.3518,
        "6. feature 차원 일치": dim_ok,
        "7. feature 순서 일치": order_ok,
        "8. 구간확률이 정상 분포(합 1, 음수 없음)":
            bool(abs(out["proba"][1].sum() - 1) < 1e-6 and (out["proba"][1] >= 0).all()),
    }
    verdict = "PASS — serving 경로 정상" if all(checks.values()) else "FAIL"
    print("\n== smoke 점검표")
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   판정: %s" % verdict)

    payload = {
        "purpose": "P3 canonical 확정 전 end-to-end 추론 경로 smoke",
        "covers": "masking -> proximity regex -> explicit feature -> masked "
                  "proximity TF-IDF/SVD -> structured/title/body -> ordinal soft routing",
        "not_covers": "HWP/PDF -> 텍스트 추출 -> F06 스키마 (M82 가 바꾸지 않은 기존 경로)",
        "dataset": {"path": fp["path"], "sha256": fp["sha256"], "rows": int(n)},
        "synthetic_doc": {"title": SYNTHETIC_TITLE,
                          "proximity_extracted": syn_prox,
                          "context_digit_residue": bool(syn_ctx_digits),
                          "pred_log10": round(pred_syn, 4),
                          "pred_won": int(won_syn),
                          "bucket_proba": [round(float(x), 4) for x in out["proba"][1]],
                          "bucket_edges_won": [int(round(10 ** e)) for e in out["edges"]]},
        "real_row": {"row_id": str(d.iloc[real_i]["row_id"]),
                     "pred_log10": round(pred_real, 4), "true_log10": round(true_real, 4),
                     "abs_err": round(abs(pred_real - true_real), 4),
                     "pred_won": int(won_real), "true_won": int(10 ** true_real)},
        "feature_dims": {"train": len(out["cols_train"]), "infer": len(out["cols_infer"])},
        "checks": {k: bool(v) for k, v in checks.items()},
        "verdict": verdict,
        "seconds": round(time.time() - t0, 1),
    }
    C.save_report("m82c_serving_smoke.json", payload)

    L = ["# M82-C — P3 End-to-End Serving Smoke\n",
         "> 질문: **신규 공고문 하나가 masking → proximity → SVD → routing 을 거쳐 "
         "금액까지 정상적으로 나오는가?**\n",
         "## 경로\n", "```text\n신규 문서\n↓ 본문 추출 (기존 F06, 이 실험 범위 밖)\n"
         "↓ masking ([AMOUNT]/#)\n↓ proximity regex\n↓ explicit proximity feature\n"
         "↓ masked proximity TF-IDF/SVD\n↓ structured + title/body feature\n"
         "↓ M73 ordinal soft routing\n최종 금액\n```\n",
         "## 합성 신규 문서 결과\n",
         "```text\nproximity   %s\n예측        %.4f log10  (약 %s원)\n"
         "구간확률    Low %.3f / Mid %.3f / High %.3f\n구간경계    %s원\n```\n"
         % (syn_prox, pred_syn, format(int(won_syn), ","),
            out["proba"][1][0], out["proba"][1][1], out["proba"][1][2],
            [format(int(round(10 ** e)), ",") for e in out["edges"]]),
         "## 실제 행 대조\n",
         "```text\n행          %s\n예측        %.4f  (%s원)\n실제        %.4f  (%s원)\n"
         "|오차|      %.4f\n```\n"
         % (d.iloc[real_i]["row_id"], pred_real, format(int(won_real), ","),
            true_real, format(int(10 ** true_real), ","), abs(pred_real - true_real)),
         "## 점검표\n"]
    for k, ok in checks.items():
        L.append("- [%s] %s" % ("x" if ok else " ", k))
    L.append("\n## 판정\n\n```text\n%s\n```\n" % verdict)
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print("   [md] %s" % MD)
    print("\n총 %.0f초" % (time.time() - t0))
    return payload


if __name__ == "__main__":
    main()
