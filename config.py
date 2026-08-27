# -*- coding: utf-8 -*-
"""
브리핑 설정 파일.
내용을 바꾸고 싶으면 이 파일만 고치면 됩니다. brief.py 는 건드릴 필요 없습니다.
고친 뒤에는 run.bat 을 다시 실행하면 바로 반영됩니다.
"""

# ── 관심종목 ────────────────────────────────────────────────
# 실제 목록은 같은 폴더의 watchlist.json 에 있습니다.
#
# 왜 따로 뒀나: watchlist.json 은 깃허브에 올라가지 않습니다.
# 저장소를 공개해도 내가 어떤 종목을 보는지는 남에게 드러나지 않습니다.
#
# 바꾸는 방법 (둘 중 편한 쪽으로):
#   · 바탕화면의 "관심종목 편집" 실행   ← 권장. 검색해서 버튼으로 추가/삭제
#   · watchlist.json 을 메모장으로 열어 직접 수정
#
# 파일이 없으면 관심종목 표와 '내 종목 뉴스'가 브리핑에서 빠집니다.
# 깃허브가 만드는 공개용 브리핑이 바로 이 상태입니다.
import json as _json
from pathlib import Path as _Path

try:
    WATCHLIST = _json.loads(
        (_Path(__file__).resolve().parent / "watchlist.json")
        .read_text(encoding="utf-8"))
except Exception:
    WATCHLIST = {}

# ── 뉴스 ───────────────────────────────────────────────────
NEWS_COUNT = 10                                   # 보여줄 헤드라인 개수
NEWS_QUERIES = ["stock market", "federal reserve", "wall street"]

# ── AI 핵심 요약 (선택 사항) ─────────────────────────────────
# 비워두면 요약 없이 헤드라인만 나옵니다. 나머지 기능은 전부 정상 동작합니다.
#
# 키는 이 파일이 아니라 같은 폴더의 .env 파일에 들어 있습니다.
# 설정과 비밀을 분리해 두면, config.py 를 남에게 보여줘도 키가 새지 않습니다.
#
#   .env 파일을 열어 이 줄을 고치세요:
#       GEMINI_API_KEY=여기에_키
#
# 키 발급: https://aistudio.google.com/apikey (구글 계정 로그인 → Create API key)
# 키가 비어 있으면 요약·번역만 빠지고 나머지는 정상 동작합니다.

# AI 요약·번역에 쓸 모델. 2026-08-27 기준 실제 작동을 확인한 값입니다.
# 나중에 "모델을 찾을 수 없다"는 오류가 나면 구글이 모델을 교체한 것이니,
# 오류 메시지가 알려주는 새 모델명으로 이 줄만 바꿔주세요.
#   (확인된 대안: gemini-flash-lite-latest, gemini-3-flash-preview)
GEMINI_MODEL = "gemini-3.6-flash"

# ── 화면 ───────────────────────────────────────────────────
# "us" = 상승 초록 / 하락 빨강  (미국식, 기본값)
# "kr" = 상승 빨강 / 하락 파랑  (한국식)
COLOR_STYLE = "us"

AUTO_OPEN = True      # 브리핑 생성 후 브라우저를 자동으로 열지
SAVE_HISTORY = True   # 매일 결과를 history.csv 에 누적할지 (나중에 비교 기능용)

# ── 경제지표 캘린더 ─────────────────────────────────────────
# 미국 정부 공식 소스에서 발표 일정을 가져옵니다.
#   노동통계국(CPI·고용·PPI) / 연준(FOMC) / 경제분석국(GDP·PCE)
# 하루 두 번까지만 실제로 접속하고 나머지는 cache 폴더의 결과를 씁니다.
ECON_CALENDAR = True
ECON_DAYS = 30        # 앞으로 며칠치를 보여줄지
