# 개발 계획 (Claude Code 참고용)

## 프로젝트 개요
경제 문맹 청년을 위한 AI 투자 학습 앱. PER, 금리 등 지표를 해설해주고, 포트폴리오를 트리맵으로 시각화하는 웹앱.

## 기술 스택
- 언어: Python
- 프론트엔드/백엔드: Streamlit (단일 프레임워크로 화면+로직 처리)
- 시각화: Plotly (트리맵)
- 데이터베이스: SQLite (MVP 단계, 추후 PostgreSQL 등으로 이전 가능)
- 외부 API:
  - DART Open API (공시·재무제표, 연동 완료)
  - 한국투자증권 KIS Developers API (실시간 주가, python-kis 라이브러리 사용 예정)
  - LLM API (Claude 또는 GPT, 지표 해설 문장 생성용, 아직 미연동)
- 환경변수 관리: python-dotenv (.env 파일, API 키는 반드시 .env로 분리하고 .gitignore에 포함)

## 현재까지 진행 상태
- [x] Streamlit 기본 화면 뼈대 (종목 등록 → 트리맵 → 지표 해설)
- [x] DART Open API 연동 (`dart_client.py`, 회사개황·재무제표·지표 계산)
- [x] SQLite DB 도입 (`db.py`, MOCK_STOCK_DATA 대체 완료)
- [x] 무료 슬롯 제한 로직 (3종목) — DB 기반 `add_stock(slot_limit=)` 으로 재구현 완료
- [x] 주가 조회 — **pykrx(KRX)로 먼저 구현** (`price_client.py`). 전 거래일 종가 기준
- [x] LLM API 연동 (`llm_client.py`) — 코드·가드레일·캐시 완료, `ANTHROPIC_API_KEY` 투입만 남음
- [x] API 키 관리 통합 (`config.py`) + `.gitignore` 추가
- [ ] 한국투자증권 KIS API 연동 — **보류** (실계좌 개설 필요. pykrx로 충분히 동작 중)

## 데이터베이스 설계 (SQLite) — 실제 구현 기준

DB 파일: `SetUp/data/app.db` · 스키마 버전 v2 (`meta.schema_version`)

**계획서 초안에서 바뀐 점:** 처음에는 `stocks` 한 테이블에 PER·EPS·주가를 같이 두려 했으나,
셋의 **갱신 주기가 완전히 달라서** 테이블을 분리했다.
회사 목록은 거의 안 바뀌고(주 단위), 재무는 분기·연 단위, 주가는 매일 바뀐다.
한 테이블에 두면 주가를 갱신할 때마다 회사 정보 행 전체를 건드려야 한다.
또 DART의 키는 종목코드가 아니라 `corp_code`(8자리)라서 이것을 PK로 삼아야 한다.

### stocks 테이블 (종목 마스터, DART corpCode.xml 전량)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| corp_code | TEXT PRIMARY KEY | DART 고유번호 8자리 |
| corp_name | TEXT | 회사명 |
| stock_code | TEXT | 종목코드 6자리 (비상장이면 빈 문자열) |
| is_listed | INTEGER | 상장 여부 (검색 시 상장사 우선 노출용) |
| updated_at | TEXT | 마지막 갱신 시각 |

### portfolio 테이블 (사용자 보유 종목)
| 컬럼 | 타입 | 설명 |
|---|---|---|
| id | INTEGER PRIMARY KEY AUTOINCREMENT | 고유 ID |
| user_id | TEXT | 사용자 식별자 (로그인 전까지 `'local'` 고정) |
| corp_name / corp_code / stock_code | TEXT | 종목 식별 |
| shares | REAL | **보유 주식 수** (비중은 저장하지 않고 주가와 함께 매번 계산) |
| memo | TEXT | 사용자 메모 |
| added_at | TEXT | 등록 시각 |

### financial_cache 테이블 (DART 재무, TTL 7일)
`(corp_code, bsns_year, reprt)` 복합 PK + `payload`(JSON) + `fetched_at`
→ DART 무료 키의 하루 20,000건 제한을 아끼기 위함

### price_cache 테이블 (KRX 일별 시세, TTL 30분)
`(stock_code, trade_date)` 복합 PK + OHLCV + `change_pct` + `fetched_at`
→ 거래일별로 쌓이므로 나중에 기간 차트에 그대로 재사용 가능

## 다음 작업 순서 (우선순위)

### 1. SQLite DB 도입 — [x] 완료
- [x] `db.py` 신설: `init_db()`, `sync_stock_master()`, `search_stocks()`, `get_stock()` 구현
      (계획서의 `save_stock`/`get_all_stocks`는 실제 쓰임에 맞춰 위 이름으로 구현)
- [x] `app.py`의 `MOCK_STOCK_DATA` 참조를 DB 조회로 교체 (mock은 DART 실패 시 fallback으로만 잔존)
- [x] 포트폴리오 영구 저장 (`portfolio` 테이블) — 앱 재시작해도 유지
- [x] 스키마 버전 관리 + 마이그레이션 (`_migrate()`, 현재 v2)

### 2. KIS API 연동 — [ ] 보류 (사용자 결정: 계좌 개설 전까지 pykrx 사용)
- [x] **선행 대체 구현**: `price_client.py` (pykrx) 로 KRX 종가 조회. 계좌 없이 즉시 사용 가능
- [ ] `python-kis` 설치 및 `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT` 발급
- [ ] `kis_client.py` 신설: `get_current_price(stock_code)` (장중 실시간)
- [x] 주가 + DART EPS 결합 → PER 계산 후 저장 (`compute_indicators`, `price_cache` 테이블)
- 참고: KIS는 한국투자증권 **실계좌 개설**이 필요. 계좌가 없으면 pykrx만으로도 앱은 완전히 동작함

### 3. DART 재무제표 → EPS 계산 로직 — [x] 완료
- [x] `dart_client.get_shares_outstanding()` — 발행주식총수 조회 (`stockTotqySttus`)
- [x] `dart_client.compute_indicators()` — EPS = 당기순이익 / 상장주식수
- [x] 추가로 BPS, ROE, 부채비율, 영업이익률, PER, PBR 까지 계산

### 4. 종목 검색/선택 UX 개선 — [x] 완료
- [x] 종목명 검색 → DB `stocks` 테이블에서 부분일치 검색 후 선택
- [x] 계획의 "지연 로딩" 대신 **전체 마스터 일괄 동기화** 채택.
      DART `corpCode.xml`이 전체 목록을 한 번에 주므로(11만 건, 14MB) 매번 API를 치는 것보다
      한 번 받아두는 쪽이 빠르고 API 호출 수도 적음. 사이드바 버튼으로 재동기화 가능

### 5. LLM API 연동 (지표 해설) — [x] 코드 완료 / 키 투입 대기
- [x] `llm_client.py` 신설 (모델 `claude-opus-5`, effort `low`)
- [x] `app.py`에 연결 — 사이드바 "AI 지표 해설" 토글, 실패 시 규칙 기반으로 자동 fallback
- [x] 시스템 프롬프트에 예측성 표현 금지 명시 (매수/매도/오른다/유망/목표주가 등)
- [x] **2중 방어선**: 프롬프트만 믿지 않고, 생성된 문장을 `contains_forbidden()` 으로 재검사.
      금지 표현이 섞이면 그 응답을 버리고 규칙 기반 해설로 되돌린다 (DB에도 저장하지 않음)
- [x] 화면에 "투자 권유가 아님" 고지 문구 상시 노출
- [x] `explanation_cache` 테이블에 해설 캐시 (지표값 SHA-256 해시가 키, 30일)
- [x] 키 관리를 `config.py` 로 통합 (환경변수 → SetUp → work → work/Security 순 탐색)
- [ ] **남은 일: `ANTHROPIC_API_KEY` 발급 후 `Security/ANTHROPIC_API_KEY.env` 에 저장**
      키를 넣으면 코드 수정 없이 바로 AI 해설로 전환됨 (현재는 규칙 기반으로 동작 중)

### 6. 무료 슬롯 제한 로직 재구현 — [x] 완료
- [x] `st.session_state` → DB 기반으로 변경 (`db.add_stock(slot_limit=3)`, `SlotLimitExceeded`)
- [ ] 튜토리얼 완료 시 슬롯 확장 — 이번 스코프 아님 (그대로 유지)

### 7. 보유 비중 계산 — [x] 완료 (계획서에 없던 추가 작업)
- [x] 등록 시 **보유 수량**을 입력받고, 비중 = (보유수량 × 현재주가) ÷ 전체 평가금액 으로 계산
- [x] 비중은 주가에 따라 매일 바뀌므로 DB에 저장하지 않고 화면을 그릴 때마다 계산
- [x] 트리맵 크기를 평가금액 기준으로 변경

## 이번 스코프에 포함하지 않는 것 (다음 단계로 미룸)
- 게이미피케이션 (퀴즈, 뱃지, 튜토리얼)
- 네이티브 광고 (타일 로고 전환)
- 종목별 커뮤니티
- 뉴스 큐레이션
- 사용자 로그인/회원가입

## 코딩 시 지켜야 할 원칙
- API 키, 시크릿 값은 코드에 하드코딩 금지, 반드시 `.env`에서 로드
  → `config.py` 로 통합. `.gitignore` 에 `*.env` 포함
- 지표 해설 문구는 "오른다/내린다" 같은 예측성 표현 금지 — 객관적 지표 설명에 그칠 것 (투자자문업 규제 회피)
  → 시스템 프롬프트 + `llm_client.contains_forbidden()` 사후 검사 2중 방어
- DB 스키마 변경 시 기존 데이터 마이그레이션 고려
  → `db._migrate()` + `meta.schema_version` (현재 v3)
- 함수는 파일별로 역할 분리

## 현재 파일 구성
| 파일 | 역할 |
|---|---|
| `app.py` | Streamlit 화면 + 흐름 제어 |
| `config.py` | API 키 로딩 (환경변수 / `.env` 탐색) |
| `db.py` | SQLite — 포트폴리오, 종목 마스터, 재무·주가·해설 캐시 |
| `dart_client.py` | DART 공시 API (재무제표, 발행주식수, 지표 계산) |
| `price_client.py` | pykrx (KRX 종가) |
| `llm_client.py` | Claude 지표 해설 + 규제 가드레일 |
| `kis_client.py` | (미작성 — KIS 실시간 시세, 보류) |
