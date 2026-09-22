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

KST = timezone(timedelta(hours=9))

BASE_URL = "https://hibrain.net"
LIST_URL = f"{BASE_URL}/recruitment/recruits?listType=D3NEW&pagesize=50&sortType=SORTDTM"
SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
MAX_SEEN = 500


def _env_int(name: str, default: int) -> int:
    """환경변수를 정수로 읽되, 값이 없거나 잘못되면 기본값을 쓴다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


# 네트워크 튜닝 (환경변수로 재정의 가능)
MAX_ATTEMPTS = _env_int("FETCH_MAX_ATTEMPTS", 3)      # 각 방식당 최대 시도 횟수
BACKOFF_BASE = _env_int("FETCH_BACKOFF_BASE", 3)      # 재시도 대기(초) = BACKOFF_BASE * 시도횟수
SCRAPERAPI_TIMEOUT = _env_int("SCRAPERAPI_TIMEOUT", 70)  # ScraperAPI는 프록시/렌더링으로 느릴 수 있어 넉넉히
DIRECT_TIMEOUT = _env_int("DIRECT_TIMEOUT", 30)

# Slack 메시지에 카드로 표시할 최대 공고 수 (나머지는 "외 N건"으로 요약)
MAX_CARDS = _env_int("SLACK_MAX_CARDS", 15)

# 이미지 카드에 표시할 공고 행 수
CARD_ROWS = _env_int("CARD_ROWS", 8)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def log(msg: str) -> None:
    """KST 타임스탬프가 붙은 구조적 로그."""
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


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
            log(f"{name} 시도 {attempt}/{MAX_ATTEMPTS}...")
            html = fetch_fn()
        except requests.RequestException as e:
            log(f"  {name} 실패: {type(e).__name__}: {e}")
        else:
            if _looks_like_job_list(html):
                log(f"  {name} 성공")
                return html
            log(f"  {name} 응답에 채용 목록이 없습니다. (차단/빈 응답 가능성)")

        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_BASE * attempt
            log(f"  {wait}초 후 재시도...")
            time.sleep(wait)

    log(f"{name} 최종 실패")
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
        log("ScraperAPI가 실패하여 직접 요청으로 폴백합니다.")

    html = _try_method("직접 요청", _fetch_direct)
    if html is not None:
        return html

    raise RuntimeError("모든 방식으로 페이지를 가져오지 못했습니다.")


def scrape_jobs() -> list[dict]:
    try:
        text = fetch_page()
    except Exception as e:
        log(f"페이지 가져오기 실패: {e}")
        return []

    soup = BeautifulSoup(text, "html.parser")
    article_list = soup.find("ul", id="articleList")
    if not article_list:
        log("articleList를 찾을 수 없습니다.")
        return []

    jobs = []
    seen_ids = set()
    for li in article_list.find_all("li", class_="row"):
        job = parse_job_row(li)
        if job and job["id"] not in seen_ids:
            seen_ids.add(job["id"])
            jobs.append(job)

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
    link_tag = li.find("a", href=True)
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
    m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text or "")
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
    except ValueError:
        return None


def compute_d_day(end_date: str) -> Optional[int]:
    """마감일까지 남은 일수. 오늘=0(D-DAY), 내일=1. 파싱 실패 시 None."""
    deadline = _parse_date(end_date)
    if not deadline:
        return None
    today = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return (deadline - today).days


# 색상 팔레트 (Slack attachment 좌측 색상 바)
COLOR_DEFAULT = "#0A66C2"   # 하이브레인 블루
COLOR_URGENT = "#E01E5A"    # 마감 임박(빨강)
COLOR_SOON = "#ECB22E"      # 마감 임박 직전(노랑)

# 원형 숫자 (①~⑳) — 카드 번호를 깔끔하게 표시
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _circled(n: int) -> str:
    return _CIRCLED[n - 1] if 1 <= n <= len(_CIRCLED) else f"{n}."


def d_day_status(job: dict) -> tuple[str, str]:
    """(상태 이모지, 마감 표기) 튜플을 반환한다.

    이모지는 카드 제목 앞에 붙여 한눈에 긴급도를 보여주고,
    표기는 필드에 들어갈 사람 친화적 문자열이다.
    """
    if job.get("always_open"):
        return "🟦", "상시채용"
    d = job.get("d_day")
    if d is None:
        return "⬜", "상시/미정"
    if d < 0:
        return "⬛", "마감"
    if d == 0:
        return "🟥", "오늘 마감 (D-DAY)"
    if d <= 3:
        return "🟥", f"D-{d} · 마감임박"
    if d <= 7:
        return "🟨", f"D-{d}"
    return "🟩", f"D-{d}"


def _accent_color(jobs: list[dict]) -> str:
    """가장 급한 공고 기준으로 첨부 색상을 정한다."""
    urgent = soon = False
    for j in jobs:
        if j.get("always_open"):
            continue
        d = j.get("d_day")
        if d is None or d < 0:
            continue
        if d <= 3:
            urgent = True
        elif d <= 5:
            soon = True
    if urgent:
        return COLOR_URGENT
    if soon:
        return COLOR_SOON
    return COLOR_DEFAULT


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _job_card_blocks(job: dict, index: int) -> list[dict]:
    """공고 하나를 표현하는 Block Kit 블록 목록.

    - 제목: 번호 + 상태 이모지 + 링크 (굵게)
    - 필드: 회사 / 지역 / 직종 / 마감 을 2열 정렬로 표기
    - 우측 '지원' 버튼
    """
    emoji, deadline_label = d_day_status(job)
    title = _truncate(job.get("title", "(제목 없음)"), 120)
    url = job.get("url", "")
    heading = f"{_circled(index)} {emoji}  *<{url}|{title}>*" if url else f"{_circled(index)} {emoji}  *{title}*"

    section = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": heading},
    }
    if url:
        section["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "지원", "emoji": True},
            "url": url,
            "style": "primary",
        }

    # 2열 필드 (있는 항목만) — Slack은 필드 최대 10개, 각 2000자
    fields = []
    if job.get("company"):
        fields.append(f"*🏢 기관*\n{_truncate(job['company'], 60)}")
    if job.get("region"):
        fields.append(f"*📍 지역*\n{_truncate(job['region'], 40)}")
    if job.get("job_type"):
        fields.append(f"*🗂 유형*\n{_truncate(job['job_type'], 40)}")
    if deadline_label:
        deadline_val = deadline_label
        if job.get("end_date"):
            deadline_val = f"{deadline_label}\n{job['end_date']}"
        fields.append(f"*⏳ 마감*\n{deadline_val}")

    if fields:
        section["fields"] = [{"type": "mrkdwn", "text": f} for f in fields[:10]]

    return [section]


def build_slack_message(new_jobs: list[dict], *, title: str = "") -> dict:
    now = datetime.now(KST)
    timestamp = now.strftime("%Y.%m.%d (%a) %H:%M")
    count = len(new_jobs)
    display_jobs = new_jobs[:MAX_CARDS]
    hidden = count - len(display_jobs)

    # 긴급(마감 D-3 이내) 건수 집계 — 상단 요약에 노출
    urgent_count = sum(
        1
        for j in display_jobs
        if not j.get("always_open")
        and j.get("d_day") is not None
        and 0 <= j["d_day"] <= 3
    )

    header_text = title or f"📢 하이브레인 신규 채용 {count}건"

    summary = f"🔔  새 공고 *{count}건*"
    if urgent_count:
        summary += f"   ·   🔴 마감임박 *{urgent_count}건*"
    summary += f"   ·   🕒 {timestamp} KST"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text[:150], "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": summary},
            ],
        },
        {"type": "divider"},
    ]

    for i, job in enumerate(display_jobs, start=1):
        blocks.extend(_job_card_blocks(job, i))

    if hidden > 0:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"➕  그 외 *{hidden}건*의 신규 공고가 더 있습니다."},
            ],
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📋 전체 신규 공고 보기", "emoji": True},
                "url": f"{BASE_URL}/recruitment/recruits?listType=D3NEW",
                "style": "primary",
            },
        ],
    })
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "image",
                "image_url": "https://hibrain.net/favicon.ico",
                "alt_text": "hibrain",
            },
            {"type": "mrkdwn", "text": "HiBrain Job Alert · 자동 수집 봇"},
        ],
    })

    return {
        "attachments": [
            {
                "color": _accent_color(display_jobs),
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
        log("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        log("Slack 전송을 건너뜁니다. (아래는 전송하려던 메시지 미리보기)")
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
            log(f"Slack {label} 전송 실패 (네트워크 오류): {redact(str(e), url)}")
            continue

        if resp.ok:
            delivered += 1
            log(f"Slack {label} 전송 완료!")
            continue

        body = redact(resp.text.strip(), url)
        hint, permanent = explain_slack_error(resp.status_code, body)
        detail = f"Slack {label} 전송 실패: HTTP {resp.status_code} {body} — {hint}"
        if permanent:
            warn(detail)
            dead_channels.append(label)
        else:
            log(detail)

    log(f"Slack 전송 결과: {delivered}/{len(urls)} 채널 성공")

    if dead_channels:
        warn(
            f"영구적으로 실패하는 웹훅 {len(dead_channels)}개: {', '.join(dead_channels)}. "
            "SLACK_WEBHOOK_URL 시크릿에서 제거하거나 새 웹훅으로 교체하세요."
        )

    return delivered, len(urls)


# ─────────────────────────────────────────────────────────────
# 이미지 카드 렌더링 + Slack 파일 업로드
# ─────────────────────────────────────────────────────────────

def render_cards(jobs: list[dict]) -> list[str]:
    """공고들을 개별 상품 카드(PNG)로 렌더링해 경로 목록을 반환한다.

    Playwright/Chromium이 없거나 렌더링에 실패하면 빈 리스트를 반환해
    호출 측이 텍스트(Block Kit) 방식으로 폴백할 수 있게 한다.
    """
    try:
        import card_renderer
    except Exception as e:
        log(f"카드 렌더러를 불러올 수 없습니다: {e}")
        return []

    out_dir = str(Path(__file__).parent / "cards")
    try:
        paths = card_renderer.render_cards(jobs, out_dir)
    except Exception as e:
        log(f"이미지 카드 렌더링 실패 (텍스트 방식으로 폴백): {type(e).__name__}: {e}")
        return []

    log(f"이미지 카드 {len(paths)}장 렌더링 완료")
    return paths


def _slack_api(method: str, token: str, **kwargs) -> dict:
    """Slack Web API 호출 헬퍼. 응답 JSON을 반환한다."""
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(url, headers=headers, timeout=30, **kwargs)
    try:
        return resp.json()
    except ValueError:
        return {"ok": False, "error": f"non_json_response_{resp.status_code}"}


def _upload_one_file(path: str, token: str, title: str) -> Optional[str]:
    """파일 1개를 Slack에 업로드하고 file_id를 반환. 실패 시 None."""
    try:
        size = os.path.getsize(path)
        filename = os.path.basename(path)
    except OSError as e:
        log(f"이미지 파일을 읽을 수 없습니다: {e}")
        return None

    info = _slack_api(
        "files.getUploadURLExternal", token,
        data={"filename": filename, "length": str(size)},
    )
    if not info.get("ok"):
        log(f"Slack 업로드 URL 발급 실패({filename}): {info.get('error')}")
        return None

    try:
        with open(path, "rb") as f:
            up = requests.post(info["upload_url"], files={"file": (filename, f, "image/png")}, timeout=60)
        if not up.ok:
            log(f"Slack 파일 업로드 실패({filename}): HTTP {up.status_code}")
            return None
    except requests.RequestException as e:
        log(f"Slack 파일 업로드 네트워크 오류({filename}): {e}")
        return None

    return info["file_id"]


def upload_cards_to_slack(image_paths: list[str], titles: list[str], comment: str) -> tuple[int, int]:
    """여러 카드 이미지를 한 메시지로 Slack 채널들에 게시한다(갤러리).

    최신 external-upload 흐름:
      1) 각 파일마다 files.getUploadURLExternal → 바이트 업로드
      2) files.completeUploadExternal 를 files 배열로 한 번 호출 → 갤러리로 표시

    SLACK_BOT_TOKEN + SLACK_CHANNEL_IDS 가 없으면 (0, 0)을 반환해 폴백하게 한다.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    channels = [c.strip() for c in os.environ.get("SLACK_CHANNEL_IDS", "").split(",") if c.strip()]
    if not token or not channels:
        return 0, 0
    if not image_paths:
        return 0, len(channels)

    # 1) 모든 파일 업로드 → file_id 수집
    files_meta = []
    for path, title in zip(image_paths, titles):
        file_id = _upload_one_file(path, token, title)
        if file_id:
            files_meta.append({"id": file_id, "title": title})

    if not files_meta:
        log("업로드된 카드가 없어 이미지 게시를 건너뜁니다.")
        return 0, len(channels)

    # 2) 채널별로 한 번에 게시 (files 배열 → 갤러리)
    import json as _json
    delivered = 0
    for ch in channels:
        res = _slack_api(
            "files.completeUploadExternal", token,
            data={
                "files": _json.dumps(files_meta),
                "channel_id": ch,
                "initial_comment": comment,
            },
        )
        if res.get("ok"):
            delivered += 1
            log(f"Slack 채널 {ch} 카드 {len(files_meta)}장 게시 완료!")
        else:
            log(f"Slack 채널 {ch} 카드 게시 실패: {res.get('error')}")

    log(f"Slack 이미지 전송 결과: {delivered}/{len(channels)} 채널 성공")
    return delivered, len(channels)


def deliver_jobs(jobs: list[dict], *, subtitle: str, comment: str) -> tuple[int, int]:
    """공고를 Slack에 전달한다. 상품 카드 갤러리를 우선 시도하고, 불가하면 Block Kit로 폴백.

    반환: (성공 채널 수, 대상 채널 수). 이미지/웹훅 중 실제 사용된 경로 기준.
    """
    # 봇 토큰 + 채널이 설정된 경우에만 이미지 카드 경로를 시도
    if os.environ.get("SLACK_BOT_TOKEN", "").strip() and os.environ.get("SLACK_CHANNEL_IDS", "").strip():
        card_jobs = jobs[:CARD_ROWS]
        paths = render_cards(card_jobs)
        if paths:
            titles = [j.get("title", "채용공고") for j in card_jobs]
            delivered, total = upload_cards_to_slack(paths, titles, comment=comment)
            if delivered > 0:
                return delivered, total
            log("이미지 전송이 실패하여 텍스트(Block Kit) 방식으로 폴백합니다.")

    # 폴백: 기존 Block Kit 메시지 (웹훅)
    message = build_slack_message(jobs, title=f"📢 {subtitle}")
    return send_to_slack(message)


def _deliver(message: dict) -> None:
    """Slack 전송 후 결과에 따라 프로세스 종료 코드를 정한다."""
    delivered, total = send_to_slack(message)
    if total and not delivered:
        log("모든 Slack 채널 전송에 실패했습니다.")
        sys.exit(1)


def run_test_mode(jobs: list[dict]) -> None:
    sample = jobs[:CARD_ROWS]
    log(f"[테스트 모드] 최근 {len(sample)}개 공고를 샘플로 Slack에 전송합니다.")
    delivered, total = deliver_jobs(
        sample,
        subtitle=f"[테스트] 신규 채용공고 {len(sample)}건",
        comment=f"🧪 테스트 알림 · 신규 채용공고 {len(sample)}건",
    )
    if total and not delivered:
        log("모든 Slack 채널 전송에 실패했습니다.")
        sys.exit(1)
    log(f"테스트 메시지 전송 완료 ({len(sample)}건)")


def main():
    test_mode = os.environ.get("TEST_MODE", "").lower() == "true"

    log("hibrain.net 채용정보 스크래핑 시작...")
    jobs = scrape_jobs()
    log(f"총 {len(jobs)}개 공고 발견")

    if not jobs:
        log("공고를 가져오지 못했습니다. (다음 주기에 재시도)")
        sys.exit(0)

    if test_mode:
        run_test_mode(jobs)
        return

    seen = load_seen()
    seen_set = set(seen)
    new_jobs = [j for j in jobs if j["id"] not in seen_set]
    log(f"신규 공고: {len(new_jobs)}개")

    if not new_jobs:
        log("새로운 공고가 없습니다.")
        return

    # 마감 임박 순 → 최신 순으로 정렬해 중요한 공고가 위로 오게 한다.
    def sort_key(j: dict):
        d = j.get("d_day")
        # 상시/마감/파싱불가는 뒤로, 남은 일수가 적을수록 앞으로
        if j.get("always_open") or d is None or d < 0:
            return (1, 0)
        return (0, d)

    new_jobs.sort(key=sort_key)

    delivered, total = deliver_jobs(
        new_jobs,
        subtitle=f"신규 채용공고 {len(new_jobs)}건",
        comment=f"📢 하이브레인 신규 채용공고 *{len(new_jobs)}건*이 등록되었습니다.",
    )

    if total == 0:
        log("Slack 전송 대상(웹훅/채널)이 없어 seen_jobs.json을 갱신하지 않습니다.")
        return

    if delivered == 0:
        log("모든 Slack 채널 전송에 실패했습니다.")
        log("seen_jobs.json을 갱신하지 않고 종료합니다. (다음 주기에 재시도)")
        sys.exit(1)

    for job in new_jobs:
        seen.append(job["id"])
    save_seen(seen)

    log(f"seen_jobs.json 업데이트 완료 (총 {len(load_seen())}개)")


if __name__ == "__main__":
    main()
