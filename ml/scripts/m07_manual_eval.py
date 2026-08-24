"""M07 — 사람이 붙인 정답과 모델 예측을 대조해 실제 정확도를 잰다.

왜 필요한가
    추론 대상인 Open API 에는 정답 라벨이 없다. 그동안 판단보류율·확신도만 봤는데,
    그건 "모델이 얼마나 자신 있어 하는가"지 "맞는가"가 아니다. 확신에 차서 틀릴
    수도 있다. 그래서 원문을 직접 읽고 50건에 손으로 라벨을 붙였다.

정답셋
    ml/data/labels/openapi_manual_50.csv
    대분류 8종에서 균형 표집한 50건. PDF 는 표 셀 병합으로 원문이 깨지므로
    HWP/HWPX 만 골랐다. 판정은 요약문이 아니라 첨부 원문을 읽고 했다.

    label_26class  태깅 당시(26클래스) 라벨. 기록용.
    label_19class  현재 학습 체계(19클래스)로 옮긴 라벨. 평가는 이걸로 한다.
    exclude_reason 평가에서 빼는 이유. 빈 값이면 평가 대상.

평가에서 빼는 9건
    대상밖-관광(4)      여행사 대상 관광객 유치 인센티브. 중소기업 지원이 아니다.
    대상밖-개인복지(1)  소상공인 개인 건강검진비.
    대상밖-비지원(1)    세금 체납 분납. 돈을 주는 게 아니다.
    대상밖-복합(1)      성격이 여러 갈래라 하나로 안 묶인다.
    컷오프미달(2)       '자격인증'은 학습 8건으로 MIN_SUPPORT(10) 미달이라 클래스가
                        아예 없다. 모델이 무엇을 내놓든 구조적으로 틀린다.
    이 9건을 정확도에 넣으면 모델 탓이 아닌 것을 모델 탓으로 돌리게 된다.
    대신 따로 세어서 "정답이 없는 공고가 얼마나 들어오는가"로 보고한다.

주의
    50건은 클래스당 표본이 매우 적다(융자 6건, 나머지 대부분 1~4건).
    전체 정확도는 참고가 되지만 클래스별 수치는 신뢰구간이 매우 넓다.
    M06 와 같은 이유다. 점추정만 보고 판단하지 말 것.
"""
import argparse
import io
import os

import numpy as np
import pandas as pd

from common import PROC, save_report

LABELS = os.path.join(PROC, "..", "labels", "openapi_manual_50.csv")
PRED_ML = PROC + "/announcement_detail_with_support_type_v2.parquet"   # M02 (LogisticRegression)
PRED_DL = PROC + "/openapi_support_type_roberta.parquet"               # DL05 (KLUE-RoBERTa)


def load_labels():
    lab = pd.read_csv(LABELS, encoding="utf-8-sig")
    lab["announcement_id"] = lab["announcement_id"].astype(str)
    for c in ("label_19class", "exclude_reason"):
        lab[c] = lab[c].fillna("").astype(str)
    return lab


def load_pred(path, id_col="announcement_id"):
    if not os.path.exists(path):
        return None
    p = pd.read_parquet(path)
    p[id_col] = p[id_col].astype(str)
    keep = [id_col, "support_type_pred"]
    ren = {}
    for a, b in (("support_type_confidence", "confidence"),
                 ("confidence", "confidence"),
                 ("support_type_status", "status"),
                 ("status", "status")):
        if a in p.columns and b not in ren.values():
            keep.append(a)
            ren[a] = b
    return p[keep].rename(columns=ren)


def evaluate(lab, pred, name, only_usable):
    """정답이 있는 건만 대상. only_usable 이면 판단보류를 제외하고 잰다."""
    m = lab.merge(pred, on="announcement_id", how="left")
    m = m[m["label_19class"] != ""].copy()
    n_total = len(m)
    n_missing = int(m["support_type_pred"].isna().sum())
    m = m[m["support_type_pred"].notna()]

    if only_usable and "status" in m.columns:
        m = m[m["status"] != "판단보류"]
    m["correct"] = m["support_type_pred"] == m["label_19class"]

    acc = float(m["correct"].mean()) if len(m) else float("nan")
    res = {
        "model": name,
        "scope": "판단보류 제외" if only_usable else "전체(보류 포함)",
        "n_labeled": n_total,
        "n_missing_pred": n_missing,
        "n_scored": int(len(m)),
        "n_correct": int(m["correct"].sum()),
        "accuracy": round(acc, 4) if len(m) else None,
    }
    return res, m


def confusion_lines(m, limit=20):
    """틀린 건만 정답->예측으로 세어 보여준다."""
    bad = m[~m["correct"]]
    if bad.empty:
        return []
    c = (bad.groupby(["label_19class", "support_type_pred"])
            .size().sort_values(ascending=False))
    return [(a, b, int(n)) for (a, b), n in c.head(limit).items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None, help="건별 대조표를 쓸 경로(.md)")
    args = ap.parse_args()

    lab = load_labels()
    n_lab = int((lab["label_19class"] != "").sum())
    print("정답셋 %d건 중 평가 대상 %d건 / 제외 %d건"
          % (len(lab), n_lab, len(lab) - n_lab))
    print("제외 사유:", lab[lab["exclude_reason"] != ""]["exclude_reason"]
          .value_counts().to_dict())
    print()

    report = {
        "labels": LABELS,
        "n_labeled_total": int(len(lab)),
        "n_evaluable": n_lab,
        "excluded": lab[lab["exclude_reason"] != ""]["exclude_reason"]
                       .value_counts().to_dict(),
        "results": [],
    }

    dumps = []
    for name, path in (("M02 LogisticRegression", PRED_ML),
                       ("DL05 KLUE-RoBERTa", PRED_DL)):
        pred = load_pred(path)
        if pred is None:
            print("%-24s 예측 파일 없음 — 건너뜀 (%s)" % (name, os.path.basename(path)))
            continue
        for only_usable in (False, True):
            res, m = evaluate(lab, pred, name, only_usable)
            report["results"].append(res)
            acc = "%.1f%%" % (res["accuracy"] * 100) if res["accuracy"] is not None else "—"
            print("%-24s %-14s 채점 %2d건 중 %2d건 정답  정확도 %s"
                  % (name, res["scope"], res["n_scored"], res["n_correct"], acc))
            if only_usable:
                mis = confusion_lines(m)
                if mis:
                    print("     오분류 (정답 -> 예측):")
                    for a, b, n in mis:
                        print("       %-12s -> %-12s %d건" % (a, b, n))
                dumps.append((name, m))
        print()

    if args.dump and dumps:
        with io.open(args.dump, "w", encoding="utf-8") as f:
            f.write("# 수동 정답 vs 모델 예측 — 건별 대조\n")
            for name, m in dumps:
                f.write("\n## %s (판단보류 제외)\n\n" % name)
                f.write("| # | 공고 | 정답 | 예측 | 확신 | 결과 |\n|---|---|---|---|---|:--:|\n")
                for _, r in m.sort_values("idx").iterrows():
                    conf = r.get("confidence")
                    f.write("| %d | %s | %s | %s | %s | %s |\n"
                            % (r["idx"], str(r["title"])[:44], r["label_19class"],
                               r["support_type_pred"],
                               "%.3f" % conf if pd.notna(conf) else "—",
                               "○" if r["correct"] else "×"))
        print("건별 대조표 → %s" % args.dump)

    save_report("m07_manual_eval.json", report)


if __name__ == "__main__":
    main()
