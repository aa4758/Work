"""매수 시점·금액 기준 손익, 자산별 손절/익절 신호, 시드 배분 계산 (순수 함수). 근거: PORTFOLIO.md"""
from __future__ import annotations

import math

import pandas as pd

STOP_PCT, TAKE_PCT = 5, 15
SLOTS, SLOT_RISK_PCT = 10, 5  # 슬롯 10개, 슬롯 1개의 기준 위험 = 슬롯 금액의 5% (주식 손절폭)
MAX_WEIGHT = 0.15             # 한 종목 최대 비중
HOLD_DAYS = 120               # 손절·익절 없는 자산의 위험 측정 기간(거래일)

# 비주식 자산의 청산 규칙 (손절%, 익절%). None = 손절·익절 없이 보유. 여기 없는 티커는 주식 규칙(-5/+15).
# 10년 총수익 백테스트 결과 (PORTFOLIO.md): 금만 -5/+15가 맞고, 은은 변동성이 2배라 폭을 2배로,
# 국채·달러는 +15%에 거의 닿지 않아 손절만 걸리는 규칙이 되므로 보유형으로 둔다.
ASSET_RULES: dict[str, tuple[float, float] | None] = {
    "GLD": (5, 15), "SLV": (10, 30), "TLT": None, "IEF": None, "BIL": None,
}
ASSET_NAMES = {"GLD": "금", "SLV": "은", "TLT": "미 장기국채", "IEF": "미 중기국채", "BIL": "미 초단기국채(달러 현금)"}


def rule(ticker: str) -> tuple[float, float] | None:
    return ASSET_RULES.get(ticker, (STOP_PCT, TAKE_PCT))


def evaluate(df: pd.DataFrame, buy_date, amount: float, stop_pct: float | None = STOP_PCT,
             take_pct: float | None = TAKE_PCT, sell_date=None) -> dict | None:
    """
    df: 일봉 OHLC (DatetimeIndex). buy_date 종가(휴장일이면 다음 거래일 종가)에 amount 달러를 샀다고 본다.
    매수 다음 날부터 저가/고가로 손절·익절 도달을 판정한다 (market_data.bracket_backtest 와 같은 규칙):
      - 같은 날 둘 다 닿으면 보수적으로 손절, 갭이면 시가 체결
      - 도달했으면 그 가격에 판 것으로, 아니면 현재가로 손익을 계산
    stop_pct=None 이면 손절·익절 없이 현재가로만 평가한다 (국채 등 보유형 자산).
    sell_date 가 있으면 그날까지만 보고, 손절·익절 전이면 그날 종가에 '매도'한 것으로 확정한다.
    '신호'가 '보유'인 포지션만 아직 열려 있는 것이다.
    """
    after = df[df.index >= pd.Timestamp(buy_date)]
    if sell_date is not None:
        after = after[after.index <= pd.Timestamp(sell_date)]
    if after.empty:
        return None
    buy = float(after["close"].iloc[0])
    stop = take = None
    signal, price, when = "보유", float(after["close"].iloc[-1]), after.index[-1]
    if stop_pct is not None:
        stop, take = buy * (100 - stop_pct) / 100, buy * (100 + take_pct) / 100
        for day, row in after.iloc[1:].iterrows():
            if row["low"] <= stop:
                signal, price, when = "손절", min(row["open"], stop), day
                break
            if row["high"] >= take:
                signal, price, when = "익절", max(row["open"], take), day
                break
    if signal == "보유" and sell_date is not None:
        signal = "매도"

    shares = amount / buy
    return {
        "매수일": after.index[0].date(), "매수가": buy, "수량": shares,
        "손절가": stop, "익절가": take,
        "신호": signal, "기준일": when.date(), "기준가": price,
        "손익($)": shares * (price - buy), "수익률(%)": (price / buy - 1) * 100,
    }


def risk_pct(ticker: str, df: pd.DataFrame | None) -> float:
    """포지션 1개의 '최악 손실' 추정(%). 손절이 있으면 손절폭, 없으면 보유기간 수익률의 하위 5% (1.65σ√t)."""
    r = rule(ticker)
    if r is not None:
        return r[0]
    if df is None or len(df) < 60:
        return 3 * SLOT_RISK_PCT  # 데이터가 없으면 보수적으로 크게 잡아 비중을 줄인다
    vol = df["close"].pct_change().tail(252).std()
    return max(1.65 * vol * math.sqrt(HOLD_DAYS) * 100, 0.1)


def allocate(scores: list[float], risks: list[float], budget: float = 1.0) -> list[float]:
    """
    슬롯 기반 위험 균등 + 점수 기울기 배분. 반환: 종목별 비중(시드 대비, 합 ≤ 1). 나머지는 현금.
      1) 기본 비중 = 1슬롯(10%) — 주식은 -5% 손절이라 슬롯당 최대손실이 시드의 0.5%로 같다
      2) 점수 기울기 = 점수 / 평균점수, 0.5~1.5배로 제한 (추정 오차에 강하게)
      3) 위험 조정 = 5% / 자산 위험 (1 이하) — 손절 없는 자산은 최대손실이 주식 슬롯과 같아지게 줄인다
      4) 한 종목 15% 상한, 합이 budget(쓸 수 있는 현금 비율, 보유 종목 제외)을 넘으면 비례 축소
    """
    if not scores:
        return []
    mean = sum(scores) / len(scores)
    tilt = [min(1.5, max(0.5, s / mean)) if mean > 0 else 1.0 for s in scores]
    w = [min(MAX_WEIGHT, t / SLOTS * min(1.0, SLOT_RISK_PCT / r)) for t, r in zip(tilt, risks)]
    total = sum(w)
    return [x * budget / total for x in w] if total > budget else w


def cash_split(action: str | None) -> tuple[float, float]:
    """현금의 (원화, 달러) 비율. 환율분석가 판단을 따른다."""
    return {"환전 추천": (0.0, 1.0), "분할 환전": (0.5, 0.5)}.get(action or "", (1.0, 0.0))


if __name__ == "__main__":
    idx = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"])
    ohlc = lambda rows: pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])

    # 주말(1/3)에 샀다고 하면 다음 거래일 1/5 종가 100에 매수. 1/7 고가 116 → 익절 115
    r = evaluate(ohlc([[90, 91, 89, 90], [99, 101, 98, 100], [100, 104, 99, 103], [103, 116, 102, 110]]), "2026-01-03", 1000)
    assert (r["매수가"], r["신호"], r["기준가"]) == (100, "익절", 115), r
    assert round(r["손익($)"], 6) == 150 and round(r["수익률(%)"], 6) == 15

    # 갭하락: 시가 90으로 손절선(95) 아래에서 시작 → 90에 체결
    r = evaluate(ohlc([[100, 100, 100, 100], [90, 92, 88, 91], [91, 92, 90, 91], [91, 92, 90, 91]]), "2026-01-02", 500)
    assert (r["신호"], r["기준가"], r["손익($)"]) == ("손절", 90, -50), r

    # 같은 날 손절·익절 둘 다 닿으면 손절
    r = evaluate(ohlc([[100, 100, 100, 100], [100, 120, 94, 100], [100, 100, 100, 100], [100, 100, 100, 100]]), "2026-01-02", 100)
    assert r["신호"] == "손절", r

    # 아무것도 안 닿으면 현재가로 평가
    r = evaluate(ohlc([[100, 100, 100, 100], [100, 102, 99, 101], [101, 103, 100, 102], [102, 104, 101, 103]]), "2026-01-02", 100)
    assert (r["신호"], r["기준가"]) == ("보유", 103) and round(r["수익률(%)"], 6) == 3

    # 보유형 자산(stop=None): 90까지 빠져도 신호 없이 현재가로 평가
    r = evaluate(ohlc([[100, 100, 100, 100], [90, 92, 88, 91], [91, 92, 90, 91], [91, 92, 90, 92]]), "2026-01-02", 100, None, None)
    assert (r["신호"], r["기준가"], r["손절가"]) == ("보유", 92, None), r
    assert rule("TLT") is None and rule("SLV") == (10, 30) and rule("NVDA") == (5, 15)

    assert evaluate(ohlc([[1, 1, 1, 1]] * 4), "2027-01-01", 100) is None

    # 직접 매도: 1/6 종가 102에 팔았다 → 이후 1/7 급락은 반영하지 않는다
    rows4 = [[100, 100, 100, 100], [100, 102, 99, 101], [101, 103, 100, 102], [102, 103, 80, 81]]
    r = evaluate(ohlc(rows4), "2026-01-02", 100, sell_date="2026-01-06")
    assert (r["신호"], r["기준가"], str(r["기준일"])) == ("매도", 102, "2026-01-06"), r
    # 매도일 전에 손절이 먼저 났으면 손절로 확정
    r = evaluate(ohlc(rows4), "2026-01-02", 100, sell_date="2026-01-07")
    assert r["신호"] == "손절", r

    # 배분: 점수가 같은 주식 10개 = 10%씩, 합 100%
    assert [round(x, 6) for x in allocate([1.0] * 10, [5] * 10)] == [0.1] * 10
    # 점수가 높을수록 비중이 크고, 15% 상한
    w = allocate([2.0, 1.0, 0.5], [5, 5, 5])
    assert w[0] > w[1] > w[2] and max(w) <= MAX_WEIGHT and abs(sum(w) - 0.3) < 0.06, w
    # 위험이 15%인 보유형 자산은 같은 점수의 주식보다 1/3 비중 → 최대손실이 같아진다
    w = allocate([1.0, 1.0], [5, 15])
    assert abs(w[1] - w[0] / 3) < 1e-9 and abs(w[0] * 5 - w[1] * 15) < 1e-9, w
    # 슬롯 10개를 넘겨도 합은 100% 이하
    assert sum(allocate([2.0] * 5 + [0.5] * 7, [5] * 12)) <= 1 + 1e-9
    # 보유 종목이 시드의 70%를 쓰고 있으면 새 종목은 남은 30% 안에서
    assert abs(sum(allocate([2.0] * 3, [5] * 3, budget=0.3)) - 0.3) < 1e-9
    assert allocate([], []) == [] and cash_split("환전 보류") == (1.0, 0.0) and cash_split("분할 환전") == (0.5, 0.5)
    print("ok")
