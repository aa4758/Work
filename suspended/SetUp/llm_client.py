"""
LLM(Claude) 지표 해설 생성 모듈
--------------------------------
`app.py`의 규칙 기반 `explain_from_dart()` 를 대체한다.

핵심 제약 (계획서 원칙 2):
    "오른다/내린다" 같은 예측성 표현 금지 — 객관적 지표 설명에 그칠 것 (투자자문업 규제 회피)
이 제약은 시스템 프롬프트에 명시하고, 생성된 문장을 한 번 더 검사해서
금지 표현이 섞이면 규칙 기반 해설로 되돌린다 (프롬프트만 믿지 않는다).

키가 없거나 SDK가 없으면 `LLMUnavailable` 을 던지므로,
호출하는 쪽에서 기존 규칙 기반 해설로 fallback 하면 된다.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

import config

try:
    import anthropic
    _HAS_SDK = True
except Exception:  # noqa: BLE001
    anthropic = None
    _HAS_SDK = False

# Claude Opus 5 — 현 세대 최신 모델
MODEL = "claude-opus-5"

# 해설은 짧은 글이라 출력 토큰이 많이 필요 없다.
# Opus 5는 사고(thinking)가 기본으로 켜져 있고 그 토큰도 max_tokens에 포함되므로 여유를 둔다.
MAX_TOKENS = 4000

# 지표 설명은 정형화된 쉬운 작업이라 effort를 낮춰 비용을 아낀다.
# 해설 품질을 더 올리고 싶으면 "medium" 또는 "high"로 바꾸면 된다.
EFFORT = "low"


class LLMUnavailable(RuntimeError):
    """키·SDK가 없거나 호출에 실패해서 LLM 해설을 만들 수 없을 때"""


SYSTEM_PROMPT = """당신은 투자를 처음 접하는 청년에게 기업의 재무지표를 설명해주는 '교육용' 도우미입니다.

[역할]
- 주어진 재무 숫자가 무엇을 뜻하는지 객관적으로 풀어서 설명합니다.
- 회계 용어를 일상어로 바꿔 설명합니다. (예: ROE → "회사가 주주 돈으로 얼마나 벌었는지")

[반드시 지켜야 할 금지 규칙]
1. 주가 예측·매매 권유를 절대 하지 마세요.
   금지 표현: 오른다, 내린다, 상승/하락할 것, 유망하다, 매수/매도, 사야/팔아야,
   추천한다, 저평가다/고평가다(단정), 지금이 기회다, 목표주가 등.
2. 단정하지 마세요. 하나의 지표는 여러 해석이 가능하다는 점을 함께 적으세요.
   (예: "PER이 낮다"는 것은 이익 대비 주가가 낮다는 '사실'이며,
    그 이유가 저평가인지 이익 감소 전망인지는 이 숫자만으로 알 수 없습니다.)
3. 주어진 숫자 외의 사실을 지어내지 마세요. 업종 평균, 경쟁사, 뉴스, 향후 실적,
   최근 주가 흐름은 당신에게 주어지지 않았습니다. 모르면 모른다고 쓰세요.
4. 마지막 줄에 투자 판단은 사용자 본인의 몫이라는 취지의 문장을 넣으세요.

[형식]
- 한국어, 마크다운 불릿 4~6개, 전체 500자 내외
- 각 불릿은 "지표 이름 + 숫자 + 그게 무슨 뜻인지" 순서
- 금액은 조/억 단위로 읽기 쉽게 (예: 333조 6,059억 원)
- 어려운 용어를 쓰면 괄호로 짧게 풀이"""

# 생성 결과에 이런 표현이 있으면 규제 위험이 있다고 보고 규칙 기반으로 되돌린다.
_FORBIDDEN = [
    "매수", "매도", "사야", "팔아야", "추천드", "추천합", "추천해",
    "유망", "목표주가", "지금이 기회", "오를 것", "내릴 것",
    "상승할 것", "하락할 것", "상승이 예상", "하락이 예상", "수익률이 기대",
]


def is_available() -> bool:
    """LLM 해설을 쓸 수 있는 상태인지 (화면에서 토글 on/off 판단용)."""
    return _HAS_SDK and config.has_key("ANTHROPIC_API_KEY")


def unavailable_reason() -> Optional[str]:
    if not _HAS_SDK:
        return "anthropic 패키지가 없습니다. `pip install anthropic` 후 다시 실행하세요."
    if not config.has_key("ANTHROPIC_API_KEY"):
        return (
            "ANTHROPIC_API_KEY가 없습니다. Security 폴더에 "
            "`ANTHROPIC_API_KEY.env` 파일을 만들고 `ANTHROPIC_API_KEY = sk-ant-...` 로 저장하세요."
        )
    return None


def _won(v) -> str:
    """프롬프트에 넣을 금액 표기 (app.py의 _won과 같은 규칙)."""
    if v is None:
        return "정보 없음"
    v = float(v)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        jo = int(a // 1e12)
        eok = round((a - jo * 1e12) / 1e8)
        if eok >= 10000:
            jo, eok = jo + 1, 0
        return f"{sign}{jo:,}조 원" if eok == 0 else f"{sign}{jo:,}조 {eok:,}억 원"
    if a >= 1e8:
        return f"{sign}{a / 1e8:,.0f}억 원"
    return f"{sign}{a:,.0f} 원"


def build_prompt(info: dict) -> str:
    """DART 조회 결과(dict)를 LLM에게 줄 텍스트로 변환."""
    ind = info.get("지표", {})
    pinfo = info.get("주가정보") or {}

    lines = [
        f"회사명: {info.get('회사명')}",
        f"기준: {info.get('연도')}년 {info.get('보고서')} (출처: 금융감독원 DART 전자공시)",
        "",
        "[실적]",
        f"- 매출액: {_won(ind.get('매출액'))}",
        f"- 영업이익: {_won(ind.get('영업이익'))}",
        f"- 당기순이익: {_won(ind.get('당기순이익'))}",
        f"- 영업이익률: {ind.get('영업이익률(%)', '정보 없음')}%",
        "",
        "[재무 건전성·효율]",
        f"- ROE(자기자본이익률): {ind.get('ROE(%)', '정보 없음')}%",
        f"- 부채비율: {ind.get('부채비율(%)', '정보 없음')}%",
        "",
        "[주당 지표]",
        f"- EPS(주당순이익): {ind.get('EPS(원)', '정보 없음')}원",
        f"- BPS(주당순자산): {ind.get('BPS(원)', '정보 없음')}원",
    ]

    if ind.get("PER") is not None:
        basis = f"{pinfo.get('price_date')} 종가" if pinfo.get("available") else "사용자 입력 주가"
        lines += [
            "",
            "[주가 대비 지표]",
            f"- 기준 주가: {info.get('적용주가', 0):,.0f}원 ({basis})",
            f"- PER: {ind.get('PER')}배",
            f"- PBR: {ind.get('PBR')}배",
        ]
    else:
        lines += ["", "[주가 대비 지표] 주가 정보가 없어 PER/PBR은 계산되지 않았습니다."]

    lines += [
        "",
        "위 숫자만 근거로, 투자 초보자가 이해할 수 있게 설명해주세요.",
        "주어지지 않은 정보(업종 평균, 경쟁사, 향후 전망)는 언급하지 마세요.",
    ]
    return "\n".join(lines)


def _cache_key(info: dict) -> str:
    """같은 회사·연도·지표 조합이면 같은 키 → 같은 해설 재사용 (과금 절약)."""
    ind = info.get("지표", {})
    material = {
        "corp": info.get("corp_code") or info.get("회사명"),
        "year": info.get("연도"),
        "reprt": info.get("보고서"),
        "model": MODEL,
        # 지표를 반올림해서 넣는다 — 주가가 조금 움직인 정도로는 재생성하지 않도록
        "ind": {
            k: (round(v, 1) if isinstance(v, (int, float)) else v)
            for k, v in sorted(ind.items())
        },
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def contains_forbidden(text: str) -> Optional[str]:
    """규제상 위험한 표현이 있으면 그 표현을 반환."""
    for word in _FORBIDDEN:
        if word in text:
            return word
    return None


def _call_claude(prompt: str) -> str:
    client = anthropic.Anthropic(api_key=config.get_key("ANTHROPIC_API_KEY", required=True))

    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        output_config={"effort": EFFORT},
        messages=[{"role": "user", "content": prompt}],
    )

    # 안전 분류기가 요청을 거절하면 서버가 자동으로 다른 모델로 넘겨준다.
    # (베타 기능이라, 거절당하면 베타 없이 한 번 더 시도)
    try:
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    except anthropic.BadRequestError:
        response = client.messages.create(**kwargs)

    # refusal 은 예외가 아니라 정상 응답(200)으로 오므로 먼저 확인해야 한다
    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None)
        raise LLMUnavailable(f"모델이 응답을 거절했습니다 (사유: {detail})")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        raise LLMUnavailable("모델이 빈 응답을 반환했습니다.")
    return text


def explain(info: dict, cache_get=None, cache_set=None) -> tuple[str, bool]:
    """
    지표 해설을 생성한다.

    cache_get(key) -> str | None,  cache_set(key, text, info) -> None
    을 넘기면 DB 캐시를 사용한다 (같은 지표 조합에 매번 과금되지 않도록).

    반환: (해설 텍스트, 캐시에서 나왔는지 여부)
    실패 시 LLMUnavailable 예외 → 호출하는 쪽에서 규칙 기반으로 fallback.
    """
    if not _HAS_SDK:
        raise LLMUnavailable(unavailable_reason())
    if not config.has_key("ANTHROPIC_API_KEY"):
        raise LLMUnavailable(unavailable_reason())

    key = _cache_key(info)
    if cache_get:
        cached = cache_get(key)
        if cached:
            return cached, True

    prompt = build_prompt(info)

    try:
        text = _call_claude(prompt)
    except LLMUnavailable:
        raise
    except anthropic.AuthenticationError:
        raise LLMUnavailable("ANTHROPIC_API_KEY가 올바르지 않습니다.")
    except anthropic.RateLimitError:
        raise LLMUnavailable("API 호출 한도를 초과했습니다. 잠시 후 다시 시도하세요.")
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"Claude API 오류 ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        raise LLMUnavailable("네트워크 오류로 Claude API에 연결하지 못했습니다.")

    # 프롬프트만 믿지 않고 결과를 한 번 더 검사 (규제 방어선 2중화)
    bad = contains_forbidden(text)
    if bad:
        raise LLMUnavailable(
            f"생성된 해설에 투자 권유로 읽힐 수 있는 표현('{bad}')이 있어 사용하지 않았습니다."
        )

    if cache_set:
        cache_set(key, text, info)
    return text, False


if __name__ == "__main__":
    import sys

    print(f"anthropic SDK : {'설치됨' if _HAS_SDK else '미설치'}")
    print(f"API 키        : {'있음' if config.has_key('ANTHROPIC_API_KEY') else '없음'}")
    print(f"사용 가능      : {is_available()}")
    if not is_available():
        print(f"\n사유: {unavailable_reason()}")

    # 프롬프트가 어떻게 만들어지는지는 키 없이도 확인할 수 있다
    if "--prompt" in sys.argv:
        import dart_client

        name = sys.argv[sys.argv.index("--prompt") + 1] if len(sys.argv) > 2 else "삼성전자"
        info = dart_client.get_stock_fundamentals(name)
        print("\n" + "=" * 60)
        print(build_prompt(info))
