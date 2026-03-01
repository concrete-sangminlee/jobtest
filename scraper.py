import json
import os
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import quote

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
    seen = seen[-MAX_SEEN:]
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_job_id(href: str) -> str:
    match = re.search(r"/recruits/(\d+)", href)
    return match.group(1) if match else ""


def fetch_page() -> str:
    scraper_api_key = os.environ.get("SCRAPER_API_KEY")
    if scraper_api_key:
        print("ScraperAPI를 통해 페이지 가져오는 중...")
        url = f"http://api.scraperapi.com?api_key={scraper_api_key}&url={quote(LIST_URL)}"
        resp = requests.get(url, timeout=60)
    else:
        print("직접 요청으로 페이지 가져오는 중...")
        resp = requests.get(LIST_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        }, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape_jobs() -> list[dict]:
    try:
        text = fetch_page()
    except Exception as e:
        print(f"페이지 가져오기 실패: {e}")
        return []

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

    for job in new_jobs[:20]:
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
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"

    if test_mode:
        print("[테스트 모드] 최근 3개 공고를 샘플로 Slack에 전송합니다.")

    print("hibrain.net 채용정보 스크래핑 시작...")

    jobs = scrape_jobs()
    print(f"총 {len(jobs)}개 공고 발견")

    if not jobs:
        print("공고를 가져오지 못했습니다. (다음 주기에 재시도)")
        sys.exit(0)

    if test_mode:
        sample = jobs[:3]
        message = build_slack_message(sample)
        message["blocks"][0]["text"]["text"] = "🧪 [테스트] 하이브레인 채용공고 알림 테스트"
        send_to_slack(message)
        print(f"테스트 메시지 전송 완료 ({len(sample)}건)")
        return

    seen = load_seen()
    seen_set = set(seen)

    new_jobs = [j for j in jobs if j["id"] not in seen_set]
    print(f"신규 공고: {len(new_jobs)}개")

    if not new_jobs:
        print("새로운 공고가 없습니다.")
        return

    message = build_slack_message(new_jobs)
    send_to_slack(message)

    for job in new_jobs:
        seen.append(job["id"])
    save_seen(seen)

    print(f"seen_jobs.json 업데이트 완료 (총 {len(load_seen())}개)")


if __name__ == "__main__":
    main()
