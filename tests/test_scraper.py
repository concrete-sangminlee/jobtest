"""scraper.py 단위 테스트.

네트워크를 타지 않는다. 목록/상세 HTML은 tests/fixtures/ 의 실제 응답 조각을
쓰고, Slack 전송은 가짜 응답 객체로 대체한다.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def job_list_html():
    return (FIXTURES / "job_list.html").read_text(encoding="utf-8")


@pytest.fixture
def job_detail_html():
    return (FIXTURES / "job_detail.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """재시도 대기로 테스트가 느려지지 않게 한다."""
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """상태 파일이 리포의 실제 파일을 건드리지 않게 한다."""
    monkeypatch.setattr(scraper, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(scraper, "TRACKED_FILE", tmp_path / "tracked_jobs.json")


def make_job(job_id, **kw):
    job = {
        "id": str(job_id),
        "title": f"공고 {job_id}",
        "period": "",
        "start_date": "",
        "end_date": "",
        "always_open": False,
        "d_day": None,
        "org": "",
        "industry": "",
        "employment_type": "",
        "summary": "",
        "url": f"https://hibrain.net/recruitment/recruits/{job_id}",
    }
    job.update(kw)
    return job


class FakeResponse:
    def __init__(self, status=200, text="ok", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300


# --------------------------------------------------------------------------
# 날짜 파싱 — 두 자리 연도 때문에 D-day가 전혀 계산되지 않던 회귀 버그
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("26.10.23", (2026, 10, 23)),      # 하이브레인 목록 표기
    ("2026-10-07", (2026, 10, 7)),     # JSON-LD 표기
    ("2026.10.23", (2026, 10, 23)),
    ("26.1.3", (2026, 1, 3)),
    ("25.12.31", (2025, 12, 31)),
])
def test_parse_date_accepts_two_and_four_digit_years(text, expected):
    got = scraper._parse_date(text)
    assert got is not None, f"{text!r} 파싱 실패"
    assert (got.year, got.month, got.day) == expected


@pytest.mark.parametrize("text", ["", None, "상시채용", "26.13.45", "공고문참조"])
def test_parse_date_rejects_garbage(text):
    assert scraper._parse_date(text) is None


def test_compute_d_day_counts_from_today(monkeypatch):
    from datetime import datetime
    fixed = datetime(2026, 10, 11, 15, 30, tzinfo=scraper.KST)
    monkeypatch.setattr(scraper, "_today_kst",
                        lambda: fixed.replace(hour=0, minute=0, second=0, microsecond=0))
    assert scraper.compute_d_day("26.10.11") == 0     # 오늘 마감
    assert scraper.compute_d_day("26.10.12") == 1     # 내일
    assert scraper.compute_d_day("26.10.23") == 12
    assert scraper.compute_d_day("26.10.10") == -1    # 어제 마감
    assert scraper.compute_d_day("") is None


@pytest.mark.parametrize("job,label", [
    (make_job(1, d_day=0), "D-DAY"),
    (make_job(2, d_day=7), "D-7"),
    (make_job(3, d_day=-2), "마감"),
    (make_job(4, always_open=True, d_day=5), "상시"),
    (make_job(5, d_day=None), ""),
])
def test_d_day_label(job, label):
    assert scraper.d_day_label(job) == label


# --------------------------------------------------------------------------
# 목록 파싱
# --------------------------------------------------------------------------

def test_parse_job_list_extracts_jobs_and_dedupes(job_list_html):
    jobs = scraper.parse_job_list(job_list_html)
    ids = [j["id"] for j in jobs]
    assert len(ids) == len(set(ids)), "중복 ID가 걸러지지 않았다"
    assert len(jobs) >= 5
    for job in jobs:
        assert job["id"].isdigit()
        assert job["title"]
        assert job["url"].endswith(job["id"])


def test_parse_job_list_computes_d_day_for_dated_rows(job_list_html):
    jobs = scraper.parse_job_list(job_list_html)
    dated = [j for j in jobs if j["end_date"]]
    assert dated, "픽스처에 마감일이 있는 공고가 없다"
    # 회귀 방지: 예전에는 전부 None이었다.
    assert all(j["d_day"] is not None for j in dated)


def test_parse_job_list_tolerates_missing_period(job_list_html):
    jobs = scraper.parse_job_list(job_list_html)
    no_period = [j for j in jobs if not j["end_date"]]
    assert no_period, "픽스처에 기간 없는 공고가 없다"
    assert all(j["d_day"] is None and j["title"] for j in no_period)


def test_parse_job_list_detects_always_open(job_list_html):
    jobs = scraper.parse_job_list(job_list_html)
    assert any(j["always_open"] for j in jobs)


def test_parse_job_list_without_article_list():
    assert scraper.parse_job_list("<html><body>차단되었습니다</body></html>") == []


# --------------------------------------------------------------------------
# 상세 보강
# --------------------------------------------------------------------------

def test_parse_job_detail_reads_json_ld(job_detail_html):
    detail = scraper.parse_job_detail(job_detail_html)
    assert detail["org"] == "국방어학원"
    assert detail["industry"] == "정출연/국/공립연구소"
    assert detail["employment_type"] == "강사"
    assert detail["posted"] == "2026-10-07"
    assert "국방어학원" in detail["summary"]


def test_parse_job_detail_drops_placeholder_employment_type():
    html = """<script type="application/ld+json">
    {"@type": "JobPosting", "employmentType": "공고문참조",
     "hiringOrganization": {"name": "한미약품 "}, "industry": "기업/기업연구소"}
    </script>"""
    detail = scraper.parse_job_detail(html)
    assert detail["employment_type"] == ""   # 자리표시자는 노이즈라 버린다
    assert detail["org"] == "한미약품"


def test_parse_job_detail_survives_broken_json():
    assert scraper.parse_job_detail(
        '<script type="application/ld+json">{nope</script>'
    )["org"] == ""


def test_parse_job_detail_handles_ld_json_array():
    html = """<script type="application/ld+json">
    [{"@type": "WebSite"}, {"@type": "JobPosting", "industry": "대학",
      "hiringOrganization": {"name": "POSTECH "}}]
    </script>"""
    assert scraper.parse_job_detail(html)["org"] == "POSTECH"


def test_enrich_jobs_fills_fields(monkeypatch):
    monkeypatch.setattr(scraper, "ENRICH", True)
    jobs = [make_job(1), make_job(2)]
    detail = {"org": "서울대", "industry": "대학", "employment_type": "교수", "summary": "요약"}
    assert scraper.enrich_jobs(jobs, fetcher=lambda _: detail) == 2
    assert all(j["org"] == "서울대" and j["employment_type"] == "교수" for j in jobs)


def test_enrich_jobs_is_best_effort(monkeypatch):
    """상세 보강이 전부 실패해도 공고 자체는 멀쩡해야 한다."""
    monkeypatch.setattr(scraper, "ENRICH", True)
    jobs = [make_job(1, title="제목 유지")]
    assert scraper.enrich_jobs(jobs, fetcher=lambda _: {}) == 0
    assert jobs[0]["title"] == "제목 유지"
    assert jobs[0]["org"] == ""


def test_enrich_jobs_can_be_disabled(monkeypatch):
    monkeypatch.setattr(scraper, "ENRICH", False)
    calls = []
    scraper.enrich_jobs([make_job(1)], fetcher=lambda i: calls.append(i) or {})
    assert calls == []


def test_enrich_jobs_respects_max(monkeypatch):
    monkeypatch.setattr(scraper, "ENRICH", True)
    monkeypatch.setattr(scraper, "ENRICH_MAX", 2)
    calls = []
    scraper.enrich_jobs([make_job(i) for i in range(5)],
                        fetcher=lambda i: calls.append(i) or {})
    assert len(calls) == 2


def test_fetch_job_detail_returns_empty_on_network_error(monkeypatch):
    def boom(*_a, **_k):
        raise scraper.requests.RequestException("timeout")
    monkeypatch.setattr(scraper.requests, "get", boom)
    assert scraper.fetch_job_detail("123") == {}


# --------------------------------------------------------------------------
# 선별·정렬
# --------------------------------------------------------------------------

def test_sort_key_puts_urgent_first_and_undated_last():
    jobs = [
        make_job(1, d_day=10),
        make_job(2, d_day=1),
        make_job(3, d_day=None),
        make_job(4, always_open=True),
        make_job(5, d_day=0),
        make_job(6, d_day=-3),
    ]
    order = [j["id"] for j in sorted(jobs, key=scraper.sort_key)]
    assert order[:3] == ["5", "2", "1"]           # D-DAY → D-1 → D-10
    assert set(order[3:]) == {"3", "4", "6"}      # 상시/미파싱/마감은 뒤로


def test_sort_key_is_deterministic_for_ties():
    jobs = [make_job(100, d_day=5), make_job(300, d_day=5), make_job(200, d_day=5)]
    order = [j["id"] for j in sorted(jobs, key=scraper.sort_key)]
    assert order == ["300", "200", "100"]         # 동률이면 최신(ID 큰 것) 먼저


@pytest.mark.parametrize("include,exclude,expected", [
    ([], [], True),
    (["교수"], [], True),
    (["연구원"], [], False),
    ([], ["포스텍"], False),
    (["교수"], ["포스텍"], False),
])
def test_matches_filters(include, exclude, expected):
    job = make_job(1, title="포스텍 전임교원 초빙", employment_type="교수", org="POSTECH")
    assert scraper.matches_filters(job, include, exclude) is expected


def test_matches_filters_is_case_insensitive():
    job = make_job(1, title="UNIST Post-Doc 모집")
    assert scraper.matches_filters(job, ["post-doc"], []) is True


# --------------------------------------------------------------------------
# 메시지 구성
# --------------------------------------------------------------------------

def test_job_block_shows_d_day_badge():
    block = scraper._job_block(make_job(1, period="26.10.07 ~ 26.10.23", d_day=12))
    text = block["text"]["text"]
    assert "D-12" in text and "26.10.07 ~ 26.10.23" in text
    assert block["accessory"]["url"].endswith("/1")


def test_job_block_shows_enriched_meta():
    block = scraper._job_block(
        make_job(1, org="국방어학원", industry="정출연/국/공립연구소", employment_type="강사")
    )
    assert "국방어학원 · 정출연/국/공립연구소 · 강사" in block["text"]["text"]


def test_job_block_omits_missing_fields():
    text = scraper._job_block(make_job(1))["text"]["text"]
    assert "🏢" not in text and "📅" not in text


def test_build_slack_message_marks_parts():
    msg = scraper.build_slack_message([make_job(1)], total=40, part=2, parts=3)
    blocks = msg["attachments"][0]["blocks"]
    assert "(2/3)" in blocks[0]["text"]["text"]
    assert "40건" in blocks[1]["elements"][0]["text"]


def test_message_overhead_constant_matches_reality():
    """머리말/구분선/꼬리말 블록 수가 바뀌면 상한 계산도 같이 틀어진다."""
    blocks = scraper.build_slack_message([])["attachments"][0]["blocks"]
    assert len(blocks) == scraper._MESSAGE_OVERHEAD_BLOCKS


# --------------------------------------------------------------------------
# Slack 전송
# --------------------------------------------------------------------------

def test_webhook_label_and_redact_hide_token():
    url = "https://hooks.slack.com/services/T06/B0A/supersecret"
    assert "supersecret" not in scraper.webhook_label(url)
    assert "B0A" in scraper.webhook_label(url)
    msg = f"Max retries exceeded with url: {url}"
    assert "supersecret" not in scraper.redact(msg, url)


# --------------------------------------------------------------------------
# 분할 전송 — 20건 초과가 조용히 버려지던 회귀 버그
# --------------------------------------------------------------------------

def test_chunk_splits_evenly():
    assert scraper.chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert scraper.chunk([], 2) == []


# --------------------------------------------------------------------------
# 상태 파일
# --------------------------------------------------------------------------

def test_load_seen_roundtrip():
    scraper.save_seen(["1", "2", "3"])
    assert scraper.load_seen() == ["1", "2", "3"]


def test_save_seen_trims_to_max(monkeypatch):
    monkeypatch.setattr(scraper, "MAX_SEEN", 5)
    scraper.save_seen([str(i) for i in range(20)])
    kept = scraper.load_seen()
    assert kept == ["15", "16", "17", "18", "19"]


def test_load_seen_missing_file_is_empty():
    assert scraper.load_seen() == []


# --------------------------------------------------------------------------
# 마감 임박 리마인더
# --------------------------------------------------------------------------

def test_track_jobs_preconsumes_passed_thresholds(monkeypatch):
    """막 알린 공고가 같은 주기에 마감 임박으로 또 나가면 안 된다."""
    monkeypatch.setattr(scraper, "REMINDER_DAYS", [3, 1])
    tracked = scraper.track_jobs({}, [
        make_job(1, end_date="26.10.13", d_day=2),    # 이미 D-3 지남
        make_job(2, end_date="26.10.21", d_day=10),   # 아직 멀다
        make_job(3, end_date="26.10.11", d_day=0),    # D-DAY
    ])
    assert tracked["1"]["reminded"] == [3]
    assert tracked["2"]["reminded"] == []
    assert tracked["3"]["reminded"] == [3, 1]


def test_track_jobs_skips_undated_and_always_open():
    tracked = scraper.track_jobs({}, [
        make_job(1, end_date="26.10.23", d_day=12),
        make_job(2, always_open=True),
        make_job(3, d_day=None),
    ])
    assert set(tracked) == {"1"}
    assert tracked["1"]["reminded"] == []


def test_prune_tracked_drops_expired(monkeypatch):
    monkeypatch.setattr(scraper, "compute_d_day",
                        lambda e: {"live": 5, "gone": -1, "bad": None}[e])
    tracked = {
        "1": {"end_date": "live"},
        "2": {"end_date": "gone"},
        "3": {"end_date": "bad"},
        "4": "손상된 값",
    }
    assert set(scraper.prune_tracked(tracked)) == {"1"}


def test_load_tracked_survives_corrupt_file():
    scraper.TRACKED_FILE.write_text("nope", encoding="utf-8")
    assert scraper.load_tracked() == {}


def test_save_and_load_tracked_roundtrip():
    scraper.save_tracked({"1": {"end_date": "26.10.23", "reminded": [3]}})
    assert json.loads(scraper.TRACKED_FILE.read_text(encoding="utf-8"))["1"]["reminded"] == [3]


def test_configured_webhooks_parses_comma_list(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", " https://a/x/y/1 , ,https://b/x/y/2 ")
    assert scraper.configured_webhooks() == ["https://a/x/y/1", "https://b/x/y/2"]


def test_configured_webhooks_empty_when_unset(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert scraper.configured_webhooks() == []
