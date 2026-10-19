# HiBrain Job Alert

[hibrain.net](https://hibrain.net) 신규 채용공고를 긁어 Slack으로 알리는 GitHub Actions 봇.

하루 3회(09:07 / 14:07 / 19:07 KST) 실행된다. 이미 알린 공고는 `seen_jobs.json`에
기록해 다시 보내지 않는다.

```
목록 수집 → 신규 선별 → 마감 임박 순 정렬 → 상세 보강 → 키워드 필터
         → Slack 분할 전송 → 전달된 것만 seen 기록 → 마감 임박 리마인더
```

## 동작 원리

**목록 수집** — `ScraperAPI`(키가 있으면) → 실패 시 직접 요청으로 폴백하고, 각
방식마다 3회까지 재시도한다. 응답에 `articleList`가 없으면 차단/빈 응답으로 보고
실패 처리한다.

**상세 보강** — 목록에는 제목과 접수기간만 있다. 기관명·기관유형·직종은 상세
페이지의 schema.org `JobPosting` JSON-LD에서 가져온다. CSS 클래스보다 안정적이다.
베스트 에포트라 보강이 실패해도 제목·기간만으로 정상 발송된다. 로그인 없이는
본문이 렌더되지 않는 공고가 전체의 20~30%쯤 있어 이들은 보강되지 않는다.

**분할 전송** — Slack 첨부 하나에 블록 50개 제한이 있어 한 메시지에 공고 44건이
상한이다. 신규 공고가 많으면 여러 메시지로 쪼개 보낸다. 용량을 넘긴 공고는
**버리지 않고** `seen`에도 기록하지 않아 다음 주기에 이어서 알림된다.

**전달 보장** — 실제로 한 채널 이상에 전달된 공고만 `seen`에 기록한다. 전송에
실패한 묶음은 다음 주기에 재시도된다. 429/5xx는 `Retry-After`를 보고 재시도하고,
폐기된 웹훅처럼 영구적인 실패는 즉시 포기하고 경고만 남긴다.

## 설정

### 시크릿 (필수)

| 이름 | 설명 |
| --- | --- |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL. 콤마로 여러 개를 넣으면 모든 채널에 보낸다. |
| `SCRAPER_API_KEY` | (선택) ScraperAPI 키. 없으면 직접 요청만 쓴다. |

> 웹훅 토큰은 로그에 절대 찍히지 않는다. 에러 메시지와 예외 문자열에서도
> `.../B0A.../***` 형태로 가려진다. 여러 URL을 콤마로 합치면 GitHub의 시크릿
> 마스킹이 개별 URL에는 걸리지 않기 때문이다.

### 저장소 Variables (모두 선택)

`Settings → Secrets and variables → Actions → Variables`에서 코드 수정 없이 조절한다.

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `REMINDER_DAYS` | (없음) | 마감 임박 리마인더 기준. 예: `3,1` → D-3과 D-1에 각각 한 번. 비우면 기능 꺼짐. |
| `JOB_INCLUDE_KEYWORDS` | (없음) | 이 키워드 중 하나라도 들어간 공고만 알림. 제목·기관명·기관유형·직종을 대소문자 무시하고 검사. |
| `JOB_EXCLUDE_KEYWORDS` | (없음) | 이 키워드가 들어간 공고는 제외. include보다 우선한다. |
| `JOBS_PER_MESSAGE` | `20` | 한 메시지에 담을 공고 수. 44를 넘기면 자동으로 44로 조인다. |
| `MAX_MESSAGES` | `5` | 한 주기에 보낼 최대 메시지 수. 기본값은 주기당 100건. |
| `ENRICH` | `true` | `false`로 두면 상세 보강을 건너뛴다. (요청 수 절약) |

환경변수로만 조절하는 값: `FETCH_MAX_ATTEMPTS`, `FETCH_BACKOFF_BASE`,
`SCRAPERAPI_TIMEOUT`, `DIRECT_TIMEOUT`, `SLACK_TIMEOUT`, `SLACK_MAX_ATTEMPTS`,
`ENRICH_WORKERS`, `ENRICH_TIMEOUT`, `ENRICH_MAX`, `TRACK_LIMIT`.

### 마감 임박 리마인더

`REMINDER_DAYS`를 채우면 켜진다. 알림을 보낸 공고를 `tracked_jobs.json`에
기록해 두고, 마감이 가까워지면 별도 메시지(주황색)로 한 번 더 알린다.

- 기준별로 **한 번만** 보낸다. 실행이 밀려 기준일을 지나쳤어도 한 번은 보낸다.
- 신규 알림 시점에 이미 지난 기준은 소비 처리한다. D-2 공고가 같은 주기에
  신규 알림과 리마인더로 두 번 나가지 않는다.
- 전송에 실패하면 '보냄' 표시를 되돌려 다음 주기에 재시도한다.
- 마감이 지난 공고는 상태에서 자동으로 지워진다.
- 처음 켠 직후에는 이미 마감이 가까운 공고들이 한꺼번에 나갈 수 있다.

## 로컬 실행

```bash
pip install -r requirements-dev.txt

# 드라이런 — SLACK_WEBHOOK_URL이 없으면 전송하지 않고 메시지를 출력한다
python scraper.py

# 테스트
python -m pytest tests/ -v
```

테스트는 네트워크를 타지 않는다. 목록/상세 HTML은 `tests/fixtures/`의 실제 응답
조각을 쓰고 Slack 전송은 가짜 응답으로 대체한다.

수동 실행은 Actions → `Check hibrain.net Job Postings` → `Run workflow`.
`테스트 모드`를 켜면 신규 여부와 무관하게 샘플 공고를 보내고 상태를 바꾸지 않는다.

## 상태 파일

| 파일 | 내용 |
| --- | --- |
| `seen_jobs.json` | 이미 알린 공고 ID. 최근 2000건까지 유지. |
| `tracked_jobs.json` | 마감 리마인더 추적 상태. `REMINDER_DAYS`를 쓸 때만 생성된다. |

워크플로우가 매 실행 후 커밋한다. 깨진 JSON을 만나면 경고를 남기고 빈 상태로
진행한다. (실행이 멈추는 대신 중복 알림을 감수한다)
