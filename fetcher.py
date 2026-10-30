"""Free collection: verified HTTP response, then an ordinary Chromium browser."""
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://hibrain.net"
LIST_URL = f"{BASE_URL}/recruitment/recruits?listType=D3NEW&pagesize=50&sortType=SORTDTM"


class FetchError(RuntimeError):
    pass


def positive_int(name, default, maximum=300):
    value = os.environ.get(name, str(default))
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not 1 <= result <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return result


def retry_delay(value, attempt):
    """Bound waits and support both Retry-After forms. None means defer to next run."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            delay = 2 ** attempt
    if delay > 60:
        return None
    return max(1, delay)


def valid_listing(html):
    soup = BeautifulSoup(html, "html.parser")
    return any(
        re.search(r"/recruits/\d+", a.get("href", ""))
        and (a.get("title", "").strip() or a.get_text(strip=True))
        for a in soup.select("#articleList li.row a[href]")
    )


def fetch_http(log):
    attempts = positive_int("FETCH_MAX_ATTEMPTS", 3, 5)
    timeout = positive_int("DIRECT_TIMEOUT", 30, 120)
    for attempt in range(1, attempts + 1):
        delay = 2 ** attempt
        try:
            response = requests.get(
                LIST_URL,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            # Exception URLs may contain proxy credentials. Never log their text.
            log(f"직접 요청 {attempt}/{attempts}: {type(exc).__name__}")
        else:
            if response.status_code == 200 and valid_listing(response.text):
                return response.text
            log(f"직접 요청 {attempt}/{attempts}: HTTP {response.status_code}, 유효한 채용 목록 없음")
            if response.status_code != 429 and response.status_code < 500:
                break  # Auth failures, blocks, and changed markup won't improve by repeating.
            delay = retry_delay(response.headers.get("Retry-After"), attempt)
        if attempt < attempts:
            if delay is None:
                break
            time.sleep(delay)
    raise FetchError("직접 요청으로 유효한 채용 목록을 가져오지 못했습니다.")


def fetch_browser(log):
    from playwright.sync_api import Error, sync_playwright

    options = {"headless": True}
    executable = os.environ.get("BROWSER_EXECUTABLE_PATH")
    if executable:
        options["executable_path"] = executable
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        parsed = urlsplit(proxy)
        options["proxy"] = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}",
                            "bypass": os.environ.get("NO_PROXY", "localhost,127.0.0.1")}
        if parsed.username:
            from urllib.parse import unquote
            options["proxy"].update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    timeout = positive_int("BROWSER_TIMEOUT", 45, 120) * 1000
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**options)
            try:
                page = browser.new_page(locale="ko-KR", timezone_id="Asia/Seoul")
                # Keep scripts/styles; images and video are unnecessary for listing extraction.
                page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_())
                response = page.goto(LIST_URL, wait_until="domcontentloaded", timeout=timeout)
                if response and response.status >= 400:
                    raise FetchError(f"브라우저 요청 HTTP {response.status}: 실행 위치의 접속 허용 여부를 확인하세요.")
                page.wait_for_selector("#articleList li.row a[href*='/recruits/']", timeout=timeout)
                html = page.content()
                if not valid_listing(html):
                    raise FetchError("브라우저 응답에 유효한 채용 목록이 없습니다.")
                return html
            finally:
                browser.close()
    except Error as exc:
        raise FetchError(f"브라우저 수집 실패 ({type(exc).__name__}); Chromium 설치·네트워크·사이트 응답을 확인하세요.") from None


def fetch_page(log=print):
    mode = os.environ.get("FETCH_MODE", "auto")
    if mode not in {"auto", "http", "browser"}:
        raise ValueError("FETCH_MODE must be auto, http, or browser")
    if mode in {"auto", "http"}:
        try:
            html = fetch_http(log)
            log("수집 성공: HTTP")
            return html
        except FetchError:
            if mode == "http":
                raise
            log("직접 요청 실패. 무료 Chromium 브라우저로 확인합니다.")
    html = fetch_browser(log)
    log("수집 성공: Chromium")
    return html
