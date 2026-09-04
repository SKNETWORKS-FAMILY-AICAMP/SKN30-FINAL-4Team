"""최종 파이프라인 smoke test — 구조를 바꾼 뒤에도 같은 결과가 나오는지 확인한다.

무엇을 재는가 (전부 결과서에 적힌 수치와 대조한다):
  0  수집 표본(D02)·기준 테이블(F01~F05)이 리포트에 적힌 건수를 담고 있는가
  1  F06 변형이 얼어 있는 지문을 bit 단위로 재현하는가 (v1 / v2 / v3)
  2  모델 2 serving artifact 를 저장물만으로 불러 추론이 되는가
  3  모델 3 스코어링 스택이 pool 을 읽어 점수를 내는가
  4  모델 1 학습번들이 결과서에 적힌 행수를 담고 있는가

브랜치마다 가진 산출물이 다르다. **없는 단계는 [SKIP] 으로 지나가고 실패로
치지 않는다** — 이 저장소는 수집 → 피처 → ML → DL 로 브랜치가 나뉘어 있고
각 브랜치는 자기 단계까지만 갖고 있기 때문이다.

성공하면 exit 0. 실패한 항목이 있으면 [FAIL] 로 찍고 exit 1.
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

CODE = next((d for d in ("pipelines", "scripts") if os.path.isdir(os.path.join(_ML, d))), None)
PROC = os.path.join(_ML, "data", "processed")

# 결과서(성능 2.5·3.8 / 전처리 8.4·8.5)에 적힌 값
EXPECT = {
    "design_features.parquet":
        ("9f308112fb99e750fcbd4fdcba980a9a3ac698eaa623b99e60e205e11c8043e4", ["--legacy"]),
    "design_features_v2.parquet":
        ("eced88f6767e2e2460a05812dd8e9cd39990937e4c1a2a8363e02d114bfdc7f4", []),
    "design_features_v3.parquet":
        ("79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf", ["--supply"]),
}
FAILED = []
SKIPPED = []


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


def skip(name, why):
    print("  [SKIP] %s  — %s" % (name, why))
    SKIPPED.append(name)


def t00_collect():
    """수집 표본이 리포트에 적힌 건수를 담고 있는가 (D02 · D02B/D03)."""
    print("\n== 0-1 수집 표본 (D02)")
    import json
    import pandas as pd
    REPORTS = os.path.join(_ML, "reports")
    spec = [(("d02_sample.json",), "sampled", "list_sample.parquet"),
            (("d02b_targeted_sample.json", "d03_targeted_sample.json"), "sampled",
             "list_sample_targeted.parquet")]
    seen = 0
    for reps, key, out in spec:
        rp = next((os.path.join(REPORTS, r) for r in reps
                   if os.path.exists(os.path.join(REPORTS, r))), None)
        op = os.path.join(PROC, out)
        if rp is None or not os.path.exists(op):
            continue
        seen += 1
        want = json.load(open(rp, encoding="utf-8")).get(key)
        n = len(pd.read_parquet(op))
        check("%s = %s 건" % (out, format(want, ",")), n == want, "실측 %s" % format(n, ","))
    if seen == 0:
        skip("수집 표본 전체", "list_sample*.parquet 이 없다")


def t0_tables():
    """기준 테이블이 리포트에 적힌 행수를 담고 있는가 — F01~F06 정합성."""
    print("\n== 0 기준 테이블 (F01~F06)")
    import json
    import pandas as pd
    REPORTS = os.path.join(_ML, "reports")
    # (리포트, 행수 키, 산출물)
    spec = [("f01_master.json", "rows_final", "announcement_master.parquet"),
            ("f02_detail.json", "rows_final", "announcement_detail.parquet"),
            ("f03_taxonomy.json", "rows_final", "business_taxonomy.parquet"),
            ("f04_merge_documents.json", "rows", "announcement_detail_enriched.parquet"),
            ("f05_amount_observations.json", None, "support_amount_observations.parquet")]
    seen = 0
    for rep, key, out in spec:
        rp, op = os.path.join(REPORTS, rep), os.path.join(PROC, out)
        if not (os.path.exists(rp) and os.path.exists(op)):
            continue
        seen += 1
        n = len(pd.read_parquet(op))
        if key is None:
            check("%s 적재" % out, n > 0, "n=%d" % n)
            continue
        want = json.load(open(rp, encoding="utf-8")).get(key)
        check("%s = %s 행" % (out, format(want, ",")), n == want, "실측 %s" % format(n, ","))
    if seen == 0:
        skip("기준 테이블 전체", "F01~F05 산출물이 없다")


def t1_f06():
    """F06 변형 재현. 얼어 있는 파일 자체의 지문도 같이 확인한다."""
    print("\n== 1 F06 재현 (v1 legacy / v2 근거문 수정 / v3 공급 보강)")
    src = os.path.join(_ML, CODE, "f06_design_features.py")
    if not os.path.exists(src):
        skip("F06 전체", "f06_design_features.py 없음 (이 브랜치 범위 밖)")
        return
    have = [fn for fn in EXPECT if os.path.exists(os.path.join(PROC, fn))]
    if not have:
        skip("F06 전체", "design_features*.parquet 없음")
        return
    for fn in have:
        check("얼어 있는 %s" % fn, sha256(os.path.join(PROC, fn)) == EXPECT[fn][0], "지문 일치")
    with tempfile.TemporaryDirectory() as td:
        for fn in have:
            want, flags = EXPECT[fn]
            out = os.path.join(td, fn)
            r = subprocess.run([sys.executable, src, "--out", out] + flags,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", cwd=os.path.join(_ML, CODE))
            label = "F06 %s 재생성" % (" ".join(flags) or "(기본)")
            if r.returncode != 0:
                tail = [l for l in (r.stderr or "").strip().split("\n") if l.strip()][-1:]
                check(label, False, tail[0][:120] if tail else "실행 실패")
                continue
            check(label, sha256(out) == want, "재생성 지문이 얼어 있는 것과 일치")


def t2_model2():
    """저장물만으로 서빙되는가 — 학습 코드 경로를 타지 않는다."""
    print("\n== 2 모델 2 serving artifact")
    art = next((c for c in (os.path.join(_ML, "models", "m65_model2_canonical"),
                            os.path.join(PROC, "m65_model2_canonical"))
                if os.path.exists(os.path.join(c, "model2_canonical.joblib"))), None)
    v2 = os.path.join(PROC, "design_features_v2.parquet")
    if art is None or not os.path.exists(v2):
        skip("모델 2 전체", "canonical artifact 또는 design_features_v2 없음")
        return
    import joblib
    import pandas as pd
    bundle = joblib.load(os.path.join(art, "model2_canonical.joblib"))
    check("번들 로드", set(bundle) >= {"vectorizer", "svd", "point", "quantile", "meta"},
          os.path.relpath(art, _ML))

    import m56_m2_canonical as M56
    import m45_m2_amount as M45
    d, _ = M45.prepare(pd.read_parquet(v2))
    check("필터 후 행수 1,877 (성능결과서 2.5)", len(d) == 1877, "실측 %d" % len(d))

    out = M56.serve(bundle, d.head(20)[M56.SERVING_FIELDS].to_dict("records"))
    ok = (out["pred_log10"].notna().all() and out["pred_log10"].between(3, 12).all()
          and bool((out["lo_log10"] <= out["hi_log10"]).all()))
    check("추론 20건", ok, "pred_log10 %.3f~%.3f · 구간 순서 정상"
          % (out["pred_log10"].min(), out["pred_log10"].max()))


def t3_model3():
    """모델 3 은 저장된 가중치가 없다 — 비교군 대비 거리를 매번 계산하는 구조다."""
    print("\n== 3 모델 3 스코어링 스택")
    try:
        import m3_lab as L
    except ImportError:
        skip("모델 3 전체", "m3_lab.py 없음 (이 브랜치 범위 밖)")
        return
    v3 = os.path.join(PROC, "design_features_v3.parquet")
    v2 = os.path.join(PROC, "design_features_v2.parquet")
    path, expect_n, expect_fb = (v3, 2626, 0.0232) if os.path.exists(v3) else (v2, None, None)
    if not os.path.exists(path):
        skip("모델 3 전체", "design_features_v2/v3 없음")
        return
    pool = L.load_pool(path)
    tag = os.path.basename(path)
    if expect_n is None:
        check("pool 적재 (%s)" % tag, len(pool) > 0, "n=%d" % len(pool))
    else:
        check("v3 pool 2,626행 (성능결과서 3.8)", len(pool) == expect_n, "실측 %d" % len(pool))
    res = L.score_pool(pool, pool)
    fb = L.cohort_profile(res)["n_global_fallback"] / len(pool)
    if expect_fb is None:
        check("전역 fallback 산출", 0 <= fb < 0.2, "실측 %.2f%%" % (fb * 100))
    else:
        check("전역 fallback 2.32% (성능결과서 3.8)", abs(fb - expect_fb) < 0.002,
              "실측 %.2f%%" % (fb * 100))
    s = res["score"]
    check("이례성 점수 산출", s.notna().all() and s.min() >= 0,
          "n=%d · %.3f~%.3f" % (len(s), s.min(), s.max()))


def t4_model1():
    """모델 1 가중치는 저장소 밖(400MB+)이라 학습번들의 정합성만 확인한다."""
    print("\n== 4 모델 1 학습번들")
    b = next((c for c in (os.path.join(_ML, "models", "m1_dl_bundle"),
                          os.path.join(PROC, "m1_dl_bundle"))
              if os.path.exists(os.path.join(c, "train.parquet"))), None)
    if b is None:
        skip("모델 1 전체", "m1_dl_bundle 없음 (deep-learning 브랜치 산출물)")
        return
    import pandas as pd
    tr = pd.read_parquet(os.path.join(b, "train.parquet"))
    ex = pd.read_parquet(os.path.join(b, "external.parquet"))
    check("학습 1,404건 (성능결과서 1.1)", len(tr) == 1404, "실측 %d" % len(tr))
    check("외부 검증 131건 (성능결과서 1.2)", len(ex) == 131, "실측 %d" % len(ex))
    check("19클래스", tr["label"].nunique() == 19 if "label" in tr else True,
          "실측 %s" % (tr["label"].nunique() if "label" in tr else "label 컬럼 없음"))


if __name__ == "__main__":
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    print("smoke test — 브랜치 %s · 코드 위치 ml/%s/" % (branch or "?", CODE or "?"))
    if CODE is None:
        print("\nml/pipelines/ 도 ml/scripts/ 도 없다 — 검사할 것이 없다")
        sys.exit(0)
    for fn in (t00_collect, t0_tables, t1_f06, t2_model2, t3_model3, t4_model1):
        try:
            fn()
        except Exception as e:                                    # noqa: BLE001
            check(fn.__name__, False, "%s: %s" % (type(e).__name__, e))
    print("\n%s  실패 %d건 · 건너뜀 %d건%s"
          % ("SMOKE TEST 실패" if FAILED else "SMOKE TEST 통과",
             len(FAILED), len(SKIPPED), (" — " + ", ".join(FAILED)) if FAILED else ""))
    sys.exit(1 if FAILED else 0)
