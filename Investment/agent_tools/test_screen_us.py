"""screen-us 규칙 자체 검사: python Investment/agent_tools/test_screen_us.py"""
import numpy as np
import pandas as pd

from market_data import rank_us, us_factors


def series(daily_ret: float, n: int = 300, vol: float = 1e6) -> pd.DataFrame:
    c = 100 * (1 + daily_ret) ** np.arange(n)
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": vol}, index=idx)


# 꾸준히 오르는 종목: 추세 통과, 모멘텀 양수, 상승일만 있어 정보불연속성 = -1 (가장 '조용한' 모멘텀)
f = us_factors(series(0.002))
assert f["above_ma200"] and f["mom_12_1"] > 0 and f["info_discrete"] == -1, f
assert 1.5 < f["atr_pct"] < 3.0 and abs(f["near_52w_high"] - 1 / 1.01) < 1e-9, f
assert us_factors(series(0.002, n=200)) is None  # 이력 부족

base = dict(price=50, dollar_vol=1e8, atr_pct=2.0, above_ma200=True, max_ret=0.02,
            mom_12_1=0.3, near_52w_high=0.95, info_discrete=-0.2)
rows = [dict(base, ticker=f"T{i}", name="", sector="A", mom_12_1=0.1 + i / 100) for i in range(10)]
rows += [
    dict(base, ticker="CHEAP", sector="B", price=5),
    dict(base, ticker="ILLIQ", sector="B", dollar_vol=1e7),
    dict(base, ticker="WILD", sector="B", atr_pct=5.0),
    dict(base, ticker="DOWN", sector="B", above_ma200=False),
    dict(base, ticker="LOTTO", sector="B", max_ret=0.3),
    dict(base, ticker="GOOD", sector="C", mom_12_1=0.9),
]
picked, funnel = rank_us(rows, top=5)
tickers = [p["ticker"] for p in picked]
assert not {"CHEAP", "ILLIQ", "WILD", "DOWN", "LOTTO"} & set(tickers), tickers
assert tickers[0] == "GOOD", tickers                     # 모멘텀 최고
assert sum(p["sector"] == "A" for p in picked) == 1, tickers  # 섹터 상한 ceil(5*0.2)=1
assert funnel["MAX 상위10% 제외"] < funnel["추세(>MA200, MA50>MA200)"], funnel
print("ok")
