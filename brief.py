# -*- coding: utf-8 -*-
"""
전날 미국장 브리핑 생성기.

실행하면 output/ 폴더에 HTML 브리핑을 만들고 브라우저로 엽니다.
데이터는 yfinance 에서 가져오며 API 키가 필요 없습니다.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
import urllib.parse
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

import config
import econ_calendar

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("BRIEF_OUTPUT_DIR") or (BASE / "output"))
if not OUT_DIR.is_absolute():
    OUT_DIR = BASE / OUT_DIR
HISTORY = BASE / "history.csv"
LOG = BASE / "last-run.log"
KST = timezone(timedelta(hours=9))


class _Tee:
    """화면에 찍으면서 동시에 로그 파일에도 남긴다.

    아침 자동 실행은 창이 곧바로 닫혀서, 무슨 일이 있었는지 확인할 방법이
    로그밖에 없다.
    """

    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh

    def write(self, s):
        self.stream.write(s)
        try:
            self.fh.write(s)
        except Exception:
            pass

    def flush(self):
        self.stream.flush()
        try:
            self.fh.flush()
        except Exception:
            pass


def _trim_log(max_lines: int = 400) -> None:
    """로그가 무한정 커지지 않게 최근 것만 남긴다."""
    try:
        if LOG.exists() and LOG.stat().st_size > 200_000:
            tail = LOG.read_text(encoding="utf-8").splitlines()[-max_lines:]
            LOG.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_env(path: Path | None = None) -> None:
    """같은 폴더의 .env 파일을 읽어 환경변수로 올린다.

    비밀 키를 config.py 가 아니라 .env 에 두는 이유:
    설정 파일은 남에게 보여주거나 백업해도 되지만 키는 그러면 안 되기 때문이다.
    이미 설정돼 있는 환경변수는 덮어쓰지 않는다(시스템 설정이 우선).
    """
    path = path or BASE / ".env"
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"  .env 읽기 실패: {type(e).__name__}")
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


load_env()

# ── 무엇을 가져올지 ──────────────────────────────────────────
# (티커, 표시이름) — 티커는 야후파이낸스 기준
INDEXES = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥 종합"),
           ("^DJI", "다우존스"), ("^RUT", "러셀 2000")]

# 지표가 많아져 성격별로 묶는다. (티커, 표시이름, 표시형식)
RATES = [
    ("^IRX", "3개월", "rate"), ("^FVX", "5년", "rate"),
    ("^TNX", "10년", "rate"),  ("^TYX", "30년", "rate"),
]
COMMODITIES = [
    ("CL=F", "WTI 유가", "usd"), ("GC=F", "금", "usd"), ("SI=F", "은", "usd"),
    ("HG=F", "구리", "usd"),     ("NG=F", "천연가스", "usd"),
    ("PL=F", "백금", "usd"),
]
FX = [
    ("KRW=X", "원/달러", "krw"),    ("JPYKRW=X", "원/엔", "krw"),
    ("EURKRW=X", "원/유로", "krw"), ("JPY=X", "달러/엔", "pt"),
    ("EURUSD=X", "유로/달러", "fx4"), ("DX-Y.NYB", "달러인덱스", "pt"),
]
# 미국 밖 시장. ETF 가 아니라 각국의 실제 대표지수다.
WORLD = [
    ("^KS11", "코스피", "pt"),      ("^KQ11", "코스닥", "pt"),
    ("^N225", "니케이225", "pt"),   ("^HSI", "항셍", "pt"),
    ("000001.SS", "상해종합", "pt"), ("^STOXX50E", "유로스톡스50", "pt"),
    ("^GDAXI", "독일 DAX", "pt"),   ("^FTSE", "영국 FTSE", "pt"),
    ("^TWII", "대만 가권", "pt"),
    ("^NSEI", "인도 니프티", "pt"),
    ("^GSPTSE", "캐나다 TSX", "pt"),
]
RISK = [
    ("^VIX", "VIX 공포지수", "pt"), ("BTC-USD", "비트코인", "usd"),
    ("TLT", "미국채 20년", "usd"),  ("HYG", "하이일드 채권", "usd"),
]
GAUGE_GROUPS = [("금리", RATES), ("원자재", COMMODITIES), ("환율", FX),
                ("해외 지수", WORLD), ("심리·기타", RISK)]
GAUGES = RATES + COMMODITIES + FX + WORLD + RISK

# 시장 폭: 시총가중과 동일가중을 비교하면 소수 대형주가 끈 장인지 알 수 있다
BREADTH = [("SPY", "시총가중"), ("RSP", "동일가중")]

# 특징주를 뽑을 대형주 유니버스
MOVERS = {
    "AAPL": "애플", "MSFT": "마이크로소프트", "NVDA": "엔비디아",
    "GOOGL": "알파벳", "AMZN": "아마존", "META": "메타", "TSLA": "테슬라",
    "AVGO": "브로드컴", "LLY": "일라이릴리", "JPM": "JP모건", "V": "비자",
    "XOM": "엑슨모빌", "UNH": "유나이티드헬스", "MA": "마스터카드",
    "COST": "코스트코", "HD": "홈디포", "PG": "P&G", "JNJ": "존슨앤존슨",
    "WMT": "월마트", "NFLX": "넷플릭스", "CRM": "세일즈포스", "AMD": "AMD",
    "ORCL": "오라클", "BAC": "뱅크오브아메리카", "KO": "코카콜라",
    "PEP": "펩시코", "TMO": "써모피셔", "CSCO": "시스코", "MRK": "머크",
    "ABBV": "애브비", "ADBE": "어도비", "MCD": "맥도날드", "DIS": "디즈니",
    "INTC": "인텔", "QCOM": "퀄컴", "TXN": "텍사스인스트루먼트",
    "PLTR": "팔란티어", "MU": "마이크론", "ARM": "ARM", "SMCI": "슈퍼마이크로",
}

# 지금 이 순간 값을 보여줄 대상. 미국장 마감 뒤에도 계속 거래되는 것들이라
# "어젯밤 종가 대비 지금" 을 비교하면 오늘 장 방향의 힌트가 된다.
LIVE = [
    ("ES=F",    "S&P 선물"),
    ("NQ=F",    "나스닥 선물"),
    ("YM=F",    "다우 선물"),
    ("BTC-USD", "비트코인"),
]

SECTORS = [
    ("XLK", "기술"), ("XLC", "커뮤니케이션"), ("XLY", "임의소비재"),
    ("XLF", "금융"), ("XLV", "헬스케어"), ("XLI", "산업재"),
    ("XLP", "필수소비재"), ("XLE", "에너지"), ("XLU", "유틸리티"),
    ("XLB", "소재"), ("XLRE", "부동산"),
]


# ── 데이터 수집 ─────────────────────────────────────────────
def fetch_closes(tickers: list[str]) -> pd.DataFrame:
    """모든 티커의 일별 종가를 한 번의 요청으로 받아온다."""
    df = yf.download(sorted(set(tickers)), period="1y", interval="1d",
                     auto_adjust=True, progress=False, group_by="column")
    if df is None or df.empty:
        raise SystemExit("데이터를 받지 못했습니다. 인터넷 연결을 확인해 주세요.")
    if isinstance(df.columns, pd.MultiIndex):
        return df["Close"]
    return df[["Close"]]


def anchor_date(close: pd.DataFrame, now_ny=None):
    """기준일 = S&P 500 이 '마감까지 끝난' 마지막 날.

    두 가지를 걸러야 한다.
    1) 미국장이 아직 안 열렸으면 선물·환율만 오늘 값이 있고 주식은 비어 있다.
    2) 미국장이 지금 열려 있으면 오늘 봉이 이미 생기지만 그건 '진행 중'이다.
       그대로 쓰면 개장 몇 분짜리 값을 '종가'라고 부르게 된다.
    한국 밤 시간(= 미국 낮)에 직접 실행할 때 2번이 실제로 걸린다.
    """
    now_ny = now_ny or datetime.now(ZoneInfo("America/New_York"))
    for ref in ("^GSPC", "SPY", "^DJI"):
        if ref not in close.columns:
            continue
        s = close[ref].dropna()
        if not len(s):
            continue
        last = s.index[-1]
        in_progress = (last.date() == now_ny.date() and now_ny.hour < 16)
        if in_progress and len(s) >= 2:
            return s.index[-2]          # 아직 안 끝난 오늘을 빼고 어제로
        return last
    return close.index[-1]


def quote(close: pd.DataFrame, ticker: str, anchor) -> dict | None:
    """기준일 시점의 종가와 그 전 거래일 대비 변화."""
    if ticker not in close.columns:
        return None
    s = close[ticker].dropna()
    s = s[s.index <= anchor]
    if len(s) < 2:
        return None
    last, prev = float(s.iloc[-1]), float(s.iloc[-2])
    hi52, lo52 = float(s.max()), float(s.min())
    return {
        "ticker": ticker,
        "last": last,
        "prev": prev,
        "chg": last - prev,
        "pct": (last - prev) / prev * 100 if prev else 0.0,
        "date": s.index[-1],
        "series": s,
        "hi52": hi52,
        "lo52": lo52,
        # 52주 고점에서 얼마나 내려와 있나 (0 이면 신고가)
        "from_hi": (last / hi52 - 1) * 100 if hi52 else 0.0,
        # 52주 저점~고점 사이에서 현재 위치 (0~100)
        "pos52": (last - lo52) / (hi52 - lo52) * 100 if hi52 > lo52 else 50.0,
    }


def perf(close: pd.DataFrame, ticker: str, anchor) -> dict:
    """기간별 누적 등락률. 히스토리가 쌓이길 기다릴 필요 없이 과거 시세로 바로 계산한다."""
    out: dict[str, float | None] = {}
    if ticker not in close.columns:
        return out
    s = close[ticker].dropna()
    s = s[s.index <= anchor]
    if len(s) < 2:
        return out
    last = float(s.iloc[-1])
    for label, days in (("1주", 7), ("1개월", 30), ("3개월", 91), ("6개월", 182)):
        prior = s[s.index <= anchor - pd.Timedelta(days=days)]
        out[label] = (last / float(prior.iloc[-1]) - 1) * 100 if len(prior) else None
    ytd = s[s.index >= pd.Timestamp(anchor.year, 1, 1)]
    out["연초대비"] = (last / float(ytd.iloc[0]) - 1) * 100 if len(ytd) else None
    return out


def fetch_live(tickers: list[str]) -> dict:
    """지금 이 순간 값(15분봉의 마지막). 실패해도 브리핑은 계속 나온다."""
    try:
        df = yf.download(sorted(set(tickers)), period="2d", interval="15m",
                         auto_adjust=True, progress=False, group_by="column")
        if df is None or df.empty:
            return {}
        c = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
        out = {}
        for tk in tickers:
            if tk not in c.columns:
                continue
            s = c[tk].dropna()
            if len(s):
                out[tk] = {"value": float(s.iloc[-1]), "at": s.index[-1]}
        return out
    except Exception as e:
        print(f"  실시간 시세 건너뜀: {type(e).__name__} — {e}")
        return {}


def fetch_earnings(tickers: list[str]) -> dict:
    """관심종목의 직전/다음 실적 발표일.

    시장 전체 실적 캘린더는 무료로 안 되지만 종목별로는 된다.
    직전 날짜도 같이 받아야 "어제 실적 발표한 종목"을 표시할 수 있다.
    """
    out = {}
    today = date.today()
    for tk in tickers:
        try:
            ed = yf.Ticker(tk).get_earnings_dates(limit=12)
            if ed is None or not len(ed):
                continue
            days = sorted({d.date() for d in ed.index})
            out[tk] = {
                "next": next((d for d in days if d >= today), None),
                "last": next((d for d in reversed(days) if d < today), None),
            }
        except Exception:
            continue
    return out


def fetch_news(queries: list[str], count: int) -> list[dict]:
    seen: set[str] = set()
    items: list[dict] = []
    for q in queries:
        try:
            res = yf.Search(q, news_count=count).news or []
        except Exception as e:  # 뉴스는 실패해도 브리핑 자체는 나와야 한다
            print(f"  뉴스 '{q}' 실패: {type(e).__name__}")
            continue
        for n in res:
            key = n.get("uuid") or n.get("title")
            title = (n.get("title") or "").strip()
            if not key or key in seen or not title:
                continue
            seen.add(key)
            items.append({
                "title": title,
                "publisher": n.get("publisher") or "",
                "link": n.get("link") or "",
                "ts": n.get("providerPublishTime") or 0,
            })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:count]


# ── AI 요약 (선택) ──────────────────────────────────────────
def ai_summary(anchor, indexes, sectors, news, watch=None, tnews=None,
                movers=None) -> dict:
    """3줄 요약 + 뉴스 헤드라인 한글 번역을 한 번의 호출로 받는다.

    키가 없으면 조용히 건너뛴다. 둘 다 없어도 브리핑은 정상적으로 나온다.
    """
    # .env → 시스템 환경변수 → (예전 방식) config.py 순서로 찾는다
    key = (os.environ.get("GEMINI_API_KEY", "")
           or getattr(config, "GEMINI_API_KEY", "")).strip()
    if not key:
        return {}
    if not key.isascii():
        print("  AI 요약 건너뜀: API 키에 한글이나 따옴표가 섞여 있습니다. "
              "config.py 의 GEMINI_API_KEY 를 다시 확인하세요.")
        return {}

    idx_txt = ", ".join(f"{n} {q['pct']:+.2f}%" for q, n in indexes)
    top = sorted(sectors, key=lambda x: -x[0]["pct"])[:3]
    bottom = sorted(sectors, key=lambda x: x[0]["pct"])[:3]
    sec_txt = ("상승 " + ", ".join(f"{n} {q['pct']:+.2f}%" for q, n in top)
               + " / 하락 " + ", ".join(f"{n} {q['pct']:+.2f}%" for q, n in bottom))
    # 시장 뉴스와 종목 뉴스를 한 번호 목록으로 합쳐 보내고, 받은 뒤 다시 나눈다.
    # 배열을 둘로 나눠 요청하면 한쪽만 개수가 틀어지는 일이 잦다.
    main_heads = list(news[:10])
    tick_heads = list(tnews or [])
    heads = main_heads + tick_heads
    news_txt = "\n".join(f"{i}. {n['title']}" for i, n in enumerate(heads, 1))
    # 개별 종목 등락을 같이 넘겨야 한다. 안 그러면 AI 가 헤드라인만 보고
    # 종목이 올랐는지 내렸는지를 추측하다가 사실과 반대로 쓴다.
    # 관심종목이 없는 공개용 브리핑에서도 특징주는 있으므로 그걸 쓴다.
    named = []
    if watch:
        named += [f"{n} {q['pct']:+.2f}%" for q, n
                  in sorted(watch, key=lambda x: -abs(x[0]["pct"]))]
    if movers:
        up, down = movers
        named += [f"{n} {q['pct']:+.2f}%" for q, n in list(up) + list(down)]
    watch_txt = ("[개별 종목] " + ", ".join(dict.fromkeys(named)) + "\n"
                 if named else "")

    prompt = (
        f"{anchor:%Y년 %m월 %d일} 미국 증시 마감 데이터다.\n\n"
        f"[지수] {idx_txt}\n"
        f"[섹터] {sec_txt}\n"
        f"{watch_txt}"
        f"[헤드라인]\n{news_txt}\n\n"
        "한국 개인투자자에게 브리핑한다고 생각하고, 이날 시장이 왜 그렇게 움직였는지 "
        "핵심만 3줄로 요약해라. 각 줄은 한 문장이고 40자 내외로 짧게.\n"
        "규칙:\n"
        "- 위에 주어진 숫자와 어긋나는 말을 절대 쓰지 마라.\n"
        "- 종목이나 섹터가 올랐다/내렸다고 쓸 때는 반드시 위 숫자를 확인하고 써라. "
        "헤드라인 제목만 보고 등락 방향을 추측하지 마라.\n"
        "- 위 데이터에 없는 종목·지표·수치를 새로 만들어내지 마라.\n\n"
        f"그리고 위 헤드라인 {len(heads)}개를 한국어로 번역해라. "
        "번호 순서를 그대로 지키고, 개수도 정확히 맞춰라. "
        "기사 제목답게 간결하게 옮기고, 회사명은 한국에서 통용되는 이름을 써라.\n"
        '반드시 이 JSON 형식으로만 답해라: '
        '{"summary": ["...", "...", "..."], "news_ko": ["1번 번역", "2번 번역", ...]}'
    )

    model = getattr(config, "GEMINI_MODEL", "gemini-3.6-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3,
                             "responseMimeType": "application/json"},
    }
    try:
        # 503(서버 혼잡)·429(순간 한도) 는 잠시 뒤 대개 풀린다. 자동 실행은
        # 사람이 다시 눌러줄 수 없으므로 여기서 몇 번 더 시도한다.
        r = None
        for attempt, wait in enumerate((6, 18, 0), 1):
            r = requests.post(url, headers={"x-goog-api-key": key},
                              json=payload, timeout=90)
            if r.status_code == 200 or r.status_code not in (429, 500, 502, 503):
                break
            if wait:
                print(f"  AI 재시도 {attempt}/2 (HTTP {r.status_code}, {wait}초 뒤)")
                time.sleep(wait)
        if r is None or r.status_code != 200:
            code = r.status_code if r is not None else "?"
            print(f"  AI 요약 건너뜀: HTTP {code} — "
                  f"{(r.text[:160] if r is not None else '')}")
            return {}
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        for fence in ("```json", "```"):            # 가끔 코드펜스를 붙여서 준다
            if text.startswith(fence):
                text = text[len(fence):].strip()
        text = text.removesuffix("```").strip()
        data = json.loads(text)

        lines = [str(x).strip() for x in (data.get("summary") or [])
                 if str(x).strip()]
        ko = [str(x).strip() for x in (data.get("news_ko") or [])]
        # 개수가 안 맞으면 어느 기사의 번역인지 알 수 없으므로 통째로 버린다
        if len(ko) != len(heads):
            if ko:
                print(f"  번역 개수 불일치({len(ko)}/{len(heads)}) — 번역 생략")
            ko = []
        n_main = len(main_heads)
        return {"summary": lines,
                "news_ko": ko[:n_main],
                "tnews_ko": ko[n_main:]}
    except Exception as e:
        print(f"  AI 요약 건너뜀: {type(e).__name__} — {e}")
        return {}


# ── 표시 형식 ───────────────────────────────────────────────
def fmt_value(v: float, kind: str) -> str:
    if kind == "rate":
        return f"{v:,.3f}%"
    if kind == "usd":
        return f"${v:,.2f}"
    if kind == "krw":
        return f"{v:,.2f}원"
    if kind == "fx4":                        # 환율은 소수점이 중요하다
        return f"{v:,.4f}"
    return f"{v:,.2f}"


def fmt_change(q: dict, kind: str) -> str:
    if kind == "rate":                       # 금리는 bp(베이시스포인트)로 본다
        bp = q["chg"] * 100
        return f"{bp:+,.1f}bp"
    return f"{q['pct']:+.2f}%"


def tone(pct: float) -> str:
    if pct > 0.0001:
        return "up"
    if pct < -0.0001:
        return "down"
    return "flat"


def sparkline(series: pd.Series, width: int = 132, height: int = 34) -> str:
    vals = [float(v) for v in series.tail(30)]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    step = width / (len(vals) - 1)
    pts = " ".join(
        f"{i * step:.1f},{height - (v - lo) / rng * height:.1f}"
        for i, v in enumerate(vals)
    )
    return (f'<svg class="spark" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="none"><polyline points="{pts}"/></svg>')


def line_chart(df: pd.DataFrame, names: list[str],
               width: int = 900, height: int = 250) -> str:
    """여러 계열을 '시작점 100' 기준으로 정규화해 한 판에 겹쳐 그린다.

    S&P 7,600 과 러셀 3,000 처럼 절대 숫자가 달라도 같은 축에서 비교된다.
    """
    df = df.dropna()
    if df.empty or len(df) < 2:
        return ""
    norm = df / df.iloc[0] * 100
    lo, hi = float(norm.min().min()), float(norm.max().max())
    pad = (hi - lo) * 0.10 or 1.0
    lo, hi = lo - pad, hi + pad
    span = hi - lo

    L, R, T, B = 46, 14, 14, 26                  # 여백
    iw, ih, n = width - L - R, height - T - B, len(norm)

    def px(i): return L + iw * i / (n - 1)
    def py(v): return T + ih * (1 - (v - lo) / span)

    out = []
    for k in range(5):                            # 가로 눈금 + 수익률 라벨
        v = lo + span * k / 4
        y = py(v)
        out.append(f'<line class="grid" x1="{L}" y1="{y:.1f}" '
                   f'x2="{width - R}" y2="{y:.1f}"/>')
        out.append(f'<text class="ax" x="{L - 7}" y="{y + 3.5:.1f}" '
                   f'text-anchor="end">{v - 100:+.0f}%</text>')

    prev = None                                   # 월이 바뀌는 지점에 세로선
    for i, dt in enumerate(norm.index):
        if prev is not None and dt.month != prev:
            out.append(f'<line class="grid" x1="{px(i):.1f}" y1="{T}" '
                       f'x2="{px(i):.1f}" y2="{T + ih}"/>')
            out.append(f'<text class="ax" x="{px(i):.1f}" y="{height - 7}" '
                       f'text-anchor="middle">{dt.month}월</text>')
        prev = dt.month

    for ci, col in enumerate(norm.columns, 1):
        pts = " ".join(f"{px(i):.1f},{py(float(v)):.1f}"
                       for i, v in enumerate(norm[col]))
        out.append(f'<polyline class="ln k{ci}" points="{pts}"/>')

    legend = "".join(
        f'<span class="lg"><i class="k{ci}"></i>{esc(nm)} '
        f'<b>{float(norm[col].iloc[-1]) - 100:+.1f}%</b></span>'
        for ci, (col, nm) in enumerate(zip(norm.columns, names), 1))

    return (f'<div class="chart"><svg viewBox="0 0 {width} {height}">'
            + "".join(out) + f'</svg><div class="legend">{legend}</div></div>')


def top_movers(close: pd.DataFrame, anchor, universe: dict, n: int = 5):
    """대형주 중 가장 많이 오른/내린 종목. 관심종목 밖에서 벌어진 일을 잡아준다."""
    rows = []
    for tk, name in universe.items():
        q = quote(close, tk, anchor)
        if q:
            rows.append((q, name))
    rows.sort(key=lambda r: r[0]["pct"])
    return rows[-n:][::-1], rows[:n]


def market_breadth(close: pd.DataFrame, anchor, sectors) -> dict:
    """시총가중 vs 동일가중. 차이가 크면 소수 대형주가 끌어올린 장이다."""
    cap = quote(close, "SPY", anchor)
    eq = quote(close, "RSP", anchor)
    if not (cap and eq):
        return {}
    return {
        "cap": cap, "eq": eq, "gap": cap["pct"] - eq["pct"],
        "sector_up": sum(1 for q, _ in sectors if q["pct"] > 0),
        "sector_all": len(sectors),
    }


def _iso_kst(s: str):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(KST)
    except Exception:
        return None


def fetch_ticker_news(watch_items, per_ticker: int = 1, total: int = 8):
    """관심종목별 뉴스. 시장 전체 뉴스와 달리 '내 종목' 소식만 모은다.

    Ticker.news 는 검색 뉴스와 구조가 달라서(content 안에 중첩) 따로 판다.
    """
    out, seen = [], set()
    for tk, name in watch_items:
        try:
            items = yf.Ticker(tk).news or []
        except Exception:
            continue
        picked = 0
        for it in items:
            c = it.get("content") or {}
            title = (c.get("title") or "").strip()
            if not title or title in seen:
                continue
            url = ((c.get("clickThroughUrl") or c.get("canonicalUrl") or {})
                   or {}).get("url", "")
            seen.add(title)
            out.append({
                "ticker": tk, "name": name, "title": title,
                "publisher": (c.get("provider") or {}).get("displayName", ""),
                "link": url, "pub": str(c.get("pubDate") or ""),
            })
            picked += 1
            if picked >= per_ticker:
                break
    out.sort(key=lambda x: x["pub"], reverse=True)
    return out[:total]


def past_briefs(current: str, limit: int = 10):
    """지난 브리핑 파일 목록. 날짜별로 건너뛸 수 있게 한다."""
    try:
        files = sorted(OUT_DIR.glob("brief-*.html"), reverse=True)
    except Exception:
        return []
    out = []
    for f in files[:limit]:
        day = f.stem.replace("brief-", "")
        if day != current:
            out.append((f.name, day))
    return out


def yurl(ticker: str) -> str:
    """야후파이낸스 종목 페이지 주소.

    ^GSPC, CL=F, KRW=X 처럼 기호가 든 티커도 있어서 URL 인코딩이 필요하다.
    """
    return ("https://finance.yahoo.com/quote/"
            + urllib.parse.quote(str(ticker), safe=""))


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ── 화면 ───────────────────────────────────────────────────
DARK = """
    --bg: #0f1216; --panel: #171b21; --line: #262c34;
    --text: #e8ecf1; --muted: #9aa4b0; --faint: #6b7480;
    --k1: #60a5fa; --k2: #4ade80; --k3: #fbbf24; --k4: #c084fc;
  """

CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  --bg: #f6f7f9; --panel: #ffffff; --line: #e3e6ea;
  --text: #14181d; --muted: #6b7480; --faint: #9aa3ad;
  --up: __UP__; --down: __DOWN__; --flat: #8b949e;
  --k1: #2563eb; --k2: #16a34a; --k3: #d97706; --k4: #9333ea;
}
/* 다크 색을 두 군데에 건다.
   1) OS 설정이 어두울 때 — 단, 보는 사람이 '밝게'를 직접 골랐으면 제외
   2) 보는 사람이 '어둡게'를 직접 골랐을 때
   웹에 올리면 뷰어가 테마를 지정할 수 있어서, 미디어쿼리 하나만으로는
   배경만 어둡고 글자는 밝은 채로 남는 일이 생긴다. */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {__DARK__}
}
:root[data-theme="dark"] {__DARK__}
body {
  margin: 0; padding: 32px 20px 64px; background: var(--bg); color: var(--text);
  font-family: Pretendard, "Malgun Gothic", -apple-system, BlinkMacSystemFont,
               "Segoe UI", system-ui, sans-serif;
  font-size: 15px; line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 980px; margin: 0 auto; }

header { margin-bottom: 28px; }
h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.02em; font-weight: 700; }
.sub { color: var(--muted); font-size: 14px; }
.sub b { color: var(--text); font-weight: 600; }

h2 {
  margin: 34px 0 12px; font-size: 13px; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--faint);
}

.up { color: var(--up); } .down { color: var(--down); } .flat { color: var(--flat); }

.ai-tag { font-size: 11px; color: var(--faint); font-weight: 500;
          letter-spacing: 0; text-transform: none; margin-left: 8px; }
.summary { background: var(--panel); border: 1px solid var(--line);
           border-radius: 12px; padding: 4px 18px; }
.summary .line { display: grid; grid-template-columns: 20px 1fr; gap: 10px;
                 align-items: baseline; padding: 11px 0; }
.summary .line + .line { border-top: 1px solid var(--line); }
.summary .n { color: var(--faint); font-size: 12px; font-weight: 700;
              font-variant-numeric: tabular-nums; }
.summary .tx { font-size: 15px; line-height: 1.55; }

.idx { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  padding: 16px 18px;
}
.card .name { font-size: 13px; color: var(--muted); margin-bottom: 6px; }
.card .val { font-size: 27px; font-weight: 700; letter-spacing: -0.02em;
             font-variant-numeric: tabular-nums; }
.card .chg { font-size: 14px; font-weight: 600; margin-top: 2px;
             font-variant-numeric: tabular-nums; }
.spark { width: 100%; height: 34px; margin-top: 10px; display: block; }
.spark polyline { fill: none; stroke: currentColor; stroke-width: 1.6;
                  vector-effect: non-scaling-stroke; opacity: 0.75; }

.gauges { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.gauge {
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 14px;
}
.gauge .name { font-size: 12px; color: var(--muted); }
.gauge .val { font-size: 18px; font-weight: 700; margin-top: 3px;
              font-variant-numeric: tabular-nums; }
.gauge .chg { font-size: 12px; font-weight: 600;
              font-variant-numeric: tabular-nums; }

.live { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.live .g { background: var(--panel); border: 1px solid var(--line);
           border-radius: 10px; padding: 12px 14px; }
.live .name { font-size: 12px; color: var(--muted); }
.live .val { font-size: 18px; font-weight: 700; margin-top: 3px;
             font-variant-numeric: tabular-nums; }
.live .chg { font-size: 12px; font-weight: 600;
             font-variant-numeric: tabular-nums; }
.dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
       background: #16a34a; margin-right: 5px; vertical-align: middle; }

.tablewrap { overflow-x: auto; }
.badge { display: inline-block; font-size: 11px; font-weight: 600;
         padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line);
         color: var(--muted); margin-left: 7px; white-space: nowrap; }
.badge.soon { border-color: var(--down); color: var(--down); }

.pos { display: inline-block; width: 52px; height: 5px; border-radius: 3px;
       background: var(--line); position: relative; vertical-align: middle;
       margin-right: 9px; }
.pos i { position: absolute; top: -3px; width: 2px; height: 11px;
         background: currentColor; border-radius: 1px; }

.econ td, .econ th { text-align: left; }
.econ td.dday { font-weight: 700; white-space: nowrap;
                font-variant-numeric: tabular-nums; }
.econ td.dday.soon { color: var(--down); }
.econ .lv { letter-spacing: 2px; font-size: 11px; white-space: nowrap; }
.econ .lv.h { color: var(--text); }
.econ .lv.m { color: var(--muted); }
.econ .lv.l { color: var(--faint); }
.econ .src { color: var(--faint); font-size: 12px; white-space: nowrap; }

.chart { background: var(--panel); border: 1px solid var(--line);
         border-radius: 12px; padding: 14px 16px 12px; }
.chart svg { width: 100%; height: auto; display: block; overflow: visible; }
.chart .grid { stroke: var(--line); stroke-width: 1; }
.chart .ax { fill: var(--faint); font-size: 10px;
             font-family: inherit; font-variant-numeric: tabular-nums; }
.chart .ln { fill: none; stroke-width: 1.7; stroke-linejoin: round;
             vector-effect: non-scaling-stroke; }
.ln.k1 { stroke: var(--k1); } .ln.k2 { stroke: var(--k2); }
.ln.k3 { stroke: var(--k3); } .ln.k4 { stroke: var(--k4); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 10px;
          font-size: 12.5px; color: var(--muted); }
.legend b { color: var(--text); font-variant-numeric: tabular-nums;
            margin-left: 2px; }
.lg i { display: inline-block; width: 12px; height: 3px; border-radius: 2px;
        margin-right: 6px; vertical-align: middle; }
.lg i.k1 { background: var(--k1); } .lg i.k2 { background: var(--k2); }
.lg i.k3 { background: var(--k3); } .lg i.k4 { background: var(--k4); }

.badge.hot { border-color: var(--up); color: var(--up); }

/* 카드·행을 눌러 야후파이낸스 원본으로 갈 수 있게. 링크 티는 안 낸다 */
a.q { color: inherit; text-decoration: none; display: block;
      transition: border-color .12s; }
a.q:hover { border-color: var(--faint); }
a.lnk { color: inherit; text-decoration: none; }
a.lnk:hover { text-decoration: underline; }

.two { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.mv { background: var(--panel); border: 1px solid var(--line);
      border-radius: 12px; padding: 6px 16px; }
.mv .cap { font-size: 12px; color: var(--faint); font-weight: 600;
           padding: 9px 0 7px; }
.mv .row { display: grid; grid-template-columns: 1fr auto; gap: 10px;
           padding: 7px 0; border-top: 1px solid var(--line); font-size: 14px; }
.mv .row b { font-weight: 600; }
.mv .row .tk { color: var(--faint); font-size: 12px; font-weight: 400;
               margin-left: 5px; }
.mv .row .p { font-weight: 700; font-variant-numeric: tabular-nums; }

.breadth { background: var(--panel); border: 1px solid var(--line);
           border-radius: 12px; padding: 14px 18px; font-size: 14px;
           line-height: 1.7; }
.breadth .big { font-size: 15px; }
.breadth .note { color: var(--muted); font-size: 13px; margin-top: 6px; }

.tnews .item { display: block; padding: 10px 0; }
.tnews .who { font-size: 12px; color: var(--faint); font-weight: 600;
              margin-bottom: 2px; }

.nav { display: flex; flex-wrap: wrap; gap: 8px; }
.nav a { display: inline-block; padding: 5px 11px; border-radius: 8px;
         border: 1px solid var(--line); background: var(--panel);
         color: var(--muted); text-decoration: none; font-size: 12.5px;
         font-variant-numeric: tabular-nums; }
.nav a:hover { color: var(--text); border-color: var(--faint); }

.sector { background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 8px 16px; }
.srow { display: grid; grid-template-columns: 96px 1fr 62px;
        align-items: center; gap: 10px; padding: 5px 0; }
.srow .lbl { font-size: 13px; color: var(--muted); }
.srow .num { font-size: 13px; font-weight: 600; text-align: right;
             font-variant-numeric: tabular-nums; }
.bar { position: relative; height: 8px; background: transparent; }
.bar::before {
  content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px;
  width: 1px; background: var(--line);
}
.bar i { position: absolute; top: 0; height: 8px; border-radius: 2px;
         background: currentColor; opacity: 0.85; }

table { width: 100%; border-collapse: collapse; background: var(--panel);
        border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
th, td { padding: 10px 16px; text-align: right; font-size: 14px;
         font-variant-numeric: tabular-nums; }
th { font-size: 12px; color: var(--faint); font-weight: 600;
     border-bottom: 1px solid var(--line); }
th:first-child, td:first-child { text-align: left; }
tbody tr + tr td { border-top: 1px solid var(--line); }
td.nm { font-weight: 600; }
td .tk { color: var(--faint); font-size: 12px; font-weight: 400; margin-left: 6px; }
td.pct { font-weight: 700; }

.news { background: var(--panel); border: 1px solid var(--line);
        border-radius: 12px; padding: 4px 16px; }
.news .item { padding: 11px 0; }
.news .item + .item { border-top: 1px solid var(--line); }
.news a { color: var(--text); text-decoration: none; font-size: 14.5px;
          line-height: 1.45; }
.news a:hover { text-decoration: underline; }
.news .orig { color: var(--muted); font-size: 12.5px; margin-top: 3px;
              line-height: 1.4; }
.news .meta { color: var(--faint); font-size: 12px; margin-top: 3px; }

footer { margin-top: 40px; color: var(--faint); font-size: 12px;
         border-top: 1px solid var(--line); padding-top: 14px; }

@media (max-width: 980px) {
  .idx { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 720px) {
  .idx { grid-template-columns: 1fr; }
  .gauges, .live { grid-template-columns: repeat(2, 1fr); }
  .two { grid-template-columns: 1fr; }
  .card .val { font-size: 24px; }
  body { padding: 20px 14px 48px; }
}
"""


def earn_badge(ticker: str, earnings: dict) -> str:
    """실적 배지. 방금 발표했으면 그걸 먼저, 아니면 다음 예정일을 보여준다."""
    e = earnings.get(ticker)
    if not e:
        return ""
    today = date.today()
    last = e.get("last")
    if last and 0 <= (today - last).days <= 3:
        return '<span class="badge hot">실적 발표함</span>'
    d = e.get("next")
    if d is None:
        return ""
    dd = (d - today).days
    if dd < 0:
        return ""
    if dd == 0:
        return '<span class="badge soon">실적 오늘</span>'
    if dd <= 7:
        return f'<span class="badge soon">실적 D-{dd}</span>'
    return f'<span class="badge">실적 {d:%m/%d}</span>'


def render(anchor, indexes, gauges, sectors, watch, news, summary=None,
           live=None, perf_rows=None, earnings=None, fx=None, econ=None,
           chart=None, movers=None, breadth=None, tnews=None, navs=None) -> str:
    up = "#16a34a" if config.COLOR_STYLE == "us" else "#dc2626"
    down = "#dc2626" if config.COLOR_STYLE == "us" else "#2563eb"
    css = (CSS.replace("__UP__", up).replace("__DOWN__", down)
              .replace("__DARK__", DARK))

    now = datetime.now(KST)
    parts: list[str] = []

    # AI 핵심 요약 (키가 있을 때만)
    if summary:
        lines = "".join(
            f'<div class="line"><div class="n">{i}</div>'
            f'<div class="tx">{esc(s)}</div></div>'
            for i, s in enumerate(summary, 1)
        )
        parts.append('<h2>핵심 요약<span class="ai-tag">AI 생성</span></h2>'
                     '<div class="summary">' + lines + "</div>")

    # 지수 카드
    cards = []
    for q, name in indexes:
        t = tone(q["pct"])
        cards.append(
            f'<a class="card q" href="{yurl(q["ticker"])}" target="_blank" '
            f'rel="noopener"><div class="name">{esc(name)}</div>'
            f'<div class="val">{q["last"]:,.2f}</div>'
            f'<div class="chg {t}">{q["chg"]:+,.2f} ({q["pct"]:+.2f}%)</div>'
            f'<div class="{t}">{sparkline(q["series"])}</div></a>'
        )
    parts.append('<h2>지수</h2><div class="idx">' + "".join(cards) + "</div>")

    # 지금 이 순간 — 선물·비트코인 (미국장 마감 뒤에도 계속 거래되는 것들)
    if live:
        gs, at = [], None
        for q, name in live:
            t = tone(q["live_pct"])
            at = at or q.get("live_at")
            gs.append(
                f'<a class="g q" href="{yurl(q["ticker"])}" target="_blank" '
                f'rel="noopener"><div class="name">{esc(name)}</div>'
                f'<div class="val">{q["live"]:,.2f}</div>'
                f'<div class="chg {t}">{q["live_pct"]:+.2f}%</div></a>'
            )
        stamp = f"{at.astimezone(KST):%H:%M} KST 기준" if at is not None else "실시간"
        parts.append(
            f'<h2><span class="dot"></span>지금'
            f'<span class="ai-tag">{stamp} · 미국장 마감 대비</span></h2>'
            '<div class="live">' + "".join(gs) + "</div>"
        )

    # 경제지표 캘린더 (앞으로 예정)
    if econ:
        rows = []
        for e in econ:
            cls = {3: "h", 2: "m"}.get(e["level"], "l")
            soon = " soon" if e["dday"] <= 7 else ""
            wd = "월화수목금토일"[e["d"].weekday()]
            dd = "오늘" if e["dday"] == 0 else f"D-{e['dday']}"
            rows.append(
                f'<tr><td class="dday{soon}">{dd}</td>'
                f'<td>{e["d"]:%m/%d}({wd})</td>'
                f'<td class="lv {cls}">{"●" * e["level"]}</td>'
                f'<td class="nm">{esc(e["name"])}</td>'
                f'<td class="src">{esc(e["source"])}</td></tr>'
            )
        parts.append(
            '<h2>경제 캘린더<span class="ai-tag">미국 정부 공식 발표 일정</span></h2>'
            '<div class="tablewrap"><table class="econ"><thead><tr>'
            "<th></th><th>날짜</th><th>중요도</th><th>지표</th><th>출처</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        )

    # 1년 추이 차트
    if chart:
        parts.append('<h2>1년 추이<span class="ai-tag">1년 전을 100으로 놓고 비교'
                     '</span></h2>' + chart)

    # 기간별 성과
    if perf_rows:
        cols = ["1주", "1개월", "3개월", "6개월", "연초대비"]
        head = "".join(f"<th>{c}</th>" for c in cols)
        rows = []
        for name, p in perf_rows:
            tds = []
            for c in cols:
                v = p.get(c)
                tds.append(f'<td class="{tone(v)}">{v:+.2f}%</td>' if v is not None
                           else '<td class="flat">—</td>')
            rows.append(f'<tr><td class="nm">{esc(name)}</td>' + "".join(tds) + "</tr>")
        parts.append(
            '<h2>기간별 성과</h2><div class="tablewrap"><table><thead><tr>'
            f"<th>지수</th>{head}</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
        )

    # 시장 폭 — 지수가 넓게 올랐는지, 몇 개가 끌어올렸는지
    if breadth:
        cap, eq, gap = breadth["cap"], breadth["eq"], breadth["gap"]
        if gap > 0.15:
            verdict = "소수 대형주가 지수를 끌어올린 장입니다."
        elif gap < -0.15:
            verdict = "대형주보다 그 외 종목들이 더 오른 장입니다."
        else:
            verdict = "대형주와 나머지가 고르게 움직인 장입니다."
        parts.append(
            '<h2>시장 폭<span class="ai-tag">같은 500 종목을 시총가중(SPY)과 '
            '동일가중(RSP)으로 각각 담은 ETF 비교</span></h2><div class="breadth">'
            f'<div class="big">시총가중 SPY '
            f'<b class="{tone(cap["pct"])}">{cap["pct"]:+.2f}%</b> · 동일가중 RSP '
            f'<b class="{tone(eq["pct"])}">{eq["pct"]:+.2f}%</b> '
            f'<span class="{tone(gap)}">(차이 {gap:+.2f}%p)</span></div>'
            f'<div>섹터 {breadth["sector_all"]}개 중 '
            f'<b>{breadth["sector_up"]}개</b> 상승</div>'
            f'<div class="note">{esc(verdict)}</div></div>'
        )

    # 시장 지표 — 성격별로 나눠 보여준다
    for gname, items in (gauges or []):
        if not items:
            continue
        gs = []
        for q, name, kind in items:
            t = tone(q["pct"])
            gs.append(
                f'<a class="gauge q" href="{yurl(q["ticker"])}" target="_blank" '
                f'rel="noopener"><div class="name">{esc(name)}</div>'
                f'<div class="val">{fmt_value(q["last"], kind)}</div>'
                f'<div class="chg {t}">{fmt_change(q, kind)}</div></a>'
            )
        extra = ""
        if gname == "금리":
            # 10년물이 3개월물보다 낮으면 '장단기 금리 역전' — 침체 신호로 본다
            by = {q["ticker"]: q for q, _, _ in items}
            t10, t3 = by.get("^TNX"), by.get("^IRX")
            if t10 and t3:
                sp = t10["last"] - t3["last"]
                word = "역전 · 침체 신호" if sp < 0 else "정상"
                extra = (f'<span class="ai-tag">장단기차(10년−3개월) '
                         f'{sp:+.2f}%p · {word}</span>')
        elif gname == "해외 지수":
            # 아시아·유럽장은 미국장보다 먼저 닫힌다. 날짜만 맞춰 비교한다.
            extra = '<span class="ai-tag">각 시장의 같은 날짜 종가</span>'
        parts.append(f'<h2>{esc(gname)}{extra}</h2>'
                     '<div class="gauges">' + "".join(gs) + "</div>")

    # 섹터
    if sectors:
        peak = max(abs(q["pct"]) for q, _ in sectors) or 1.0
        rows = []
        for q, name in sectors:
            t = tone(q["pct"])
            w = abs(q["pct"]) / peak * 50          # 가운데 기준 최대 50%
            style = (f"left:50%;width:{w:.1f}%" if q["pct"] >= 0
                     else f"right:50%;width:{w:.1f}%")
            rows.append(
                f'<div class="srow"><div class="lbl">'
                f'<a class="lnk" href="{yurl(q["ticker"])}" target="_blank" '
                f'rel="noopener">{esc(name)}</a></div>'
                f'<div class="bar {t}"><i style="{style}"></i></div>'
                f'<div class="num {t}">{q["pct"]:+.2f}%</div></div>'
            )
        parts.append('<h2>섹터별 흐름</h2><div class="sector">' + "".join(rows) + "</div>")

    # 오늘의 특징주 — 관심종목 밖에서 벌어진 일
    if movers:
        ups, downs = movers

        def mv_block(title, rows_):
            r = "".join(
                f'<div class="row"><div>'
                f'<a class="lnk" href="{yurl(q["ticker"])}" target="_blank" '
                f'rel="noopener"><b>{esc(nm)}</b>'
                f'<span class="tk">{esc(q["ticker"])}</span></a></div>'
                f'<div class="p {tone(q["pct"])}">{q["pct"]:+.2f}%</div></div>'
                for q, nm in rows_)
            return (f'<div class="mv"><div class="cap">{title}</div>'
                    + r + "</div>")

        parts.append(
            '<h2>오늘의 특징주<span class="ai-tag">미국 대형주 40개 중</span></h2>'
            '<div class="two">'
            + mv_block("많이 오른 종목", ups)
            + mv_block("많이 내린 종목", downs) + "</div>"
        )

    # 관심종목
    if watch:
        earnings = earnings or {}
        fx_pct = (fx["pct"] / 100) if fx else 0.0
        rows = []
        for q, name in watch:
            t = tone(q["pct"])
            # 원화 기준 = 주가 등락과 환율 등락을 곱해서 합친 값
            krw = ((1 + q["pct"] / 100) * (1 + fx_pct) - 1) * 100
            th = tone(q["from_hi"])
            rows.append(
                f'<tr><td class="nm">'
                f'<a class="lnk" href="{yurl(q["ticker"])}" target="_blank" '
                f'rel="noopener">{esc(name)}'
                f'<span class="tk">{esc(q["ticker"])}</span></a>'
                f'{earn_badge(q["ticker"], earnings)}</td>'
                f'<td>{q["last"]:,.2f}</td>'
                f'<td class="pct {t}">{q["pct"]:+.2f}%</td>'
                f'<td class="pct {tone(krw)}">{krw:+.2f}%</td>'
                f'<td class="{th}"><span class="pos">'
                f'<i style="left:{max(0, min(100, q["pos52"])):.0f}%"></i></span>'
                f'{q["from_hi"]:+.1f}%</td></tr>'
            )
        parts.append(
            '<h2>관심종목<span class="ai-tag">원화 기준 = 주가 등락 × 환율 등락</span></h2>'
            '<div class="tablewrap"><table><thead><tr><th>종목</th><th>종가</th>'
            "<th>등락률</th><th>원화 기준</th><th>52주 고점 대비</th>"
            "</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>"
        )

    # 뉴스
    if news:
        items = []
        for n in news:
            when = (datetime.fromtimestamp(n["ts"], KST).strftime("%m/%d %H:%M")
                    if n["ts"] else "")
            meta = " · ".join(x for x in (esc(n["publisher"]), when) if x)
            ko = n.get("ko")
            # 번역이 있으면 한글을 제목으로, 원문은 아래에 작게 (대조 확인용)
            head = esc(ko) if ko else esc(n["title"])
            orig = f'<div class="orig">{esc(n["title"])}</div>' if ko else ""
            items.append(
                f'<div class="item"><a href="{esc(n["link"])}" target="_blank" '
                f'rel="noopener">{head}</a>{orig}'
                f'<div class="meta">{meta}</div></div>'
            )
        tag = ('<span class="ai-tag">AI 번역 · 원문 병기</span>'
               if any(n.get("ko") for n in news) else "")
        parts.append(f'<h2>주요 뉴스{tag}</h2><div class="news">'
                     + "".join(items) + "</div>")

    # 내 종목 뉴스 — 시장 전체가 아니라 관심종목에 붙은 소식만
    if tnews:
        items = []
        for n in tnews:
            when = _iso_kst(n["pub"])
            meta = " · ".join(x for x in (
                esc(n["publisher"]), f"{when:%m/%d %H:%M}" if when else "") if x)
            ko = n.get("ko")
            head = esc(ko) if ko else esc(n["title"])
            orig = f'<div class="orig">{esc(n["title"])}</div>' if ko else ""
            items.append(
                f'<div class="item"><div class="who">{esc(n["name"])}'
                f'<span class="tk">{esc(n["ticker"])}</span></div>'
                f'<a href="{esc(n["link"])}" target="_blank" rel="noopener">'
                f'{head}</a>{orig}'
                f'<div class="meta">{meta}</div></div>')
        parts.append('<h2>내 종목 뉴스</h2><div class="news tnews">'
                     + "".join(items) + "</div>")

    # 지난 브리핑으로 이동
    if navs:
        links = "".join(f'<a href="{esc(fn)}">{esc(day)}</a>' for fn, day in navs)
        parts.append('<h2>지난 브리핑</h2><div class="nav">' + links + "</div>")

    body = "".join(parts)
    return (
        '<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>미국장 브리핑 · {anchor:%Y-%m-%d}</title>\n"
        f"<style>{css}</style></head><body><div class=\"wrap\">\n"
        "<header><h1>전날 미국장 브리핑</h1>\n"
        f'<div class="sub">기준일 <b>{anchor:%Y년 %m월 %d일}</b> 미국장 마감 종가 · '
        f"생성 {now:%Y-%m-%d %H:%M} KST</div></header>\n"
        f"{body}\n"
        "<footer>데이터 출처 야후파이낸스(yfinance). "
        "투자 판단의 근거로 삼기 전에 반드시 원본을 확인하세요.</footer>\n"
        "</div></body></html>"
    )


def to_artifact(html: str) -> str:
    """웹에 올릴 때 쓰는 형태로 바깥 껍데기를 벗긴다.

    게시 플랫폼이 <!doctype>·<html>·<head>·<body> 를 알아서 감싸므로
    제목·스타일·본문만 남긴다. 내용과 디자인은 그대로다.
    """
    title = re.search(r"<title>(.*?)</title>", html, re.S)
    style = re.search(r"<style>(.*?)</style>", html, re.S)
    body = re.search(r'<div class="wrap">.*</div>', html, re.S)
    if not (title and style and body):
        return ""
    # 게시물 이름은 날짜를 뺀 고정 이름으로. 매일 이름이 바뀌면
    # 목록에서 같은 문서인지 알아보기 어렵다. 날짜는 본문 머리에 이미 있다.
    name = title.group(1).split("·")[0].strip() or "미국장 브리핑"
    return "\n".join([
        f"<title>{name}</title>",
        f"<style>{style.group(1)}</style>",
        body.group(0),
        "",
    ])


# ── 히스토리 누적 ───────────────────────────────────────────
def save_history(close: pd.DataFrame, anchor, tracked: list[tuple[str, str]]) -> int:
    """1년치를 통째로 다시 쓴다.

    매일 한 줄씩 쌓기를 기다릴 필요 없이, 받아온 과거 시세로 파일을 소급해서 채운다.
    매 실행마다 전체를 새로 쓰므로 중간에 며칠 건너뛰어도 저절로 메워진다.
    """
    cols = [t for t, _ in tracked if t in close.columns]
    if not cols:
        return 0
    sub = close[cols]
    sub = sub[sub.index <= anchor].dropna(how="all")
    # 비트코인은 주말에도 거래되므로 그냥 두면 주식 칸이 텅 빈 행이 생긴다.
    # 미국장이 열린 날만 남긴다.
    for ref in ("^GSPC", "^DJI", "^IXIC"):
        if ref in sub.columns:
            sub = sub[sub[ref].notna()]
            break
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date"] + cols)
        for dt, row in sub.iterrows():
            w.writerow([f"{dt:%Y-%m-%d}"]
                       + ["" if pd.isna(v) else round(float(v), 4) for v in row])
    return len(sub)


# ── 실행 ───────────────────────────────────────────────────
def main():
    watch_items = list(config.WATCHLIST.items())
    tickers = ([t for t, _ in INDEXES] + [t for t, _, _ in GAUGES]
               + [t for t, _ in SECTORS] + [t for t, _ in watch_items]
               + [t for t, _ in LIVE] + [t for t, _ in BREADTH]
               + list(MOVERS))

    print(f"데이터 수집 중… (티커 {len(set(tickers))}개)")
    close = fetch_closes(tickers)
    anchor = anchor_date(close)
    print(f"기준일: {anchor:%Y-%m-%d} (미국장 마감)")

    def collect(spec, has_kind=False):
        out = []
        for item in spec:
            ticker, name = item[0], item[1]
            q = quote(close, ticker, anchor)
            if q is None:
                print(f"  건너뜀: {ticker} (데이터 없음)")
                continue
            out.append((q, name, item[2]) if has_kind else (q, name))
        return out

    indexes = collect(INDEXES)
    gauge_groups = [(gname, collect(items, has_kind=True))
                    for gname, items in GAUGE_GROUPS]
    sectors = collect(SECTORS)
    watch = collect(watch_items)

    # 1년 추이 차트 (지수들을 같은 축에서 비교)
    cols = [t for t, _ in INDEXES if t in close.columns]
    names = [n for t, n in INDEXES if t in close.columns]
    cdf = close[cols]
    chart = line_chart(cdf[cdf.index <= anchor], names) if cols else ""

    movers = top_movers(close, anchor, MOVERS, 5)
    breadth = market_breadth(close, anchor, sectors)
    print(f"특징주 상승 {len(movers[0])} / 하락 {len(movers[1])}")

    if not indexes:
        raise SystemExit("지수 데이터를 하나도 받지 못했습니다.")

    # 지금 이 순간 값 (선물·비트코인)
    print("실시간 시세 수집 중…")
    live_raw = fetch_live([t for t, _ in LIVE])
    live = []
    for t, name in LIVE:
        q, lv = quote(close, t, anchor), live_raw.get(t)
        if not q or not lv or not q["last"]:
            continue
        q = dict(q)
        q["live"], q["live_at"] = lv["value"], lv["at"]
        q["live_pct"] = (lv["value"] / q["last"] - 1) * 100
        live.append((q, name))
    print(f"  {len(live)}개")

    print("실적 발표일 조회 중…")
    earnings = fetch_earnings([t for t, _ in watch_items])
    print(f"  {len(earnings)}개")

    perf_rows = [(name, perf(close, t, anchor)) for t, name in INDEXES
                 if t in close.columns]
    fx = quote(close, "KRW=X", anchor)

    econ = []
    if getattr(config, "ECON_CALENDAR", True):
        print("경제지표 일정 수집 중…")
        try:
            econ = econ_calendar.upcoming(getattr(config, "ECON_DAYS", 30))
            print(f"  {len(econ)}건")
        except Exception as e:
            print(f"  경제지표 일정 건너뜀: {type(e).__name__} — {e}")

    print("뉴스 수집 중…")
    news = fetch_news(config.NEWS_QUERIES, config.NEWS_COUNT)
    print(f"  헤드라인 {len(news)}건")

    print("종목별 뉴스 수집 중…")
    tnews = fetch_ticker_news(watch_items, per_ticker=1,
                              total=getattr(config, "TICKER_NEWS_COUNT", 8))
    print(f"  {len(tnews)}건")

    OUT_DIR.mkdir(exist_ok=True)
    navs = past_briefs(f"{anchor:%Y-%m-%d}")

    ai = ai_summary(anchor, indexes, sectors, news, watch, tnews, movers)
    summary = ai.get("summary") or None
    ko_list = ai.get("news_ko") or []
    tko_list = ai.get("tnews_ko") or []
    for n, ko in zip(news, ko_list):
        n["ko"] = ko
    for n, ko in zip(tnews, tko_list):
        n["ko"] = ko
    if summary:
        print(f"AI 요약 {len(summary)}줄 생성"
              + (f", 헤드라인 {len(ko_list) + len(tko_list)}건 번역"
                 if ko_list or tko_list else ""))

    html = render(anchor, indexes, gauge_groups, sectors, watch, news, summary,
                  live=live, perf_rows=perf_rows, earnings=earnings, fx=fx,
                  econ=econ, chart=chart, movers=movers, breadth=breadth,
                  tnews=tnews, navs=navs)

    OUT_DIR.mkdir(exist_ok=True)
    dated = OUT_DIR / f"brief-{anchor:%Y-%m-%d}.html"
    latest = OUT_DIR / "latest.html"
    dated.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    # 웹사이트는 index.html 을 첫 화면으로 찾는다
    (OUT_DIR / "index.html").write_text(html, encoding="utf-8")

    # 웹 게시용(아티팩트) 사본
    art = to_artifact(html)
    if art:
        (OUT_DIR / "artifact.html").write_text(art, encoding="utf-8")

    if config.SAVE_HISTORY:
        tracked = list(INDEXES) + [(t, n) for t, n, _ in GAUGES]
        n = save_history(close, anchor, tracked)
        print(f"history.csv {n}일치 기록")

    print(f"\n완료 → {dated}")
    for q, name in indexes:
        print(f"  {name:<12} {q['last']:>10,.2f}  {q['pct']:+.2f}%")

    if config.AUTO_OPEN and not os.environ.get("BRIEF_NO_OPEN"):
        webbrowser.open(latest.resolve().as_uri())


if __name__ == "__main__":
    _trim_log()
    with LOG.open("a", encoding="utf-8") as _fh:
        _fh.write(f"\n{'=' * 58}\n"
                  f"{datetime.now(KST):%Y-%m-%d %H:%M:%S} KST 실행 시작\n")
        _orig = sys.stdout
        sys.stdout = _Tee(_orig, _fh)
        try:
            main()
            print("== 정상 종료 ==")
        except Exception:
            import traceback
            print("== 오류로 중단 ==")
            traceback.print_exc(file=sys.stdout)
            sys.stdout = _orig
            raise
        finally:
            sys.stdout = _orig
