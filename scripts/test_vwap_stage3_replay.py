"""
VWAP 3단계 JR-2 검증 — 원클릭 리플레이 (설계 문서 §4·§11 T-R1~R3, T-A1(리플레이), T-PERF). 외부 네트워크 없이 실행됩니다.

검증 항목
  T-R1  bar_source  적재분 우선 병합 / Yahoo 분할 요청 횟수·기간(requests 목) / 캐시 재사용 / gaps / .KS→.KQ / 일수 상한 / mock 봉 제외
  T-R2  replay 작업 실제 자식 프로세스 실행 → done, 진행률 단조 증가, 409 동시성, 타임아웃 kill, 프로세스 사망,
        서버 재시작 정리, no_data/exception 실패 기록, 보관 5개(최근 성공 보호)
  T-R3  결과 스키마 필드 전부 / equity ≤ 500점 / verdict 규칙 / Buy&Hold / trades ≤ 500 / JSON 엄격 직렬화 /
        run_compare 와 같은 조건(S0_Current 연구 경로)에서 수치 일치 / initial_balance 0 폴백
  T-A1  리플레이 API 3개: 401, 정상 202, 파라미터 400, 409, 404, latest + 설정 키 bool/숫자 캐스팅("False" 문자열 저장 금지)
  T-PERF 1분봉 2만 봉 상당 리플레이 소요시간·메모리(자식 프로세스 최대 상주 메모리)
  T-G   운영 data/ 해시(하위 폴더 포함) 전후 동일, trading_bot_*.log 신규 생성 없음

실행:  PYTHONUTF8=1 C:\\Users\\user\\butler_pjt\\venv312\\Scripts\\python.exe scripts/test_vwap_stage3_replay.py
"""
import os
import sys
import glob
import json
import time
import shutil
import hashlib
import logging
import traceback
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

LOGS_BEFORE = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log")))


def _hash_tree(path):
    """하위 폴더까지 포함한 전체 해시(상대경로 -> sha256)."""
    out = {}
    for root, _dirs, files in os.walk(path):
        for n in files:
            fp = os.path.join(root, n)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


REAL_DATA_TREE_BEFORE = _hash_tree(os.path.join(PROJECT_ROOT, "data"))

# 1단계 하네스 재사용: import 시점에 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + Discord 전송 no-op
import test_vwap_reliability as h  # noqa: E402

# 봇 로거를 콘솔 전용으로 미리 등록(운영 로그 파일에 쓰지 않음). API 모듈은 5개 봇을 모두 만든다.
for _name in ["vwap_bot_virtual_1", "vwap_bot_virtual_2", "vwap_bot_virtual_3", "vwap_bot_real"]:
    _lg = logging.getLogger(_name)
    if not _lg.handlers:
        _lg.setLevel(logging.INFO)
        _lg.addHandler(logging.NullHandler())
        _lg.propagate = False
    for _hd in _lg.handlers:
        _hd.setLevel(logging.CRITICAL)

# 실제 자격증명/.env 가 설정에 섞이지 않게: .env 로드를 막고 Toss 환경변수를 제거
import core.vwap.config_manager as cm  # noqa: E402
cm.load_dotenv = lambda *a, **k: False
for _k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ", "VWAP_ADMIN_PASSWORD"):
    os.environ.pop(_k, None)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from core.vwap import bars_store, bar_source, replay, replay_job  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap.session import SessionSpec, interval_minutes  # noqa: E402

check = h.check
D = datetime
TMP = h.TMP_DIR
PY = sys.executable


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def fresh_env():
    """임시 DATA_DIR 초기화(파일 + bars/replay/replay_cache 폴더) 및 메모리 상태 리셋."""
    for name in os.listdir(TMP):
        p = os.path.join(TMP, name)
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
    bars_store._reset_state()
    replay._procs.clear()


def synth_bars(rng, times, base=100.0, mean_revert=0.05, vol=0.25):
    """평균회귀 + 잡음의 결정적 OHLCV (거래가 충분히 발생하도록). times: DatetimeIndex."""
    n = len(times)
    x = np.empty(n)
    level = base
    for i in range(n):
        level += mean_revert * (base - level) + rng.normal(0, vol)
        x[i] = level
    close = np.round(np.maximum(x, 1.0), 2)
    open_ = np.round(np.r_[close[0], close[:-1]] + rng.normal(0, vol * 0.3, n), 2)
    hi = np.round(np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.6, n)), 2)
    lo = np.round(np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.6, n)), 2)
    volume = np.round(rng.uniform(100, 5000, n))
    return pd.DataFrame({"time": times, "open": open_, "high": hi, "low": lo, "close": close, "volume": volume})


US_SPEC = SessionSpec.for_market("US", "22:30", "TEST")


def session_times(spec, label_date, interval="5m", until=None):
    """세션 label_date 의 모든 봉 시각(세션 시작 ~ 다음 세션 시작 직전). until 이 있으면 그 시각 이전(포함)만."""
    start = D.combine(label_date, spec.reset_on(label_date))
    nd = label_date + timedelta(days=1)
    end = D.combine(nd, spec.reset_on(nd))
    step = interval_minutes(interval)
    t = pd.date_range(start, end - timedelta(minutes=step), freq=f"{step}min")
    if until is not None:
        t = t[t <= until]
    return t


def write_store(rng, ticker, interval, dates, spec=US_SPEC, source="toss", until=None):
    """dates(세션 날짜 date 목록)의 합성 봉을 bars_store 에 적재(마지막 진행 중 봉 제외 규칙 때문에 더미 1행을 덧붙임). 적재 행 수 반환."""
    frames = [synth_bars(rng, session_times(spec, d, interval, until)) for d in dates]
    df = pd.concat(frames, ignore_index=True).sort_values("time").reset_index(drop=True)
    sentinel = df.iloc[[-1]].copy()
    sentinel["time"] = sentinel["time"] + timedelta(minutes=interval_minutes(interval))
    return bars_store.append_closed_bars(ticker, interval, pd.concat([df, sentinel], ignore_index=True), "22:30", source)


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class YahooMock:
    """requests.get 대체. 지정한 세션 라벨(allowed)의 봉만 돌려주고 호출을 기록한다."""

    def __init__(self, spec, allowed_labels, close=999.0, fail_symbols=()):
        self.spec, self.allowed, self.close, self.fail = spec, set(allowed_labels), close, set(fail_symbols)
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        sym = url.rsplit("/", 1)[1]
        self.calls.append({"symbol": sym, "params": dict(params), "headers": headers or {}, "timeout": timeout})
        if sym in self.fail:
            return FakeResp(404, {})
        step = interval_minutes(params["interval"]) * 60
        p1 = (params["period1"] + step - 1) // step * step
        stamps = list(range(p1, params["period2"], step))
        if not stamps:
            return FakeResp(200, {"chart": {"result": []}})
        kst = pd.Series(pd.to_datetime(stamps, unit="s") + pd.Timedelta(hours=9))
        labels = self.spec.label_bars(kst).dt.strftime("%Y-%m-%d")
        keep = [i for i, lab in enumerate(labels) if lab in self.allowed]
        if not keep:
            return FakeResp(200, {"chart": {"result": []}})
        ts = [stamps[i] for i in keep]
        c = [self.close] * len(ts)
        return FakeResp(200, {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{
            "open": c, "high": [x + 0.5 for x in c], "low": [x - 0.5 for x in c], "close": c, "volume": [1000.0] * len(ts)}]}}]}})


class patched_requests:
    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        self.orig = requests.get
        requests.get = self.fn
        return self.fn

    def __exit__(self, *a):
        requests.get = self.orig


def no_network(*a, **k):
    raise AssertionError("네트워크 호출이 일어났습니다(테스트에서는 허용되지 않음)")


def test_config(**over):
    """리플레이용 설정 dict(기본값 + REAL 설정 덮어쓰기)."""
    cfg = VwapConfigManager.get_default_config()
    cfg.update({"real_ticker": "TEST", "real_market": "US", "real_interval": "5m", "real_reset_time": "22:30",
                "real_start_time": "", "real_initial_balance": 10000.0, "real_k_percent": 50.0,
                "real_n_percent": 0.3, "real_m_percent": 0.5, "real_x_percent": 1.0})
    cfg.update(over)
    return cfg


def weekdays_between(a, b):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------------------------------
# T-R1
# ---------------------------------------------------------------------------
def test_r1_bar_source():
    print("\nT-R1. bar_source — 적재분 우선 병합 / Yahoo 분할 요청 / 캐시 / gaps")
    NOW = D(2026, 10, 8, 6, 0)          # 목요일 06:00 KST → 진행 중 세션 라벨 10/7
    rng = np.random.default_rng(7)

    # --- 1) 적재분 우선 병합 (5m, days=7) ---
    fresh_env()
    stored_dates = [D(2026, 10, 1).date(), D(2026, 10, 2).date(), D(2026, 10, 5).date(), D(2026, 10, 6).date()]
    n_store = write_store(rng, "TEST", "5m", stored_dates)
    check("적재분 준비: 4세션 × 288봉 기록", n_store == 4 * 288, str(n_store))
    allowed = {"2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"}
    mock = YahooMock(US_SPEC, allowed)
    with patched_requests(mock):
        df, meta = bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    sb = meta["source_breakdown"]
    check("빠진 세션(9/30, 10/7)만 Yahoo 에서 보충: 적재분 1152 + Yahoo 378(288 + 진행 중 봉 제외한 90)",
          sb == {"bars_store": 1152, "yahoo": 378} and meta["bars"] == 1530, f"{sb} bars={meta['bars']}")
    check("5m 7일 → Yahoo 요청 1회(59일 단위 분할), User-Agent/period1/period2 사용",
          len(mock.calls) == 1 and "Mozilla" in mock.calls[0]["headers"].get("User-Agent", "")
          and mock.calls[0]["params"]["period1"] < mock.calls[0]["params"]["period2"], str(len(mock.calls)))
    first_p1 = mock.calls[0]["params"]["period1"]
    exp_p1 = int((D(2026, 9, 30, 22, 30) - timedelta(hours=9) - D(1970, 1, 1)).total_seconds())
    check("요청 시작 = 가장 이른 빠진 세션(9/30 22:30 KST) 의 UTC epoch", first_p1 == exp_p1, f"{first_p1} vs {exp_p1}")
    ov = df[(df["time"] >= D(2026, 10, 1, 22, 30)) & (df["time"] < D(2026, 10, 2, 22, 30))]
    check("겹치는 시각은 적재분 우선(close≈100, source=toss, 999 없음)",
          len(ov) == 288 and (ov["source"] == "toss").all() and (ov["close"] < 500).all())
    check("정렬·중복 없음, 필수 열 + source", df["time"].is_monotonic_increasing and df["time"].is_unique
          and list(df.columns) == ["time", "open", "high", "low", "close", "volume", "source"])
    check("진행 중(마감 전) Yahoo 봉 제외: 마지막 봉 시각 + 5분 <= now", df["time"].iloc[-1] + timedelta(minutes=5) <= NOW,
          str(df["time"].iloc[-1]))
    check("gaps 없음 / sessions=6 / from·to", meta["gaps"] == [] and meta["sessions"] == 6
          and meta["from"] == "2026-09-30" and meta["to"] == "2026-10-08", str(meta["gaps"]) + str(meta["sessions"]) + str(meta["from"]) + str(meta["to"]))

    # --- 2) 캐시 재사용 ---
    cache_files = sorted(os.listdir(bar_source.cache_dir())) if os.path.isdir(bar_source.cache_dir()) else []
    check("Yahoo 원본 캐시 저장: TEST_5m_20261008.csv(+meta)", "TEST_5m_20261008.csv" in cache_files, str(cache_files))
    mock2 = YahooMock(US_SPEC, allowed)
    with patched_requests(mock2):
        df2, meta2 = bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    check("같은 날 재호출은 캐시 사용 → Yahoo 요청 0회, 결과 동일", len(mock2.calls) == 0 and len(df2) == len(df)
          and meta2["source_breakdown"] == sb)
    mock3 = YahooMock(US_SPEC, allowed)
    with patched_requests(mock3):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW + timedelta(days=1))
    check("다음 날(조회일 변경)은 캐시 무효 → 다시 요청", len(mock3.calls) >= 1)

    # --- 3) gaps: 해당 세션에 Yahoo 응답이 없으면 gaps 로 남음 ---
    fresh_env()
    write_store(rng, "TEST", "5m", stored_dates)
    mock = YahooMock(US_SPEC, {"2026-10-01"})   # 9/30, 10/7 은 응답 없음(공휴일 가정)
    with patched_requests(mock):
        df, meta = bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    check("Yahoo 에도 없는 평일 세션은 gaps 로 보고: ['2026-09-30','2026-10-07']", meta["gaps"] == ["2026-09-30", "2026-10-07"], str(meta["gaps"]))

    # --- 4) 1분봉 20일: 7일 단위 3회 분할, 구간이 이어짐 ---
    fresh_env()
    allowed_all = {d.strftime("%Y-%m-%d") for d in weekdays_between(D(2026, 9, 10).date(), D(2026, 10, 8).date())}
    mock = YahooMock(US_SPEC, allowed_all)
    with patched_requests(mock):
        df, meta = bar_source.load_bars("TEST", "1m", 20, "22:30", now=NOW)
    ps = [(c["params"]["period1"], c["params"]["period2"]) for c in mock.calls]
    check("1m 20일(적재분 없음) → Yahoo 3회 요청", len(mock.calls) == 3, str(len(mock.calls)))
    check("각 요청 구간 <= 7일, 다음 요청 시작 = 이전 요청 끝", all(b - a <= 7 * 86400 for a, b in ps)
          and all(ps[i][1] == ps[i + 1][0] for i in range(len(ps) - 1)), str(ps))
    check("전부 Yahoo 출처, gaps 없음", meta["source_breakdown"]["bars_store"] == 0 and meta["gaps"] == [] and meta["bars"] > 10000,
          f"{meta['source_breakdown']} gaps={meta['gaps']}")

    # --- 5) 5m 90일 → 60일로 절단 + 경고, 59일 단위 분할 ---
    fresh_env()
    mock = YahooMock(US_SPEC, set())
    with patched_requests(mock):
        df, meta = bar_source.load_bars("TEST", "5m", 90, "22:30", now=NOW)
    check("일수 상한: 5m 90일 → 60일 절단 경고", any("60일" in w for w in meta["warnings"]), str(meta["warnings"]))
    span = mock.calls[0]["params"]["period2"] - mock.calls[0]["params"]["period1"] if mock.calls else 0
    check("60일 구간(첫 평일 세션부터 ≈59.3일): 59일 단위 → 요청 1회, 한 요청 구간 ≤ 59일 / 빈 응답이면 bars=0, gaps 에 평일 세션 전부",
          len(mock.calls) == 1 and span <= 59 * 86400 and meta["bars"] == 0 and len(df) == 0 and len(meta["gaps"]) > 30,
          f"calls={len(mock.calls)} span_days={span / 86400:.2f} gaps={len(meta['gaps'])}")

    # --- 6) 국내 종목: .KS 실패 → .KQ ---
    fresh_env()
    kr_spec = SessionSpec.for_market("KR", "09:00", "005930")
    kr_allowed = {d.strftime("%Y-%m-%d") for d in weekdays_between(D(2026, 10, 1).date(), D(2026, 10, 8).date())}
    mock = YahooMock(kr_spec, kr_allowed, fail_symbols={"005930.KS"})
    with patched_requests(mock):
        df, meta = bar_source.load_bars("005930", "5m", 3, "09:00", now=D(2026, 10, 7, 16, 0))
    syms = [c["symbol"] for c in mock.calls]
    check("6자리 숫자 티커: .KS 실패 시 .KQ 재시도", syms[:2] == ["005930.KS", "005930.KQ"] and meta["bars"] > 0, str(syms))

    # --- 7) mock 출처 적재 봉 제외 / 잘못된 interval ---
    fresh_env()
    write_store(rng, "TEST", "5m", [D(2026, 10, 6).date()], source="mock")
    with patched_requests(no_network):
        df, meta = bar_source.load_bars("TEST", "5m", 1, "22:30", now=D(2026, 10, 7, 12, 0))
    check("source=mock 적재 봉은 리플레이에서 제외 + 경고", meta["bars"] == 0 and any("mock" in w for w in meta["warnings"]), str(meta["warnings"]))
    try:
        bar_source.load_bars("TEST", "1h", 3, "22:30")
        bad = False
    except ValueError:
        bad = True
    check("지원하지 않는 interval(1h) → ValueError", bad)
    # 네트워크 오류는 예외가 아니라 경고
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("down")
    fresh_env()
    with patched_requests(boom):
        df, meta = bar_source.load_bars("TEST", "5m", 3, "22:30", now=NOW)
    check("Yahoo 네트워크 오류는 예외 없이 경고로 남고 빈 결과", len(df) == 0 and any("Yahoo" in w for w in meta["warnings"]))


# ---------------------------------------------------------------------------
# T-R2
# ---------------------------------------------------------------------------
def make_spawn(code):
    def _spawn(job_id):
        return __import__("subprocess").Popen([PY, "-c", code])
    return _spawn


def wait_state(job_id, states, timeout=120, sample=None):
    t0 = time.time()
    while time.time() - t0 < timeout:
        job = replay.get_job(job_id)
        if sample is not None and job:
            sample.append(job["progress"])
        if job and job["state"] in states:
            return job
        time.sleep(0.15)
    return replay.get_job(job_id)


def prepare_store_for_replay(days=5, interval="5m", asof=D(2026, 10, 8, 6, 0), seed=11):
    """as_of 기준 days 일치 평일 세션을 전부 적재 → 자식 프로세스가 네트워크 없이 끝나도록."""
    rng = np.random.default_rng(seed)
    start = US_SPEC.session_start(asof - timedelta(days=days)).date()
    end = US_SPEC.session_start(asof).date()
    dates = weekdays_between(start, end)
    write_store(rng, "TEST", interval, dates, until=asof - timedelta(minutes=interval_minutes(interval)))
    return dates


def test_r2_jobs():
    print("\nT-R2. replay 작업 — 자식 프로세스 / 진행률 / 409 / 타임아웃 / 사망 / 정리")
    ASOF = D(2026, 10, 8, 6, 0)
    fresh_env()
    prepare_store_for_replay(5, "5m", ASOF)
    params = replay.make_params(test_config(), days=5, as_of=ASOF)

    orig_spawn = replay._spawn_child
    with patched_requests(no_network):   # 이 프로세스(부모)에서는 네트워크 금지. (자식은 적재분만으로 끝나야 함 — 자식이 요청하면 실패로 드러남)
        t0 = time.time()
        job_id = replay.start_job(params, timeout_sec=120)
        check("start_job → job_id 형식 rp-YYYYMMDD-HHMMSS-xxxx", replay.JOB_ID_RE.match(job_id) is not None, job_id)
        j0 = replay.get_job(job_id)
        check("직후 상태 queued/running, 파일 data/replay/<id>.json 생성", j0["state"] in ("queued", "running")
              and os.path.exists(os.path.join(TMP, "replay", job_id + ".json")))
        check("공개 응답에 내부 필드(pid/timeout_sec/…_ts) 없음", not any(k in j0 for k in replay._INTERNAL_KEYS))
        try:
            replay.start_job(params)
            conflict = None
        except replay.JobRunning as e:
            conflict = e.job_id
        check("실행 중 작업이 있으면 JobRunning(409 근거) + 실행 중 job_id", conflict == job_id, str(conflict))
        prog = []
        job = wait_state(job_id, ("done", "failed"), timeout=180, sample=prog)
        elapsed = time.time() - t0
    check("자식 프로세스가 끝까지 실행되어 done / progress 100 / result 존재", job["state"] == "done" and job["progress"] == 100
          and job["result"] is not None and job["error"] is None, f"{job['state']} {job.get('error')} {job.get('message')}")
    check("진행률 단조 증가(관측값)", all(b >= a for a, b in zip(prog, prog[1:])) and prog[-1] == 100, str(prog[:40]))
    check("자식이 부모와 같은 data 폴더에 로그 기록(<id>.log)", os.path.exists(os.path.join(TMP, "replay", job_id + ".log")))
    check("params 에 ticker/interval/days/스냅샷/비용 포함", job["params"]["ticker"] == "TEST" and job["params"]["interval"] == "5m"
          and job["params"]["days"] == 5 and job["params"]["config_snapshot"]["params"]["n_percent"] == 0.3
          and job["params"]["fee_roundtrip_pct"] == 0.2)
    check("스냅샷에 비밀값 없음(Toss 키/비밀번호 키 없음)", "toss" not in json.dumps(job["params"]).lower() and "password" not in json.dumps(job["params"]).lower())
    print(f"      (참고) 소형 리플레이 {job['result']['data']['bars']}봉: 총 {elapsed:.1f}초, 계산 {job['result']['perf']['compute_sec']}초")
    check("완료 후 새 작업 시작 가능(409 해제)", replay.active_job_id() is None)

    # latest
    check("latest_done_job = 방금 작업", (replay.latest_done_job() or {}).get("job_id") == job_id)

    # --- 타임아웃: 오래 도는 자식을 kill ---
    replay._spawn_child = make_spawn("import time; time.sleep(60)")
    try:
        jid = replay.start_job(params, timeout_sec=1)
        proc = replay._procs[jid]
        time.sleep(1.4)
        jt = replay.get_job(jid)
        check("타임아웃: failed / error=timeout", jt["state"] == "failed" and jt["error"] == "timeout", str(jt["state"]) + str(jt["error"]))
        check("타임아웃: 자식 프로세스 kill 확인", proc.poll() is not None)
        check("타임아웃 후 409 해제", replay.active_job_id() is None)

        # --- 프로세스 사망(완료 기록 없이 종료) ---
        replay._spawn_child = make_spawn("import sys; sys.exit(3)")
        jid2 = replay.start_job(params, timeout_sec=60)
        replay._procs[jid2].wait(timeout=20)
        jd = replay.get_job(jid2)
        check("프로세스 사망: failed / error=process_exited(종료 코드 포함)", jd["state"] == "failed" and jd["error"] == "process_exited"
              and "3" in jd["message"], str(jd["error"]) + jd["message"])

        # --- 서버 재시작 정리 ---
        replay._spawn_child = make_spawn("import time; time.sleep(60)")
        jid3 = replay.start_job(params, timeout_sec=600)
        sl = replay._procs.pop(jid3)          # 재시작 시뮬레이션: 이 프로세스의 핸들을 잃은 상태
        n = replay.recover_on_startup()
        j3 = replay.get_job(jid3)
        sl.kill()
        check("서버 재시작 정리: running 으로 남은 작업 → failed/server_restarted", n == 1 and j3["state"] == "failed"
              and j3["error"] == "server_restarted", f"n={n} {j3['state']} {j3['error']}")
    finally:
        replay._spawn_child = orig_spawn

    # --- 자식 실행 함수 직접 호출: no_data / exception ---
    fresh_env()
    p_nodata = replay.make_params(test_config(), days=3, as_of=D(2026, 10, 8, 6, 0))
    replay._spawn_child = make_spawn("pass")
    try:
        jid = replay.start_job(p_nodata)
        replay._procs[jid].wait(timeout=20)
        replay.get_job(jid)                   # process_exited 로 정리됨 → 아래에서 새 작업으로 대체
    finally:
        replay._spawn_child = orig_spawn
    # 새 queued 작업 파일을 직접 만들어 replay_job.run 을 프로세스 없이 실행(Yahoo 는 404 목)
    jid_nd = "rp-20261008-060000-aaaa"
    job = {"job_id": jid_nd, "state": "queued", "progress": 0, "stage": "queued", "message": "", "error": None,
           "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0, "params": p_nodata, "result": None,
           "pid": None, "timeout_sec": 60, "created_ts": time.time(), "started_ts": None, "finished_ts": None}
    replay.write_job(job)
    mock404 = YahooMock(US_SPEC, set(), fail_symbols={"TEST"})
    with patched_requests(mock404):
        rc = replay_job.run(jid_nd)
    jn = replay.get_job(jid_nd)
    check("봉이 없으면 failed / error=no_data (한글 사유)", rc == 1 and jn["state"] == "failed" and jn["error"] == "no_data"
          and "봉" in jn["message"], f"{jn['state']} {jn['error']} {jn['message']}")

    jid_ex = "rp-20261008-060001-bbbb"
    job2 = dict(job, job_id=jid_ex, params=dict(p_nodata, interval="1h"))   # 지원하지 않는 interval → ValueError → exception
    replay.write_job(job2)
    with patched_requests(no_network):
        rc2 = replay_job.run(jid_ex)
    je = replay.get_job(jid_ex)
    check("예상 밖 예외는 failed / error=exception (프로세스는 정상 종료 코드 1)", rc2 == 1 and je["state"] == "failed" and je["error"] == "exception", str(je))

    # --- 보관 5개 + 최근 done 보호 ---
    fresh_env()
    ids = []
    for i in range(9):
        jid = f"rp-20261001-0000{i:02d}-{i:04x}"
        st = "done" if i == 1 else "failed"      # 가장 최근 성공이 오래된(5개 밖) 작업이라도 보호돼야 함
        replay.write_job({"job_id": jid, "state": st, "progress": 100, "stage": "done", "message": "", "error": None,
                          "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0,
                          "params": {}, "result": {} if st == "done" else None, "created_ts": 1.0, "finished_ts": 2.0})
        open(os.path.join(TMP, "replay", jid + ".log"), "w").write("x")
        ids.append(jid)
    replay._spawn_child = make_spawn("pass")
    try:
        new_id = replay.start_job(params)
    finally:
        replay._spawn_child = orig_spawn
    left = replay.list_job_ids()
    check("보관: 최근 4개 + 신규 1개 + 가장 최근 성공 1개(보호) 만 남음", len(left) == 6 and new_id in left and ids[1] in left
          and set(ids[-4:]) <= set(left), str(left))
    check("삭제된 작업의 .log 도 함께 삭제", not os.path.exists(os.path.join(TMP, "replay", ids[0] + ".log"))
          and os.path.exists(os.path.join(TMP, "replay", ids[-1] + ".log")))
    # 보호 대상이 없을 때는 정확히 5개
    fresh_env()
    for i in range(9):
        jid = f"rp-20261001-0000{i:02d}-{i:04x}"
        replay.write_job({"job_id": jid, "state": "done", "progress": 100, "stage": "done", "message": "", "error": None,
                          "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0,
                          "params": {}, "result": {}, "created_ts": 1.0, "finished_ts": 2.0})
    replay._spawn_child = make_spawn("pass")
    try:
        replay.start_job(params)
    finally:
        replay._spawn_child = orig_spawn
    check("보관: 최근 5개(신규 포함)", len(replay.list_job_ids()) == 5, str(replay.list_job_ids()))
    # 정리: 남은 자식 프로세스 종료
    for pr in replay._procs.values():
        if pr.poll() is None:
            pr.kill()


# ---------------------------------------------------------------------------
# T-R4  QA 경미 이슈 정리 (캐시 TTL/경계/실패, 보관 7일, 부분 적재, 자식 타임아웃, 고아 kill, job_id, bool)
# ---------------------------------------------------------------------------
def _sleeper(jid=None, marker=True):
    """replay_job 처럼 보이는(또는 아닌) 60초 대기 프로세스."""
    import subprocess
    args = [PY, "-c", "import time; time.sleep(60)"]
    if marker:
        args += ["core.vwap.replay_job", jid]
    return subprocess.Popen(args)


def _store_with_late_start(rng, drop_bars, until):
    """10/2, 10/6 은 정상, 10/5 는 첫 drop_bars 봉 누락, 10/7 은 until 까지."""
    frames = []
    for d, drop, unt in [(D(2026, 10, 2).date(), 0, None), (D(2026, 10, 5).date(), drop_bars, None),
                         (D(2026, 10, 6).date(), 0, None), (D(2026, 10, 7).date(), 0, until)]:
        t = session_times(US_SPEC, d, "5m", unt)[drop:]
        frames.append(synth_bars(rng, t))
    df = pd.concat(frames, ignore_index=True)
    sentinel = df.iloc[[-1]].copy()
    sentinel["time"] = sentinel["time"] + timedelta(minutes=5)
    bars_store.append_closed_bars("TEST", "5m", pd.concat([df, sentinel], ignore_index=True), "22:30", "toss")


def test_r4_cleanup():
    print("\nT-R4. QA 경미 이슈 — 캐시 재사용 조건 / 보관 정책 / 부분 적재 / 자식 타임아웃 / 고아 kill / job_id / bool")
    NOW = D(2026, 10, 8, 6, 0)
    rng = np.random.default_rng(5)
    stored = [D(2026, 10, 1).date(), D(2026, 10, 2).date(), D(2026, 10, 5).date(), D(2026, 10, 6).date()]
    allowed = {"2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07"}

    def meta_path():
        return bar_source._cache_paths("TEST", "5m", "20261008")[1]

    def edit_meta(**kw):
        with open(meta_path(), "r", encoding="utf-8") as f:
            m = json.load(f)
        m.update(kw)
        with open(meta_path(), "w", encoding="utf-8") as f:
            json.dump(m, f)

    # --- 1) 캐시 재사용 조건 ---
    fresh_env()
    write_store(rng, "TEST", "5m", stored)
    with patched_requests(YahooMock(US_SPEC, allowed)):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    with open(meta_path(), "r", encoding="utf-8") as f:
        m0 = json.load(f)
    check("캐시 meta 에 created_ts 기록", isinstance(m0.get("created_ts"), float), str(m0))
    mk = YahooMock(US_SPEC, allowed)
    with patched_requests(mk):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    check("(기준) TTL 안 + 경계 충족 → 재사용(요청 0회)", len(mk.calls) == 0)

    edit_meta(created_ts=time.time() - 3700)
    mk = YahooMock(US_SPEC, allowed)
    with patched_requests(mk):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    check("생성 후 1시간 초과 → 재사용 안 함(다시 요청)", len(mk.calls) >= 1)
    with open(meta_path(), "r", encoding="utf-8") as f:
        m1 = json.load(f)
    check("재요청 결과로 캐시 갱신(created_ts 최신)", time.time() - m1["created_ts"] < 60)

    mk = YahooMock(US_SPEC, allowed)
    with patched_requests(mk):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW + timedelta(hours=2))
    check("같은 날이라도 meta.to 가 이번 요청의 마지막 마감 봉 경계를 못 덮으면(now +2h) 재사용 안 함", len(mk.calls) >= 1)

    fresh_env()
    write_store(rng, "TEST", "5m", stored)
    mk = YahooMock(US_SPEC, allowed, fail_symbols={"TEST"})
    with patched_requests(mk):
        df_f, meta_f = bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    cdir = bar_source.cache_dir()
    check("일부/전체 구간 조회 실패(경고 발생) 결과는 캐시에 쓰지 않음", not os.path.exists(cdir) or not os.listdir(cdir),
          str(os.listdir(cdir) if os.path.exists(cdir) else ""))
    check("실패 경고는 meta.warnings 에 남음", any("Yahoo" in w for w in meta_f["warnings"]))
    # meta.partial 표시가 있으면 재사용 대상에서 제외
    fresh_env()
    write_store(rng, "TEST", "5m", stored)
    with patched_requests(YahooMock(US_SPEC, allowed)):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    edit_meta(partial=True)
    mk = YahooMock(US_SPEC, allowed)
    with patched_requests(mk):
        bar_source.load_bars("TEST", "5m", 7, "22:30", now=NOW)
    check("meta.partial=true 캐시는 재사용 안 함", len(mk.calls) >= 1)

    # --- 2) 7일 보관 정책 ---
    fresh_env()
    cdir = bar_source.cache_dir()
    os.makedirs(cdir, exist_ok=True)
    old_t = time.time() - 8 * 86400
    names_old = ["OLD_5m_20260901.csv", "OLD_5m_20260901.meta.json"]
    names_new = ["NEW_5m_20261007.csv", "NEW_5m_20261007.meta.json"]
    for n in names_old + names_new + ["keep_old.txt"]:
        with open(os.path.join(cdir, n), "w") as f:
            f.write("x")
    for n in names_old + ["keep_old.txt"]:
        os.utime(os.path.join(cdir, n), (old_t, old_t))
    os.makedirs(os.path.join(cdir, "sub.csv"), exist_ok=True)       # 폴더는 건드리지 않음
    outside = os.path.join(TMP, "outside_old.csv")                  # 캐시 폴더 밖 파일은 건드리지 않음
    with open(outside, "w") as f:
        f.write("x")
    os.utime(outside, (old_t, old_t))
    bar_source._write_cache("TEST", "5m", "20261008", pd.DataFrame(columns=bar_source.BAR_COLUMNS),
                            NOW - timedelta(days=1), NOW, [])
    left = set(os.listdir(cdir))
    check("_write_cache 시 7일 지난 csv/meta.json 삭제, 최근 파일은 유지",
          not (set(names_old) & left) and set(names_new) <= left, str(sorted(left)))
    check("대상 외(.txt, 하위 폴더, 캐시 폴더 밖 파일)는 삭제하지 않음",
          "keep_old.txt" in left and "sub.csv" in left and os.path.exists(outside))
    orig_remove = os.remove
    try:
        with open(os.path.join(cdir, "OLD2.csv"), "w") as f:
            f.write("x")
        os.utime(os.path.join(cdir, "OLD2.csv"), (old_t, old_t))

        def bad_remove(path, *a, **k):
            raise PermissionError("잠김")
        os.remove = bad_remove
        w = []
        bar_source._write_cache("TEST", "5m", "20261008", pd.DataFrame(columns=bar_source.BAR_COLUMNS), NOW - timedelta(days=1), NOW, w)
    finally:
        os.remove = orig_remove
    check("삭제 실패는 무시하고 캐시 저장은 계속", os.path.exists(bar_source._cache_paths("TEST", "5m", "20261008")[0]) and not w, str(w))

    # --- 3) 부분 적재 세션 경고 ---
    for drop, expect_partial in ((3, False), (4, True)):
        fresh_env()
        _store_with_late_start(np.random.default_rng(3), drop, NOW - timedelta(minutes=5))
        with patched_requests(no_network):
            _df, mp = bar_source.load_bars("TEST", "5m", 3, "22:30", now=NOW)
        got = mp.get("partial_sessions") == ["2026-10-05"]
        tag_w = any(bar_source.PARTIAL_LABEL in w for w in mp["warnings"])
        tag_g = any(g.startswith("2026-10-05") and bar_source.PARTIAL_LABEL in g for g in mp["gaps"])
        tag_l = bar_source.PARTIAL_LABEL in mp["limits"]
        if expect_partial:
            check(f"첫 봉이 시작+{drop}봉(허용 3봉 초과) → 부분 적재 표시(warnings/gaps/limits)", got and tag_w and tag_g and tag_l, str(mp))
        else:
            check(f"첫 봉이 시작+{drop}봉(허용 오차 이내) → 표시 없음", mp.get("partial_sessions") == [] and not tag_w and not tag_g,
                  str(mp.get("partial_sessions")))
        check("Yahoo 보충·병합 없음(source_breakdown.yahoo=0)", mp["source_breakdown"]["yahoo"] == 0)

    # --- 4) 자식 프로세스 자체 타임아웃 (워치독) ---
    fresh_env()
    params = replay.make_params(test_config(), days=5, as_of=NOW)
    jid = "rp-20261008-060000-aaaa"
    replay.write_job({"job_id": jid, "state": "queued", "progress": 0, "stage": "queued", "message": "", "error": None,
                      "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0.0, "params": params,
                      "result": None, "pid": None, "timeout_sec": 1.0, "created_ts": time.time(), "started_ts": None,
                      "finished_ts": None})
    exits = []
    orig_exit, orig_load = replay_job._hard_exit, bar_source.load_bars
    replay_job._hard_exit = lambda code: exits.append(code)

    def slow_load(*a, **k):
        time.sleep(2.5)
        raise replay.NoDataError("느린 로딩 시뮬레이션")
    bar_source.load_bars = slow_load
    try:
        t0 = time.time()
        replay_job.run(jid)
    finally:
        replay_job._hard_exit, bar_source.load_bars = orig_exit, orig_load
    jt = replay.read_job(jid)
    check("자식 워치독: timeout_sec 경과 → failed/timeout 기록 후 스스로 종료(_hard_exit(1) 호출)",
          jt["state"] == "failed" and jt["error"] == "timeout" and exits == [1], f"{jt['state']} {jt['error']} {exits}")
    # 정상 완료 케이스: 워치독 취소 확인(타임아웃 0.5초인데 즉시 끝나는 작업)
    jid_b = "rp-20261008-060001-bbbb"
    replay.write_job({"job_id": jid_b, "state": "queued", "progress": 0, "stage": "queued", "message": "", "error": None,
                      "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0.0, "params": params,
                      "result": None, "pid": None, "timeout_sec": 0.5, "created_ts": time.time(), "started_ts": None,
                      "finished_ts": None})
    exits.clear()
    replay_job._hard_exit = lambda code: exits.append(code)
    bar_source.load_bars = lambda *a, **k: (_ for _ in ()).throw(replay.NoDataError("즉시 실패"))
    try:
        replay_job.run(jid_b)
        time.sleep(0.9)
    finally:
        replay_job._hard_exit, bar_source.load_bars = orig_exit, orig_load
    check("작업이 먼저 끝나면 워치독 취소(타임아웃 이후에도 종료 호출 없음)", exits == [] and replay.read_job(jid_b)["error"] == "no_data", str(exits))

    # --- 5) recover_on_startup: 남은 자식 kill ---
    fresh_env()
    orig_spawn = replay._spawn_child
    procs = []
    try:
        def mk_job(jid_, proc):
            j = {"job_id": jid_, "state": "running", "progress": 10, "stage": "loading_data", "message": "", "error": None,
                 "created_at": "", "started_at": None, "finished_at": None, "elapsed_sec": 0.0, "params": params,
                 "result": None, "pid": proc.pid, "timeout_sec": 600.0, "created_ts": time.time(),
                 "started_ts": time.time(), "finished_ts": None}
            replay.write_job(j)

        ja, jb, jc = "rp-20261008-060010-aaa1", "rp-20261008-060011-aaa2", "rp-20261008-060012-aaa3"
        pa, pb, pc = _sleeper(ja), _sleeper(None, marker=False), _sleeper(jc)
        procs += [pa, pb, pc]
        time.sleep(1.0)
        mk_job(ja, pa)
        mk_job(jb, pb)
        mk_job(jc, pc)
        st, cmd = replay._pid_cmdline(pa.pid)
        check("명령줄 조회 가능한 플랫폼: replay_job 마커 확인(%s)" % sys.platform, st == "ok" and "core.vwap.replay_job" in cmd and ja in cmd,
              f"{st} {cmd[:120]}")
        orig_cmd = replay._pid_cmdline
        # jc: 명령줄 확인 불가 → kill 금지
        replay._pid_cmdline = lambda pid: ("unknown", "") if pid == pc.pid else orig_cmd(pid)
        try:
            n = replay.recover_on_startup()
        finally:
            replay._pid_cmdline = orig_cmd
        time.sleep(0.8)
        check("recover: 3건 모두 failed/server_restarted 정리", n == 3 and all(
            replay.read_job(x)["error"] == "server_restarted" for x in (ja, jb, jc)), str(n))
        check("pid 가 살아 있는 replay_job(명령줄 일치) → kill", pa.poll() is not None)
        check("replay_job 이 아닌 프로세스(pid 재사용 가정) → kill 하지 않음", pb.poll() is None)
        check("명령줄 확인 불가(unknown) → kill 하지 않고 로그만", pc.poll() is None)
        check("이미 죽은 pid / pid 없음 → 오류 없이 정리", replay._kill_orphan({"job_id": ja, "pid": 2 ** 30}) in ("dead", "skipped")
              and replay._kill_orphan({"job_id": ja, "pid": None}) == "skipped")
        check("자기 자신 pid 는 절대 kill 대상 아님", replay._kill_orphan({"job_id": ja, "pid": os.getpid()}) == "skipped")
    finally:
        replay._spawn_child = orig_spawn
        for pr in procs:
            try:
                if pr.poll() is None:
                    pr.kill()
            except Exception:
                pass

    # --- 6) job_id fullmatch ---
    check("JOB_ID: 정상 id 읽기 가능 / 개행 꼬리·접두·접미 문자는 거부",
          replay.JOB_ID_RE.fullmatch("rp-20261008-060000-abcd") is not None
          and replay.read_job("rp-20261008-060000-abcd\n") is None
          and all(replay.JOB_ID_RE.fullmatch(x) is None for x in
                  ("rp-20261008-060000-abcd\n", "rp-20261008-060000-abcde", "xrp-20261008-060000-abcd")))
    fresh_env()
    os.makedirs(replay.replay_dir(), exist_ok=True)
    orig_ld = os.listdir
    os.listdir = lambda d: ["rp-20261008-060000-abcd\n.json", "rp-20261008-060001-abcd.json"]   # Windows 는 개행 파일명을 못 만들어 목록만 흉내
    try:
        ids = replay.list_job_ids()
    finally:
        os.listdir = orig_ld
    check("list_job_ids 도 개행 꼬리 파일명 제외", ids == ["rp-20261008-060001-abcd"], str(ids))
    # --- 7) _to_bool: 구/신 _cast_config_value 비교 ---
    import api.vwap_api as vapi

    def old_bool(v):                 # 변경 전(3단계 초기) 동작: 문자열은 "true" 만 True
        return v if isinstance(v, bool) else str(v).lower() == "true"
    strs = ["true", "True", "TRUE", "false", "False", "1", "0", "yes", "no", "y", "n", "on", "off", "", "t", "tru"]
    diffs = [x for x in strs if vapi._cast_config_value("shadow_enabled", x) != old_bool(x)]
    check("문자열 bool: 구/신 _cast_config_value 동일(true 만 True)", not diffs, str(diffs))
    check("\"1\"/\"yes\"/\"on\"/\"y\" 는 False", not any(vapi._cast_config_value(k, v) for k in ("shadow_enabled", "use_adx_filter")
                                                      for v in ("1", "yes", "on", "y", "Y", "ON")))
    check("JSON bool 은 그대로, 숫자는 기존(bool(val)) 처리 유지",
          vapi._cast_config_value("bars_store_enabled", True) is True and vapi._cast_config_value("bars_store_enabled", False) is False
          and vapi._cast_config_value("shadow_enabled", 1) is True and vapi._cast_config_value("shadow_enabled", 0) is False)
    check("use_*/discord_notify 키(접미어 분기)도 같은 규칙", vapi._cast_config_value("discord_notify", "On") is False
          and vapi._cast_config_value("discord_notify", "TRUE") is True)


# ---------------------------------------------------------------------------
# T-R3
# ---------------------------------------------------------------------------
SUMMARY_KEYS = ["trades", "win_rate_pct", "avg_win_pct", "avg_loss_pct", "payoff_ratio", "expectancy_pct", "expectancy_amount",
                "net_pnl", "total_return_pct", "mdd_pct", "profit_factor", "avg_bars_held", "t_stat", "verdict"]
TRADE_KEYS = ["entry_time", "exit_time", "entry_price", "exit_price", "qty", "net_pnl", "ret_pct", "bars_held", "exit_reason", "fees"]


def schema_ok(r):
    try:
        return (set(r) >= {"summary", "benchmark", "equity", "trades", "exit_reasons", "data", "assumptions", "warnings"}
                and set(r["summary"]) == set(SUMMARY_KEYS)
                and set(r["benchmark"]) == {"buy_hold_return_pct", "buy_hold_net_pnl"}
                and all(set(p) == {"t", "strategy", "buy_hold"} for p in r["equity"])
                and all(set(t) == set(TRADE_KEYS) for t in r["trades"])
                and all(set(x) == {"reason", "count", "avg_ret_pct", "sum_ret_pct"} for x in r["exit_reasons"])
                and set(r["data"]) >= {"bars", "sessions", "from", "to", "interval", "source_breakdown", "gaps", "limits"}
                and set(r["data"]["source_breakdown"]) == {"bars_store", "yahoo"}
                and set(r["assumptions"]) >= {"fee_roundtrip_pct", "slippage_roundtrip_pct", "limit_fill_buffer_pct", "fill_model",
                                              "notional", "not_modeled"}
                and isinstance(r["warnings"], list))
    except Exception:
        return False


def test_r3_result():
    print("\nT-R3. 결과 스키마 / verdict / Buy&Hold / run_compare 일치")
    from backtest.engine import CostModel, run_backtest, summarize, Trade
    from backtest.indicators import add_indicators
    from backtest.strategies import S0_Current
    from core.vwap.replay_engine_adapter import build_engine_frame
    from core.vwap.strategies import S0CurrentStrategy, StrategyContext

    ASOF = D(2026, 10, 8, 6, 0)
    fresh_env()
    prepare_store_for_replay(5, "5m", ASOF, seed=21)
    with patched_requests(no_network):
        bars, meta = bar_source.load_bars("TEST", "5m", 5, "22:30", now=ASOF)
    params = replay.make_params(test_config(), days=5, fee_roundtrip_pct=0.2, slippage_roundtrip_pct=0.05,
                                limit_fill_buffer_pct=0.0, as_of=ASOF)
    emitted = []
    result = replay.compute_result(bars, meta, params, progress=lambda s, p, m: emitted.append((s, p)))
    n_bars = len(bars)
    print(f"      (참고) 합성 {n_bars}봉(5m×5일): 거래 {result['summary']['trades']}건, perf={result['perf']}")

    check("결과 스키마 필드 전부 존재(설계 §4.4)", schema_ok(result))
    try:
        json.dumps(result, allow_nan=False)
        strict = True
    except ValueError:
        strict = False
    check("JSON 엄격 직렬화 가능(NaN/inf 없음)", strict)
    check("equity ≤ 500점, 첫·마지막 봉 포함, 시각 형식 YYYY-MM-DD HH:MM",
          2 <= len(result["equity"]) <= 500 and result["equity"][0]["t"] == bars["time"].iloc[0].strftime("%Y-%m-%d %H:%M")
          and result["equity"][-1]["t"] == bars["time"].iloc[-1].strftime("%Y-%m-%d %H:%M"), str(len(result["equity"])))
    stages = [s for s, _ in emitted]
    check("단계 알림: preparing → simulating → summarizing 순서, 진행률 단조 증가",
          stages[0] == "preparing" and stages[-1] == "summarizing" and "simulating" in stages
          and all(b[1] >= a[1] for a, b in zip(emitted, emitted[1:])), str(emitted[:6]))

    # --- 연구 경로(run_compare 와 같은 조건)와 수치 일치 ---
    ctx = StrategyContext(ticker="TEST", market="US", interval="5m", reset_time="22:30", start_time="",
                          params=params["config_snapshot"]["params"])
    S0 = S0CurrentStrategy()
    frame = build_engine_frame(S0, bars[["time", "open", "high", "low", "close", "volume"]], ctx)
    ref_in = bars[["time", "open", "high", "low", "close", "volume"]].copy()
    ref_in["session_date"] = frame["session_date"].to_numpy()
    ref = add_indicators(ref_in)
    cost = CostModel(0.2, 0.05, 0.0)
    r_ref = run_backtest(ref, S0_Current(0.3, 0.5, 1.0), cost, initial_equity=10000.0, k_percent=50.0)
    s_ref = summarize(r_ref)
    sm = result["summary"]
    pairs = [("거래수", "trades"), ("승률%", "win_rate_pct"), ("평균이익%", "avg_win_pct"), ("평균손실%", "avg_loss_pct"),
             ("손익비", "payoff_ratio"), ("기대값%/거래", "expectancy_pct"), ("기대값$/거래", "expectancy_amount"),
             ("총순손익$", "net_pnl"), ("총수익률%", "total_return_pct"), ("MDD%", "mdd_pct"), ("PF", "profit_factor"),
             ("평균보유봉", "avg_bars_held"), ("t-stat", "t_stat")]
    bad = []
    for ko, en in pairs:
        a = s_ref[ko]
        a = None if (isinstance(a, float) and a != a) else a
        if a != sm[en]:
            bad.append((ko, a, sm[en]))
    check("run_compare 경로(S0_Current + add_indicators, 같은 세션 라벨/비용)와 요약 수치 전부 일치: "
          f"{len(pairs) - len(bad)}/{len(pairs)} (거래 {sm['trades']}건)", not bad and sm["trades"] > 0, str(bad))
    ref_tr = [{"exit_reason": t.exit_reason, "net_pnl": round(float(t.net_pnl), 2)} for t in r_ref.trades][::-1]
    got_tr = [{"exit_reason": t["exit_reason"], "net_pnl": t["net_pnl"]} for t in result["trades"]]
    check("거래 목록(최신순)이 연구 경로와 같음", ref_tr[:len(got_tr)] == got_tr, "")
    eq_ref = r_ref.equity.to_numpy(float)
    check("자산곡선 마지막 값 == 연구 경로 자산곡선 마지막 값, 초기자본+순손익과는 0.1% 이내(엔진이 진입 수수료를 체결가 기준/거래 기록은 원가 기준으로 달리 계산해 미세 차이)",
          abs(result["equity"][-1]["strategy"] - eq_ref[-1]) < 0.01
          and abs(result["equity"][-1]["strategy"] - (10000.0 + sm["net_pnl"])) < 10.0,
          f"{result['equity'][-1]['strategy']} vs ref {eq_ref[-1]:.2f} vs init+net {10000.0 + sm['net_pnl']:.2f}")
    check("exit_reasons 합계 == 거래수, 사유는 LIMIT_SELL/STOP_LOSS_MKT/DATA_END 중",
          sum(x["count"] for x in result["exit_reasons"]) == sm["trades"]
          and {x["reason"] for x in result["exit_reasons"]} <= {"LIMIT_SELL", "STOP_LOSS_MKT", "DATA_END"},
          str(result["exit_reasons"]))

    # --- Buy&Hold: 독립 계산 ---
    o = frame["open"].to_numpy(float)
    c = frame["close"].to_numpy(float)
    buy = cost.buy_price(o[30])
    qty = float(int(5000.0 / buy))
    sell = cost.sell_price(c[-1])
    net = (sell - buy) * qty - cost.fee(buy * qty) - cost.fee(sell * qty)
    bm = result["benchmark"]
    check("Buy&Hold: warmup 이후 첫 봉 시가 매수 → 마지막 종가 매도, 같은 명목금액·비용(독립 계산과 일치)",
          abs(bm["buy_hold_net_pnl"] - round(net, 2)) < 0.011 and abs(bm["buy_hold_return_pct"] - round(net / 10000.0 * 100, 3)) < 0.001,
          f"{bm} vs {net:.2f}")
    check("B&H 곡선: 마지막 값 == 초기자본 + B&H 순손익, warmup 이전은 초기자본",
          abs(result["equity"][-1]["buy_hold"] - (10000.0 + net)) < 0.02 and result["equity"][0]["buy_hold"] == 10000.0)
    try:   # run_compare 의 benchmark_row 와도 대조(같은 구간: warmup 이후)
        from backtest.run_compare import benchmark_row
        br = benchmark_row(frame.iloc[30:].reset_index(drop=True), cost, equity=10000.0, k=50.0)
        check("run_compare.benchmark_row 와 B&H 순손익 일치", abs(br["총순손익$"] - bm["buy_hold_net_pnl"]) < 0.011, str(br["총순손익$"]))
    except ImportError as e:
        check("run_compare.benchmark_row import 가능", False, str(e))

    # --- 가정/기타 ---
    a = result["assumptions"]
    check("assumptions: 비용 반영 + not_modeled 에 요청된 2항목 포함",
          a["fee_roundtrip_pct"] == 0.2 and a["slippage_roundtrip_pct"] == 0.05 and a["notional"] == 5000.0
          and "거래 시작 대기 판단 시각 = 봉 마감 시각" in a["not_modeled"]
          and "예산: 고정 명목금액(현금 부족 시 축소 없음)" in a["not_modeled"] and len(a["not_modeled"]) == 7, str(a["not_modeled"]))
    check("data 메타: bars/sessions/interval/source_breakdown 반영", result["data"]["bars"] == n_bars and result["data"]["interval"] == "5m"
          and result["data"]["source_breakdown"]["bars_store"] == n_bars and result["data"]["sessions"] == 4,
          str(result["data"]))

    # --- 사용자 지정 비용 반영 ---
    p2 = replay.make_params(test_config(), days=5, fee_roundtrip_pct=0.0, slippage_roundtrip_pct=0.0, limit_fill_buffer_pct=0.0, as_of=ASOF)
    r2 = replay.compute_result(bars, meta, p2)
    check("비용 0 이면 순손익이 비용 반영(0.2/0.05)보다 큼 + assumptions 반영", r2["summary"]["net_pnl"] > sm["net_pnl"]
          and r2["assumptions"]["fee_roundtrip_pct"] == 0.0)

    # --- verdict 규칙 ---
    jv = replay.judge_verdict
    check("verdict: 30건 미만 insufficient (t_stat 무관)", jv(29, 5.0, 1.0) == "insufficient" and jv(0, None, None) == "insufficient")
    check("verdict: t<=-2 negative, t>=2 & 기대값>0 positive, 그 외 inconclusive",
          jv(30, -2.0, -0.5) == "negative" and jv(30, 2.0, 0.1) == "positive" and jv(30, 2.5, -0.1) == "inconclusive"
          and jv(100, 1.99, 0.3) == "inconclusive" and jv(50, None, None) == "inconclusive" and jv(50, -1.9, -0.2) == "inconclusive")
    check("거래 30건 미만이면 warnings 에 '표본 부족 — 판단 불가'", (sm["trades"] >= 30) or "표본 부족 — 판단 불가" in result["warnings"])
    check("verdict 값이 summary 와 일치(insufficient|negative|inconclusive|positive)", sm["verdict"] in
          ("insufficient", "negative", "inconclusive", "positive") and sm["verdict"] == jv(sm["trades"], sm["t_stat"], sm["expectancy_pct"]))

    # --- trades 500건 상한 / 최신순 ---
    t0 = pd.Timestamp("2026-01-01 10:00")
    fake = [Trade(t0 + pd.Timedelta(minutes=i), t0 + pd.Timedelta(minutes=i + 1), 100.0, 101.0, 1.0, 1.0, 0.1, 0.9, 0.9, 1,
                  "LIMIT_SELL") for i in range(700)]
    ft = replay.format_trades(fake)
    check("trades 는 최근 500건만, 최신순", len(ft) == 500 and ft[0]["entry_time"] == (t0 + pd.Timedelta(minutes=699)).strftime("%Y-%m-%d %H:%M")
          and ft[-1]["entry_time"] == (t0 + pd.Timedelta(minutes=200)).strftime("%Y-%m-%d %H:%M"))
    check("downsample: 500점 초과 입력을 500점 이하로, 끝점 보존", len(replay._downsample_idx(20000)) <= 500
          and replay._downsample_idx(20000)[-1] == 19999 and replay._downsample_idx(20000)[0] == 0)

    # --- 거래 0건(평탄한 가격) ---
    flat_t = pd.date_range("2026-10-05 22:30", periods=300, freq="5min")
    flat = pd.DataFrame({"time": flat_t, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0})
    r0 = replay.compute_result(flat, {"from": "2026-10-05", "to": "2026-10-06"}, params)
    check("거래 0건: 예외 없이 결과 생성, 통계는 null, verdict=insufficient, JSON 직렬화 가능",
          r0["summary"]["trades"] == 0 and r0["summary"]["t_stat"] is None and r0["summary"]["verdict"] == "insufficient"
          and r0["trades"] == [] and r0["exit_reasons"] == [] and schema_ok(r0) and json.dumps(r0, allow_nan=False) is not None)

    # --- initial_balance 0 폴백 ---
    p0 = replay.make_params(test_config(real_initial_balance=0.0), days=5, as_of=ASOF)
    rb = replay.compute_result(bars, meta, p0)
    check("initial_balance=0 → 기본 자본으로 폴백 + 경고 + notional 반영",
          rb["assumptions"]["initial_equity"] == replay.DEFAULT_INITIAL_EQUITY and any("초기 자본" in w for w in rb["warnings"])
          and rb["assumptions"]["notional"] == replay.DEFAULT_INITIAL_EQUITY * 0.5, str(rb["assumptions"]))

    # --- 봉 부족 ---
    try:
        replay.compute_result(bars.iloc[:20], meta, params)
        nd = False
    except replay.NoDataError:
        nd = True
    check("봉이 너무 적으면 NoDataError", nd)

    # --- make_params 검증 ---
    def bad_params(**kw):
        try:
            replay.make_params(test_config(), **kw)
            return False
        except ValueError:
            return True
    check("make_params 검증: days 0/-1/1000/'abc'/1.5/True, interval 1h, 수수료 음수/NaN 거부",
          all([bad_params(days=0), bad_params(days=-1), bad_params(days=1000), bad_params(days="abc"), bad_params(days=1.5),
               bad_params(days=True), bad_params(interval="1h"), bad_params(fee_roundtrip_pct=-0.1),
               bad_params(slippage_roundtrip_pct=float("nan")), bad_params(limit_fill_buffer_pct="x")]))
    check("make_params: 일수 문자열 '10' 허용, interval 생략 시 real_interval", replay.make_params(test_config(), days="10")["days"] == 10
          and replay.make_params(test_config(real_interval="1m"), days=3)["interval"] == "1m")



# ---------------------------------------------------------------------------
# T-A1
# ---------------------------------------------------------------------------
def test_a1_api():
    print("\nT-A1. 리플레이 API (+ 설정 키 캐스팅)")
    fresh_env()
    from flask import Flask
    import api.vwap_api as vapi
    from core.vwap.crypto import VwapCrypto

    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "api", "templates"))
    app.register_blueprint(vapi.vwap_bp, url_prefix="/vwap")
    anon = app.test_client()
    cli = app.test_client()
    cli.set_cookie("vwap_session", VwapCrypto.generate_session_token("admin"))

    # 설정: 임시 DATA_DIR 의 config 에 테스트용 REAL 설정 저장 (자격증명 비움 → mock)
    VwapConfigManager.save_config(test_config(toss_client_id="", toss_client_secret="", toss_account_seq=""))

    # 401
    r1 = anon.post("/vwap/api/replay", json={"days": 5})
    r2 = anon.get("/vwap/api/replay/rp-20261008-060000-aaaa")
    r3 = anon.get("/vwap/api/replay/latest")
    check("세션 없으면 401 (POST /replay, GET /replay/<id>, GET /replay/latest)",
          (r1.status_code, r2.status_code, r3.status_code) == (401, 401, 401) and r1.get_json()["reason"] == "unauthorized")

    # latest: 없음
    r = cli.get("/vwap/api/replay/latest")
    check("latest: 작업이 없으면 200 + job=null", r.status_code == 200 and r.get_json() == {"status": "success", "job": None})

    # 404
    r_a = cli.get("/vwap/api/replay/rp-20261008-060000-aaaa")
    r_b = cli.get("/vwap/api/replay/..%5C..%5Cvwap_config")
    r_c = cli.get("/vwap/api/replay/not-a-job")
    check("없는 job_id / 형식이 틀린 job_id(경로 탈출 시도 포함) → 404", r_a.status_code == 404 and r_b.status_code == 404
          and r_c.status_code == 404 and r_a.get_json()["reason"] == "not_found")

    # 400
    cases = [{"days": 0}, {"days": "abc"}, {"days": 1000}, {"days": 1.5}, {"interval": "1h"}, {"fee_roundtrip_pct": -1},
             {"slippage_roundtrip_pct": "x"}, {"limit_fill_buffer_pct": 99}]
    codes = [(cli.post("/vwap/api/replay", json=c).status_code, cli.post("/vwap/api/replay", json=c).get_json()) for c in cases]
    check("잘못된 파라미터 8종 → 400 invalid_params + 한글 message",
          all(c == 400 and j["status"] == "failed" and j["reason"] == "invalid_params" and j["message"] for c, j in codes), str(codes[:2]))
    rl = cli.post("/vwap/api/replay", data="[1,2]", content_type="application/json")
    check("본문이 JSON 객체가 아니면 400", rl.status_code == 400)

    # 202 / 409 / GET (자식은 잠자는 프로세스로 대체: 네트워크·계산 없이 API 동작만 검증)
    orig_spawn = replay._spawn_child
    replay._spawn_child = make_spawn("import time; time.sleep(30)")
    try:
        ok = cli.post("/vwap/api/replay", json={"days": 5, "interval": "5m", "fee_roundtrip_pct": 0.3})
        body = ok.get_json()
        check("정상 요청 → 202 {status:accepted, job_id}", ok.status_code == 202 and body["status"] == "accepted"
              and replay.JOB_ID_RE.match(body["job_id"]) is not None, str(body))
        jid = body["job_id"]
        dup = cli.post("/vwap/api/replay", json={"days": 5})
        check("실행 중 재요청 → 409 {status:failed, reason:job_running, job_id=실행 중 작업}", dup.status_code == 409
              and dup.get_json() == {"status": "failed", "reason": "job_running", "job_id": jid}, str(dup.get_json()))
        g = cli.get(f"/vwap/api/replay/{jid}")
        gj = g.get_json()
        check("GET /replay/<id> → 200 success + job(state queued/running, params.ticker, 비용, 스냅샷, result=null)",
              g.status_code == 200 and gj["status"] == "success" and gj["job"]["state"] in ("queued", "running")
              and gj["job"]["params"]["ticker"] == "TEST" and gj["job"]["params"]["fee_roundtrip_pct"] == 0.3
              and gj["job"]["params"]["days"] == 5 and gj["job"]["result"] is None
              and set(gj["job"]) >= {"job_id", "state", "progress", "stage", "message", "error", "created_at", "started_at",
                                     "finished_at", "elapsed_sec", "params", "result"}, str(gj)[:300])
        check("응답에 내부 필드·비밀값 없음", not any(k in gj["job"] for k in replay._INTERNAL_KEYS)
              and "toss_client_secret" not in json.dumps(gj) and "admin_password" not in json.dumps(gj))
        # 타임아웃 설정 반영: replay_timeout_sec 를 15 로 저장 → 다음 작업 상태 파일의 timeout 은 설정값
        for p in replay._procs.values():
            p.kill()
        replay._procs.clear()
        replay.recover_on_startup()
    finally:
        replay._spawn_child = orig_spawn

    # latest: done 작업 하나 만들어 두고 조회
    done_id = "rp-20261008-070000-abcd"
    replay.write_job({"job_id": done_id, "state": "done", "progress": 100, "stage": "done", "message": "완료", "error": None,
                      "created_at": "2026-10-08 07:00:00", "started_at": None, "finished_at": None, "elapsed_sec": 1.0,
                      "params": {"ticker": "TEST"}, "result": {"summary": {"trades": 0}}, "created_ts": 1.0, "finished_ts": 2.0})
    lj = cli.get("/vwap/api/replay/latest").get_json()
    check("latest: 가장 최근 done 작업 반환(실패 작업은 건너뜀)", lj["status"] == "success" and lj["job"]["job_id"] == done_id
          and lj["job"]["result"] == {"summary": {"trades": 0}})

    # replay_timeout_sec 설정이 작업에 반영되는지
    cli.post("/vwap/api/config", json={"replay_timeout_sec": 45})
    replay._spawn_child = make_spawn("import time; time.sleep(30)")
    try:
        ok = cli.post("/vwap/api/replay", json={"days": 3})
        raw = replay.read_job(ok.get_json()["job_id"])
        check("replay_timeout_sec 설정(45초)이 작업의 제한시간으로 사용됨", raw["timeout_sec"] == 45.0, str(raw["timeout_sec"]))
    finally:
        for p in replay._procs.values():
            p.kill()
        replay._spawn_child = orig_spawn

    # --- 설정 키 캐스팅 ---
    def post_cfg(d):
        return cli.post("/vwap/api/config", json=d)

    rr = post_cfg({"shadow_enabled": "False", "bars_store_enabled": "false", "shadow_fee_roundtrip_pct": "0.3",
                   "shadow_price_tolerance_pct": 0.1, "replay_timeout_sec": "120"})
    saved = json.load(open(cm.CONFIG_PATH, encoding="utf-8"))
    check("3단계 설정 저장: bool 문자열 'False'/'false' → JSON false(문자열 아님)", rr.status_code == 200
          and saved["shadow_enabled"] is False and saved["bars_store_enabled"] is False, str((saved["shadow_enabled"], saved["bars_store_enabled"])))
    check("3단계 설정 저장: 숫자 캐스팅(float/int)", saved["shadow_fee_roundtrip_pct"] == 0.3 and isinstance(saved["shadow_fee_roundtrip_pct"], float)
          and saved["shadow_price_tolerance_pct"] == 0.1 and saved["replay_timeout_sec"] == 120 and isinstance(saved["replay_timeout_sec"], int))
    post_cfg({"shadow_enabled": "true", "bars_store_enabled": True})
    saved = json.load(open(cm.CONFIG_PATH, encoding="utf-8"))
    check("'true'/True → true", saved["shadow_enabled"] is True and saved["bars_store_enabled"] is True)
    check("load_config 로 읽어도 bool/숫자 타입 유지", isinstance(VwapConfigManager.load_config()["shadow_enabled"], bool)
          and VwapConfigManager.load_config()["replay_timeout_sec"] == 120)
    bad_cfgs = [{"replay_timeout_sec": "abc"}, {"replay_timeout_sec": 5}, {"replay_timeout_sec": 99999}, {"replay_timeout_sec": 30.5},
                {"shadow_fee_roundtrip_pct": "x"}, {"shadow_price_tolerance_pct": -1}, {"shadow_fee_roundtrip_pct": 50}]
    before = open(cm.CONFIG_PATH, "rb").read()
    codes = [post_cfg(c).status_code for c in bad_cfgs]
    check("잘못된 3단계 설정 값 7종 → 400, 설정 파일은 바뀌지 않음", codes == [400] * 7 and open(cm.CONFIG_PATH, "rb").read() == before, str(codes))
    # 기존 키 동작 불변
    post_cfg({"real_use_adx_filter": "False", "real_n_percent": "0.7", "real_adx_period": "10", "real_discord_notify": "false",
              "real_ticker": "AAPL", "unknown_key": "x"})
    saved = json.load(open(cm.CONFIG_PATH, encoding="utf-8"))
    check("기존 키 캐스팅 불변(bool 'False'→false, float, int, str) + 허용 목록 밖 키 무시",
          saved["real_use_adx_filter"] is False and saved["real_n_percent"] == 0.7 and saved["real_adx_period"] == 10
          and saved["real_discord_notify"] is False and saved["real_ticker"] == "AAPL" and "unknown_key" not in saved)
    # 모든 bool 계열 키에 'False' 문자열이 저장되지 않음(전수)
    str_bools = [k for k, v in saved.items() if v in ("False", "True", "false", "true")]
    check("저장된 설정에 'False'/'True' 문자열 값이 하나도 없음", not str_bools, str(str_bools))


# ---------------------------------------------------------------------------
# T-PERF
# ---------------------------------------------------------------------------
def test_perf():
    print("\nT-PERF. 1분봉 2만 봉 상당 리플레이 (자식 프로세스 실측)")
    ASOF = D(2026, 10, 21, 6, 0)
    fresh_env()
    rng = np.random.default_rng(5)
    days = 20
    start = US_SPEC.session_start(ASOF - timedelta(days=days)).date()
    dates = weekdays_between(start, US_SPEC.session_start(ASOF).date())
    n = write_store(rng, "TEST", "1m", dates, until=ASOF - timedelta(minutes=1))
    print(f"      적재 {n}봉 (1m, 세션 {len(dates)}개)")
    params = replay.make_params(test_config(real_interval="1m"), days=days, as_of=ASOF)
    with patched_requests(no_network):
        t0 = time.time()
        job_id = replay.start_job(params, timeout_sec=600)
        job = wait_state(job_id, ("done", "failed"), timeout=600)
        wall = time.time() - t0
    ok = job["state"] == "done"
    check("2만 봉 리플레이 완료(done)", ok and job["result"]["data"]["bars"] >= 20000, f"{job['state']} {job.get('error')} {job.get('message')}")
    if ok:
        pf = job["result"]["perf"]
        res = job["result"]
        print(f"      [PERF] bars={pf['bars']} 거래={res['summary']['trades']}건  총 소요(부모 관측)={wall:.1f}s  "
              f"자식 total={pf['total_sec']}s (load {pf['load_sec']}s / prepare {pf['prepare_sec']}s / simulate {pf['simulate_sec']}s)  "
              f"자식 최대 RSS={pf['peak_rss_mb']}MB")
        check("PC 실측이 설계 예산 이내(자식 RSS ≤ 200MB, 소요 ≤ 300초)", (pf["peak_rss_mb"] or 0) <= 200 and wall <= 300,
              f"RSS={pf['peak_rss_mb']}MB wall={wall:.1f}s")
        check("RSS 가 측정됨(None 아님)", pf["peak_rss_mb"] is not None)


# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print(" VWAP 3단계 JR-2 — 리플레이 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % TMP)
    print("=" * 70)
    tests = [test_r1_bar_source, test_r2_jobs, test_r4_cleanup, test_r3_result, test_a1_api, test_perf]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()
    for pr in list(replay._procs.values()):
        try:
            if pr.poll() is None:
                pr.kill()
        except Exception:
            pass

    time.sleep(0.3)
    shutil.rmtree(TMP, ignore_errors=True)
    print("\nT-G. 운영 데이터 보호")
    after = _hash_tree(os.path.join(PROJECT_ROOT, "data"))
    check("운영 data/ 전체(하위 폴더 포함) 해시 전후 동일, replay/ replay_cache/ bars/ 신규 생성 없음", after == REAL_DATA_TREE_BEFORE,
          str(sorted(set(after) ^ set(REAL_DATA_TREE_BEFORE))))
    new_logs = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log"))) - LOGS_BEFORE
    check("trading_bot_*.log 신규 생성 없음", not new_logs, str(sorted(new_logs)))

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
