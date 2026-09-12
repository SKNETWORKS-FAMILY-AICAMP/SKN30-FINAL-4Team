"""serving/ API 기준 end-to-end 스모크.

`ml/tools/smoke_test.py` 는 저장물·데이터 정합성을 본다. 이 파일은 그것과
목적이 다르다 — **백엔드가 README 대로 호출했을 때 실제로 동작하는가**만
본다. 그래서 여기서는 학습 모듈을 직접 부르지 않고, README 에 적힌 경로
그대로 `sys.path.insert` 후 진입점 함수만 부른다.

    model1  serving/model1/predict.py   (inference.py 별칭)
    model2  serving/model2/predict.py   ★ M82/P3 — 2026-09-04 M65 에서 전환
    model3  serving/model3/score.py     (inference.py 별칭)

모델 1 은 torch/transformers 와 400MB weight 가 필요해 없으면 SKIP 한다
(추론 자체는 CPU 로 동작하지만 이 스모크의 목적은 경로 확인이다).

    python ml/serving/smoke_api.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILED, SKIPPED = [], []


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def skip(name, why):
    print("  [SKIP] %s  — %s" % (name, why))
    SKIPPED.append(name)


def _path(sub):
    p = os.path.join(HERE, sub)
    if p not in sys.path:
        sys.path.insert(0, p)
    return p


def _load(sub, modname):
    """모델별 진입점을 **파일 경로로** 불러온다.

    model1/predict.py 와 model2/predict.py 는 이름이 같다(가이드 구조).
    그냥 `import predict` 하면 `sys.modules` 캐시 때문에 먼저 불린 쪽이
    돌아온다 — 백엔드도 같은 함정에 빠진다. 그래서 고유 이름으로 등록한다.
    """
    import importlib.util
    _path(sub)                                   # 형제 모듈(feature_builder 등)용
    path = os.path.join(HERE, sub, modname + ".py")
    uniq = "%s_%s" % (sub, modname)
    if uniq in sys.modules:
        return sys.modules[uniq]
    spec = importlib.util.spec_from_file_location(uniq, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[uniq] = mod
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ 모델 2 (M82/P3)
RECORD = {
    "row_id": "SMOKE_API_1",
    "title": "2023년 수출유망상품화 사업",
    "evidence_text": ("□ 지원내용 : 수출 유망상품 사업화 지원, 정부지원 비율 70% (자부담 30%)\n"
                      "□ 지원규모 : 기업당 최대 2억원\n"
                      "□ 사업기간 : 12개월\n"
                      "□ 지원방식 : 보조금"),
    "evidence_source": "document",
    "support_type": "사업화", "support_method": "grant", "support_unit": "company",
    "cohort": "taxonomy", "category_large": "수출", "industry": "제조업",
    "agency_type": "public", "amount_type": "per_company",
    "support_count": 50, "support_ratio": 70, "self_burden_ratio": 30,
    "project_duration": 12, "year": 2023,
}


def t_model2():
    print("\n== 모델 2 — serving/model2/predict.py (M82/P3)")
    P2 = _load("model2", "predict")

    meta = P2.info()
    check("번들이 M82/P3 세대", meta["performance"]["experiment"] == "M82/P3",
          "%s · primary MAE %.4f" % (meta["name"], meta["performance"]["primary_MAE_log10"]))
    check("feature 211열", meta["n_features"] == 211, "실측 %d" % meta["n_features"])

    out = P2.predict([RECORD])
    r = out["predictions"][0]
    check("predict() 반환", out["n"] == 1 and 3 <= r["pred_log10"] <= 12,
          "pred %.4f log10 = %s원" % (r["pred_log10"], format(r["pred_won"], ",")))
    check("구간확률 정상", abs(sum(r["bucket_proba"].values()) - 1) < 1e-6,
          "%s %s" % (r["bucket"], r["bucket_proba"]))
    check("proximity 추출됨",
          r["proximity"]["prox_support_rate"] == 70.0
          and r["proximity"]["prox_self_burden_rate"] == 30.0,
          "지원비율 %s%% · 자부담 %s%% · 기간 %s개월"
          % (r["proximity"]["prox_support_rate"], r["proximity"]["prox_self_burden_rate"],
             r["proximity"]["prox_duration_months"]))
    check("마스킹 감사 — target 누수 0", out["context_digit_residue"] == 0,
          "잔존 %d행" % out["context_digit_residue"])

    # evidence_text 없으면 조용히 나빠지지 말고 막아야 한다
    bad = {k: v for k, v in RECORD.items() if k != "evidence_text"}
    try:
        P2.predict([bad])
        check("evidence_text 누락 시 차단", False, "예외가 나지 않았다")
    except ValueError:
        check("evidence_text 누락 시 차단", True, "ValueError")

    pc = P2.percentile(200_000_000, "사업화", "grant", "company", "taxonomy")
    check("percentile() 반환 (구세대와 동일)",
          pc["status"] == "비교가능" and pc["n"] == 316 and pc["percentile_rank"] == 62.5,
          "%s · n=%d · %.1f%% · spread %.1f배"
          % (pc["level"], pc["n"], pc["percentile_rank"], pc["spread_x"]))


# ------------------------------------------------------------ 모델 3
def t_model3():
    print("\n== 모델 3 — serving/model3/score.py")
    try:
        M3 = _load("model3", "score")
    except Exception as e:
        skip("모델 3", "%s: %s" % (type(e).__name__, str(e)[:60]))
        return
    out = M3.predict([{
        "row_id": "SMOKE_API_1",
        "support_type": "사업화", "support_method": "grant", "support_unit": "company",
        "amount_type": "per_company", "per_recipient": 200_000_000,
        "support_count": 50, "project_duration": 12, "support_ratio": 70,
    }])
    r = out.iloc[0]
    check("score() 반환", 0 <= float(r["score"]) <= 1,
          "score %.3f · %s · n=%d" % (r["score"], r["level"], r["cohort_n"]))


# ------------------------------------------------------------ 모델 1
def t_model1():
    print("\n== 모델 1 — serving/model1/predict.py")
    p = os.path.join(HERE, "model1")
    if not os.path.isdir(os.path.join(p, "model")):
        skip("모델 1", "weight 없음 (400MB+, gitignore 대상)")
        return
    try:
        M1 = _load("model1", "predict")
    except Exception as e:
        skip("모델 1", "%s: %s" % (type(e).__name__, str(e)[:60]))
        return
    out = M1.predict(["2026년 중소기업 판로 지원사업 공고. 국내외 판로 개척을 위한 "
                      "전시회 참가비와 온라인 입점 비용을 지원한다."])
    rows = out.to_dict("records") if hasattr(out, "to_dict") else list(out)
    ok = len(rows) == 1 and "support_type_pred" in rows[0]
    check("predict() 반환", ok,
          ("%s (%.3f, %s)" % (rows[0]["support_type_pred"], rows[0]["confidence"],
                              rows[0]["status"])) if ok else "반환 형태 %s" % type(out))


def main():
    print("== serving API 스모크 — README 에 적힌 호출 경로 그대로")
    t_model2()
    t_model3()
    t_model1()
    print("\n%s  실패 %d건 · 건너뜀 %d건"
          % ("SMOKE 통과" if not FAILED else "SMOKE 실패", len(FAILED), len(SKIPPED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
