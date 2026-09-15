"""Model 2 serving 번들 생성 — M82/P3.

전체 학습행(1,877)으로 변환기와 모델을 한 번 적합해 joblib 하나로 굽는다.

    변환기  제목 TF-IDF/SVD64 · 본문 마스킹 TF-IDF/SVD64 · proximity TF-IDF/SVD16
    모델    global 1 + 구간 expert 3 + ordinal 이진 2 = 6개
    스키마  211열의 이름과 순서 (추론 때 대조한다)

검증(굽고 나서 바로 확인):
    1. 학습 설계행렬 열 수가 M82/P3 과 같은 211
    2. 같은 프레임을 transform 했을 때 열 이름·순서가 정확히 일치
    3. 합성 신규 문서 1건이 크래시 없이 예측까지 도달
    4. proximity 문맥에 숫자 잔존 0

출력
    ml/models/model2_canonical/model2_p3_bundle.joblib
    ml/models/model2_canonical/manifest.json
"""
import os
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _d in ("pipelines", "evaluation", "experiments"):
    _base = os.path.join(_ML, _d)
    if not os.path.isdir(_base):
        continue
    for _dp, _dn, _fn in os.walk(_base):
        if "__pycache__" in _dp:
            continue
        if _dp not in sys.path:
            sys.path.insert(0, _dp)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import m2_features as F                    # noqa: E402
import feature_builder as FB               # noqa: E402
import preprocessing as PP                 # noqa: E402
import router as RT                        # noqa: E402
import masking as MK                       # noqa: E402

OUT_DIR = os.path.join(_ML, "models", "model2_canonical")
BUNDLE = os.path.join(OUT_DIR, "model2_p3_bundle.joblib")
MANIFEST = os.path.join(OUT_DIR, "manifest.json")

# M82-B 3-run 재현성으로 확정된 성능 (ml/reports/model2/validation/)
PERF = {
    "experiment": "M82/P3",
    "primary_MAE_log10": 0.3518, "strict_MAE_log10": 0.3751,
    "within_2x": 0.5717, "within_3x": 0.7437,
    "split": "GroupKFold(5), group=program_stem (엄격 기준 normalized_title)",
    "vs_m73": -0.0045, "fold_wins": "5/5", "ci95": [-0.0083, -0.0003],
    "reproducibility": "3-run exact match (m82b)",
    "serving_smoke": "8/8 PASS (m82c)",
    "superseded": "M65 세대(단일 XGB) -> M69 -> M73 -> M82/P3",
}

SYNTHETIC = {
    "row_id": "BUNDLE_SELFTEST_0001",
    "title": "2026년 스마트공장 고도화 지원사업 모집 공고",
    "evidence_text": ("□ 지원내용 : 정부지원 비율은 총 사업비의 60% 이내이며 자부담 40%\n"
                      "□ 지원규모 : 기업당 최대 4천만원 이내 (총 25개사 내외 선정)\n"
                      "□ 사업기간 : 협약체결일로부터 10개월\n"
                      "□ 지원조건 : 보조금 방식"),
    "evidence_source": "document",
}


def main():
    t0 = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    print("== 학습 프레임")
    d, src = PP.training_frame()
    fp = F.dataset_fingerprint(src)
    print("   %s / sha %s… / 행 %d" % (fp["path"], fp["sha256"][:16], len(d)))

    print("\n== 변환기 적합 (전체 행)")
    X, y, feat_bundle = FB.fit(d)
    print("   설계행렬 %d행 × %d열" % X.shape)

    print("\n== 모델 적합 — global 1 + expert 3 + ordinal 2")
    model = RT.fit(X, y)
    print("   구간 경계(원) %s" % [int(round(10 ** e)) for e in model["edges"]])

    bundle = {
        "features": feat_bundle,
        "model": model,
        "template": d.head(1).copy(),      # 컬럼 스키마 기준 (값은 쓰지 않는다)
        "meta": {
            "name": "model2_p3", "created": pd.Timestamp.now().isoformat(),
            "dataset": {"path": fp["path"], "sha256": fp["sha256"], "rows": int(len(d))},
            "n_features": int(X.shape[1]),
            "target": "log10(per_recipient), basis=stated_cap",
            "performance": PERF,
            "pipeline": ["preprocessing", "masking", "proximity",
                         "feature_builder", "router"],
        },
    }

    # ------------------------------------------------ 굽기 전 자체 검증
    print("\n== 자체 검증")
    checks = {}
    checks["설계행렬 211열 (M82/P3 과 동일)"] = X.shape[1] == 211
    Xt, P = FB.transform(d, feat_bundle)
    checks["transform 열 이름·순서 일치"] = list(Xt.columns) == list(X.columns)
    residue_train = int(sum(MK.has_digit_residue(t) for t in P["prox_context_text"]))
    checks["학습 proximity 문맥 숫자 잔존 0"] = residue_train == 0

    syn = PP.to_frame(SYNTHETIC, bundle["template"])
    Xs_, Ps = FB.transform(syn, feat_bundle)
    out = RT.predict(model, Xs_)
    pred = float(out["pred_log10"][0])
    checks["합성 신규 문서 예측 도달"] = bool(np.isfinite(pred))
    lo, hi = float(np.percentile(y, 1)), float(np.percentile(y, 99))
    checks["합성 예측이 학습 타깃 1~99 분위 안"] = bool(lo <= pred <= hi)
    checks["합성 proximity 문맥 숫자 잔존 0"] = not MK.has_digit_residue(
        Ps["prox_context_text"].iloc[0])
    for k, ok in checks.items():
        print("   [%s] %s" % ("O" if ok else "X", k))
    print("   합성 예측 %.4f log10 (약 %s원) · 구간확률 %s"
          % (pred, format(int(10 ** pred), ","),
             [round(float(x), 3) for x in out["bucket_proba"][0]]))
    if not all(checks.values()):
        raise SystemExit("자체 검증 실패 — 번들을 쓰지 않는다")

    import joblib
    joblib.dump(bundle, BUNDLE, compress=3)
    size = os.path.getsize(BUNDLE) / 1e6
    print("\n   [bundle] %s (%.1f MB)" % (BUNDLE, size))

    import json
    man = dict(bundle["meta"])
    man["bundle_file"] = os.path.basename(BUNDLE)
    man["bundle_mb"] = round(size, 1)
    man["self_checks"] = {k: bool(v) for k, v in checks.items()}
    man["synthetic_selftest"] = {"pred_log10": round(pred, 4),
                                 "pred_won": int(10 ** pred)}
    man["feature_columns"] = feat_bundle["columns"]
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2, default=str)
    print("   [manifest] %s" % MANIFEST)
    print("\n총 %.0f초" % (time.time() - t0))
    return bundle


if __name__ == "__main__":
    main()
