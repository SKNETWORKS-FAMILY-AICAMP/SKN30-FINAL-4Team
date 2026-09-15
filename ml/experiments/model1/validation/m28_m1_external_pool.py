"""M28 — 모델 1 외부 검증셋 확장(41건 → 150건) 후보 추출.

왜 필요한가
    지금 모델 1 의 외부 성능 근거는 M07 의 수동 정답 41건 하나다. 41건은
    신호로는 의미가 있어도 결론으로는 약하다. 19클래스에 41건을 나누면
    클래스당 1~10건이고, 실제로 '융자' 10건 / 나머지 대부분 1~4건이라
    클래스별 수치는 신뢰구간이 거의 전 구간을 덮는다. 정확도 한 자리가
    ±15%p 씩 흔들리는 상태에서 ML/DL 최종 채택을 결정할 수는 없다.

    수정 방향 문서(1.4 A)가 요구한 것도 같다 — 외부 hold-out 을 100~200건으로
    늘리되, **표본 수가 아니라 대표성**이 핵심이라는 단서가 붙어 있다.

무엇을 하는가
    라벨링 자체는 원문을 읽어야 하는 일이라 여기서 못 한다. 대신 **바로 채울 수
    있는 후보 시트**를 만든다. 기존 50건과 같은 규칙으로 100건을 더 뽑아
    합계 150건을 만든다.

표집 규칙 — 기존 50건과 동일하게 맞춘다
    원문 필수    HWP/HWPX 첨부 원문이 추출된 공고만. PDF 는 표 셀이 뭉쳐
                 원문이 깨진다(F05·M08 과 같은 기준). 요약문만 보고 붙인
                 라벨은 정답이 아니라 '요약문 기준 추정'이 되어 외부 정답셋의
                 자격을 잃는다. M07 에서 실제로 원문이 판정을 뒤집은 사례가 있다
                 (M&A 활성화 사업 — 요약문으로는 컨설팅/융자 구분이 안 됐다).
    대분류 균등  CORE8 에 균등 배분. Open API 는 경영 422 / 기타 18 로 편중돼
                 있어 비례 추출하면 경영이 표본을 먹는다.
    기관 상한    두 축을 나눠 건다. agency 는 광역시도 단위라 후보 569건에
                 30종밖에 없어(경기도 73 / 전남광주 52 …) 여기에 낮은 상한을
                 걸면 표본 수 자체가 천장에 걸린다. 실제 공고 문체를 결정하는
                 건 집행기관(executor, 182종)이다. executor 2건 / agency 15건
                 (추가분의 15%)으로 잡아 한 지역·한 기관이 외부 성능을
                 대표하지 못하게 한다.
    사업 중복    program_stem(제목에서 연도·차수·괄호 제거) 이 같으면 1건만.
                 같은 사업이 시·군별로 20건 들어오는 걸 막는다.
    학습 중복    학습 원천(business_taxonomy)의 program_stem 과 겹치면 제외.
                 외부 hold-out 인데 학습에서 본 사업이면 외부가 아니다.
    기존 50건    이미 라벨이 있으니 후보에서 뺀다(평가 때 다시 합친다).

의도적으로 하지 않는 것 — 모델 예측 기준 층화
    19클래스가 골고루 들어오게 하려면 모델 예측으로 층화하는 게 제일 쉽다.
    하지만 그러면 외부 정확도가 '모델이 각 클래스라고 생각한 건들' 위에서
    계산돼 숫자의 의미가 바뀐다. 층화는 메타데이터(대분류·기관)로만 하고,
    예측은 라벨링이 끝난 뒤에 붙인다. 시트에 모델 예측·확신도를 넣지 않는
    것도 같은 이유다(M23 의 blind 원칙).

한계
    대분류 균등 표집이라 클래스 편중이 완전히 사라지지는 않는다. 어떤 클래스가
    몇 건 들어올지는 라벨링이 끝나야 안다. 리포트에 기존 41건의 클래스 분포를
    같이 실어, 라벨링 후 어느 클래스가 여전히 얇은지 바로 보게 한다.
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
# 스크립트끼리 평면 이름으로 import 한다(`import common as C`,
# `from m13_m3_anomaly import prepare`). 역할별 디렉터리로 나눈 뒤에도 그
# import 가 그대로 동작하게 세 디렉터리를 얹는다. 파일 위치는 ml/ 바로 아래
# 한 단계여야 한다 — common.ROOT 가 그렇게 계산된다.
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

import argparse
import json
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
from f03_taxonomy import program_stem

DETAIL = os.path.join(C.PROC, "announcement_detail.parquet")
TAX = os.path.join(C.PROC, "business_taxonomy.parquet")
DOCS = C.report_path("e01_documents.jsonl")
LABELED = os.path.join(C.DATA, "labels", "openapi_manual_50.csv")
OUT_SHEET = os.path.join(C.DATA, "labels", "openapi_manual_150_candidates.csv")

SEED = 42
N_ADD = 100                 # 50 + 100 = 150
AGENCY_CAP = 15             # 광역시도 단위(30종). 추가분의 15% 상한
EXECUTOR_CAP = 2            # 집행기관 단위(182종). 문체 다양성의 실제 축
MIN_CHARS = 500             # 이보다 짧으면 원문으로 판정할 수 없다
DOC_EXT = ("hwp", "hwpx")

# 원문에서 판정 근거가 되는 절. 라벨러가 전문을 다 읽지 않아도 되게 뽑아 준다.
EVIDENCE_HEADS = ["지원내용", "지원 내용", "지원사항", "지원분야", "지원규모",
                  "지원조건", "지원방식", "사업내용", "지원한도", "융자조건"]
EVIDENCE_SPAN = 260         # 표제어 뒤로 몇 자를 근거로 볼지
EVIDENCE_MAX = 900

_BRACKET = re.compile(r"[\[\(<【][^\]\)>】]*[\]\)>】]")
_ROUND = re.compile(r"제?\s*\d+\s*차|[1-9]차|상반기|하반기")
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")
# 같은 사업의 재공고를 다른 사업으로 보이게 만드는 상용구.
# '해외지사화 지원사업 참여업체 모집 공고' 와 '해외지사화 지원사업 추가모집 공고'
# 는 같은 사업인데 이 단어들 때문에 키가 갈렸다.
_BOILER = re.compile("|".join([
    "추가모집", "재모집", "재공고", "추가공고", "변경공고", "연장공고", "정정공고",
    "참여업체", "참여기업", "참가기업", "수혜기업", "참여자", "모집공고",
    "상시모집", "수시모집", "모집", "공고", "신청", "접수", "안내", "선정",
    "추가", "연장", "변경", "정정",
]))


def norm_stem(title):
    """제목에서 연도·차수·괄호(지역/기관 표기)·모집 상용구를 걷어낸 사업 식별 키."""
    s = program_stem(str(title or ""))
    s = _BRACKET.sub(" ", s)
    s = _ROUND.sub(" ", s)
    s = _BOILER.sub(" ", s)
    return _NONWORD.sub("", s)


def load_docs(path, exts=DOC_EXT, source="api"):
    """공고별로 가장 긴 원문 하나. F05·M08 과 같은 규칙."""
    best = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("source") != source or d.get("ext") not in exts:
                continue
            text = d.get("text") or ""
            aid = d.get("announcement_id")
            if len(text) > len(best.get(aid, ("", ""))[0]):
                best[aid] = (text, d.get("ext"))
    return best


def evidence(text):
    """판정 근거 절 발췌. 표제어가 없으면 앞부분을 준다."""
    flat = re.sub(r"[ \t ]+", " ", text)
    out, seen = [], set()
    for head in EVIDENCE_HEADS:
        i = flat.find(head)
        if i < 0 or head in seen:
            continue
        seen.add(head)
        out.append(flat[i:i + EVIDENCE_SPAN])
        if sum(len(x) for x in out) >= EVIDENCE_MAX:
            break
    body = " / ".join(out) if out else flat[:EVIDENCE_MAX]
    return re.sub(r"\s*\n\s*", " / ", body)[:EVIDENCE_MAX].strip()


def allocate(pool, n, seed=SEED, cap=AGENCY_CAP, ex_cap=EXECUTOR_CAP):
    """대분류 균등 배분 + 기관 상한. 모자란 칸은 여유 있는 쪽에서 채운다.

    상한 때문에 칸이 남는 경우가 있어(창업 20건, 기타 3건처럼 얇은 대분류)
    2단계로 채운다. ① 대분류 할당량대로, ② 남은 칸은 대분류를 풀고 상한만
    지키며 채운다. 그래도 모자라면 executor 상한을 한 칸 올려 마지막으로
    채운다 — 상한은 쏠림 방지 장치지 표본 수를 깎는 목적이 아니다.
    """
    rng = np.random.default_rng(seed)
    cats = [c for c in C.CORE8 if (pool["category_large"] == c).any()]
    quota = {c: n // len(cats) for c in cats}
    for c in cats[: n % len(cats)]:
        quota[c] += 1

    taken, used_ag, used_ex = [], {}, {}

    def draw(sub, k, ex_limit):
        got = 0
        for i in rng.permutation(len(sub)):
            if got >= k:
                break
            r = sub.iloc[int(i)]
            if r["announcement_id"] in taken:
                continue
            ag, ex = str(r["agency"] or ""), str(r["executor"] or "")
            if used_ag.get(ag, 0) >= cap or used_ex.get(ex, 0) >= ex_limit:
                continue
            used_ag[ag] = used_ag.get(ag, 0) + 1
            used_ex[ex] = used_ex.get(ex, 0) + 1
            taken.append(r["announcement_id"])
            got += 1
        return got

    for c in cats:
        draw(pool[pool["category_large"] == c], quota[c], ex_cap)
    for limit in (ex_cap, ex_cap + 1):          # 남은 칸 채우기
        if len(taken) >= n:
            break
        draw(pool, n - len(taken), limit)
    return taken


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=N_ADD, help="추가로 뽑을 건수")
    ap.add_argument("--cap", type=int, default=AGENCY_CAP, help="기관당 상한")
    ap.add_argument("--force", action="store_true", help="라벨이 채워진 시트도 덮어쓴다")
    ap.add_argument("--dry-run", action="store_true", help="시트를 쓰지 않고 표집만 본다")
    a = ap.parse_args()

    det = pd.read_parquet(DETAIL)
    tax = pd.read_parquet(TAX)
    docs = load_docs(DOCS)
    done = pd.read_csv(LABELED, encoding="utf-8-sig")
    done.columns = [c.strip("﻿") for c in done.columns]

    steps = [("Open API 전체", len(det))]
    d = det[det["announcement_id"].isin(docs)].copy()
    steps.append(("HWP/HWPX 원문 있음", len(d)))

    d["doc_text"] = [docs[i][0] for i in d["announcement_id"]]
    d["ext"] = [docs[i][1] for i in d["announcement_id"]]
    d = d[d["doc_text"].str.len() >= MIN_CHARS]
    steps.append(("원문 %d자 이상" % MIN_CHARS, len(d)))

    d = d[~d["announcement_id"].isin(set(done["announcement_id"]))]
    steps.append(("기존 라벨 50건 제외", len(d)))

    d["stem"] = [norm_stem(t) for t in d["title"]]
    tax_stems = {norm_stem(t) for t in tax["title"]}
    d = d[~d["stem"].isin(tax_stems)]
    steps.append(("학습셋과 같은 사업 제외", len(d)))

    d = d.sort_values("announcement_id").drop_duplicates("stem")
    steps.append(("같은 사업 중복 제거", len(d)))

    d["category_large"] = [C.norm_category(v) for v in d["category_large"]]
    pool = d[d["category_large"].isin(C.CORE8)].reset_index(drop=True)
    steps.append(("대분류 CORE8", len(pool)))

    picked = allocate(pool, a.n, cap=a.cap)
    sel = pool[pool["announcement_id"].isin(picked)].reset_index(drop=True)
    rng = np.random.default_rng(SEED)
    sel = sel.iloc[rng.permutation(len(sel))].reset_index(drop=True)

    sheet = pd.DataFrame({
        "idx": np.arange(len(done) + 1, len(done) + len(sel) + 1),
        "announcement_id": sel["announcement_id"],
        "category_large": sel["category_large"],
        "agency": sel["agency"],
        "title": sel["title"],
        "ext": sel["ext"],
        "n_chars": sel["doc_text"].str.len(),
        "evidence": [evidence(t) for t in sel["doc_text"]],
        "source_url": sel["source_url"],
        "label_19class": "",
        "confidence": "",
        "exclude_reason": "",
        "note": "",
    })
    # 이미 라벨이 채워진 시트를 덮어쓰면 사람이 붙인 정답이 날아간다.
    # 표집은 seed 고정이라 결과가 같지만, 덮어쓰기는 명시적으로만 허용한다.
    if os.path.exists(OUT_SHEET) and not a.force:
        prev = pd.read_csv(OUT_SHEET, encoding="utf-8-sig")
        if prev.get("label_19class", pd.Series(dtype=str)).fillna("").ne("").any():
            raise SystemExit(
                "이미 라벨이 채워진 시트가 있다: %s / 덮어쓰려면 --force, "
                "표집만 다시 보려면 --dry-run." % OUT_SHEET)
    if not a.dry_run:
        sheet.to_csv(OUT_SHEET, index=False, encoding="utf-8-sig")

    prior = done["label_19class"].fillna("(판단보류/대상밖)").value_counts()
    report = {
        "filter_steps": [{"step": s, "n": int(n)} for s, n in steps],
        "n_existing_labeled": int(len(done)),
        "n_existing_evaluable": int(done["exclude_reason"].isna().sum()),
        "n_added": int(len(sheet)),
        "n_target_total": int(len(done) + len(sheet)),
        "agency_cap": a.cap,
        "executor_cap": EXECUTOR_CAP,
        "min_chars": MIN_CHARS,
        "seed": SEED,
        "added_by_category": {k: int(v) for k, v in
                              sheet["category_large"].value_counts().items()},
        "added_by_ext": {k: int(v) for k, v in sheet["ext"].value_counts().items()},
        "n_agencies": int(sheet["agency"].nunique()),
        "existing_label_distribution": {k: int(v) for k, v in prior.items()},
        "sheet": os.path.relpath(OUT_SHEET, C.ROOT),
        "blind": "모델 예측·확신도는 시트에 넣지 않는다. 라벨링이 끝난 뒤 M29 에서 붙인다.",
    }
    C.save_report("m28_m1_external_pool.json", report)

    print("외부 검증셋 확장 후보 %d건 → %s" % (len(sheet), OUT_SHEET))
    for s, n in steps:
        print("  %-24s %5d" % (s, n))
    print("  기관 %d곳 / 대분류 %s" % (report["n_agencies"],
                                      report["added_by_category"]))


if __name__ == "__main__":
    main()
