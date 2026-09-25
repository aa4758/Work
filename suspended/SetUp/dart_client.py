"""
DART(전자공시시스템) OpenAPI 연동 모듈
----------------------------------------
이 파일이 하는 일:
  1. API 키를 파일/환경변수에서 읽어온다 (DART_API_KEY.env)
  2. 회사 이름 -> DART 고유번호(corp_code) 로 바꾼다 (전체 목록을 받아 캐시)
  3. 회사 개황 / 재무제표 / 발행주식 총수를 받아온다
  4. 받아온 숫자로 교육용 지표(ROE, 부채비율, 영업이익률, EPS, BPS, PER, PBR)를 계산한다

주의:
  - DART는 '재무 데이터'만 제공합니다. 주가(현재가)는 제공하지 않으므로
    PER/PBR을 계산하려면 현재 주가를 인자로 직접 넘겨야 합니다.
  - 무료 API는 하루 20,000건 제한이 있습니다. corp_code 목록은 캐시해서 재사용합니다.

공식 문서: https://opendart.fss.or.kr/guide/main.do
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import requests

BASE_URL = "https://opendart.fss.or.kr/api"
_CACHE_DIR = Path(__file__).parent / ".cache"
_CORP_CACHE = _CACHE_DIR / "corp_code.json"
_CORP_CACHE_TTL = 60 * 60 * 24 * 7  # 7일

# 사업보고서 종류 코드
REPRT_CODE = {
    "1분기": "11013",
    "반기": "11012",
    "3분기": "11014",
    "사업보고서": "11011",  # 연간
}


# ----------------------------------------------------------------------
# 1. API 키 로딩
# ----------------------------------------------------------------------
def load_api_key() -> str:
    """
    우선순위:
      1) 환경변수 DART_API_KEY
      2) 이 폴더의 DART_API_KEY.env
      3) 상위 폴더(C:\\work)의 DART_API_KEY.env
      4) 현재 작업 디렉터리의 DART_API_KEY.env
    파일 형식 예:  DART_API_KEY = feb02cc...
    """
    env_key = os.environ.get("DART_API_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    here = Path(__file__).parent
    candidates = [
        here / "DART_API_KEY.env",
        here.parent / "DART_API_KEY.env",
        here.parent / "Security" / "DART_API_KEY.env",
        here.parent.parent / "Security" / "DART_API_KEY.env",  # suspended/ 로 옮겨진 경우
        Path.cwd() / "DART_API_KEY.env",
        Path.cwd() / "Security" / "DART_API_KEY.env",
    ]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DART_API_KEY":
                return value.strip().strip('"').strip("'")

    raise RuntimeError(
        "DART_API_KEY를 찾을 수 없습니다. "
        "환경변수 DART_API_KEY 를 설정하거나 DART_API_KEY.env 파일을 만들어주세요."
    )


# ----------------------------------------------------------------------
# 2. 공통 요청 헬퍼
# ----------------------------------------------------------------------
class DartError(RuntimeError):
    """DART API가 정상(000)이 아닌 상태코드를 돌려줬을 때"""


def _get_json(endpoint: str, params: dict) -> dict:
    params = {"crtfc_key": load_api_key(), **params}
    resp = requests.get(f"{BASE_URL}/{endpoint}", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    status = data.get("status")
    # 000 정상 / 013 데이터 없음 -> 호출한 쪽에서 처리하도록 그대로 반환
    if status not in ("000", "013"):
        raise DartError(f"[{endpoint}] status={status} message={data.get('message')}")
    return data


# ----------------------------------------------------------------------
# 3. 회사명 -> corp_code 매핑 (전체 목록 다운로드 + 캐시)
# ----------------------------------------------------------------------
def _download_corp_code_map() -> dict[str, dict]:
    """corpCode.xml (ZIP) 를 받아 {회사명: {corp_code, stock_code}} 딕셔너리로 변환"""
    resp = requests.get(
        f"{BASE_URL}/corpCode.xml",
        params={"crtfc_key": load_api_key()},
        timeout=30,
    )
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])

    root = ET.fromstring(xml_bytes)
    mapping: dict[str, dict] = {}
    for item in root.iter("list"):
        name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        stock_code = (item.findtext("stock_code") or "").strip()
        if not name or not corp_code:
            continue
        # 상장사(종목코드 있음)를 우선. 이미 상장사가 등록돼 있으면 덮어쓰지 않는다.
        if name in mapping and mapping[name]["stock_code"] and not stock_code:
            continue
        mapping[name] = {"corp_code": corp_code, "stock_code": stock_code}
    return mapping


def get_corp_code_map(force_refresh: bool = False) -> dict[str, dict]:
    """캐시가 있으면 캐시, 없거나 오래됐으면 새로 받는다."""
    _CACHE_DIR.mkdir(exist_ok=True)
    if not force_refresh and _CORP_CACHE.exists():
        age = time.time() - _CORP_CACHE.stat().st_mtime
        if age < _CORP_CACHE_TTL:
            return json.loads(_CORP_CACHE.read_text(encoding="utf-8"))

    mapping = _download_corp_code_map()
    _CORP_CACHE.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def find_corp_code(company_name: str) -> Optional[str]:
    """회사명으로 corp_code 조회. 정확히 일치 -> 부분 일치(상장사 우선) 순으로 시도."""
    mapping = get_corp_code_map()
    if company_name in mapping:
        return mapping[company_name]["corp_code"]

    partial = [
        (name, info)
        for name, info in mapping.items()
        if company_name in name
    ]
    if not partial:
        return None
    # 상장사(stock_code 존재) 우선, 그다음 이름이 짧은(가장 근접한) 것
    partial.sort(key=lambda kv: (kv[1]["stock_code"] == "", len(kv[0])))
    return partial[0][1]["corp_code"]


# ----------------------------------------------------------------------
# 4. 개별 엔드포인트
# ----------------------------------------------------------------------
def get_company_overview(corp_code: str) -> dict:
    """회사 개황 (company.json): 이름, 대표자, 업종코드, 상장 여부 등"""
    data = _get_json("company.json", {"corp_code": corp_code})
    if data.get("status") == "013":
        return {}
    return {
        "corp_name": data.get("corp_name"),
        "stock_name": data.get("stock_name"),
        "stock_code": data.get("stock_code"),
        "ceo_nm": data.get("ceo_nm"),
        "induty_code": data.get("induty_code"),
        "est_dt": data.get("est_dt"),
        "adres": data.get("adres"),
    }


# 표준 계정ID -> 우리가 쓰는 이름
_ACCOUNT_MAP = {
    "매출액": ["ifrs-full_Revenue", "ifrs_Revenue", "dart_OperatingRevenue"],
    "영업이익": ["dart_OperatingIncomeLoss", "ifrs-full_OperatingIncomeLoss"],
    "당기순이익": ["ifrs-full_ProfitLoss", "ifrs_ProfitLoss"],
    "자산총계": ["ifrs-full_Assets"],
    "부채총계": ["ifrs-full_Liabilities"],
    "자본총계": ["ifrs-full_Equity"],
    "지배주주지분": ["ifrs-full_EquityAttributableToOwnersOfParent"],
}


def _to_number(raw: str) -> Optional[float]:
    if raw is None:
        return None
    raw = raw.replace(",", "").strip()
    if raw in ("", "-"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_financials(corp_code: str, year: int, reprt_code: str = "11011") -> dict:
    """
    단일회사 전체 재무제표 (fnlttSinglAcntAll.json).
    연결(CFS) 우선, 없으면 별도(OFS).
    반환: {"매출액": 값, "영업이익": 값, ...} (원 단위, float)
    """
    for fs_div in ("CFS", "OFS"):
        data = _get_json(
            "fnlttSinglAcntAll.json",
            {
                "corp_code": corp_code,
                "bsns_year": str(year),
                "reprt_code": reprt_code,
                "fs_div": fs_div,
            },
        )
        rows = data.get("list") or []
        if not rows:
            continue

        by_id: dict[str, str] = {}
        by_name: dict[str, str] = {}
        for row in rows:
            amount = row.get("thstrm_amount")
            acc_id = (row.get("account_id") or "").strip()
            acc_nm = (row.get("account_nm") or "").strip()
            if acc_id and acc_id not in by_id:
                by_id[acc_id] = amount
            if acc_nm and acc_nm not in by_name:
                by_name[acc_nm] = amount

        result: dict[str, Optional[float]] = {"fs_div": fs_div}
        for label, id_candidates in _ACCOUNT_MAP.items():
            value = None
            for cid in id_candidates:
                if cid in by_id:
                    value = _to_number(by_id[cid])
                    break
            if value is None and label in by_name:  # 계정ID 매칭 실패 시 이름으로
                value = _to_number(by_name[label])
            result[label] = value
        return result

    return {}


def get_shares_outstanding(corp_code: str, year: int, reprt_code: str = "11011") -> Optional[float]:
    """
    주식의 총수 현황 (stockTotqySttus.json) 에서 '보통주 유통주식수'를 우선 반환.
    없으면 발행주식총수 - 자기주식수.
    """
    data = _get_json(
        "stockTotqySttus.json",
        {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code},
    )
    rows = data.get("list") or []
    for row in rows:
        se = (row.get("se") or "").strip()
        if se in ("보통주", "합계"):
            issued = _to_number(row.get("istc_totqy"))
            treasury = _to_number(row.get("tesstk_co")) or 0.0
            distb = _to_number(row.get("distb_stock_co"))
            if distb and distb > 0:
                return distb
            if issued:
                return issued - treasury
    return None


# ----------------------------------------------------------------------
# 5. 교육용 지표 계산
# ----------------------------------------------------------------------
def compute_indicators(
    financials: dict,
    shares: Optional[float] = None,
    price: Optional[float] = None,
) -> dict:
    """
    재무 숫자 -> 지표. 계산에 필요한 값이 없으면 해당 지표는 None.
    price(현재 주가)를 넘기면 EPS/BPS 기준으로 PER/PBR도 계산.
    """
    revenue = financials.get("매출액")
    op_income = financials.get("영업이익")
    net_income = financials.get("당기순이익")
    equity = financials.get("지배주주지분") or financials.get("자본총계")
    liabilities = financials.get("부채총계")
    total_equity = financials.get("자본총계")

    def ratio(numer, denom):
        if numer is None or denom in (None, 0):
            return None
        return round(numer / denom * 100, 2)

    eps = None
    bps = None
    if shares and shares > 0:
        if net_income is not None:
            eps = round(net_income / shares, 1)
        if equity is not None:
            bps = round(equity / shares, 1)

    per = round(price / eps, 2) if price and eps and eps > 0 else None
    pbr = round(price / bps, 2) if price and bps and bps > 0 else None

    return {
        "매출액": revenue,
        "영업이익": op_income,
        "당기순이익": net_income,
        "영업이익률(%)": ratio(op_income, revenue),
        "ROE(%)": ratio(net_income, equity),
        "부채비율(%)": ratio(liabilities, total_equity),
        "EPS(원)": eps,
        "BPS(원)": bps,
        "PER": per,
        "PBR": pbr,
    }


# ----------------------------------------------------------------------
# 6. 앱에서 부르는 하이레벨 함수
# ----------------------------------------------------------------------
def guess_latest_year(reprt: str = "사업보고서") -> int:
    """
    '지금 시점에서 나와 있을 법한' 최신 사업연도 추정치.
    사업보고서는 보통 다음 해 3월 말에 공시되므로, 4월 이후면 작년 / 이전이면 재작년.
    (실제로 데이터가 있는지는 find_latest_year_with_data 가 확인)
    """
    now = time.localtime()
    if reprt == "사업보고서":
        return now.tm_year - 1 if now.tm_mon >= 4 else now.tm_year - 2
    return now.tm_year if now.tm_mon >= 6 else now.tm_year - 1


def find_latest_year_with_data(
    corp_code: str, reprt_code: str = "11011", max_back: int = 3
) -> tuple[Optional[int], dict]:
    """
    최신 연도부터 거꾸로 내려가며 실제로 재무데이터가 있는 첫 연도를 찾는다.
    회사마다 공시 시점이 달라서(결산월 차이, 지연공시 등) 고정 연도로는 빈 결과가 나올 수 있음.
    반환: (연도, 재무dict) — 끝까지 못 찾으면 (None, {})
    """
    start = guess_latest_year()
    for offset in range(max_back + 1):
        year = start - offset
        financials = get_financials(corp_code, year, reprt_code)
        if financials:
            return year, financials
    return None, {}


def get_stock_fundamentals(
    company_name: str,
    year: Optional[int] = None,
    reprt: str = "사업보고서",
    price: Optional[float] = None,
) -> dict:
    """
    회사명 하나로 개황 + 재무 + 지표를 한 번에.
    year를 생략하면 데이터가 있는 '가장 최근 연도'를 자동으로 찾는다.
    반환 예:
      {
        "회사명": "삼성전자", "종목코드": "005930", "대표자": "...",
        "연도": 2025, "보고서": "사업보고서", "연도자동": True,
        "재무": {...}, "지표": {...},
        "출처": "DART",
      }
    데이터가 없으면 DartError 를 던지므로, 호출하는 쪽에서 mock 으로 fallback 하세요.
    """
    corp_code = find_corp_code(company_name)
    if not corp_code:
        raise DartError(f"'{company_name}' 의 corp_code를 찾지 못했습니다.")

    reprt_code = REPRT_CODE.get(reprt, "11011")
    overview = get_company_overview(corp_code)

    auto_year = year is None
    if auto_year:
        year, financials = find_latest_year_with_data(corp_code, reprt_code)
        if not financials:
            raise DartError(
                f"'{company_name}' 의 최근 {reprt} 재무데이터를 찾지 못했습니다. "
                f"(최근 4개 연도 조회)"
            )
    else:
        financials = get_financials(corp_code, year, reprt_code)
        if not financials:
            raise DartError(f"'{company_name}' {year}년 {reprt} 재무데이터가 없습니다.")

    shares = None
    try:
        shares = get_shares_outstanding(corp_code, year, reprt_code)
    except DartError:
        pass

    indicators = compute_indicators(financials, shares=shares, price=price)

    return {
        "corp_code": corp_code,
        "회사명": overview.get("corp_name") or company_name,
        "종목명": overview.get("stock_name"),
        "종목코드": overview.get("stock_code"),
        "대표자": overview.get("ceo_nm"),
        "설립일": overview.get("est_dt"),
        "연도": year,
        "연도자동": auto_year,
        "보고서": reprt,
        "상장주식수": shares,
        "재무": financials,
        "지표": indicators,
        "출처": "DART",
    }


# ----------------------------------------------------------------------
# 직접 실행하면 간단 점검
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "삼성전자"
    price_arg = float(sys.argv[2]) if len(sys.argv) > 2 else None

    print(f"API 키 앞 6자리: {load_api_key()[:6]}...")
    print(f"corp_code({target}): {find_corp_code(target)}")

    info = get_stock_fundamentals(target, price=price_arg)
    print(json.dumps(info, ensure_ascii=False, indent=2))
