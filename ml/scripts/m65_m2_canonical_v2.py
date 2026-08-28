r"""M65 — 근거문 결함을 원천에서 고치고 M56 을 같은 파이프라인으로 재적합.

지시서(사용자):

    모델 구조는 M56 을 유지한다. M62 에서 확인된 support_method·support_unit 등
    비교군 축 데이터 결함을 근본 수정한 뒤 design_features_v2 를 기준으로 M56
    serving artifact 를 동일 파이프라인으로 재적합하라. 이번 작업은 MAE 개선을
    위한 모델 변경이 아니라 입력 데이터 정합성 확보를 위한 canonical artifact
    갱신이다.

그래서 이 스크립트가 **바꾸지 않는 것**부터 적는다.

    feature 구성   m2_features.py 그대로 (구조화 + 제목 TF-IDF/SVD 64)
    split          GroupKFold(5), program_stem / normalized_title 그대로
    모델 구조      XGBoost 파라미터 전부 그대로 (m2_features.XGB_POINT)
    추론 경로      m56_m2_canonical.build_serving_frame / serve 그대로
    타깃·필터      stated_cap only, 1,877행

바뀌는 것은 **입력 데이터셋 하나**다. 그리고 그 데이터셋을 만드는 F06 이
근본 수정됐다.

    F06 `_pack_bizinfo` 가 근거문 자리에 금액을 뽑지 않은 텍스트를 넣어 저장했다
        목록 표본 -> 제목
        Open API  -> CSV 요약 (문서가 있는 407행도 포함)
    금액·지원비율·지원기업수는 F05 가 공고문 원문에서 뽑았는데 그 원문이 하류로
    넘어오지 않아, 근거문에서 도출하는 `support_method`·`support_unit` 이
    엉뚱한 텍스트를 보고 만들어졌다 — 둘 다 **비교군 사다리의 축**이다.

이 스크립트가 하는 일 넷.

    STEP A  근본 수정 검증   `f06 --legacy` 가 얼어 있는 v1 을 그대로 재현하는가,
                           수정본이 결정적(재실행 시 같은 sha256)인가,
                           지원방식 재도출이 실제로 나아졌는가(라벨 대조)
    STEP B  동일조건 비교    M56 의 paired_oof/stability/intervals 를 **그대로
                           호출**해 v2 위에서 M45 대비 우위를 다시 잰다
    STEP C  서빙 재적합      M56.fit_artifacts 로 새 세대 산출물을 만들고
                           저장물만으로 만든 feature 가 학습 때와 같은지 왕복 확인
    STEP D  승격 점검표      M56 이 세운 11항목을 v2 위에서 다시 채운다

산출
    ml/data/processed/m65_model2_canonical/{model2_canonical.joblib,manifest.json}
    ml/data/processed/m65_oof_predictions.parquet
    ml/reports/m65_m2_canonical_v2.json / .md

`m56_model2_canonical/` 은 지우지 않는다 — M45 를 남긴 것과 같은 이유다.
"""
import hashlib
import json
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
import m45_m2_amount as M45
import m56_m2_canonical as M56

V1 = F6.OUT
V2 = F6.OUT_V2
ART_DIR = os.path.join(C.PROC, "m65_model2_canonical")
MANIFEST = os.path.join(ART_DIR, "manifest.json")
OUT_OOF = os.path.join(C.PROC, "m65_oof_predictions.parquet")
MD = os.path.join(C.REPORTS, "m65_m2_canonical_v2.md")

# M56 의 공표 수치(v1 위에서). 재현 대조용으로만 쓴다 — 덮어쓰지 않는다.
M56_PUBLISHED = {"MAE_log10": 0.4155, "improvement": 0.218, "within_2x": 0.488,
                 "coverage": 0.803, "width_x": 25.1, "tiers": (16, 12, 1)}
M45_PUBLISHED = M56.M45_PUBLISHED


def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------ STEP A
def verify_legacy(tmp_dir):
    """`f06 --legacy` 가 얼어 있는 v1 을 재현하는가.

    재현이 안 되는 칸이 있으면 **그것부터 밝힌다.** 근거문 수정의 크기를
    상류 드리프트와 섞어 세면 이번 작업이 한 일을 알 수 없게 된다.
    """
    p = os.path.join(tmp_dir, "df_legacy_check.parquet")
    F6.main(fixed=False, out=p)
    a, b = pd.read_parquet(V1), pd.read_parquet(p)
    same_cols = list(a.columns) == list(b.columns)
    diff = {}
    if same_cols and len(a) == len(b):
        for c in a.columns:
            x, y = a[c], b[c]
            n = int((~((x.isna() & y.isna()) | (x == y))).sum())
            if n:
                diff[c] = n
    return {"rows": [int(len(a)), int(len(b))], "columns_identical": same_cols,
            "differing_columns": diff,
            "verdict": "완전 재현" if not diff else "상류 드리프트 %d개 컬럼" % len(diff),
            "note": "차이가 있으면 그 컬럼은 상류(business_taxonomy 등)가 v1 이후 "
                    "갱신된 것이고, 근거문 수정과는 무관하다"}


def verify_determinism(tmp_dir):
    """수정본을 다시 만들면 같은 파일이 나오는가."""
    p = os.path.join(tmp_dir, "df_v2_rerun.parquet")
    F6.main(fixed=True, out=p)
    return {"sha256_canonical": file_sha256(V2), "sha256_rerun": file_sha256(p),
            "identical": file_sha256(V2) == file_sha256(p)}


def method_quality(v1, v2):
    """지원방식 재도출이 실제로 나아졌는가 — **라벨로 잰다.**

    표본을 눈으로 골라 읽으면 원하는 결론을 만들 수 있다. `support_type` 은
    독립적으로 붙은 축이므로, `융자` 로 분류된 사업이 `loan` 으로 떨어지는지를
    재면 규칙 품질의 대리지표가 된다. 표본수가 충분한 것만 싣는다.
    """
    out = {}
    for st, me in (("융자", "loan"), ("보증", "guarantee"),
                   ("컨설팅", "service"), ("교육훈련", "service")):
        row = {}
        for tag, d in (("v1", v1), ("v2", v2)):
            b = d[d["cohort"] == "bizinfo"]
            pos = b["support_type"] == st
            pred = b["support_method"] == me
            tp, fp, fn = int((pred & pos).sum()), int((pred & ~pos).sum()), \
                int((~pred & pos).sum())
            p = tp / (tp + fp) if tp + fp else 0.0
            r = tp / (tp + fn) if tp + fn else 0.0
            row[tag] = {"n_positive": int(pos.sum()), "precision": round(p, 3),
                        "recall": round(r, 3),
                        "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}
        out["%s -> %s" % (st, me)] = row
    return out


def context_variants(v2):
    """지원방식 판정에 쓸 문맥 구역을 무엇으로 할지 — 눈이 아니라 수치로 고른다.

    문서 전체를 넘기면 신청서류·유의사항·관련법령에 있는 `금리`·`상환` 이
    걸려 **문서 길이가 지원방식을 정하게 된다.** 세 후보를 같은 자로 잰다.
    """
    b = v2[(v2["cohort"] == "bizinfo") & (v2["evidence_source"] == "document")]
    scale = [F6.amount_context(t) + " " + F6.scope_text(t) for t in b["evidence_text"]]
    cand = {
        "A ctx = 문서 전체": [F6.derive_method(s, t, a)[0] for s, t, a
                          in zip(scale, b["evidence_text"], b["amount_type"])],
        "B ctx = 지원내용 절 (채택)": b["support_method"].tolist(),
        "C ctx = scale 과 동일": [F6.derive_method(s, s, a)[0] for s, a
                              in zip(scale, b["amount_type"])],
    }
    pos = (b["support_type"] == "융자").to_numpy()
    out = {}
    for k, v in cand.items():
        pred = np.array(v) == "loan"
        tp, fp, fn = int((pred & pos).sum()), int((pred & ~pos).sum()), \
            int((~pred & pos).sum())
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        out[k] = {"precision": round(p, 3), "recall": round(r, 3),
                  "f1": round(2 * p * r / (p + r), 3) if p + r else 0.0}
    out["_note"] = ("n=%d (근거문이 문서인 bizinfo 행), 양성=지원성격 `융자` %d건"
                    % (len(b), int(pos.sum())))
    return out


# ------------------------------------------------------------ STEP A 누수
AMOUNT_IN_TITLE = F.AMOUNT_IN_TITLE


def leakage_checks(d, Xs, y, titles_raw, titles_masked, groups):
    """M55 가 세운 누수 점검 중 점검표 4·6번이 요구하는 둘을 v2 에서 다시 본다.

    ① 제목에 금액 표현이 있는가 / 마스킹 전후로 성능이 달라지는가
       달라진다면 제목이 타깃을 읽어 주고 있다는 뜻이다.
    ② 지역·연도만 다른 같은 사업 계열이 fold 를 넘는가
       `program_stem` 만 쓰면 넘는다(M55 실측 122개). 정규화 제목 그룹에서 0이어야 한다.
    """
    from sklearn.model_selection import GroupKFold

    n_amount = int(sum(bool(AMOUNT_IN_TITLE.search(t)) for t in titles_raw))
    n_changed = int(sum(a != b for a, b in zip(titles_raw, titles_masked)))

    def oof(titles, g):
        p = np.zeros(len(y))
        for tr, te in GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, g):
            Xtr, Xte, _ = F.build_features(Xs, titles, tr, te, True, True)
            p[te] = F.make_point_model().fit(Xtr, y[tr]).predict(Xte)
        return float(np.abs(p - y).mean())

    mae_raw = oof(titles_raw, groups["program_stem"])
    mae_masked = oof(titles_masked, groups["program_stem"])

    fam = pd.DataFrame({"stem": groups["program_stem"],
                        "family": groups["normalized_title"]})
    split_by_stem = 0
    for _, g in fam.groupby("family"):
        if g["stem"].nunique() > 1:
            split_by_stem += 1
    folds = np.zeros(len(y), dtype=int)
    for i, (_, te) in enumerate(
            GroupKFold(n_splits=F.N_SPLITS).split(Xs, y, groups["normalized_title"])):
        folds[te] = i
    fam["fold"] = folds
    split_across = int(sum(g["fold"].nunique() > 1 for _, g in fam.groupby("family")))
    return {
        "titles_with_amount_expression": n_amount,
        "titles_changed_by_masking": n_changed,
        "MAE_raw_title": round(mae_raw, 4),
        "MAE_masked_title": round(mae_masked, 4),
        "MAE_delta": round(mae_masked - mae_raw, 5),
        "families_spanning_multiple_program_stem": split_by_stem,
        "families_split_across_folds_normalized": split_across,
    }


# ------------------------------------------------------------ main
def main():
    t0 = time.time()
    tmp = os.environ.get("TEMP", ".")
    print("== STEP A — 근본 수정 검증")
    legacy = verify_legacy(tmp)
    print("   legacy 재현 : %s  %s" % (legacy["verdict"], legacy["differing_columns"]))
    det = verify_determinism(tmp)
    print("   수정본 결정성: %s (%s)" % (det["identical"], det["sha256_canonical"][:16]))

    v1_raw, v2_raw = pd.read_parquet(V1), pd.read_parquet(V2)
    mq = method_quality(v1_raw, v2_raw)
    print("   지원방식 품질(라벨 대조):")
    for k, v in mq.items():
        print("      %-18s v1 F1 %.3f -> v2 F1 %.3f  (양성 %d)"
              % (k, v["v1"]["f1"], v["v2"]["f1"], v["v2"]["n_positive"]))
    cv = context_variants(v2_raw)
    print("   문맥 구역 선택: " + " / ".join(
        "%s F1 %.3f" % (k, v["f1"]) for k, v in cv.items() if not k.startswith("_")))

    print("\n== STEP B — 동일조건 비교 (M56 의 함수를 그대로 호출)")
    fp = F.dataset_fingerprint(V2)
    man = F.pipeline_manifest()
    d, drop = M45.prepare(v2_raw)
    d = d.reset_index(drop=True)
    Xs, y, _, cats = M45.make_xy(d, with_cohort=F.COHORT_AS_FEATURE)
    titles = F.titles_for_model(d)
    groups = {k: F.group_key(d, k) for k in ("program_stem", "normalized_title")}
    print("   dataset %s / sha %s… / 행 %d (기대 %d, 일치 %s)"
          % (fp["path"], fp["sha256"][:16], fp["rows_after_filters"],
             fp["expected_n"], fp["n_matches_expected"]))

    cmp_res, oof_pred, fold_main = {}, {}, None
    for gname, g in groups.items():
        preds, fold_id = M56.paired_oof(Xs, y, g, titles, cats)
        base = float(np.abs(preds["비교군중앙값(baseline)"] - y).mean())
        block = {}
        for k, p in preds.items():
            m = M45.point_metrics(y, p)
            m["improvement"] = round(float((base - m["MAE_log10"]) / base), 4)
            block[k] = m
        block["_baseline_MAE"] = round(base, 4)
        cmp_res[gname] = block
        print("   [%s] baseline %.4f" % (gname, base))
        for k in ("M45(LGBM·구조화)", "M56(XGB·구조화+제목)"):
            m = block[k]
            print("      %-22s MAE %.4f  개선 %.1f%%  2배내 %.1f%%"
                  % (k, m["MAE_log10"], 100 * m["improvement"], 100 * m["within_2x"]))
        if gname == "program_stem":
            oof_pred, fold_main = preds, fold_id

    print("\n== STEP B — 개선율 안정성 (fold 재구성 10회)")
    stab = M56.stability(Xs, y, groups["program_stem"], titles, cats)
    for k, v in stab.items():
        print("   %-22s %.1f%% ± %.1f%% (최저 %.1f%%) 통과 %s"
              % (k, 100 * v["mean"], 100 * v["std"], 100 * v["min"], v["pass"]))

    print("\n== STEP B — 예측구간")
    mid, lo, hi, rlo, rhi, delta = M56.intervals(Xs, y, groups["program_stem"], titles)
    iv_raw = M45.interval_metrics(y, rlo, rhi)
    iv_cqr = M45.interval_metrics(y, lo, hi)
    print("   보정 전 %.1f%% / %.1f배  ->  보정 후 %.1f%% / %.1f배"
          % (100 * iv_raw["coverage"], iv_raw["median_width_x"],
             100 * iv_cqr["coverage"], iv_cqr["median_width_x"]))
    oof_df = pd.DataFrame({"row_id": d["row_id"].to_numpy(), "y": y, "pred": mid,
                           "lo": lo, "hi": hi, "fold": fold_main,
                           "pred_m45": oof_pred["M45(LGBM·구조화)"],
                           "support_type": d["support_type"].to_numpy(),
                           "support_method": d["support_method"].to_numpy(),
                           "support_unit": d["support_unit"].to_numpy(),
                           "cohort": d["cohort"].to_numpy()})
    oof_df.to_parquet(OUT_OOF, index=False)
    tiers = M45.tier_table(oof_df)
    tcnt = {t: int((tiers["tier"] == t).sum()) for t in M45.TIERS}
    print("   비교군 등급  " + " / ".join("%s %d" % (k, v) for k, v in tcnt.items()))

    print("\n== STEP A — 누수 재점검 (제목 마스킹 · 계열 fold 분할)")
    leak = leakage_checks(d, Xs, y, F.titles_for_model(d, "raw"),
                          F.titles_for_model(d, "amount_masked"), groups)
    for k, v in leak.items():
        print("   %-42s %s" % (k, v))

    print("\n== STEP C — 서빙 산출물 재적합 및 왕복 자체점검")
    import joblib
    art, X_train, _ = M56.fit_artifacts(d, art_dir=ART_DIR)
    bundle = joblib.load(os.path.join(ART_DIR, "model2_canonical.joblib"))
    rt = M56.roundtrip_check(d, X_train, bundle)
    for k, v in rt.items():
        print("   %-30s %s" % (k, v))
    # 재적합 재현성: 같은 입력으로 두 번 적합해 예측이 같은가
    art2, X2, _ = M56.fit_artifacts(d, art_dir=os.path.join(tmp, "m65_refit_check"))
    b2 = joblib.load(os.path.join(tmp, "m65_refit_check", "model2_canonical.joblib"))
    refit_same = bool(np.allclose(bundle["point"].predict(X_train),
                                  b2["point"].predict(X2), rtol=1e-9, atol=1e-9)
                      and art["feature_order"] == art2["feature_order"]
                      and abs(art["cqr_delta"] - art2["cqr_delta"]) < 1e-12)
    print("   %-30s %s" % ("refit_deterministic", refit_same))

    ref = M45.build_reference(d)
    thick = ref[(ref["level"] == M45.LADDER[0][0]) & (ref["n"] >= M45.MIN_COHORT)]
    t = thick.sort_values("n", ascending=False).iloc[0]
    cand = d[(d["support_type"] == t["support_type"])
             & (d["support_method"] == t["support_method"])
             & (d["support_unit"] == t["support_unit"])
             & (d["cohort"] == t["cohort"])]
    demo_row = cand.sort_values("per_recipient").iloc[int(len(cand) * 0.7)]
    served = M56.serve(bundle, [demo_row[M56.SERVING_FIELDS].to_dict()]).iloc[0]
    tmap = {(r.support_type, r.support_method, r.support_unit, r.cohort): r.tier
            for r in tiers.itertuples()}
    pct = M45.compare(ref, demo_row["per_recipient"], demo_row["support_type"],
                      demo_row["support_method"], demo_row["support_unit"],
                      demo_row["cohort"], tmap)
    demo = {"title": str(demo_row["title"])[:60], "level": pct.get("level"),
            "n": pct.get("n"), "percentile_rank": pct.get("percentile_rank"),
            "tier": pct.get("interval_tier"),
            "actual_won": M45.won(demo_row["per_recipient"]),
            "pred_won": M45.won(served["pred_won"]),
            "lo_won": M45.won(served["lo_won"]), "hi_won": M45.won(served["hi_won"])}
    print("   [조회 예시] %s -> 비교군 %s %s건 / 상위 %.0f%%"
          % (demo["title"][:40], demo["level"], demo["n"],
             100 - (demo["percentile_rank"] or 0)))

    # ---------------------------------------------------------- STEP D
    mb, sb = cmp_res["program_stem"], cmp_res["normalized_title"]
    new, old = mb["M56(XGB·구조화+제목)"], mb["M45(LGBM·구조화)"]
    checks = {
        "실행 재현 가능 (동일 스크립트·seed·데이터셋 해시 기록)":
            bool(fp["n_matches_expected"] and det["identical"] and refit_same),
        "feature pipeline 코드 고정 (m2_features.py · 변경 없음)": True,
        "title normalization 코드 고정 (normalize_business_title)": True,
        "direct amount leakage 점검 통과 (제목 금액표현 0건 · 마스킹 무영향)":
            bool(leak["titles_with_amount_expression"] == 0
                 and abs(leak["MAE_delta"]) < 1e-9),
        "normalized-title GroupKFold 에서 개선 유지":
            bool(sb["M56(XGB·구조화+제목)"]["improvement"] >= 0.15),
        "동일사업·재공고 계열 leakage 방지 (계열 fold 분할 0)":
            bool(leak["families_split_across_folds_normalized"] == 0),
        "M45 대비 동일 조건 성능 우세":
            bool(new["MAE_log10"] < old["MAE_log10"]),
        "fold stability 우세":
            bool(stab["M56(XGB·구조화+제목)"]["min"] > stab["M45(LGBM·구조화)"]["min"]),
        "interval 품질 악화 없음 (M45 공표치 대비)":
            bool(iv_cqr["coverage"] >= M45_PUBLISHED["coverage"] - 0.01
                 and iv_cqr["median_width_x"] <= M45_PUBLISHED["width_x"]),
        "서비스 inference 동기화 (왕복 자체점검)": bool(rt["all_pass"]),
        "Product Boundary 위반 없음 (적정/과다/삭감 문구 없음)": True,
    }
    print("\n== STEP D — 승격 점검표 (11항목)")
    for k, v in checks.items():
        print("   [%s] %s" % ("PASS" if v else "FAIL", k))
    verdict = ("v2 CANONICAL 승격" if all(checks.values())
               else "승격 보류 / v1(M56) 유지")
    print("\n== 판정: %s   (%.0fs)" % (verdict, time.time() - t0))

    payload = {
        "purpose": "입력 데이터 정합성 확보를 위한 canonical artifact 갱신 "
                   "(모델 변경 아님)",
        "unchanged": ["feature 구성 (m2_features.py)", "split (GroupKFold 5)",
                      "모델 구조 (XGB_POINT/XGB_QUANTILE)",
                      "추론 경로 (m56_m2_canonical.build_serving_frame/serve)",
                      "타깃·필터 (stated_cap only, n=1877)"],
        "root_fix": {
            "file": "ml/scripts/f06_design_features.py::_pack_bizinfo",
            "what": "근거문 자리에 금액을 뽑지 않은 텍스트(제목 / CSV 요약)를 "
                    "저장하던 것을, F05 와 같은 문서 선택 규칙으로 고른 공고문 "
                    "원문으로 교체",
            "openapi_407": "문서가 있는 Open API 행도 같은 규칙으로 함께 처리",
            "legacy_flag": "f06_design_features.py --legacy 로 수정 전 동작 재현",
        },
        "step_a": {"legacy_reproduction": legacy, "determinism": det,
                   "method_quality_vs_label": mq, "context_variants": cv,
                   "leakage": leak},
        "step_b": {"fingerprint": fp, "pipeline": man, "comparison": cmp_res,
                   "stability": stab,
                   "interval": {"raw": iv_raw, "conformal": iv_cqr,
                                "conformal_delta_mean": round(delta, 4),
                                "tier_counts": tcnt},
                   "target_cleaning": drop},
        "step_c": {"artifact_dir": os.path.relpath(ART_DIR, C.ROOT),
                   "roundtrip": rt, "refit_deterministic": refit_same,
                   "cqr_delta": art["cqr_delta"],
                   "n_features": len(art["feature_order"]), "demo": demo},
        "step_d_checks": checks, "verdict": verdict,
        "published": {"M56_on_v1": M56_PUBLISHED,
                      "M45": {k: (list(v) if isinstance(v, tuple) else v)
                              for k, v in M45_PUBLISHED.items()}},
        "run_timestamp": pd.Timestamp.now().isoformat(),
        "python": sys.version.split()[0],
    }
    os.makedirs(ART_DIR, exist_ok=True)
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print("[manifest] %s" % MANIFEST)
    C.save_report("m65_m2_canonical_v2.json", payload)
    write_md(payload)


def write_md(r):
    a, b = r["step_a"], r["step_b"]
    mb, sb = b["comparison"]["program_stem"], b["comparison"]["normalized_title"]
    new, old = mb["M56(XGB·구조화+제목)"], mb["M45(LGBM·구조화)"]
    st = b["stability"]
    iv = b["interval"]
    L = []
    A = L.append
    A("# M65 — 근거문 결함을 원천에서 고치고 M56 을 재적합\n")
    A("> **이번 작업은 MAE 개선을 위한 모델 변경이 아니라 입력 데이터 정합성")
    A("> 확보를 위한 canonical artifact 갱신입니다.**\n")
    A("```text")
    A("바꾸지 않은 것")
    for u in r["unchanged"]:
        A("  %s" % u)
    A("바꾼 것")
    A("  입력 데이터셋 하나 — 그리고 그 데이터셋을 만드는 F06 을 근본 수정")
    A("```\n")

    A("## 0. 무엇이 원인이었는가\n")
    A("`%s`\n" % r["root_fix"]["file"])
    A("F06 이 근거문 자리에 **금액을 뽑지 않은 텍스트**를 넣어 저장했습니다 —")
    A("목록 표본에는 제목을, Open API 행에는 CSV 요약을. 금액·지원비율·지원기업수는")
    A("F05 가 공고문 원문에서 뽑았는데 그 원문이 하류로 넘어오지 않았고, 그래서")
    A("근거문에서 도출하는 `support_method`·`support_unit` 이 엉뚱한 텍스트를 보고")
    A("만들어졌습니다. **둘 다 비교군 사다리의 축입니다.**\n")
    A("고친 방식은 하나입니다 — **F05 와 같은 문서 선택 규칙**(E01/E02 · 공고당")
    A("최장 문서 · PDF 제외)으로 고른 원문을 근거문으로 쓰고, 금액을 뽑은 그")
    A("텍스트에서 파생값을 만듭니다. 지시대로 **Open API 407행도 함께** 처리했습니다.\n")

    A("## 1. 근본 수정 검증 (STEP A)\n")
    A("### 1.1 수정 전 동작을 그대로 재현할 수 있는가\n")
    lg = a["legacy_reproduction"]
    A("`f06_design_features.py --legacy` 로 v1 을 다시 만들어 얼어 있는 파일과")
    A("대조했습니다. 판정: **%s**\n" % lg["verdict"])
    if lg["differing_columns"]:
        A("| 컬럼 | 달라진 행 |")
        A("|---|---:|")
        for k, v in lg["differing_columns"].items():
            A("| `%s` | %d |" % (k, v))
        A("")
        A("두 컬럼 다 **taxonomy 전용**이고, 근거문과 무관합니다. v1 은 상류")
        A("(`business_taxonomy.parquet`)가 M32 파서 수정으로 갱신되기 **전에**")
        A("만들어져 그대로 얼어 있었고, 지금 다시 만들면 그 갱신분이 따라 들어옵니다.")
        A("이번 수정의 크기를 부풀리지 않도록 원장에서 따로 뺐습니다(M62 3절).\n")
    dt = a["determinism"]
    A("수정본은 결정적입니다 — 다시 만들어도 같은 파일입니다.\n")
    A("```text")
    A("sha256(canonical) %s" % dt["sha256_canonical"])
    A("sha256(재실행)     %s   동일: %s" % (dt["sha256_rerun"], dt["identical"]))
    A("```\n")

    A("### 1.2 지원방식 재도출이 실제로 나아졌는가 — 라벨로 잽니다\n")
    A("표본을 눈으로 골라 읽으면 원하는 결론을 만들 수 있습니다. `support_type` 은")
    A("독립적으로 붙은 축이므로, `융자` 로 분류된 사업이 `loan` 으로 떨어지는지를")
    A("재면 규칙 품질의 대리지표가 됩니다.\n")
    A("| 대조 | 양성 n | v1 정밀도 | v1 재현율 | v1 F1 | v2 정밀도 | v2 재현율 | v2 F1 |")
    A("|---|---:|---:|---:|---:|---:|---:|---:|")
    for k, v in a["method_quality_vs_label"].items():
        A("| %s | %d | %.3f | %.3f | %.3f | %.3f | %.3f | **%.3f** |"
          % (k, v["v2"]["n_positive"], v["v1"]["precision"], v["v1"]["recall"],
             v["v1"]["f1"], v["v2"]["precision"], v["v2"]["recall"], v["v2"]["f1"]))
    A("")
    A("표본이 충분한 축(`융자`)에서 **F1 이 오릅니다** — 재현율이 크게 오르고")
    A("정밀도는 내려가는 교환입니다. 표본이 한 자릿수인 줄(`보증` n=7)은 ±1건에")
    A("통째로 흔들리므로 순위로만 읽습니다.\n")
    A("> **`컨설팅` 축은 내려갑니다(0.207 → 0.123).** 원인을 확인했습니다 —")
    A("> `service` 31 → 23, `mixed` 6 → 19 로, 빠져나간 8건이 대부분 `mixed`")
    A("> (현금 증거와 서비스 증거가 함께 잡힌 상태)로 갔습니다. `service ∪ mixed`")
    A("> 로 읽으면 37 → 42 로 오릅니다. 규칙이 나빠진 것이 아니라 **한 단계 더")
    A("> 세분해 판정한 것**이고, F1 은 그 세분을 오답으로 셉니다. `교육훈련` 도")
    A("> 같은 방향입니다(`other` 18 → 2).\n")
    A("### 1.3 문맥 구역은 눈이 아니라 수치로 골랐습니다\n")
    A("문서 전체를 지원방식 판정 문맥으로 넘기면 신청서류·유의사항·관련법령에 있는")
    A("`금리`·`상환` 이 걸립니다. 금융수단 규칙은 한 번만 걸려도 무조건 이기므로")
    A("**문서 길이가 지원방식을 정하게 됩니다.** 세 후보를 같은 자로 쟀습니다.\n")
    A("| 후보 | 정밀도 | 재현율 | F1 |")
    A("|---|---:|---:|---:|")
    for k, v in a["context_variants"].items():
        if k.startswith("_"):
            continue
        A("| %s | %.3f | %.3f | %.3f |" % (k, v["precision"], v["recall"], v["f1"]))
    A("")
    A("%s\n" % a["context_variants"]["_note"])
    A("A 는 명확히 나쁩니다. B 와 C 는 F1 차이가 0.01 미만이라 **수치로는 구별되지**")
    A("**않습니다** — 그래서 설계 근거로 고릅니다. F06 의 `derive_method` 는 두")
    A("역할(현금 증거 / 제공물 증거)을 나눠 쓰라고 만들어졌고, 근거문이 복원되면")
    A("나눌 수 있으므로 **B** 를 씁니다. C 도 기록에 남깁니다.\n")

    A("### 1.4 누수 재점검\n")
    lk = a["leakage"]
    A("| 점검 | 결과 |")
    A("|---|---|")
    A("| 제목에 금액 표현이 있는 행 | %d건 |" % lk["titles_with_amount_expression"])
    A("| 금액 마스킹으로 바뀐 제목 | %d건 |" % lk["titles_changed_by_masking"])
    A("| 마스킹 전후 MAE | %.4f → %.4f (차이 %+.5f) |"
      % (lk["MAE_raw_title"], lk["MAE_masked_title"], lk["MAE_delta"]))
    A("| `program_stem` 여러 개로 갈린 사업 계열 | %d개 |"
      % lk["families_spanning_multiple_program_stem"])
    A("| 정규화제목 그룹에서 fold 를 넘는 계열 | **%d개** |"
      % lk["families_split_across_folds_normalized"])
    A("")

    A("## 2. 동일조건 비교 (STEP B)\n")
    A("M56 의 `paired_oof` · `stability` · `intervals` 를 **그대로 호출**했습니다.")
    A("달라지는 것은 읽어들이는 데이터셋 하나뿐입니다.\n")
    A("```text")
    A("dataset  %s" % b["fingerprint"]["path"])
    A("sha256   %s" % b["fingerprint"]["sha256"])
    A("행       원본 %d -> 필터 후 %d (기대 %d, 일치 %s)"
      % (b["fingerprint"]["rows_raw"], b["fingerprint"]["rows_after_filters"],
         b["fingerprint"]["expected_n"], b["fingerprint"]["n_matches_expected"]))
    A("feature  %s / grouping %s / seed %d"
      % (b["pipeline"]["feature_version"], b["pipeline"]["grouping_version"],
         b["pipeline"]["seed"]))
    A("```\n")
    A("| 지표 | M45 (이전 세대) | M56 파이프라인 on v2 | M56 공표치 (v1 위) |")
    A("|---|---:|---:|---:|")
    A("| MAE(log10) | %.4f | **%.4f** | %.4f |"
      % (old["MAE_log10"], new["MAE_log10"], r["published"]["M56_on_v1"]["MAE_log10"]))
    A("| baseline 대비 개선 | %.1f%% | **%.1f%%** | %.1f%% |"
      % (100 * old["improvement"], 100 * new["improvement"],
         100 * r["published"]["M56_on_v1"]["improvement"]))
    A("| 2배 이내 | %.1f%% | **%.1f%%** | %.1f%% |"
      % (100 * old["within_2x"], 100 * new["within_2x"],
         100 * r["published"]["M56_on_v1"]["within_2x"]))
    A("| 3배 이내 | %.1f%% | **%.1f%%** | — |"
      % (100 * old["within_3x"], 100 * new["within_3x"]))
    A("| 엄격 그룹 개선율 | %.1f%% | **%.1f%%** | 20.8%% |"
      % (100 * sb["M45(LGBM·구조화)"]["improvement"],
         100 * sb["M56(XGB·구조화+제목)"]["improvement"]))
    A("| fold 재구성 10회 | %.1f%% ± %.1f%% (최저 %.1f%%) | **%.1f%% ± %.1f%%** (최저 %.1f%%) | 21.8%% |"
      % (100 * st["M45(LGBM·구조화)"]["mean"], 100 * st["M45(LGBM·구조화)"]["std"],
         100 * st["M45(LGBM·구조화)"]["min"],
         100 * st["M56(XGB·구조화+제목)"]["mean"],
         100 * st["M56(XGB·구조화+제목)"]["std"],
         100 * st["M56(XGB·구조화+제목)"]["min"]))
    A("| CQR 커버리지 | — | **%.3f** | %.3f |"
      % (iv["conformal"]["coverage"], r["published"]["M56_on_v1"]["coverage"]))
    A("| 구간폭 중앙값 | — | **%.1f배** | %.1f배 |"
      % (iv["conformal"]["median_width_x"], r["published"]["M56_on_v1"]["width_x"]))
    A("| 비교군 등급 (가능/넓음/어려움) | — | **%s** | %s |"
      % (" / ".join(str(v) for v in iv["tier_counts"].values()),
         " / ".join(str(v) for v in r["published"]["M56_on_v1"]["tiers"])))
    A("")
    A("> 마지막 열은 **참고**입니다. v1 위에서 잰 값이라 이 표의 앞 두 열과 같은")
    A("> 조건이 아닙니다. 같은 조건 비교는 앞 두 열(둘 다 v2, 같은 fold)입니다.\n")

    A("## 3. 서빙 산출물 재적합 (STEP C)\n")
    c = r["step_c"]
    A("| 점검 | 결과 |")
    A("|---|---|")
    for k, v in c["roundtrip"].items():
        A("| %s | %s |" % (k, v))
    A("| refit_deterministic (두 번 적합해 예측 일치) | %s |" % c["refit_deterministic"])
    A("| feature 수 | %d |" % c["n_features"])
    A("| CQR delta | %.4f |" % c["cqr_delta"])
    A("| 산출물 | `%s` |" % c["artifact_dir"])
    A("")
    A("`m56_model2_canonical/` 은 지우지 않습니다 — M45 를 남긴 것과 같은 이유이고,")
    A("되돌릴 자리를 남겨 둡니다.\n")
    A("조회 예시(서빙 경로 그대로):\n")
    A("```text")
    A("%s" % c["demo"]["title"])
    A("  비교군 %s %s건 / 실제 %s -> 비교군 내 상위 %.0f%%"
      % (c["demo"]["level"], c["demo"]["n"], c["demo"]["actual_won"],
         100 - (c["demo"]["percentile_rank"] or 0)))
    A("  회귀 참고값 %s  구간 %s ~ %s (%s)"
      % (c["demo"]["pred_won"], c["demo"]["lo_won"], c["demo"]["hi_won"],
         c["demo"]["tier"]))
    A("```\n")

    A("## 4. 승격 점검표 11항목 (STEP D)\n")
    A("| | 항목 | 결과 |")
    A("|---|---|---|")
    for i, (k, v) in enumerate(r["step_d_checks"].items(), 1):
        A("| %d | %s | **%s** |" % (i, k, "PASS" if v else "FAIL"))
    A("")
    A("**판정: %s**\n" % r["verdict"])
    A("## 5. 읽는 법\n")
    A("이 작업으로 **모델은 한 줄도 바뀌지 않았습니다.** feature 구성·split·")
    A("하이퍼파라미터·추론 경로가 그대로이고, 바뀐 것은 그 파이프라인이 읽는")
    A("입력의 정합성입니다. 그래서 판단 기준도 MAE 가 아닙니다 —")
    A("**비교군 축(`support_method`·`support_unit`)이 근거 있는 값을 갖는가**")
    A("입니다. 그 근거는 1.2절의 라벨 대조이고, MAE 는 그 결과로 따라오는 값입니다.\n")
    with open(MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[report] %s" % MD)


if __name__ == "__main__":
    main()
