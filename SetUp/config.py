"""
API 키 / 설정 로딩 모듈
------------------------
계획서 원칙: "API 키, 시크릿 값은 코드에 하드코딩 금지, 반드시 .env에서 로드"

키를 쓰는 곳이 DART 하나에서 여러 개(DART, KIS, Anthropic)로 늘어나서
탐색 규칙을 이 파일 한 곳에 모았다.

찾는 순서 (먼저 찾은 것 사용):
  1. 환경변수
  2. SetUp/*.env
  3. work/*.env
  4. work/Security/*.env       <- 키 파일을 모아두는 곳
  5. 현재 작업 디렉터리

파일 형식은 표준 .env 와 같다 (`KEY = value`, 앞뒤 공백/따옴표 허용, # 주석 무시).
python-dotenv가 설치돼 있으면 그것을 쓰고, 없으면 내장 파서로 동작한다.
(의존성 하나 때문에 앱이 안 켜지는 일이 없도록)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

try:
    from dotenv import dotenv_values  # python-dotenv
    _HAS_DOTENV = True
except Exception:  # noqa: BLE001
    dotenv_values = None
    _HAS_DOTENV = False

_HERE = Path(__file__).parent

# .env 파일을 찾을 디렉터리 (앞쪽이 우선)
_SEARCH_DIRS = [
    _HERE,
    _HERE.parent,
    _HERE.parent / "Security",
    Path.cwd(),
    Path.cwd() / "Security",
]

_cache: dict[str, Optional[str]] = {}


def _parse_env_file(path: Path) -> dict[str, str]:
    """python-dotenv가 있으면 그걸로, 없으면 최소 파서로."""
    if _HAS_DOTENV:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}

    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_key(name: str, required: bool = False) -> Optional[str]:
    """
    API 키 하나를 가져온다.
    `name`이 'DART_API_KEY'면 환경변수 DART_API_KEY 또는
    DART_API_KEY.env / .env 파일 안의 DART_API_KEY 항목을 찾는다.
    """
    if name in _cache:
        value = _cache[name]
    else:
        value = os.environ.get(name) or None

        if not value:
            for directory in _SEARCH_DIRS:
                if not directory.is_dir():
                    continue
                # 키 이름과 같은 파일(DART_API_KEY.env)을 먼저, 그다음 일반 .env
                for filename in (f"{name}.env", ".env", "keys.env"):
                    path = directory / filename
                    if not path.exists():
                        continue
                    found = _parse_env_file(path).get(name)
                    if found:
                        value = found
                        break
                if value:
                    break

        _cache[name] = value

    if required and not value:
        raise RuntimeError(
            f"{name} 를 찾을 수 없습니다. 환경변수로 설정하거나, "
            f"다음 위치 중 한 곳에 '{name}.env' 또는 '.env' 파일을 만들고 "
            f"`{name} = 값` 형식으로 저장하세요:\n  "
            + "\n  ".join(str(d) for d in _SEARCH_DIRS if d.is_dir())
        )
    return value


def has_key(name: str) -> bool:
    """키가 있는지만 확인 (화면에서 기능 on/off 판단용)."""
    return bool(get_key(name))


def clear_cache() -> None:
    """키 파일을 새로 넣은 뒤 다시 읽고 싶을 때."""
    _cache.clear()


if __name__ == "__main__":
    print("탐색 경로:")
    for d in _SEARCH_DIRS:
        print(f"  {'O' if d.is_dir() else 'X'}  {d}")
    print(f"\npython-dotenv: {'설치됨' if _HAS_DOTENV else '미설치 (내장 파서 사용)'}\n")
    print("키 상태:")
    for key_name in ("DART_API_KEY", "ANTHROPIC_API_KEY", "KIS_APP_KEY", "KIS_APP_SECRET"):
        value = get_key(key_name)
        state = f"설정됨 ({value[:6]}...)" if value else "없음"
        print(f"  {key_name:<20} {state}")
