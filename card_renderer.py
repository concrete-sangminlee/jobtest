"""하이브레인 채용 알림용 '상품 카드' 이미지 렌더러.

각 채용공고를 개별 세로형 카드(PNG)로 렌더링한다. 여러 장을 한 번에
Slack에 올리면 상품 진열대(갤러리)처럼 가로로 나열되어 보인다.

의존성(런타임): playwright + chromium. 한글은 시스템 CJK 폰트(fonts-noto-cjk)와
Pretendard 웹폰트를 사용한다.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

# 카드 크기(px). devicePixelRatio=2로 찍어 레티나급 선명도.
CARD_WIDTH = 340
DEVICE_SCALE = 2


def _esc(text: str) -> str:
    return html.escape((text or "").strip(), quote=True)


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def dday_view(job: dict) -> tuple[str, str, str]:
    """(배지 라벨, 색상 클래스, 마감일 텍스트)."""
    end = job.get("end_date") or ""
    date_text = f"~ {end}" if end else ""
    if job.get("always_open"):
        return "상시", "gray", "상시채용"
    d = job.get("d_day")
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


def _initial(company: str) -> str:
    """기관명 첫 글자(로고용). 한글/영문 모두 첫 글자를 쓴다."""
    company = (company or "").strip()
    return _esc(company[0]) if company else "H"


# 썸네일 그라데이션 팔레트 (인덱스별로 살짝 다르게 → 진열대 다양성)
_THUMBS = [
    "radial-gradient(130% 130% at 10% 0%, rgba(99,102,241,.65), transparent 55%),"
    "radial-gradient(130% 130% at 100% 20%, rgba(34,211,238,.55), transparent 55%),"
    "linear-gradient(135deg,#2563eb,#0ea5e9)",
    "radial-gradient(130% 130% at 0% 0%, rgba(236,72,153,.55), transparent 55%),"
    "radial-gradient(130% 130% at 100% 10%, rgba(139,92,246,.6), transparent 55%),"
    "linear-gradient(135deg,#7c3aed,#db2777)",
    "radial-gradient(130% 130% at 0% 0%, rgba(16,185,129,.55), transparent 55%),"
    "radial-gradient(130% 130% at 100% 10%, rgba(59,130,246,.5), transparent 55%),"
    "linear-gradient(135deg,#0ea5e9,#10b981)",
    "radial-gradient(130% 130% at 0% 0%, rgba(251,146,60,.6), transparent 55%),"
    "radial-gradient(130% 130% at 100% 10%, rgba(244,63,94,.55), transparent 55%),"
    "linear-gradient(135deg,#f59e0b,#ef4444)",
]

_IC_BRIEFCASE = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none">'
    '<rect x="3" y="7" width="18" height="13" rx="2" stroke="#a7b4cc" stroke-width="1.7"/>'
    '<path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke="#a7b4cc" stroke-width="1.7"/></svg>')
_IC_PIN = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none">'
    '<path d="M12 21s7-6.4 7-11a7 7 0 1 0-14 0c0 4.6 7 11 7 11Z" stroke="#a7b4cc" stroke-width="1.7"/>'
    '<circle cx="12" cy="10" r="2.2" stroke="#a7b4cc" stroke-width="1.7"/></svg>')
_IC_HEART = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none">'
    '<path d="M12 21s-7-4.6-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 5.4-7 10-7 10Z" stroke="#fff" stroke-width="1.8"/></svg>')
_IC_ARROW = ('<svg width="14" height="14" viewBox="0 0 24 24" fill="none">'
    '<path d="M5 12h14M13 6l6 6-6 6" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def build_card_html(job: dict, *, index: int = 0) -> str:
    """공고 1건을 상품 카드 HTML(자기완결형)로 만든다."""
    label, cls, date_text = dday_view(job)
    thumb = _THUMBS[index % len(_THUMBS)]

    company = _truncate(job.get("company") or "하이브레인", 22)
    region = job.get("region") or ""
    job_type = job.get("job_type") or ""

    inst_sub_parts = [p for p in (region, "연구·학계") if p]
    inst_sub = _esc(" · ".join(inst_sub_parts[:1])) if region else "연구 · 학계"

    tags = []
    if job_type:
        tags.append(f'<span class="tag">{_IC_BRIEFCASE}{_esc(_truncate(job_type, 16))}</span>')
    if region:
        tags.append(f'<span class="tag">{_IC_PIN}{_esc(_truncate(region, 12))}</span>')
    tags_html = "".join(tags) or '<span class="tag">채용</span>'

    return _TEMPLATE.format(
        thumb=thumb,
        badge_cls=cls,
        badge=_esc(label),
        heart=_IC_HEART,
        initial=_initial(job.get("company", "")),
        company=_esc(company),
        inst_sub=inst_sub,
        title=_esc(_truncate(job.get("title", "(제목 없음)"), 60)),
        tags=tags_html,
        deadline=_esc(date_text or "상시"),
        arrow=_IC_ARROW,
    )


_TEMPLATE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<style>
@font-face{{font-family:'Pretendard';font-weight:700;font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/packages/pretendard/dist/web/static/woff2/Pretendard-Bold.woff2') format('woff2');}}
@font-face{{font-family:'Pretendard';font-weight:800;font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/packages/pretendard/dist/web/static/woff2/Pretendard-ExtraBold.woff2') format('woff2');}}
*{{margin:0;padding:0;box-sizing:border-box;-webkit-font-smoothing:antialiased}}
body{{background:#0b1220}}
.card{{width:340px;font-family:'Pretendard','Noto Sans CJK KR','Noto Sans KR','Apple SD Gothic Neo',system-ui,sans-serif;
  background:#0f1830;border:1px solid rgba(255,255,255,.08);border-radius:22px;overflow:hidden;
  box-shadow:0 24px 50px -18px rgba(0,0,0,.75)}}
.thumb{{position:relative;height:150px;background:{thumb};display:flex;align-items:flex-end;padding:16px}}
.badge{{position:absolute;top:14px;left:14px;font-size:12px;font-weight:800;color:#fff;padding:6px 11px;
  border-radius:999px;letter-spacing:.3px;background:linear-gradient(135deg,#ff5470,#ff2d55);
  box-shadow:0 8px 18px -6px rgba(255,45,85,.9)}}
.badge.amber{{background:linear-gradient(135deg,#ffd35c,#ffb020);color:#3a2600;box-shadow:0 8px 18px -6px rgba(255,176,32,.7)}}
.badge.green{{background:linear-gradient(135deg,#5ff0bd,#37d399);color:#04231a;box-shadow:0 8px 18px -6px rgba(55,211,153,.6)}}
.badge.gray{{background:rgba(0,0,0,.35);color:#e7eefc;box-shadow:none}}
.fav{{position:absolute;top:12px;right:14px;width:30px;height:30px;border-radius:50%;
  background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);display:flex;align-items:center;justify-content:center}}
.inst{{display:flex;align-items:center;gap:9px;position:relative;z-index:1}}
.logo{{width:40px;height:40px;border-radius:11px;background:rgba(255,255,255,.92);display:flex;
  align-items:center;justify-content:center;font-weight:800;color:#1e40af;font-size:16px}}
.inst .name{{color:#fff;font-size:13.5px;font-weight:800;text-shadow:0 1px 3px rgba(0,0,0,.35)}}
.inst .sub{{color:#e6f0ff;font-size:11.5px;opacity:.9}}
.body{{padding:16px 18px 18px}}
.jtitle{{color:#eef3fb;font-size:16.5px;font-weight:800;line-height:1.35;letter-spacing:-.2px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:44px}}
.tags{{margin-top:13px;display:flex;flex-wrap:wrap;gap:7px}}
.tag{{font-size:12px;color:#a7b4cc;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);
  padding:5px 10px;border-radius:9px;display:inline-flex;gap:5px;align-items:center}}
.divider{{height:1px;background:rgba(255,255,255,.07);margin:15px 0}}
.row{{display:flex;align-items:center;justify-content:space-between}}
.deadline .lbl{{font-size:11px;color:#7c8aa3}}
.deadline .val{{font-size:14px;color:#e6ecf7;font-weight:800;margin-top:2px}}
.apply{{color:#fff;font-size:13.5px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#2563eb);
  padding:11px 16px;border-radius:12px;border:1px solid rgba(255,255,255,.18);display:inline-flex;gap:7px;align-items:center}}
</style></head><body>
<div class="card">
  <div class="thumb">
    <div class="badge {badge_cls}">{badge}</div>
    <div class="fav">{heart}</div>
    <div class="inst">
      <div class="logo">{initial}</div>
      <div><div class="name">{company}</div><div class="sub">{inst_sub}</div></div>
    </div>
  </div>
  <div class="body">
    <div class="jtitle">{title}</div>
    <div class="tags">{tags}</div>
    <div class="divider"></div>
    <div class="row">
      <div class="deadline"><div class="lbl">마감</div><div class="val">{deadline}</div></div>
      <div class="apply">지원하기 {arrow}</div>
    </div>
  </div>
</div>
</body></html>"""


def render_cards(jobs: list[dict], out_dir: str) -> list[str]:
    """공고 목록을 각각 PNG 카드로 렌더링하고 파일 경로 목록을 반환한다.

    Playwright(Chromium) 필요. 브라우저를 한 번만 띄워 여러 카드를 연속 렌더링한다.
    """
    import os
    from playwright.sync_api import sync_playwright

    os.makedirs(out_dir, exist_ok=True)
    paths: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--force-color-profile=srgb"])
        page = browser.new_page(
            viewport={"width": CARD_WIDTH + 40, "height": 600},
            device_scale_factor=DEVICE_SCALE,
        )
        for i, job in enumerate(jobs):
            page.set_content(build_card_html(job, index=i), wait_until="networkidle")
            try:
                page.evaluate("document.fonts.ready")
                page.wait_for_timeout(200)
            except Exception:
                pass
            out_path = os.path.join(out_dir, f"card_{i:02d}.png")
            page.locator(".card").screenshot(path=out_path)
            paths.append(out_path)
        browser.close()

    return paths


# 하위 호환: 단일 카드 렌더(구 인터페이스). 첫 공고만 렌더링.
def render_card_png(jobs, out_path: str, **_ignored) -> str:
    import os
    out_dir = os.path.dirname(out_path) or "."
    paths = render_cards(jobs[:1] if isinstance(jobs, list) else [jobs], out_dir)
    if paths and paths[0] != out_path:
        os.replace(paths[0], out_path)
    return out_path
