"""
투자 에이전트 팀용 데이터 도구 (CLI)
------------------------------------
`.claude/agents/` 의 분석 에이전트들이 Bash로 호출해 '실제 숫자'를 얻는 창구.
LLM이 숫자를 지어내지 않도록, 계산은 전부 이 파일에서 하고 결과는 JSON으로 출력한다.

사용법 (저장소 루트에서):
  python Investment/agent_tools/market_data.py screen   --market ALL --top 40
  python Investment/agent_tools/market_data.py chart    005930 000660 035420   # 여러 종목 한 번에
  python Investment/agent_tools/market_data.py bracket  005930 --stop 5 --take 15 --horizon 60
  python Investment/agent_tools/market_data.py investor 005930 000660 --days 20
  python Investment/agent_tools/market_data.py fundamental 삼성전자
  python Investment/agent_tools/market_data.py fx
  python Investment/agent_tools/market_data.py screen-us --top 30          # 미국 S&P500 규칙 기반 후보군
  python Investment/agent_tools/market_data.py macro                       # 미국 지수·금리·유가·달러·VIX

데이터 출처와 fallback:
  - 시세(OHLCV): pykrx → 실패 시 네이버 차트 API (로그인 불필요)
  - 시가총액/거래대금 스크리닝, 투자자별 매매: pykrx (최신 pykrx는 KRX_ID/KRX_PW 환경변수 필요)
  - 재무: dart_client.py (DART_API_KEY 필요)
  - 환율: frankfurter.app (ECB 기준, 키 불필요)
  - 미국: 시세 Yahoo chart API, S&P500 구성종목·GICS 업종 Wikipedia
어느 소스든 실패하면 예외 대신 {"error": ...} 를 출력한다 — 에이전트가 "데이터 없음"을
명시하고 넘어갈 수 있게.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# 보류된 앱(suspended/SetUp/)의 기존 모듈(dart_client, config)을 import 하기 위해
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "suspended" / "SetUp"))

try:
    # pykrx는 import 시점에 "KRX 로그인 실패" 같은 메시지를 print 하므로 JSON 출력을 지키려고 삼킨다
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from pykrx import stock as _krx
except Exception:  # noqa: BLE001
    _krx = None

_UA = {"User-Agent": "Mozilla/5.0"}


def _ymd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _quiet(fn, *args, **kwargs):
    """pykrx가 내부 오류를 print 하므로 JSON 출력이 깨지지 않게 삼킨다."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _r(v, n: int = 2):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), n)


# ----------------------------------------------------------------------
# 시세 로딩
# ----------------------------------------------------------------------
def _ohlcv_pykrx(code: str, days: int) -> Optional[pd.DataFrame]:
    if _krx is None:
        return None
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.5) + 10)  # 휴장일 여유
    try:
        df = _quiet(_krx.get_market_ohlcv, _ymd(start), _ymd(end), code)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low", "종가": "close", "거래량": "volume"})
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def _ohlcv_naver(code: str, days: int) -> Optional[pd.DataFrame]:
    url = "https://fchart.stock.naver.com/sise.nhn"
    params = {"symbol": code, "timeframe": "day", "count": days + 10, "requestType": 0}
    try:
        resp = requests.get(url, params=params, headers=_UA, timeout=10)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    rows = re.findall(r'data="([^"]+)"', resp.text)
    records = []
    for row in rows:
        parts = row.split("|")
        if len(parts) < 6:
            continue
        records.append({
            "date": pd.to_datetime(parts[0], format="%Y%m%d"),
            "open": float(parts[1]), "high": float(parts[2]), "low": float(parts[3]),
            "close": float(parts[4]), "volume": float(parts[5]),
        })
    if not records:
        return None
    return pd.DataFrame(records).set_index("date").sort_index()


def _ohlcv_yahoo(code: str, days: int) -> Optional[pd.DataFrame]:
    """미국 티커(AAPL, BRK-B)나 환율(KRW=X). 거래일→달력일 여유로 1.5배 요청."""
    start = datetime.now() - timedelta(days=int(days * 1.5) + 10)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}"
    params = {"period1": int(start.timestamp()), "period2": int(datetime.now().timestamp()), "interval": "1d"}
    try:
        resp = requests.get(url, params=params, headers=_UA, timeout=10)
        resp.raise_for_status()
        res = resp.json()["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError):
        return None
    df = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")},
                      index=pd.to_datetime(res["timestamp"], unit="s").normalize())
    df = df.dropna(subset=["close"]).astype(float)
    return df[~df.index.duplicated(keep="last")] if len(df) else None


def load_ohlcv(code: str, days: int = 400) -> tuple[Optional[pd.DataFrame], str]:
    """(DataFrame, 출처). 거래정지 등으로 거래량 0인 날은 제외. 6자리 숫자가 아니면 미국 티커로 본다."""
    if not re.fullmatch(r"\d{6}", code):
        df = _ohlcv_yahoo(code, days)
        # 환율(KRW=X)은 거래량이 0이라 거래량 필터를 건너뛴다
        return (df.tail(days), "yahoo") if df is not None and len(df) >= 30 else (None, "none")
    for name, loader in (("pykrx", _ohlcv_pykrx), ("naver", _ohlcv_naver)):
        df = loader(code, days)
        if df is not None and len(df) >= 30:
            df = df[df["volume"] > 0].tail(days)
            return df, name
    return None, "none"


# ----------------------------------------------------------------------
# 지표 계산 (순수 함수 — 테스트하기 쉽게 DataFrame만 받는다)
# ----------------------------------------------------------------------
def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(100)


def technicals(df: pd.DataFrame) -> dict:
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    last = float(c.iloc[-1])

    ma = {n: c.rolling(n).mean().iloc[-1] for n in (5, 20, 60, 120)}
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean().iloc[-1]

    daily_ret = c.pct_change()
    win = df.tail(250)
    hi52, lo52 = float(win["high"].max()), float(win["low"].min())

    def ret(n):
        return _r((last / c.iloc[-n - 1] - 1) * 100) if len(c) > n else None

    ma_order = [ma[n] for n in (5, 20, 60, 120)]
    if all(pd.notna(x) for x in ma_order):
        if ma_order == sorted(ma_order, reverse=True):
            alignment = "정배열(5>20>60>120)"
        elif ma_order == sorted(ma_order):
            alignment = "역배열(5<20<60<120)"
        else:
            alignment = "혼조"
    else:
        alignment = "데이터 부족"

    return {
        "기준일": df.index[-1].strftime("%Y-%m-%d"),
        "종가": _r(last),
        "수익률(%)": {"1주": ret(5), "1개월": ret(20), "3개월": ret(60), "6개월": ret(120)},
        "이동평균": {f"MA{n}": _r(x) for n, x in ma.items()},
        "종가/MA20(%)": _r((last / ma[20] - 1) * 100) if pd.notna(ma[20]) else None,
        "종가/MA60(%)": _r((last / ma[60] - 1) * 100) if pd.notna(ma[60]) else None,
        "이평배열": alignment,
        "RSI14": _r(rsi(c).iloc[-1], 1),
        "MACD": {
            "macd": _r(macd.iloc[-1]), "signal": _r(signal.iloc[-1]),
            "hist": _r(macd.iloc[-1] - signal.iloc[-1]),
            "골든크로스_최근5일": bool(((macd > signal) & (macd.shift(1) <= signal.shift(1))).tail(5).any()),
            "데드크로스_최근5일": bool(((macd < signal) & (macd.shift(1) >= signal.shift(1))).tail(5).any()),
        },
        "ATR14(%)": _r(atr14 / last * 100),
        "일간변동성_20일(%)": _r(daily_ret.tail(20).std() * 100),
        "52주": {
            "고가": _r(hi52), "저가": _r(lo52),
            "고가대비(%)": _r((last / hi52 - 1) * 100),
            "저가대비(%)": _r((last / lo52 - 1) * 100),
        },
        "거래량비율(5일/60일)": _r(v.tail(5).mean() / v.tail(60).mean()) if len(v) >= 60 else None,
        "20일_평균거래대금(억원)": _r((c * v).tail(20).mean() / 1e8, 1),
    }


def bracket_backtest(df: pd.DataFrame, stop_pct: float = 5, take_pct: float = 15,
                     horizon: int = 60, lookback: int = 250) -> dict:
    """
    '-stop% 손절 / +take% 익절' 전략을 과거 매일 매수했다고 가정하고 결과를 센다.
      - 매수가: 해당일 종가
      - 이후 각 거래일의 저가/고가로 손절·익절 도달 여부 판정
      - 같은 날 둘 다 닿으면 보수적으로 '손절'로 처리 (장중 순서를 알 수 없음)
      - 갭하락으로 손절가 아래에서 시작하면 시가로 체결 (슬리피지 반영)
      - horizon 거래일 안에 둘 다 안 닿으면 '미결'로 보고 그날 종가로 평가
    손익분기 승률 = stop / (stop + take)  (예: 5/(5+15) = 25%)
    """
    stop_pct, take_pct = abs(stop_pct), abs(take_pct)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    n = len(df)
    first = max(0, n - horizon - lookback)
    last_entry = n - horizon - 1  # horizon을 끝까지 볼 수 있는 마지막 진입일

    wins = losses = opens = 0
    pnl, days_to_exit = [], []
    for i in range(first, last_entry + 1):
        entry = c[i]
        stop, take = entry * (1 - stop_pct / 100), entry * (1 + take_pct / 100)
        result = None
        for j in range(i + 1, i + 1 + horizon):
            if l[j] <= stop:
                fill = min(o[j], stop)  # 갭하락이면 시가 체결
                result = ("loss", (fill / entry - 1) * 100, j - i)
                break
            if h[j] >= take:
                fill = max(o[j], take)
                result = ("win", (fill / entry - 1) * 100, j - i)
                break
        if result is None:
            result = ("open", (c[i + horizon] / entry - 1) * 100, horizon)
        kind, r, d = result
        wins += kind == "win"
        losses += kind == "loss"
        opens += kind == "open"
        pnl.append(r)
        days_to_exit.append(d)

    trials = len(pnl)
    if trials == 0:
        return {"error": f"데이터 부족: 최소 {horizon + 2}거래일 필요 (현재 {n}일)"}

    pnl_s = pd.Series(pnl)
    return {
        "조건": f"-{stop_pct}% 손절 / +{take_pct}% 익절 / 최대 {horizon}거래일 보유",
        "표본수(진입일)": trials,
        "익절확률(%)": _r(wins / trials * 100, 1),
        "손절확률(%)": _r(losses / trials * 100, 1),
        "미결확률(%)": _r(opens / trials * 100, 1),
        "손익분기_익절확률(%)": _r(stop_pct / (stop_pct + take_pct) * 100, 1),
        "평균수익률(%)": _r(pnl_s.mean()),
        "중앙값수익률(%)": _r(pnl_s.median()),
        "평균청산일수": _r(sum(days_to_exit) / trials, 1),
        "해석주의": "과거 1년 가격 경로 기반 통계일 뿐, 미래 확률이 아님. 표본 간 기간이 겹쳐 독립이 아님.",
    }


# ----------------------------------------------------------------------
# 서브커맨드
# ----------------------------------------------------------------------
def _chart_one(code: str, args) -> dict:
    df, src = load_ohlcv(code, days=400)
    if df is None:
        return {"code": code, "error": "시세 조회 실패 (pykrx/naver/yahoo 모두 실패)"}
    out = {"code": code, "출처": src, **technicals(df)}
    out["전략백테스트"] = bracket_backtest(df, args.stop, args.take, args.horizon)
    return out


def cmd_chart(args):
    """종목코드 여러 개를 한 번에 받을 수 있다 (후보 30개를 한 번의 호출로)."""
    results = [_chart_one(code, args) for code in args.codes]
    return results[0] if len(results) == 1 else results


def cmd_bracket(args) -> dict:
    df, src = load_ohlcv(args.code, days=args.lookback + args.horizon + 5)
    if df is None:
        return {"code": args.code, "error": "시세 조회 실패"}
    return {"code": args.code, "출처": src,
            **bracket_backtest(df, args.stop, args.take, args.horizon, args.lookback)}


def _latest_business_day_df(fn, *fargs, **fkwargs):
    """오늘부터 거꾸로 10일간 시도해서 데이터가 있는 가장 최근 거래일 결과를 돌려준다."""
    d = datetime.now()
    for _ in range(10):
        try:
            df = _quiet(fn, _ymd(d), *fargs, **fkwargs)
            if df is not None and not df.empty and df.select_dtypes("number").sum().sum() > 0:
                return df, d.strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
        d -= timedelta(days=1)
    return None, None


def cmd_screen(args) -> dict:
    """시총·유동성 기준 1차 후보군. 손절 -5%가 슬리피지로 무너지지 않도록 거래대금 하한을 둔다."""
    if _krx is None:
        return {"error": "pykrx 미설치 — `pip install pykrx`"}
    markets = ["KOSPI", "KOSDAQ"] if args.market == "ALL" else [args.market]
    frames, date = [], None
    for m in markets:
        df, date = _latest_business_day_df(_krx.get_market_cap, market=m)
        if df is None:
            continue
        df = df.copy()
        df["market"] = m
        frames.append(df)
    if not frames:
        return {"error": "KRX 시가총액 조회 실패. 최신 pykrx는 KRX_ID/KRX_PW 환경변수가 필요합니다. "
                         "대안: WebSearch로 코스피/코스닥 시총·거래대금 상위 종목을 수집하세요."}
    df = pd.concat(frames)
    df = df[df["거래대금"] >= args.min_value * 1e8]
    df = df.sort_values("시가총액", ascending=False).head(args.top)

    results = []
    for code, row in df.iterrows():
        try:
            name = _krx.get_market_ticker_name(code)
        except Exception:  # noqa: BLE001
            name = None
        results.append({
            "code": code, "name": name, "market": row["market"],
            "시가총액(억원)": _r(row["시가총액"] / 1e8, 0),
            "거래대금(억원)": _r(row["거래대금"] / 1e8, 0),
            "종가": float(row["종가"]),
        })
    return {"기준일": date, "필터": f"거래대금 ≥ {args.min_value}억, 시총 상위 {args.top}",
            "count": len(results), "candidates": results}


def cmd_investor(args):
    """투자자별 순매수 — 개인(개미) vs 외국인·기관 수급. 종목코드 여러 개 가능."""
    results = [_investor_one(code, args.days) for code in args.codes]
    return results[0] if len(results) == 1 else results


def _investor_one(code: str, days: int) -> dict:
    if _krx is None:
        return {"code": code, "error": "pykrx 미설치"}
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6) + 7)
    try:
        df = _quiet(_krx.get_market_trading_value_by_date, _ymd(start), _ymd(end), code)
    except Exception as e:  # noqa: BLE001
        return {"code": code, "error": f"투자자별 매매 조회 실패: {e}"}
    if df is None or df.empty:
        return {"code": code, "error": "투자자별 매매 데이터 없음 (KRX 로그인 필요할 수 있음)"}
    df = df.tail(days)
    cols = [c for c in ("개인", "외국인합계", "기관합계", "기타법인") if c in df.columns]

    def streak(s: pd.Series) -> int:
        """최근 연속 순매수(+)/순매도(-) 일수."""
        sign, n = (1 if s.iloc[-1] > 0 else -1), 0
        for x in reversed(s.tolist()):
            if (x > 0) == (sign > 0) and x != 0:
                n += 1
            else:
                break
        return sign * n

    return {
        "code": code, "기간": f"최근 {len(df)}거래일",
        "순매수합계(억원)": {c: _r(df[c].sum() / 1e8, 1) for c in cols},
        "최근5일_순매수(억원)": {c: _r(df[c].tail(5).sum() / 1e8, 1) for c in cols},
        "연속매매일수(+매수/-매도)": {c: streak(df[c]) for c in cols},
    }


def cmd_fundamental(args) -> dict:
    try:
        import dart_client
        info = dart_client.get_stock_fundamentals(args.name)
    except Exception as e:  # noqa: BLE001
        return {"name": args.name, "error": f"DART 조회 실패: {e}"}
    info.pop("재무", None)  # 원시 계정과목은 길어서 생략, 지표만

    code = info.get("종목코드")
    if code and _krx is not None:
        try:
            from price_client import get_price_info
            pinfo = get_price_info(code)
            info["주가정보"] = pinfo
            val = pinfo.get("krx_valuation") or {}
            ind = info.get("지표", {})
            price = pinfo.get("price")
            if price and ind.get("EPS(원)"):
                ind["PER(계산)"] = _r(price / ind["EPS(원)"])
            if price and ind.get("BPS(원)"):
                ind["PBR(계산)"] = _r(price / ind["BPS(원)"])
            if val.get("DIV") is not None:
                ind["배당수익률(%)"] = val["DIV"]
        except Exception:  # noqa: BLE001
            pass
    return info


def cmd_fx(args) -> dict:
    """USD/KRW 등 주요 환율의 현재 수준과 최근 추세."""
    end = datetime.now().date()
    start = end - timedelta(days=args.days + 10)
    url = f"https://api.frankfurter.app/{start}..{end}"
    try:
        resp = requests.get(url, params={"from": "USD", "to": "KRW,JPY,CNY,EUR"}, timeout=10)
        resp.raise_for_status()
        rates = resp.json()["rates"]
    except (requests.RequestException, KeyError, ValueError) as e:
        return {"error": f"환율 조회 실패: {e}. WebSearch로 'USD/KRW 환율'을 확인하세요."}

    df = pd.DataFrame(rates).T.sort_index()
    df.index = pd.to_datetime(df.index)
    krw = df["KRW"]
    out = {
        "출처": "frankfurter.app (ECB 기준환율, 일 1회)",
        "기준일": krw.index[-1].strftime("%Y-%m-%d"),
        "USD/KRW": _r(krw.iloc[-1]),
        "USD/KRW_변화(%)": {
            "1주": _r((krw.iloc[-1] / krw.iloc[-6] - 1) * 100) if len(krw) > 6 else None,
            "1개월": _r((krw.iloc[-1] / krw.iloc[-22] - 1) * 100) if len(krw) > 22 else None,
            "기간전체": _r((krw.iloc[-1] / krw.iloc[0] - 1) * 100),
        },
        "USD/KRW_기간고저": {"고": _r(krw.max()), "저": _r(krw.min())},
        "USD/KRW_MA20": _r(krw.tail(20).mean()),
    }
    # 원/엔(100엔당), 원/위안, 원/유로 교차환율
    for cur, unit in (("JPY", 100), ("CNY", 1), ("EUR", 1)):
        if cur in df:
            cross = krw / df[cur] * unit
            label = f"{cur}{'(100)' if unit == 100 else ''}/KRW"
            out[label] = _r(cross.iloc[-1])
    return out


# ----------------------------------------------------------------------
# 미국 후보군 스크리닝 — 규칙과 근거 논문은 Investment/SCREENING.md
# ----------------------------------------------------------------------
US_RULES = {
    "min_history": 273,        # 12-1 모멘텀 계산에 252+21 거래일 필요
    "min_price": 10.0,         # 저가주 제외 (호가 단위 대비 변동 큼)
    "min_dollar_vol": 50e6,    # 20일 중앙값 거래대금 $5천만 — 손절 주문 슬리피지 (Amihud 2002)
    "atr_band": (1.5, 3.0),    # ATR14% — 손절폭 5%가 ATR의 1.7~3.3배 (Wilder 1978, ATR 손절 관행)
    "max_decile": 0.9,         # 최근 21일 최대 일간수익률 상위 10% 제외 (Bali·Cakici·Whitelaw 2011)
    "sector_share": 0.2,       # 후보군 중 한 GICS 섹터 최대 20%
}


def us_factors(df: pd.DataFrame) -> Optional[dict]:
    """한 종목의 필터·팩터 원자료. 이력이 짧으면 None."""
    if len(df) < US_RULES["min_history"]:
        return None
    c, h, l = df["close"], df["high"], df["low"]
    last = float(c.iloc[-1])
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    form = c.iloc[-253:-20].pct_change().dropna()   # 형성기간: t-252 ~ t-21 (최근 1개월 제외)
    mom = float(c.iloc[-21] / c.iloc[-253] - 1)
    return {
        "price": last,
        "dollar_vol": float((c * df["volume"]).tail(20).median()),
        "atr_pct": float(tr.tail(14).mean() / last * 100),
        "above_ma200": last > c.tail(200).mean() and c.tail(50).mean() > c.tail(200).mean(),
        "max_ret": float(c.pct_change().tail(21).max()),
        "mom_12_1": mom,                                              # Jegadeesh·Titman 1993
        "near_52w_high": last / float(h.tail(252).max()),             # George·Hwang 2004
        # 정보 불연속성 ID = sign(PRET)·(%하락일 − %상승일). 낮을수록 '조용한' 모멘텀 (Da·Gurun·Warachka 2014)
        "info_discrete": (1 if mom > 0 else -1) * ((form < 0).mean() - (form > 0).mean()),
    }


def rank_us(rows: list[dict], top: int) -> tuple[list[dict], dict]:
    """rows: {ticker, name, sector, **us_factors}. 필터 → 팩터 백분위 동일가중 → 섹터 상한. (후보, 단계별 생존 수)"""
    R = US_RULES
    df = pd.DataFrame(rows)
    funnel = {"대상": len(df)}
    steps = [
        ("가격≥$10", df["price"] >= R["min_price"]),
        ("거래대금≥$50M", df["dollar_vol"] >= R["min_dollar_vol"]),
        ("ATR 1.5~3.0%", df["atr_pct"].between(*R["atr_band"])),
        ("추세(>MA200, MA50>MA200)", df["above_ma200"].astype(bool)),
    ]
    for name, mask in steps:
        df = df[mask.loc[df.index]]
        funnel[name] = len(df)
    # MAX 효과는 유니버스 내 상대 기준 — 추세 통과 종목 중 상위 10%
    df = df[df["max_ret"] <= df["max_ret"].quantile(R["max_decile"])]
    funnel["MAX 상위10% 제외"] = len(df)
    if df.empty:
        return [], funnel

    df = df.assign(score=(df["mom_12_1"].rank(pct=True) + df["near_52w_high"].rank(pct=True)
                          + df["info_discrete"].rank(pct=True, ascending=False)) / 3)
    cap = max(1, math.ceil(top * R["sector_share"]))
    picked = (df.sort_values("score", ascending=False)
                .groupby("sector", sort=False).head(cap)
                .sort_values("score", ascending=False).head(top))
    funnel["최종 후보"] = len(picked)
    return picked.to_dict("records"), funnel


def _sp500() -> pd.DataFrame:
    resp = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=_UA, timeout=15)
    resp.raise_for_status()
    t = pd.read_html(io.StringIO(resp.text))[0]
    return pd.DataFrame({"ticker": t["Symbol"].str.replace(".", "-", regex=False),
                         "name": t["Security"], "sector": t["GICS Sector"]})


def cmd_screen_us(args) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    try:
        universe = _sp500()
    except Exception as e:  # noqa: BLE001
        return {"error": f"S&P500 구성종목 조회 실패: {e}. WebSearch로 후보군을 구성하세요."}

    with ThreadPoolExecutor(8) as ex:
        prices = list(ex.map(lambda t: _ohlcv_yahoo(t, 300), universe["ticker"]))
    rows = []
    for u, df in zip(universe.to_dict("records"), prices):
        f = us_factors(df) if df is not None else None
        if f:
            rows.append({**u, **f})

    # 시장 국면: 지수가 200일선 아래면 모멘텀 붕괴 위험 (Daniel·Moskowitz 2016, Faber 2007)
    spx = _ohlcv_yahoo("^GSPC", 300)
    bull = bool(spx is not None and spx["close"].iloc[-1] > spx["close"].tail(200).mean())

    picked, funnel = rank_us(rows, args.top)
    funnel = {"S&P500": len(universe), "시세·이력 확보": len(rows), **{k: v for k, v in funnel.items() if k != "대상"}}
    return {
        "기준일": prices[0].index[-1].strftime("%Y-%m-%d") if prices and prices[0] is not None else None,
        "시장국면": "강세(S&P500 > 200일선)" if bull else "약세(S&P500 < 200일선) — 모멘텀 붕괴 위험, 현금 슬롯 검토",
        "규칙": "Investment/SCREENING.md",
        "단계별_생존": funnel,
        "후보": [{
            "ticker": p["ticker"], "name": p["name"], "sector": p["sector"], "종합점수": _r(p["score"], 3),
            "종가": _r(p["price"]), "12-1모멘텀(%)": _r(p["mom_12_1"] * 100, 1),
            "52주고가근접(%)": _r(p["near_52w_high"] * 100, 1), "정보불연속성": _r(p["info_discrete"], 3),
            "ATR14(%)": _r(p["atr_pct"]), "거래대금_중앙값($M)": _r(p["dollar_vol"] / 1e6, 0),
        } for p in picked],
    }


MACRO = {
    "^GSPC": "S&P500", "^NDX": "나스닥100", "^RUT": "러셀2000(소형주)", "^VIX": "VIX(변동성)",
    "^IRX": "미 3개월 국채금리(%)", "^FVX": "미 5년 국채금리(%)", "^TNX": "미 10년 국채금리(%)",
    "DX-Y.NYB": "달러인덱스", "CL=F": "WTI 유가($)", "BZ=F": "브렌트 유가($)", "GC=F": "금($)",
    "HYG": "하이일드 채권 ETF(신용위험)",
}


def cmd_macro(args) -> dict:
    """미국 시장 매크로 지표: 수준·변화율·200일선 대비·52주 위치, 장단기 금리차."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex:
        data = dict(zip(MACRO, ex.map(lambda s: _ohlcv_yahoo(s, 300), MACRO)))
    out, ref = {}, None
    for sym, label in MACRO.items():
        df = data[sym]
        if df is None or len(df) < 200:
            out[label] = {"error": "조회 실패"}
            continue
        c = df["close"]
        last, ref = float(c.iloc[-1]), ref or df.index[-1]
        out[label] = {
            "현재": _r(last), "1주(%)": _r((last / c.iloc[-6] - 1) * 100),
            "1개월(%)": _r((last / c.iloc[-22] - 1) * 100), "3개월(%)": _r((last / c.iloc[-64] - 1) * 100),
            "200일선 대비(%)": _r((last / c.tail(200).mean() - 1) * 100),
            "52주 범위 위치(%)": _r((last - c.tail(252).min()) / (c.tail(252).max() - c.tail(252).min()) * 100, 0),
        }
    t10, t3m = data["^TNX"], data["^IRX"]
    spread = None if t10 is None or t3m is None else _r(t10["close"].iloc[-1] - t3m["close"].iloc[-1])
    return {
        "기준일": ref.strftime("%Y-%m-%d") if ref is not None else None, "출처": "Yahoo Finance",
        # 10년-3개월 역전(음수)은 역사적으로 경기침체 선행 신호 (Estrella·Mishkin 1998)
        "장단기금리차_10Y-3M(%p)": spread,
        "지표": out,
    }


def main(argv=None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # 윈도우에서 `> 파일` 리다이렉트 시 cp949 로 저장되지 않게
    p = argparse.ArgumentParser(description="투자 에이전트 팀용 데이터 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="시총·거래대금 기준 후보군")
    s.add_argument("--market", default="ALL", choices=["ALL", "KOSPI", "KOSDAQ"])
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--min-value", type=float, default=100, help="최소 거래대금(억원)")
    s.set_defaults(fn=cmd_screen)

    s = sub.add_parser("chart", help="기술적 지표 + 전략 백테스트 (여러 종목 가능)")
    s.add_argument("codes", nargs="+")
    s.add_argument("--stop", type=float, default=5)
    s.add_argument("--take", type=float, default=15)
    s.add_argument("--horizon", type=int, default=60)
    s.set_defaults(fn=cmd_chart)

    s = sub.add_parser("bracket", help="손절/익절 전략 과거 시뮬레이션")
    s.add_argument("code")
    s.add_argument("--stop", type=float, default=5)
    s.add_argument("--take", type=float, default=15)
    s.add_argument("--horizon", type=int, default=60)
    s.add_argument("--lookback", type=int, default=250)
    s.set_defaults(fn=cmd_bracket)

    s = sub.add_parser("investor", help="투자자별 순매수 (여러 종목 가능)")
    s.add_argument("codes", nargs="+")
    s.add_argument("--days", type=int, default=20)
    s.set_defaults(fn=cmd_investor)

    s = sub.add_parser("fundamental", help="DART 재무지표")
    s.add_argument("name")
    s.set_defaults(fn=cmd_fundamental)

    s = sub.add_parser("screen-us", help="미국 S&P500 규칙 기반 후보군 (Investment/SCREENING.md)")
    s.add_argument("--top", type=int, default=30)
    s.set_defaults(fn=cmd_screen_us)

    s = sub.add_parser("macro", help="미국 매크로 지표 (지수·금리·유가·달러·VIX)")
    s.set_defaults(fn=cmd_macro)

    s = sub.add_parser("fx", help="환율")
    s.add_argument("--days", type=int, default=90)
    s.set_defaults(fn=cmd_fx)

    args = p.parse_args(argv)
    result = args.fn(args)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    items = result if isinstance(result, list) else [result]
    return 1 if all("error" in r for r in items) else 0


if __name__ == "__main__":
    sys.exit(main())
