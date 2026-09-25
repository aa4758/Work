"""
/pick-10 에이전트 간 핸드오프 요약기. 리포트 전문 대신 `## 요약`·`## 점수표`만 뽑아 다음 에이전트 프롬프트로 넘긴다.
점수 계산을 코드로 해서 결정권자·진행자가 수십 종목의 가중합을 직접 계산하지 않게 한다.

  python Investment/agent_tools/handoff.py <폴더> survivors  # 1a 뒤: 차트 -2(회피)가 아닌 주식 → 기업가치·뉴스·심리 대상
  python Investment/agent_tools/handoff.py <폴더> debate     # 1단계 뒤: 토론 대상 요약 → 10_handoff.md
  python Investment/agent_tools/handoff.py <폴더> final      # 토론 뒤: 최종 점수표 → 11_scores.md (결정권자 입력)

리포트 형식 (모든 에이전트 공통): `## 요약` (3줄 이내), `## 점수표` 의 `| 티커 | 점수 | 근거 |` 행.
토론 반박 파일(05b/06b)은 `| 티커 | 최종 강도 | 반박 |`.
최소 기준(.claude/agents/decision-maker.md): 가중점수 ≥ +0.5 (시장 비우호·약세장이면 +1.0), 어느 분석가도 -2 아님, 반대 우세 아님.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ASSETS = ["TLT", "IEF", "GLD", "SLV"]  # 비주식 자산 (Investment/PORTFOLIO.md)
STOCK_W = {"01_chart": .25, "02_fundamental": .20, "03_news": .20, "04_sentiment": .15, "07_fx": .10}
ASSET_W = {"01_chart": .30, "09_macro": .50}
DEBATE_W = {"stock": .10, "asset": .20}
NAMES = {"01_chart": "차트", "02_fundamental": "기업", "03_news": "뉴스", "04_sentiment": "심리", "07_fx": "환율", "09_macro": "시장"}
BAR, BAR_STRICT = 0.5, 1.0
ROW = re.compile(r"^\|\s*([A-Z0-9][A-Z0-9.\-]*)\s*\|\s*([+-]?\d+(?:\.\d+)?)\s*\|\s*(.*?)\s*\|?\s*$")


def section(text: str, title: str) -> list[str]:
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().startswith(title)
        elif inside and line.strip():
            out.append(line.rstrip())
    return out


def read(folder: Path, stem: str) -> str:
    f = folder / f"{stem}.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def table(text: str, title: str = "점수표") -> dict[str, tuple[float, str]]:
    return {m[1]: (float(m[2]), m[3]) for line in section(text, title) if (m := ROW.match(line))}


def candidates(folder: Path) -> tuple[list[str], dict[str, str]]:
    """(주식 티커, 티커→업종). 미국 모드는 00_screen.json, 아니면 차트 점수표의 종목."""
    screen = folder / "00_screen.json"
    if screen.exists():
        rows = json.loads(screen.read_text(encoding="utf-8")).get("후보", [])
        return [r["ticker"] for r in rows], {r["ticker"]: r.get("sector", "") for r in rows}
    return [t for t in table(read(folder, "01_chart")) if t not in ASSETS], {}


def survivors(folder: Path) -> list[str]:
    chart = table(read(folder, "01_chart"))
    return [t for t in candidates(folder)[0] if chart.get(t, (0,))[0] > -2]


def prescore(folder: Path) -> list[dict]:
    stocks, sectors = candidates(folder)
    tables = {stem: table(read(folder, stem)) for stem in NAMES}
    us = (folder / "00_screen.json").exists()
    rows = []
    for t in stocks + (ASSETS if us else []):
        kind = "asset" if t in ASSETS else "stock"
        weights = ASSET_W if kind == "asset" else STOCK_W
        s = {stem: tables[stem].get(t) for stem in weights}
        pre = sum(w * (s[stem][0] if s[stem] else 0) for stem, w in weights.items())
        veto = [NAMES[k] for k, v in s.items() if v and v[0] <= -2]
        skipped = kind == "stock" and s["01_chart"] and s["01_chart"][0] <= -2  # 차트 -2 는 1b 분석을 건너뛴다
        rows.append({"ticker": t, "kind": kind, "sector": "비주식" if kind == "asset" else sectors.get(t, ""),
                     "scores": {k: (v[0] if v else None) for k, v in s.items()},
                     "reasons": " / ".join(f"{NAMES[k]}: {v[1]}" for k, v in s.items() if v and v[1]),
                     "pre": pre, "veto": veto, "skipped": skipped,
                     # 토론에서 최대로 받아도(+2) 최소 기준에 못 미치면 토론하지 않는다 (결과가 바뀔 수 없으므로)
                     "debate": not veto and pre + DEBATE_W[kind] * 2 >= BAR})
    return rows


def fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:+g}"


def summaries(folder: Path) -> str:
    out = []
    for stem, name in NAMES.items():
        lines = section(read(folder, stem), "요약")[:3]
        if lines:
            out.append(f"**{name}**: " + " ".join(l.lstrip("-* ") for l in lines))
    return "\n".join(out)


def cmd_debate(folder: Path) -> str:
    rows = prescore(folder)
    keep = [r for r in rows if r["debate"]]
    drop = [r for r in rows if not r["debate"]]
    lines = [f"# 토론 핸드오프 ({folder.name}) — 토론 대상 {len(keep)} / 전체 {len(rows)}", "",
             "## 분석가 요약", summaries(folder), "",
             "## 토론 대상 (사전점수 = 토론 제외 가중합)",
             "| 티커 | 업종 | " + " | ".join(NAMES[k] for k in STOCK_W) + " | 시장 | 사전점수 | 근거 |",
             "|---" * (len(STOCK_W) + 5) + "|"]
    for r in sorted(keep, key=lambda r: -r["pre"]):
        sc = r["scores"]
        lines.append(f"| {r['ticker']} | {r['sector']} | " + " | ".join(fmt(sc.get(k)) for k in STOCK_W)
                     + f" | {fmt(sc.get('09_macro'))} | {r['pre']:+.2f} | {r['reasons']} |")
    why = lambda r: "회피: " + "·".join(r["veto"]) if r["veto"] else f"사전점수 {r['pre']:+.2f}"
    lines += ["", "## 토론 제외 (토론 결과와 무관하게 최소 기준 미달)",
              ", ".join(f"{r['ticker']}({why(r)})" for r in drop) or "없음"]
    return "\n".join(lines)


def macro_view(folder: Path) -> str | None:
    m = re.search(r"시장 판단\s*[:=]?\s*\**\s*(우호|중립|비우호)", "\n".join(section(read(folder, "09_macro"), "요약")))
    return m[1] if m else None


def regime(folder: Path) -> str | None:
    screen = folder / "00_screen.json"
    return json.loads(screen.read_text(encoding="utf-8")).get("시장국면") if screen.exists() else None


def strength(folder: Path, stem: str) -> dict[str, float]:
    """1차 변론 강도표를 반박 라운드의 최종 강도로 덮어쓴다. 1~5."""
    s = {t: v[0] for t, v in table(read(folder, stem), "강도표").items()}
    s.update({t: v[0] for t, v in table(read(folder, stem + "_rebuttal"), "반박").items()})
    return s


def final(folder: Path) -> tuple[list[dict], float]:
    view, reg = macro_view(folder), regime(folder)
    bar = BAR_STRICT if view == "비우호" or (reg or "").startswith("약세") else BAR
    bull, bear = strength(folder, "05_bull"), strength(folder, "06_bear")
    rows = prescore(folder)
    for r in rows:
        b, a = bull.get(r["ticker"]), bear.get(r["ticker"])
        r["bull"], r["bear"] = b, a
        r["debate_score"] = max(-2.0, min(2.0, ((b or 3) - (a or 3)) / 2)) if r["debate"] else None
        r["total"] = r["pre"] + DEBATE_W[r["kind"]] * (r["debate_score"] or 0)
        why = ([f"회피({'·'.join(r['veto'])})"] if r["veto"] else []) \
            + ([f"점수 {r['total']:+.2f} < {bar:g}"] if r["total"] < bar else []) \
            + (["반대 우세"] if r["debate"] and (a or 3) > (b or 3) else [])
        r["pass"], r["why"] = not why, ", ".join(why)
    return sorted(rows, key=lambda r: (not r["pass"], -r["total"])), bar


def cmd_final(folder: Path) -> str:
    rows, bar = final(folder)
    lines = [f"# 최종 점수표 ({folder.name})", "",
             f"- 시장 판단: {macro_view(folder) or '-'} · 시장국면: {regime(folder) or '-'} → 최소 기준 **{bar:+g}**",
             f"- 최소 기준 통과 {sum(r['pass'] for r in rows)} / {len(rows)} (통과 종목만 편입 후보, 10개 미만이면 나머지는 현금 슬롯)",
             "", "## 분석가 요약", summaries(folder), "",
             "## 점수표 (가중: 주식 차트25·기업20·뉴스20·심리15·환율10·토론10 / 비주식 차트30·시장50·토론20)",
             "| 티커 | 업종 | " + " | ".join(NAMES[k] for k in STOCK_W) + " | 시장 | 찬성 | 반대 | 토론 | 가중점수 | 결과 | 근거 |",
             "|---" * (len(STOCK_W) + 10) + "|"]
    for r in rows:
        sc = r["scores"]
        lines.append(f"| {r['ticker']} | {r['sector']} | " + " | ".join(fmt(sc.get(k)) for k in STOCK_W)
                     + f" | {fmt(sc.get('09_macro'))} | {fmt(r['bull'])} | {fmt(r['bear'])} | {fmt(r['debate_score'])}"
                     + f" | {r['total']:+.2f} | {'통과' if r['pass'] else '미달: ' + r['why']} | {r['reasons'] if r['pass'] else ''} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    folder, cmd = Path(argv[0]), argv[1]
    if cmd == "survivors":
        s = survivors(folder)
        print(f"# 1b 분석 대상 {len(s)} / {len(candidates(folder)[0])} (차트 -2 회피 제외)\n" + " ".join(s))
    elif cmd in ("debate", "final"):
        text = cmd_debate(folder) if cmd == "debate" else cmd_final(folder)
        (folder / ("10_handoff.md" if cmd == "debate" else "11_scores.md")).write_text(text + "\n", encoding="utf-8")
        print(text)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
