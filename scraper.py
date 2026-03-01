import json
import os
import re
import sys
from html import unescape
from pathlib import Path

import cloudscraper
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hibrain.net"
LIST_URL = f"{BASE_URL}/recruitment/recruits?listType=D3NEW&pagesize=50&sortType=SORTDTM"
SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
MAX_SEEN = 500


def load_seen() -> list[str]:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return []


def save_seen(seen: list[str]) -> None:
    # 최근 MAX_SEEN개만 유지
    seen = seen[-MAX_SEEN:]
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_job_id(href: str) -> str:
    """URL에서 공고 ID 추출: /recruitment/recruits/3563236?... -> 3563236"""
    match = re.search(r"/recruits/(\d+)", href)
    return match.group(1) if match else ""


def fetch_page() -> str:
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "linux"})
    resp = scraper.get(LIST_URL, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape_jobs() -> list[dict]:
    text = fetch_page()


    soup = BeautifulSoup(text, "html.parser")
    article_list = soup.find("ul", id="articleList")
    if not article_list:
        print("articleList를 찾을 수 없습니다.")
        return []

    jobs = []
    for li in article_list.find_all("li", class_="row"):
        link_tag = li.find("a", href=True)
        if not link_tag:
            continue

        href = unescape(link_tag["href"])
        job_id = extract_job_id(href)
        if not job_id:
            continue

        title = link_tag.get("title", "").strip() or link_tag.get_text(strip=True)

        # 접수기간 추출
        receipt_span = li.find("span", class_="td_receipt")
        period = ""
        if receipt_span:
            numbers = receipt_span.find_all("span", class_="number")
            if len(numbers) >= 2:
                period = f"{numbers[0].get_text(strip=True)} ~ {numbers[1].get_text(strip=True)}"

        jobs.append({
            "id": job_id,
            "title": title,
            "period": period,
            "url": f"{BASE_URL}/recruitment/recruits/{job_id}",
        })

    return jobs


def build_slack_message(new_jobs: list[dict]) -> dict:
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "🆕 하이브레인 신규 채용공고",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for job in new_jobs[:20]:  # Slack 블록 제한 고려하여 최대 20개
        text = f"*<{job['url']}|{job['title']}>*"
        if job["period"]:
            text += f"\n접수기간: {job['period']}"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        })

    if len(new_jobs) > 20:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"외 {len(new_jobs) - 20}건 더 있습니다. <{BASE_URL}/recruitment/recruits?listType=D3NEW|전체 보기>",
                }
            ],
        })

    return {"blocks": blocks}


def send_to_slack(message: dict) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        print("Slack 전송을 건너뜁니다.")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return

    resp = requests.post(webhook_url, json=message, timeout=10)
    resp.raise_for_status()
    print("Slack 전송 완료!")


def main():
    print("hibrain.net 채용정보 스크래핑 시작...")

    jobs = scrape_jobs()
    print(f"총 {len(jobs)}개 공고 발견")

    if not jobs:
        print("공고를 가져오지 못했습니다.")
        sys.exit(1)

    seen = load_seen()
    seen_set = set(seen)

    new_jobs = [j for j in jobs if j["id"] not in seen_set]
    print(f"신규 공고: {len(new_jobs)}개")

    if not new_jobs:
        print("새로운 공고가 없습니다.")
        return

    # Slack 전송
    message = build_slack_message(new_jobs)
    send_to_slack(message)

    # seen 목록 업데이트
    for job in new_jobs:
        seen.append(job["id"])
    save_seen(seen)

    print(f"seen_jobs.json 업데이트 완료 (총 {len(load_seen())}개)")


if __name__ == "__main__":
    main()
