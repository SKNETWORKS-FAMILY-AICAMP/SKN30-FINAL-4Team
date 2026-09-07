"""사전협의서 Adapter 회귀 검사 — 구현계획 v2 의 필수 테스트 12항목.

저장소에 pytest 가 없으므로 `ml/tools/smoke_test.py` 와 같은 방식으로 쓴다.
통과하면 exit 0, 하나라도 실패하면 [FAIL] 을 찍고 exit 1.

    python ml/serving/shared/test_preconsultation_adapter.py
    python ml/serving/shared/test_preconsultation_adapter.py --with-smoke

fixture(`fixtures/preconsultation_example.txt`)는 실제 사전협의서 예시 문서를
**운영 파서로 추출한 결과**다 — backend `RhwpDocumentParser`(rhwp-python 0.8.1).
손으로 뽑은 텍스트를 쓰면 서비스가 실제로 보는 것과 다른 것을 검사하게 된다.
실측: 수기 추출본에는 제어레코드 오독으로 생긴 깨진 줄이 섞여 있었고, 그
차이만으로 모델 2 예측이 503,223,872 → 521,132,928 로 3.6% 움직였다.

이 문서가 회귀 기준이다 — 값이 바뀌면 어댑터가 아니라 문서 해석이 바뀐
것이므로 여기서 멈춘다.
"""
import hashlib
import os
import subprocess
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, "..", ".."))
_ROOT = os.path.dirname(_ML)
for _p in (_HERE, os.path.join(_ML, "serving", "model2"),
           os.path.join(_ML, "serving", "model3")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import preconsultation_adapter as PA        # noqa: E402

FIXTURE = os.path.join(_HERE, "fixtures", "preconsultation_example.txt")

# 학습 경로가 얼어 있는지 확인하는 지문. 공용 파서와 canonical artifact 는
# 이 작업에서 **한 글자도 바뀌면 안 된다** — 바뀌면 모델 2 재학습·모델 3
# 재검증이 따라온다. 경로가 없으면 SKIP 한다(브랜치마다 산출물이 다르다).
FROZEN = {
    "ml/models/model2_canonical/model2_p3_bundle.joblib":
        "0c5b93e2a04c778ace8e07d7551b1fc7c3cc2b91cde80d94b2b1b1cf38cbabff",
    "ml/serving/model2/model2_canonical.joblib":
        "d07a1d75e12eaece10643a734a26375920783264ee83f903c7fbe584c2f43a2d",
    "ml/serving/model3/design_features_v3.parquet":
        "79649c095b1775832b73c18c629eca7695688a3eaa3554f3af3b96565a23a0bf",
    "ml/pipelines/shared/amount_parser.py":
        "9f5043fa70e99062d02b37001562a2f3912cf409ff4bfa4705e1998b88e159e5",
    "ml/pipelines/shared/f06_design_features.py":
        "5e40c527c98bc1a92f6d15941b4d141f9b29251e79c1a11f721dfca98545653a",
}

# 문서에서 뽑을 수 없는 분류축. 모델 1 의 출력과 수집 메타가 채워야 할 자리다.
BASE = {"support_type": "연구개발", "support_method": "grant",
        "support_unit": "project", "title": "ICT지원사업",
        "cohort": "연구개발|grant", "year": 2024}

_fail = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  — %s" % detail) if detail else ""))
    if not cond:
        _fail.append(name)
    return cond


def skip(name, why):
    print("  [SKIP] %s  — %s" % (name, why))


def main():
    with open(FIXTURE, encoding="utf-8") as fh:
        text = fh.read()
    meta = PA.adapt(text, row_id="FIXTURE001", base=dict(BASE))
    f = meta["features"]

    print("== 1~4 기간 의미")
    _, cands, rejected = PA.extract_durations(text)
    check("1 백틱 축약연도가 기간으로 오인되지 않는다",
          all(c["value"] != 28.0 for c in cands)
          and meta["project_duration_years"] != 28.0,
          "기간 후보 %d개 · 28.0 없음" % len(cands))
    check("2 사업기간 5년과 지원기간 3년이 분리된다",
          meta["program_duration_years"] == 5.0
          and meta["project_duration_years"] == 3.0,
          "program=%s · project=%s" % (meta["program_duration_years"],
                                       meta["project_duration_years"]))
    # 기본 정책은 학습 규칙(B) — 기존 pool 이 학습 규칙으로 만들어져 있어
    # 신규 요청만 지원기간으로 바꾸면 같은 축에서 다른 것을 재게 된다.
    check("3a 기본 정책은 학습 규칙 → project_duration 5.0",
          f["project_duration"] == 5.0
          and meta["duration_policy"] == PA.DURATION_POLICY_TRAINING,
          "값 %s · 근거 %s" % (f["project_duration"], meta["duration_basis"]))
    alt = PA.adapt(text, base=dict(BASE),
                   duration_policy=PA.DURATION_POLICY_SUPPORT_FIRST)
    check("3b 지원기간 우선 정책을 고르면 3.0",
          alt["features"]["project_duration"] == 3.0
          and alt["duration_basis"] == "지원기간",
          "값 %s" % alt["features"]["project_duration"])
    check("3c 두 의미는 정책과 무관하게 항상 보존된다",
          meta["duration_evidence"]["training_rule_value"] == 5.0
          and meta["duration_evidence"]["support_period_value"] == 3.0,
          "학습규칙 %s · 지원기간 %s"
          % (meta["duration_evidence"]["training_rule_value"],
             meta["duration_evidence"]["support_period_value"]))

    # sanity guard 는 fixture 에서 발동하지 않는다(백틱 정규화로 28 이 애초에
    # 후보가 아니다). 규칙 자체가 사는지는 합성 입력으로 확인한다.
    syn = PA.adapt("◦(사업기간) 30년\n ㅇ (지원기간) 최대 15년")
    _, _, syn_rej = PA.extract_durations("◦(사업기간) 30년\n ㅇ (지원기간) 최대 15년")
    check("4 10년 초과 기간은 context 등급이어도 채택되지 않는다",
          syn["features"]["project_duration"] is None
          and syn["project_duration_years"] is None
          and syn["program_duration_years"] is None
          and len(syn_rej) == 2
          and all(r["reason"] == "duration_out_of_range" for r in syn_rej)
          and any(r.get("reason") == "duration_out_of_range" for r in syn["review"]),
          "반려 %d건 · 모델값 None" % len(syn_rej))

    print("== 5~6 지원건수 의미")
    vals = [(c["value"], c["semantic"]) for c in meta["support_count_candidates"]]
    check("5 4과제 / 6과제를 직접 인식한다",
          (4, "new_projects_company_innovation") in vals
          and (6, "new_projects_market_expansion") in vals,
          str(vals))
    check("6 3차년도 지원과제(4개)를 신규과제 수로 쓰지 않는다",
          f["support_count"] is None
          and meta["support_count_basis"] == "multiple_semantic_candidates"
          and (4, "third_year_selected_projects") in vals,
          "대표값 %s · 근거 %s" % (f["support_count"], meta["support_count_basis"]))

    print("== 7~8 없는 값 (생성 금지)")
    check("7 없는 support_ratio 를 만들지 않는다", f["support_ratio"] is None,
          repr(f["support_ratio"]))
    check("8 없는 self_burden_ratio 를 만들지 않는다",
          f["self_burden_ratio"] is None, repr(f["self_burden_ratio"]))

    print("== 금액 (참고 — 기존 파서가 이미 맞게 뽑던 부분)")
    a = meta["amounts"]
    check("금액 semantic 이 유지된다",
          a["support_amount_min"] == 9e8 and a["support_amount_max"] == 15e8
          and a["support_amount_type"] == "per_project"
          and a["support_per_project_annual"] == 6e8,
          "%s~%s · %s · 연간 %s" % (a["support_amount_min"], a["support_amount_max"],
                                   a["support_amount_type"],
                                   a["support_per_project_annual"]))

    print("== 9 모델 2 추론")
    try:
        import predict as M2
        out = M2.predict_document(text, base=dict(BASE))
        r = out["predictions"][0]
        check("9 기존 canonical 번들로 추론이 된다",
              r["pred_won"] > 0 and r["input_completeness"] in ("partial", "sparse")
              and "support_ratio" in r["missing_features"],
              "pred_won=%d · %s · 누락 %d개"
              % (r["pred_won"], r["input_completeness"], len(r["missing_features"])))
    except FileNotFoundError as e:
        skip("9 모델 2 추론", "번들 없음: %s" % e)

    print("== 10 모델 3 축 유효성")
    try:
        import score as M3
        v = M3.axis_validity(meta)
        conf = M3.confidence(v)
        check("10 축 유효성이 의미까지 본다",
              v["project_duration"]["valid"] is True
              and v["project_duration"].get("note") == "training_rule_parity"
              and v["project_duration"].get("semantic") == "지원기간"
              and v["log_support_count"]["valid"] is False
              and v["log_support_count"]["reason"] == "multiple_semantic_candidates"
              and conf["n_valid_axes"] == 2 and conf["confidence"] == "medium",
              "유효축 %s · %s" % (conf["axes_used"], conf["status"]))
    except Exception as e:                                  # noqa: BLE001
        check("10 축 유효성이 의미까지 본다", False, "%s: %s" % (type(e).__name__, e))

    print("== 11 학습 경로 동결")
    for rel, want in FROZEN.items():
        p = os.path.join(_ROOT, rel)
        if not os.path.exists(p):
            skip("11 %s" % rel, "파일 없음")
            continue
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        check("11 %s" % os.path.basename(rel), h == want, h[:16])

    if "--with-smoke" in sys.argv:
        print("== 12 기존 smoke test")
        rc = subprocess.run([sys.executable, os.path.join(_ML, "tools", "smoke_test.py")],
                            capture_output=True, text=True, encoding="utf-8")
        tail = [ln for ln in rc.stdout.splitlines() if ln.strip()][-1:]
        check("12 smoke test 통과", rc.returncode == 0, tail[0] if tail else "")
    else:
        skip("12 기존 smoke test", "--with-smoke 로 함께 돌린다")

    print()
    if _fail:
        print("실패 %d건: %s" % (len(_fail), _fail))
        return 1
    print("ADAPTER 회귀 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
