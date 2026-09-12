"""팀원 산출물 패키지를 설치 없이 import 가능하게 만든다.

ponytail: editable install 대신 sys.path 삽입이다. ``profile_structuring`` 은
``[tool.uv] package = false`` 라서 설치 대상이 아니다.

``backend/vendor/**`` 는 원칙적으로 납품본 그대로 두고, 호환 보정이 필요하면
``profiles.py`` 의 경계 shim 처럼 import 시점에 덮는다. 다만 납품본 자체의
결함은 shim 으로 덮지 않고 여기서 고친 뒤 근거를 남긴다. 상류에 반영되면
그 수정은 재납품본에 흡수된다.

수정 이력:

- 2026-09-11 ``request_profile_v012._DATE_RANGE_SEPARATOR``.
  공문 서식은 일자 뒤에도 마침표를 찍는다(``2026. 01. 01. ~ 2028. 12. 31.``).
  구분자가 ``. ~`` 를 넘지 못해 백트래킹이 ``01. 01.`` 을 년-월로 오인했고,
  앞의 ``2026.`` 이 통째로 사라진 후보가 유일한 program_period 후보가 됐다.
  이 필드는 서버 후보만 쓸 수 있어(``required_candidate_kind``) LLM 이
  바로잡을 경로가 없고, 틀린 값이 ``confirmed`` 로 올라갔다. 구분자가 앞
  마침표를 흡수하게 바꿨다. 끝 마침표는 값에 넣지 않는다 — ``공고일~2027.12.31``
  과 같은 관례다. 함께 ``_DROPPED_DOTTED_YEAR_PREFIX`` 가드에서
  ``_MONTH_ONLY_CANDIDATE_PREFIX`` 조건을 걷어냈다. ``2027. 3월`` 만 막고
  점 표기를 놓치던 조건이다. 회귀는 ``backend/tests/test_program_period_candidates.py``
  가 잠근다.

- 2026-09-11 ``adapters/rhwp.py`` ``emit_table``.
  표 셀의 문단을 ``"
".join()`` 으로 합쳐 occurrence 하나만 냈다. 투영
  코드(``common_ir_v1``)는 ``cell["text_occurrence_ids"]`` 를 문단으로 열거하는데
  항상 1 개라 계약이 끊겨 있었고, 공문 서식처럼 한 셀에 사업기간부터 수행기관까지
  담긴 문서는 26 문단 1000 자가 후보 블록 하나가 됐다. LLM 이 그 안에서 20 여 개
  필드의 정확한 문자 좌표를 한꺼번에 골라야 해서 같은 문서·같은 모델에서도 런마다
  결과가 달라졌다. 셀은 whole-cell occurrence 를 ``evidence_ids`` 로 유지한 채
  문단을 따로 접지한다. 한 문단짜리 셀은 자기 자신이 그 문단이므로 출력이 바뀌지
  않는다. ``role`` 은 닫힌 enum 이라 ``rhwp_cell`` 을 그대로 쓴다. 회귀는
  ``backend/tests/test_rhwp_cell_paragraphs.py`` 가 잠근다.

- 2026-09-12 ``field_regions.FIELD_LABELS`` 에 네 필드를 더했다.
  ``applicant_eligibility`` · ``beneficiary`` · ``participation_requirements``
  · ``support_methods``. 구역을 만들기 위해서가 아니라 라벨이 문서에 있는지
  묻기 위해서다. 이 넷은 요청서(서식 1)에 대응 행이 없어 늘 ``not_found`` 로
  나오는데, 그 상태가 ``aggregate_display`` 를 통해 CPL-10·11 항목 전체를
  ``needs_confirmation`` 으로 끌어내렸다. 판별기준 §11.3 · §12.3 은 "조건이
  별도로 없다고 해서 자동으로 오류로 판단하지 않는다" 고 못 박는다.

  라벨 표기는 관측한 것만 넣었다. 요청서 Common IR 58 건에서 이 넷은 라벨
  자리에 0 회, 같은 문서들의 ``지원대상`` 은 199 회 · ``지원조건`` 115 회다.
  ``실제 수혜자`` 는 공고 서식에서 확인된 표기라 함께 뒀다.

  ``build_field_regions`` 는 언제나 명시적 ``field_name`` 으로만 불리므로
  구역 생성 범위는 넓어지지 않는다. ``_ANY_LABEL`` 은 넓어지는데, 넷 중
  ``지원방식`` 만 ``_BOUNDARY_WORD`` 에 없던 낱말이고 그것이 앞 구역을 끊는
  것은 옳다. 회귀는 ``backend/tests/test_cpl_form_absence.py`` 가 잠근다.

- 2026-09-11 ``shared.GENERATOR_VERSION`` 1.0.1 -> 1.1.0.
  위 문단 분리로 산출물 구조가 바뀌었는데 버전이 그대로면 어떤 방식으로 만든
  근거인지 나중에 구분할 수 없다. ``markdown_fixture`` 는 자기 상수를 쓰고
  generator 이름도 달라 영향받지 않는다.
"""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]

VENDOR_PATHS = (
    REPO_ROOT / "backend" / "vendor" / "common_ir_pipeline" / "src",
    REPO_ROOT / "backend" / "vendor" / "portable_existing_request_profiles_20260831",
)


def install() -> None:
    """중복 삽입 없이 vendored 경로를 ``sys.path`` 앞에 둔다."""

    for path in reversed(VENDOR_PATHS):
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)


install()
