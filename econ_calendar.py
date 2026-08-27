# -*- coding: utf-8 -*-
"""미국 경제지표 발표 일정.

investing.com 같은 민간 사이트 대신 **미국 정부 공식 소스**에서 직접 가져온다.

  · 노동통계국(BLS)   공식 ICS 캘린더 피드  → CPI, 고용보고서, PPI, JOLTS
  · 연준(Fed)         FOMC 일정 페이지      → 금리 결정 회의
  · 경제분석국(BEA)   발표 일정 페이지      → GDP, PCE

민간 사이트를 안 쓰는 이유:
  1. 정부 사이트는 구조가 거의 안 바뀐다 (스크래핑이 깨지는 주된 원인 제거)
  2. 봇 차단·Cloudflare 가 없다
  3. 애초에 민간 사이트가 보여주는 그 숫자의 원본이다

BLS 는 아예 ICS(캘린더) 규격으로 제공하므로 HTML 파싱조차 하지 않는다.
세 곳 중 하나가 실패해도 나머지만으로 계속 진행한다.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

CACHE = Path(__file__).resolve().parent / "cache"
CACHE_TTL_HOURS = 12
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) market-brief/1.0"}

BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BEA_URL = "https://www.bea.gov/news/schedule"

# (원문에 들어있는 문구, 한글 이름, 중요도 3=상 2=중 1=하)
BLS_KEEP = [
    ("Consumer Price Index",              "소비자물가 CPI",   3),
    ("Employment Situation",              "고용보고서",       3),
    ("Producer Price Index",              "생산자물가 PPI",   2),
    ("Job Openings and Labor Turnover",   "구인·이직 JOLTS",  2),
    ("Employment Cost Index",             "고용비용지수",     1),
    ("U.S. Import and Export Price",      "수출입 물가",      1),
]
BEA_KEEP = [
    ("Personal Income and Outlays",       "개인소득·지출 PCE", 3),
    ("Gross Domestic Product",            "GDP",              3),
    ("GDP",                               "GDP",              3),
    ("International Trade in Goods",      "무역수지",         1),
]
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=UA, timeout=25)
        return r.text if r.ok else None
    except Exception:
        return None


def _match(text: str, table) -> tuple[str, int] | None:
    for needle, label, level in table:
        if needle.lower() in text.lower():
            return label, level
    return None


# ── 소스별 수집 ─────────────────────────────────────────────
def from_bls() -> list[dict]:
    """노동통계국 공식 ICS 피드. HTML 이 아니라 캘린더 규격이라 잘 안 깨진다."""
    raw = _get(BLS_ICS)
    if not raw:
        return []
    raw = re.sub(r"\r?\n[ \t]", "", raw)          # ICS 줄바꿈 접힘 펴기
    out = []
    for ev in raw.split("BEGIN:VEVENT")[1:]:
        md = re.search(r"DTSTART[^:]*:(\d{8})", ev)
        ms = re.search(r"SUMMARY:(.*)", ev)
        if not (md and ms):
            continue
        hit = _match(ms.group(1), BLS_KEEP)
        if not hit:
            continue
        out.append({
            "date": datetime.strptime(md.group(1), "%Y%m%d").date().isoformat(),
            "name": hit[0], "level": hit[1], "source": "노동통계국",
        })
    return out


def from_fomc() -> list[dict]:
    """연준 FOMC 회의 일정. 회의 마지막 날이 금리 결정일이다."""
    html = _get(FOMC_URL)
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(html, "lxml")
    out = []
    for panel in soup.select("div.panel.panel-default"):
        head = panel.select_one(".panel-heading")
        ym = re.search(r"(20\d{2})", head.get_text() if head else "")
        if not ym:
            continue
        year = int(ym.group(1))
        months = panel.select("div.fomc-meeting__month")
        days = panel.select("div.fomc-meeting__date")
        for m_el, d_el in zip(months, days):
            mtxt = m_el.get_text(" ", strip=True).lower()
            dtxt = d_el.get_text(" ", strip=True)
            # "1월/2월" 처럼 달을 걸치면 마지막 달이 결정일이 있는 달
            names = [w for w in re.split(r"[^a-z]+", mtxt) if w in MONTHS]
            if not names:
                continue
            month = MONTHS[names[-1]]
            nums = re.findall(r"\d+", dtxt)
            if not nums:
                continue
            day = int(nums[-1])              # 이틀 회의의 둘째 날 = 발표일
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            star = "*" in dtxt               # * 는 기자회견·경제전망 동반
            out.append({
                "date": d.isoformat(),
                "name": "FOMC 금리 결정" + (" (기자회견)" if star else ""),
                "level": 3, "source": "연준",
            })
    return out


def from_bea() -> list[dict]:
    """경제분석국 발표 일정. 표에 연도가 없어서 현재 연도로 추정한다."""
    html = _get(BEA_URL)
    if not html:
        return []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    soup = BeautifulSoup(html, "lxml")
    today = date.today()
    out = []
    for tr in soup.select("table tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 3:
            continue
        m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", tds[0])
        if not m or m.group(1).lower() not in MONTHS:
            continue
        hit = _match(tds[-1], BEA_KEEP)
        if not hit:
            continue
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
        try:
            d = date(today.year, month, day)
        except ValueError:
            continue
        if d < today - timedelta(days=45):    # 연말→연초로 넘어간 경우
            d = date(today.year + 1, month, day)
        out.append({"date": d.isoformat(), "name": hit[0],
                    "level": hit[1], "source": "경제분석국"})
    return out


# ── 합치기 + 캐시 ───────────────────────────────────────────
def _fetch_all() -> list[dict]:
    events: list[dict] = []
    for fn, label in ((from_bls, "노동통계국"), (from_fomc, "연준"),
                      (from_bea, "경제분석국")):
        try:
            got = fn()
            events += got
            print(f"  {label} {len(got)}건")
        except Exception as e:
            print(f"  {label} 실패: {type(e).__name__} — {e}")
    # 같은 날 같은 이름이 여러 소스에서 겹치면 하나만
    seen, uniq = set(), []
    for e in sorted(events, key=lambda x: (x["date"], -x["level"])):
        key = (e["date"], e["name"])
        if key not in seen:
            seen.add(key)
            uniq.append(e)
    return uniq


def upcoming(days: int = 30, use_cache: bool = True) -> list[dict]:
    """앞으로 `days` 일 안에 예정된 주요 지표. 실패하면 빈 목록."""
    CACHE.mkdir(exist_ok=True)
    path = CACHE / "econ.json"
    events = None

    if use_cache and path.exists():
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            age = datetime.now() - datetime.fromisoformat(blob["fetched_at"])
            if age < timedelta(hours=CACHE_TTL_HOURS):
                events = blob["events"]
                print(f"  캐시 사용 ({age.seconds // 3600}시간 전)")
        except Exception:
            events = None

    if events is None:
        fresh = _fetch_all()
        if fresh:
            events = fresh
            try:
                path.write_text(json.dumps(
                    {"fetched_at": datetime.now().isoformat(), "events": events},
                    ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass
        elif path.exists():
            # 네트워크가 끊겼어도 지난 캐시로 버틴다. 발표 일정은 몇 달 전에
            # 확정되므로 며칠 지난 자료도 거의 그대로 맞다.
            try:
                events = json.loads(path.read_text(encoding="utf-8"))["events"]
                print("  수집 실패 — 지난 캐시 사용")
            except Exception:
                events = []

    today = date.today()
    limit = today + timedelta(days=days)
    out = []
    for e in events or []:
        d = date.fromisoformat(e["date"])
        if today <= d <= limit:
            out.append({**e, "d": d, "dday": (d - today).days})
    out.sort(key=lambda x: (x["d"], -x["level"]))
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for e in upcoming(45, use_cache=False):
        print(f"{e['d']}  D-{e['dday']:<3} {'●' * e['level']:<3} "
              f"{e['name']:<20} ({e['source']})")
