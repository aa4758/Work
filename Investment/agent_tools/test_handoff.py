"""handoff.py 자체 검사: python SetUp/agent_tools/test_handoff.py"""
import json
import tempfile
from pathlib import Path

import handoff as h

with tempfile.TemporaryDirectory() as d:
    f = Path(d)
    (f / "00_screen.json").write_text(json.dumps({"시장국면": "강세", "후보": [
        {"ticker": "AAA", "sector": "IT"}, {"ticker": "BBB", "sector": "IT"},
        {"ticker": "CCC", "sector": "금융"}, {"ticker": "DDD", "sector": "에너지"}]}), encoding="utf-8")
    rep = lambda stem, rows, extra="": (f / f"{stem}.md").write_text(
        f"# x\n## 요약\n- {stem} 요약{extra}\n## 점수표\n| 티커 | 점수 | 근거 |\n|---|---|---|\n"
        + "".join(f"| {t} | {s:+d} | {stem}-{t} |\n" for t, s in rows.items()) + "## 상세\n| ZZZ | 2 | 무시 |\n", encoding="utf-8")
    rep("01_chart", {"AAA": 2, "BBB": 1, "CCC": -2, "DDD": 0, "GLD": 1, "TLT": 0, "IEF": -1, "SLV": 0})
    assert h.survivors(f) == ["AAA", "BBB", "DDD"]  # 차트 -2 는 1b 분석 제외
    rep("02_fundamental", {"AAA": 2, "BBB": 1, "DDD": 0})
    rep("03_news", {"AAA": 1, "BBB": -2, "DDD": 1})
    rep("04_sentiment", {"AAA": 1, "BBB": 1, "DDD": 0})
    rep("07_fx", {"AAA": 0, "BBB": 0, "DDD": 0})
    rep("09_macro", {"GLD": 2, "TLT": 0, "IEF": 0, "SLV": -1}, "\n- 시장 판단: 중립")

    rows = {r["ticker"]: r for r in h.prescore(f)}
    assert abs(rows["AAA"]["pre"] - (.5 + .4 + .2 + .15)) < 1e-9
    assert rows["BBB"]["veto"] == ["뉴스"] and not rows["BBB"]["debate"]      # 누구든 -2 → 토론 없이 탈락
    assert rows["CCC"]["skipped"] and not rows["CCC"]["debate"]
    assert not rows["DDD"]["debate"]                                          # 0.2 + 토론 최대 0.2 < 0.5
    assert rows["GLD"]["debate"] and abs(rows["GLD"]["pre"] - 1.3) < 1e-9     # 비주식: 차트30·시장50
    assert "## 상세" not in h.cmd_debate(f) and "ZZZ" not in h.cmd_debate(f)  # 점수표 밖 표는 무시
    assert "시장: " in h.summaries(f) or "**시장**" in h.summaries(f)

    # 토론: 반박 라운드의 최종 강도가 1차 강도를 덮어쓴다
    (f / "05_bull.md").write_text("## 강도표\n| AAA | 4 | a |\n| GLD | 2 | g |\n", encoding="utf-8")
    (f / "06_bear.md").write_text("## 강도표\n| AAA | 2 | a |\n| GLD | 2 | g |\n", encoding="utf-8")
    (f / "06_bear_rebuttal.md").write_text("## 반박\n| GLD | 4 | 금리 |\n", encoding="utf-8")
    res, bar = h.final(f)
    res = {r["ticker"]: r for r in res}
    assert bar == .5
    assert res["AAA"]["pass"] and abs(res["AAA"]["total"] - (1.25 + .1)) < 1e-9
    assert not res["GLD"]["pass"] and "반대 우세" in res["GLD"]["why"]
    assert not res["BBB"]["pass"] and "회피" in res["BBB"]["why"]

    # 시장 비우호면 기준 +1.0
    rep("09_macro", {"GLD": 2}, "\n- 시장 판단: **비우호**")
    assert h.final(f)[1] == 1.0
print("ok")
