"""
경제교육 핀테크 앱 - MVP
실행 방법: 터미널에서 `streamlit run app.py` 입력

데이터 흐름:
  db.py          <- 포트폴리오 영구 저장 + 종목 마스터 + 조회 캐시 (SQLite)
  dart_client.py <- DART 재무제표 (ROE, 부채비율, EPS, BPS ...)
  price_client.py<- KRX 종가 (pykrx) -> PER, PBR 계산
"""

import streamlit as st
import plotly.express as px
import pandas as pd

import db

# DART 연동 모듈 (없거나 오류여도 앱은 예시 데이터로 계속 동작)
try:
    import dart_client
    _DART_AVAILABLE = True
    _DART_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001
    dart_client = None
    _DART_AVAILABLE = False
    _DART_IMPORT_ERROR = str(e)

# 주가 자동 수집 모듈 (pykrx). 없어도 수동 입력으로 동작
try:
    import price_client
    _PRICE_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    price_client = None
    _PRICE_AVAILABLE = False

# LLM 해설 모듈. 키가 없으면 규칙 기반 해설로 자동 fallback
try:
    import llm_client
    _LLM_IMPORTED = True
except Exception as e:  # noqa: BLE001
    llm_client = None
    _LLM_IMPORTED = False

# ----------------------------
# 1. 예시(mock) 데이터 - DART 연동 실패 시 fallback
# ----------------------------
MOCK_STOCK_DATA = {
    "삼성전자": {"PER": 25.3, "업종평균PER": 15.1, "금리민감도": "높음"},
    "SK하이닉스": {"PER": 18.7, "업종평균PER": 15.1, "금리민감도": "높음"},
    "카카오": {"PER": 40.2, "업종평균PER": 22.0, "금리민감도": "중간"},
    "NAVER": {"PER": 28.5, "업종평균PER": 22.0, "금리민감도": "중간"},
    "현대차": {"PER": 6.8, "업종평균PER": 9.5, "금리민감도": "낮음"},
}

FREE_SLOT_LIMIT = 3  # 무료 사용자 종목 등록 제한


# ----------------------------
# 2. DB 준비 (앱 실행당 1회)
# ----------------------------
@st.cache_resource
def setup_db():
    db.init_db()
    return db.cache_stats()


setup_db()


# ----------------------------
# 3. 데이터 조회 (DB 캐시 -> 없으면 API 호출 -> DB에 저장)
# ----------------------------
def fetch_from_dart(stock_name: str, reprt: str) -> dict:
    """
    DART 재무. 연도는 자동으로 '데이터가 있는 가장 최근 연도'를 사용한다.
    SQLite에 캐시된 최신 연도가 있으면(7일 이내) 그것을 쓰고, 없을 때만 API 호출.
    """
    master = db.get_stock(stock_name)
    corp_code = master["corp_code"] if master else None

    if corp_code:
        cached = db.get_latest_cached_financials(corp_code, reprt)
        if cached:
            cached["_from_cache"] = True
            return cached

    info = dart_client.get_stock_fundamentals(stock_name, year=None, reprt=reprt)
    code = corp_code or info.get("corp_code")
    if code:
        db.save_financials(code, info["연도"], reprt, info)
    return info


def fetch_price(ticker: str) -> dict:
    """KRX 종가. SQLite 캐시(30분)를 먼저 보고, 없을 때만 pykrx 호출."""
    cached = db.get_cached_price(ticker, max_age_minutes=30)
    if cached:
        return {
            "ticker": ticker, "price": cached["close"], "price_date": cached["date"],
            "change_pct": cached["change_pct"], "ohlcv": cached,
            "krx_valuation": None, "available": True,
            "note": f"{cached['date']} 종가 (DB 캐시)",
        }

    pinfo = price_client.get_price_info(ticker)
    if pinfo.get("available") and pinfo.get("ohlcv"):
        db.save_price(ticker, pinfo["ohlcv"])
    return pinfo


def value_portfolio(portfolio: list[dict], use_price: bool = True) -> tuple[list[dict], float, list[str]]:
    """
    보유 수량 x 현재 주가 = 평가금액, 그 합에서 각 종목이 차지하는 비율 = 보유 비중.

        비중(%) = (보유수량 x 주가) / 전체 평가금액 x 100

    비중을 DB에 저장하지 않는 이유: 주가가 매일 바뀌면 비중도 같이 바뀌기 때문에,
    변하지 않는 '보유 수량'만 저장하고 비중은 그릴 때마다 계산한다.

    반환: (행 목록, 전체 평가금액, 주가를 못 구한 종목명 목록)
    """
    rows, missing = [], []
    for h in portfolio:
        price = None
        if use_price and _PRICE_AVAILABLE and h.get("stock_code"):
            try:
                pinfo = fetch_price(h["stock_code"])
                if pinfo.get("available"):
                    price = pinfo["price"]
            except Exception:  # noqa: BLE001
                price = None

        shares = float(h.get("shares") or 0)
        if price is None and shares > 0:
            missing.append(h["corp_name"])

        rows.append({
            "id": h["id"],
            "종목": h["corp_name"],
            "종목코드": h.get("stock_code") or "-",
            "보유수량": shares,
            "현재가": price,
            "평가금액": (price or 0) * shares,
        })

    total = sum(r["평가금액"] for r in rows)
    for r in rows:
        r["비중(%)"] = round(r["평가금액"] / total * 100, 2) if total > 0 else 0.0
    return rows, total, missing


def load_stock_view(
    stock_name: str, use_dart: bool, reprt: str,
    manual_price: float | None, auto_price: bool,
):
    """반환: (source, payload)  —  source: "DART" | "MOCK" """
    if not (use_dart and _DART_AVAILABLE):
        return "MOCK", {"회사명": stock_name, "지표": MOCK_STOCK_DATA.get(stock_name, {})}

    try:
        info = dict(fetch_from_dart(stock_name, reprt))
    except Exception as e:  # noqa: BLE001
        st.warning(f"DART 조회 실패 → 예시 데이터로 대체합니다. ({e})")
        return "MOCK", {"회사명": stock_name, "지표": MOCK_STOCK_DATA.get(stock_name, {})}

    # --- 주가 결정: 자동 수집(pykrx/DB캐시) 우선, 없으면 수동 입력값 ---
    price = manual_price or None
    ticker = info.get("종목코드")
    if auto_price and _PRICE_AVAILABLE and ticker:
        try:
            pinfo = fetch_price(ticker)
            info["주가정보"] = pinfo
            if pinfo.get("available"):
                price = pinfo["price"]
        except Exception as e:  # noqa: BLE001
            info["주가정보"] = {"available": False, "note": f"주가 조회 오류: {e}"}

    # --- 주가가 생겼으면 PER/PBR 다시 계산 ---
    if price:
        info["적용주가"] = price
        info["지표"] = dart_client.compute_indicators(
            info["재무"], shares=info.get("상장주식수"), price=price
        )
    return "DART", info


# ----------------------------
# 4. 지표 해설 (규칙 기반; 나중에 AI API로 교체 지점)
# ----------------------------
def _won(v) -> str:
    """
    큰 금액을 한국식 단위로 읽기 쉽게.
      333605938000000 -> '333조 6,059억 원'
      -161870567171   -> '-1,619억 원'
      7871692199887   -> '7조 8,717억 원'
    """
    if v is None:
        return "-"
    v = float(v)
    sign = "-" if v < 0 else ""
    a = abs(v)

    if a >= 1e12:  # 1조 이상: '조 + 억' 두 단위까지만
        jo = int(a // 1e12)
        eok = round((a - jo * 1e12) / 1e8)
        if eok >= 10000:  # 반올림으로 1조가 채워진 경우 보정
            jo, eok = jo + 1, 0
        return f"{sign}{jo:,}조 원" if eok == 0 else f"{sign}{jo:,}조 {eok:,}억 원"
    if a >= 1e8:
        return f"{sign}{a / 1e8:,.0f}억 원"
    if a >= 1e4:
        return f"{sign}{a / 1e4:,.0f}만 원"
    return f"{sign}{a:,.0f} 원"


def _count(v) -> str:
    """주식 수처럼 '원'이 아닌 큰 개수."""
    if v is None:
        return "-"
    a = float(v)
    if a >= 1e8:
        return f"{a / 1e8:,.2f}억 주"
    if a >= 1e4:
        return f"{a / 1e4:,.0f}만 주"
    return f"{a:,.0f} 주"


# 지표별 표시 규칙
_MONEY_KEYS = {"매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계", "지배주주지분"}


def format_indicator(key: str, value) -> str:
    """표에 넣을 때 사람이 읽기 좋은 문자열로."""
    if value is None:
        return "-"
    if key in _MONEY_KEYS:
        return _won(value)
    if key.endswith("(%)"):
        return f"{value:,.2f} %"
    if key in ("EPS(원)", "BPS(원)"):
        return f"{value:,.0f} 원"
    if key in ("PER", "PBR"):
        return f"{value:,.2f} 배"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return f"{value:,}"


def explain_from_dart(info: dict) -> str:
    ind = info["지표"]
    lines = [f"### {info['회사명']} · {info['연도']}년 {info['보고서']} 기준 (출처: DART)\n"]

    lines.append(
        f"- 매출액 **{_won(ind.get('매출액'))}**, "
        f"영업이익 **{_won(ind.get('영업이익'))}** "
        f"(영업이익률 {ind.get('영업이익률(%)', '-')}%)"
    )

    roe = ind.get("ROE(%)")
    if roe is not None:
        tone = "자기자본을 꽤 효율적으로 굴리고 있는" if roe >= 10 else "자기자본 대비 이익 효율이 낮은"
        lines.append(f"- ROE **{roe}%** → {tone} 편입니다. (보통 10% 이상이면 양호하다고 봅니다)")

    debt = ind.get("부채비율(%)")
    if debt is not None:
        tone = "재무구조가 안정적인" if debt < 100 else "부채 부담을 함께 봐야 하는"
        lines.append(f"- 부채비율 **{debt}%** → {tone} 편입니다. (100% 미만이면 보수적으로 안정적)")

    per, pbr = ind.get("PER"), ind.get("PBR")
    pinfo = info.get("주가정보")
    if per is not None:
        if pinfo and pinfo.get("available"):
            basis = f"{pinfo['price_date']} 종가 {pinfo['price']:,.0f}원 (KRX 자동 수집)"
        elif info.get("적용주가"):
            basis = f"입력한 주가 {info['적용주가']:,.0f}원"
        else:
            basis = "적용 주가"
        lines.append(
            f"- {basis} 기준 **PER {per}배**, **PBR {pbr}배** "
            f"(EPS {ind.get('EPS(원)')}원 / BPS {ind.get('BPS(원)')}원)"
        )
        lines.append(
            "  - PER가 낮다고 무조건 싸다는 뜻은 아닙니다. 이익이 앞으로 줄어들 것으로 "
            "시장이 보고 있으면 PER는 낮게 나옵니다."
        )
        lines.append(
            "  - 여기 EPS/BPS는 최근 '사업보고서' 기준입니다. 분기 실적이 반영된 "
            "증권사 화면의 PER와는 다를 수 있어요."
        )
    else:
        lines.append(
            "- PER/PBR은 주가가 있어야 계산됩니다. 사이드바의 '주가 자동 수집'을 켜거나 "
            "현재 주가를 직접 입력하세요."
        )

    lines.append("\n숫자는 판단의 재료일 뿐입니다. 최종 판단은 스스로 내려보세요.")
    return "\n".join(lines)


def explain_with_llm(info: dict) -> tuple[str, str]:
    """
    LLM 해설을 시도하고, 안 되면 규칙 기반 해설로 되돌린다.
    반환: (해설 텍스트, 상태 문자열)
      상태: "LLM" | "LLM(캐시)" | "규칙기반: <사유>"
    """
    if not (_LLM_IMPORTED and llm_client.is_available()):
        reason = llm_client.unavailable_reason() if _LLM_IMPORTED else "llm_client 로드 실패"
        return explain_from_dart(info), f"규칙기반: {reason}"

    try:
        text, from_cache = llm_client.explain(
            info,
            cache_get=db.get_cached_explanation,
            cache_set=lambda key, txt, i: db.save_explanation(key, txt, i, llm_client.MODEL),
        )
        return text, ("LLM(캐시)" if from_cache else "LLM")
    except llm_client.LLMUnavailable as e:
        return explain_from_dart(info), f"규칙기반: {e}"
    except Exception as e:  # noqa: BLE001 — 어떤 오류가 나도 앱은 계속 동작해야 한다
        return explain_from_dart(info), f"규칙기반: 예상치 못한 오류 ({e})"


def explain_from_mock(stock_name: str) -> str:
    data = MOCK_STOCK_DATA.get(stock_name)
    if not data:
        return f"'{stock_name}'에 대한 예시 데이터가 없습니다."
    per, avg_per = data["PER"], data["업종평균PER"]
    if per > avg_per:
        comparison = (
            f"업종 평균({avg_per}배)보다 높은 편입니다. 시장이 이 회사의 미래 성장을 "
            f"더 높게 평가하거나, 반대로 고평가 상태일 가능성을 의미할 수도 있습니다."
        )
    else:
        comparison = (
            f"업종 평균({avg_per}배)보다 낮은 편입니다. 상대적으로 저평가되어 있거나, "
            f"성장성에 대한 우려가 반영된 것일 수도 있습니다."
        )
    return (
        f"**{stock_name}의 현재 PER은 {per}배입니다. (예시 데이터)**\n\n"
        f"{comparison}\n\n"
        f"금리 민감도는 '{data['금리민감도']}'로 분류됩니다. "
        f"이 지표들을 종합해 투자 판단은 스스로 내려보세요."
    )


# ----------------------------
# 5. 화면 구성
# ----------------------------
st.set_page_config(page_title="경제교육 투자 앱 (MVP)", layout="centered")

st.title("📊 경제 문맹 청년을 위한 투자 학습 앱")
st.caption("찍어주는 앱이 아니라, 이해시켜주는 앱")

with st.sidebar:
    st.header("데이터 설정")
    use_dart = st.toggle("실제 DART 데이터 사용", value=_DART_AVAILABLE, disabled=not _DART_AVAILABLE)
    if not _DART_AVAILABLE:
        st.caption(f"DART 모듈 사용 불가: {_DART_IMPORT_ERROR}")

    reprt = st.selectbox("보고서 종류", ["사업보고서", "3분기", "반기", "1분기"])
    st.caption("조회 연도는 공시된 가장 최근 연도로 자동 선택됩니다.")

    auto_price = st.toggle(
        "주가 자동 수집 (pykrx)", value=_PRICE_AVAILABLE, disabled=not _PRICE_AVAILABLE,
        help="KRX 최근 거래일 종가를 받아 PER/PBR을 자동 계산합니다. (실시간 아님)",
    )
    if not _PRICE_AVAILABLE:
        st.caption("pykrx 미설치: `pip install pykrx`")

    st.divider()
    st.header("AI 해설")
    _llm_ok = _LLM_IMPORTED and llm_client.is_available()
    use_llm = st.toggle(
        "AI 지표 해설 (Claude)", value=_llm_ok, disabled=not _llm_ok,
        help="재무지표를 AI가 초보자 눈높이로 설명합니다. 같은 지표는 DB에 캐시되어 재과금되지 않습니다.",
    )
    if _llm_ok:
        st.caption(f"모델: `{llm_client.MODEL}` · effort `{llm_client.EFFORT}`")
    else:
        st.caption(
            (llm_client.unavailable_reason() if _LLM_IMPORTED else "llm_client 로드 실패")
            + "\n\n지금은 규칙 기반 해설로 동작합니다."
        )

    st.divider()
    st.header("데이터베이스")
    stats = db.cache_stats()
    st.caption(
        f"종목 마스터 **{stats['stocks']:,}**건 (상장 {stats['listed']:,})  \n"
        f"재무 캐시 {stats['financial_cache']}건 · 주가 캐시 {stats['price_cache']}건  \n"
        f"AI 해설 캐시 {stats['explanation_cache']}건 · 크기 {stats['db_size_kb']:,.0f} KB"
    )
    if stats["stocks"] == 0:
        st.warning("종목 마스터가 비어 있습니다. 아래 버튼으로 동기화하세요.")
    if st.button("종목 마스터 동기화", disabled=not _DART_AVAILABLE):
        with st.spinner("DART 회사 목록 다운로드 중... (처음엔 30초 정도)"):
            n = db.sync_stock_master(dart_client.get_corp_code_map(force_refresh=True))
        st.success(f"{n:,}건 동기화 완료")
        st.rerun()

st.divider()

# --- 1. 종목 등록 ---
st.subheader("1. 관심 종목 등록")

portfolio = db.list_portfolio()
registered_names = [row["corp_name"] for row in portfolio]

if use_dart and _DART_AVAILABLE and stats["stocks"] > 0:
    keyword = st.text_input("종목명 검색 (예: 삼성, 카카오, 현대)", value="")
    matches = db.search_stocks(keyword, limit=20) if keyword else []

    if keyword and not matches:
        st.caption("검색 결과가 없습니다. 회사명 일부만 입력해보세요.")

    options = [f"{m['corp_name']} ({m['stock_code']})" for m in matches]
    picked = st.selectbox("검색 결과에서 선택", options=options) if options else None
    candidate = matches[options.index(picked)]["corp_name"] if picked else ""
else:
    # DART를 안 쓰거나 마스터가 비었을 때: 예시 5종목만
    available = [s for s in MOCK_STOCK_DATA if s not in registered_names]
    candidate = st.selectbox("등록할 종목 선택", options=available or ["등록 가능한 종목 없음"])

col_w, col_b = st.columns([2, 1])
with col_w:
    new_shares = st.number_input(
        "보유 수량 (주)", min_value=0, value=0, step=1,
        help="보유 비중은 (보유수량 × 현재주가) ÷ 전체 평가금액 으로 자동 계산됩니다. "
             "아직 안 샀으면 0으로 두고 관심종목으로만 등록해도 됩니다.",
    )
with col_b:
    st.write("")
    st.write("")
    add_clicked = st.button("추가하기", use_container_width=True)

if add_clicked:
    if not candidate or candidate == "등록 가능한 종목 없음":
        st.warning("종목을 검색해서 선택하세요.")
    else:
        try:
            added = db.add_stock(candidate, shares=new_shares, slot_limit=FREE_SLOT_LIMIT)
            if added:
                st.success(f"'{candidate}' {new_shares:,}주 등록 완료")
                st.rerun()
            else:
                st.info("이미 등록된 종목입니다.")
        except db.SlotLimitExceeded as e:
            st.warning(f"{e} (튜토리얼 완료 시 슬롯 확장 예정)")

st.write(f"현재 등록된 종목: **{len(portfolio)} / {FREE_SLOT_LIMIT}**")

st.divider()

# --- 2. 포트폴리오 트리맵 ---
st.subheader("2. 내 포트폴리오 트리맵")

if portfolio:
    with st.spinner("현재가를 불러와 비중을 계산하는 중..."):
        holdings, total_value, missing_price = value_portfolio(portfolio, use_price=auto_price)

    if missing_price:
        st.warning(
            f"주가를 못 가져온 종목이 있어 비중에서 빠졌습니다: {', '.join(missing_price)}"
        )

    if total_value > 0:
        st.metric("전체 평가금액", _won(total_value))

        vdf = pd.DataFrame([h for h in holdings if h["평가금액"] > 0])
        fig = px.treemap(
            vdf, path=["종목"], values="평가금액", color="종목",
            custom_data=["보유수량", "현재가", "비중(%)"],
        )
        fig.update_traces(
            texttemplate="<b>%{label}</b><br>%{customdata[2]}%",
            hovertemplate=(
                "<b>%{label}</b><br>보유 %{customdata[0]:,.0f}주"
                "<br>현재가 %{customdata[1]:,.0f}원"
                "<br>평가금액 %{value:,.0f}원"
                "<br>비중 %{customdata[2]}%<extra></extra>"
            ),
        )
        fig.update_layout(margin=dict(t=10, l=10, r=10, b=10))
        st.plotly_chart(fig, use_container_width=True)

        # 비중 계산 내역을 표로도 보여준다 (계산 과정을 눈으로 확인)
        table = pd.DataFrame([{
            "종목": h["종목"],
            "보유수량": f"{h['보유수량']:,.0f} 주",
            "현재가": f"{h['현재가']:,.0f} 원" if h["현재가"] else "-",
            "평가금액": _won(h["평가금액"]),
            "비중": f"{h['비중(%)']:.2f} %",
        } for h in holdings])
        st.dataframe(table, hide_index=True, use_container_width=True)
        st.caption("비중 = (보유수량 × 현재가) ÷ 전체 평가금액 × 100")
    else:
        st.info(
            "보유 수량이 아직 0이라 비중을 계산할 수 없어요. "
            "아래 '등록 종목 관리'에서 보유 수량을 입력해보세요. (관심종목으로만 두어도 괜찮습니다)"
        )

    # 등록 종목 관리 (보유 수량 수정 / 삭제)
    with st.expander("등록 종목 관리", expanded=(total_value == 0)):
        for row in portfolio:
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.write(f"**{row['corp_name']}** `{row['stock_code'] or '-'}`")
            s = c2.number_input(
                "보유수량", min_value=0, value=int(row["shares"] or 0), step=1,
                key=f"s_{row['id']}", label_visibility="collapsed",
            )
            if s != int(row["shares"] or 0):
                db.update_shares(row["corp_name"], s)
                st.rerun()
            if c3.button("삭제", key=f"d_{row['id']}"):
                db.remove_stock(row["corp_name"])
                st.rerun()

    # --- 3. 지표 해설 ---
    st.subheader("3. 지표 해설 보기")
    stock_to_explain = st.selectbox("해설을 볼 종목", options=registered_names)
    manual_price = st.number_input(
        "현재 주가(원) — 자동 수집이 꺼져 있거나 실패할 때만 사용, 모르면 0",
        min_value=0.0, value=0.0, step=100.0,
    )

    if st.button("해설 보기"):
        with st.spinner("데이터를 불러오는 중..."):
            source, payload = load_stock_view(
                stock_to_explain, use_dart, reprt, manual_price, auto_price,
            )

        if source == "DART":
            head = f"📅 **{payload['연도']}년 {payload['보고서']}** 기준 (자동 선택된 최근 연도)"
            if payload.get("_from_cache"):
                head += " · 💾 DB 캐시"
            st.caption(head)

            pinfo = payload.get("주가정보")
            if pinfo and pinfo.get("available"):
                st.metric(
                    f"{payload['회사명']} 주가 ({pinfo['price_date']} 종가)",
                    f"{pinfo['price']:,.0f} 원",
                    f"{pinfo['change_pct']}%",
                )
            elif pinfo:
                st.caption(f"⚠️ 주가 자동 수집 실패: {pinfo.get('note')}")
            elif payload.get("적용주가"):
                st.caption(f"입력한 주가 {payload['적용주가']:,.0f}원 기준")

            ind = payload["지표"]

            # 큰 금액은 카드로 크게 (조/억 단위로 변환해서 표시)
            m1, m2, m3 = st.columns(3)
            m1.metric("매출액", _won(ind.get("매출액")))
            m2.metric("영업이익", _won(ind.get("영업이익")))
            m3.metric("당기순이익", _won(ind.get("당기순이익")))

            # 비율·주당 지표는 표로
            ratio_keys = ["영업이익률(%)", "ROE(%)", "부채비율(%)", "EPS(원)", "BPS(원)", "PER", "PBR"]
            metric_df = pd.DataFrame(
                [(k, format_indicator(k, ind.get(k))) for k in ratio_keys],
                columns=["지표", "값"],
            )
            st.dataframe(metric_df, hide_index=True, use_container_width=True)

            with st.expander("원본 재무 숫자 보기"):
                fin = payload.get("재무", {})
                raw_df = pd.DataFrame(
                    [(k, _won(v)) for k, v in fin.items() if k != "fs_div"],
                    columns=["계정", "금액"],
                )
                st.dataframe(raw_df, hide_index=True, use_container_width=True)
                st.caption(
                    f"연결/별도 구분: {fin.get('fs_div', '-')} · "
                    f"상장주식수: {_count(payload.get('상장주식수'))}"
                )

            if use_llm:
                with st.spinner("AI가 해설을 작성하는 중..."):
                    text, status = explain_with_llm(payload)
            else:
                text, status = explain_from_dart(payload), "규칙기반 (사용자가 AI 해설을 끔)"

            st.info(text)
            if status.startswith("LLM"):
                st.caption(f"🤖 {status} · `{llm_client.MODEL}`")
            else:
                st.caption(f"📐 {status}")
            st.caption(
                "이 해설은 공시된 숫자를 설명하는 교육용 자료이며, 투자 권유나 "
                "주가 예측이 아닙니다. 투자 판단과 그 결과는 본인에게 있습니다."
            )
        else:
            st.info(explain_from_mock(stock_to_explain))

    if st.button("포트폴리오 초기화", type="secondary"):
        db.clear_portfolio()
        st.rerun()
else:
    st.info("아직 등록된 종목이 없어요. 위에서 종목을 추가해보세요.")
