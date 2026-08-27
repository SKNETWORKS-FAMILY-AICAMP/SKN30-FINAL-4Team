"""E2E용 사전협의 요청서 목업 두 건을 HWPX 로 만든다.

중소벤처기업부 「중소기업지원사업 사전협의 지침」[서식 1] (24년도) 구조와
안내자료의 우수/미흡 사례를 따른 시험용 문서다. 실제 제출 문서가 아니다.

    python scripts/make_mock_hwpx.py
"""

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

NS = (
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph"'
)


def _para(text: str, index: int) -> str:
    return (
        f'<hp:p id="{index}" paraPrIDRef="0" styleIDRef="0">'
        f'<hp:run charPrIDRef="0"><hp:t>{escape(text)}</hp:t></hp:run></hp:p>'
    )


def _cell(text: str, row: int, col: int) -> str:
    inner = "".join(_para(line, i) for i, line in enumerate(text.split("\n")))
    return (
        f"<hp:tc><hp:subList>{inner}</hp:subList>"
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
        f'<hp:cellSpan colSpan="1" rowSpan="1"/>'
        f'<hp:cellSz width="8000" height="1200"/></hp:tc>'
    )


def _table(rows: list[list[str]], para_id: int) -> str:
    body = "".join(
        "<hp:tr>" + "".join(_cell(c, r, k) for k, c in enumerate(cols)) + "</hp:tr>"
        for r, cols in enumerate(rows)
    )
    return (
        f'<hp:p id="{para_id}"><hp:run charPrIDRef="0">'
        f'<hp:tbl rowCnt="{len(rows)}" colCnt="{max(len(r) for r in rows)}">'
        f"{body}</hp:tbl></hp:run></hp:p>"
    )


def build(blocks: list[object]) -> bytes:
    """blocks: str 은 문단, list[list[str]] 은 표."""
    xml = ""
    for index, block in enumerate(blocks):
        xml += (
            _para(block, index)
            if isinstance(block, str)
            else _table(block, 900 + index)
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # mimetype 은 반드시 첫 엔트리이자 무압축이어야 업로드 검증을 통과한다.
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/hwp+zip",
            zipfile.ZIP_STORED,
        )
        archive.writestr(
            "version.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version"'
            ' tagetApplication="WORDPROCESSOR" major="5" minor="0" micro="5"'
            ' buildNumber="0" os="1" application="Hancom Office Hangul"'
            ' appVersion="9.0"/>',
        )
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<ocf:rootfiles><ocf:rootfile full-path="Contents/content.hpf"'
            ' media-type="application/hwpml-package+xml"/></ocf:rootfiles>'
            "</ocf:container>",
        )
        archive.writestr(
            "Contents/content.hpf",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<opf:package xmlns:opf="http://www.idpf.org/2007/opf/" version="">'
            "<opf:metadata/><opf:manifest>"
            '<opf:item id="section0" href="Contents/section0.xml"'
            ' media-type="application/xml"/></opf:manifest>'
            '<opf:spine><opf:itemref idref="section0" linear="yes"/></opf:spine>'
            "</opf:package>",
        )
        archive.writestr(
            "Contents/header.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hh:head xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head"'
            ' version="1.4" secCnt="1"><hh:refList/></hh:head>',
        )
        archive.writestr(
            "Contents/section0.xml",
            f'<?xml version="1.0" encoding="UTF-8"?><hs:sec {NS}>{xml}</hs:sec>',
        )
    return buffer.getvalue()


# ─────────────────────────────────────────────────────────────
# 우수 사례 — 필수 기재항목 13개가 모두 채워진 문서
# ─────────────────────────────────────────────────────────────
GOOD = [
    "【서식 1】 사전협의 요청서",
    "중소기업지원사업 사전협의 요청서",
    "< 과학기술정보통신부 >",
    [
        [
            "사전협의 요청사유",
            "■ 세부사업 신설  □ 내역사업 신설  □ 내내역사업 신설\n"
            "□ 사업내용 변경(지원내용, 지원대상, 사업추진방식 등)",
        ],
        [
            "사업명",
            "(세부) ICT 미래시장 선점 R&D\n"
            "(내역) 세부사업 신설 시 미기재\n"
            "(내내역) 세부사업·내역사업 신설 시 미기재",
        ],
        [
            "신설·변경 필요성(사유)",
            "○ (사업목적) ICT혁신기업이 신시장 창출 동력을 확보하여 고성장 기업으로 "
            "도약할 수 있도록 시장예측 기반 단계별 기술개발·사업화를 지원\n"
            "○ (사업필요성) 국경 없는 ICT 기술기반 시장경쟁에서 ICT 중소기업이 "
            "선제적으로 신시장을 창출하고 신규 서비스를 선점할 수 있도록 R&D 지원 집중 필요\n"
            "○ (지원근거) 「정보통신 진흥 및 융합 활성화 등에 관한 특별법」 제18조"
            "(중소기업 등의 연구개발 지원), 제32조(정보통신·방송융합 기술·서비스 개발 등의 지원)\n"
            "○ (연계정책) 국정과제 [Ⅱ-43] 소프트웨어 강국·ICT 르네상스로 4차 산업혁명 "
            "선도 기반 구축, [Ⅱ-434] 고부가가치 창출 미래형 신산업 발굴·육성, "
            "[Ⅱ-436] 혁신을 응원하는 창업국가 조성",
        ],
        [
            "신설·변경 주요내용",
            "○ (사업기간) '21년 ~ '25년(5년)\n"
            "○ (사업예산) 총사업비 35,300백만원('21년 3,300백만원)\n"
            "○ (지원대상) ICT분야 중소·벤처기업(법인)\n"
            "○ (지원조건) 기업혁신형은 신시장 선점을 위하여 중소·중소 기업간 M&A를 한 "
            "ICT중소기업, 시장개척형은 신시장 진출을 위하여 중소·중소 기업간 전략적 "
            "제휴를 계획하고 있는 ICT중소기업(컨소시엄)\n"
            "○ (지원기간) 최대 3년(2년+1년), 최종 1년(3차년) 지원은 선별적 지원\n"
            "○ (지원규모) 과제당 총 9억원~15억원, 연간 6억원 / 3차년 선별지원\n"
            "○ (지원내용) 시장수요최적화기술개발(3개월) + 고성장기업도약기술개발(12개월), "
            "신규과제 6과제 × 600백만원\n"
            "○ (수행기관) 주무부처 과학기술정보통신부(정책수립 및 예산 지원), "
            "전문기관 정보통신기획평가원(사업 기획 및 평가관리), "
            "수행기관 1~N(R&D 과제수행)",
        ],
        [
            "기대효과",
            "○ (파급효과) ICT분야 창업 → 기술혁신 → M&A성장 → 매출확보로 이어지는 "
            "전주기 지원으로 선순환 혁신 생태계 고도화에 기여\n"
            "○ (성과지표) 사업화성공률, 10억원당 사업화 매출액, 전략적 제휴 건수, "
            "시장수요적용률",
        ],
        [
            "타 제도 협의·심사여부(해당 시)",
            "제 도 명: 해당 없음\n협의부처: -\n협의일시: -\n협의결과: -",
        ],
        [
            "사업담당자(부처, 기관)",
            "담당부서: 정보통신산업기반과\n직급: 사무관\n성명: 홍길동\n"
            "연락처: 044-000-0000\n전자메일: sample@example.go.kr",
        ],
    ],
    "첨 부 : 1. 제출자료 자체 점검표 1부",
    "        2. '24년도 사업계획서(안) 또는 예산 설명자료 및 참고자료 등",
    "□ 사업추진 체계 및 절차",
    "○ (사업추진체계) 주무부처(과학기술정보통신부) → 전문기관(정보통신기획평가원) → "
    "수행기관(R&D 과제수행), 협력기관은 시장·수요예측 전문가 POOL 관리 및 위원회 지원",
    "○ (연차별 지원규모) 1차년도 300백만원(6개월), 2차년도 600백만원(12개월), "
    "3차년도 600백만원(12개월)",
]


# ─────────────────────────────────────────────────────────────
# 미흡 사례 — 필수 기재항목 누락 + 지원내용 설명 부족
# ─────────────────────────────────────────────────────────────
POOR = [
    "【서식 1】 사전협의 요청서",
    "중소기업지원사업 사전협의 요청서",
    "< ○○광역시 >",
    [
        [
            "세부사업명",
            "중소기업 기술사업화 지원사업",
        ],
        [
            "사전협의 요청사유",
            "■ 세부사업 신설  □ 내역사업 신설\n"
            "□ 사업내용 변경(지원내용, 지원대상, 사업추진방식 등)",
        ],
        [
            "신설·변경 필요성(사유)",
            "○ 중소기업 및 중견기업의 현장 애로기술을 해소함으로써 도약 단계의 "
            "맞춤형 지원체계 구축 필요",
        ],
        [
            "신설·변경 주요내용",
            "○ 기술력은 있으나 자금력 등이 취약한 중소기업을 지원\n"
            "- 시제품 제작, 디자인 개발, 인체적용시험, 성과 분석 및 평가비 등 지원",
        ],
        [
            "기대효과",
            "○ 기술개발 이후 사업화 하지 못한 제품에 대하여 현장 수요에 맞는 "
            "긴급 지원으로 기업 성장 기대",
        ],
        [
            "타 제도 협의·심사여부(해당 시)",
            "제 도 명:\n협의부처:\n협의일시:\n협의결과:",
        ],
        [
            "사업담당자(부처, 기관)",
            "담당부서:\n직급:\n성명:\n연락처:\n전자메일:",
        ],
    ],
    "첨 부 : 1. 제출자료 자체 점검표 1부",
    "        2. '24년도 사업계획서(안) 또는 예산 설명자료 및 참고자료 등",
    "□ 사업개요",
    "○ 사업 목적 : 중소기업의 잠재적 보유기술을 상품화하여 시장 진입과 매출증대를 "
    "통해 기업성장 촉진",
    "○ 사업 위치 : ○○광역시 일원",
    "○ 사업 기간 : 2024.1.1. ~ 2024.12.31.(단년도 신규사업)",
    "○ 사 업 량 : 15개사",
    "○ 사업시행방법 : 출연기관 대행",
    "○ 사업 내용 : 기술력은 있으나 자금력 등이 취약한 중소기업을 선정하여 시제품 제작, "
    "디자인 개발, 인체적용시험, 성과 분석 및 평가비 등 지원",
    "○ 지원근거",
    "- 「지방자치단체 출자·출연기관의 운영에 관한 법률」 제20조",
]


def main() -> None:
    out = Path("output/mock")
    out.mkdir(parents=True, exist_ok=True)
    for name, blocks in (
        ("사전협의요청서_우수사례.hwpx", GOOD),
        ("사전협의요청서_미흡사례.hwpx", POOR),
    ):
        path = out / name
        path.write_bytes(build(blocks))
        print(f"{path}  ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
