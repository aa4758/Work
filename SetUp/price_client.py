"""
주가 자동 수집 모듈 (pykrx 기반)
--------------------------------
이 파일이 하는 일:
  - 6자리 종목코드로 '최근 종가'와 등락률을 가져온다 (KRX 일별 시세)
  - 가능하면 KRX가 공시하는 PER/PBR/EPS/BPS/배당수익률도 같이 가져온다
  - KRX 응답이 불안정할 때가 있어, 실패해도 예외 대신 부분 결과를 돌려준다

한계:
  - '실시간'이 아니라 '전 거래일 종가' 기준입니다. (교육용 PER/PBR 계산엔 충분)
  - 장중 현재가가 필요하면 증권사 API(한국투자·키움 등)를 붙여야 합니다.
  - pykrx는 KRX 사이트를 스크레이핑하므로 KRX가 포맷을 바꾸면 일부 함수가 멎을 수 있습니다.
    그래서 종가(get_market_ohlcv) 하나만 성공해도 동작하도록 설계했습니다.
"""

from __future__ import annotations

import contextlib
import io
import time
from datetime import datetime, timedelta
from typing import Optional

try:
    from pykrx import stock as _krx
    _PYKRX_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    _krx = None
    _PYKRX_AVAILABLE = False
    _PYKRX_ERROR = str(e)


def _yyyymmdd(d: datetime) -> str:
    return d.strftime("%Y%m%d")


def _retry(fn, tries: int = 3, delay: float = 0.8):
    """KRX가 가끔 빈 응답/JSON 오류를 주므로 몇 번 재시도."""
    last = None
    for i in range(tries):
        try:
            out = fn()
            if out is not None and (not hasattr(out, "empty") or not out.empty):
                return out
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(delay * (i + 1))
    if last:
        raise last
    return None


def get_latest_close(ticker: str, lookback_days: int = 14) -> Optional[dict]:
    """
    최근 거래일 종가. 반환: {"date","close","open","high","low","volume","change_pct"} 또는 None
    """
    if not _PYKRX_AVAILABLE:
        return None
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    def _call():
        return _krx.get_market_ohlcv(_yyyymmdd(start), _yyyymmdd(end), ticker)

    try:
        df = _retry(_call)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None

    row = df.iloc[-1]
    # pykrx 컬럼: 시가/고가/저가/종가/거래량/등락률
    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "close": float(row["종가"]),
        "open": float(row["시가"]),
        "high": float(row["고가"]),
        "low": float(row["저가"]),
        "volume": int(row["거래량"]),
        "change_pct": round(float(row["등락률"]), 2),
    }


def get_krx_valuation(ticker: str, lookback_days: int = 14) -> Optional[dict]:
    """
    KRX가 공시하는 밸류에이션 지표. 반환: {"date","PER","PBR","EPS","BPS","DIV","DPS"} 또는 None.
    (KRX 엔드포인트가 불안정하면 None — 이땐 DART EPS/BPS로 직접 계산하면 됩니다.)
    """
    if not _PYKRX_AVAILABLE:
        return None
    end = datetime.now()
    start = end - timedelta(days=lookback_days)

    def _call():
        # pykrx가 내부에서 오류 메시지를 print 하므로 조용히 삼킨다
        with contextlib.redirect_stdout(io.StringIO()):
            return _krx.get_market_fundamental(_yyyymmdd(start), _yyyymmdd(end), ticker)

    try:
        df = _retry(_call, tries=2)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None

    row = df.iloc[-1]

    def g(col):
        try:
            v = float(row[col])
            return v if v != 0 else None
        except (KeyError, TypeError, ValueError):
            return None

    return {
        "date": df.index[-1].strftime("%Y-%m-%d"),
        "PER": g("PER"),
        "PBR": g("PBR"),
        "EPS": g("EPS"),
        "BPS": g("BPS"),
        "DIV": g("DIV"),
        "DPS": g("DPS"),
    }


def resolve_ticker(name: str) -> Optional[str]:
    """
    회사명 -> 6자리 종목코드. dart_client가 stock_code를 주므로 보통 필요 없지만,
    보조 수단으로 KRX 상장목록에서도 찾아본다.
    """
    if not _PYKRX_AVAILABLE:
        return None
    today = _yyyymmdd(datetime.now())
    for market in ("KOSPI", "KOSDAQ", "KONEX"):
        try:
            for code in _krx.get_market_ticker_list(today, market=market):
                if _krx.get_market_ticker_name(code) == name:
                    return code
        except Exception:  # noqa: BLE001
            continue
    return None


def get_price_info(ticker: str) -> dict:
    """
    앱에서 부르는 하이레벨 함수. 종가 + (가능하면) KRX 밸류에이션을 묶어서 반환.
    반환:
      {
        "ticker": "005930",
        "price": 71000.0, "price_date": "2026-09-04", "change_pct": -1.2,
        "krx_valuation": {...} | None,
        "available": True/False,   # 종가라도 얻었는지
        "note": "설명 문자열",
      }
    """
    if not _PYKRX_AVAILABLE:
        return {
            "ticker": ticker, "price": None, "available": False,
            "krx_valuation": None,
            "note": f"pykrx 사용 불가: {_PYKRX_ERROR}",
        }

    close = get_latest_close(ticker)
    valuation = get_krx_valuation(ticker)

    if close is None:
        return {
            "ticker": ticker, "price": None, "available": False,
            "krx_valuation": valuation,
            "note": "KRX에서 종가를 가져오지 못했습니다. (휴장일/코드오류/KRX 응답 오류)",
        }

    return {
        "ticker": ticker,
        "price": close["close"],
        "price_date": close["date"],
        "change_pct": close["change_pct"],
        "ohlcv": close,
        "krx_valuation": valuation,
        "available": True,
        "note": f"{close['date']} 종가 기준" + ("" if valuation else " (KRX 밸류에이션은 제공 안 됨 → DART로 계산)"),
    }


if __name__ == "__main__":
    import json
    import sys

    code = sys.argv[1] if len(sys.argv) > 1 else "005930"
    print(json.dumps(get_price_info(code), ensure_ascii=False, indent=2))
