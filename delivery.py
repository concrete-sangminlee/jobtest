"""Durable per-webhook outbox. No webhook credentials are written to disk."""
import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import requests

from fetcher import positive_int, retry_delay


def channel_id(url):
    return hashlib.sha256(url.encode()).hexdigest()


def webhook_urls():
    urls = list(dict.fromkeys(u.strip() for u in os.environ.get("SLACK_WEBHOOK_URL", "").split(",") if u.strip()))
    if not urls:
        raise ValueError("SLACK_WEBHOOK_URL이 없습니다. 읽기 전용 검증은 --dry-run을 사용하세요.")
    for url in urls:
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in {"hooks.slack.com", "hooks.slack-gov.com"} or not re.fullmatch(r"/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", p.path) or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
            raise ValueError("SLACK_WEBHOOK_URL에 올바른 Slack Incoming Webhook HTTPS 주소를 설정하세요.")
    return urls


def load_state(path):
    if not path.exists():
        return {"version": 2, "baseline": [], "channels": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, list) and all(isinstance(x, str) and x.isdigit() for x in state):
            return {"version": 2, "baseline": list(dict.fromkeys(state)), "channels": {}}
        assert isinstance(state, dict) and state["version"] == 2
        assert isinstance(state["baseline"], list) and all(isinstance(x, str) and x.isdigit() for x in state["baseline"])
        assert isinstance(state["channels"], dict)
        for key, channel in state["channels"].items():
            assert re.fullmatch(r"[0-9a-f]{64}", key)
            assert isinstance(channel["sent"], list) and all(isinstance(x, str) and x.isdigit() for x in channel["sent"])
            assert isinstance(channel["pending"], list)
            ids = set()
            for job in channel["pending"]:
                assert isinstance(job["id"], str) and job["id"].isdigit()
                assert isinstance(job["title"], str) and job["title"].strip()
                assert job["url"] == f"https://hibrain.net/recruitment/recruits/{job['id']}"
                assert job["id"] not in ids and job["id"] not in channel["sent"]
                ids.add(job["id"])
        return state
    except (ValueError, AssertionError, KeyError, TypeError):
        raise ValueError("전송 기록이 손상되었거나 지원하지 않는 형식입니다. 파일을 보존하고 복구하세요.") from None


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as f:
            name = f.name
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


@contextmanager
def state_lock(path):
    # Lock a byte on Windows and the whole file on POSIX. Never commit this file.
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            if f.tell() == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            acquire = lambda: msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (f.seek(0), msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(f, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise RuntimeError("다른 수집 프로세스가 실행 중입니다.") from None
        try:
            yield
        finally:
            release()


def post_message(url, message, log=print):
    attempts = positive_int("SLACK_MAX_ATTEMPTS", 3, 5)
    label = channel_id(url)[:8]
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, json=message, timeout=15, allow_redirects=False)
        except requests.RequestException as exc:
            log(f"채널 {label}: {type(exc).__name__}, 다음 실행에서 재시도합니다.")
            # Timeout can occur AFTER delivery. Avoid immediate duplicate retries.
            return False
        if response.status_code == 200 and response.text.strip() == "ok":
            return True
        body = response.text.strip()
        known = {"channel_is_archived", "channel_not_found", "no_service", "no_active_hooks", "no_team", "invalid_token", "action_prohibited", "invalid_payload"}
        reason = body if body in known else "응답 거부"
        log(f"채널 {label}: HTTP {response.status_code}, {reason}")
        if response.status_code != 429 and response.status_code < 500:
            return False
        delay = retry_delay(response.headers.get("Retry-After"), attempt)
        if delay is None or attempt == attempts:
            return False
        time.sleep(delay)
    return False


def deliver(jobs, urls, path, state, build_message, log=print):
    """Checkpoint the entire outbox before the first POST, then after each batch."""
    for url in urls:
        channel = state["channels"].setdefault(channel_id(url), {"sent": list(state["baseline"]), "pending": []})
        known = set(channel["sent"]) | {j["id"] for j in channel["pending"]}
        for job in jobs:
            if job["id"] not in known:
                channel["pending"].append(job)
                known.add(job["id"])
    save_state(path, state)
    delivered = failures = 0
    for url in urls:
        channel = state["channels"][channel_id(url)]
        while channel["pending"]:
            batch = channel["pending"][:20]
            if not post_message(url, build_message(batch), log):
                failures += 1
                break
            channel["sent"].extend(j["id"] for j in batch)
            del channel["pending"][:len(batch)]
            save_state(path, state)
            delivered += len(batch)
            log(f"채널 {channel_id(url)[:8]}: {len(batch)}건 전송 완료")
            if channel["pending"]:
                time.sleep(1)  # Slack recommends <= one message per second per channel.
    pending = sum(len(state["channels"][channel_id(u)]["pending"]) for u in urls)
    return {"delivered": delivered, "failed_channels": failures, "pending": pending}
