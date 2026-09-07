"""CPL 결과 → Model 1 입력 텍스트.

학습 때의 입력 조합을 그대로 재현한다
------------------------------------
Model 1 은 `text_for_model` 하나로 학습했고, 그 문자열은 F03 이 이렇게 만들었다.

    ml/pipelines/shared/f03_taxonomy.py:158
        text_for_model = title + "\\n" + purpose + "\\n"
                       + content + "\\n" + target_text

네 조각의 원래 의미는 F03 의 섹션 정규식이 정한다.

    purpose      ① 목적
    content      ② 내용
    target_text  ③ 대상
    (scale_text) ④ 규모 — **일부러 뺐다.** 규모는 Model 2 의 타깃 원천이라
                 Model 1 입력에 넣으면 누수가 된다. 여기서도 넣지 않는다.

CPL 은 이 네 필드를 그대로 갖고 있지 않다
----------------------------------------
CPL 은 13개 `CplFieldCode` 로 되어 있고 `title`·`purpose`·`content`·
`target_text` 라는 이름은 없다. 그래서 아래 표로 옮긴다. 근거는 각 CPL 필드가
사전협의서에서 차지하는 자리이고, 학습 때의 ①②③ 과 뜻이 겹치는 것만 골랐다.

    purpose      <- PURPOSE_GOAL                 (사업목적)
    content      <- NEW_OR_CHANGED_CONTENT       (신설·변경 주요내용)
    target_text  <- TARGET_AND_CONDITIONS        (지원대상 및 요건)
    title        <- **CPL 에 없다.** 호출부가 넘긴다(문서 제목/사건 제목).

`SUPPORT_CONTENT_AND_SCALE` 은 content 후보로 보이지만 기본값에서 **뺐다** —
이름 그대로 지원규모(금액)를 담고 있어 위의 ④규모 제외 원칙과 충돌한다.
문서에 따라 지원내용이 그쪽에만 있는 경우가 있어 `include_scale_field=True`
로 열어 두되, 기본은 꺼 둔다.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED = os.path.abspath(os.path.join(_HERE, "..", "shared"))
if _SHARED not in sys.path:
    sys.path.insert(0, _SHARED)

# 학습 4필드 <- CPL field_code. 값은 우선순위 목록이며 앞에서부터 채운다.
CPL_FIELD_MAP = {
    "purpose": ["PURPOSE_GOAL"],
    "content": ["NEW_OR_CHANGED_CONTENT"],
    "target_text": ["TARGET_AND_CONDITIONS"],
}
SCALE_FIELD = "SUPPORT_CONTENT_AND_SCALE"

# 근거로 쓸 수 있는 상태. MISSING/PARSE_FAILED 는 본문이 없다.
USABLE_STATUS = {"PRESENT", "NEEDS_CONFIRMATION"}

MODEL1_FIELDS = ("title", "purpose", "content", "target_text")


def _as_dict(cpl_result):
    """pydantic 모델이든 dict 든 같은 모양으로 본다 — backend 에 의존하지 않는다."""
    if hasattr(cpl_result, "model_dump"):
        return cpl_result.model_dump(mode="json")
    if isinstance(cpl_result, dict):
        return cpl_result
    raise TypeError("CPL 결과는 dict 또는 pydantic 모델이어야 한다: %r"
                    % type(cpl_result).__name__)


def _items(cpl):
    """items 를 field_code -> item 으로 색인한다."""
    raw = cpl.get("items") or []
    out = {}
    for it in raw:
        code = it.get("field_code")
        if code:
            out[str(code)] = it
    return out


def _text_of(item):
    """한 CPL 항목의 근거 문장들을 이어 붙인다. 못 쓰는 상태면 빈 문자열."""
    if not item or str(item.get("status")) not in USABLE_STATUS:
        return ""
    parts = []
    for occ in item.get("occurrences") or []:
        t = (occ.get("raw_text") or "").strip()
        if t and t not in parts:
            parts.append(t)
    return "\n".join(parts)


def extract_model1_fields(cpl_result, title=None, include_scale_field=False):
    """CPL 결과에서 학습 4필드를 꺼낸다. 없으면 빈 문자열로 둔다(추정 금지)."""
    cpl = _as_dict(cpl_result)
    idx = _items(cpl)

    fields = {"title": (title or "").strip()}
    for name, codes in CPL_FIELD_MAP.items():
        text = ""
        for code in codes:
            text = _text_of(idx.get(code))
            if text:
                break
        fields[name] = text

    if include_scale_field and not fields["content"]:
        fields["content"] = _text_of(idx.get(SCALE_FIELD))

    # 어느 CPL 필드에서 왔는지 남긴다 — 결과에서 원문까지 되짚을 수 있어야 한다.
    fields["_source"] = {
        "title": "caller" if fields["title"] else None,
        "purpose": CPL_FIELD_MAP["purpose"][0] if fields["purpose"] else None,
        "content": (CPL_FIELD_MAP["content"][0] if fields["content"] else None),
        "target_text": (CPL_FIELD_MAP["target_text"][0]
                        if fields["target_text"] else None),
    }
    fields["_block_ids"] = _block_ids(idx)
    return fields


def _block_ids(idx):
    """근거 block_id 목록. CPL occurrence 가 갖고 있는 유일한 원문 앵커다."""
    out = {}
    for name, codes in CPL_FIELD_MAP.items():
        ids = []
        for code in codes:
            item = idx.get(code) or {}
            if str(item.get("status")) not in USABLE_STATUS:
                continue
            for occ in item.get("occurrences") or []:
                b = occ.get("block_id")
                if b and b not in ids:
                    ids.append(b)
        out[name] = ids
    return out


class Model1InputError(ValueError):
    pass


def validate_model1_fields(fields):
    """비어 있으면 조용히 나쁜 예측을 내므로 여기서 막는다.

    네 조각이 전부 필요하지는 않다 — 학습 데이터에도 빈 칸이 있었다. 다만
    **전부 비면** 토큰이 없는 문자열을 분류하는 셈이라 그때는 멈춘다.
    """
    missing = [f for f in MODEL1_FIELDS if not (fields.get(f) or "").strip()]
    if len(missing) == len(MODEL1_FIELDS):
        raise Model1InputError("Model 1 입력이 전부 비어 있다: %s" % list(MODEL1_FIELDS))
    return missing


def build_model1_text(fields):
    """학습과 같은 순서·같은 구분자로 잇는다. 빈 조각은 건너뛴다.

    F03 은 빈 칸도 그대로 이어 붙여 줄바꿈이 연달아 나올 수 있었다. 여기서는
    빈 조각을 빼는데, 서빙 입력에는 결측이 훨씬 흔해 빈 줄만 쌓이면 실제 내용이
    max_len 256 토큰 밖으로 밀려나기 때문이다. 남는 조각의 **순서는 같다.**
    """
    return "\n".join((fields.get(f) or "").strip()
                     for f in MODEL1_FIELDS
                     if (fields.get(f) or "").strip())


def build_model1_input(cpl_result, title=None, include_scale_field=False):
    """CPL 결과 → Model 1 러너가 그대로 쓸 입력 묶음."""
    fields = extract_model1_fields(cpl_result, title=title,
                                   include_scale_field=include_scale_field)
    missing = validate_model1_fields(fields)
    text = build_model1_text(fields)
    return {
        "text": text,
        "fields": {f: fields[f] for f in MODEL1_FIELDS},
        "missing_fields": missing,
        "source_fields": fields["_source"],
        "evidence_block_ids": fields["_block_ids"],
        "char_len": len(text),
    }
