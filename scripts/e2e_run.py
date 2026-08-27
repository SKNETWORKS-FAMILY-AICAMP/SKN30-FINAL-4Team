"""전체 파이프라인을 실제 서버에 대고 한 번 통과시킨다.

    uvicorn main:app  (backend/ 에서)  를 먼저 띄우고
    python scripts/e2e_run.py <협의요청서.hwpx>
"""

import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
LOGIN = {"login_id": "demo", "password": "demo-password-1234"}
POLL_TIMEOUT_SECONDS = 600


def main(document: Path) -> int:
    if not document.is_file():
        print(f"파일 없음: {document}")
        return 1

    with httpx.Client(base_url=BASE, timeout=60) as client:
        print("[1/7] 로그인")
        token = client.post("/api/v1/auth/login", json=LOGIN).raise_for_status()
        auth = {"Authorization": f"Bearer {token.json()['access_token']}"}

        print(f"[2/7] 업로드  {document.name}")
        with document.open("rb") as handle:
            created = client.post(
                "/api/v1/cases",
                headers=auth,
                files={"file": (document.name, handle)},
            )
        if created.status_code != 201:
            print(f"  실패 {created.status_code}: {created.text[:300]}")
            return 1
        case_id = created.json()["case_id"]
        print(f"  case_id={case_id}")

        print("[3/7] 분석 시작")
        started = client.post(f"/api/v1/cases/{case_id}/analyze", headers=auth)
        if started.status_code != 202:
            print(f"  실패 {started.status_code}: {started.text[:300]}")
            return 1

        print("[4/7] 상태 폴링")
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        last = None
        while time.monotonic() < deadline:
            status = client.get(
                f"/api/v1/cases/{case_id}/status", headers=auth
            ).json()["status"]
            if status != last:
                print(f"  {status}")
                last = status
            # 한글·코드값 어느 쪽이든 종료 상태를 잡는다.
            if status in {"COMPLETED", "FAILED", "분석 완료", "분석 실패"}:
                break
            time.sleep(3)
        else:
            print("  타임아웃")
            return 1

        if status in {"FAILED", "분석 실패"}:
            print("  분석 실패로 종료. 서버 로그를 확인한다.")
            return 1

        print("[5/7] 리포트 조회")
        report = client.get(f"/api/v1/cases/{case_id}/report", headers=auth)
        if report.status_code != 200:
            print(f"  실패 {report.status_code}: {report.text[:300]}")
            return 1
        body = report.json()
        check = body["self_check"]
        print(
            f"  확인율 {check['confirmed_count']}/{check['total_count']}"
            f" = {check['confirmation_rate']}%"
        )
        fit = body["structural_consistency"]
        print(f"  FIT  {fit['score']['value']}  관계 {len(fit['relations'])}건")
        print(f"  후보 {len(body['similar_candidates'])}건")
        print(f"  쟁점 {len(body['review_issues'])}건")
        if body.get("warnings"):
            print(f"  경고 {body['warnings']}")

        print("[6/7] PDF 내려받기")
        pdf = client.get(f"/api/v1/cases/{case_id}/report.pdf", headers=auth)
        if pdf.status_code != 200:
            print(f"  실패 {pdf.status_code}: {pdf.text[:200]}")
            return 1
        out = Path("output/pdf") / f"e2e_case_{case_id}.pdf"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(pdf.content)
        print(f"  {out}  ({len(pdf.content):,} bytes)")

        print("[7/7] 채팅 질의")
        chat = client.post(
            f"/api/v1/cases/{case_id}/chat/messages",
            headers=auth,
            json={"content": "확인이 필요한 항목을 알려줘"},
        )
        if chat.status_code != 200:
            print(f"  실패 {chat.status_code}: {chat.text[:300]}")
            return 1
        print(f"  {str(chat.json())[:200]}")

    print("\n전체 통과")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(Path(sys.argv[1])))
