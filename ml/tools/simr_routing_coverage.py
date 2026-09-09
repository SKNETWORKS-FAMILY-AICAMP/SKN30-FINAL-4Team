"""SIM-R routing 사다리가 실제 공고 라벨에서 몇 단계까지 내려가는지 센다.

공고를 DB 에 backfill 하기 전에 정책의 현실성을 먼저 본다. 필요한 것은 이미
있다. `announcement_detail_with_support_type_v2.parquet` 에 공고 1,570건의
Model 1 예측(support_type / confidence / status)이 들어 있다.

재는 것은 하나다. **요청서가 trusted 일 때 사다리 어느 단계에서 Top-K 가
채워지는가.**

    1 동일 support_type + 공고 trusted
    2 동일 support_type + 공고 trusted/reference
    3 동일 support_type 전체 grade
    4 unfiltered

1단계에서 멈추면 hard routing 이 실제로 코퍼스를 좁힌 것이고, 4단계까지 가면
좁히기가 아무 일도 하지 않은 것이다. 그 비율이 정책의 값어치다.

계산부는 backfill script 도 쓴다
-------------------------------
`scripts/backfill_support_type.py` 가 dry-run 에서 같은 표를 찍는다. 두 곳이
따로 세면 backfill 전후 숫자를 비교할 수 없으므로 계산은 여기 `coverage()` 하나만
둔다. 표준 라이브러리만 쓰므로 pandas 없이도 import 된다.

사다리 정의는 backend 의 런타임 쪽(`retrieval._LADDER`)과 같은 뜻이어야 한다.
한쪽만 바꾸면 여기서 잰 숫자가 실제 검색과 어긋난다.

요청 분포는 알 수 없다
---------------------
요청서가 어떤 support_type 으로 분류될지의 분포는 아직 표본이 없다. 그래서 두
가지로 나눠 적는다. 클래스별 표는 가정이 없고, 가중 요약은 **요청이 공고와
같은 분포로 들어온다**는 가정 위에 있다. 대리 지표이지 측정값이 아니다.
"""
import os
import sys
from collections import Counter

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ML = os.path.abspath(os.path.join(_HERE, ".."))
CORPUS = os.path.join(_ML, "data", "processed",
                      "announcement_detail_with_support_type_v2.parquet")

# 러너의 TRUST_GRADE 와 같은 대응. 한국어 status 를 라우팅이 쓰는 등급으로 옮긴다.
GRADE = {"신뢰": "trusted", "참고용": "reference", "판단보류": "hold"}

K = 5

# 사다리 각 단계가 허용하는 공고 등급. 4단계는 support_type 자체를 보지 않는다.
STAGES = (
    (1, ("trusted",)),
    (2, ("trusted", "reference")),
    (3, ("trusted", "reference", "hold")),
)
UNFILTERED_STAGE = 4


def settle_stage(counts, k=K):
    """후보가 k 이상이 되는 첫 단계. 3단계까지도 모자라면 4(unfiltered)."""
    for stage, _grades in STAGES:
        if counts[stage] >= k:
            return stage
    return UNFILTERED_STAGE


def coverage(pairs, k=K):
    """(support_type, grade) 목록 → 단계별 정착 통계.

    grade 는 trusted / reference / hold 여야 한다. 라벨이 없는 공고는 애초에
    사다리에 걸리지 않으므로 호출부에서 빼고 넘긴다.
    """
    per_class = {}
    for support_type, grade in pairs:
        bucket = per_class.setdefault(support_type, Counter())
        bucket[grade] += 1

    total = sum(sum(bucket.values()) for bucket in per_class.values())
    classes = []
    for support_type, bucket in per_class.items():
        counts = {
            stage: sum(bucket[grade] for grade in grades)
            for stage, grades in STAGES
        }
        classes.append({
            "support_type": support_type,
            "n_trusted": counts[1],
            "n_trusted_reference": counts[2],
            "n_all": counts[3],
            "stage": settle_stage(counts, k),
        })
    classes.sort(key=lambda row: (-row["n_all"], row["support_type"]))

    stages = (1, 2, 3, UNFILTERED_STAGE)
    by_class = {stage: 0 for stage in stages}
    by_weight = {stage: 0.0 for stage in stages}
    for row in classes:
        by_class[row["stage"]] += 1
        if total:
            by_weight[row["stage"]] += row["n_all"] / total

    return {
        "k": k,
        "total": total,
        "classes": classes,
        "by_stage_classes": by_class,
        "by_stage_weight": by_weight,
    }


def format_coverage(report, label="공고"):
    """coverage() 결과를 사람이 읽는 표로."""
    lines = []
    total = report["total"]
    k = report["k"]
    classes = report["classes"]
    lines.append("%s %d건 · K=%d · 요청서가 trusted 일 때" % (label, total, k))
    lines.append("")
    lines.append("%-12s %8s %8s %8s   %s"
                 % ("support_type", "1단계", "2단계", "3단계", "정착 단계"))
    lines.append("-" * 60)
    for row in classes:
        lines.append("%-12s %8d %8d %8d   %d단계%s"
                     % (row["support_type"], row["n_trusted"],
                        row["n_trusted_reference"], row["n_all"], row["stage"],
                        "  ← 좁히기 무효" if row["stage"] == UNFILTERED_STAGE else ""))
    lines.append("")

    n_classes = len(classes) or 1
    lines.append("클래스 %d개가 어느 단계에서 Top-%d 를 채우는가"
                 % (len(classes), k))
    for stage in (1, 2, 3, UNFILTERED_STAGE):
        n = report["by_stage_classes"][stage]
        lines.append("  %d단계  %2d개 (%4.1f%%)" % (stage, n, 100 * n / n_classes))
    lines.append("")

    lines.append("요청이 공고와 같은 분포로 들어온다고 볼 때 (대리 지표)")
    for stage in (1, 2, 3, UNFILTERED_STAGE):
        lines.append("  %d단계  %5.1f%%"
                     % (stage, 100 * report["by_stage_weight"][stage]))
    lines.append("")

    settled = report["by_stage_weight"][1]
    lines.append("  1단계에서 끝남 (hard 가 실제로 좁힘) : %.1f%%" % (100 * settled))
    lines.append("  2단계 이상 확장                     : %.1f%%"
                 % (100 * (1 - settled) if total else 0.0))
    lines.append("")

    lines.append("1단계로 정착한 클래스의 코퍼스 축소율")
    first = [row for row in classes if row["stage"] == 1]
    if not first:
        lines.append("  없음")
    for row in first:
        lines.append("  %-12s %4d/%d 건으로 축소 (%.1f%%)"
                     % (row["support_type"], row["n_trusted"], total,
                        100 * row["n_trusted"] / total if total else 0.0))
    return "\n".join(lines)


def load_pairs_from_parquet(path=CORPUS):
    import pandas as pd

    df = pd.read_parquet(path)
    unknown = sorted(set(df["support_type_status"]) - set(GRADE))
    if unknown:
        raise SystemExit("알 수 없는 status: %s" % unknown)
    return [
        (row.support_type_pred, GRADE[row.support_type_status])
        for row in df.itertuples(index=False)
    ]


def main():
    print(format_coverage(coverage(load_pairs_from_parquet())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
