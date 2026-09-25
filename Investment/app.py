"""
미국 주식 10종목 투자 도우미
실행: Investment 폴더에서 `streamlit run app.py`

  - 투자팀(9 에이전트)을 `claude -p "/pick-10 US"` 로 돌려 reports/<날짜>-US/picks.json 을 만든다
  - 최신 picks.json 의 추천 종목(최대 10, 미달 시 현금 슬롯)과 원→달러 환전 추천을 보여준다
  - 시드머니를 추천 점수·자산 위험에 따라 배분한다 (PORTFOLIO.md)
  - 종목별 매수일·매수금액·매도일을 저장하고, 손익과 자산별 손절/익절 신호를 계산한다
  - 매수한 종목은 매도(매도일 입력 또는 손절·익절 도달) 전까지 새 분석과 상관없이 보유로 남는다
"""
import json
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from portfolio import STOP_PCT, TAKE_PCT, allocate, cash_split, evaluate, risk_pct, rule

ROOT = Path(__file__).resolve().parent.parent  # 에이전트·스킬이 있는 저장소 루트
REPORTS = ROOT / "reports"
POSITIONS = Path(__file__).resolve().parent / "data" / "positions.json"  # 개인 데이터: .gitignore 의 **/data/

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent_tools"))
import team_run  # noqa: E402  투자팀 실행·사용 한도 후 자동 이어서 실행
from team_run import run_folder  # noqa: E402  리포트 폴더 = 마지막 미국 장 마감일
import handoff  # noqa: E402  에이전트 간 핸드오프 요약 (분석 대상 종목 계산에 같은 규칙을 쓴다)
import market_data  # noqa: E402  에이전트와 같은 시세 소스(Yahoo)를 쓴다

st.set_page_config(page_title="미장 10종목", page_icon="📈", layout="wide")


@st.cache_data(ttl=600)
def ohlcv(ticker: str, days: int) -> pd.DataFrame | None:
    return market_data.load_ohlcv(ticker, days)[0]


def latest_picks() -> tuple[dict | None, Path | None]:
    files = sorted(REPORTS.glob("*-US/picks.json"))
    if not files:
        return None, None
    return json.loads(files[-1].read_text(encoding="utf-8")), files[-1].parent


def load_positions() -> list[dict]:
    """[{ticker, name, buy_date, amount, sell_date?}] — 같은 종목을 팔고 다시 사도 기록이 따로 남도록 리스트."""
    data = json.loads(POSITIONS.read_text(encoding="utf-8")) if POSITIONS.exists() else []
    return [{"ticker": t, **p} for t, p in data.items()] if isinstance(data, dict) else data  # 예전 {티커: …} 형식


def evaluate_positions(positions: list[dict]) -> list[tuple[dict, dict | None]]:
    out = []
    for pos in positions:
        t = pos["ticker"]
        df = ohlcv(t, max(60, (date.today() - pd.to_datetime(pos["buy_date"]).date()).days + 40))
        r = evaluate(df, pos["buy_date"], pos["amount"], *(rule(t) or (None, None)),
                     sell_date=pos.get("sell_date")) if df is not None else None
        out.append((pos, r))
    return out


@st.cache_resource
def job() -> dict:
    """Streamlit 재실행 사이에 투자팀 프로세스를 유지한다."""
    return {}


# (리포트, 이름, 에이전트, 분석 대상) — 대상: all = 후보 + 비주식 자산, survivors = 차트 -2 를 뺀 주식, None = 종목별 작업 아님
TEAM = [("01_chart", "차트", "chart-analyst", "all"), ("02_fundamental", "기업가치", "fundamental-analyst", "survivors"),
        ("03_news", "뉴스", "news-analyst", "survivors"), ("04_sentiment", "심리", "sentiment-analyst", "survivors"),
        ("07_fx", "환율", "fx-analyst", None), ("09_macro", "미국 시장", "macro-analyst", None),
        ("05_bull", "찬성론", "bull-advocate", None), ("06_bear", "반대론", "bear-advocate", None),
        ("08_decision", "결정권자", "decision-maker", None)]
SESSIONS = Path.home() / ".claude" / "projects" / re.sub(r"[:\\/]", "-", str(ROOT))  # claude 가 대화 기록을 남기는 곳


def touched_tickers(tickers: list[str], folder: Path) -> dict[str, set]:
    """이번 실행(폴더가 생긴 뒤)의 /pick-10 세션 에이전트별로, 검색·도구 호출에 등장한 후보 티커 (= 분석을 시작한 종목, 추정)."""
    since = folder.stat().st_ctime
    sessions = [f for f in SESSIONS.glob("*.jsonl")
                if f.stat().st_mtime >= since
                and "/pick-10 US" in f.open(encoding="utf-8", errors="ignore").read(2000)]
    if not sessions:
        return {}
    latest = max(sessions, key=lambda f: f.stat().st_mtime).with_suffix("") / "subagents"
    pat = re.compile(r"(?<![A-Za-z0-9.])(" + "|".join(map(re.escape, sorted(tickers, key=len, reverse=True))) + r")(?![A-Za-z0-9:])")
    out: dict[str, set] = {}
    for meta in latest.glob("*.meta.json"):
        agent = json.loads(meta.read_text(encoding="utf-8")).get("agentType")
        out.setdefault(agent, set())  # 기록이 있으면 시작한 것
        for line in meta.with_name(meta.name.replace(".meta.json", ".jsonl")).open(encoding="utf-8", errors="ignore"):
            if '"tool_use"' not in line:
                continue
            for c in json.loads(line).get("message", {}).get("content", []):
                # 파일 읽기·쓰기는 제외 (리포트 전체에 모든 티커가 나온다) — 검색어·명령만 본다
                if c.get("type") == "tool_use" and c.get("name") not in ("Read", "Write", "Edit"):
                    out[agent].update(pat.findall(json.dumps(c.get("input"))))
    return out


def team_progress(folder: Path) -> list[tuple[str, str, int | None, int | None]] | None:
    """[(이름, 상태 ✅/⏳/⬜, 분석한 종목 수, 대상 종목 수)]. 끝난 리포트는 파일로, 진행 중인 것은 대화 기록으로 판단.
    찬반 토론은 종목별 검색을 거의 하지 않으므로 종목 수 대신 단계(1차 변론 → 반박)로 보여준다."""
    if not (folder / "00_screen.json").exists():
        return None
    stocks = handoff.candidates(folder)[0]
    scope = {"all": stocks + handoff.ASSETS, "survivors": handoff.survivors(folder), None: []}
    seen = touched_tickers(scope["all"], folder)
    rows = []
    for stem, label, agent, target in TEAM:
        f, total = folder / f"{stem}.md", scope[target]
        done = f.exists() and f.stat().st_size > 0
        if done and agent in ("bull-advocate", "bear-advocate") and not (folder / f"{stem}_rebuttal.md").exists():
            done, label = False, f"{label} (1차 변론 완료 · 반박 대기)"  # 반박까지 끝나야 완료
        n = len(total) if done else len(seen.get(agent, set()) & set(total))
        state = "✅" if done else "⏳" if agent in seen or f.exists() else "⬜"
        rows.append((label, state, n if total else None, len(total) or None))
    return rows


# ----------------------------------------------------------------------
if "opened" not in st.session_state:  # 앱을 열 때(새 브라우저 세션)마다 시세를 새로 받는다
    ohlcv.clear()
    st.session_state.opened = time.strftime("%Y-%m-%d %H:%M")

st.title("📈 미국 주식 10종목 — 투자팀 추천")
st.caption(f"전략: 최대 10종목, 주식은 -{STOP_PCT}% 손절 / +{TAKE_PCT}% 익절 (안전자산은 자산별 규칙). "
           "AI 에이전트의 분석 보조 자료이며 투자 권유가 아닙니다. 최종 판단과 책임은 투자자 본인에게 있습니다.")

# --- 투자팀 실행 -------------------------------------------------------
with st.sidebar:
    st.header("내 투자금")
    seed = st.number_input("시드머니 (원)", min_value=0, value=10_000_000, step=1_000_000, format="%d")
    st.header("투자팀 실행")
    st.write("9명의 에이전트(분석가 6 · 찬반 토론 2 · 결정권자)가 미국 시장에서 최대 10종목을 고릅니다. "
             "수십 분 걸리고 Claude 사용량이 많이 듭니다.")
    running = team_run.running(job())
    waiting = job().get("resume_at")  # 사용 한도가 다시 차기를 기다리는 중
    busy = running or bool(waiting)
    candidates = st.number_input("분석할 후보 종목 수", min_value=15, max_value=60, value=30, step=5,
                                 disabled=busy, help="많을수록 넓게 보지만 시간과 사용량이 비례해 늘어납니다.")
    folder_now = job()["folder"] if busy else run_folder()  # 실행 중에는 장 마감이 지나도 그 실행의 폴더
    if not shutil.which("claude"):
        st.error("`claude` CLI를 찾을 수 없습니다.")
    elif st.button("🚀 투자팀 가동", disabled=busy, type="primary"):
        team_run.launch(job(), int(candidates))
        st.rerun()
    elif folder_now.exists() and not (folder_now / "picks.json").exists() and not busy:
        # 같은 미국 장 마감 기준으로 시작했다가 멈춘 실행 — 새 종가가 나오면 시점이 섞이므로 이어 가지 않는다
        done = sorted(f.stem for f in folder_now.glob("0*.md"))
        st.warning(f"{folder_now.name} 실행이 중간에 멈췄습니다. 끝난 리포트: " + (", ".join(done) or "없음"))
        if st.button("▶ 이어서 실행", help="끝난 분석은 건너뛰고 남은 단계만 실행합니다. 후보는 처음 실행 그대로입니다."):
            team_run.launch(job(), int(candidates), resume=True)
            st.rerun()

    @st.fragment(run_every=20 if busy else None)  # 실행·대기 중에는 20초마다 상태와 진행 상황을 새로 그린다
    def status():
        running = team_run.running(job())
        waiting = job().get("resume_at")
        if (running or bool(waiting)) != busy:  # 시작·종료·대기·재개가 바뀌면 버튼 상태까지 앱 전체를 다시 그린다
            st.rerun()
        if note := job().get("note"):
            st.info(note)
        if running:
            st.info(f"실행 중… {int((time.time() - job()['started']) // 60)}분 경과"
                    + (f" (자동 이어서 실행 {job()['attempts']}/{team_run.MAX_AUTO}회째)" if job().get("attempts") else ""))
            st.button("상태 새로고침")
        elif waiting:
            st.warning(f"⏸ 사용 한도에 도달했습니다. {waiting.astimezone():%m/%d %H:%M}에 자동으로 이어서 실행합니다 "
                       f"({job()['attempts'] + 1}/{team_run.MAX_AUTO}회). 앱과 PC를 켜 두세요.")
            if st.button("자동 이어서 실행 취소"):
                job()["cancel"] = True
                job()["resume_at"] = None
                st.rerun()
            st.button("상태 새로고침")
        elif job().get("proc") is not None:
            code = job()["proc"].returncode
            (st.success if code == 0 else st.error)(f"종료 (코드 {code}). 로그: {team_run.LOG}")

        if folder_now.exists() and (busy or not (folder_now / "picks.json").exists()):
            if prog := team_progress(folder_now):
                st.subheader("📊 진행 상황")
                k = sum(state == "✅" for _, state, _, _ in prog)
                st.progress(k / len(prog), text=f"전체 {k}/{len(prog)}명 완료")
                for label, state, n, total in prog:
                    if total:
                        st.progress(n / total, text=f"{state} {label} {n}/{total}")
                    else:
                        st.write(f"{state} {label}")
                st.caption("✅ 완료 · ⏳ 진행 중 · ⬜ 대기. 진행 중 종목 수는 검색 기록으로 추정한 값입니다.")
            else:
                st.caption("📊 후보군 선정 중…")

    status()


picks, folder = latest_picks()
saved = load_positions()
evals = evaluate_positions(saved)
held = [(p, r) for p, r in evals if r and r["신호"] == "보유"]  # 매도·손절·익절 전 = 보유 중
held_tickers = {p["ticker"] for p, _ in held}

# --- 미국 시장 전망 ----------------------------------------------------
if picks and picks.get("macro"):
    m = picks["macro"]
    icon = {"우호": "🟢", "중립": "🟡", "비우호": "🔴"}.get(m.get("view"), "⚪")
    st.subheader(f"🇺🇸 미국 시장 전망 (1~3개월): {icon} {m.get('view', '-')} · 확신도 {m.get('confidence', '-')}")
    st.write(m.get("summary", ""))
    macro_md = folder / "09_macro.md"
    if macro_md.exists():
        with st.expander("미국 시장 분석가 리포트 전문"):
            st.markdown(macro_md.read_text(encoding="utf-8"))

# --- 환전 추천 ---------------------------------------------------------
st.subheader("💱 원화 → 달러 환전")
fx = ohlcv("KRW=X", 60)
usdkrw = float(fx["close"].iloc[-1]) if fx is not None else None
c1, c2, c3 = st.columns(3)
if fx is not None:
    c1.metric("현재 USD/KRW", f"{usdkrw:,.2f}원",
              f"{(usdkrw / fx['close'].iloc[-22] - 1) * 100:+.2f}% (1개월)", delta_color="inverse")
    c2.metric("20일 평균", f"{fx['close'].tail(20).mean():,.2f}원")
else:
    c1.warning("환율 조회 실패")
if picks and picks.get("fx"):
    action = picks["fx"].get("action", "-")
    icon = {"환전 추천": "🟢", "분할 환전": "🟡", "환전 보류": "🔴"}.get(action, "⚪")
    c3.metric(f"투자팀 판단 ({picks['date']})", f"{icon} {action}")
    st.write(picks["fx"].get("reason", ""))
else:
    c3.metric("투자팀 판단", "-")

def rule_text(ticker: str) -> str:
    r = rule(ticker)
    return f"-{r[0]:g}% / +{r[1]:g}%" if r else "보유 (손절·익절 없음)"


# --- 추천 종목 (최대 10) ------------------------------------------------
n_picks = len((picks or {}).get("picks", []))
st.subheader(f"🏆 추천 종목 ({n_picks}/10)" if picks else "🏆 추천 종목")
if not picks:
    st.info("아직 추천 결과가 없습니다. 왼쪽에서 '투자팀 가동'을 누르세요.")
else:
    st.caption(f"결정일 {picks['date']} · 리포트: {folder}")
    if n_picks < 10:
        # 기준 미달 종목으로 채우지 않는다 — 빈자리는 현금
        st.warning(f"💵 현금 슬롯 {10 - n_picks}개 — {picks.get('cash_reason') or '최소 기준을 통과한 종목이 부족합니다.'}")
    if n_picks == 0:
        st.info("이번에는 매수를 권하지 않습니다.")
    table = pd.DataFrame(picks["picks"]).rename(columns={
        "ticker": "티커", "name": "종목명", "sector": "업종", "price": "기준가($)",
        "stop": "손절가", "take": "익절가",
        "score": "종합점수", "reason": "매수 이유", "risk": "최대 리스크"})
    if n_picks:
        table.insert(3, "청산 규칙", [rule_text(t) for t in table["티커"]])
    st.dataframe(table, hide_index=True, width="stretch")
    if picks.get("reserves"):
        st.caption("예비 후보: " + ", ".join(f"{r['ticker']} {r.get('name', '')}" for r in picks["reserves"]))
    screen = folder / "00_screen.json"
    if screen.exists():
        s = json.loads(screen.read_text(encoding="utf-8"))
        with st.expander(f"후보군 선정 과정 — 시장국면: {s.get('시장국면', '-')}"):
            st.write(" → ".join(f"{k} {v}" for k, v in s.get("단계별_생존", {}).items()))
            st.dataframe(pd.DataFrame(s.get("후보", [])), hide_index=True, width="stretch")
    decision = folder / "08_decision.md"
    if decision.exists():
        with st.expander("결정권자 리포트 전문"):
            st.markdown(decision.read_text(encoding="utf-8"))
with st.expander("📚 후보군 선정 규칙과 근거 논문"):
    st.markdown((Path(__file__).parent / "SCREENING.md").read_text(encoding="utf-8"))

# --- 시드 배분 ----------------------------------------------------------
alloc = {}  # 티커 → 달러 금액. 매수 설정의 기본 매수금액으로 쓴다
if (n_picks or held) and seed and usdkrw:
    st.subheader(f"🧮 시드 배분 ({seed:,.0f}원)")
    # 보유 중인 종목은 그대로 두고(현재 평가액), 남은 돈으로만 새 추천 종목을 배분한다
    rows = []
    for p, r in held:
        value = (p["amount"] + r["손익($)"]) * usdkrw
        loss = r["수량"] * (r["기준가"] - r["손절가"]) * usdkrw if r["손절가"] else value * risk_pct(p["ticker"], ohlcv(p["ticker"], 300)) / 100
        rows.append({"구분": "보유 중", "티커": p["ticker"], "종목명": p.get("name", ""), "청산 규칙": rule_text(p["ticker"]),
                     "비중(%)": value / seed * 100, "금액(원)": value, "금액($)": value / usdkrw,
                     "현재가($)": r["기준가"], "수량(주)": r["수량"], "최대 손실(원)": max(loss, 0.0)})
    held_krw = sum(r["금액(원)"] for r in rows)
    items = [p for p in (picks or {}).get("picks", []) if p["ticker"] not in held_tickers]
    risks = [risk_pct(p["ticker"], ohlcv(p["ticker"], 300) if rule(p["ticker"]) is None else None) for p in items]
    prices = [float(df["close"].iloc[-1]) if (df := ohlcv(p["ticker"], 300)) is not None else None for p in items]
    weights = allocate([float(p.get("score") or 0) for p in items], risks, budget=max(0.0, 1 - held_krw / seed))
    new = [{"구분": "신규 추천", "티커": p["ticker"], "종목명": p.get("name", ""), "청산 규칙": rule_text(p["ticker"]),
            "종합점수": p.get("score"), "최악 손실(%)": r, "비중(%)": w * 100,
            "금액(원)": seed * w, "금액($)": seed * w / usdkrw,
            "현재가($)": px, "수량(주)": int(seed * w / usdkrw // px) if px else None, "최대 손실(원)": seed * w * r / 100}
           for p, r, w, px in zip(items, risks, weights, prices)]
    rows += new
    alloc = {r["티커"]: round(r["금액($)"], 2) for r in new}
    cash = max(0.0, seed - held_krw - seed * sum(weights))
    krw_share, usd_share = cash_split(((picks or {}).get("fx") or {}).get("action"))
    for label, share in (("원화 현금", krw_share), ("달러 현금 (BIL·예수금)", usd_share)):
        if share and cash > 1:
            rows.append({"구분": "현금", "티커": "-", "종목명": label, "청산 규칙": "보유", "비중(%)": cash * share / seed * 100,
                         "금액(원)": cash * share, "금액($)": cash * share / usdkrw, "최대 손실(원)": 0.0})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
        "최악 손실(%)": st.column_config.NumberColumn(format="%.1f%%"),
        "비중(%)": st.column_config.NumberColumn(format="%.1f%%"),
        "금액(원)": st.column_config.NumberColumn(format="%.0f원"),
        "금액($)": st.column_config.NumberColumn(format="$%.2f"),
        "현재가($)": st.column_config.NumberColumn(format="$%.2f"),
        "수량(주)": st.column_config.NumberColumn(format="%.4g", help="신규는 배분 금액으로 살 수 있는 온주 수 (1주 미만 나머지는 현금)"),
        "최대 손실(원)": st.column_config.NumberColumn(format="%.0f원"),
    })
    a1, a2, a3 = st.columns(3)
    a1.metric("투자 (보유 + 신규)", f"{seed - cash:,.0f}원", f"{(1 - cash / seed) * 100:.1f}%", delta_color="off")
    a2.metric("현금", f"{cash:,.0f}원", f"{cash / seed * 100:.1f}%", delta_color="off")
    a3.metric("전부 손절 시 최대 손실", f"{sum(r['최대 손실(원)'] for r in rows):,.0f}원")
    st.caption("보유 중인 종목은 현재 평가액 그대로 두고 남은 시드로만 새 추천 종목을 배분합니다. "
               "추천 점수가 높을수록 더 싣고(0.5~1.5배), 한 종목에서 잃을 수 있는 최대 금액이 비슷하도록 "
               "손절폭이 넓거나 없는 자산은 줄입니다. 갭하락 시 손실은 더 커질 수 있습니다. 매수 설정의 기본 금액으로 들어갑니다. "
               "수량은 최근 종가 기준 온주이고, 소수점 거래를 쓰면 금액($)대로 사면 됩니다.")
with st.expander("📚 배분 방법과 안전자산(국채·금·은·달러) 청산 규칙 분석"):
    st.markdown((Path(__file__).parent / "PORTFOLIO.md").read_text(encoding="utf-8"))

# --- 매수·매도 설정 -----------------------------------------------------
st.subheader("🛒 매수·매도 설정")
st.caption("매수일 종가(휴장일이면 다음 거래일 종가)에 매수금액만큼 샀다고 보고 계산합니다. 소수점 수량 기준. "
           "팔았으면 매도일을 입력하세요 — 매도일 종가로 손익을 확정합니다. 매도 전까지는 투자팀을 다시 돌려도 보유로 남습니다.")
if msg := st.session_state.pop("saved_msg", None):
    st.success(msg)
# 저장된 기록 전부 + 아직 보유하지 않은 새 추천 종목(매수일을 넣어야 저장된다)
fresh = [p for p in (picks or {}).get("picks", []) if p["ticker"] not in held_tickers]
editor = pd.DataFrame({
    "티커": [p["ticker"] for p in saved] + [p["ticker"] for p in fresh],
    "종목명": [p.get("name", "") for p in saved] + [p.get("name", "") for p in fresh],
    # datetime 타입으로 고정해야 빈 표(추천·보유 없음)에서도 DateColumn 이 동작한다
    "매수일": pd.to_datetime([p["buy_date"] for p in saved] + [None] * len(fresh)),
    "매수금액($)": [p["amount"] for p in saved] + [alloc.get(p["ticker"], 0.0) for p in fresh],
    "매도일": pd.to_datetime([p.get("sell_date") for p in saved] + [None] * len(fresh)),
})
with st.form("positions"):
    edited = st.data_editor(
        editor, hide_index=True, num_rows="dynamic", width="stretch",
        column_config={
            "매수일": st.column_config.DateColumn(max_value=date.today(), format="YYYY-MM-DD"),
            "매수금액($)": st.column_config.NumberColumn(min_value=0.0, step=10.0, format="$%.2f"),
            "매도일": st.column_config.DateColumn(max_value=date.today(), format="YYYY-MM-DD", help="비워 두면 보유 중"),
        })
    if st.form_submit_button("저장"):
        data = [{"ticker": str(r["티커"]).strip().upper(), "name": r["종목명"] or "",
                 "buy_date": str(pd.Timestamp(r["매수일"]).date()), "amount": float(r["매수금액($)"] or 0),
                 **({"sell_date": str(pd.Timestamp(r["매도일"]).date())} if pd.notna(r["매도일"]) else {})}
                for _, r in edited.dropna(subset=["티커", "매수일"]).iterrows()
                if str(r["티커"]).strip() and (r["매수금액($)"] or 0) > 0]
        POSITIONS.parent.mkdir(exist_ok=True)
        POSITIONS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        st.session_state.saved_msg = f"{len(data)}건 저장"
        st.rerun()  # 보유 목록이 바뀌었으니 배분부터 다시 계산

# --- 손익 현황 ----------------------------------------------------------
st.subheader("💰 손익 현황")
st.caption(f"시세 기준: 앱을 연 시각 {st.session_state.opened} (앱을 열 때마다 새로 조회)")
recommended = set([p["ticker"] for p in (picks or {}).get("picks", [])])
results = []
for pos, r in evals:
    if r is None:
        st.warning(f"{pos['ticker']}: 시세를 가져올 수 없습니다 (티커·매수일·매도일 확인)")
        continue
    results.append({"티커": pos["ticker"], "종목명": pos.get("name", ""), "매수금액($)": pos["amount"], **r,
                    "최신 추천": "✅" if pos["ticker"] in recommended else "빠짐"})

if not results:
    st.info("매수일과 매수금액을 입력하고 저장하면 손익이 표시됩니다.")
else:
    res = pd.DataFrame(results)
    is_open = res["신호"] == "보유"
    res["신호"] = res["신호"].map({"손절": "🔴 손절 도달", "익절": "🟢 익절 도달", "보유": "⚪ 보유 중", "매도": "🔵 매도"})
    if usdkrw:
        res["손익(원)"] = res["손익($)"] * usdkrw
    st.dataframe(res.sort_values("신호", key=lambda s: s != "⚪ 보유 중", kind="stable"),
                 hide_index=True, width="stretch", column_config={
        "매수금액($)": st.column_config.NumberColumn(format="$%.2f"),
        "매수가": st.column_config.NumberColumn(format="$%.2f"),
        "수량": st.column_config.NumberColumn(format="%.4f"),
        "손절가": st.column_config.NumberColumn(format="$%.2f"),
        "익절가": st.column_config.NumberColumn(format="$%.2f"),
        "기준가": st.column_config.NumberColumn("기준가(현재/청산)", format="$%.2f"),
        "손익($)": st.column_config.NumberColumn(format="$%+.2f"),
        "수익률(%)": st.column_config.NumberColumn(format="%+.2f%%"),
        "손익(원)": st.column_config.NumberColumn(format="%+.0f원"),
    })
    invested, unreal = res.loc[is_open, "매수금액($)"].sum(), res.loc[is_open, "손익($)"].sum()
    realized = res.loc[~is_open, "손익($)"].sum()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("보유 중 매수금액", f"${invested:,.2f}")
    m2.metric("평가 손익 (보유 중)", f"${unreal:+,.2f}", f"{unreal / invested * 100:+.2f}%" if invested else None)
    m3.metric("실현 손익 (청산)", f"${realized:+,.2f}")
    if usdkrw:
        m4.metric("총 손익(원, 현재 환율)", f"{(unreal + realized) * usdkrw:+,.0f}원")
    st.caption("손절/익절에 도달한 종목은 그 가격에 매도했다고 보고 손익을 확정합니다. "
               "'최신 추천: 빠짐'인 보유형 자산(국채 등)은 매도를 검토하세요 (PORTFOLIO.md). "
               "원화 손익은 현재 환율로 환산한 값이며 환차익·환차손은 반영하지 않습니다.")
