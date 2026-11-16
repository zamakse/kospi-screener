# 코스피 연속 음봉 스크리너

평일 20:20(KST)에 GitHub Actions가 코스피 보통주의 일봉을 수집해, 최근 봉부터 음봉(종가 < 시가)이 6개 이상 연속인 종목을 일봉·주봉 기준으로 찾아 `results/`에 저장합니다.

- `results/latest.json` / `results/latest.md`: 가장 최근 결과
- `results/YYYY-MM-DD.json`: 날짜별 기록
- 기준 시세: 네이버 금융 일봉(KRX 정규장 종가, 수정주가). NXT 시간외 체결 미반영
- 조건 변경: `screener.py` 상단의 `THRESHOLD`, `MIN_TRADING_VALUE`

조건에 맞는 종목을 걸러 주는 도구이며 매매 추천이 아닙니다.
