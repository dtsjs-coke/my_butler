"""VWAP 봉 데이터 적재(bars_store) — 3단계 리플레이가 쓸 '마감된 봉'을 CSV 로 쌓아 둡니다.

[저장 규칙]
- 파일: data/bars/{TICKER}_{interval}_{세션날짜}.csv   (헤더 time,open,high,low,close,volume,source)
- 세션 날짜는 core/vwap/session.py 의 SessionSpec(ADR-0008) 기준 '세션 시작 시각의 KST 날짜'입니다(자정 기준 아님).
- 입력 df 의 마지막 행은 진행 중인 봉일 수 있으므로 저장하지 않습니다(마감된 봉만 저장).
- (ticker, interval)별 '마지막으로 저장한 봉 시각'보다 큰 행만 append 합니다. 이미 저장된 봉은 다시 쓰지 않습니다.
  프로세스를 재시작하면 해당 종목·간격의 가장 최근 파일 마지막 정상 줄에서 복원합니다.
- 예외는 밖으로 던지지 않습니다(매매 루프 보호). 경고 로그는 10분에 1회만 남깁니다.
- 이 모듈은 아직 봇에 연결되지 않았습니다(hook 함수만 제공). 연결은 3단계 Phase B 에서 합니다.
"""
import os
import glob
import logging
import threading
import time as _time
from datetime import datetime

import pandas as pd

import core.vwap.config_manager as _cm
from core.vwap.session import SessionSpec, infer_market, to_kst_naive

logger = logging.getLogger("vwap_bot")

COLUMNS = ["time", "open", "high", "low", "close", "volume", "source"]
HEADER = ",".join(COLUMNS) + "\n"
TIME_FMT = "%Y-%m-%d %H:%M:%S"
WARN_INTERVAL_SEC = 600  # 경고 로그 10분에 1회

_lock = threading.Lock()
_last_saved = {}      # (TICKER, interval) -> pd.Timestamp (마지막으로 저장한 봉 시각)
_last_warn_ts = 0.0   # time.monotonic 기준. 0 이면 아직 경고한 적 없음


def bars_dir() -> str:
    """봉 저장 폴더. DATA_DIR 은 호출 시점에 읽습니다(테스트에서 임시 폴더로 바꿔치기 가능)."""
    return os.path.join(_cm.DATA_DIR, "bars")


def _bars_path(ticker: str, interval: str, session_date: str) -> str:
    return os.path.join(bars_dir(), f"{str(ticker).upper()}_{interval}_{session_date}.csv")


def _warn(msg: str):
    """10분에 1회만 경고 로그를 남깁니다. 로깅 자체가 실패해도 무시합니다."""
    global _last_warn_ts
    try:
        now = _time.monotonic()
        if _last_warn_ts and now - _last_warn_ts < WARN_INTERVAL_SEC:
            return
        _last_warn_ts = now
        logger.warning(msg)
    except Exception:
        pass


def _reset_state():
    """메모리 상태 초기화(테스트용 — 재시작 시뮬레이션)."""
    global _last_warn_ts
    with _lock:
        _last_saved.clear()
        _last_warn_ts = 0.0


def _parse_line(line: str):
    """CSV 한 줄 -> (Timestamp, open, high, low, close, volume, source) 또는 None(손상/헤더)."""
    parts = line.strip().split(",")
    if len(parts) != len(COLUMNS):
        return None
    try:
        ts = pd.Timestamp(datetime.strptime(parts[0], TIME_FMT))
        vals = [float(x) for x in parts[1:6]]
    except Exception:
        return None
    if any(v != v for v in vals):  # NaN
        return None
    return (ts, *vals, parts[6])


def _restore_last_saved(ticker: str, interval: str):
    """해당 (ticker, interval) 의 가장 최근 세션 파일 마지막 정상 줄에서 last_saved 복원. 없으면 None."""
    pattern = os.path.join(bars_dir(), f"{str(ticker).upper()}_{interval}_*.csv")
    for path in reversed(sorted(glob.glob(pattern))):  # 날짜 큰 파일부터
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            continue
        for raw in reversed(lines):  # 마지막 줄이 손상이면 그 앞 줄
            row = _parse_line(raw)
            if row is not None:
                return row[0]
    return None


def _fmt_line(t, o, h, l, c, v, src) -> str:
    return f"{t.strftime(TIME_FMT)},{float(o)!r},{float(h)!r},{float(l)!r},{float(c)!r},{float(v)!r},{src}\n"


def append_closed_bars(ticker, interval, df, reset_time="22:30", source="", market=None) -> int:
    """df 의 마감된 봉 중 아직 저장하지 않은 것만 세션별 파일에 append. 기록한 행 수를 반환(실패/없음 0).
    예외는 던지지 않습니다."""
    try:
        if df is None or len(df) < 2:  # 마지막 행은 진행 중 봉일 수 있어 제외 → 최소 2행 필요
            return 0
        ticker = str(ticker).upper()
        interval = str(interval)
        src = str(source or "").replace(",", "_").replace("\n", " ").strip()

        d = df[["time", "open", "high", "low", "close", "volume"]].copy()
        d["time"] = to_kst_naive(d["time"])
        d = d.sort_values("time").reset_index(drop=True)
        d = d.iloc[:-1]  # 마지막(진행 중일 수 있는) 행 제외
        for c in ("open", "high", "low", "close", "volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna().drop_duplicates(subset="time", keep="last").reset_index(drop=True)
        if d.empty:
            return 0

        key = (ticker, interval)
        with _lock:
            last = _last_saved.get(key)
            if last is None:
                last = _restore_last_saved(ticker, interval)
                if last is not None:
                    _last_saved[key] = last
            if last is not None:
                d = d[d["time"] > last].reset_index(drop=True)
            if d.empty:
                return 0

            spec = SessionSpec.for_market(infer_market(ticker, market), reset_time, ticker)
            d["_sess"] = spec.label_bars(d["time"]).dt.strftime("%Y-%m-%d").values

            os.makedirs(bars_dir(), exist_ok=True)
            written = 0
            # 시간 순서대로 세션별로 기록. 실패하면 멈추고 성공한 지점까지만 last_saved 를 올림
            for sess, g in d.groupby("_sess", sort=True):
                path = _bars_path(ticker, interval, sess)
                lines = [_fmt_line(r.time, r.open, r.high, r.low, r.close, r.volume, src)
                         for r in g.itertuples(index=False)]
                try:
                    prefix = ""
                    if os.path.exists(path):
                        # 쓰다 꺼져 줄바꿈 없이 끝난 파일이면 새 줄부터 시작(손상 줄이 새 행을 오염시키지 않게)
                        with open(path, "rb") as fb:
                            fb.seek(0, os.SEEK_END)
                            if fb.tell() > 0:
                                fb.seek(-1, os.SEEK_END)
                                if fb.read(1) != b"\n":
                                    prefix = "\n"
                    else:
                        prefix = HEADER
                    with open(path, "a", encoding="utf-8", newline="") as f:
                        f.write(prefix + "".join(lines))
                except Exception as e:
                    _warn(f"[VWAP 봉적재] 쓰기 실패 (무시하고 계속): {path}: {e}")
                    break
                written += len(g)
                _last_saved[key] = g["time"].iloc[-1]
            return written
    except Exception as e:
        _warn(f"[VWAP 봉적재] 적재 중 예외 (무시하고 계속): {e}")
        return 0


def read_range(ticker, interval, start_date, end_date) -> pd.DataFrame:
    """세션 날짜가 [start_date, end_date] (포함)인 파일을 합쳐 반환. 손상된 줄은 건너뛰고,
    time 중복은 마지막 값을 유지하며 time 오름차순 정렬. 날짜는 'YYYY-MM-DD' 문자열 또는 date."""
    empty = pd.DataFrame({"time": pd.Series(dtype="datetime64[ns]"),
                          **{c: pd.Series(dtype="float64") for c in COLUMNS[1:6]},
                          "source": pd.Series(dtype="object")})
    try:
        s = str(start_date)[:10]
        e = str(end_date)[:10]
        prefix = f"{str(ticker).upper()}_{interval}_"
        rows = []
        for path in sorted(glob.glob(os.path.join(bars_dir(), prefix + "*.csv"))):
            sess = os.path.basename(path)[len(prefix):-4]
            if not (s <= sess <= e):
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        row = _parse_line(raw)
                        if row is not None:
                            rows.append(row)
            except Exception as ex:
                _warn(f"[VWAP 봉적재] 파일 읽기 실패 (건너뜀): {path}: {ex}")
        if not rows:
            return empty
        out = pd.DataFrame(rows, columns=COLUMNS)
        return out.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    except Exception as ex:
        _warn(f"[VWAP 봉적재] read_range 예외 (빈 결과 반환): {ex}")
        return empty


def hook(ctx) -> int:
    """봇 주기 훅. ctx["df"] 가 비어 있지 않을 때만 동작하고 ctx["candles_source"] 를 source 로 기록.
    config.bars_store_enabled 가 false 면 아무것도 하지 않습니다(키가 없으면 기본 true). 예외는 던지지 않습니다.
    시장(US/KR)은 ticker 형식(숫자 6자리=KR)으로 추론합니다."""
    try:
        cfg = ctx.get("config") or {}
        if not cfg.get("bars_store_enabled", True):
            return 0
        df = ctx.get("df")
        if df is None or len(df) == 0:
            return 0
        return append_closed_bars(ctx.get("ticker"), ctx.get("interval"), df,
                                  ctx.get("reset_time") or "22:30", ctx.get("candles_source") or "")
    except Exception as e:
        _warn(f"[VWAP 봉적재] 훅 예외 (무시하고 계속): {e}")
        return 0
