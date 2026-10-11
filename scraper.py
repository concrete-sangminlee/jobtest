import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
DATA_DIR = Path(__file__).parent
SEEN_FILE = DATA_DIR / "seen_jobs.json"
TRACKED_FILE = DATA_DIR / "tracked_jobs.json"
MAX_SEEN = 2000


def _env_int(name: str, default: int) -> int:
    """환경변수를 정수로 읽되, 값이 없거나 잘못되면 기본값을 쓴다."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str]:
    """콤마로 구분된 환경변수를 소문자 키워드 목록으로 읽는다."""
    raw = os.environ.get(name, "")
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


# 네트워크 튜닝 (환경변수로 재정의 가능)
MAX_ATTEMPTS = _env_int("FETCH_MAX_ATTEMPTS", 3)      # 각 방식당 최대 시도 횟수
BACKOFF_BASE = _env_int("FETCH_BACKOFF_BASE", 3)      # 재시도 대기(초) = BACKOFF_BASE * 시도횟수
SCRAPERAPI_TIMEOUT = _env_int("SCRAPERAPI_TIMEOUT", 70)  # ScraperAPI는 프록시/렌더링으로 느릴 수 있어 넉넉히
DIRECT_TIMEOUT = _env_int("DIRECT_TIMEOUT", 30)

# Slack 전송 튜닝
SLACK_TIMEOUT = _env_int("SLACK_TIMEOUT", 10)
SLACK_MAX_ATTEMPTS = _env_int("SLACK_MAX_ATTEMPTS", 3)   # 일시적 오류(429/5xx)에만 적용

# Slack 첨부 하나에 들어갈 수 있는 블록은 50개다. 머리말/구분선/꼬리말이
# 6개를 쓰므로 공고는 한 메시지에 최대 44건까지만 담을 수 있다.
SLACK_MAX_BLOCKS = 50
_MESSAGE_OVERHEAD_BLOCKS = 6
MAX_JOBS_PER_MESSAGE = SLACK_MAX_BLOCKS - _MESSAGE_OVERHEAD_BLOCKS

# 한 메시지에 담는 공고 수와 한 번에 보낼 최대 메시지 수.
# 상한을 넘긴 공고는 seen에 기록하지 않아 다음 주기에 다시 알림된다. (누락 방지)
JOBS_PER_MESSAGE = _env_int("JOBS_PER_MESSAGE", 20)
MAX_MESSAGES = _env_int("MAX_MESSAGES", 5)

# 상세 페이지 보강 (기관명/기관유형/직종/요약)
ENRICH = _env_bool("ENRICH", True)
ENRICH_WORKERS = _env_int("ENRICH_WORKERS", 6)
ENRICH_TIMEOUT = _env_int("ENRICH_TIMEOUT", 15)
ENRICH_MAX = _env_int("ENRICH_MAX", 100)   # 기본 발송 용량과 동일

# 마감 임박 리마인더: 알림할 남은 일수 목록 (예: "3,1"). 비어 있으면 기능 꺼짐.
REMINDER_DAYS = sorted({int(d) for d in _env_list("REMINDER_DAYS") if d.isdigit()}, reverse=True)
TRACK_LIMIT = _env_int("TRACK_LIMIT", 1000)

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


def warn(text: str) -> None:
    """GitHub Actions 실행 요약에도 보이도록 경고를 남긴다."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") == "true" else "⚠️  "
    print(f"{prefix}{text}", flush=True)


# --------------------------------------------------------------------------
# 상태 파일
# --------------------------------------------------------------------------

def _read_json(path: Path, expected_type, what: str):
    """JSON 상태 파일을 방어적으로 읽는다.

    파일이 깨져 있어도 실행을 중단시키지 않는다. 손상된 파일을 덮어쓰기 전에
    경고를 남겨, 알림이 중복되더라도 원인을 추적할 수 있게 한다.
    """
    if not path.exists():
        return expected_type()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        warn(f"{what}를 읽을 수 없습니다 ({type(e).__name__}: {e}). 빈 상태로 진행합니다.")
        return expected_type()
    if not isinstance(data, expected_type):
        warn(f"{what}의 형식이 예상과 다릅니다 ({type(data).__name__}). 빈 상태로 진행합니다.")
        return expected_type()
    return data


def load_seen() -> list[str]:
    """이미 알린 공고 ID 목록."""
    return [str(x) for x in _read_json(SEEN_FILE, list, "seen_jobs.json")]


def save_seen(seen: list[str]) -> None:
    seen = seen[-MAX_SEEN:]
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tracked() -> dict:
    """마감 리마인더용 추적 상태. {job_id: {...}}"""
    return _read_json(TRACKED_FILE, dict, "tracked_jobs.json")


def save_tracked(tracked: dict) -> None:
    TRACKED_FILE.write_text(json.dumps(tracked, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 날짜
# --------------------------------------------------------------------------

def _parse_date(text: str) -> Optional[datetime]:
    """날짜 표기를 KST 자정 기준 datetime으로 파싱한다.

    하이브레인 목록은 두 자리 연도(`26.10.23`)를, JSON-LD는 네 자리
    연도(`2026-10-07`)를 쓴다. 두 형태를 모두 받는다. 두 자리 연도는
    2000년대로 해석한다.
    """
    m = re.search(r"(\d{4}|\d{2})\D+(\d{1,2})\D+(\d{1,2})", text or "")
    if not m:
        return None
    year = int(m.group(1))
    if year < 100:
        year += 2000
    try:
        return datetime(year, int(m.group(2)), int(m.group(3)), tzinfo=KST)
    except ValueError:
        return None


def _today_kst() -> datetime:
    return datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)


def compute_d_day(end_date: str) -> Optional[int]:
    """마감일까지 남은 일수. 오늘=0(D-DAY), 내일=1. 파싱 실패 시 None."""
    deadline = _parse_date(end_date)
    if not deadline:
        return None
    return (deadline - _today_kst()).days


def d_day_label(job: dict) -> str:
    """공고의 마감 상태를 짧은 배지 문자열로."""
    if job.get("always_open"):
        return "상시"
    d = job.get("d_day")
    if d is None:
        return ""
    if d < 0:
        return "마감"
    if d == 0:
        return "D-DAY"
    return f"D-{d}"


# --------------------------------------------------------------------------
# 목록 수집
# --------------------------------------------------------------------------

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


def _clean(text: str) -> str:
    """공백/개행을 한 칸으로 정리."""
    return re.sub(r"\s+", " ", text or "").strip()


def parse_job_row(li) -> Optional[dict]:
    """채용 목록의 <li.row> 하나를 구조화된 dict로 변환한다.

    목록에는 제목과 접수 기간만 있다. 기관명·직종 등은 상세 페이지의
    JSON-LD에서 따로 보강한다. (enrich_jobs)
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

    return {
        "id": job_id,
        "title": title,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "always_open": always_open,
        "d_day": compute_d_day(end_date) if end_date else None,
        # 보강 필드 (상세 페이지에서 채워짐)
        "org": "",
        "industry": "",
        "employment_type": "",
        "summary": "",
        "url": f"{BASE_URL}/recruitment/recruits/{job_id}",
    }


def parse_job_list(html: str) -> list[dict]:
    """목록 HTML에서 공고를 파싱한다. 중복 ID는 처음 것만 남긴다."""
    soup = BeautifulSoup(html, "html.parser")
    article_list = soup.find("ul", id="articleList")
    if not article_list:
        log("articleList를 찾을 수 없습니다.")
        return []

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for li in article_list.find_all("li", class_="row"):
        job = parse_job_row(li)
        if job and job["id"] not in seen_ids:
            seen_ids.add(job["id"])
            jobs.append(job)
    return jobs


def scrape_jobs() -> list[dict]:
    try:
        text = fetch_page()
    except Exception as e:
        log(f"페이지 가져오기 실패: {e}")
        return []
    return parse_job_list(text)


# --------------------------------------------------------------------------
# 상세 보강 (JSON-LD)
# --------------------------------------------------------------------------

def parse_job_detail(html: str) -> dict:
    """상세 페이지에서 기관명·기관유형·직종·요약을 뽑는다.

    CSS 클래스 대신 schema.org JSON-LD(JobPosting)와 og:description을 쓴다.
    이 둘은 SEO용이라 사이트 개편에도 비교적 안정적이다.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = {"org": "", "industry": "", "employment_type": "", "summary": "", "posted": ""}

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        if isinstance(data, list):
            data = next((d for d in data if isinstance(d, dict)
                         and d.get("@type") == "JobPosting"), None)
        if not isinstance(data, dict) or data.get("@type") != "JobPosting":
            continue

        org = data.get("hiringOrganization")
        if isinstance(org, dict):
            out["org"] = _clean(org.get("name", ""))
        out["industry"] = _clean(data.get("industry", ""))
        out["posted"] = _clean(data.get("datePosted", ""))

        # 사이트가 직종을 모르면 '공고문참조' 같은 자리표시자를 넣는다. 노이즈라 버린다.
        emp = _clean(data.get("employmentType", ""))
        if emp and emp not in ("공고문참조", "공고참조", "기타"):
            out["employment_type"] = emp
        break

    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        out["summary"] = _clean(og["content"])

    return out


def fetch_job_detail(job_id: str) -> dict:
    """한 공고의 상세 정보를 가져온다. 실패하면 빈 dict."""
    try:
        resp = requests.get(
            f"{BASE_URL}/recruitment/recruits/{job_id}",
            headers=BROWSER_HEADERS,
            timeout=ENRICH_TIMEOUT,
        )
        resp.raise_for_status()
        return parse_job_detail(resp.text)
    except (requests.RequestException, ValueError) as e:
        log(f"  상세 보강 실패 (id={job_id}): {type(e).__name__}")
        return {}


def enrich_jobs(jobs: list[dict], fetcher=fetch_job_detail) -> int:
    """알림할 공고에 상세 정보를 덧붙인다. 보강 성공 건수를 반환한다.

    베스트 에포트다. 네트워크가 흔들려도 공고 자체는 제목·기간만으로
    정상 발송된다.
    """
    if not ENRICH or not jobs:
        return 0

    targets = jobs[:ENRICH_MAX]
    if len(jobs) > len(targets):
        log(f"상세 보강은 상위 {ENRICH_MAX}건만 수행합니다. (전체 {len(jobs)}건)")

    log(f"상세 정보 보강 중... ({len(targets)}건, 동시 {ENRICH_WORKERS})")
    workers = max(1, min(ENRICH_WORKERS, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        details = list(pool.map(lambda j: fetcher(j["id"]), targets))

    enriched = 0
    for job, detail in zip(targets, details):
        if not detail:
            continue
        for key in ("org", "industry", "employment_type", "summary"):
            if detail.get(key):
                job[key] = detail[key]
        if detail.get("org") or detail.get("industry"):
            enriched += 1

    log(f"상세 보강 완료: {enriched}/{len(targets)}건")
    return enriched


# --------------------------------------------------------------------------
# 선별·정렬
# --------------------------------------------------------------------------

def matches_filters(job: dict, include: list[str], exclude: list[str]) -> bool:
    """키워드 필터 통과 여부. include가 비면 전부 통과, exclude는 항상 적용."""
    haystack = " ".join(
        str(job.get(k, "")) for k in ("title", "org", "industry", "employment_type")
    ).lower()
    if any(k in haystack for k in exclude):
        return False
    if include and not any(k in haystack for k in include):
        return False
    return True


def sort_key(job: dict):
    """마감 임박 순 → 그 외는 뒤로. 동률은 ID 내림차순(최신 먼저)으로 안정화."""
    d = job.get("d_day")
    newest = -int(job["id"]) if job.get("id", "").isdigit() else 0
    if job.get("always_open") or d is None or d < 0:
        return (1, 0, newest)
    return (0, d, newest)


# --------------------------------------------------------------------------
# Slack 메시지
# --------------------------------------------------------------------------

def _job_block(job: dict) -> dict:
    """공고 한 건을 Slack 섹션 블록으로. (기존 포맷 유지 + 정보 보강)"""
    lines = [f"> *<{job['url']}|{job['title']}>*"]

    badge = d_day_label(job)
    if job.get("period") and badge:
        lines.append(f"> 📅 `{job['period']}`  ｜  `{badge}`")
    elif job.get("period"):
        lines.append(f"> 📅 `{job['period']}`")
    elif badge:
        lines.append(f"> 📅 `{badge}`")

    meta = " · ".join(x for x in (job.get("org"), job.get("industry"),
                                  job.get("employment_type")) if x)
    if meta:
        lines.append(f"> 🏢 {meta}")

    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "지원하기", "emoji": True},
            "url": job["url"],
            "style": "primary",
        },
    }


def build_slack_message(
    jobs: list[dict],
    *,
    title: str = "",
    total: Optional[int] = None,
    part: int = 1,
    parts: int = 1,
    color: str = "#0054a6",
) -> dict:
    """공고 목록을 Slack 메시지로 만든다.

    total은 이번 주기의 전체 신규 건수(분할 전송 시 표시용)이며,
    part/parts는 분할 번호다.
    """
    now = datetime.now(KST)
    timestamp = now.strftime("%Y. %m. %d  %H:%M KST")
    shown = len(jobs)
    total = shown if total is None else total

    header_line = title or "*📢  하이브레인 신규 채용공고*"
    if parts > 1:
        header_line += f"  _({part}/{parts})_"

    summary = f"🏢 하이브레인  ｜  🔔 *{total}건*의 새로운 채용공고"
    if parts > 1:
        summary += f"  ｜  이 메시지 {shown}건"

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_line}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": summary}]},
        {"type": "divider"},
    ]

    blocks.extend(_job_block(job) for job in jobs)

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
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"🤖 HiBrain Job Alert  ｜  {timestamp}"}],
    })

    return {"attachments": [{"color": color, "blocks": blocks}]}


# --------------------------------------------------------------------------
# Slack 전송
# --------------------------------------------------------------------------

def webhook_label(url: str) -> str:
    """웹훅 URL에서 토큰을 제거한 식별용 라벨. (로그에 시크릿이 남지 않도록)"""
    parts = url.rstrip("/").split("/")
    if len(parts) >= 2:
        return f".../{parts[-2]}/***"
    return "***"


def redact(text: str, url: str) -> str:
    """예외/응답 메시지에 섞여 있을 수 있는 웹훅 토큰을 가린다."""
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
    "invalid_blocks": "메시지 블록 구조가 잘못되었습니다. 코드 쪽 버그이니 로그의 미리보기를 확인하세요.",
    "invalid_payload": "메시지 본문이 잘못되었습니다. 코드 쪽 버그이니 로그의 미리보기를 확인하세요.",
    "msg_too_long": "메시지가 너무 깁니다. JOBS_PER_MESSAGE를 줄이세요.",
}


def explain_slack_error(status: int, body: str) -> tuple[str, bool]:
    """(안내 문구, 영구적 실패 여부)를 반환한다."""
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
        return "요청이 제한되었습니다.", False
    if status >= 500:
        return "Slack 측 일시적 오류입니다.", False
    return "알 수 없는 오류입니다.", False


def _retry_after(resp) -> Optional[int]:
    try:
        value = int(resp.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return max(0, min(value, 60))


def _post_one(url: str, message: dict, label: str) -> tuple[bool, bool]:
    """웹훅 하나에 전송한다. (성공 여부, 영구적 실패 여부)를 반환한다.

    429/5xx 같은 일시적 오류는 백오프 후 재시도한다. 웹훅이 폐기된 경우처럼
    영구적인 실패는 즉시 포기한다.
    """
    for attempt in range(1, SLACK_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=message, timeout=SLACK_TIMEOUT)
        except requests.RequestException as e:
            log(f"Slack {label} 전송 실패 (네트워크 오류): {redact(str(e), url)}")
            if attempt == SLACK_MAX_ATTEMPTS:
                return False, False
            time.sleep(BACKOFF_BASE * attempt)
            continue

        if resp.ok:
            log(f"Slack {label} 전송 완료!")
            return True, False

        body = redact(resp.text.strip(), url)
        hint, permanent = explain_slack_error(resp.status_code, body)
        detail = f"Slack {label} 전송 실패: HTTP {resp.status_code} {body} — {hint}"

        if permanent:
            warn(detail)
            return False, True

        log(detail)
        if attempt == SLACK_MAX_ATTEMPTS:
            return False, False
        wait = _retry_after(resp) or BACKOFF_BASE * attempt
        log(f"  {wait}초 후 재시도... ({attempt + 1}/{SLACK_MAX_ATTEMPTS})")
        time.sleep(wait)

    return False, False


def configured_webhooks() -> list[str]:
    """SLACK_WEBHOOK_URL에 설정된 웹훅 목록. 비어 있으면 드라이런 모드다."""
    raw = os.environ.get("SLACK_WEBHOOK_URL", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


def send_to_slack(message: dict) -> tuple[int, int]:
    """메시지를 모든 웹훅에 전송하고 (성공 채널 수, 설정된 채널 수)를 반환한다.

    한 채널이 실패해도 나머지 채널 전송은 계속 진행한다.
    """
    urls = configured_webhooks()
    if not urls:
        log("SLACK_WEBHOOK_URL 환경변수가 설정되지 않았습니다.")
        log("Slack 전송을 건너뜁니다. (아래는 전송하려던 메시지 미리보기)")
        print(json.dumps(message, ensure_ascii=False, indent=2))
        return 0, 0

    delivered = 0
    dead_channels = []

    for i, url in enumerate(urls, 1):
        label = f"채널 {i} ({webhook_label(url)})"
        ok, permanent = _post_one(url, message, label)
        if ok:
            delivered += 1
        elif permanent:
            dead_channels.append(label)

    log(f"Slack 전송 결과: {delivered}/{len(urls)} 채널 성공")

    if dead_channels:
        warn(
            f"영구적으로 실패하는 웹훅 {len(dead_channels)}개: {', '.join(dead_channels)}. "
            "SLACK_WEBHOOK_URL 시크릿에서 제거하거나 새 웹훅으로 교체하세요."
        )

    return delivered, len(urls)


def chunk(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def send_jobs(jobs: list[dict], *, title: str = "", color: str = "#0054a6") -> list[dict]:
    """공고를 여러 메시지로 나눠 보내고, 실제로 전달된 공고만 반환한다.

    MAX_MESSAGES를 넘기는 공고는 아예 보내지 않고 반환값에서도 제외한다.
    호출자가 이를 seen으로 기록하지 않으므로 다음 주기에 다시 알림된다.
    """
    # Slack이 거부하지 않도록 블록 상한에 맞춰 조인다. 넘친 공고는 버리지 않고
    # 다음 묶음으로 넘어간다.
    per_message = max(1, min(JOBS_PER_MESSAGE, MAX_JOBS_PER_MESSAGE))
    batches = chunk(jobs, per_message)
    deferred: list[dict] = []
    if len(batches) > MAX_MESSAGES:
        deferred = [j for batch in batches[MAX_MESSAGES:] for j in batch]
        batches = batches[:MAX_MESSAGES]
        warn(
            f"신규 공고가 많아 {len(deferred)}건은 이번에 보내지 않았습니다. "
            "다음 주기에 이어서 알림됩니다."
        )

    planned = sum(len(b) for b in batches)
    delivered_jobs: list[dict] = []
    configured = None

    for idx, batch in enumerate(batches, 1):
        message = build_slack_message(
            batch, title=title, total=planned, part=idx, parts=len(batches), color=color
        )
        delivered, total = send_to_slack(message)
        configured = total if configured is None else configured
        if delivered:
            delivered_jobs.extend(batch)
        else:
            log(f"{idx}번째 메시지 전송 실패 — 해당 {len(batch)}건은 다음 주기에 재시도합니다.")

    if configured == 0:
        log("Slack 웹훅이 설정되지 않아 전달된 공고가 없습니다.")
        return []

    return delivered_jobs


# --------------------------------------------------------------------------
# 마감 임박 리마인더
# --------------------------------------------------------------------------

def prune_tracked(tracked: dict) -> dict:
    """마감이 지난 공고와 오래된 항목을 추적 상태에서 지운다."""
    alive = {}
    for job_id, info in tracked.items():
        if not isinstance(info, dict):
            continue
        d = compute_d_day(info.get("end_date", ""))
        if d is None or d < 0:
            continue
        alive[job_id] = info
    if len(alive) > TRACK_LIMIT:
        # 마감이 먼 것부터 버린다.
        ordered = sorted(alive.items(), key=lambda kv: compute_d_day(kv[1].get("end_date", "")) or 0)
        alive = dict(ordered[:TRACK_LIMIT])
    return alive


def track_jobs(tracked: dict, jobs: list[dict]) -> dict:
    """알림 완료된 공고를 마감 리마인더 대상으로 등록한다.

    등록 시점에 이미 지나간 기준은 '보냄'으로 미리 표시한다. 그러지 않으면
    D-2 공고가 신규 알림과 마감 임박 알림에 같은 주기에 두 번 등장한다.
    """
    for job in jobs:
        if job.get("always_open") or job.get("d_day") is None or job["d_day"] < 0:
            continue
        already = [day for day in REMINDER_DAYS if job["d_day"] <= day]
        tracked.setdefault(job["id"], {
            "title": job["title"],
            "url": job["url"],
            "end_date": job["end_date"],
            "period": job.get("period", ""),
            "org": job.get("org", ""),
            "industry": job.get("industry", ""),
            "employment_type": job.get("employment_type", ""),
            "reminded": already,
        })
    return tracked


def due_reminders(tracked: dict) -> tuple[list[dict], dict]:
    """오늘 리마인더를 보낼 공고 목록과, 보냄 표시를 반영한 상태를 반환한다."""
    due: list[dict] = []
    for job_id, info in tracked.items():
        d = compute_d_day(info.get("end_date", ""))
        if d is None:
            continue
        reminded = info.get("reminded") or []
        # 알림을 놓친 경우(실행 실패 등)에도 한 번은 보내도록 '이하' 비교를 쓴다.
        hit = next((day for day in REMINDER_DAYS if d <= day and day not in reminded), None)
        if hit is None:
            continue
        due.append({
            "id": job_id,
            "title": info.get("title", ""),
            "url": info.get("url", ""),
            "period": info.get("period", ""),
            "end_date": info.get("end_date", ""),
            "org": info.get("org", ""),
            "industry": info.get("industry", ""),
            "employment_type": info.get("employment_type", ""),
            "d_day": d,
            "always_open": False,
        })
        info["reminded"] = reminded + [hit]
    due.sort(key=sort_key)
    return due, tracked


def run_reminders(tracked: dict) -> dict:
    """마감 임박 리마인더를 보낸다. REMINDER_DAYS가 비어 있으면 아무것도 하지 않는다."""
    if not REMINDER_DAYS:
        return tracked

    tracked = prune_tracked(tracked)
    due, tracked = due_reminders(tracked)
    if not due:
        log(f"마감 임박 리마인더 대상 없음 (기준 D-{REMINDER_DAYS})")
        return tracked

    log(f"마감 임박 리마인더 {len(due)}건 전송 (기준 D-{REMINDER_DAYS})")
    sent = send_jobs(due, title=f"*⏰  마감 임박 채용공고 {len(due)}건*", color="#e8912d")
    sent_ids = {j["id"] for j in sent}

    # 보내지 못한 건은 '보냄' 표시를 되돌려 다음 주기에 재시도한다.
    for job in due:
        if job["id"] not in sent_ids and job["id"] in tracked:
            reminded = tracked[job["id"]].get("reminded") or []
            if reminded:
                tracked[job["id"]]["reminded"] = reminded[:-1]

    return tracked


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------

def run_test_mode(jobs: list[dict]) -> None:
    sample = sorted(jobs, key=sort_key)[:JOBS_PER_MESSAGE]
    log(f"[테스트 모드] 공고 {len(sample)}건을 샘플로 Slack에 전송합니다.")
    enrich_jobs(sample)
    sent = send_jobs(sample, title=f"🧪 [테스트] 하이브레인 신규 채용 {len(sample)}건")
    if not sent:
        if not configured_webhooks():
            log("웹훅이 설정되지 않아 미리보기만 출력했습니다. (드라이런)")
            return
        log("테스트 메시지를 전송하지 못했습니다.")
        sys.exit(1)
    log(f"테스트 메시지 전송 완료 ({len(sent)}건)")


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

    tracked = load_tracked()

    if new_jobs:
        # 마감 임박 순으로 정렬해 중요한 공고가 위로 오게 한다.
        new_jobs.sort(key=sort_key)
        enrich_jobs(new_jobs)

        include, exclude = _env_list("JOB_INCLUDE_KEYWORDS"), _env_list("JOB_EXCLUDE_KEYWORDS")
        if include or exclude:
            kept = [j for j in new_jobs if matches_filters(j, include, exclude)]
            kept_ids = {j["id"] for j in kept}
            filtered_out = [j for j in new_jobs if j["id"] not in kept_ids]
            log(f"키워드 필터 적용: {len(kept)}/{len(new_jobs)}건 통과")
            new_jobs = kept
        else:
            filtered_out = []

        delivered_jobs = send_jobs(new_jobs) if new_jobs else []

        if new_jobs and not delivered_jobs:
            if not configured_webhooks():
                # 로컬 드라이런. 알린 적이 없으니 seen도 갱신하지 않는다.
                log("Slack 웹훅이 없어 seen_jobs.json을 갱신하지 않습니다. (드라이런)")
                return
            log("신규 공고를 어떤 채널에도 전달하지 못했습니다.")
            log("seen_jobs.json을 갱신하지 않고 종료합니다. (다음 주기에 재시도)")
            sys.exit(1)

        # 실제로 전달한 공고 + 필터로 걸러낸 공고만 seen에 기록한다.
        # 전송하지 못한 공고는 기록하지 않아 다음 주기에 다시 시도된다.
        recorded = delivered_jobs + filtered_out
        if recorded:
            seen.extend(job["id"] for job in recorded)
            save_seen(seen)
            log(f"seen_jobs.json 업데이트 완료 (전달 {len(delivered_jobs)}건, "
                f"필터 제외 {len(filtered_out)}건, 총 {len(load_seen())}개)")

        if REMINDER_DAYS:
            tracked = track_jobs(tracked, delivered_jobs)
    else:
        log("새로운 공고가 없습니다.")

    tracked = run_reminders(tracked)
    if REMINDER_DAYS or TRACKED_FILE.exists():
        save_tracked(prune_tracked(tracked))


if __name__ == "__main__":
    main()
