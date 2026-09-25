"""
안전자산에 '-5% 손절 / +15% 익절'이 맞는지 검증 (결과 해석: Investment/PORTFOLIO.md)
실행: python SetUp/agent_tools/safe_asset_study.py   (약 20초)

- 최근 10년 일봉, 배당·이자를 포함한 총수익 기준 (Yahoo adjclose 비율로 OHLC 보정)
- 매일 진입했다고 보고 120거래일 안에 손절/익절 중 무엇이 먼저 오는지 (market_data.bracket_backtest)
- 비교: 규칙 없이 120일 보유했을 때의 평균과 하위 5% 수익률
"""
from datetime import datetime, timedelta

import pandas as pd
import requests

from market_data import _UA, bracket_backtest

ASSETS = {"SPY": "S&P500(비교)", "TLT": "미 장기국채", "IEF": "미 중기국채", "BIL": "미 초단기국채",
          "GLD": "금", "SLV": "은", "KRW=X": "달러(원화 기준)"}
H, YEARS = 120, 10


def total_return_ohlc(sym: str) -> pd.DataFrame:
    start = datetime.now() - timedelta(days=int(YEARS * 380))
    res = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", headers=_UA, timeout=15,
                       params={"period1": int(start.timestamp()), "period2": int(datetime.now().timestamp()),
                               "interval": "1d"}).json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]  # 환율은 adjclose 없음
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")},
                      index=pd.to_datetime(res["timestamp"], unit="s").normalize()).assign(adj=adj).dropna()
    f = df["adj"] / df["close"]
    return df[["open", "high", "low", "close"]].mul(f, axis=0).tail(YEARS * 252)


def main():
    rows, grid = [], []
    for sym, name in ASSETS.items():
        df = total_return_ohlc(sym)
        c = df["close"]
        hold = (c.shift(-H) / c - 1).dropna() * 100
        b = bracket_backtest(df, 5, 15, H, lookback=len(df) - H - 2)
        rows.append({"자산": name, "연변동성(%)": round(c.pct_change().std() * 252 ** 0.5 * 100, 1),
                     "익절(%)": b["익절확률(%)"], "손절(%)": b["손절확률(%)"], "미결(%)": b["미결확률(%)"],
                     "규칙 평균(%)": b["평균수익률(%)"], "보유 평균(%)": round(hold.mean(), 2),
                     "보유 하위5%(%)": round(hold.quantile(0.05), 2)})
        for stop in (2, 3, 5, 8, 10):
            g = bracket_backtest(df, stop, 3 * stop, H, lookback=len(df) - H - 2)
            grid.append({"자산": name, "규칙": f"-{stop}/+{3 * stop}",
                         "익절/손절/평균": f"{g['익절확률(%)']:.0f}/{g['손절확률(%)']:.0f}/{g['평균수익률(%)']:+.2f}"})
    print(f"== -5%/+15%, {H}거래일, {YEARS}년 총수익 기준 (손익분기 익절확률 25%) ==")
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n== 손절폭 그리드 (익절 = 손절 x3) ==")
    print(pd.DataFrame(grid).pivot(index="자산", columns="규칙", values="익절/손절/평균").to_string())


if __name__ == "__main__":
    main()
