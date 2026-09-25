"""
투자팀(/pick-10 US) 실행과 사용 한도 후 자동 이어서 실행.
앱 서버 프로세스 안의 스레드로 돌기 때문에 브라우저를 닫아도 계속된다 (서버·PC가 켜져 있어야 함).

리포트 폴더 날짜 = 마지막으로 마감된 미국 정규장 날짜. 새 종가가 나오기 전까지는 한국 날짜가 바뀌어도
같은 폴더에 이어서 쓰고, 기다리는 사이 장이 마감되면 새 종가로 처음부터 다시 분석한다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
LOG = REPORTS / "pick10_US.log"
NY = ZoneInfo("America/New_York")
MAX_AUTO = 3            # 자동 이어서 실행 최대 횟수
MAX_WAIT_H = 6          # 이보다 오래 기다려야 하면(주간 한도 등) 자동으로 기다리지 않는다
MARGIN = timedelta(minutes=3)
# claude CLI 의 5시간 한도 메시지: "You've hit your session limit · resets 11:40pm (Asia/Seoul)"
LIMIT = re.compile(r"hit your (?:session )?limit\s*·\s*resets (\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)")


def session_date(now: datetime | None = None) -> date:
    """마지막으로 마감된 미국 정규장 날짜 (뉴욕 16:00 마감, 주말 제외)."""
    t = (now or datetime.now(NY)).astimezone(NY)
    d = t.date() if t.hour >= 16 else t.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d  # ponytail: 미국 공휴일은 모른다 — 공휴일 다음 날은 새 종가가 없어도 새 폴더로 처음부터 시작


def run_folder(now: datetime | None = None) -> Path:
    return REPORTS / f"{session_date(now)}-US"


def limit_reset(log: str, now: datetime | None = None) -> datetime | None:
    """로그 끝의 5시간 한도 메시지에서 다시 차는 시각. 주간 한도·다른 형식·너무 먼 시각이면 None."""
    m = LIMIT.search(log[-500:])
    if not m:
        return None
    tz = ZoneInfo(m[4])
    now = (now or datetime.now(tz)).astimezone(tz)
    at = now.replace(hour=int(m[1]) % 12 + (12 if m[3] == "pm" else 0), minute=int(m[2] or 0), second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    return at if at - now <= timedelta(hours=MAX_WAIT_H) else None


def _start(state: dict, resume: bool):
    REPORTS.mkdir(exist_ok=True)
    folder = run_folder()
    if not resume and folder.exists():
        # 새로 시작할 때 이전 실행의 리포트가 섞이지 않도록 옆으로 치워 둔다 (지우지 않음)
        folder.rename(folder.with_name(f"{folder.name}-old-{time.strftime('%H%M%S')}"))
    rel = folder.relative_to(ROOT).as_posix()
    log = open(LOG, "a" if resume else "w", encoding="utf-8")
    state.update(log_pos=log.tell(), folder=folder, started=time.time(), resume_at=None, proc=subprocess.Popen(
        [shutil.which("claude"), "-p",
         f"/pick-10 US 이어서 폴더 {rel}" if resume else f"/pick-10 US 후보 {state['candidates']}개 폴더 {rel}",
         "--model", "sonnet",  # 조율만 한다. 결정권자·시장 분석가는 에이전트 설정에서 opus 고정
         "--permission-mode", "acceptEdits",
         "--allowedTools", "Agent,Read,Write,Glob,Grep,WebSearch,WebFetch,Bash(python:*),Bash(mkdir:*),Bash(ls:*)"],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT))


def _supervise(state: dict, gen: int):
    """끝날 때마다 로그를 보고, 5시간 한도로 멈췄으면 다시 찰 때까지 기다렸다가 이어서 실행한다."""
    while True:
        state["proc"].wait()
        if state["gen"] != gen:
            return
        with open(LOG, encoding="utf-8", errors="ignore") as f:
            f.seek(state["log_pos"])
            at = limit_reset(f.read())  # 이번 실행이 쓴 부분만 본다
        if at is None:
            return
        if state["attempts"] >= MAX_AUTO:
            state["note"] = f"자동 이어서 실행을 {MAX_AUTO}회 했지만 끝나지 않아 멈췄습니다."
            return
        state["resume_at"] = at + MARGIN
        while datetime.now(at.tzinfo) < state["resume_at"]:  # 벽시계로 비교 — 절전에서 깨어나도 맞다
            time.sleep(20)
            if state["gen"] != gen or state.get("cancel"):
                state["resume_at"] = None
                return
        state["attempts"] += 1
        same = run_folder() == state["folder"]
        if not same:
            state["note"] = "기다리는 사이 미국 장이 마감돼 새 종가로 처음부터 다시 분석합니다."
        _start(state, resume=same)


def launch(state: dict, candidates: int, resume: bool = False):
    """사용자가 실행·이어서 실행을 누를 때. 이전 감시 스레드는 gen 이 바뀌어 스스로 끝난다."""
    state.update(gen=state.get("gen", 0) + 1, attempts=0, cancel=False, note=None, candidates=candidates)
    _start(state, resume)
    threading.Thread(target=_supervise, args=(state, state["gen"]), daemon=True).start()


def running(state: dict) -> bool:
    return state.get("proc") is not None and state["proc"].poll() is None


if __name__ == "__main__":
    kst = ZoneInfo("Asia/Seoul")
    k = lambda s: datetime.fromisoformat(s).replace(tzinfo=kst)
    # 한국 금요일 밤(= 뉴욕 금요일 장중)은 목요일 종가, 토요일 아침(= 뉴욕 금요일 장 마감 후)은 금요일
    assert session_date(k("2026-09-25T23:00")) == date(2026, 9, 24)
    assert session_date(k("2026-09-26T06:00")) == date(2026, 9, 25)
    # 주말·월요일 장 마감 전은 금요일 종가
    assert session_date(k("2026-09-28T12:00")) == date(2026, 9, 25)
    assert session_date(k("2026-09-29T05:30")) == date(2026, 9, 28)

    msg = "...\nYou've hit your session limit · resets 11:40pm (Asia/Seoul)\n"
    assert limit_reset(msg, k("2026-09-25T20:35")) == k("2026-09-25T23:40")
    assert limit_reset(msg, k("2026-09-25T23:50")) is None                       # 다음 날 23:40 은 너무 멀다
    assert limit_reset(msg.replace("11:40pm", "3am"), k("2026-09-25T22:10")) == k("2026-09-26T03:00")  # 자정 넘김
    assert limit_reset(msg.replace("session", "weekly"), k("2026-09-25T20:35")) is None  # 주간 한도는 기다리지 않음
    assert limit_reset("정상 종료", k("2026-09-25T20:35")) is None
    print("ok")
