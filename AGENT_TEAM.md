# 투자 에이전트 팀 (8명)

전략: **10종목 매수 → 종목별 매수가 대비 -5% 손절 / +15% 익절**

손익비가 1:3이라 한 종목에서 **익절이 먼저 나올 확률이 25%를 넘으면** 기대값이 플러스입니다.
그래서 모든 에이전트는 "좋은 회사인가?"보다 **"-5%를 먼저 찍기 전에 +15%에 먼저 닿을 종목인가?"**를 기준으로 판단합니다.

## 팀 구성 (`.claude/agents/`)

| # | 에이전트 | 역할 | 주 데이터 |
|---|---|---|---|
| ① | `chart-analyst` | 차트분석 — 추세·모멘텀·변동성, 전략 백테스트 | pykrx / 네이버 시세 |
| ② | `fundamental-analyst` | 회사가치 기본 분석 — 수익성·안정성·밸류에이션 | DART 재무 |
| ③ | `news-analyst` | 뉴스분석 — 촉매, 악재, 이벤트 일정 | 웹검색, 공시 |
| ④ | `sentiment-analyst` | 일반투자자 분위기 — 커뮤니티 톤, 개인/외국인/기관 수급 | pykrx 수급, 웹검색 |
| ⑤ | `bull-advocate` | 매수 찬성 — 가장 강한 매수 논리 변론 | ①~④,⑦ 리포트 |
| ⑥ | `bear-advocate` | 매수 반대 — 리스크 레드팀 | ①~④,⑦ 리포트 |
| ⑦ | `fx-analyst` | 환율분석 — 외국인 수급·수출입 업종 영향 | frankfurter(ECB), 웹검색 |
| ⑧ | `decision-maker` | 결정권자 — 최종 10종목 + 손절/익절가 확정 | 전체 리포트 |

## 진행 순서 (`/pick-10`)

```
0. 후보군 30종목 구성 (시총 상위 + 거래대금 ≥100억)
1. ①②③④⑦ 병렬 분석  ──►  reports/<날짜>/01~04, 07
2. ⑤ 찬성 vs ⑥ 반대 토론 (+ 반박 1회)  ──►  05, 06
3. ⑧ 결정권자 최종 선정  ──►  08_decision.md
```

사용법 (저장소 루트에서 Claude Code 실행 후):

```
/pick-10
/pick-10 반도체·조선 위주로 후보 25개
/pick-10 005930 000660 012450 ... (후보 직접 지정)
```

개별 에이전트만 호출할 수도 있습니다: "chart-analyst로 005930, 000660 분석해줘"

## 데이터 도구 (`SetUp/agent_tools/market_data.py`)

에이전트가 숫자를 지어내지 않도록 계산은 이 스크립트가 하고 JSON으로 돌려줍니다.

```bash
python SetUp/agent_tools/market_data.py screen --market ALL --top 60 --min-value 100
python SetUp/agent_tools/market_data.py chart 005930 000660           # 기술지표 + 전략 백테스트
python SetUp/agent_tools/market_data.py bracket 005930 --stop 5 --take 15 --horizon 60
python SetUp/agent_tools/market_data.py investor 005930 --days 20     # 개인/외국인/기관 순매수
python SetUp/agent_tools/market_data.py fundamental 삼성전자           # DART 재무지표
python SetUp/agent_tools/market_data.py fx                            # 원/달러 등 환율
```

`bracket`(전략 백테스트): 과거 1년 동안 매일 종가에 샀다고 가정하고, 이후 고가/저가로
+15% 익절과 -5% 손절 중 어느 쪽에 먼저 닿았는지 셉니다. 같은 날 둘 다 닿으면 보수적으로 손절 처리하고,
갭하락은 시가 체결로 반영합니다. 과거 통계일 뿐 미래 확률은 아닙니다.

## 필요한 설정

| 항목 | 용도 | 없으면 |
|---|---|---|
| `pip install -r SetUp/requirements.txt` | pykrx, pandas 등 | 도구 실행 불가 |
| `DART_API_KEY` | 재무제표 | 기업가치 분석가가 웹검색으로 대체 |
| `KRX_ID`, `KRX_PW` 환경변수 | 최신 pykrx의 시총·수급 조회 | 시세는 네이버로 대체, 스크리닝·수급은 웹검색으로 대체 |

## 모델 설정
- 결정권자는 `model: opus`, 나머지는 메인 세션 모델을 따릅니다(`inherit`).
  비용을 줄이려면 분석가 파일의 `model:` 을 `sonnet` 으로 바꾸세요.

> 이 팀의 결과는 분석 보조 자료이며 투자 권유가 아닙니다. 최종 판단과 책임은 투자자 본인에게 있습니다.
> (앱(`SetUp/app.py`)의 "매수·매도 표현 금지" 원칙과는 별개인, 개인 의사결정용 도구입니다.)
