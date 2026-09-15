"""모델 2 본문 semantic embedding 층 (M72).

지시서(사용자, `model2_semantic_body_embedding_experiment_plan.md`):

    M69 는 본문 텍스트가 지원규모 신호를 가진다는 것을 보였고, M70 은 TF-IDF
    표현을 아무리 조정해도 추가 개선이 없다는 것을 보였다. 그렇다면 본문의
    **의미**를 dense sentence embedding 으로 표현하면 TF-IDF/SVD 보다 신호를
    더 잘 잡는가 — 그것만 검증한다.

## 입력 텍스트는 M69 것을 그대로 쓴다 (지시서 3장)

`m2_source_features.build()` 가 만든 **마스킹 본문**을 받는다. 금액 표현은
`[AMOUNT]`, 남은 숫자는 `#` 로 덮인 그 문자열이다. 이 파일은 마스킹을 다시
하지 않는다 — 두 곳에서 마스킹하면 규칙이 갈라진다.

## 인코더

    jhgan/ko-sroberta-multitask     한국어 Sentence-BERT (지시서 6장 A)
    BM-K/KoSimCSE-roberta-multitask 한국어 SimCSE      (지시서 6장 B, 대조용)

둘 다 **frozen** 이다. 지시서 12장이 "처음부터 fine-tuning 하지 않는다"를
못 박았고, frozen 에서 신호가 없으면 semantic 축을 종료한다.

## 긴 본문 (지시서 7장)

`ko-sroberta-multitask` 의 `max_seq_length` 는 **128 토큰**이다. bizinfo
공고문 본문 중앙값이 2,594자라 한 번에 넣으면 앞 10%만 보고 나머지는 잘린다.
그래서 세 가지를 다 만들어 비교한다.

    head        앞 한 덩어리만 (지시서 방식 A)
    chunk_mean  전체를 덩어리로 잘라 각각 임베딩한 뒤 평균 (방식 B, 권장 시작)
    section     '사업개요/지원대상/지원내용' 계열 표제 단위로 잘라 평균 (방식 C)

덩어리 길이는 모델의 `max_seq_length` 에서 유도한다 — 모델을 바꾸면 자동으로
따라간다. 한국어는 토큰당 대략 2자이므로 `max_seq_length * 2` 를 쓴다.

## 누수

임베딩은 **텍스트만의 함수**다. 인코더는 pretrained frozen 이라 y 를 본 적이
없고, 이 파일 어디에서도 타깃·예측·오차를 읽지 않는다. 그래서 전체 행을 한
번에 인코딩해도 fold 를 넘나드는 정보가 생기지 않는다. **차원 축소기(SVD)는
다르다** — 그것은 데이터에 적합하는 변환이라 fold train 안에서만 fit 해야
하고, 그 일은 실험 스크립트가 한다(지시서 8장).
"""
# --- ml/ 하위 역할 디렉터리를 sys.path 에 올린다 --------------------------
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

import hashlib
import os
import re
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEMANTIC_VERSION = "m2-semantic-v1"
CACHE = os.path.join(C.PROC, "m72_embeddings")
os.makedirs(CACHE, exist_ok=True)

PRIMARY = "jhgan/ko-sroberta-multitask"          # 지시서 6장 A
SECONDARY = "BM-K/KoSimCSE-roberta-multitask"    # 지시서 6장 B (대조)
ENCODERS = [PRIMARY, SECONDARY]

# 표제 어휘는 M70 이 쓴 것과 같은 것을 쓴다. 두 실험이 다른 '섹션'을 보면
# 비교가 성립하지 않는다.
SECTION_RE = re.compile(r"사업\s*개요|사업\s*목적|사업\s*내용|지원\s*대상|지원\s*내용|"
                        r"지원\s*분야|지원\s*사항|지원\s*자격|신청\s*자격|추진\s*내용|"
                        r"모집\s*분야|사업\s*설명")
POOLINGS = ["head", "chunk_mean", "section"]
MAX_CHUNKS = 16           # 아주 긴 공고문에서 앞 16덩어리(≈4,000자)까지만
_MODELS = {}


def device():
    """`M2_SEMANTIC_DEVICE` 로 고른다. 기본은 cpu.

    원격 GPU 박스에서 임베딩만 뽑아 오는 경로가 있어서 환경변수로 둔다. 같은
    파일을 로컬과 원격이 함께 쓰게 하려는 것이다 — 덩어리 자르기와 pooling 이
    두 곳에서 갈라지면 임베딩이 미묘하게 달라지고, 그 차이는 나중에 추적이
    거의 불가능하다.

    XGBoost 는 여기서 건드리지 않는다. 트리 쪽을 GPU 로 옮기면 histogram
    binning 구현이 달라져 M65 부터 고정해 온 `tree_method="hist"` 결과와
    비교가 깨진다. GPU 는 인코더 forward 에만 쓴다.
    """
    return os.environ.get("M2_SEMANTIC_DEVICE", "cpu")


def _load(name):
    """인코더를 한 번만 올린다. 기본은 오프라인 캐시."""
    if name not in _MODELS:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from sentence_transformers import SentenceTransformer
        _MODELS[name] = SentenceTransformer(name, device=device())
    return _MODELS[name]


def chunk_chars(model):
    """덩어리 길이. 모델의 최대 토큰 수에서 유도한다(한국어 ≈ 토큰당 2자)."""
    return int(model.max_seq_length) * 2


def split_head(text, n):
    return [text[:n]] if text.strip() else [""]


def split_chunks(text, n):
    if not text.strip():
        return [""]
    return [text[i:i + n] for i in range(0, len(text), n)][:MAX_CHUNKS] or [""]


def split_sections(text, n):
    """표제 단위로 자른다. 표제가 없으면 chunk 로 후퇴한다.

    후퇴 경로를 두는 이유는 M70 과 같다 — taxonomy 행의 본문에는 표제가 없어서,
    빈 문자열을 돌려주면 이 변형이 '한 코호트를 지운 실험'이 된다.
    """
    if not text.strip():
        return [""]
    starts = [m.start() for m in SECTION_RE.finditer(text)]
    if not starts:
        return split_chunks(text, n)
    starts = [0] + starts if starts[0] > 0 else starts
    segs = [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)])]
    segs = [s[:n] for s in segs if s.strip()][:MAX_CHUNKS]
    return segs or [""]


SPLITTERS = {"head": split_head, "chunk_mean": split_chunks, "section": split_sections}


def _key(model_name, pooling, texts):
    h = hashlib.sha256()
    h.update(("%s|%s|%s" % (SEMANTIC_VERSION, model_name, pooling)).encode())
    for t in texts:
        h.update(t.encode("utf-8", "ignore"))
        h.update(b"\x00")
    return h.hexdigest()[:24]


def embed(texts, model_name=PRIMARY, pooling="chunk_mean", batch_size=64, verbose=True):
    """마스킹 본문 -> (n, dim) 임베딩. 같은 입력이면 캐시를 읽는다.

    덩어리 임베딩은 **L2 정규화한 뒤** 평균한다. 정규화 없이 평균하면 긴 덩어리의
    노름이 커서 그쪽이 문서 벡터를 지배한다 — 우리가 원하는 것은 '이 문서가
    무엇에 관한 글인가'의 평균이지 '어느 덩어리가 긴가'가 아니다.
    """
    texts = [str(t or "") for t in texts]
    path = os.path.join(CACHE, "%s.npy" % _key(model_name, pooling, texts))
    meta = path.replace(".npy", ".seconds")
    if os.path.exists(path):
        secs = float(open(meta).read()) if os.path.exists(meta) else float("nan")
        return np.load(path), {"cached": True, "seconds": secs}

    model = _load(model_name)
    n = chunk_chars(model)
    pieces, owner = [], []
    for i, t in enumerate(texts):
        segs = SPLITTERS[pooling](t, n)
        pieces.extend(segs)
        owner.extend([i] * len(segs))
    t0 = time.time()
    if verbose:
        print("      [embed] %s / %s — 문서 %d -> 덩어리 %d"
              % (model_name.split("/")[-1], pooling, len(texts), len(pieces)))
    V = model.encode(pieces, batch_size=batch_size, convert_to_numpy=True,
                     normalize_embeddings=True, show_progress_bar=False)
    owner = np.asarray(owner)
    out = np.zeros((len(texts), V.shape[1]), dtype=np.float32)
    for i in range(len(texts)):
        m = owner == i
        out[i] = V[m].mean(0) if m.any() else 0.0
    secs = time.time() - t0
    np.save(path, out)
    with open(meta, "w") as f:
        f.write("%.3f" % secs)
    if verbose:
        print("      [embed] %.0f초 / %d차원  -> %s" % (secs, out.shape[1],
                                                     os.path.basename(path)))
    return out, {"cached": False, "seconds": round(secs, 1)}


def encoder_artifact_bytes(model_name=PRIMARY):
    """서빙에 실어야 하는 인코더 용량 (지시서 11장 8번).

    HF 캐시의 `models--<org>--<repo>` 폴더를 그대로 잰다. 심볼릭 링크가 걸린
    blobs 를 두 번 세지 않도록 실제 파일 크기만 더한다.
    """
    hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME", ""), "hub") if os.environ.get("HF_HOME") else hub
    folder = os.path.join(hub, "models--" + model_name.replace("/", "--"))
    if not os.path.isdir(folder):
        return -1
    seen, total = set(), 0
    for dirpath, _, files in os.walk(folder):
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_ino and st.st_ino in seen:
                continue
            seen.add(st.st_ino)
            total += st.st_size
    return int(total)


def inference_latency(model_name=PRIMARY, pooling="chunk_mean", sample=None, n=32):
    """문서 1건당 인코딩 지연 (지시서 10장). 캐시가 아니라 실제 forward 를 잰다."""
    model = _load(model_name)
    cc = chunk_chars(model)
    docs = list(sample or ["중소기업 시설 자금을 지원합니다. " * 40])[:n]
    pieces = []
    for t in docs:
        pieces.extend(SPLITTERS[pooling](str(t or ""), cc))
    model.encode(pieces[:8], convert_to_numpy=True, show_progress_bar=False)   # warm-up
    t0 = time.time()
    model.encode(pieces, batch_size=64, convert_to_numpy=True,
                 normalize_embeddings=True, show_progress_bar=False)
    dt = time.time() - t0
    return {"n_docs": len(docs), "n_chunks": len(pieces),
            "ms_per_doc": round(1000 * dt / max(len(docs), 1), 1),
            "ms_per_chunk": round(1000 * dt / max(len(pieces), 1), 1)}


def manifest():
    return {
        "semantic_version": SEMANTIC_VERSION,
        "encoders": {"primary": PRIMARY, "secondary": SECONDARY},
        "frozen": "fine-tuning 없음 (지시서 12장) — frozen 에서 신호가 없으면 축 종료",
        "input_text": "m2_source_features 의 마스킹 본문 그대로 (지시서 3장)",
        "poolings": list(POOLINGS),
        "chunk_chars": "max_seq_length * 2 (한국어 ≈ 토큰당 2자)",
        "max_chunks": MAX_CHUNKS,
        "chunk_aggregation": "덩어리를 L2 정규화한 뒤 평균 — 긴 덩어리가 노름으로 "
                             "문서 벡터를 지배하지 않게",
        "leakage": "임베딩은 텍스트만의 함수이고 인코더는 frozen pretrained 다. "
                   "차원 축소기(SVD)는 데이터 적합이므로 fold train 안에서만 fit 한다",
        "cache_dir": os.path.relpath(CACHE, C.ROOT),
    }
