# HiBrain Job Alert

하이브레인 최신 채용 목록을 확인해 Slack으로 보내는 Python 3.12 작업입니다.
ScraperAPI나 유료 프록시 키를 사용하지 않습니다. 직접 HTTP 요청을 먼저 시도하고,
실패하면 Playwright Chromium으로 페이지를 읽습니다.

**사이트가 실행 서버의 IP를 차단하면 브라우저도 실패할 수 있습니다.**
무료 브라우저 실행은 접속 성공을 보장하지 않습니다. 이 경우 아래의 개인 기기 실행을
사용하거나 사이트 운영자에게 자동 수집 허용 여부를 문의해야 합니다.

## 설치·검증

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m unittest discover -s tests -v
python scraper.py --dry-run --report run_summary.json
```

`--dry-run`은 실제 수집과 모든 메시지 묶음 생성을 검증하며, Slack 전송이나 상태 변경을 하지 않습니다.
수집 실패·빈 목록·구조 변경은 종료 코드 1입니다. 신규 공고가 없어도 정상 수집이면 성공입니다.
실행 결과는 콘솔, 지정한 JSON 파일, GitHub Actions Summary에서 확인할 수 있습니다.

## GitHub Actions 운영

1. 저장소 Actions secret `SLACK_WEBHOOK_URL`에 Incoming Webhook을 등록합니다. 여러 채널은 쉼표로 구분합니다.
2. `Check hibrain.net Job Postings`를 수동 실행합니다. 기본 `dry_run=true`로 접속을 먼저 확인합니다.
3. 검증 후 `dry_run=false`로 실행하면 실제 알림을 보냅니다. 예약 실행은 KST 09:07·14:07·19:07입니다.
   GitHub 스케줄은 지연·누락될 수 있어 정확한 시각을 보장하지 않습니다.
4. 기존 `SCRAPER_API_KEY`는 더 이상 읽지 않습니다. 해당 secret은 삭제해도 됩니다.
5. 실패를 놓치지 않도록 GitHub 개인 알림 설정에서 Actions 실패 알림을 활성화하세요.

수동 검증의 `runner`에서 `ubuntu-latest` 또는 `macos-latest`를 선택해 실행 위치별 접근 여부를
확인할 수 있습니다. 성공한 환경을 예약 실행에 쓰려면 `JOB_RUNNER`를 JSON 문자열
`"macos-latest"`처럼 설정하세요. `configured`는 기존 저장소 설정을 사용합니다.
공개 저장소의 표준 GitHub-hosted runner는 무료입니다. 비공개 저장소는 사용 한도/요금을 확인하세요.

상태 저장에는 저장소 Actions의 `contents: write` 권한과 봇의 브랜치 쓰기 권한이 필요합니다.
브랜치 보호로 push가 거절되면 작업이 실패하며 복구용 artifact가 남습니다.
개발 브랜치에서는 `dry_run=true`만 사용하세요. 실제 전송은 운영 브랜치에서만 실행하세요.

## GitHub 서버가 차단될 때: 개인 PC/NAS

Linux/macOS에서 동일한 설치 후 `SLACK_WEBHOOK_URL`을 안전하게 환경에 등록하고 예약 실행합니다.
GitHub와 로컬을 동시에 운영하지 마세요. 운영 상태 `seen_jobs.json`을 옮겨 중복을 방지하세요.
실행 시간에 기기가 켜져 있어야 합니다. 먼저 `--dry-run`으로 해당 회선의 접근을 확인하세요.

예: Linux cron (아래는 **서버 시간대가 Asia/Seoul**인 경우)

```cron
7 9,14,19 * * * /path/to/jobtest/.venv/bin/python /path/to/jobtest/scraper.py >> /path/to/jobtest/scraper.log 2>&1
```

cron은 로그인 셸의 환경을 자동으로 읽지 않습니다. 웹훅은 권한 600의 별도 설정을 읽는 실행 스크립트
또는 OS 서비스 환경 설정으로 주입하세요. 저장소나 crontab에 웹훅을 직접 적지 마세요.
GitHub 스케줄을 유지하려면 전용 기기에 self-hosted runner를 설치하고 저장소 변수 `JOB_RUNNER`를
`["self-hosted","linux","x64","jobtest"]`로 설정할 수도 있습니다.
공개 저장소 PR은 개인 runner에서 실행하지 말고, 전용 계정·전용 기기를 사용하세요.

## 전송 기록과 복구

- 기존 `seen_jobs.json` 배열은 첫 실제 실행에서 version 2 형식으로 자동 전환됩니다. 기존 ID는 재전송하지 않습니다.
- 채널별로 공고를 보관하고 20건씩 전송합니다. 모든 채널의 대기 목록을 먼저 저장하고 각 묶음 성공 직후 다시 저장합니다.
- 실패한 채널의 공고는 다음 실행에서 재시도합니다. 이후 사이트 목록에서 사라져도 이미 수집한 대기 공고는 유지됩니다.
- 채널 일부가 실패해도 성공한 채널의 기록은 저장하고 작업은 실패로 표시합니다.
- 전송 ID를 임의로 500개에서 잘라내지 않습니다. 원자적 파일 교체와 프로세스 잠금으로 상태를 보호합니다.
- 429/5xx는 제한된 재시도를 합니다. `Retry-After`가 60초보다 길면 다음 실행으로 미룹니다.
- Slack 수신 직후 프로세스가 죽거나 응답이 유실되면 중복이 생길 수 있습니다. 웹훅에는 중복 방지 키가 없어 정확히 한 번 전송은 보장하지 않습니다.
- 웹훅 변경은 새 채널로 취급되어 전환 이후 공고가 재전송될 수 있습니다. 교체 시 이전 채널의 상태를 보존·이관하세요.
- 실패한 상태 push는 Actions artifact의 `seen_jobs.json`으로 복구할 수 있습니다. 최신 로컬 상태를 먼저 백업하고 복구하세요.

수집 범위는 기존과 같은 **최신 목록 첫 페이지**입니다. 페이지 밖으로 밀려난 미수집 공고와
오래 중단된 기간의 모든 공고를 소급 복구하지는 못합니다. 기존 기록에서 이미 잘려나간 ID도 복구할 수 없습니다.
주기와 수집 범위는 필요한 공고량에 맞춰 점검하세요.

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | 없음 | 실제 전송에 필수; dry-run에서는 필요 없음 |
| `FETCH_MODE` | `auto` | `auto`, `http`, `browser` |
| `FETCH_MAX_ATTEMPTS` | `3` | 직접 요청 최대 횟수, 1~5 |
| `DIRECT_TIMEOUT` | `30` | HTTP 제한 초, 1~120 |
| `BROWSER_TIMEOUT` | `45` | 브라우저 탐색/목록 대기 각각의 제한 초, 1~120 |
| `BROWSER_EXECUTABLE_PATH` | Playwright Chromium | 이미 설치한 Chromium 경로 |
| `SLACK_MAX_ATTEMPTS` | `3` | 429/5xx 최대 시도 횟수, 1~5 |

HTTPS 인증서 검증은 유지합니다. 관리형 프록시 환경에서는 브라우저도 프록시 CA를 신뢰하도록 환경을 구성해야 합니다.
`run_summary.json`과 잠금 파일은 Git에서 제외됩니다. 웹훅 주소나 토큰은 로그·상태 파일에 저장하지 않습니다.
