import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from html import unescape, escape
from pathlib import Path
from typing import Optional

from fetcher import BASE_URL, LIST_URL, FetchError, fetch_page
from delivery import deliver, load_state, state_lock, webhook_urls
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))

SEEN_FILE = Path(__file__).parent / "seen_jobs.json"


def log(msg: str) -> None:
    """KST 타임스탬프가 붙은 구조적 로그."""
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def extract_job_id(href: str) -> str:
    match = re.search(r"/recruits/(\d+)", href)
    return match.group(1) if match else ""


def scrape_jobs() -> list[dict]:
    return parse_jobs(fetch_page(log))


def parse_jobs(text: str) -> list[dict]:
    soup = BeautifulSoup(text, "html.parser")
    article_list = soup.find(id="articleList")
    if not article_list:
        raise FetchError("articleList를 찾을 수 없습니다. 차단 또는 페이지 구조 변경을 확인하세요.")
    jobs = []
    seen_ids = set()
    rows = article_list.find_all("li", class_="row")
    invalid = 0
    for li in rows:
        if not li.get_text(strip=True) and not li.find("a", href=True):
            continue  # Empty spacer/placeholder rows are not job postings.
        job = parse_job_row(li)
        if not job:
            invalid += 1
        elif job["id"] not in seen_ids:
            jobs.append(job)
            seen_ids.add(job["id"])
    if not jobs:
        raise FetchError("유효한 공고가 0건입니다. 빈 목록/차단/파싱 오류를 확인하세요.")
    if invalid:
        # Refuse silent partial collection when rows no longer match the parser.
        raise FetchError(f"공고 행 {invalid}/{len(rows)}개를 파싱하지 못했습니다. 사이트 구조를 확인하세요.")
    return jobs


# 행에서 부가 정보를 뽑을 때 시도할 후보 CSS 클래스들.
# 사이트 마크업이 조금씩 달라도 최대한 값을 건지도록 여러 후보를 순회한다.
_COMPANY_CLASSES = ("td_company", "td_institution", "td_agency", "company", "institution")
_REGION_CLASSES = ("td_area", "td_region", "td_location", "area", "region")
_TYPE_CLASSES = ("td_type", "td_employ", "td_field", "type", "field")


def _first_text(li, class_names) -> str:
    """후보 클래스 중 처음 발견되는 요소의 텍스트를 정리해 반환한다."""
    for cls in class_names:
        el = li.find(class_=cls)
        if el:
            text = _clean(el.get_text(" ", strip=True))
            if text:
                return text
    return ""


def _clean(text: str) -> str:
    """공백/개행을 한 칸으로 정리."""
    return re.sub(r"\s+", " ", text or "").strip()


def parse_job_row(li) -> Optional[dict]:
    """채용 목록의 <li.row> 하나를 구조화된 dict로 변환한다.

    필수 값(id, title)이 없으면 None을 반환하고,
    부가 값(회사/지역/유형/마감일)은 있으면 채우고 없으면 생략한다.
    """
    link_tag = li.find("a", href=re.compile(r"/recruits/\d+"))
    if not link_tag:
        return None

    href = unescape(link_tag["href"])
    job_id = extract_job_id(href)
    if not job_id:
        return None

    title = _clean(link_tag.get("title", "")) or _clean(link_tag.get_text(strip=True))
    if not title:
        return None

    # 접수 기간 (시작 ~ 마감)
    start_date = end_date = ""
    receipt_span = li.find("span", class_="td_receipt")
    if receipt_span:
        numbers = receipt_span.find_all("span", class_="number")
        if len(numbers) >= 2:
            start_date = _clean(numbers[0].get_text(strip=True))
            end_date = _clean(numbers[1].get_text(strip=True))
        elif len(numbers) == 1:
            end_date = _clean(numbers[0].get_text(strip=True))

    period = f"{start_date} ~ {end_date}" if start_date and end_date else (end_date or "")

    # 상시채용 여부 (접수 기간 텍스트에 '상시' 등이 들어가는 경우)
    receipt_text = _clean(receipt_span.get_text(" ", strip=True)) if receipt_span else ""
    always_open = any(k in receipt_text for k in ("상시", "수시", "채용시"))

    d_day = compute_d_day(end_date) if end_date else None

    return {
        "id": job_id,
        "title": title,
        "company": _first_text(li, _COMPANY_CLASSES),
        "region": _first_text(li, _REGION_CLASSES),
        "job_type": _first_text(li, _TYPE_CLASSES),
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "always_open": always_open,
        "d_day": d_day,
        "url": f"{BASE_URL}/recruitment/recruits/{job_id}",
    }


def _parse_date(text: str) -> Optional[datetime]:
    """'2026.09.21' / '2026-09-21' / '2026.9.21' 같은 표기를 날짜로 파싱."""
    m = re.search(r"(?<!\d)(\d{4}|\d{2})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})(?!\d)", text or "")
    if not m:
        return None
    try:
        year = int(m.group(1))
        if len(m.group(1)) == 2:
            year += 2000
        return datetime(year, int(m.group(2)), int(m.group(3)), tzinfo=KST)
    except ValueError:
        return None


def compute_d_day(end_date: str) -> Optional[int]:
    """마감일까지 남은 일수. 오늘=0(D-DAY), 내일=1. 파싱 실패 시 None."""
    deadline = _parse_date(end_date)
    if not deadline:
        return None
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (deadline - today).days


def build_slack_message(new_jobs: list[dict], *, title: str = "") -> dict:
    now = datetime.now(KST)
    timestamp = now.strftime("%Y. %m. %d  %H:%M KST")
    count = len(new_jobs)
    if not 1 <= count <= 20:
        raise ValueError("Slack 메시지는 1~20건씩 생성해야 합니다.")
    display_jobs = new_jobs

    header_line = title or "*📢  하이브레인 신규 채용공고*"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header_line},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"🏢 하이브레인  ｜  🔔 *{count}건*의 새로운 채용공고"},
            ],
        },
        {"type": "divider"},
    ]

    for job in display_jobs:
        safe_title = escape(job["title"][:500], quote=False).replace("|", "｜")
        title_line = f"> *<{job['url']}|{safe_title}>*"
        if job.get("period"):
            safe_period = escape(job["period"][:100], quote=False).replace("`", "")
            title_line += f"\n> 📅 `{safe_period}`"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": title_line},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "지원하기", "emoji": True},
                "url": job["url"],
                "style": "primary",
            },
        })

    blocks.append({"type": "divider"})

    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📋 전체 채용공고 보기", "emoji": True},
                "url": f"{BASE_URL}/recruitment/recruits?listType=D3NEW",
            },
        ],
    })

    footer_text = f"🤖 HiBrain Job Alert  ｜  {timestamp}"

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": footer_text},
        ],
    })

    return {
        "text": f"하이브레인 신규 채용공고 {count}건",
        "attachments": [
            {
                "color": "#0054a6",
                "blocks": blocks,
            }
        ]
    }


def write_report(report, output):
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("### HiBrain 수집 결과\n\n```json\n" + text + "```\n")


def run(dry_run=False, state_path=SEEN_FILE):
    report = {"time": datetime.now(KST).isoformat(), "dry_run": dry_run,
              "collected": 0, "delivered": 0, "failed_channels": 0, "pending": 0}
    urls = [] if dry_run else webhook_urls()
    # Validate state before collecting/sending; never silently reset corrupted state.
    state = load_state(state_path)
    fetch_error = None
    try:
        jobs = scrape_jobs()
        report["collected"] = len(jobs)
        log(f"총 {len(jobs)}개 공고 발견")
    except FetchError as exc:
        jobs = []
        fetch_error = str(exc)
        log(fetch_error)
    if dry_run:
        # Generate every batch for payload validation; no POST and no state writes.
        for offset in range(0, len(jobs), 20):
            build_slack_message(jobs[offset:offset + 20])
    else:
        def sort_key(job):
            day = compute_d_day(job.get("end_date", ""))
            return (1, 0) if job.get("always_open") or day is None or day < 0 else (0, day)
        jobs.sort(key=sort_key)
        # Retry an existing outbox even if today's collection fails.
        report.update(deliver(jobs, urls, state_path, state, build_slack_message, log))
    report["status"] = "failed" if fetch_error or report["failed_channels"] else "success"
    if fetch_error:
        report["error"] = fetch_error
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="무료 하이브레인 수집 및 Slack 알림")
    parser.add_argument("--dry-run", action="store_true", help="수집/메시지 생성만 검증 (전송·기록 변경 없음)")
    parser.add_argument("--state-file", type=Path, default=SEEN_FILE)
    parser.add_argument("--report", type=Path, help="실행 결과 JSON 경로")
    args = parser.parse_args(argv)
    try:
        # A report path must never overwrite state or source files.
        if args.report and (args.report.resolve() in {args.state_file.resolve(), SEEN_FILE.resolve()} or args.report.suffix != ".json"):
            args.report = None
            raise ValueError("--report에는 상태 파일과 다른 .json 경로를 사용하세요.")
        if args.dry_run:
            report = run(True, args.state_file)
        else:
            with state_lock(args.state_file):
                report = run(False, args.state_file)
    except (ValueError, RuntimeError, OSError) as exc:
        # IO error text can contain paths; our handled errors never contain secret values.
        log(f"실행 실패: {type(exc).__name__}")
        report = {"status": "failed", "dry_run": args.dry_run,
                  "error": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}
    write_report(report, args.report)
    detail = report.get("error") or f"수집 {report.get('collected', 0)}건, 전송 {report.get('delivered', 0)}건, 실패 채널 {report.get('failed_channels', 0)}개, 대기 {report.get('pending', 0)}건"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        level = "error" if report["status"] == "failed" else "notice"
        print(f"::{level}::{escaped}")
    else:
        log(detail)
    log(f"실행 결과: {report['status']}")
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
