"""하이브레인 채용 알림용 이미지 카드 렌더러.

채용공고 목록을 세련된 HTML 카드로 만들고, Playwright(Chromium)로
PNG 이미지를 렌더링한다. Slack에는 이 이미지를 업로드해 붙인다.

의존성(런타임): playwright + chromium. 폰트는 시스템 CJK 폰트(fonts-noto-cjk)를
사용하고, 네트워크가 되면 Pretendard 웹폰트를 추가로 적용한다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 카드 렌더링 폭(px). devicePixelRatio=2로 찍어 레티나급 선명도를 얻는다.
CARD_WIDTH = 900
DEVICE_SCALE = 2

# 아이콘(SVG path). 색상은 currentColor 대신 고정값을 넣어 폰트 독립적으로 렌더.
_IC_BUILDING = (
    '<svg class="icon" viewBox="0 0 24 24" fill="none">'
    '<path d="M3 21V7l7-4v18M10 21V9l8 4v8M3 21h18M6.5 10h0M6.5 14h0M14 13h0M14 17h0" '
    'stroke="#93a1ba" stroke-width="1.7" stroke-linecap="round"/></svg>'
)
_IC_PIN = (
    '<svg class="icon" viewBox="0 0 24 24" fill="none">'
    '<path d="M12 21s7-6.4 7-11a7 7 0 1 0-14 0c0 4.6 7 11 7 11Z" stroke="#93a1ba" stroke-width="1.7"/>'
    '<circle cx="12" cy="10" r="2.4" stroke="#93a1ba" stroke-width="1.7"/></svg>'
)
_IC_BRIEFCASE = (
    '<svg class="icon" viewBox="0 0 24 24" fill="none">'
    '<rect x="3" y="7" width="18" height="13" rx="2" stroke="#93a1ba" stroke-width="1.7"/>'
    '<path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke="#93a1ba" stroke-width="1.7"/></svg>'
)

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def _esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def dday_view(job: dict) -> tuple[str, str, str]:
    """(D-day 라벨, 색상 클래스, 마감일 텍스트)."""
    if job.get("always_open"):
        return "상시", "gray", "상시채용"
    d = job.get("d_day")
    end = job.get("end_date") or ""
    date_text = f"~ {end}" if end else ""
    if d is None:
        return "미정", "gray", date_text or "기간 미정"
    if d < 0:
        return "마감", "gray", date_text
    if d == 0:
        return "D-DAY", "red", date_text
    if d <= 3:
        return f"D-{d}", "red", date_text
    if d <= 7:
        return f"D-{d}", "amber", date_text
    return f"D-{d}", "green", date_text


def _pill(icon: str, text: str, limit: int) -> str:
    return f'<span class="pill">{icon}{_esc(_truncate(text, limit))}</span>'


def _job_row(job: dict, index: int) -> str:
    rank = _CIRCLED[index - 1] if 1 <= index <= len(_CIRCLED) else str(index)
    label, cls, date_text = dday_view(job)

    pills = []
    if job.get("company"):
        pills.append(_pill(_IC_BUILDING, job["company"], 44))
    if job.get("region"):
        pills.append(_pill(_IC_PIN, job["region"], 24))
    if job.get("job_type"):
        pills.append(_pill(_IC_BRIEFCASE, job["job_type"], 24))
    meta = f'<div class="meta">{"".join(pills)}</div>' if pills else ""

    date_html = f'<div class="date">{_esc(date_text)}</div>' if date_text else ""

    return (
        '<div class="job">'
        f'<div class="rank">{rank}</div>'
        '<div class="main">'
        f'<div class="jtitle">{_esc(_truncate(job.get("title", ""), 60))}</div>'
        f'{meta}'
        '</div>'
        '<div class="right">'
        f'<div class="dday {cls}">{_esc(label)}</div>'
        f'{date_html}'
        '</div>'
        '</div>'
    )


def build_card_html(jobs: list[dict], *, subtitle: str = "신규 채용공고", max_rows: int = 8) -> str:
    now = datetime.now(KST)
    ts_date = now.strftime("%Y.%m.%d")
    ts_time = now.strftime("%H:%M")
    total = len(jobs)
    rows = jobs[:max_rows]
    hidden = total - len(rows)

    urgent = sum(
        1 for j in rows
        if not j.get("always_open") and j.get("d_day") is not None and 0 <= j["d_day"] <= 3
    )

    kpi_hot = (
        f'<div class="kpi hot"><b>{urgent}</b><span>마감임박</span></div>' if urgent else ""
    )
    more = (
        f'<div class="more">+ {hidden}건의 신규 공고가 더 있습니다</div>'
        if hidden > 0 else '<div class="more">전체 공고를 확인해 보세요</div>'
    )

    rows_html = "".join(_job_row(j, i) for i, j in enumerate(rows, start=1))

    return _TEMPLATE.format(
        subtitle=_esc(subtitle),
        total=total,
        kpi_hot=kpi_hot,
        ts_date=ts_date,
        ts_time=ts_time,
        rows=rows_html,
        more=more,
    )


# 자기완결형 HTML (인라인 CSS). {..} 자리표시자만 포맷팅으로 치환.
_TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<style>
@font-face{{font-family:'Pretendard';font-weight:400;font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/packages/pretendard/dist/web/static/woff2/Pretendard-Regular.woff2') format('woff2');}}
@font-face{{font-family:'Pretendard';font-weight:700;font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/packages/pretendard/dist/web/static/woff2/Pretendard-Bold.woff2') format('woff2');}}
@font-face{{font-family:'Pretendard';font-weight:800;font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/packages/pretendard/dist/web/static/woff2/Pretendard-ExtraBold.woff2') format('woff2');}}
:root{{--card:#111a2e;--card2:#0d1424;--line:rgba(255,255,255,.07);--txt:#eaf0fb;--muted:#93a1ba}}
*{{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}}
body{{background:#0b1220}}
.wrap{{width:900px;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR','Apple SD Gothic Neo',system-ui,sans-serif}}
.card{{background:linear-gradient(180deg,var(--card),var(--card2));border:1px solid var(--line);
  border-radius:26px;overflow:hidden;box-shadow:0 30px 70px -20px rgba(0,0,0,.7)}}
.icon{{width:15px;height:15px;display:inline-block;vertical-align:-2px;flex:none}}
.hero{{position:relative;padding:32px 36px 28px;background:
  radial-gradient(120% 170% at 100% -10%, rgba(34,211,238,.40), transparent 55%),
  radial-gradient(120% 170% at -10% 0%, rgba(99,102,241,.55), transparent 55%),
  linear-gradient(120deg,#1e40af,#0ea5e9)}}
.hero::after{{content:"";position:absolute;inset:0;background:linear-gradient(180deg,transparent 55%,rgba(13,20,36,.55))}}
.brandrow{{display:flex;align-items:center;gap:13px;position:relative;z-index:1}}
.logo{{width:46px;height:46px;border-radius:14px;background:rgba(255,255,255,.16);
  border:1px solid rgba(255,255,255,.4);display:flex;align-items:center;justify-content:center}}
.brandname{{color:#f2f8ff;font-size:15px;font-weight:800;letter-spacing:.3px}}
.brandsub{{color:#d5e6ff;font-size:12.5px;opacity:.85;margin-top:2px}}
.title{{position:relative;z-index:1;margin-top:20px;color:#fff;font-size:31px;font-weight:800;letter-spacing:-.5px}}
.kpis{{position:relative;z-index:1;margin-top:16px;display:flex;gap:10px}}
.kpi{{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.24);border-radius:13px;
  padding:9px 15px;color:#fff;display:flex;align-items:baseline;gap:7px}}
.kpi b{{font-size:18px;font-weight:800}}
.kpi span{{font-size:12px;opacity:.9}}
.kpi.hot{{background:linear-gradient(135deg,#ff5470,#ff2d55);border-color:rgba(255,255,255,.35)}}
.list{{padding:6px 16px}}
.job{{display:grid;grid-template-columns:46px 1fr auto;align-items:center;gap:18px;padding:17px 18px}}
.job + .job{{border-top:1px solid var(--line)}}
.rank{{width:46px;height:46px;border-radius:13px;display:flex;align-items:center;justify-content:center;
  font-size:18px;font-weight:800;color:#cfe0ff;background:rgba(59,130,246,.15);border:1px solid rgba(59,130,246,.3)}}
.main{{min-width:0}}
.jtitle{{color:var(--txt);font-size:17px;font-weight:700;letter-spacing:-.2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.meta{{margin-top:9px;display:flex;flex-wrap:wrap;gap:7px}}
.pill{{font-size:12.5px;color:var(--muted);background:rgba(255,255,255,.05);border:1px solid var(--line);
  padding:5px 11px;border-radius:999px;display:inline-flex;gap:6px;align-items:center}}
.right{{display:flex;flex-direction:column;align-items:flex-end;gap:8px}}
.dday{{font-size:13px;font-weight:800;letter-spacing:.3px;padding:8px 13px;border-radius:11px;white-space:nowrap}}
.dday.red{{color:#fff;background:linear-gradient(135deg,#ff5470,#ff2d55);box-shadow:0 6px 16px -6px rgba(255,45,85,.8)}}
.dday.amber{{color:#3a2600;background:linear-gradient(135deg,#ffd35c,#ffb020)}}
.dday.green{{color:#04231a;background:linear-gradient(135deg,#5ff0bd,#37d399)}}
.dday.gray{{color:#d3dceb;background:rgba(255,255,255,.08);border:1px solid var(--line)}}
.date{{font-size:11.5px;color:#6b7a92}}
.foot{{display:flex;align-items:center;justify-content:space-between;padding:17px 32px;
  border-top:1px solid var(--line);background:rgba(255,255,255,.015)}}
.more{{color:#9fb2cc;font-size:13px}}
.cta{{color:#fff;font-size:13.5px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#2563eb);
  padding:11px 17px;border-radius:12px;border:1px solid rgba(255,255,255,.18);display:inline-flex;align-items:center;gap:7px}}
</style></head>
<body><div class="wrap"><div class="card">
  <div class="hero">
    <div class="brandrow">
      <div class="logo"><svg width="24" height="24" viewBox="0 0 24 24" fill="none"><path d="M12 3 2 8l10 5 8-4v6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M6 11v4c0 1.1 2.7 3 6 3s6-1.9 6-3v-4" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
      <div><div class="brandname">HiBrain Job Alert</div><div class="brandsub">hibrain.net · 연구 · 학계 채용정보</div></div>
    </div>
    <div class="title">📢 하이브레인 {subtitle}</div>
    <div class="kpis">
      <div class="kpi"><b>{total}</b><span>건</span></div>
      {kpi_hot}
      <div class="kpi"><b>{ts_date}</b><span>{ts_time} KST</span></div>
    </div>
  </div>
  <div class="list">{rows}</div>
  <div class="foot">
    {more}
    <div class="cta">전체 공고 보기
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M5 12h14M13 6l6 6-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
  </div>
</div></div></body></html>"""


def render_card_png(jobs: list[dict], out_path: str, *, subtitle: str = "신규 채용공고",
                    max_rows: int = 8) -> str:
    """공고 목록을 PNG 카드로 렌더링해 out_path에 저장하고 경로를 반환한다.

    Playwright(Chromium)가 필요하다. 호출 측에서 예외를 처리해 폴백할 수 있다.
    """
    from playwright.sync_api import sync_playwright  # 지연 임포트

    html_doc = build_card_html(jobs, subtitle=subtitle, max_rows=max_rows)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--force-color-profile=srgb"])
        page = browser.new_page(
            viewport={"width": CARD_WIDTH, "height": 1200},
            device_scale_factor=DEVICE_SCALE,
        )
        page.set_content(html_doc, wait_until="networkidle")
        # 웹폰트 로딩 완료를 대기 (실패해도 시스템 폰트로 진행)
        try:
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(300)
        except Exception:
            pass
        card = page.locator(".card")
        card.screenshot(path=out_path)
        browser.close()

    return out_path
