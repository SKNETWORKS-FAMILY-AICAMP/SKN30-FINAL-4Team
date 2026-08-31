"""최종 파이프라인 smoke test — 재구성 전후로 같은 결과가 나오는지 확인한다.

무엇을 재는가 (전부 '문서에 적힌 수치'와 대조한다):
  1  F06 3변형이 얼어 있는 지문을 bit 단위로 재현하는가 (v1/v2/v3)
  2  모델 2 serving artifact 를 저장물만으로 불러 추론이 되는가
  3  모델 3 스코어링 스택이 v3 pool 을 읽어 점수를 내는가
  4  모델 1 학습번들이 문서에 적힌 행수를 담고 있는가

성공하면 exit 0. 실패한 항목은 [FAIL] 로 찍고 exit 1.
"""
import os
import sys
import hashlib
import tempfile
import subprocess

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_ML = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("pipelines", "evaluation", "experiments", "scripts"):
    _p = os.path.join(_ML, _d)
    if os.path.isdir(_p):
        sys.path.insert(0, _p)

CODE = next(d for d in ("pipelines", "scripts") if os.path.isdir(os.path.join(_ML, d)))
PROC = os.path.join(_ML, "data", "processed")

# 문서(성능결과서 2.5·3.8, 전처리결과서 8.4)에 적힌 값
EXPECT = {
    "design_features.parquet":
        "9f308112fb99e750fcbd4fdcba980a9a3ac698eaa623b99e60e205e11c8043e4",
    "design_features_v2.parquet":
        "eced88f6767e2e2460a05812dd8e9cd39990937e4c1a2a8363e02d114bfdc7f4",
    "design_features_v3.parquet":
        "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf",
}
FAILED = []


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(name, ok, detail=""):
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  — " + detail) if detail else ""))
    if not ok:
        FAILED.append(name)


def t1_f06():
    """F06 3변형 재현. 얼어 있는 파일 자체의 지문도 같이 확인한다."""
    print("\n== 1 F06 재현 (v1 legacy / v2 근거문 수정 / v3 공급 보강)")
    for fn, want in EXPECT.items():
        p = os.path.join(PROC, fn)
        if not os.path.exists(p):
            check("얼어 있는 %s" % fn, False, "파일 없음")
            continue
        check("얼어 있는 %s" % fn, sha256(p) == want, "지문 일치")

    src = os.path.join(_ML, CODE, "f06_design_features.py")
    with tempfile.TemporaryDirectory() as td:
        for fn, flags in (("design_features.parquet", ["--legacy"]),
                          ("design_features_v2.parquet", []),
                          ("design_features_v3.parquet", ["--supply"])):
            out = os.path.join(td, fn)
            r = subprocess.run([sys.executable, src, "--out", out] + flags,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=os.path.join(_ML, CODE))
            if r.returncode != 0:
                check("F06 %s 재생성" % (" ".join(flags) or "(기본)"), False,
                      (r.stderr or "").strip().splitlines()[-1:] or "실행 실패")
                continue
            check("F06 %s 재생성" % (" ".join(flags) or "(기본)"),
                  sha256(out) == EXPECT[fn], "재생성 지문이 얼어 있는 것과 일치")


def t2_model2():
    """저장물만으로 서빙되는가 — 학습 코드 경로를 타지 않는다."""
    print("\n== 2 모델 2 serving artifact")
    import joblib
    import pandas as pd
    art = None
    for cand in (os.path.join(_ML, "models", "m65_model2_canonical"),
                 os.path.join(PROC, "m65_model2_canonical")):
        if os.path.exists(os.path.join(cand, "model2_canonical.joblib")):
            art = cand
            break
    if art is None:
        check("artifact 존재", False, "model2_canonical.joblib 없음")
        return
    bundle = joblib.load(os.path.join(art, "model2_canonical.joblib"))
    check("번들 로드", set(bundle) >= {"vectorizer", "svd", "point", "quantile", "meta"},
          os.path.relpath(art, _ML))

    import m56_m2_canonical as M56
    import m45_m2_amount as M45
    raw = pd.read_parquet(os.path.join(PROC, "design_features_v2.parquet"))
    d, _ = M45.prepare(raw)
    check("필터 후 행수 1,877 (성능결과서 2.5)", len(d) == 1877, "실측 %d" % len(d))

    recs = d.head(20)[M56.SERVING_FIELDS].to_dict("records")
    out = M56.serve(bundle, recs)
    finite = out["pred_log10"].notna().all() and out["pred_log10"].between(3, 12).all()
    ordered = bool((out["lo_log10"] <= out["hi_log10"]).all())
    check("추론 20건", finite and ordered,
          "pred_log10 %.3f~%.3f · 구간 순서 정상"
          % (out["pred_log10"].min(), out["pred_log10"].max()))


def t3_model3():
    """모델 3 은 저장된 모델이 없다 — 비교군 대비 거리를 매번 계산하는 구조다."""
    print("\n== 3 모델 3 스코어링 스택")
    import m3_lab as L
    v3 = os.path.join(PROC, "design_features_v3.parquet")
    if not os.path.exists(v3):
        check("design_features_v3.parquet", False, "파일 없음")
        return
    pool = L.load_pool(v3)
    check("v3 pool 2,626행 (성능결과서 3.8)", len(pool) == 2626, "실측 %d" % len(pool))
    res = L.score_pool(pool, pool)
    prof = L.cohort_profile(res)
    fb = prof["n_global_fallback"] / len(pool)
    check("전역 fallback 2.32% (성능결과서 3.8)", abs(fb - 0.0232) < 0.002,
          "실측 %.2f%%" % (fb * 100))
    s = res["score"]
    check("이례성 점수 산출", s.notna().all() and s.min() >= 0,
          "n=%d · %.3f~%.3f" % (len(s), s.min(), s.max()))


def t4_model1():
    """모델 1 가중치는 저장소 밖(400MB+)이라 학습번들의 정합성만 확인한다."""
    print("\n== 4 모델 1 학습번들")
    import pandas as pd
    b = None
    for cand in (os.path.join(_ML, "models", "m1_dl_bundle"),
                 os.path.join(PROC, "m1_dl_bundle")):
        if os.path.exists(os.path.join(cand, "train.parquet")):
            b = cand
            break
    if b is None:
        check("번들 존재", False, "m1_dl_bundle/train.parquet 없음")
        return
    tr = pd.read_parquet(os.path.join(b, "train.parquet"))
    ex = pd.read_parquet(os.path.join(b, "external.parquet"))
    check("학습 1,404건 (성능결과서 1.1)", len(tr) == 1404, "실측 %d" % len(tr))
    check("외부 검증 131건 (성능결과서 1.2)", len(ex) == 131, "실측 %d" % len(ex))
    check("19클래스", tr["label"].nunique() == 19 if "label" in tr else True,
          "실측 %s" % (tr["label"].nunique() if "label" in tr else "label 컬럼 없음"))


if __name__ == "__main__":
    print("smoke test — 코드 위치: ml/%s/" % CODE)
    for fn in (t1_f06, t2_model2, t3_model3, t4_model1):
        try:
            fn()
        except Exception as e:                                    # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(e).__name__, e))
    print("\n%s  실패 %d건%s"
          % ("SMOKE TEST 실패" if FAILED else "SMOKE TEST 통과",
             len(FAILED), (" — " + ", ".join(FAILED)) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
