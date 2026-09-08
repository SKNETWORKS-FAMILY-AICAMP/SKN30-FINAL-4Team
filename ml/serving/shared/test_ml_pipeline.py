"""ML Orchestrator 회귀 검사 — Model 1 Runner · Result envelope · MIN_AXES gate.

    python ml/serving/shared/test_ml_pipeline.py            (Model 1 제외, 빠름)
    python ml/serving/shared/test_ml_pipeline.py --with-model1

Model 1 은 442MB 가중치를 올리므로 기본에서는 건너뛴다. CPL 입력은 실제
사전협의서 fixture 문장을 13개 CplFieldCode 에 담아 만든다 — 합성 문자열이
아니라 그 문서가 실제로 담고 있는 문장이다.
"""
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVING = os.path.abspath(os.path.join(_HERE, ".."))
for _p in (_HERE, os.path.join(_SERVING, "model1"),
           os.path.join(_SERVING, "model2"), os.path.join(_SERVING, "model3")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import result_envelope as RE                   # noqa: E402
import input_builder as IB                     # noqa: E402
import ml_orchestrator as ORCH                 # noqa: E402
import preconsultation_adapter as PA           # noqa: E402

FIXTURE = os.path.join(_HERE, "fixtures", "preconsultation_example.txt")

BASE = {"support_method": "grant", "support_unit": "project",
        "title": "ICT지원사업", "cohort": "taxonomy", "year": 2024}

_fail = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  — %s" % detail) if detail else ""))
    if not cond:
        _fail.append(name)
    return cond


def skip(name, why):
    print("  [SKIP] %s  — %s" % (name, why))


def _occ(text, block_id):
    return {"raw_text": text, "block_id": block_id, "extraction_method": "RULE",
            "section_path": [], "source_locator": {}}


def build_cpl():
    """실제 사전협의서 문장으로 채운 CPL 결과(13필드)."""
    present = {
        "PURPOSE_GOAL": ("ICT혁신기업이 신시장 창출 동력 확보를 위한 전략적 협업을 통해 "
                         "고성장 기업으로 도약할 수 있도록 시장・수요예측 기반 단계별 "
                         "기술개발 지원"),
        "NEW_OR_CHANGED_CONTENT": ("시장수요최적화 R&D + 고성장기업도약 R&D. "
                                   "선정과제에 대해 시장예측전문가 플랫폼을 활용하여 "
                                   "과제별 전문가를 매칭, 先시장・수요검증 + 後기술개발 추진"),
        "TARGET_AND_CONDITIONS": ("전략적 제휴완료 또는 예정인 ICT중소・벤처기업(법인). "
                                  "기업혁신형은 중소-중소 기업간 M&A를 한 ICT중소기업, "
                                  "시장개척형은 전략적 제휴를 계획하고 있는 ICT중소기업"),
        "SUPPORT_CONTENT_AND_SCALE": "과제당 총 9억원~15억원, 연간 6억원 / 3차년 선별지원",
        "BUSINESS_PERIOD": "'24 ~ '28(5년), 지원기간 최대 3년(2년+1년)",
        "BUDGET": "35,300백만원('24년 3,300백만원)",
        "LEGAL_BASIS": "정보통신 진흥 및 융합 활성화 등에 관한 특별법 제18조, 제32조",
        "LINKED_POLICY": "국정과제 78. 세계 최고의 네트워크 구축 및 디지털 혁신 가속화",
        "BUSINESS_NEED": "신시장 불확실성 우려를 해소하고 빠른 시장수요 변화에 적기대응",
        "REQUEST_TYPE": "세부사업 신설",
        "IMPLEMENTATION_PLAN": "사업 공고 및 선정 → 수요기반 시장검증 → 기술개발 → 최종평가",
        "DELIVERY_SYSTEM": "주무부처 - 전문기관 - 협력기관 - 수행기관",
        "EXPECTED_EFFECTS_AND_PERFORMANCE": "사업화성공률, 10억원당 사업화 매출액, 전략적 제휴 건수",
    }
    return {"ruleset_version": "test", "items": [
        {"field_code": k, "status": "PRESENT", "occurrences": [_occ(v, "blk-%02d" % i)],
         "reason_code": None, "explanation": None}
        for i, (k, v) in enumerate(present.items())]}


def main():
    cpl = build_cpl()
    with open(FIXTURE, encoding="utf-8") as fh:
        text = fh.read()
    meta = PA.adapt(text, row_id="AN-TEST-0001", base=dict(BASE))
    record = dict(meta["features"])

    print("== 1 Model 1 입력 조립 (CPL -> text_for_model)")
    built = IB.build_model1_input(cpl, title="ICT지원사업")
    check("1a 학습 4필드가 CPL 에서 채워진다",
          all(built["fields"][f] for f in IB.MODEL1_FIELDS),
          "누락 %s" % (built["missing_fields"] or "없음"))
    check("1b 결합 순서가 title→purpose→content→target",
          built["text"].startswith("ICT지원사업\n")
          and built["fields"]["purpose"] in built["text"]
          and built["text"].index(built["fields"]["purpose"])
          < built["text"].index(built["fields"]["target_text"]),
          "%d자" % built["char_len"])
    check("1c 규모(금액) 필드는 기본에서 입력에 넣지 않는다",
          "9억원~15억원" not in built["text"],
          "SUPPORT_CONTENT_AND_SCALE 제외 — 모델2 타깃 누수 방지")
    check("1d 근거 block_id 가 보존된다",
          all(built["evidence_block_ids"][f] for f in
              ("purpose", "content", "target_text")),
          str(built["evidence_block_ids"]["purpose"]))

    empty = {"items": [{"field_code": "PURPOSE_GOAL", "status": "MISSING",
                        "occurrences": []}]}
    try:
        IB.build_model1_input(empty)
        check("1e 전부 빈 입력은 막는다", False, "예외가 안 났다")
    except IB.Model1InputError:
        check("1e 전부 빈 입력은 막는다", True, "Model1InputError")

    print("== 2 Result envelope")
    ok = RE.success("A1", "m", "1.0", "t", result={"x": 1})
    bad = RE.failed("A1", "m", "1.0", "t", code="E", message="boom")
    na = RE.not_available("A1", "m", "1.0", "t", depends_on="model_1")
    check("2a success 는 result 를 담는다", ok["result"] == {"x": 1})
    check("2b 실패 상태는 result 를 비운다",
          bad["result"] is None and na["result"] is None)
    check("2c 의존 실패는 failed 가 아니라 not_available",
          na["status"] == RE.NOT_AVAILABLE
          and na["error"]["code"] == "DEPENDENCY_NOT_AVAILABLE")
    s = RE.summarize({"a": ok, "b": bad})
    check("2d 부분 실패를 요약한다", s["partial_failure"] and not s["all_success"],
          str(s["counts"]))

    print("== 3 Model 3 MIN_AXES gate")
    import score as M3
    thin = {"row_id": "THIN", "support_type": "연구개발", "support_method": "grant",
            "support_unit": "project", "amount_type": "per_project",
            "per_recipient": 1.5e9}          # 축 1개(log_per_recipient)뿐
    rep = M3.axis_report([thin])
    check("3a 축 부족 행을 식별한다",
          rep and rep[0]["n_axes"] == 1 and not rep[0]["scorable"],
          "n_axes=%s" % (rep[0]["n_axes"] if rep else "?"))
    check("3b predict() 진입 함수에서 막힌다 (우회 불가)",
          len(M3.predict([thin])) == 0, "채점 행 0")
    check("3c 게이트를 끄면 예전 동작", len(M3.predict([thin], enforce_min_axes=False)) == 1)
    env3 = ORCH.run_model_3("AN-TEST-0001", thin)
    check("3d insufficient_data 로 감싼다",
          env3["status"] == RE.INSUFFICIENT_DATA
          and env3["error"]["code"] == "INSUFFICIENT_NUMERIC_AXES"
          and env3["result"] is None,
          env3["error"]["message"])

    import predict as M2
    print("== 4 Model 2 / Model 3 정상 경로")
    rec = dict(record); rec["support_type"] = "연구개발"
    env2 = ORCH.run_model_2("AN-TEST-0001", rec, text=text, cohort=BASE["cohort"])
    check("4a Model 2 success",
          RE.is_ok(env2)
          and env2["result"]["predicted_per_recipient"]["amount"] > 0
          and env2["result"]["level"] in ("low", "mid", "high"),
          "%s원 · level=%s" % (env2["result"]["predicted_per_recipient"]["amount"],
                              env2["result"]["level"]) if RE.is_ok(env2)
          else str(env2["error"]))
    check("4b 관측 금액과 예측 금액을 분리해 담는다",
          env2["result"]["observed_per_recipient"]["amount"] == 1500000000
          and env2["result"]["predicted_per_recipient"]["amount"]
          != env2["result"]["observed_per_recipient"]["amount"])
    # cohort 는 비교군 키가 아니라 출처다. 엉뚱한 값을 넘기면 어느 사다리 단계에도
    # 걸리지 않아 percentile 이 조용히 null 이 된다 — 실측으로 겪은 뒤 넣은 검사다.
    check("4b2 percentile 이 실제로 계산된다",
          env2["result"]["cohort_percentile"] is not None
          and env2["result"]["reference"]["sample_count"] > 0
          and env2["metadata"]["percentile_unavailable_reason"] is None,
          "%s백분위 · n=%s · %s" % (env2["result"]["cohort_percentile"],
                                  env2["result"]["reference"]["sample_count"],
                                  env2["result"]["reference"]["cohort_level"]))
    bad_cohort = ORCH.run_model_2("AN-TEST-0001", rec, text=text,
                                  cohort="연구개발|grant")
    check("4b3 잘못된 cohort 는 이유를 남긴다",
          bad_cohort["result"]["cohort_percentile"] is None
          and bad_cohort["metadata"]["percentile_unavailable_reason"],
          bad_cohort["metadata"]["percentile_unavailable_reason"])
    # Model 1 은 19종, Model 2 는 23종으로 학습했고 포함관계가 아니다.
    # `상담` 이 Model 1 에만 있어 Model 2 에서 미학습 범주가 된다.
    levels = ORCH.model2_support_type_levels()
    check("4c1 학습 레벨을 artifact 에서 읽는다 (하드코딩 아님)",
          len(levels) == 23 and "연구개발" in levels and "상담" not in levels,
          "%d개" % len(levels))
    check("4c2 known label 은 known 으로 표시",
          env2["metadata"]["support_type_compatibility"] == "known"
          and env2["metadata"]["unseen_support_type"] is None
          and env2["metadata"]["prediction_scope_warning"] is None)

    unseen_rec = dict(rec); unseen_rec["support_type"] = "상담"
    env2u = ORCH.run_model_2("AN-TEST-0001", unseen_rec, text=text,
                             cohort=BASE["cohort"])
    check("4c3 미학습 label 이어도 예측은 그대로 낸다 (status=success)",
          RE.is_ok(env2u)
          and env2u["result"]["predicted_per_recipient"]["amount"] > 0,
          "%s원" % (env2u["result"]["predicted_per_recipient"]["amount"]
                   if RE.is_ok(env2u) else "-"))
    check("4c4 미학습 사실을 metadata 에 드러낸다",
          env2u["metadata"]["support_type_compatibility"] == "unseen_by_model2"
          and env2u["metadata"]["unseen_support_type"] == "상담"
          and env2u["metadata"]["prediction_scope_warning"],
          env2u["metadata"]["prediction_scope_warning"])
    # 미학습이어도 percentile 은 null 이 되지 않는다 — 사다리 마지막 단계
    # (단위x출처)가 support_type 을 키에 안 써서 거기 걸린다. 숫자가 정상으로
    # 보이는데 지원성격을 반영하지 않은 비교라, 그 사실을 남겨야 한다.
    check("4c5 미학습이면 성격 없는 단계로 폴백되고 그 사실을 남긴다",
          env2u["result"]["cohort_percentile"] is not None
          and env2u["metadata"]["percentile_uses_support_type"] is False
          and env2u["metadata"]["percentile_caveat"]
          == "fell_back_to_cohort_without_support_type",
          "%s백분위 · %s" % (env2u["result"]["cohort_percentile"],
                           env2u["metadata"]["percentile_cohort_level"]))
    # caveat 은 unseen 전용이 아니다. 비교군이 얇으면 known label 도 같은 단계로
    # 물러난다 — 이 fixture 의 연구개발|grant|project|taxonomy 가 15건뿐이라
    # 실제로 그렇게 된다. 플래그는 label 이 아니라 **나온 단계**를 따른다.
    check("4c6 얇은 비교군이면 known label 도 같은 caveat 을 받는다",
          env2["metadata"]["percentile_uses_support_type"]
          == str(env2["metadata"]["percentile_cohort_level"]).startswith("성격")
          and (env2["metadata"]["percentile_caveat"] is None)
          == bool(env2["metadata"]["percentile_uses_support_type"]),
          "%s · uses_support_type=%s · caveat=%s"
          % (env2["metadata"]["percentile_cohort_level"],
             env2["metadata"]["percentile_uses_support_type"],
             env2["metadata"]["percentile_caveat"]))

    env3b = ORCH.run_model_3("AN-TEST-0001", rec, adapter_meta=meta)
    check("4c Model 3 success + 비교군 반환",
          RE.is_ok(env3b) and env3b["result"]["reference"]["sample_count"] > 0
          and env3b["result"]["available_axis_count"] >= 2,
          "%s · n=%s · %d축" % (env3b["result"]["reference"]["cohort_level"],
                               env3b["result"]["reference"]["sample_count"],
                               env3b["result"]["available_axis_count"])
          if RE.is_ok(env3b) else str(env3b["error"]))
    # anomaly_level_status 는 result 안에 둔다 — 하류(챗봇)가 result 만 보고도
    # "수준을 말하면 안 된다" 를 알아야 한다.
    check("4d anomaly_level 문턱은 임의로 정하지 않는다",
          env3b["result"]["anomaly_level"] is None
          and env3b["result"]["anomaly_level_status"] == "threshold_undetermined")
    # top1_axis 는 '가장 크게 벗어난 축' 이지 원인이 아니다. result 에 있으면
    # 챗봇이 원인처럼 설명한다 — metadata 로 내려 프롬프트에 실리지 않게 한다.
    check("4e top1_axis 는 result 가 아니라 metadata 에 있다",
          "top1_axis" not in env3b["result"]
          and env3b["metadata"]["top1_axis"],
          "metadata.top1_axis=%s" % env3b["metadata"]["top1_axis"])

    print("== 5 Orchestrator 의존 실패")
    bad_cpl = {"items": [{"field_code": "PURPOSE_GOAL", "status": "MISSING",
                          "occurrences": []}]}
    out = ORCH.run_ml_pipeline("AN-TEST-0002", bad_cpl, structured_data=record)
    check("5a Model 1 실패 시 2·3·SIM-R 은 not_available",
          out["model_1"]["status"] == RE.FAILED
          and all(out[k]["status"] == RE.NOT_AVAILABLE
                  for k in ("model_2", "model_3", "sim_r")),
          str(out["summary"]["counts"]))
    check("5b 실패해도 전체가 죽지 않는다", "summary" in out and out["analysis_id"])

    if "--with-model1" in sys.argv:
        print("== 6 Model 1 실제 추론 + 전체 파이프라인")
        import runner as M1
        env1 = M1.run_model_1("AN-TEST-0001", cpl, title="ICT지원사업")
        check("6a Model 1 success",
              RE.is_ok(env1) and env1["result"]["support_type"],
              "%s conf=%.3f %s" % (env1["result"]["support_type"],
                                   env1["result"]["confidence"],
                                   env1["result"]["trust_grade"])
              if RE.is_ok(env1) else str(env1["error"]))
        check("6b trust_grade 는 dl07 임계값을 그대로 쓴다",
              env1["metadata"]["thresholds"]["hold_below"] == 0.20
              and env1["metadata"]["thresholds"]["trusted_at_or_above"] == 0.35
              and env1["result"]["trust_grade"] in ("trusted", "reference", "hold"))
        full = ORCH.run_ml_pipeline("AN-TEST-0001", cpl, structured_data=record,
                                    title="ICT지원사업", text=text,
                                    cohort=BASE["cohort"], adapter_meta=meta)
        check("6c 전체 파이프라인에서 support_type 이 2·3 으로 전달된다",
              full["model_1"]["result"]["support_type"]
              == full["model_3"]["result"]["reference"]["cohort_key"].split("|")[0]
              if RE.is_ok(full["model_3"]) else False,
              "model1=%s · model3 cohort=%s"
              % (full["model_1"]["result"]["support_type"],
                 full["model_3"]["result"]["reference"]["cohort_key"]
                 if RE.is_ok(full["model_3"]) else "-"))
        check("6d 요약이 상태를 집계한다", bool(full["summary"]["counts"]),
              str(full["summary"]["counts"]))
    else:
        skip("6 Model 1 실제 추론", "--with-model1 로 함께 돌린다 (가중치 442MB)")

    print("== 7 비교군 사다리 확장 (percentile 전용)")
    import pandas as pd
    import cohort as CH
    import preprocessing as PP
    import m45_m2_amount as M45

    key = ["level", "support_type", "support_method", "support_unit", "cohort"]

    def _same(a, b):
        try:
            pd.testing.assert_frame_equal(
                a.sort_values(key).reset_index(drop=True),
                b.sort_values(key).reset_index(drop=True), check_dtype=False)
            return True
        except AssertionError:
            return False

    frame, _src = PP.training_frame()
    ship = CH.load_reference()          # 배포 artifact (내용을 unit_safe 로 교체함)
    legacy = CH.build_reference(frame, "legacy")

    # 배포본을 학습 데이터에서 그대로 다시 구울 수 있어야 한다. 못 구우면
    # 그 표가 어디서 왔는지 아무도 모르게 된다.
    check("7a 배포 참조표를 학습 데이터에서 재현할 수 있다",
          _same(CH.build_reference(frame, CH.DEFAULT_LADDER), ship),
          "%d행 · 사다리 %s" % (len(ship), CH.DEFAULT_LADDER))

    # 파일을 덮어썼으므로 **기존 값이 바뀌지 않았는지**가 핵심이다.
    # 공통 3단계는 legacy 와 한 행도 달라지면 안 된다 — 추가만 있어야 한다.
    shared = ship[ship["level"].isin(legacy["level"].unique())]
    check("7b 교체가 기존 3단계 값을 바꾸지 않았다 (추가만)",
          _same(shared, legacy) and len(ship) > len(legacy),
          "기존 %d행 유지 + 신규 %d행" % (len(legacy), len(ship) - len(legacy)))

    combos = legacy[legacy["level"] == "성격x방식x단위x출처"][
        ["support_type", "support_method", "support_unit", "cohort"]].drop_duplicates()

    def resolve(ref, ladder):
        out = []
        for _i, c in combos.iterrows():
            row, lvl, _w = CH.lookup(ref, c.support_type, c.support_method,
                                     c.support_unit, c.cohort, ladder=ladder)
            out.append((lvl, int(row["n"]) if row is not None else None))
        return out

    old = resolve(legacy, "legacy")
    new = resolve(ship, CH.DEFAULT_LADDER)
    old_rate = sum(CH.uses_support_type(l) for l, _n in old) / len(old)
    new_rate = sum(CH.uses_support_type(l) for l, _n in new) / len(new)
    check("7c support_type 유지율이 오른다",
          new_rate > old_rate, "%.0f%% → %.0f%% (%d개 조합)"
          % (old_rate * 100, new_rate * 100, len(new)))
    # 단위를 섞으면 기업당 금액을 과제당 분포와 견주게 된다. 유지율만 올리고
    # 단위를 버리면 이름만 정확해진다 — 그래서 0 이어야 한다.
    check("7d 단위가 섞인 비교군은 없다",
          all(CH.uses_support_unit(l) for l, _n in new),
          "unit_safe 사다리")
    thin = [n for _l, n in new if n is not None and n < M45.MIN_COHORT]
    check("7e 표본이 더 얇아지지 않는다",
          len(thin) == len([n for _l, n in old
                            if n is not None and n < M45.MIN_COHORT]),
          "MIN_COHORT 미달 %d건 (기존과 동일)" % len(thin))

    # 연구개발 사례 — 기존에는 성격을 버리고 단위x출처로 갔다.
    r_old = M45.compare(legacy, 526390144, "연구개발", "grant", "project", "taxonomy")
    r_new = M2.percentile(526390144, "연구개발", "grant", "project", "taxonomy")
    check("7f 연구개발이 성격을 유지한 단계로 매칭된다",
          r_new["uses_support_type"] and not r_old["level"].startswith("성격"),
          "%s n=%s %.1f → %s n=%s %.1f"
          % (r_old["level"], r_old["n"], r_old["percentile_rank"],
             r_new["level"], r_new["n"], r_new["percentile_rank"]))
    check("7g uses_support_type 이 실제 cohort key 와 일치",
          r_new["uses_support_type"] == r_new["level"].startswith("성격"))

    # unseen 은 참조표에 아예 없으므로 여전히 성격 단계에 못 간다.
    r_unseen = M2.percentile(526390144, "상담", "grant", "project", "taxonomy")
    check("7h unseen 상담 은 여전히 성격 없는 단계",
          not r_unseen["uses_support_type"],
          "%s n=%s" % (r_unseen["level"], r_unseen["n"]))

    # 사다리는 percentile 전용이다. 회귀 예측은 손대지 않았다.
    check("7i 예측 금액과 level 은 변하지 않는다",
          env2["result"]["predicted_per_recipient"]["amount"] == 526390144
          and env2["result"]["level"] == "high",
          "%s원 · %s" % (env2["result"]["predicted_per_recipient"]["amount"],
                        env2["result"]["level"]))

    print()
    if _fail:
        print("실패 %d건: %s" % (len(_fail), _fail))
        return 1
    print("ML PIPELINE 회귀 검사 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
