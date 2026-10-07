"""원클릭 리플레이 — 작업 관리 + 결과 스키마 생성 (3단계, 설계 문서 §4.2~4.4).

[작업 관리]  start_job → data/replay/<job_id>.json (상태 파일) 작성 → 자식 프로세스(python -m core.vwap.replay_job <job_id>) 실행.
             계산(지표·시뮬레이션)은 전부 자식 프로세스에서 합니다 — Flask/봇 프로세스를 막지 않기 위함.
             get_job 이 상태 파일을 읽으며 (a) 타임아웃(replay_timeout_sec) 초과 시 kill → failed("timeout"),
             (b) 자식이 죽었는데 완료 기록이 없으면 failed("process_exited") 로 정리합니다.
             서버 재시작 시 recover_on_startup() 이 queued/running 으로 남은 작업을 failed("server_restarted") 로 정리합니다.
[보관]       최근 5개 작업(json+log). 단 '가장 최근 성공(done) 작업'은 5개 밖이어도 지우지 않습니다(/replay/latest 보호).
[결과]       compute_result() 가 설계 §4.4 의 job.result 스키마를 만듭니다. 엔진은 backtest.engine(연구 엔진) 그대로.

모든 경로는 호출 시점의 core.vwap.config_manager.DATA_DIR 아래입니다(테스트에서 임시 폴더로 바꿀 수 있음).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

import numpy as np

import core.vwap.config_manager as _cm

logger = logging.getLogger("vwap_bot")

JOB_ID_RE = re.compile(r"rp-\d{8}-\d{6}-[0-9a-f]{4}")   # 항상 fullmatch 로 사용(\n 꼬리 허용 방지)
ACTIVE_STATES = ("queued", "running")
KEEP_JOBS = 5
DEFAULT_TIMEOUT_SEC = 300
DEFAULT_INITIAL_EQUITY = 10_000_000.0   # initial_balance 가 0 이하일 때 폴백(설정 기본값과 같음)
WARMUP_BARS = 30
MAX_EQUITY_POINTS = 500
MAX_TRADES = 500
MIN_TRADES_FOR_VERDICT = 30
DATE_FMT = "%Y-%m-%d %H:%M:%S"
MIN_FMT = "%Y-%m-%d %H:%M"
MAX_DAYS_REQUEST = 365
DATA_DIR_ENV = "VWAP_DATA_DIR"   # 자식 프로세스가 부모와 같은 data 폴더를 쓰도록 전달

FILL_MODEL_TEXT = "보수적(다음 봉 체결, 갭은 시가, 손절 우선)"
NOT_MODELED = [
    "일 손실한도 패닉",
    "부분체결/호가 대기열",
    "취소·정정 지연",
    "시간외 거래 여부 차이(Yahoo vs Toss)",
    "지표 계산 창 차이(150봉 vs 전체)",
    "거래 시작 대기 판단 시각 = 봉 마감 시각",
    "예산: 고정 명목금액(현금 부족 시 축소 없음)",
]

_lock = threading.RLock()
_procs = {}   # job_id -> subprocess.Popen (이 프로세스가 띄운 자식)


class JobRunning(Exception):
    def __init__(self, job_id):
        super().__init__(f"실행 중인 리플레이 작업이 있습니다: {job_id}")
        self.job_id = job_id


class NoDataError(Exception):
    """리플레이할 봉이 없거나 너무 적음 (error="no_data")."""


# ======================================================================
# 파일 입출력
# ======================================================================
def replay_dir() -> str:
    return os.path.join(_cm.DATA_DIR, "replay")


def _job_path(job_id: str) -> str:
    return os.path.join(replay_dir(), f"{job_id}.json")


def _log_path(job_id: str) -> str:
    return os.path.join(replay_dir(), f"{job_id}.log")


def _sanitize(obj):
    """JSON 직렬화 안전화: numpy → 파이썬, NaN/inf → None, Timestamp → 문자열."""
    if isinstance(obj, dict):
        return {str(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if hasattr(obj, "strftime"):
        return obj.strftime(DATE_FMT)
    return obj


def _atomic_write(path: str, data: dict):
    """tmp → replace 원자적 저장. Windows 에서 다른 프로세스가 읽는 중이면 replace 가 잠깐 실패하므로 재시도."""
    tmp = f"{path}.{os.getpid()}.tmp"
    payload = json.dumps(_sanitize(data), ensure_ascii=False, allow_nan=False)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    last = None
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            time.sleep(0.05)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last


def write_job(job: dict):
    os.makedirs(replay_dir(), exist_ok=True)
    _atomic_write(_job_path(job["job_id"]), job)


def read_job(job_id: str) -> Optional[dict]:
    """상태 파일 읽기. 없거나 읽는 도중 교체 중이면(몇 번 재시도) None."""
    if not JOB_ID_RE.fullmatch(str(job_id)):
        return None
    path = _job_path(job_id)
    for _ in range(20):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.05)
    return None


def list_job_ids() -> list:
    """최신순 job_id 목록(job_id 는 시각이 앞에 있어 문자열 정렬 = 시간순)."""
    d = replay_dir()
    if not os.path.isdir(d):
        return []
    ids = [n[:-5] for n in os.listdir(d) if n.endswith(".json") and JOB_ID_RE.fullmatch(n[:-5])]
    return sorted(ids, reverse=True)


def update_job(job_id: str, **fields) -> Optional[dict]:
    """자식 프로세스용: 상태 파일을 읽어 필드를 바꿔 저장. 이미 종료(done/failed)된 작업은 건드리지 않음(부모가 timeout 처리한 경우 등)."""
    with _lock:
        job = read_job(job_id)
        if job is None or job.get("state") not in ACTIVE_STATES:
            return None
        job.update(fields)
        write_job(job)
        return job


# ======================================================================
# 작업 시작 / 조회
# ======================================================================
def _now_str() -> str:
    return datetime.now().strftime(DATE_FMT)


def make_params(config: dict, days=10, interval=None, fee_roundtrip_pct=0.2, slippage_roundtrip_pct=0.05,
                limit_fill_buffer_pct=0.0, as_of: Optional[datetime] = None) -> dict:
    """요청 시점의 REAL 설정 스냅샷(real_*)과 요청 값을 합쳐 작업 params 를 만듭니다. 잘못된 값은 ValueError(한글 사유).

    config_snapshot = {ticker, market, interval, reset_time, start_time, params: {n/m/x/k_percent, initial_balance,
    max_daily_loss_limit, use_adx_filter, adx_threshold, use_rsi_filter, rsi_threshold, use_vwap_band, vwap_band_sigma}}
    (민감정보 없음 — Toss 키/비밀번호는 포함하지 않음)
    """
    from core.vwap.bar_source import SUPPORTED_INTERVALS
    from core.vwap.strategies.base import StrategyContext

    try:
        ctx = StrategyContext.from_config(config, "real")
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"실거래 설정을 읽을 수 없습니다: {e}")

    interval = str(interval or ctx.interval)
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"봉 간격은 {', '.join(SUPPORTED_INTERVALS)} 중에서 선택해주세요.")
    if isinstance(days, bool) or isinstance(days, float) and not float(days).is_integer():
        raise ValueError("기간(days)은 정수여야 합니다.")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ValueError("기간(days)은 정수여야 합니다.")
    if not 1 <= days <= MAX_DAYS_REQUEST:
        raise ValueError(f"기간(days)은 1~{MAX_DAYS_REQUEST} 사이여야 합니다.")

    def _num(name, val, lo, hi):
        try:
            f = float(val)
        except (TypeError, ValueError):
            raise ValueError(f"{name} 값이 숫자가 아닙니다.")
        if isinstance(val, bool) or not math.isfinite(f) or not lo <= f <= hi:
            raise ValueError(f"{name} 는 {lo}~{hi} 사이 숫자여야 합니다.")
        return f

    fee = _num("왕복 수수료(%)", fee_roundtrip_pct, 0.0, 5.0)
    slip = _num("왕복 슬리피지(%)", slippage_roundtrip_pct, 0.0, 5.0)
    buf = _num("체결 버퍼(%)", limit_fill_buffer_pct, 0.0, 5.0)

    snapshot = {
        "ticker": ctx.ticker, "market": ctx.market, "interval": interval,
        "reset_time": ctx.reset_time, "start_time": ctx.start_time,
        "params": dict(ctx.params),
    }
    return {
        "ticker": ctx.ticker, "interval": interval, "days": days,
        "config_snapshot": snapshot,
        "fee_roundtrip_pct": fee, "slippage_roundtrip_pct": slip, "limit_fill_buffer_pct": buf,
        "strategy": "S0_CURRENT@v1",
        "as_of": (as_of or datetime.now()).strftime(DATE_FMT),   # 데이터 기준 시각(재현용)
    }


def _spawn_child(job_id: str) -> subprocess.Popen:
    """자식 프로세스 실행(테스트에서 이 함수를 바꿔치기해 타임아웃/사망 시나리오를 만든다)."""
    env = os.environ.copy()
    env[DATA_DIR_ENV] = _cm.DATA_DIR
    env["PYTHONUTF8"] = "1"
    with open(_log_path(job_id), "ab") as log:
        return subprocess.Popen([sys.executable, "-m", "core.vwap.replay_job", job_id],
                                cwd=_cm.PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT, env=env)


def _prune():
    """최근 KEEP_JOBS-1 개(새 작업 몫 1개 남김) + 실행 중 작업 + 가장 최근 done 작업만 남기고 삭제."""
    ids = list_job_ids()
    keep = set(ids[:KEEP_JOBS - 1])
    newest_done = None
    for jid in ids:
        j = read_job(jid)
        if j and j.get("state") in ACTIVE_STATES:
            keep.add(jid)
        if newest_done is None and j and j.get("state") == "done":
            newest_done = jid
    if newest_done:
        keep.add(newest_done)
    for jid in ids:
        if jid not in keep:
            for p in (_job_path(jid), _log_path(jid)):
                try:
                    os.remove(p)
                except OSError:
                    pass


def active_job_id() -> Optional[str]:
    """실행 중(queued/running)인 작업 id. 타임아웃/사망 정리를 거친 뒤 판단."""
    for jid in list_job_ids():
        job = get_job(jid)
        if job and job["state"] in ACTIVE_STATES:
            return jid
    return None


def start_job(params: dict, timeout_sec: Optional[float] = None) -> str:
    """작업 생성 + 자식 프로세스 시작. 실행 중인 작업이 있으면 JobRunning."""
    with _lock:
        running = active_job_id()
        if running:
            raise JobRunning(running)
        _prune()
        job_id = f"rp-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        now = time.time()
        job = {
            "job_id": job_id, "state": "queued", "progress": 0, "stage": "queued", "message": "대기 중",
            "error": None, "created_at": _now_str(), "started_at": None, "finished_at": None,
            "elapsed_sec": 0.0, "params": params, "result": None,
            # 내부 필드(API 응답에서는 제거)
            "pid": None, "timeout_sec": float(timeout_sec if timeout_sec else DEFAULT_TIMEOUT_SEC),
            "created_ts": now, "started_ts": None, "finished_ts": None,
        }
        write_job(job)
        try:
            proc = _spawn_child(job_id)
        except Exception as e:
            _fail(job, "exception", f"계산 프로세스를 시작하지 못했습니다: {type(e).__name__}")
            raise
        _procs[job_id] = proc   # pid 는 자식이 'running' 으로 갱신할 때 상태 파일에 기록(부모가 다시 쓰면 자식 갱신과 경합)
        return job_id


def _fail(job: dict, error: str, message: str):
    job.update({"state": "failed", "error": error, "message": message, "finished_at": _now_str(),
                "finished_ts": time.time()})
    write_job(job)


def _kill(job_id: str):
    proc = _procs.get(job_id)
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def _refresh(job: dict) -> dict:
    """실행 중 작업의 타임아웃/프로세스 사망을 반영."""
    if job.get("state") not in ACTIVE_STATES:
        return job
    jid = job["job_id"]
    proc = _procs.get(jid)
    if proc is not None and proc.poll() is not None:
        job = read_job(jid) or job          # 종료 직전에 done/failed 를 썼을 수 있음
        if job.get("state") in ACTIVE_STATES:
            _fail(job, "process_exited", f"계산 프로세스가 비정상 종료되었습니다(종료 코드 {proc.returncode}). 로그: {jid}.log")
        return job
    base = job.get("started_ts") or job.get("created_ts") or time.time()
    limit = float(job.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
    if time.time() - base > limit:
        _kill(jid)
        job = read_job(jid) or job
        if job.get("state") in ACTIVE_STATES:
            _fail(job, "timeout", f"제한 시간({int(limit)}초)을 넘겨 중단했습니다. 기간을 줄이거나 봉 간격을 늘려보세요.")
    return job


_INTERNAL_KEYS = ("pid", "timeout_sec", "created_ts", "started_ts", "finished_ts")


def get_job(job_id: str, public: bool = True) -> Optional[dict]:
    """작업 상태(필요하면 타임아웃/사망 정리 후). 없으면 None. public 이면 내부 필드 제거 + elapsed_sec 갱신."""
    with _lock:
        job = read_job(job_id)
        if job is None:
            return None
        job = _refresh(job)
        end = job.get("finished_ts") or time.time()
        job["elapsed_sec"] = round(max(0.0, end - (job.get("started_ts") or job.get("created_ts") or end)), 1)
        if public:
            job = {k: v for k, v in job.items() if k not in _INTERNAL_KEYS}
        return job


def latest_done_job() -> Optional[dict]:
    for jid in list_job_ids():
        job = get_job(jid)
        if job and job["state"] == "done":
            return job
    return None


def _pid_cmdline(pid: int):
    """(상태, 명령줄). 상태: 'ok'(명령줄 확인) / 'dead'(없음) / 'unknown'(확인 불가 -> 호출자는 kill 금지)."""
    try:
        if sys.platform.startswith("win"):
            # Windows: os.kill(pid, 0) 은 프로세스를 종료시키므로 쓰지 않고 WMI 로 명령줄 조회
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return "unknown", ""
            out = (r.stdout or "").strip()
            return ("ok", out) if out else ("dead", "")
        if os.path.isdir("/proc/self"):
            try:
                with open(f"/proc/{int(pid)}/cmdline", "rb") as f:
                    raw = f.read()
            except FileNotFoundError:
                return "dead", ""
            return "ok", raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        r = subprocess.run(["ps", "-o", "args=", "-p", str(int(pid))], capture_output=True, text=True, timeout=10)
        out = (r.stdout or "").strip()
        if out:
            return "ok", out
        return ("dead", "") if r.returncode == 1 else ("unknown", "")
    except Exception:
        return "unknown", ""


def _kill_pid(pid: int):
    import signal
    os.kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))   # Windows 는 SIGTERM = TerminateProcess


def _kill_orphan(job: dict) -> str:
    """이전 프로세스가 남긴 자식(pid)이 아직 이 작업의 replay_job 이면 kill. 반환: killed/dead/skipped(사유는 로그)."""
    pid = job.get("pid")
    jid = job["job_id"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return "skipped"
    status, cmd = _pid_cmdline(pid)
    if status == "dead":
        return "dead"
    if status != "ok":
        logger.warning(f"[replay] {jid}: pid {pid} 명령줄을 확인할 수 없어 kill 하지 않습니다.")
        return "skipped"
    if "core.vwap.replay_job" not in cmd or jid not in cmd:
        logger.warning(f"[replay] {jid}: pid {pid} 는 이 작업의 replay_job 이 아니라서(pid 재사용 추정) kill 하지 않습니다.")
        return "skipped"
    try:
        _kill_pid(pid)
        logger.warning(f"[replay] {jid}: 남은 자식 프로세스 pid {pid} 를 종료했습니다.")
        return "killed"
    except Exception as e:
        logger.warning(f"[replay] {jid}: pid {pid} 종료 실패({type(e).__name__})")
        return "skipped"


def recover_on_startup() -> int:
    """서버 시작 시: queued/running 으로 남은 작업(이전 프로세스의 자식)을 failed('server_restarted')로 정리. 정리 건수 반환.
    상태 파일의 pid 가 아직 살아 있는 이 작업의 replay_job 이면(명령줄 확인 후) kill 합니다."""
    n = 0
    with _lock:
        for jid in list_job_ids():
            job = read_job(jid)
            if job and job.get("state") in ACTIVE_STATES and jid not in _procs:
                _kill_orphan(job)
                _fail(job, "server_restarted", "서버가 재시작되어 작업이 중단되었습니다. 다시 실행해주세요.")
                n += 1
    return n


# ======================================================================
# 계산 (자식 프로세스에서 호출)
# ======================================================================
def peak_rss_mb() -> Optional[float]:
    """현재 프로세스의 최대 상주 메모리(MB). 측정 불가면 None."""
    try:
        import resource   # POSIX (S9/Termux)
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round((v / 1024.0) if sys.platform != "darwin" else (v / 1048576.0), 1)   # Linux: KB
    except ImportError:
        pass
    try:   # Windows
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        proc = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        if ctypes.windll.psapi.GetProcessMemoryInfo(proc, ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / 1048576.0, 1)
    except Exception:
        pass
    return None


def judge_verdict(n_trades: int, t_stat, expectancy_pct) -> str:
    """ADR-0006 기준: 30건 미만 insufficient / t<=-2 negative / t>=2 이고 기대값>0 positive / 그 외 inconclusive."""
    if n_trades < MIN_TRADES_FOR_VERDICT:
        return "insufficient"
    if t_stat is None or (isinstance(t_stat, float) and math.isnan(t_stat)):
        return "inconclusive"
    if t_stat <= -2.0:
        return "negative"
    if t_stat >= 2.0 and expectancy_pct is not None and expectancy_pct > 0:
        return "positive"
    return "inconclusive"


def _none_if_nan(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _downsample_idx(n: int, limit: int = MAX_EQUITY_POINTS) -> np.ndarray:
    if n <= limit:
        return np.arange(n)
    return np.unique(np.linspace(0, n - 1, limit).round().astype(int))


def format_trades(trades, limit: int = MAX_TRADES) -> list:
    """최근 limit 건(최신순)."""
    out = []
    for t in list(trades)[-limit:][::-1]:
        out.append({
            "entry_time": t.entry_time.strftime(MIN_FMT), "exit_time": t.exit_time.strftime(MIN_FMT),
            "entry_price": round(float(t.entry_price), 4), "exit_price": round(float(t.exit_price), 4),
            "qty": float(t.qty), "net_pnl": round(float(t.net_pnl), 2), "ret_pct": round(float(t.ret_pct), 4),
            "bars_held": int(t.bars_held), "exit_reason": t.exit_reason, "fees": round(float(t.fees), 2),
        })
    return out


def exit_reason_table(trades) -> list:
    groups = {}
    for t in trades:
        groups.setdefault(t.exit_reason, []).append(float(t.ret_pct))
    rows = [{"reason": r, "count": len(v), "avg_ret_pct": round(sum(v) / len(v), 4), "sum_ret_pct": round(sum(v), 4)}
            for r, v in groups.items()]
    return sorted(rows, key=lambda x: (-x["count"], x["reason"]))


def buy_hold_curve(frame, cost, initial: float, k_percent: float, warmup: int = WARMUP_BARS):
    """같은 구간 Buy&Hold: warmup 이후 첫 봉 시가 매수 → 마지막 종가 매도. 같은 명목금액·같은 CostModel. (absolute 평가액 배열, 순손익) 반환."""
    o = frame["open"].to_numpy(float)
    c = frame["close"].to_numpy(float)
    n = len(frame)
    buy = cost.buy_price(float(o[warmup]))
    qty = float(int((initial * k_percent / 100.0) / buy))
    fee_buy = cost.fee(buy * qty)
    eq = np.full(n, initial, dtype=float)
    eq[warmup:] = initial - buy * qty - fee_buy + qty * c[warmup:]
    sell = cost.sell_price(float(c[-1]))
    net = (sell - buy) * qty - fee_buy - cost.fee(sell * qty)
    eq[-1] = initial + net
    return eq, float(net)


class _ProgressAdapter:
    """PluginEngineAdapter 를 감싸 simulating 단계 진행률(50~90%)을 일정 간격으로 알립니다."""

    def __init__(self, inner, n: int, warmup: int, progress):
        self._inner = inner
        self.name = inner.name
        self._n, self._warmup, self._progress = n, warmup, progress
        self._last_emit = 0.0
        self._last_pct = 50

    def on_start(self, df, cost):
        return self._inner.on_start(df, cost)

    def on_session_start(self, i, df):
        return self._inner.on_session_start(i, df)

    def on_entry(self, i, df, pos):
        return self._inner.on_entry(i, df, pos)

    def decide(self, j, df, pos):
        now = time.monotonic()
        if self._progress is not None and now - self._last_emit >= 0.5:
            span = max(1, self._n - self._warmup)
            pct = int(50 + 40 * min(1.0, max(0.0, (j - self._warmup) / span)))
            pct = max(pct, self._last_pct)           # 단조 증가 보장
            self._last_pct = pct
            self._last_emit = now
            self._progress("simulating", pct, "시뮬레이션 중")
        return self._inner.decide(j, df, pos)


def compute_result(bars, meta: dict, params: dict, progress=None) -> dict:
    """봉 + 작업 params → 설계 §4.4 의 job.result. progress(stage, pct, message) 콜백은 선택."""
    import pandas as pd
    from backtest.engine import CostModel, run_backtest, summarize
    from core.vwap.replay_engine_adapter import PluginEngineAdapter, build_engine_frame
    from core.vwap.strategies import S0CurrentStrategy
    from core.vwap.strategies.base import StrategyContext

    emit = progress or (lambda *a, **k: None)
    t0 = time.perf_counter()
    snap = params["config_snapshot"]
    ctx = StrategyContext(ticker=snap["ticker"], market=snap["market"], interval=snap["interval"],
                          reset_time=snap["reset_time"], start_time=snap.get("start_time", ""),
                          params=snap["params"])
    strategy = S0CurrentStrategy.from_config({}, "real")
    warnings = list(meta.get("warnings") or [])

    raw = bars[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    if len(raw) <= WARMUP_BARS + 2:
        raise NoDataError(f"리플레이할 봉이 부족합니다({len(raw)}개). 기간을 늘리거나 봉 적재/시세 조회 상태를 확인해주세요.")

    initial = float(ctx.params.get("initial_balance", 0.0))
    if initial <= 0:
        initial = DEFAULT_INITIAL_EQUITY
        warnings.append(f"초기 자본(real_initial_balance)이 0 이하여서 기본값 {DEFAULT_INITIAL_EQUITY:,.0f} 로 계산했습니다.")
    k_percent = float(ctx.params["k_percent"])

    emit("preparing", 40, "지표 계산 중")
    t_prep = time.perf_counter()
    frame = build_engine_frame(strategy, raw, ctx)
    prepare_sec = time.perf_counter() - t_prep

    cost = CostModel(float(params["fee_roundtrip_pct"]), float(params["slippage_roundtrip_pct"]),
                     float(params["limit_fill_buffer_pct"]))
    emit("simulating", 50, "시뮬레이션 중")
    t_sim = time.perf_counter()
    adapter = _ProgressAdapter(PluginEngineAdapter(strategy, ctx), len(frame), WARMUP_BARS, progress)
    res = run_backtest(frame, adapter, cost, initial_equity=initial, k_percent=k_percent, warmup_bars=WARMUP_BARS)
    simulate_sec = time.perf_counter() - t_sim

    emit("summarizing", 95, "결과 정리 중")
    s = summarize(res)
    n_trades = int(s["거래수"])
    t_stat = _none_if_nan(s["t-stat"])
    expectancy = _none_if_nan(s["기대값%/거래"])
    verdict = judge_verdict(n_trades, t_stat, expectancy)
    if n_trades < MIN_TRADES_FOR_VERDICT:
        warnings.append("표본 부족 — 판단 불가")

    bh_eq, bh_net = buy_hold_curve(frame, cost, initial, k_percent)
    strat_eq = res.equity.to_numpy(float)
    times = pd.to_datetime(frame["time"])
    idx = _downsample_idx(len(frame))
    equity = [{"t": times.iloc[i].strftime(MIN_FMT), "strategy": round(float(strat_eq[i]), 2),
               "buy_hold": round(float(bh_eq[i]), 2)} for i in idx]

    result = {
        "summary": {
            "trades": n_trades,
            "win_rate_pct": _none_if_nan(s["승률%"]),
            "avg_win_pct": _none_if_nan(s["평균이익%"]),
            "avg_loss_pct": _none_if_nan(s["평균손실%"]),
            "payoff_ratio": _none_if_nan(s["손익비"]),
            "expectancy_pct": expectancy,
            "expectancy_amount": _none_if_nan(s["기대값$/거래"]),
            "net_pnl": _none_if_nan(s["총순손익$"]),
            "total_return_pct": _none_if_nan(s["총수익률%"]),
            "mdd_pct": _none_if_nan(s["MDD%"]),
            "profit_factor": _none_if_nan(s["PF"]),
            "avg_bars_held": _none_if_nan(s["평균보유봉"]),
            "t_stat": t_stat,
            "verdict": verdict,
        },
        "benchmark": {"buy_hold_return_pct": round(bh_net / initial * 100.0, 3), "buy_hold_net_pnl": round(bh_net, 2)},
        "equity": equity,
        "trades": format_trades(res.trades),
        "exit_reasons": exit_reason_table(res.trades),
        "data": {
            "bars": int(len(frame)), "sessions": int(res.sessions),
            "from": meta.get("from"), "to": meta.get("to"), "interval": params["interval"],
            "source_breakdown": meta.get("source_breakdown", {"bars_store": 0, "yahoo": 0}),
            "gaps": meta.get("gaps", []), "limits": meta.get("limits", ""),
        },
        "assumptions": {
            "fee_roundtrip_pct": cost.fee_roundtrip_pct, "slippage_roundtrip_pct": cost.slippage_roundtrip_pct,
            "limit_fill_buffer_pct": cost.limit_fill_buffer_pct, "fill_model": FILL_MODEL_TEXT,
            "notional": round(res.notional, 2), "initial_equity": initial, "not_modeled": list(NOT_MODELED),
        },
        "warnings": warnings,
        "perf": {"prepare_sec": round(prepare_sec, 2), "simulate_sec": round(simulate_sec, 2),
                 "compute_sec": round(time.perf_counter() - t0, 2), "peak_rss_mb": peak_rss_mb(), "bars": int(len(frame))},
    }
    return _sanitize(result)
