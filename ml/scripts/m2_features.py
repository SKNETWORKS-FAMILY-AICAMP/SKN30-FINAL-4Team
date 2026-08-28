"""모델 2 feature pipeline — 고정판.

M53 이 측정한 조건을 **한 곳에 못 박는다.** 실험 스크립트(M53)와 감사
스크립트(M55), canonical 스크립트(M56), 그리고 서비스 추론이 모두 여기를
import 하게 해서 "학습은 M53 인데 서빙은 M45 feature" 같은 어긋남이 생길
자리를 없앤다(M29 에서 모델 1이 정확히 그 이유로 한 번 뒤집혔다).

여기 있는 것.

    DATASET_*        데이터셋 지문(경로·행수·해시)과 타깃 정의
    STRUCTURED_*     기존 구조화 feature 목록 (M45 와 동일)
    TITLE_SPEC       제목 텍스트 feature 규격
    XGB_*            모델 파라미터 (M53 실측값. 새 튜닝을 추가하지 않는다)
    normalize_business_title()   그룹키용 제목 정규화
    mask_amount_expressions()    누수 점검용 금액 표현 마스킹
    group_key()                  GroupKFold 그룹키 생성
    build_features()             fold train 에만 적합하는 feature 조립

여기 없는 것: 비교군 사다리·percentile 조회·타깃 정제. 그것들은 바뀌지
않았으므로 `m45_m2_amount` 를 그대로 쓴다. 바뀐 것만 새로 적는다.

제목 feature 를 어떻게 읽어야 하는가 (해석 규율):

    틀린 말   사업 제목이 지원규모를 결정한다.
    쓰는 말   사업 제목은 지원규모의 원인이 아니라, 기존 구조화 feature 에서
             충분히 표현되지 않은 사업 유형·지원형태·세부 목적 정보를
             보완하는 텍스트 feature 다.
"""
import hashlib
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import m45_m2_amount as M45

# ------------------------------------------------------------ 버전 태그
FEATURE_VERSION = "m2-feat-v2"       # v1 = M45(구조화만), v2 = 구조화 + 제목 SVD
GROUPING_VERSION = "m2-group-v2"     # v1 = program_stem 만, v2 = + 정규화제목(엄격)
PIPELINE_SEED = 42

# ------------------------------------------------------------ 데이터셋
DATASET_PATH = M45.SRC
DATASET_BUILDER = "ml/scripts/f06_design_features.py"
DATASET_UPSTREAM = [
    "f05_amount_observations.py — 지원규모 관측 추출 (PDF 확장자 제외: EXCLUDE_EXT={'pdf'})",
    "e01_extract_text.py — 공고문 원문 텍스트 추출",
    "amount_parser.py — 금액/기간/기업수 파싱 (M32 패턴 수정 반영)",
]

# 타깃. 아래 7종 중 무엇을 쓰는지 못 박는다 — 섞으면 percentile 이 두 의미의
# 가중평균을 가리킨다(M45 1장에서 실측: 같은 비교군 안에서 최대 9.97배 차이).
TARGET_COLUMN = "per_recipient"
TARGET_TRANSFORM = "log10"
TARGET_BASIS_KEPT = "stated_cap"
TARGET_SEMANTICS = {
    "total_budget": "제외 — 사업 전체 예산. per_recipient 와 단위가 다르다",
    "support_cap / support_per_recipient": "**사용** — 원문에 명시된 기업당 한도(stated_cap)",
    "support_per_project": "amount_type=per_project 인 행은 support_unit=project 로 "
                           "비교군이 갈린다. 타깃 자체는 같은 stated_cap",
    "budget_div_count(총예산÷건수)": "제외 — '평균'이라 '한도'와 의미가 다르다",
    "loan_limit": "별도 컬럼 없음. 융자는 support_method=loan 으로 비교군이 갈린다",
    "support_rate": "타깃 아님 — feature(support_ratio)",
    "selected_count": "타깃 아님 — feature(support_count)",
}
TARGET_FILTERS = [
    "support_type 결측 제외",
    "per_recipient 결측·0 이하 제외",
    "amount_outlier(상식범위 밖 = 파싱오류) 제외",
    "per_recipient_basis != 'stated_cap' 제외",
    "support_unit / cohort 결측 제외",
]
EXPECTED_N = 1877

# ------------------------------------------------------------ 구조화 feature
STRUCTURED_CATS = list(M45.CATS)      # M45 와 같은 목록을 참조로 가져온다
STRUCTURED_NUMS = list(M45.NUMS)
COHORT_AS_FEATURE = True              # '사용자가 비교 모집단을 골랐다'는 가정

# ------------------------------------------------------------ 제목 feature
TITLE_SPEC = {
    "source": "title (원문 사업명). evidence_text 는 타깃이 파싱된 문장이라 사용 금지",
    "input_form": "amount_masked",    # raw | amount_masked
    "vectorizer": "TfidfVectorizer",
    "analyzer": "char_wb",
    "ngram_range": (2, 3),
    "min_df": 3,
    "max_features": 30000,
    "sublinear_tf": True,
    "reduction": "TruncatedSVD",
    "n_components": 64,
    "fitted_on": "fold train only",
    "output_prefix": "title_svd",
}

# ------------------------------------------------------------ 모델
# M53 실측값. 여기서 새 튜닝을 하지 않는다. 명시하지 않으면 라이브러리 기본값에
# 의존하게 되므로 기본값도 값으로 적어 고정한다.
XGB_POINT = {
    "objective": "reg:absoluteerror",
    "n_estimators": 800,
    "learning_rate": 0.03,
    "max_depth": 6,
    "subsample": 0.9,
    "colsample_bytree": 0.8,
    "min_child_weight": 1,
    "reg_alpha": 0.0,
    "reg_lambda": 1.0,
    "tree_method": "hist",
    "enable_categorical": True,
    "random_state": PIPELINE_SEED,
}
XGB_QUANTILE = dict(XGB_POINT, objective="reg:quantileerror")
QUANTILES = (0.10, 0.90)
NOMINAL_COVERAGE = 0.80
CQR_CAL_FRAC = 0.30
N_SPLITS = 5
MIN_IMPROVEMENT = 0.10


# ------------------------------------------------------------ 제목 전처리
# 금액 표현. amount_parser 의 단위 사전을 그대로 쓰되, 단위어가 붙지 않은
# '3억'·'50%'·'천만' 같은 표기까지 넓게 잡는다. 목적이 파싱이 아니라
# '혹시라도 남아 있는 금액 문자열을 지우는 것'이라 넓게 잡는 쪽이 안전하다.
_UNIT_ALT = "|".join(sorted(M45.__dict__.get("UNIT_MULT", {}) or
                            __import__("amount_parser").UNIT_MULT, key=len, reverse=True))
_NUM = r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"
AMOUNT_IN_TITLE = re.compile(
    r"(?:%s)\s*(?:%s)" % (_NUM, _UNIT_ALT)          # 1,000만원 / 3억원
    + r"|(?:%s)\s*[억만천](?![가-힣])" % _NUM        # 3억 / 5천
    + r"|(?:%s)\s*%%" % _NUM                          # 50%
)
AMOUNT_TOKEN = "[AMOUNT]"

# 그룹키 정규화에서 지울 것들. 같은 사업 계열이 학습/검증에 갈라지지 않게
# 하는 것이 목적이므로 '사업을 구별하지 않는 수식어'만 지운다.
_REGION = re.compile(r"[\[(（【][^\])）】]*[\])）】]")          # [대전] (경기) 등
_YEAR = re.compile(r"(19|20)\d{2}\s*년도?|['’]\d{2}\s*년도?")   # 2026년 / '25년
_ROUND = re.compile(r"\d+\s*(차|회|기|분기|호)\b|제\s*\d+\s*(차|회|기)")
_REPOST = re.compile(r"(재공고|연장\s*공고|수정\s*공고|변경\s*공고|정정\s*공고"
                     r"|추가\s*공고|기간\s*연장|모집\s*연장)")
_NOISE = re.compile(r"(공고문?|모집|시행계획|시행|계획|안내|알림|접수|선정|추진)")
_NONWORD = re.compile(r"[^0-9A-Za-z가-힣]+")
_DIGITS = re.compile(r"\d+")


def mask_amount_expressions(text):
    """제목 안의 금액·비율 표현을 [AMOUNT] 로 바꾼다.

    타깃 누수 경로를 구조적으로 끊기 위한 것이다. 이 데이터셋의 제목에는
    실제로 금액 표현이 0건이지만(M55 2.1), '지금 0건'과 '앞으로도 0건'은
    다르다. 새 공고 제목에 '최대 1억원 지원'이 들어오면 그 순간 모델 2 는
    타깃을 제목에서 읽게 된다. 그래서 파이프라인에 상시로 건다.
    """
    return AMOUNT_IN_TITLE.sub(AMOUNT_TOKEN, str(text or ""))


def normalize_business_title(title):
    """사업 계열을 식별하기 위한 제목 정규화 (그룹키 전용).

    모델 입력이 아니다. 지역·연도·회차·재공고 표현처럼 **같은 사업을 다르게
    보이게 만드는 수식어**만 지운다. 지우고 남은 문자열이 같으면 같은 사업
    계열로 보고 학습/검증을 가른다.

        "[대전] 2022년 중소기업 경영안정자금 지원 공고"
        "[충북] 2019년 중소기업 경영안정자금 지원 재공고(2차)"
        -> 둘 다 "중소기업경영안정자금지원"
    """
    s = str(title or "")
    s = mask_amount_expressions(s)
    s = _REGION.sub(" ", s)
    s = _YEAR.sub(" ", s)
    s = _ROUND.sub(" ", s)
    s = _REPOST.sub(" ", s)
    s = _NOISE.sub(" ", s)
    s = _DIGITS.sub("", s)               # 남은 숫자는 회차·연도 잔재로 본다
    s = s.replace(AMOUNT_TOKEN.replace("[", "").replace("]", ""), "")
    return _NONWORD.sub("", s)


def group_key(d, mode="program_stem"):
    """GroupKFold 그룹키.

    program_stem   M45 가 쓴 기준. 재공고(같은 사업의 반복 공고)를 묶는다.
    normalized_title  더 엄격. 지역·연도·회차만 다른 '같은 사업 계열'까지 묶는다.
                   제목 텍스트를 feature 로 쓰는 순간 이쪽이 진짜 기준이 된다.
    """
    if mode == "program_stem":
        return d["group_key"].astype(str).to_numpy()
    if mode == "normalized_title":
        return np.array([normalize_business_title(t) for t in d["title"].fillna("")])
    raise ValueError("unknown grouping mode: %s" % mode)


def titles_for_model(d, form=None):
    """모델에 들어가는 제목 문자열. 기본은 금액 마스킹본."""
    form = form or TITLE_SPEC["input_form"]
    raw = d["title"].fillna("").astype(str)
    if form == "raw":
        return raw.to_numpy()
    if form == "amount_masked":
        return raw.map(mask_amount_expressions).to_numpy()
    raise ValueError("unknown title form: %s" % form)


# ------------------------------------------------------------ feature 조립
def fit_title_features(train_titles, test_titles, spec=None):
    """TF-IDF -> SVD. fold train 에만 적합한다(M45 의 fold 내부 fitting 규율)."""
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    s = dict(TITLE_SPEC, **(spec or {}))
    v = TfidfVectorizer(analyzer=s["analyzer"], ngram_range=tuple(s["ngram_range"]),
                        min_df=s["min_df"], max_features=s["max_features"],
                        sublinear_tf=s["sublinear_tf"])
    A = v.fit_transform(train_titles)
    svd = TruncatedSVD(n_components=s["n_components"], random_state=PIPELINE_SEED)
    return svd.fit_transform(A), svd.transform(v.transform(test_titles)), (v, svd)


def title_columns(k=None):
    k = k or TITLE_SPEC["n_components"]
    return ["%s%02d" % (TITLE_SPEC["output_prefix"], i) for i in range(k)]


def build_features(Xs, titles, tr, te, use_structured=True, use_title=True, spec=None):
    """fold 단위 feature 조립. 반환 순서가 학습·서빙 공통의 feature order 다."""
    if not use_title:
        return Xs.iloc[tr], Xs.iloc[te], None
    a, b, fitted = fit_title_features(titles[tr], titles[te], spec)
    cols = title_columns(a.shape[1])
    ta = pd.DataFrame(a, columns=cols)
    tb = pd.DataFrame(b, columns=cols)
    if not use_structured:
        return ta, tb, fitted
    return (pd.concat([Xs.iloc[tr].reset_index(drop=True), ta], axis=1),
            pd.concat([Xs.iloc[te].reset_index(drop=True), tb], axis=1), fitted)


def make_point_model(**over):
    import xgboost as xgb
    return xgb.XGBRegressor(**dict(XGB_POINT, **over))


def make_quantile_model(alphas=QUANTILES, **over):
    import xgboost as xgb
    return xgb.XGBRegressor(**dict(XGB_QUANTILE, quantile_alpha=np.array(alphas), **over))


# ------------------------------------------------------------ 지문
def file_sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def dataset_fingerprint():
    st = os.stat(DATASET_PATH)
    raw = pd.read_parquet(DATASET_PATH)
    d, drop = M45.prepare(raw)
    return {
        "path": os.path.relpath(DATASET_PATH, C.ROOT),
        "builder": DATASET_BUILDER,
        "upstream": list(DATASET_UPSTREAM),
        "sha256": file_sha256(DATASET_PATH),
        "bytes": int(st.st_size),
        "mtime": pd.Timestamp(st.st_mtime, unit="s").isoformat(),
        "rows_raw": int(len(raw)),
        "rows_after_filters": int(len(d)),
        "expected_n": EXPECTED_N,
        "n_matches_expected": bool(len(d) == EXPECTED_N),
        "filter_trace": drop,
        "target": {"column": TARGET_COLUMN, "transform": TARGET_TRANSFORM,
                   "basis_kept": TARGET_BASIS_KEPT, "semantics": TARGET_SEMANTICS,
                   "filters": TARGET_FILTERS},
    }


def pipeline_manifest():
    return {
        "feature_version": FEATURE_VERSION,
        "grouping_version": GROUPING_VERSION,
        "seed": PIPELINE_SEED,
        "structured_cats": STRUCTURED_CATS + (["cohort"] if COHORT_AS_FEATURE else []),
        "structured_nums": STRUCTURED_NUMS,
        "title_spec": {k: (list(v) if isinstance(v, tuple) else v)
                       for k, v in TITLE_SPEC.items()},
        "model_point": XGB_POINT,
        "model_quantile": {k: v for k, v in XGB_QUANTILE.items()},
        "quantiles": list(QUANTILES),
        "nominal_coverage": NOMINAL_COVERAGE,
        "cqr_cal_frac": CQR_CAL_FRAC,
        "n_splits": N_SPLITS,
        "min_improvement": MIN_IMPROVEMENT,
        "excluded_inputs": ["evidence_text (타깃이 파싱된 문장 — 사용 금지)"],
    }
