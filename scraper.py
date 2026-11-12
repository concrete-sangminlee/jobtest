import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from html import escape as html_escape, unescape
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
OUTBOX_FILE = DATA_DIR / "outbox.json"     # 채널별 미전송 대기열
HEALTH_FILE = DATA_DIR / "health.json"     # 수집 연속 실패 기록 (무음 장애 방지)
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
# 사이트 robots.txt가 봇을 금지하므로 상세 요청은 적게, 천천히 한다.
ENRICH_WORKERS = _env_int("ENRICH_WORKERS", 3)
ENRICH_TIMEOUT = _env_int("ENRICH_TIMEOUT", 15)
ENRICH_MAX = _env_int("ENRICH_MAX", 30)

# 발송 대기열: 채널이 오래 죽어 있어도 대기열이 무한히 커지지 않게 한다.
OUTBOX_TTL_DAYS = _env_int("OUTBOX_TTL_DAYS", 3)
OUTBOX_MAX_PENDING = _env_int("OUTBOX_MAX_PENDING", 500)

# 수집이 연속으로 실패하면 Slack에 경보를 보낸다. 이후에는 하루 한 번만 다시 알린다.
HEALTH_ALERT_AFTER = _env_int("HEALTH_ALERT_AFTER", 2)
HEALTH_REALERT_HOURS = _env_int("HEALTH_REALERT_HOURS", 24)

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


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _gh_escape(text: str) -> str:
    """워크플로 명령 데이터 이스케이프. 줄바꿈이 있으면 명령이 잘린다."""
    return str(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def warn(text: str) -> None:
    """GitHub Actions 실행 요약에도 보이도록 경고를 남긴다."""
    if _in_actions():
        print(f"::warning::{_gh_escape(text)}", flush=True)
    else:
        print(f"⚠️  {text}", flush=True)


def error(text: str, title: str = "") -> None:
    """실행을 실패로 만드는 원인. Actions 요약 맨 위에 빨간 줄로 보인다."""
    if _in_actions():
        head = f"::error title={_gh_escape(title).replace(',', '%2C').replace(':', '%3A')}::" if title else "::error::"
        print(f"{head}{_gh_escape(text)}", flush=True)
    else:
        print(f"❌ {title + ': ' if title else ''}{text}", flush=True)


# --------------------------------------------------------------------------
# 상태 파일
# --------------------------------------------------------------------------

class StateError(RuntimeError):
    """seen_jobs.json을 믿을 수 없을 때. 대량 재알림 대신 실행을 멈춘다."""


# 실행 중 생긴 (치명적이지 않은) 상태 파일 문제. 실행 끝에 실패로 보고된다.
STATE_PROBLEMS: list[str] = []


def atomic_write_json(path: Path, data) -> None:
    """임시 파일에 쓰고 교체한다. 중간에 죽어도 반쯤 쓴 파일이 남지 않는다."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path: Path, expected_type, what: str):
    """보조 상태 파일을 읽는다. 깨져 있으면 빈 상태로 진행하되 실행은 실패로 보고한다.

    (seen_jobs.json은 다르게 다룬다 — load_seen 참고)
    """
    if not path.exists():
        return expected_type()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problem = f"{what}를 읽을 수 없어 빈 상태로 진행합니다 ({type(e).__name__})."
    else:
        if isinstance(data, expected_type):
            return data
        problem = f"{what}의 형식이 예상과 달라 빈 상태로 진행합니다 ({type(data).__name__})."
    warn(problem)
    STATE_PROBLEMS.append(problem)
    return expected_type()


def load_seen() -> list[str]:
    """이미 알린 공고 ID 목록.

    파일이 깨졌을 때 빈 목록으로 진행하면 목록의 모든 공고가 '신규'가 되어
    채널에 수백 건이 쏟아진다. 그래서 여기서는 실패를 숨기지 않고 실행을 멈춘다.
    """
    if not SEEN_FILE.exists():
        return []
    try:
        data = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StateError(f"seen_jobs.json을 읽을 수 없습니다 ({type(e).__name__}).") from None
    if not isinstance(data, list):
        raise StateError(f"seen_jobs.json의 형식이 예상과 다릅니다 ({type(data).__name__}).")
    return [str(x) for x in data]


def save_seen(seen: list[str]) -> None:
    atomic_write_json(SEEN_FILE, seen[-MAX_SEEN:])


def load_tracked() -> dict:
    """마감 리마인더용 추적 상태. {job_id: {...}}"""
    return _read_json(TRACKED_FILE, dict, "tracked_jobs.json")


def save_tracked(tracked: dict) -> None:
    atomic_write_json(TRACKED_FILE, tracked)


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


class FetchError(RuntimeError):
    """목록을 가져오지 못했다. 메시지는 운영자가 바로 이해할 원인 요약이다."""


# 같은 실행 안에서 다시 시도해도 결과가 같은 응답. 재시도는 요청만 낭비한다.
_PERMANENT_HINTS = {
    "ScraperAPI": {
        401: "ScraperAPI 키가 거부됨(401) — 키가 무효이거나 계정이 해지/만료됨. SCRAPER_API_KEY를 갱신하세요.",
        403: "ScraperAPI 크레딧 소진 또는 요금제 제한(403). ScraperAPI 대시보드를 확인하세요.",
    },
    "직접 요청": {
        403: "사이트가 이 실행 환경(GitHub Actions)의 접속을 거부함(403).",
        401: "사이트가 인증을 요구함(401).",
    },
}


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


def _hide_key(text: str) -> str:
    """예외 문자열에 섞인 ScraperAPI 키를 가린다. (URL 쿼리에 들어간다)"""
    return re.sub(r"(api_key=)[^&\s]+", r"\1***", text)


def _try_method(name: str, fetch_fn) -> tuple[Optional[str], str]:
    """한 가지 방식(ScraperAPI 또는 직접)을 재시도와 함께 실행한다.

    (HTML, 실패 사유)를 반환한다. 성공하면 사유는 빈 문자열이다.
    401/403처럼 다시 해도 같은 응답은 재시도하지 않는다.
    """
    reason = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(f"{name} 시도 {attempt}/{MAX_ATTEMPTS}...")
            html = fetch_fn()
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            hint = _PERMANENT_HINTS.get(name, {}).get(status)
            log(f"  {name} 실패: HTTP {status}")
            if hint:
                log(f"  {hint} (재시도하지 않습니다)")
                return None, hint
            reason = f"{name} HTTP {status}"
        except requests.RequestException as e:
            log(f"  {name} 실패: {type(e).__name__}: {_hide_key(str(e))}")
            reason = f"{name} 네트워크 오류({type(e).__name__})"
        else:
            if _looks_like_job_list(html):
                log(f"  {name} 성공")
                return html, ""
            log(f"  {name} 응답에 채용 목록이 없습니다. (차단/빈 응답 가능성)")
            reason = f"{name} 응답에 채용 목록 없음"

        if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_BASE * attempt
            log(f"  {wait}초 후 재시도...")
            time.sleep(wait)

    log(f"{name} 최종 실패")
    return None, reason


def fetch_page() -> str:
    """채용 목록 HTML을 가져온다.

    ScraperAPI 키가 있으면 먼저 시도하고, 실패하면 직접 요청으로 폴백한다.
    모두 실패하면 각 방식의 실패 사유를 담은 FetchError를 던진다.
    """
    reasons = []
    scraper_api_key = os.environ.get("SCRAPER_API_KEY", "").strip()

    if scraper_api_key:
        html, reason = _try_method("ScraperAPI", lambda: _fetch_via_scraperapi(scraper_api_key))
        if html is not None:
            return html
        reasons.append(reason)
        log("ScraperAPI가 실패하여 직접 요청으로 폴백합니다.")

    html, reason = _try_method("직접 요청", _fetch_direct)
    if html is not None:
        return html
    reasons.append(reason)

    raise FetchError(" / ".join(r for r in reasons if r) or "모든 방식으로 페이지를 가져오지 못했습니다.")


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
    """목록을 가져와 파싱한다. 실패하면 FetchError — 빈 목록으로 숨기지 않는다.

    목록 페이지는 왔는데 공고가 0건이면 사이트 마크업이 바뀌었을 가능성이 크다.
    (최근 3일 신규 목록이 비는 일은 사실상 없다) 이것도 실패로 본다.
    """
    jobs = parse_job_list(fetch_page())
    if not jobs:
        raise FetchError("목록 페이지는 받았지만 공고를 하나도 파싱하지 못했습니다 (사이트 구조 변경 가능성).")
    return jobs


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


# 상세 요청이 차단/제한되면 fetch_job_detail이 이 값을 돌려준다. 나머지 요청은 보내지 않는다.
BLOCKED = {"_blocked": True}


def fetch_job_detail(job_id: str) -> dict:
    """한 공고의 상세 정보를 가져온다. 실패하면 빈 dict, 차단/제한이면 BLOCKED."""
    try:
        resp = requests.get(
            f"{BASE_URL}/recruitment/recruits/{job_id}",
            headers=BROWSER_HEADERS,
            timeout=ENRICH_TIMEOUT,
        )
        if resp.status_code in (401, 403, 429):
            log(f"  상세 보강 차단됨 (id={job_id}): HTTP {resp.status_code}")
            return BLOCKED
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

    # 첫 요청으로 차단 여부를 먼저 확인한다. 막혀 있으면 나머지를 보내지 않는다.
    first = fetcher(targets[0]["id"])
    if first is BLOCKED or first.get("_blocked"):
        warn(f"상세 페이지 요청이 차단되어 보강을 건너뜁니다. ({len(targets)}건은 제목·기간만으로 발송)")
        return 0

    rest = targets[1:]
    workers = max(1, min(ENRICH_WORKERS, len(rest) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        details = [first] + list(pool.map(lambda j: fetcher(j["id"]), rest))

    enriched = 0
    for job, detail in zip(targets, details):
        if not detail or detail.get("_blocked"):
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

def _cut(text: str, limit: int) -> str:
    text = _clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def mrk(text: str, limit: int = 200) -> str:
    """사이트에서 온 문자열을 Slack mrkdwn에 안전하게 넣는다.

    &, <, > 는 Slack의 제어 문자라 그대로 두면 링크가 깨지거나 `<!channel>` 같은
    멘션이 된다. 먼저 길이를 자르고(섹션 3000자 제한) 그 다음 이스케이프한다.
    """
    return html_escape(_cut(text, limit), quote=False)


def _link_label(text: str) -> str:
    # 링크 문법 <url|label> 안에서는 | 도 구분자다.
    return mrk(text, 300).replace("|", "｜")


def _code(text: str, limit: int = 80) -> str:
    # 코드 스팬 안에서는 백틱이 스팬을 닫는다.
    return mrk(text, limit).replace("`", "'")


def _job_block(job: dict) -> dict:
    """공고 한 건을 Slack 섹션 블록으로. (기존 포맷 유지 + 정보 보강)"""
    lines = [f"> *<{job['url']}|{_link_label(job['title'])}>*"]

    badge = d_day_label(job)
    period = _code(job.get("period", ""))
    if period and badge:
        lines.append(f"> 📅 `{period}`  ｜  `{badge}`")
    elif period:
        lines.append(f"> 📅 `{period}`")
    elif badge:
        lines.append(f"> 📅 `{badge}`")

    meta = " · ".join(mrk(x, 100) for x in (job.get("org"), job.get("industry"),
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

    # 첨부만 있으면 푸시 알림·검색에 내용이 안 보인다. fallback은 화면에는 그려지지 않는다.
    plain_title = re.sub(r"[*_~`]", "", title).strip() or "하이브레인 신규 채용공고"
    fallback = f"{plain_title} {shown}건" if parts == 1 else f"{plain_title} {shown}건 ({part}/{parts})"
    return {"attachments": [{"color": color, "fallback": fallback, "blocks": blocks}]}


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


# 채널(웹훅)이 쓸 수 없게 된 경우의 안내. 운영자가 시크릿을 고쳐야 한다.
SLACK_ERROR_HINTS = {
    "channel_is_archived": "채널이 아카이브되었습니다. 채널을 복구하거나 SLACK_WEBHOOK_URL에서 이 웹훅을 제거하세요.",
    "channel_not_found": "채널을 찾을 수 없습니다. 공개→비공개 전환 후 앱이 채널에서 빠졌을 수 있습니다. 해당 채널에 앱을 다시 초대하세요.",
    "no_service": "웹훅이 삭제되었습니다. Slack 앱 설정에서 새로 발급받아 SLACK_WEBHOOK_URL을 갱신하세요.",
    "no_active_hooks": "이 웹훅에 연결된 활성 훅이 없습니다. 웹훅이 폐기되었으니 SLACK_WEBHOOK_URL에서 제거하거나 새 웹훅으로 교체하세요.",
    "no_team": "워크스페이스에서 앱이 제거되었습니다. 앱을 다시 설치하세요.",
    "invalid_token": "웹훅 토큰이 유효하지 않습니다. 새로 발급받아 SLACK_WEBHOOK_URL을 갱신하세요.",
    "action_prohibited": "워크스페이스 관리 정책으로 이 웹훅이 차단되었습니다.",
}

# 메시지 내용 쪽 문제. 웹훅은 멀쩡하다 — 웹훅을 지우라고 안내하면 안 된다.
PAYLOAD_ERRORS = {
    "invalid_blocks": "메시지 블록 구조를 Slack이 거부했습니다. 코드 쪽 문제입니다.",
    "invalid_payload": "메시지 본문을 Slack이 거부했습니다. 코드 쪽 문제입니다.",
    "msg_too_long": "메시지가 너무 깁니다. JOBS_PER_MESSAGE를 줄이세요.",
    "invalid_attachments": "첨부 구조를 Slack이 거부했습니다. 코드 쪽 문제입니다.",
    "too_many_attachments": "첨부가 너무 많습니다. 코드 쪽 문제입니다.",
}

# 이 시간보다 오래 기다리라는 Retry-After는 다음 실행으로 미룬다.
SLACK_MAX_WAIT = 60


def classify_slack_response(status: int, body: str) -> str:
    """'ok' | 'rejected'(메시지 문제) | 'dead'(웹훅 문제) | 'transient'(일시적)."""
    body = (body or "").strip()
    if status == 200 and body == "ok":
        return "ok"
    if body in PAYLOAD_ERRORS:
        return "rejected"
    if body in SLACK_ERROR_HINTS or status in (403, 404, 410):
        return "dead"
    return "transient"


def explain_slack_error(status: int, body: str) -> tuple[str, bool]:
    """(안내 문구, 영구적 실패 여부)를 반환한다."""
    body = (body or "").strip()
    if body in PAYLOAD_ERRORS:
        return PAYLOAD_ERRORS[body], True
    if body in SLACK_ERROR_HINTS:
        return SLACK_ERROR_HINTS[body], True
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
        value = int(str(resp.headers.get("Retry-After", "")).strip())
    except (TypeError, ValueError):
        return None
    return max(0, value)


def post_message(url: str, message: dict, label: str) -> str:
    """웹훅 하나에 메시지 하나를 보낸다. classify_slack_response의 결과를 반환한다.

    - 연결 단계에서 실패하면(아무것도 전달되지 않았음이 확실) 백오프 후 재시도한다.
    - 읽기 시간 초과처럼 Slack이 이미 받았을 수도 있는 오류는 재시도하지 않는다.
      바로 재시도하면 같은 메시지가 두 번 올라간다. 대기열에 남겨 다음 실행에 보낸다.
    - 429/5xx는 Retry-After를 지켜 재시도하고, 너무 길면 다음 실행으로 미룬다.
    """
    for attempt in range(1, SLACK_MAX_ATTEMPTS + 1):
        last = attempt == SLACK_MAX_ATTEMPTS
        try:
            resp = requests.post(url, json=message, timeout=SLACK_TIMEOUT, allow_redirects=False)
        except requests.ConnectionError as e:
            # ConnectTimeout 포함. 연결이 안 됐으니 중복 위험이 없다.
            log(f"Slack {label} 연결 실패: {type(e).__name__}")
            if last:
                return "transient"
            time.sleep(BACKOFF_BASE * attempt)
            continue
        except requests.RequestException as e:
            log(f"Slack {label} 전송 결과 불명: {type(e).__name__} — 중복을 피하려고 다음 실행에서 재시도합니다.")
            return "transient"

        kind = classify_slack_response(resp.status_code, resp.text)
        if kind == "ok":
            log(f"Slack {label} 전송 완료!")
            return kind

        body = redact((resp.text or "").strip()[:200], url)
        hint, _ = explain_slack_error(resp.status_code, body)
        detail = f"Slack {label} 전송 실패: HTTP {resp.status_code} {body} — {hint}"
        if kind in ("dead", "rejected"):
            warn(detail)
            return kind

        log(detail)
        if resp.status_code != 429 and resp.status_code < 500:
            return "transient"   # 알 수 없는 4xx — 같은 요청을 반복하지 않는다
        wait = _retry_after(resp)
        wait = BACKOFF_BASE * attempt if wait is None else max(1, wait)
        if last or wait > SLACK_MAX_WAIT:
            return "transient"
        log(f"  {wait}초 후 재시도... ({attempt + 1}/{SLACK_MAX_ATTEMPTS})")
        time.sleep(wait)

    return "transient"


def configured_webhooks() -> list[str]:
    """SLACK_WEBHOOK_URL에 설정된 웹훅 목록. 중복은 한 번만 보낸다."""
    raw = os.environ.get("SLACK_WEBHOOK_URL", "")
    return list(dict.fromkeys(u.strip() for u in raw.split(",") if u.strip()))


def alert_webhooks() -> list[str]:
    """봇 상태 경보를 받을 웹훅. 따로 지정하지 않으면 공고 채널로 보낸다."""
    raw = os.environ.get("SLACK_ALERT_WEBHOOK_URL", "")
    urls = list(dict.fromkeys(u.strip() for u in raw.split(",") if u.strip()))
    return urls or configured_webhooks()


def send_to_slack(message: dict, urls: Optional[list[str]] = None) -> tuple[int, int]:
    """메시지 하나를 모든 웹훅에 보낸다. (성공 채널 수, 대상 채널 수)

    대기열 없이 한 번만 보내면 되는 메시지(테스트 메시지, 상태 경보)용이다.
    """
    urls = configured_webhooks() if urls is None else urls
    delivered = 0
    for i, url in enumerate(urls, 1):
        if post_message(url, message, f"채널 {i} ({webhook_label(url)})") == "ok":
            delivered += 1
    if urls:
        log(f"Slack 전송 결과: {delivered}/{len(urls)} 채널 성공")
    return delivered, len(urls)


def chunk(items: list, size: int) -> list[list]:
    size = max(1, size)
    return [items[i:i + size] for i in range(0, len(items), size)]


def per_message() -> int:
    """한 메시지의 공고 수. Slack 블록 상한(첨부당 50개)에 맞춰 조인다."""
    return max(1, min(JOBS_PER_MESSAGE, MAX_JOBS_PER_MESSAGE))


def run_capacity() -> int:
    """채널 하나에 한 실행에서 보낼 수 있는 최대 공고 수."""
    return max(1, MAX_MESSAGES) * per_message()


# --------------------------------------------------------------------------
# 발송 대기열 (outbox.json)
#
# seen_jobs.json의 의미는 그대로다: "더 찾을 필요가 없는 공고" (어느 채널에든 전달됨,
# 필터로 제외됨, 또는 포기함). 이전 버전 코드도 그대로 읽는다.
# 채널별로 아직 못 보낸 공고는 outbox.json에 쌓아 두고 다음 실행에 이어서 보낸다.
# 한 채널이 잠깐 죽어도 그 채널의 몫이 사라지지 않고, 사이트 수집이 실패한 날에도
# 밀린 알림은 나간다. 공개 저장소이므로 웹훅 주소나 토큰은 저장하지 않는다.
# --------------------------------------------------------------------------

OUTBOX_VERSION = 1
SNAPSHOT_KEYS = ("id", "title", "url", "period", "start_date", "end_date",
                 "always_open", "org", "industry", "employment_type")


def channel_key(url: str) -> str:
    """웹훅을 구분하는 키. 해시의 앞부분만 써서 주소를 확인하는 지문으로도 못 쓰게 한다."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _valid_item(it) -> bool:
    return (
        isinstance(it, dict)
        and it.get("kind") in ("new", "reminder")
        and str(it.get("id", "")).isdigit()
        and isinstance(it.get("job"), dict)
        and bool(it["job"].get("title"))
        and str(it["job"].get("url", "")).startswith(f"{BASE_URL}/")
    )


def load_outbox() -> dict:
    """대기열을 읽는다. 깨져 있으면 재시도분만 잃으므로 빈 대기열로 진행하되 실패로 보고한다."""
    empty = {"version": OUTBOX_VERSION, "channels": {}}
    data = _read_json(OUTBOX_FILE, dict, "outbox.json")
    if not data:
        return empty
    channels = data.get("channels")
    if data.get("version") != OUTBOX_VERSION or not isinstance(channels, dict):
        problem = "outbox.json의 버전/형식을 알 수 없어 빈 대기열로 진행합니다."
        warn(problem)
        STATE_PROBLEMS.append(problem)
        return empty
    out = {"version": OUTBOX_VERSION, "channels": {}}
    for key, ch in channels.items():
        items = ch.get("pending", []) if isinstance(ch, dict) else []
        good = [it for it in items if _valid_item(it)]
        if len(good) != len(items):
            problem = f"outbox.json에서 손상된 항목 {len(items) - len(good)}개를 버렸습니다."
            warn(problem)
            STATE_PROBLEMS.append(problem)
        out["channels"][str(key)] = {"pending": good}
    return out


def save_outbox(outbox: dict) -> None:
    atomic_write_json(OUTBOX_FILE, outbox)


def queued_new_ids(outbox: dict) -> set[str]:
    return {it["id"] for ch in outbox["channels"].values() for it in ch["pending"] if it["kind"] == "new"}


def enqueue(outbox: dict, urls: list[str], jobs: list[dict], kind: str, today: str) -> None:
    """모든 채널의 대기열에 공고를 넣는다. 같은 (종류, ID)는 한 번만."""
    for url in urls:
        ch = outbox["channels"].setdefault(channel_key(url), {"pending": []})
        have = {(it["kind"], it["id"]) for it in ch["pending"]}
        for job in jobs:
            if (kind, job["id"]) in have:
                continue
            ch["pending"].append({
                "kind": kind,
                "id": job["id"],
                "queued": today,
                "job": {k: job.get(k, "") for k in SNAPSHOT_KEYS},
            })
            have.add((kind, job["id"]))


def hydrate(item: dict) -> dict:
    """대기열 항목을 메시지용 공고로. D-day는 보내는 시점에 다시 계산한다."""
    job = dict(item["job"])
    job["id"] = item["id"]
    job["always_open"] = bool(job.get("always_open"))
    job["d_day"] = compute_d_day(job["end_date"]) if job.get("end_date") else None
    return job


def expire(ch: dict, today: str) -> list[dict]:
    """마감이 지났거나 TTL보다 오래 기다린 항목을 버리고, 대기열 길이를 제한한다."""
    cutoff = (datetime.fromisoformat(today) - timedelta(days=max(0, OUTBOX_TTL_DAYS))).date().isoformat()
    keep, dropped = [], []
    for it in ch["pending"]:
        d = hydrate(it)["d_day"]
        too_old = str(it.get("queued", today)) < cutoff
        (dropped if (d is not None and d < 0) or too_old else keep).append(it)
    limit = max(1, OUTBOX_MAX_PENDING)
    if len(keep) > limit:
        dropped += keep[:-limit]
        keep = keep[-limit:]
    ch["pending"] = keep
    return dropped


MESSAGE_STYLE = {
    "new": ("", "#0054a6"),
    "reminder": ("*⏰  마감 임박 채용공고*", "#e8912d"),
}


def drain(outbox: dict, urls: list[str], seen: list[str], today: str) -> dict:
    """채널마다 대기열을 순서대로 보낸다. 묶음 하나가 전달될 때마다 상태를 저장한다.

    - 채널마다 한 실행에 최대 MAX_MESSAGES개 메시지만 보낸다. (밀린 몫이 한꺼번에 쏟아지지 않게)
    - 한 채널이 실패하면 그 채널만 멈추고 다른 채널은 계속 보낸다.
    - Slack이 메시지 내용을 거부하면 한 건씩 다시 보내 문제의 공고만 걸러낸다.
    """
    seen_set = set(seen)
    per = per_message()
    stats = {"delivered": {}, "failed": [], "dead": [], "expired": 0, "rejected": [], "backlog": 0}

    def settle(ch, items):
        """항목을 대기열에서 빼고, 신규 공고는 seen에 기록한다. 대기열을 먼저 저장한다."""
        ids = {id(it) for it in items}
        ch["pending"] = [it for it in ch["pending"] if id(it) not in ids]
        for it in items:
            if it["kind"] == "new" and it["id"] not in seen_set:
                seen.append(it["id"])
                seen_set.add(it["id"])
        save_outbox(outbox)
        save_seen(seen)

    for i, url in enumerate(urls, 1):
        label = f"채널 {i} ({webhook_label(url)})"
        ch = outbox["channels"].setdefault(channel_key(url), {"pending": []})
        stats["delivered"][label] = 0

        gone = expire(ch, today)
        if gone:
            stats["expired"] += len(gone)
            warn(f"{label}: 오래되었거나 마감된 미전송 공고 {len(gone)}건을 대기열에서 정리했습니다.")
            settle(ch, gone)

        counts = {k: sum(1 for it in ch["pending"] if it["kind"] == k) for k in MESSAGE_STYLE}
        parts = {k: math.ceil(counts[k] / per) for k in MESSAGE_STYLE}
        part_no = {k: 0 for k in MESSAGE_STYLE}
        budget = max(1, MAX_MESSAGES)
        rejected_here = 0

        while ch["pending"] and budget > 0:
            kind = ch["pending"][0]["kind"]
            batch = []
            for it in ch["pending"]:
                if it["kind"] != kind or len(batch) == per:
                    break
                batch.append(it)
            title, color = MESSAGE_STYLE[kind]
            part_no[kind] += 1

            def build(items, kind=kind, title=title, color=color):
                return build_slack_message(
                    [hydrate(it) for it in items], title=title, color=color,
                    total=counts[kind], part=part_no[kind], parts=parts[kind],
                )

            result = post_message(url, build(batch), label)
            budget -= 1

            if result == "rejected" and len(batch) > 1 and rejected_here < 3:
                # 어느 공고 때문인지 한 건씩 확인한다. 거부가 계속되면 코드 쪽 문제이니 멈춘다.
                for it in list(batch):
                    one = post_message(url, build([it]), label)
                    if one == "ok":
                        settle(ch, [it])
                        stats["delivered"][label] += 1
                    elif one == "rejected":
                        rejected_here += 1
                        stats["rejected"].append(it["id"])
                        error(f"{label}: 공고 {it['id']}의 메시지를 Slack이 거부해 이 공고는 건너뜁니다.",
                              title="메시지 거부")
                        settle(ch, [it])
                        if rejected_here >= 3:
                            break
                    else:
                        result = one
                        break
                    time.sleep(1)
                else:
                    continue
                if result == "rejected":
                    stats["failed"].append(label)
                    break
            elif result == "rejected":
                rejected_here += 1
                stats["rejected"].append(batch[0]["id"])
                error(f"{label}: 공고 {batch[0]['id']}의 메시지를 Slack이 거부해 이 공고는 건너뜁니다.",
                      title="메시지 거부")
                settle(ch, batch)
                if rejected_here >= 3:
                    stats["failed"].append(label)
                    break
                continue

            if result == "ok":
                settle(ch, batch)
                stats["delivered"][label] += len(batch)
                if ch["pending"] and budget > 0:
                    time.sleep(1)   # Slack 권장: 웹훅당 초당 1건 이하
                continue

            (stats["dead"] if result == "dead" else stats["failed"]).append(label)
            break

        stats["backlog"] += len(ch["pending"])
        if ch["pending"] and label not in stats["failed"] + stats["dead"]:
            log(f"{label}: {len(ch['pending'])}건은 다음 실행에 이어서 보냅니다. (한 실행당 메시지 {MAX_MESSAGES}개 제한)")

    # 설정에서 빠진 채널의 대기열은 아무도 보낼 수 없다. 지운다.
    live = {channel_key(u) for u in urls}
    for key in [k for k in outbox["channels"] if k not in live]:
        n = len(outbox["channels"][key]["pending"])
        if n:
            log(f"더 이상 설정에 없는 채널의 미전송 {n}건을 정리합니다.")
        del outbox["channels"][key]
    save_outbox(outbox)
    return stats


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
    """알릴 공고를 마감 리마인더 대상으로 등록한다.

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


def due_reminders(tracked: dict) -> list[dict]:
    """오늘 리마인더를 보낼 공고. 보냄 표시를 tracked에 바로 남긴다.

    지나간 기준을 한 번에 모두 소비한다. D-1에 처음 확인된 공고가 D-3·D-1 기준으로
    이틀 연속 알림되는 일이 없다. 전송은 대기열이 책임지므로 되돌리기가 필요 없다.
    """
    due = []
    for job_id, info in tracked.items():
        d = compute_d_day(info.get("end_date", ""))
        if d is None or d < 0:
            continue
        reminded = set(info.get("reminded") or [])
        passed = {day for day in REMINDER_DAYS if d <= day}
        if not passed or passed <= reminded:
            continue
        info["reminded"] = sorted(reminded | passed, reverse=True)
        due.append({
            "id": job_id,
            "title": info.get("title", ""),
            "url": info.get("url", ""),
            "period": info.get("period", ""),
            "start_date": "",
            "end_date": info.get("end_date", ""),
            "org": info.get("org", ""),
            "industry": info.get("industry", ""),
            "employment_type": info.get("employment_type", ""),
            "always_open": False,
            "d_day": d,
        })
    due.sort(key=sort_key)
    return [j for j in due if j["title"] and j["url"]]


# --------------------------------------------------------------------------
# 상태 감시 — 수집이 계속 실패하는데 아무도 모르는 일이 없게 한다
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="minutes")


def load_health() -> dict:
    data = _read_json(HEALTH_FILE, dict, "health.json")
    return {
        "consecutive_failures": int(data.get("consecutive_failures") or 0),
        "failing_since": data.get("failing_since") or "",
        "last_error": data.get("last_error") or "",
        "last_alert": data.get("last_alert") or "",
        "alerted": bool(data.get("alerted")),
        "last_success": data.get("last_success") or "",
    }


def save_health(health: dict) -> None:
    atomic_write_json(HEALTH_FILE, health)


def _hours_since(iso: str) -> float:
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(KST) - then).total_seconds() / 3600


def health_update(health: dict, ok: bool, reason: str = "") -> Optional[str]:
    """수집 결과를 기록하고, 보낼 경보 종류를 돌려준다.

    'failing'   — 연속 실패가 HEALTH_ALERT_AFTER회에 도달 (이후 HEALTH_REALERT_HOURS마다 한 번)
    'recovered' — 경보를 보낸 뒤 처음 성공
    None        — 보낼 것 없음
    """
    now = _now_iso()
    if ok:
        action = "recovered" if health.get("alerted") else None
        health.update(consecutive_failures=0, failing_since="", last_error="",
                      alerted=False, last_success=now)
        return action

    health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
    if not health.get("failing_since"):
        health["failing_since"] = now
    health["last_error"] = reason[:300]
    if health["consecutive_failures"] < max(1, HEALTH_ALERT_AFTER):
        return None
    if health.get("alerted") and _hours_since(health.get("last_alert", "")) < HEALTH_REALERT_HOURS:
        return None
    health["alerted"] = True
    health["last_alert"] = now
    return "failing"


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    return f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else ""


def build_health_message(action: str, health: dict) -> dict:
    run_url = _run_url()
    if action == "recovered":
        text = (f"✅ *하이브레인 알림 봇이 복구되었습니다.*\n"
                f"공고 수집이 다시 정상입니다. (장애 시작: {health.get('recovered_from', '-')})")
        color = "#2eb67d"
    else:
        last_ok = health.get("last_success") or "기록 없음"
        text = (f"🚨 *하이브레인 공고 수집이 {health['consecutive_failures']}회 연속 실패했습니다.*\n"
                f"이 상태에서는 신규 공고 알림이 나가지 않습니다.\n"
                f"• 원인: {mrk(health.get('last_error', ''), 300)}\n"
                f"• 실패 시작: {health.get('failing_since')}  ｜  마지막 성공: {last_ok}")
        color = "#e01e5a"
    if run_url:
        text += f"\n• 실행 로그: <{run_url}|GitHub Actions>"
    return {"attachments": [{
        "color": color,
        "fallback": re.sub(r"[*_]", "", text.split("\n")[0]),
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    }]}


def send_health_alert(action: str, health: dict) -> bool:
    urls = alert_webhooks()
    if not urls:
        return False
    delivered, _ = send_to_slack(build_health_message(action, health), urls)
    return delivered > 0


# --------------------------------------------------------------------------
# 실행 결과 보고
# --------------------------------------------------------------------------

def write_report(summary: dict, report_path: str = "") -> None:
    """실행 요약을 JSON 파일과 Actions 요약 화면(GITHUB_STEP_SUMMARY)에 남긴다."""
    if report_path:
        try:
            Path(report_path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            warn(f"실행 요약 파일을 쓰지 못했습니다: {e}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        rows = [
            ("결과", "✅ 정상" if summary.get("ok") else "❌ 실패"),
            ("모드", summary.get("mode", "")),
            ("수집", f"{summary.get('collected', 0)}건" if summary.get("fetch_ok") else f"실패 — {summary.get('fetch_error', '')}"),
            ("신규", f"{summary.get('new', 0)}건 (이번에 대기열 등록 {summary.get('queued', 0)}건, 다음으로 미룸 {summary.get('deferred', 0)}건)"),
            ("필터 제외", f"{summary.get('filtered', 0)}건"),
            ("리마인더", f"{summary.get('reminders', 0)}건"),
            ("채널별 전달", ", ".join(f"{k}: {v}건" for k, v in summary.get("delivered", {}).items()) or "-"),
            ("미전송 대기", f"{summary.get('backlog', 0)}건"),
            ("연속 수집 실패", f"{summary.get('consecutive_failures', 0)}회"),
        ]
        if summary.get("problems"):
            rows.append(("문제", "<br>".join(html_escape(p) for p in summary["problems"])))
        table = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
        try:
            with open(step_summary, "a", encoding="utf-8") as f:
                f.write(f"### HiBrain Job Alert\n\n{table}\n")
        except OSError:
            pass


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{name}={value}\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------

MODES = ("live", "dry-run", "test-message")


def run_dry(jobs: list[dict], seen: list[str], outbox: dict, summary: dict) -> None:
    """보내지 않고, 상태도 바꾸지 않고, 무엇을 보낼지만 보여준다. (상세 요청도 하지 않는다)"""
    known = set(seen) | queued_new_ids(outbox)
    new = sorted((j for j in jobs if j["id"] not in known), key=sort_key)
    summary.update(new=len(new), queued=min(len(new), run_capacity()),
                   deferred=max(0, len(new) - run_capacity()))
    log(f"[드라이런] 신규 {len(new)}건 — 이번 실행이라면 채널당 최대 {run_capacity()}건을 보냅니다.")
    for i, batch in enumerate(chunk(new[:run_capacity()], per_message()), 1):
        msg = build_slack_message(batch, total=min(len(new), run_capacity()), part=i,
                                  parts=math.ceil(min(len(new), run_capacity()) / per_message()))
        blocks = msg["attachments"][0]["blocks"]
        log(f"[드라이런] 메시지 {i}: 공고 {len(batch)}건, 블록 {len(blocks)}개")
        if i == 1:
            print(json.dumps(msg, ensure_ascii=False, indent=2))


def run_test_message(jobs: list[dict], urls: list[str], summary: dict) -> bool:
    """샘플 공고로 실제 메시지를 한 번 보낸다. 상태는 바꾸지 않는다."""
    sample = sorted(jobs, key=sort_key)[:per_message()]
    log(f"[테스트 메시지] 공고 {len(sample)}건을 샘플로 Slack에 전송합니다.")
    enrich_jobs(sample)
    msg = build_slack_message(sample, title=f"🧪 [테스트] 하이브레인 신규 채용 {len(sample)}건")
    delivered, total = send_to_slack(msg, urls)
    summary["delivered"] = {"테스트": delivered}
    return delivered == total


def run_live(jobs: list[dict], fetch_ok: bool, urls: list[str], seen: list[str],
             outbox: dict, summary: dict) -> None:
    today = _today_kst().date().isoformat()

    # 1) 신규 공고 선별: seen에도 없고 대기열에도 없는 것.
    known = set(seen) | queued_new_ids(outbox)
    new = sorted((j for j in jobs if j["id"] not in known), key=sort_key)
    claimed, deferred = new[:run_capacity()], new[run_capacity():]
    if deferred:
        warn(f"신규 공고가 많아 {len(deferred)}건은 다음 실행에 이어서 처리합니다.")
    log(f"신규 공고: {len(new)}개")
    summary.update(new=len(new), deferred=len(deferred))

    # 2) 보강·필터는 이번에 처리할 공고에만.
    enrich_jobs(claimed)
    include, exclude = _env_list("JOB_INCLUDE_KEYWORDS"), _env_list("JOB_EXCLUDE_KEYWORDS")
    kept = [j for j in claimed if matches_filters(j, include, exclude)]
    if include or exclude:
        log(f"키워드 필터 적용: {len(kept)}/{len(claimed)}건 통과")
    # 이미 마감된 공고는 알리지 않는다. (목록에는 며칠 더 남아 있는 경우가 있다)
    closed = [j for j in kept if not j.get("always_open") and j.get("d_day") is not None and j["d_day"] < 0]
    if closed:
        log(f"이미 마감된 공고 {len(closed)}건은 알리지 않습니다.")
    kept = [j for j in kept if j not in closed]
    kept_ids = {j["id"] for j in kept}
    filtered = [j for j in claimed if j["id"] not in kept_ids]
    summary.update(queued=len(kept), filtered=len(filtered))

    # 3) 리마인더: 사이트 요청이 필요 없으니 수집이 실패한 날에도 계산한다.
    tracked = load_tracked() if (REMINDER_DAYS or TRACKED_FILE.exists()) else {}
    due: list[dict] = []
    if REMINDER_DAYS:
        tracked = track_jobs(prune_tracked(tracked), kept)
        due = due_reminders(tracked)
        if due:
            log(f"마감 임박 리마인더 {len(due)}건 (기준 D-{REMINDER_DAYS})")
    summary["reminders"] = len(due)

    # 4) 첫 전송 전에 저장: 대기열 → seen(필터 제외분) → 리마인더 상태.
    #    여기서 죽어도 아무것도 잃지 않는다. 다음 실행이 대기열을 이어서 보낸다.
    enqueue(outbox, urls, kept, "new", today)
    enqueue(outbox, urls, due, "reminder", today)
    save_outbox(outbox)
    if filtered:
        seen.extend(j["id"] for j in filtered if j["id"] not in set(seen))
        save_seen(seen)
    if REMINDER_DAYS or TRACKED_FILE.exists():
        save_tracked(prune_tracked(tracked))

    # 5) 채널별로 보낸다. 묶음마다 저장한다.
    stats = drain(outbox, urls, seen, today)
    summary.update(delivered=stats["delivered"], backlog=stats["backlog"], expired=stats["expired"])
    total_delivered = sum(stats["delivered"].values())
    log(f"전송 완료: {total_delivered}건, 미전송 대기 {stats['backlog']}건")

    for label in stats["dead"]:
        summary["problems"].append(f"{label}: 웹훅을 더 이상 쓸 수 없습니다. SLACK_WEBHOOK_URL을 고쳐 주세요.")
    for label in stats["failed"]:
        summary["problems"].append(f"{label}: 전송 실패 — 미전송분은 다음 실행에 다시 보냅니다.")
    if stats["rejected"]:
        summary["problems"].append(f"Slack이 메시지를 거부해 건너뛴 공고: {', '.join(stats['rejected'])}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="하이브레인 신규 채용공고 Slack 알림")
    parser.add_argument("--mode", choices=MODES,
                        default=os.environ.get("RUN_MODE", "").strip() or "live",
                        help="live: 실제 알림(기본) / dry-run: 보내지 않고 미리보기 / test-message: 샘플 1회 전송")
    parser.add_argument("--report", default="", help="실행 요약 JSON을 쓸 경로")
    args = parser.parse_args(argv)
    mode = args.mode
    if mode not in MODES:
        error(f"알 수 없는 모드: {mode}")
        return 2

    if os.environ.get("TEST_MODE", "").strip().lower() == "true" and mode == "live":
        mode = "test-message"   # 이전 워크플로 입력과의 호환

    STATE_PROBLEMS.clear()
    summary = {"mode": mode, "ok": False, "fetch_ok": False, "collected": 0, "new": 0,
               "queued": 0, "deferred": 0, "filtered": 0, "reminders": 0,
               "delivered": {}, "backlog": 0, "problems": [], "consecutive_failures": 0}

    def finish(code: int) -> int:
        summary["problems"] = summary["problems"] + [p for p in STATE_PROBLEMS if p not in summary["problems"]]
        if STATE_PROBLEMS and code == 0:
            code = 1
        summary["ok"] = code == 0
        for p in summary["problems"]:
            error(p, title="HiBrain 알림")
        write_report(summary, args.report)
        # 여기까지 왔다면 실패는 스크립트가 직접 보고했다. 워크플로의 비상 알림은 필요 없다.
        set_output("failure_reported", "true")
        log("완료" if code == 0 else "실패로 종료합니다 (위의 오류 참고).")
        return code

    urls = configured_webhooks()
    log(f"hibrain.net 채용정보 알림 시작 (모드: {mode}, 채널 {len(urls)}개)")

    # 웹훅이 없으면 알림이 나갈 수 없다. 사이트에 요청하기 전에 멈춘다.
    if mode != "dry-run" and not urls:
        summary["problems"].append("SLACK_WEBHOOK_URL이 비어 있어 알림을 보낼 수 없습니다. "
                                   "미리보기만 하려면 --mode dry-run을 쓰세요.")
        return finish(1)

    if mode == "test-message":
        try:
            jobs = scrape_jobs()
        except FetchError as e:
            summary.update(fetch_error=str(e))
            summary["problems"].append(f"공고 수집 실패: {e}")
            return finish(1)
        summary.update(fetch_ok=True, collected=len(jobs))
        return finish(0 if run_test_message(jobs, urls, summary) else 1)

    try:
        seen = load_seen()
    except StateError as e:
        summary["problems"].append(f"{e} 대량 재알림을 막기 위해 실행을 멈춥니다. "
                                   "이전 커밋의 seen_jobs.json으로 복구하세요.")
        return finish(1)
    outbox = load_outbox()

    log("hibrain.net 채용정보 스크래핑 시작...")
    try:
        jobs = scrape_jobs()
        fetch_ok, fetch_error = True, ""
        log(f"총 {len(jobs)}개 공고 발견")
    except FetchError as e:
        jobs, fetch_ok, fetch_error = [], False, str(e)
        log(f"공고를 가져오지 못했습니다: {e}")
    summary.update(fetch_ok=fetch_ok, collected=len(jobs), fetch_error=fetch_error)

    if mode == "dry-run":
        if fetch_ok:
            run_dry(jobs, seen, outbox, summary)
        else:
            summary["problems"].append(f"공고 수집 실패: {fetch_error}")
        return finish(0 if fetch_ok else 1)

    # ---- live ----
    health = load_health()
    failing_since = health.get("failing_since", "")
    action = health_update(health, fetch_ok, fetch_error)
    summary["consecutive_failures"] = health["consecutive_failures"]
    save_health(health)

    if not fetch_ok:
        summary["problems"].append(
            f"공고 수집 실패 ({health['consecutive_failures']}회 연속): {fetch_error}"
        )

    # 수집이 실패해도 밀린 대기열과 리마인더는 보낸다.
    run_live(jobs, fetch_ok, urls, seen, outbox, summary)

    if action:
        if action == "recovered":
            health["recovered_from"] = failing_since
        if send_health_alert(action, health):
            log("상태 경보를 Slack에 보냈습니다." if action == "failing" else "복구 알림을 Slack에 보냈습니다.")
        elif action == "failing":
            # 경보가 안 나갔으면 다음 실행에서 다시 시도한다.
            health["alerted"] = False
            save_health(health)
            summary["problems"].append("수집 실패 경보를 Slack에 보내지 못했습니다.")
        health.pop("recovered_from", None)

    return finish(1 if summary["problems"] else 0)


if __name__ == "__main__":
    sys.exit(main())
