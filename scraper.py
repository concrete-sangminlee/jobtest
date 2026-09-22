import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hibrain.net"
LIST_URL = f"{BASE_URL}/recruitment/recruits?listType=D3NEW&pagesize=50&sortType=SORTDTM"
SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
MAX_SEEN = 500

# 네트워크 튜닝
MAX_ATTEMPTS = 3           # 각 방식(ScraperAPI/직접)당 최대 시도 횟수
BACKOFF_BASE = 3           # 재시도 대기 시간(초) = BACKOFF_BASE * 시도횟수
SCRAPERAPI_TIMEOUT = 70    # ScraperAPI는 프록시/렌더링으로 느릴 수 있어 넉넉히
DIRECT_TIMEOUT = 30

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


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


def _looks_like_job_list(html: str) -> bool:
    """응답이 실제 채용 목록 페이지인지 최소 검증. (차단 페이지/빈 응답 거르기)"""
    return bool(html) and 'id="articleList"' in html


def _fetch_via_scraperapi(api_key: str) -> str:
    # HTTPS 엔드포인트를 사용해 프록시 구간 신뢰성을 높인다.
    url = (
        "https://api.scraperapi.com/"
        f"?api_key={api_key}&url={quote(LIST_URL)}"
    )
    resp = requests.get(url, timeout=SCRAPERAPI_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _fetch_direct() -> str:
    resp = requests.get(LIST_URL, headers=BROWSER_HEADERS, timeout=DIRECT_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _try_method(name: str, fetch_fn) -> Optional[str]:
    """한 가지 방식(ScraperAPI 또는 직접)을 재시도와 함께 실행한다.

    성공적으로 채용 목록으로 보이는 HTML을 얻으면 반환하고,
    모든 시도가 실패하면 None을 반환한다.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"{name} 시도 {attempt}/{MAX_ATTEMPTS}...")
            html = fetch_fn()
        except requests.RequestException as e:
            print(f"  {name} 실패: {type(e).__name__}: {e}")
        else:
            if _looks_like_job_list(html):
                print(f"  {name} 성공")
                return html
            print(f"  {name} 응답에 채용 목록이 없습니다. (차단/빈 응답 가능성)")

        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_BASE * attempt
            print(f"  {wait}초 후 재시도...")
            time.sleep(wait)

    print(f"{name} 최종 실패")
    return None


def fetch_page() -> str:
    """채용 목록 HTML을 가져온다.

    ScraperAPI 키가 있으면 먼저 시도(재시도 포함)하고,
    실패하면 직접 요청으로 폴백한다. 모두 실패하면 예외를 던진다.
    """
    scraper_api_key = os.environ.get("SCRAPER_API_KEY")

    if scraper_api_key:
        html = _try_method("ScraperAPI", lambda: _fetch_via_scraperapi(scraper_api_key))
        if html is not None:
            return html
        print("ScraperAPI가 실패하여 직접 요청으로 폴백합니다.")

    html = _try_method("직접 요청", _fetch_direct)
    if html is not None:
        return html

    raise RuntimeError("모든 방식으로 페이지를 가져오지 못했습니다.")


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
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    timestamp = now.strftime("%Y. %m. %d  %H:%M KST")
    count = len(new_jobs)
    display_jobs = new_jobs[:20]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*📢  하이브레인 신규 채용공고*",
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"🏢 하이브레인  ｜  🔔 *{count}건*의 새로운 채용공고",
                },
            ],
        },
        {"type": "divider"},
    ]

    for job in display_jobs:
        title_line = f"> *<{job['url']}|{job['title']}>*"
        if job["period"]:
            title_line += f"\n> 📅 `{job['period']}`"

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
    if count > 20:
        footer_text = f"외 *{count - 20}건* 추가  ｜  " + footer_text

    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": footer_text},
        ],
    })

    return {
        "attachments": [
            {
                "color": "#0054a6",
                "blocks": blocks,
            }
        ]
    }


def webhook_label(url: str) -> str:
    """웹훅 URL에서 토큰을 제거한 식별용 라벨. (로그에 시크릿이 남지 않도록)"""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2:
        return f".../{parts[-2]}/***"
    return "***"


def redact(text: str, url: str) -> str:
    """예외/응답 메시지에 섞여 있을 수 있는 웹훅 토큰을 가립니다."""
    token = url.rstrip("/").rsplit("/", 1)[-1]
    return text.replace(token, "***") if token else text


# Slack 웹훅이 돌려주는 에러 본문별 안내
SLACK_ERROR_HINTS = {
    "channel_is_archived": "채널이 아카이브되었습니다. 채널을 복구하거나 SLACK_WEBHOOK_URL에서 이 웹훅을 제거하세요.",
    "channel_not_found": "채널을 찾을 수 없습니다. 공개→비공개 전환 후 앱이 채널에서 빠졌을 수 있습니다. 해당 채널에 앱을 다시 초대하세요.",
    "no_service": "웹훅이 삭제되었습니다. Slack 앱 설정에서 새로 발급받아 SLACK_WEBHOOK_URL을 갱신하세요.",
    "no_active_hooks": "이 웹훅에 연결된 활성 훅이 없습니다. 웹훅이 폐기되었으니 SLACK_WEBHOOK_URL에서 제거하거나 새 웹훅으로 교체하세요.",
    "no_team": "워크스페이스에서 앱이 제거되었습니다. 앱을 다시 설치하세요.",
    "invalid_token": "웹훅 토큰이 유효하지 않습니다. 새로 발급받아 SLACK_WEBHOOK_URL을 갱신하세요.",
    "action_prohibited": "워크스페이스 관리 정책으로 이 웹훅이 차단되었습니다.",
}


def explain_slack_error(status: int, body: str) -> tuple[str, bool]:
    """(안내 문구, 영구적 실패 여부)를 반환합니다."""
    hint = SLACK_ERROR_HINTS.get(body.strip())
    if hint:
        return hint, True
    if status in (404, 410):
        return (
            "웹훅이 비활성화되었습니다. 채널 아카이브·앱 제거 등으로 폐기된 웹훅입니다. "
            "SLACK_WEBHOOK_URL에서 제거하거나 새 웹훅으로 교체하세요.",
            True,
        )
    if status == 403:
        return "권한이 없습니다. 비공개 채널로 전환된 뒤 앱이 채널에서 빠졌는지 확인하세요.", True
    if status == 429:
        return "요청이 제한되었습니다. 다음 주기에 재시도합니다.", False
    if status >= 500:
        return "Slack 측 일시적 오류입니다. 다음 주기에 재시도합니다.", False
    return "알 수 없는 오류입니다.", False


def warn(text: str) -> None:
    """GitHub Actions 실행 요약에도 보이도록 경고를 남깁니다."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "⚠️  "
    print(f"{prefix}{text}")


def send_to_slack(message: dict) -> tuple[int, int]:
    """메시지를 모든 웹훅에 전송하고 (성공 채널 수, 설정된 채널 수)를 반환합니다.

    한 채널이 실패해도 나머지 채널 전송은 계속 진행합니다.
    """
    webhook_urls = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_urls:
        print("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        print("Slack 전송을 건너뜁니다.")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return 0, 0

    urls = [u.strip() for u in webhook_urls.split(",") if u.strip()]
    delivered = 0
    dead_channels = []

    for i, url in enumerate(urls, 1):
        label = f"채널 {i} ({webhook_label(url)})"
        try:
            resp = requests.post(url, json=message, timeout=10)
        except requests.RequestException as e:
            print(f"Slack {label} 전송 실패 (네트워크 오류): {redact(str(e), url)}")
            continue

        if resp.ok:
            delivered += 1
            print(f"Slack {label} 전송 완료!")
            continue

        body = redact(resp.text.strip(), url)
        hint, permanent = explain_slack_error(resp.status_code, body)
        detail = f"Slack {label} 전송 실패: HTTP {resp.status_code} {body} — {hint}"
        if permanent:
            warn(detail)
            dead_channels.append(label)
        else:
            print(detail)

    print(f"Slack 전송 결과: {delivered}/{len(urls)} 채널 성공")

    if dead_channels:
        warn(
            f"영구적으로 실패하는 웹훅 {len(dead_channels)}개: {', '.join(dead_channels)}. "
            "SLACK_WEBHOOK_URL 시크릿에서 제거하거나 새 웹훅으로 교체하세요."
        )

    return delivered, len(urls)


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
        message["attachments"][0]["blocks"][0]["text"]["text"] = "*🧪  [테스트] 하이브레인 채용공고 알림*"
        message["attachments"][0]["color"] = "#f2c744"
        delivered, total = send_to_slack(message)
        if total and not delivered:
            print("모든 Slack 채널 전송에 실패했습니다.")
            sys.exit(1)
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
    delivered, total = send_to_slack(message)

    if total == 0:
        print("Slack 웹훅이 없어 seen_jobs.json을 갱신하지 않습니다.")
        return

    if delivered == 0:
        print("모든 Slack 채널 전송에 실패했습니다.")
        print("seen_jobs.json을 갱신하지 않고 종료합니다. (다음 주기에 재시도)")
        sys.exit(1)

    for job in new_jobs:
        seen.append(job["id"])
    save_seen(seen)

    print(f"seen_jobs.json 업데이트 완료 (총 {len(load_seen())}개)")


if __name__ == "__main__":
    main()
