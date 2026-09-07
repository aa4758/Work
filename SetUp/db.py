"""
SQLite 데이터베이스 모듈
------------------------
이 파일이 하는 일:
  1. 사용자가 등록한 포트폴리오를 '영구 저장'한다 (앱을 껐다 켜도 유지)
  2. DART 전체 회사 목록(약 10만 건)을 종목 마스터로 저장해, 등록 가능한 종목 제한을 없앤다
  3. DART 재무 / KRX 주가 조회 결과를 캐시해서 API 호출 수를 줄인다
     (DART 무료 키는 하루 20,000건 제한)

설계 원칙:
  - 이 모듈은 '저장'만 담당합니다. DART/KRX를 직접 호출하지 않습니다.
    (dart_client.py, price_client.py 와 의존성이 얽히지 않도록)
  - 매 호출마다 커넥션을 열고 닫습니다. Streamlit은 요청마다 스레드가 달라서
    커넥션을 전역으로 재사용하면 "SQLite objects created in a thread..." 오류가 납니다.

DB 파일 위치: SetUp/data/app.db
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).parent / "data" / "app.db"
SCHEMA_VERSION = 3
DEFAULT_USER = "local"  # 로그인 기능이 생기기 전까지 쓰는 단일 사용자 ID


class SlotLimitExceeded(RuntimeError):
    """무료 슬롯 한도를 넘겨 종목을 추가하려 할 때"""


# ----------------------------------------------------------------------
# 1. 연결 / 스키마
# ----------------------------------------------------------------------
_SCHEMA = """
-- 종목 마스터: DART 회사 목록을 그대로 받아둔다 (등록 가능 종목 제한 해소)
CREATE TABLE IF NOT EXISTS stocks (
    corp_code   TEXT PRIMARY KEY,       -- DART 고유번호 8자리
    corp_name   TEXT NOT NULL,          -- 회사명
    stock_code  TEXT,                   -- 6자리 종목코드 (비상장이면 빈 문자열)
    is_listed   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stocks_name   ON stocks(corp_name);
CREATE INDEX IF NOT EXISTS idx_stocks_code   ON stocks(stock_code);
CREATE INDEX IF NOT EXISTS idx_stocks_listed ON stocks(is_listed);

-- 사용자 포트폴리오
-- 보유 '비중'은 저장하지 않는다. 비중 = (보유수량 x 현재주가) / 전체 평가금액 이므로
-- 주가가 매일 바뀌면 비중도 바뀐다. 그래서 변하지 않는 '보유 수량'만 저장하고
-- 비중은 화면에 그릴 때마다 계산한다.
CREATE TABLE IF NOT EXISTS portfolio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL DEFAULT 'local',
    corp_name   TEXT NOT NULL,
    corp_code   TEXT,
    stock_code  TEXT,
    shares      REAL NOT NULL DEFAULT 0,     -- 보유 주식 수
    memo        TEXT,
    added_at    TEXT NOT NULL,
    UNIQUE(user_id, corp_name)
);
CREATE INDEX IF NOT EXISTS idx_portfolio_user ON portfolio(user_id);

-- DART 재무 조회 캐시 (payload는 get_stock_fundamentals 결과 JSON 통째로)
CREATE TABLE IF NOT EXISTS financial_cache (
    corp_code   TEXT NOT NULL,
    bsns_year   INTEGER NOT NULL,
    reprt       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (corp_code, bsns_year, reprt)
);

-- KRX 주가 캐시 (거래일별로 쌓이므로 나중에 차트로도 쓸 수 있음)
CREATE TABLE IF NOT EXISTS price_cache (
    stock_code  TEXT NOT NULL,
    trade_date  TEXT NOT NULL,          -- YYYY-MM-DD
    close       REAL,
    open        REAL,
    high        REAL,
    low         REAL,
    volume      INTEGER,
    change_pct  REAL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (stock_code, trade_date)
);

-- LLM이 생성한 지표 해설 캐시
-- 같은 회사·연도·지표 조합에 매번 API를 호출하면 그때마다 과금되므로,
-- 지표 값으로 만든 해시를 키로 삼아 재사용한다.
CREATE TABLE IF NOT EXISTS explanation_cache (
    cache_key   TEXT PRIMARY KEY,      -- llm_client._cache_key() 의 sha256
    corp_name   TEXT,
    bsns_year   INTEGER,
    reprt       TEXT,
    model       TEXT,
    explanation TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 스키마 버전 등 메타정보
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def get_conn():
    """커넥션을 열고, 정상 종료 시 commit / 예외 시 rollback."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn) -> None:
    """
    이미 만들어진 DB를 새 스키마에 맞춘다.
    v1 -> v2: portfolio.weight(보유 비중) 대신 portfolio.shares(보유 수량)를 쓴다.
              비중은 주가에 따라 매일 바뀌므로 저장하지 않고 매번 계산하기로 했다.
              (기존 weight 컬럼은 지우지 않고 그대로 둔다 — 값은 더 이상 쓰지 않음)
    v2 -> v3: explanation_cache 테이블 추가. 위 _SCHEMA 의 CREATE TABLE IF NOT EXISTS 가
              알아서 만들어주므로 여기서 따로 할 일은 없다.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(portfolio)")}
    if cols and "shares" not in cols:
        conn.execute("ALTER TABLE portfolio ADD COLUMN shares REAL NOT NULL DEFAULT 0")


def init_db() -> None:
    """테이블이 없으면 만들고, 있으면 새 스키마로 맞춘다. 여러 번 불러도 안전(idempotent)."""
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")  # 읽기/쓰기 동시성 개선
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------------
# 2. 종목 마스터
# ----------------------------------------------------------------------
def sync_stock_master(mapping: dict[str, dict]) -> int:
    """
    dart_client.get_corp_code_map() 결과를 통째로 저장.
    mapping 형식: {"삼성전자": {"corp_code": "00126380", "stock_code": "005930"}, ...}
    반환: 저장된 건수
    """
    now = _now()
    rows = [
        (
            info["corp_code"],
            name,
            info.get("stock_code") or "",
            1 if (info.get("stock_code") or "").strip() else 0,
            now,
        )
        for name, info in mapping.items()
    ]
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO stocks(corp_code, corp_name, stock_code, is_listed, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(corp_code) DO UPDATE SET "
            "  corp_name = excluded.corp_name, "
            "  stock_code = excluded.stock_code, "
            "  is_listed = excluded.is_listed, "
            "  updated_at = excluded.updated_at",
            rows,
        )
    return len(rows)


def stock_master_count(listed_only: bool = False) -> int:
    sql = "SELECT COUNT(*) AS c FROM stocks"
    if listed_only:
        sql += " WHERE is_listed = 1"
    with get_conn() as conn:
        return conn.execute(sql).fetchone()["c"]


def search_stocks(query: str, limit: int = 20, listed_only: bool = True) -> list[dict]:
    """
    회사명 부분검색. 정확히 일치하는 것 → 앞에서부터 일치 → 나머지 순으로 정렬.
    종목 등록 화면의 자동완성용.
    """
    query = (query or "").strip()
    if not query:
        return []
    sql = """
        SELECT corp_code, corp_name, stock_code, is_listed
        FROM stocks
        WHERE corp_name LIKE ? {listed}
        ORDER BY
            CASE WHEN corp_name = ? THEN 0
                 WHEN corp_name LIKE ? THEN 1
                 ELSE 2 END,
            is_listed DESC,
            LENGTH(corp_name),
            corp_name
        LIMIT ?
    """.format(listed="AND is_listed = 1" if listed_only else "")
    with get_conn() as conn:
        rows = conn.execute(sql, (f"%{query}%", query, f"{query}%", limit)).fetchall()
    return [dict(r) for r in rows]


def get_stock(corp_name: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT corp_code, corp_name, stock_code, is_listed FROM stocks "
            "WHERE corp_name = ? ORDER BY is_listed DESC LIMIT 1",
            (corp_name,),
        ).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------------
# 3. 포트폴리오
# ----------------------------------------------------------------------
def list_portfolio(user_id: str = DEFAULT_USER) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, corp_name, corp_code, stock_code, shares, memo, added_at "
            "FROM portfolio WHERE user_id = ? ORDER BY added_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_portfolio(user_id: str = DEFAULT_USER) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM portfolio WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]


def add_stock(
    corp_name: str,
    corp_code: Optional[str] = None,
    stock_code: Optional[str] = None,
    shares: float = 0.0,
    memo: Optional[str] = None,
    user_id: str = DEFAULT_USER,
    slot_limit: Optional[int] = None,
) -> bool:
    """
    포트폴리오에 종목 추가. shares는 보유 주식 수.
    반환: True(추가됨) / False(이미 등록된 종목)
    slot_limit을 주면 한도 초과 시 SlotLimitExceeded 예외.
    """
    corp_name = corp_name.strip()
    if not corp_name:
        raise ValueError("종목명이 비어 있습니다.")

    # 마스터에 있으면 코드들을 자동으로 채워준다
    if corp_code is None or stock_code is None:
        found = get_stock(corp_name)
        if found:
            corp_code = corp_code or found["corp_code"]
            stock_code = stock_code or found["stock_code"]

    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM portfolio WHERE user_id = ? AND corp_name = ?",
            (user_id, corp_name),
        ).fetchone()
        if exists:
            return False

        if slot_limit is not None:
            current = conn.execute(
                "SELECT COUNT(*) AS c FROM portfolio WHERE user_id = ?", (user_id,)
            ).fetchone()["c"]
            if current >= slot_limit:
                raise SlotLimitExceeded(
                    f"무료 사용자는 최대 {slot_limit}종목까지만 등록할 수 있어요."
                )

        conn.execute(
            "INSERT INTO portfolio(user_id, corp_name, corp_code, stock_code, shares, memo, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, corp_name, corp_code, stock_code, max(float(shares), 0.0), memo, _now()),
        )
    return True


def remove_stock(corp_name: str, user_id: str = DEFAULT_USER) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM portfolio WHERE user_id = ? AND corp_name = ?",
            (user_id, corp_name),
        )
    return cur.rowcount > 0


def update_shares(corp_name: str, shares: float, user_id: str = DEFAULT_USER) -> bool:
    """보유 수량 수정. 비중은 저장하지 않고 주가와 함께 매번 계산한다."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE portfolio SET shares = ? WHERE user_id = ? AND corp_name = ?",
            (max(float(shares), 0.0), user_id, corp_name),
        )
    return cur.rowcount > 0


def clear_portfolio(user_id: str = DEFAULT_USER) -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM portfolio WHERE user_id = ?", (user_id,))
    return cur.rowcount


# ----------------------------------------------------------------------
# 4. 캐시 (DART 재무 / KRX 주가)
# ----------------------------------------------------------------------
def save_financials(corp_code: str, year: int, reprt: str, payload: dict) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO financial_cache(corp_code, bsns_year, reprt, payload, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(corp_code, bsns_year, reprt) DO UPDATE SET "
            "  payload = excluded.payload, fetched_at = excluded.fetched_at",
            (corp_code, int(year), reprt, json.dumps(payload, ensure_ascii=False), _now()),
        )


def get_cached_financials(
    corp_code: str, year: int, reprt: str, max_age_days: int = 7
) -> Optional[dict]:
    """확정된 재무제표는 잘 안 바뀌므로 기본 7일 캐시."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM financial_cache "
            "WHERE corp_code = ? AND bsns_year = ? AND reprt = ?",
            (corp_code, int(year), reprt),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["fetched_at"]) < datetime.now() - timedelta(days=max_age_days):
        return None
    return json.loads(row["payload"])


def get_latest_cached_financials(
    corp_code: str, reprt: str, max_age_days: int = 7
) -> Optional[dict]:
    """
    연도를 지정하지 않고 '캐시된 것 중 가장 최근 연도'를 반환.
    앱이 연도를 자동으로 정하므로, 호출 전에 연도를 알 수 없어서 필요하다.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM financial_cache "
            "WHERE corp_code = ? AND reprt = ? ORDER BY bsns_year DESC LIMIT 1",
            (corp_code, reprt),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["fetched_at"]) < datetime.now() - timedelta(days=max_age_days):
        return None
    return json.loads(row["payload"])


def save_price(stock_code: str, ohlcv: dict) -> None:
    """price_client.get_latest_close() 결과(dict)를 거래일 기준으로 저장."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO price_cache(stock_code, trade_date, close, open, high, low, "
            "volume, change_pct, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(stock_code, trade_date) DO UPDATE SET "
            "  close = excluded.close, open = excluded.open, high = excluded.high, "
            "  low = excluded.low, volume = excluded.volume, "
            "  change_pct = excluded.change_pct, fetched_at = excluded.fetched_at",
            (
                stock_code, ohlcv["date"], ohlcv.get("close"), ohlcv.get("open"),
                ohlcv.get("high"), ohlcv.get("low"), ohlcv.get("volume"),
                ohlcv.get("change_pct"), _now(),
            ),
        )


def get_cached_price(stock_code: str, max_age_minutes: int = 30) -> Optional[dict]:
    """가장 최근 거래일 캐시가 충분히 신선하면 반환."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM price_cache WHERE stock_code = ? "
            "ORDER BY trade_date DESC LIMIT 1",
            (stock_code,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["fetched_at"]) < datetime.now() - timedelta(minutes=max_age_minutes):
        return None
    return {
        "date": row["trade_date"], "close": row["close"], "open": row["open"],
        "high": row["high"], "low": row["low"], "volume": row["volume"],
        "change_pct": row["change_pct"],
    }


def get_price_history(stock_code: str, limit: int = 60) -> list[dict]:
    """쌓인 주가 캐시를 오래된 순으로. (나중에 차트 그릴 때 사용)"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT trade_date, close, open, high, low, volume, change_pct "
            "FROM price_cache WHERE stock_code = ? ORDER BY trade_date DESC LIMIT ?",
            (stock_code, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def save_explanation(cache_key: str, text: str, info: dict, model: str) -> None:
    """LLM이 만든 해설을 저장."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO explanation_cache"
            "(cache_key, corp_name, bsns_year, reprt, model, explanation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET "
            "  explanation = excluded.explanation, created_at = excluded.created_at",
            (
                cache_key, info.get("회사명"), info.get("연도"),
                info.get("보고서"), model, text, _now(),
            ),
        )


def get_cached_explanation(cache_key: str, max_age_days: int = 30) -> Optional[str]:
    """재무 숫자가 같으면 해설도 같으므로 기본 30일 캐시."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT explanation, created_at FROM explanation_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["created_at"]) < datetime.now() - timedelta(days=max_age_days):
        return None
    return row["explanation"]


def clear_explanations() -> int:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM explanation_cache")
    return cur.rowcount


def cache_stats() -> dict[str, Any]:
    with get_conn() as conn:
        return {
            "stocks": conn.execute("SELECT COUNT(*) c FROM stocks").fetchone()["c"],
            "listed": conn.execute("SELECT COUNT(*) c FROM stocks WHERE is_listed=1").fetchone()["c"],
            "portfolio": conn.execute("SELECT COUNT(*) c FROM portfolio").fetchone()["c"],
            "financial_cache": conn.execute("SELECT COUNT(*) c FROM financial_cache").fetchone()["c"],
            "price_cache": conn.execute("SELECT COUNT(*) c FROM price_cache").fetchone()["c"],
            "explanation_cache": conn.execute("SELECT COUNT(*) c FROM explanation_cache").fetchone()["c"],
            "db_path": str(DB_PATH),
            "db_size_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0,
        }


# ----------------------------------------------------------------------
# 명령줄 도구:  python db.py init | sync | stats | list
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    init_db()

    if cmd == "init":
        print(f"DB 준비 완료: {DB_PATH}")

    elif cmd == "sync":
        # 여기서만 dart_client를 쓴다 (db 모듈 자체는 DART에 의존하지 않음)
        import dart_client

        print("DART 회사 목록 다운로드 중... (처음엔 30초 정도 걸립니다)")
        mapping = dart_client.get_corp_code_map()
        n = sync_stock_master(mapping)
        print(f"종목 마스터 저장 완료: 전체 {n:,}건 / 상장사 {stock_master_count(True):,}건")

    elif cmd == "stats":
        for k, v in cache_stats().items():
            print(f"  {k:>16}: {v}")

    elif cmd == "list":
        rows = list_portfolio()
        if not rows:
            print("등록된 종목이 없습니다.")
        for r in rows:
            print(f"  {r['corp_name']:<20} {r['stock_code'] or '-':<8} {r['shares']:,.0f}주")

    else:
        print("사용법: python db.py [init|sync|stats|list]")
