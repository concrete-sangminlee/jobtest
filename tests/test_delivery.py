"""main()을 처음부터 끝까지 돌리는 테스트.

사이트 수집과 Slack 전송은 가짜로 대체한다. 네트워크는 쓰지 않는다.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper  # noqa: E402

A = "https://hooks.slack.com/services/T0/BAAA/tokenA"
B = "https://hooks.slack.com/services/T0/BBBB/tokenB"
TODAY = datetime(2026, 11, 12, tzinfo=scraper.KST)


class Resp:
    def __init__(self, status=200, text="ok", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class FakeSlack:
    """웹훅별 응답을 지정할 수 있는 가짜 Slack. 보낸 메시지를 기록한다."""

    def __init__(self):
        self.posts = []          # (url, message)
        self.script = {}         # url -> 응답 목록(앞에서부터 소비) 또는 함수
        self.on_post = None

    def __call__(self, url, json=None, **_kw):
        if self.on_post:
            self.on_post(url, json)
        self.posts.append((url, json))
        rule = self.script.get(url)
        if callable(rule):
            return rule(json)
        if rule:
            r = rule.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return Resp()

    def to(self, url):
        return [m for u, m in self.posts if u == url]

    def ids_to(self, url):
        out = []
        for m in self.to(url):
            for b in m["attachments"][0]["blocks"]:
                if b.get("type") == "section" and "accessory" in b:
                    out.append(b["accessory"]["url"].rsplit("/", 1)[-1])
        return out


def job(i, **kw):
    j = {
        "id": str(i), "title": f"공고 {i}", "period": "26.11.10 ~ 26.11.30",
        "start_date": "26.11.10", "end_date": "26.11.30", "always_open": False,
        "d_day": 18, "org": "", "industry": "", "employment_type": "", "summary": "",
        "url": f"https://hibrain.net/recruitment/recruits/{i}",
    }
    j.update(kw)
    return j


@pytest.fixture
def env(monkeypatch, tmp_path):
    """상태 파일을 임시 폴더로, 시간과 네트워크를 고정한다."""
    for name in ("SEEN_FILE", "TRACKED_FILE", "OUTBOX_FILE", "HEALTH_FILE"):
        monkeypatch.setattr(scraper, name, tmp_path / getattr(scraper, name).name)
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    monkeypatch.setattr(scraper, "_today_kst", lambda: TODAY)
    monkeypatch.setattr(scraper, "ENRICH", False)
    monkeypatch.setattr(scraper, "REMINDER_DAYS", [])
    for var in ("SLACK_WEBHOOK_URL", "SLACK_ALERT_WEBHOOK_URL", "SCRAPER_API_KEY", "TEST_MODE",
                "RUN_MODE", "GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY",
                "JOB_INCLUDE_KEYWORDS", "JOB_EXCLUDE_KEYWORDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", A)

    slack = FakeSlack()
    monkeypatch.setattr(scraper.requests, "post", slack)

    state = {"jobs": [], "error": None, "calls": 0}

    def fake_scrape():
        state["calls"] += 1
        if state["error"]:
            raise scraper.FetchError(state["error"])
        return [dict(j) for j in state["jobs"]]

    monkeypatch.setattr(scraper, "scrape_jobs", fake_scrape)

    def guard(*_a, **_k):
        raise AssertionError("테스트 중 실제 네트워크 요청")

    monkeypatch.setattr(scraper.requests, "get", guard)

    class Env:
        pass

    e = Env()
    e.slack, e.site, e.dir, e.mp = slack, state, tmp_path, monkeypatch
    e.seen = lambda: json.loads((tmp_path / "seen_jobs.json").read_text(encoding="utf-8"))
    e.outbox = lambda: json.loads((tmp_path / "outbox.json").read_text(encoding="utf-8"))
    e.health = lambda: json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    e.run = lambda *args: scraper.main(list(args))
    return e


# --------------------------------------------------------------------------
# 무음 장애 방지
# --------------------------------------------------------------------------

def test_fetch_failure_fails_the_run(env, capsys):
    env.site["error"] = "사이트가 이 실행 환경(GitHub Actions)의 접속을 거부함(403)."
    env.mp.setenv("GITHUB_ACTIONS", "true")
    assert env.run() == 1
    out = capsys.readouterr().out
    assert "::error" in out and "403" in out
    assert env.health()["consecutive_failures"] == 1


def test_repeated_fetch_failure_alerts_once_then_daily(env, monkeypatch):
    env.site["error"] = "차단(403)"
    monkeypatch.setattr(scraper, "HEALTH_ALERT_AFTER", 2)

    assert env.run() == 1
    assert env.slack.posts == [], "첫 실패에서는 아직 경보하지 않는다 (일시적 오류일 수 있음)"

    assert env.run() == 1
    assert len(env.slack.posts) == 1
    text = env.slack.posts[0][1]["attachments"][0]["blocks"][0]["text"]["text"]
    assert "2회 연속 실패" in text and "차단(403)" in text

    assert env.run() == 1
    assert len(env.slack.posts) == 1, "같은 날 경보를 반복하면 안 된다"

    h = env.health()
    h["last_alert"] = "2000-01-01T00:00+09:00"
    (env.dir / "health.json").write_text(json.dumps(h), encoding="utf-8")
    assert env.run() == 1
    assert len(env.slack.posts) == 2, "하루가 지나면 다시 알린다"


def test_recovery_is_announced_once(env, monkeypatch):
    monkeypatch.setattr(scraper, "HEALTH_ALERT_AFTER", 1)
    env.site["error"] = "차단"
    env.run()
    assert len(env.slack.posts) == 1

    env.site["error"] = None
    env.site["jobs"] = [job(1)]
    assert env.run() == 0
    texts = [m["attachments"][0]["blocks"][0]["text"]["text"] for _, m in env.slack.posts]
    assert any("복구" in t for t in texts)
    assert env.health()["consecutive_failures"] == 0

    env.slack.posts.clear()
    env.run()
    assert not any("복구" in json.dumps(m, ensure_ascii=False) for _, m in env.slack.posts)


def test_failed_alert_is_retried_next_run(env, monkeypatch):
    monkeypatch.setattr(scraper, "HEALTH_ALERT_AFTER", 1)
    env.site["error"] = "차단"
    env.slack.script[A] = [Resp(500, "")] * 3
    assert env.run() == 1
    assert env.health()["alerted"] is False
    env.run()
    assert env.health()["alerted"] is True


def test_separate_alert_channel(env, monkeypatch):
    monkeypatch.setattr(scraper, "HEALTH_ALERT_AFTER", 1)
    monkeypatch.setenv("SLACK_ALERT_WEBHOOK_URL", B)
    env.site["error"] = "차단"
    env.run()
    assert env.slack.to(A) == [] and len(env.slack.to(B)) == 1


def test_missing_webhook_fails_before_touching_the_site(env, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    assert env.run() == 1
    assert env.site["calls"] == 0


def test_failure_reported_output(env, monkeypatch, tmp_path):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    env.site["error"] = "차단"
    env.run()
    assert "failure_reported=true" in out.read_text()


def test_step_summary_and_report(env, monkeypatch, tmp_path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    env.site["jobs"] = [job(1), job(2)]
    report = tmp_path / "run_summary.json"
    assert env.run("--report", str(report)) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["ok"] and data["collected"] == 2 and data["new"] == 2
    assert "HiBrain Job Alert" in summary.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 상태 파일
# --------------------------------------------------------------------------

def test_corrupt_seen_stops_instead_of_mass_renotifying(env):
    (env.dir / "seen_jobs.json").write_text('["1", "2"', encoding="utf-8")
    env.site["jobs"] = [job(i) for i in range(50)]
    assert env.run() == 1
    assert env.slack.posts == []
    assert (env.dir / "seen_jobs.json").read_text(encoding="utf-8") == '["1", "2"', "손상 파일을 덮어썼다"


def test_seen_file_stays_a_plain_list(env):
    """이전 버전 코드도 읽을 수 있어야 한다 (롤백 호환)."""
    (env.dir / "seen_jobs.json").write_text('["900", "901"]', encoding="utf-8")
    env.site["jobs"] = [job(1), job(900)]
    assert env.run() == 0
    seen = env.seen()
    assert isinstance(seen, list) and seen == ["900", "901", "1"]


def test_corrupt_outbox_is_reported_but_does_not_block(env):
    (env.dir / "outbox.json").write_text("nope", encoding="utf-8")
    env.site["jobs"] = [job(1)]
    assert env.run() == 1, "손상은 실패로 보고해야 한다"
    assert env.slack.ids_to(A) == ["1"], "그래도 알림은 나가야 한다"


def test_atomic_write_keeps_old_file_on_failure(env, monkeypatch):
    target = env.dir / "seen_jobs.json"
    target.write_text('["1"]', encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(scraper.os, "replace", boom)
    with pytest.raises(OSError):
        scraper.save_seen(["1", "2"])
    assert target.read_text(encoding="utf-8") == '["1"]'
    assert [p.name for p in env.dir.iterdir() if p.name.endswith(".tmp")] == []


# --------------------------------------------------------------------------
# 전달 보장
# --------------------------------------------------------------------------

def test_new_jobs_are_delivered_and_recorded(env):
    env.site["jobs"] = [job(1), job(2)]
    assert env.run() == 0
    assert sorted(env.slack.ids_to(A)) == ["1", "2"]
    assert sorted(env.seen()) == ["1", "2"]
    assert env.outbox()["channels"][scraper.channel_key(A)]["pending"] == []

    env.slack.posts.clear()
    assert env.run() == 0
    assert env.slack.posts == [], "같은 공고를 다시 보냈다"


def test_partial_channel_failure_is_recovered_without_duplicates(env, monkeypatch):
    """한 채널만 실패해도 그 채널의 몫이 사라지지 않는다. (이전 코드의 버그)"""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A},{B}")
    env.site["jobs"] = [job(1)]
    env.slack.script[B] = [Resp(500, "")] * 3
    assert env.run() == 1
    assert env.slack.ids_to(A) == ["1"]
    assert env.seen() == ["1"]

    # 다음 실행: 공고는 목록에서 사라졌고 수집도 실패한다. 그래도 B에 보내야 한다.
    env.slack.posts.clear()
    env.site["jobs"], env.site["error"] = [], "차단"
    env.run()
    assert env.slack.ids_to(B) == ["1"]
    assert env.slack.ids_to(A) == [], "A에 중복 전송"


def test_overflow_is_deferred_not_dropped(env, monkeypatch):
    monkeypatch.setattr(scraper, "JOBS_PER_MESSAGE", 10)
    monkeypatch.setattr(scraper, "MAX_MESSAGES", 2)
    env.site["jobs"] = [job(i) for i in range(1, 36)]

    assert env.run() == 0
    first = env.slack.ids_to(A)
    assert len(first) == 20 and len(env.slack.to(A)) == 2
    assert len(env.seen()) == 20

    env.slack.posts.clear()
    env.run()
    second = env.slack.ids_to(A)
    assert len(second) == 15 and not set(first) & set(second)
    assert len(env.seen()) == 35


def test_backlog_is_capped_per_run(env, monkeypatch):
    """채널이 오래 죽었다 살아나도 한 실행에 MAX_MESSAGES개까지만 보낸다."""
    monkeypatch.setattr(scraper, "JOBS_PER_MESSAGE", 5)
    monkeypatch.setattr(scraper, "MAX_MESSAGES", 2)
    env.site["jobs"] = [job(i) for i in range(1, 11)]
    env.slack.script[A] = [Resp(500, "")] * 3
    env.run()
    env.slack.posts.clear()
    env.site["jobs"] = []
    env.site["error"] = "차단"
    env.run()
    assert len(env.slack.to(A)) == 2, "한 실행 메시지 상한을 넘었다"
    assert len(env.slack.ids_to(A)) == 10


def test_state_is_checkpointed_before_first_post(env):
    env.site["jobs"] = [job(1)]
    seen_at_post = []

    def look(url, msg):
        ob = json.loads((env.dir / "outbox.json").read_text(encoding="utf-8"))
        seen_at_post.append([it["id"] for it in ob["channels"][scraper.channel_key(url)]["pending"]])

    env.slack.on_post = look
    env.run()
    assert seen_at_post == [["1"]], "첫 전송 전에 대기열이 저장되지 않았다"


def test_crash_after_first_batch_keeps_delivered_batch(env, monkeypatch):
    monkeypatch.setattr(scraper, "JOBS_PER_MESSAGE", 2)
    env.site["jobs"] = [job(i) for i in range(1, 5)]
    calls = {"n": 0}

    def crash_second(url, msg):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("runner killed")

    env.slack.on_post = crash_second
    with pytest.raises(KeyboardInterrupt):
        env.run()
    first = env.seen()
    assert len(first) == 2, "전달된 첫 묶음이 기록되지 않았다"

    env.slack.on_post = None
    env.slack.posts.clear()
    env.run()
    assert sorted(env.slack.ids_to(A)) == sorted({"1", "2", "3", "4"} - set(first)), "누락 또는 중복"


def test_removed_channel_queue_is_cleaned(env, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A},{B}")
    env.site["jobs"] = [job(1)]
    env.slack.script[B] = [Resp(500, "")] * 3
    env.run()
    assert scraper.channel_key(B) in env.outbox()["channels"]
    monkeypatch.setenv("SLACK_WEBHOOK_URL", A)
    env.run()
    assert scraper.channel_key(B) not in env.outbox()["channels"]


def test_new_channel_does_not_get_a_flood(env, monkeypatch):
    env.site["jobs"] = [job(i) for i in range(1, 6)]
    env.run()
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A},{B}")
    env.slack.posts.clear()
    env.run()
    assert env.slack.to(B) == [], "새 채널에 지난 공고를 전부 다시 보냈다"


def test_stale_queue_items_expire(env, monkeypatch):
    monkeypatch.setattr(scraper, "OUTBOX_TTL_DAYS", 3)
    ob = {"version": 1, "channels": {scraper.channel_key(A): {"pending": [
        {"kind": "new", "id": "1", "queued": "2026-11-01", "job": {"title": "오래됨", "url": job(1)["url"]}},
        {"kind": "new", "id": "2", "queued": "2026-11-12",
         "job": {"title": "마감 지남", "url": job(2)["url"], "end_date": "26.11.05"}},
        {"kind": "new", "id": "3", "queued": "2026-11-12", "job": {"title": "정상", "url": job(3)["url"]}},
    ]}}}
    (env.dir / "outbox.json").write_text(json.dumps(ob), encoding="utf-8")
    env.site["error"] = "차단"
    env.run()
    assert env.slack.ids_to(A) == ["3"]
    assert set(env.seen()) == {"1", "2", "3"}, "포기한 공고도 다시 찾지 않게 기록한다"


def test_duplicate_webhooks_are_posted_once(env, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A}, {A}")
    env.site["jobs"] = [job(1)]
    env.run()
    assert len(env.slack.posts) == 1


# --------------------------------------------------------------------------
# Slack 응답 처리
# --------------------------------------------------------------------------

def test_dead_webhook_fails_run_but_other_channel_is_served(env, monkeypatch, capsys):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A},{B}")
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [Resp(404, "no_active_hooks")]
    assert env.run() == 1
    assert env.slack.ids_to(B) == ["1"]
    assert len(env.slack.to(A)) == 1, "죽은 웹훅을 재시도했다"
    assert "SLACK_WEBHOOK_URL" in capsys.readouterr().out


def test_payload_rejection_is_not_called_a_dead_webhook():
    hint, _ = scraper.explain_slack_error(400, "invalid_blocks")
    assert "제거" not in hint and "교체" not in hint
    assert scraper.classify_slack_response(400, "invalid_blocks") == "rejected"
    assert scraper.classify_slack_response(404, "no_active_hooks") == "dead"
    assert scraper.classify_slack_response(503, "") == "transient"
    assert scraper.classify_slack_response(200, "ok") == "ok"
    assert scraper.classify_slack_response(200, "") == "transient", "200이라도 본문이 ok가 아니면 성공이 아니다"


def test_rejected_job_is_isolated(env, monkeypatch):
    """한 공고 때문에 거부되면 그 공고만 건너뛰고 나머지는 보낸다."""
    monkeypatch.setattr(scraper, "JOBS_PER_MESSAGE", 5)
    env.site["jobs"] = [job(1), job(2, title="문제 공고"), job(3)]

    def reject_bad(msg):
        return Resp(400, "invalid_blocks") if "문제 공고" in json.dumps(msg, ensure_ascii=False) else Resp()

    env.slack.script[A] = reject_bad
    assert env.run() == 1
    singles = [m for m in env.slack.to(A)
               if sum(1 for b in m["attachments"][0]["blocks"] if "accessory" in b) == 1]
    single_ids = sorted(b["accessory"]["url"].rsplit("/", 1)[-1]
                        for m in singles for b in m["attachments"][0]["blocks"] if "accessory" in b)
    assert single_ids == ["1", "2", "3"], "한 건씩 다시 보내 문제 공고를 찾지 않았다"
    assert set(env.seen()) == {"1", "2", "3"}
    assert env.outbox()["channels"][scraper.channel_key(A)]["pending"] == [], "대기열이 막혔다"


def test_read_timeout_is_not_retried_in_run(env):
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [scraper.requests.ReadTimeout("slow")]
    assert env.run() == 1
    assert len(env.slack.posts) == 1, "이미 전달됐을 수 있는 메시지를 바로 다시 보냈다"
    assert [it["id"] for it in env.outbox()["channels"][scraper.channel_key(A)]["pending"]] == ["1"]
    assert not (env.dir / "seen_jobs.json").exists(), "전달 안 된 공고를 seen에 기록했다"


def test_connection_error_is_retried(env):
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [scraper.requests.ConnectionError("dns"), Resp()]
    assert env.run() == 0
    assert env.seen() == ["1"]


def test_rate_limit_respects_retry_after(env, monkeypatch):
    waits = []
    monkeypatch.setattr(scraper.time, "sleep", waits.append)
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [Resp(429, "rate_limited", {"Retry-After": "7"}), Resp()]
    assert env.run() == 0
    assert 7 in waits


def test_long_retry_after_defers_to_next_run(env, monkeypatch):
    waits = []
    monkeypatch.setattr(scraper.time, "sleep", waits.append)
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [Resp(429, "rate_limited", {"Retry-After": "3600"})]
    assert env.run() == 1
    assert max(waits, default=0) <= scraper.SLACK_MAX_WAIT
    assert len(env.slack.posts) == 1


def test_secret_never_reaches_logs(env, capsys, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{A},{B}")
    env.site["jobs"] = [job(1)]
    env.slack.script[A] = [Resp(404, "no_service")]
    env.slack.script[B] = [scraper.requests.ConnectionError(f"Max retries exceeded with url: {B}")] * 3
    env.run()
    out = capsys.readouterr().out
    assert "tokenA" not in out and "tokenB" not in out


# --------------------------------------------------------------------------
# 리마인더
# --------------------------------------------------------------------------

def test_reminders_go_out_even_when_fetch_fails(env, monkeypatch):
    monkeypatch.setattr(scraper, "REMINDER_DAYS", [3, 1])
    tracked = {"77": {"title": "마감 임박", "url": job(77)["url"], "end_date": "26.11.13",
                      "period": "", "reminded": [3]}}
    (env.dir / "tracked_jobs.json").write_text(json.dumps(tracked), encoding="utf-8")
    env.site["error"] = "차단"
    env.run()
    assert env.slack.ids_to(A) == ["77"]
    msg = env.slack.to(A)[0]
    assert "마감 임박" in json.dumps(msg, ensure_ascii=False)


def test_reminder_consumes_all_passed_thresholds_at_once(env, monkeypatch):
    """D-1에 처음 확인된 공고가 이틀 연속 알림되면 안 된다. (이전 코드의 버그)"""
    monkeypatch.setattr(scraper, "REMINDER_DAYS", [3, 1])
    tracked = {"77": {"title": "t", "url": job(77)["url"], "end_date": "26.11.13", "reminded": []}}
    (env.dir / "tracked_jobs.json").write_text(json.dumps(tracked), encoding="utf-8")
    env.site["jobs"] = []
    env.site["error"] = "차단"
    env.run()
    env.run()
    assert env.slack.ids_to(A).count("77") == 1


def test_just_announced_job_is_not_reminded_same_run(env, monkeypatch):
    monkeypatch.setattr(scraper, "REMINDER_DAYS", [3, 1])
    env.site["jobs"] = [job(5, end_date="26.11.14", period="26.11.01 ~ 26.11.14", d_day=2)]
    env.run()
    assert env.slack.ids_to(A).count("5") == 1


def test_reminders_off_creates_no_tracked_file(env):
    env.site["jobs"] = [job(1)]
    env.run()
    assert not (env.dir / "tracked_jobs.json").exists()


# --------------------------------------------------------------------------
# 실행 모드
# --------------------------------------------------------------------------

def test_dry_run_sends_nothing_and_writes_nothing(env, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    env.site["jobs"] = [job(1), job(2)]
    assert env.run("--mode", "dry-run") == 0
    assert env.slack.posts == []
    assert sorted(p.name for p in env.dir.iterdir()) == []


def test_dry_run_reports_fetch_failure(env):
    env.site["error"] = "차단"
    assert env.run("--mode", "dry-run") == 1
    assert env.slack.posts == []


def test_test_message_mode_sends_sample_without_state(env):
    env.site["jobs"] = [job(i) for i in range(1, 30)]
    assert env.run("--mode", "test-message") == 0
    assert len(env.slack.posts) == 1
    assert "[테스트]" in json.dumps(env.slack.posts[0][1], ensure_ascii=False)
    assert not (env.dir / "seen_jobs.json").exists()


def test_legacy_test_mode_env_maps_to_test_message(env, monkeypatch):
    monkeypatch.setenv("TEST_MODE", "true")
    env.site["jobs"] = [job(1)]
    assert env.run() == 0
    assert not (env.dir / "seen_jobs.json").exists()


def test_filters_record_excluded_jobs_without_sending(env, monkeypatch):
    monkeypatch.setenv("JOB_EXCLUDE_KEYWORDS", "제외")
    env.site["jobs"] = [job(1, title="제외 대상"), job(2)]
    assert env.run() == 0
    assert env.slack.ids_to(A) == ["2"]
    assert set(env.seen()) == {"1", "2"}


# --------------------------------------------------------------------------
# 메시지 안전성
# --------------------------------------------------------------------------

def test_titles_are_escaped_for_mrkdwn():
    block = scraper._job_block(job(9, title="A&B <!channel> 교수 >초빙< | x", org="<기관>",
                                   period="26.11.01 ~ `26.11.30`"))
    text = block["text"]["text"]
    assert "<!channel>" not in text
    assert "&amp;B &lt;!channel&gt;" in text
    link = text.split("\n")[0]
    assert link.count("<") == 1 and link.count(">") == 2   # 인용 표시 '>' 하나 + 링크 닫기 하나
    assert "&lt;기관&gt;" in text
    assert "``" not in text and text.count("`") % 2 == 0


def test_long_title_stays_under_section_limit():
    text = scraper._job_block(job(9, title="가" * 5000, org="나" * 5000))["text"]["text"]
    assert len(text) < 3000


def test_message_has_notification_fallback():
    msg = scraper.build_slack_message([job(1), job(2)])
    assert msg["attachments"][0]["fallback"].endswith("2건")


def test_block_limit_holds_even_with_huge_jobs_per_message(env, monkeypatch):
    monkeypatch.setattr(scraper, "JOBS_PER_MESSAGE", 500)
    monkeypatch.setattr(scraper, "MAX_MESSAGES", 5)
    env.site["jobs"] = [job(i) for i in range(1, 101)]
    env.run()
    assert env.slack.posts
    assert max(len(m["attachments"][0]["blocks"]) for _, m in env.slack.posts) <= scraper.SLACK_MAX_BLOCKS


def test_workflow_command_text_is_escaped(capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    scraper.warn("첫 줄\n::error::가짜 명령 100%")
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and "%0A" in out and "%25" in out


# --------------------------------------------------------------------------
# 수집
# --------------------------------------------------------------------------

class HTTPResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise scraper.requests.HTTPError(f"{self.status_code}", response=self)


def test_permanent_fetch_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    monkeypatch.setenv("SCRAPER_API_KEY", "secret-key")
    calls = []

    def get(url, **_k):
        calls.append(url)
        return HTTPResp(401) if "scraperapi" in url else HTTPResp(403)

    monkeypatch.setattr(scraper.requests, "get", get)
    with pytest.raises(scraper.FetchError) as e:
        scraper.fetch_page()
    assert len(calls) == 2, "401/403을 재시도했다"
    msg = str(e.value)
    assert "SCRAPER_API_KEY" in msg and "403" in msg
    assert "secret-key" not in msg


def test_transient_fetch_errors_are_retried(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    monkeypatch.delenv("SCRAPER_API_KEY", raising=False)
    seq = [HTTPResp(502), HTTPResp(200, '<ul id="articleList"></ul>')]
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: seq.pop(0))
    assert 'articleList' in scraper.fetch_page()


def test_api_key_is_hidden_in_network_errors(monkeypatch, capsys):
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    monkeypatch.setenv("SCRAPER_API_KEY", "secret-key")

    def get(url, **_k):
        raise scraper.requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(scraper.requests, "get", get)
    with pytest.raises(scraper.FetchError):
        scraper.fetch_page()
    assert "secret-key" not in capsys.readouterr().out


def test_zero_parsed_rows_is_a_failure(monkeypatch):
    monkeypatch.setattr(scraper, "fetch_page", lambda: '<ul id="articleList"></ul>')
    with pytest.raises(scraper.FetchError):
        scraper.scrape_jobs()


def test_enrichment_stops_when_detail_pages_are_blocked(monkeypatch):
    monkeypatch.setattr(scraper, "ENRICH", True)
    calls = []

    def fetcher(i):
        calls.append(i)
        return scraper.BLOCKED

    assert scraper.enrich_jobs([job(i) for i in range(10)], fetcher=fetcher) == 0
    assert len(calls) == 1, "차단된 뒤에도 상세 요청을 계속 보냈다"


def test_fetch_job_detail_flags_blocking(monkeypatch):
    monkeypatch.setattr(scraper.requests, "get", lambda *a, **k: HTTPResp(403))
    assert scraper.fetch_job_detail("1") is scraper.BLOCKED


def test_enqueue_does_not_duplicate_pending_items():
    ob = {"version": 1, "channels": {}}
    scraper.enqueue(ob, [A], [job(1), job(2)], "new", "2026-11-12")
    scraper.enqueue(ob, [A], [job(1)], "new", "2026-11-12")
    scraper.enqueue(ob, [A], [job(1)], "reminder", "2026-11-12")   # 종류가 다르면 별개
    items = ob["channels"][scraper.channel_key(A)]["pending"]
    assert [(it["kind"], it["id"]) for it in items] == [("new", "1"), ("new", "2"), ("reminder", "1")]


def test_already_closed_jobs_are_recorded_but_not_announced(env):
    env.site["jobs"] = [job(1, end_date="26.11.05", d_day=-7), job(2)]
    assert env.run() == 0
    assert env.slack.ids_to(A) == ["2"]
    assert set(env.seen()) == {"1", "2"}
