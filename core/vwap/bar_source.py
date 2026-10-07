"""리플레이용 봉 데이터 로더 (3단계, 설계 문서 §4.1).

load_bars(ticker, interval, days, reset_time) 가 하는 일
  1. bars_store 에 적재된 봉(Toss 원본)을 먼저 씁니다.
  2. 적재분에 없는(빠진) 세션 날짜는 Yahoo chart API 를 requests 로 직접 받아 채웁니다.
     (S9 에는 yfinance 가 없으므로 requests 만 사용. 1m 은 7일, 5m/15m 은 59일 단위로 쪼개 요청)
  3. Yahoo 원본은 data/replay_cache/{TICKER}_{interval}_{조회일YYYYMMDD}.csv 로 캐시(같은 날 재사용).
  4. 합친 뒤 time 중복 제거(적재분 우선) → 정렬 → NaN / high<low 제거.

세션 날짜는 ADR-0008 SessionSpec(봇과 같은 규칙) 기준 '세션 시작 시각의 KST 날짜'입니다.
모든 시각은 KST tz-naive 입니다(Yahoo UTC 타임스탬프에 +9h — TossBroker._fetch_yahoo_candles 와 같은 변환).

[가정/한계] "빠진 세션"은 평일(월~금) 세션 라벨 중 적재분에 봉이 한 줄도 없는 날입니다.
공휴일처럼 실제로 거래가 없는 날도 Yahoo 를 한 번 조회하고, 그래도 비면 meta["gaps"] 에 남습니다.
적재분이 있는 세션은 '완전하다'고 가정하되, 첫 봉이 세션 시작보다 3봉 넘게 늦으면 '부분 적재 가능 세션'으로 경고만 합니다(Yahoo 로 보충하지 않음).
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

import core.vwap.config_manager as _cm
from core.vwap import bars_store
from core.vwap.session import SessionSpec, infer_market, interval_minutes

logger = logging.getLogger("vwap_bot")

# interval 별 최대 조회 일수(달력일) / Yahoo 1회 요청 구간(일)
INTERVAL_MAX_DAYS = {"1m": 30, "5m": 60, "15m": 60}
YAHOO_CHUNK_DAYS = {"1m": 7, "5m": 59, "15m": 59}
SUPPORTED_INTERVALS = tuple(INTERVAL_MAX_DAYS)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
YAHOO_TIMEOUT_SEC = 10
BAR_COLUMNS = ["time", "open", "high", "low", "close", "volume"]
FETCHED_SOURCE = "yahoo"
CACHE_TTL_SEC = 3600                 # Yahoo 캐시 재사용 가능 시간(생성 후 1시간)
CACHE_RETENTION_SEC = 7 * 86400      # replay_cache 보관 기간(7일)
PARTIAL_TOL_BARS = 3                 # 세션 첫 봉이 세션 시작보다 이 봉 수 넘게 늦으면 '부분 적재 가능 세션'
PARTIAL_LABEL = "부분 적재 가능 세션"
_META_FMT = "%Y-%m-%d %H:%M:%S"


def cache_dir() -> str:
    """Yahoo 캐시 폴더. DATA_DIR 은 호출 시점에 읽습니다(테스트에서 임시 폴더로 바꿀 수 있게)."""
    return os.path.join(_cm.DATA_DIR, "replay_cache")


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame({"time": pd.Series(dtype="datetime64[ns]"),
                         **{c: pd.Series(dtype="float64") for c in BAR_COLUMNS[1:]},
                         "source": pd.Series(dtype="object")})


# ----------------------------------------------------------------------
# Yahoo 수집
# ----------------------------------------------------------------------
def _yahoo_symbols(ticker: str) -> list:
    t = str(ticker).upper().strip()
    if t.isdigit() and len(t) == 6:
        return [f"{t}.KS", f"{t}.KQ"]   # 코스피 먼저, 실패하면 코스닥
    return [t]


def _to_epoch(dt_kst: datetime) -> int:
    """KST naive datetime → UTC epoch 초."""
    return calendar.timegm((dt_kst - timedelta(hours=9)).timetuple())


def _parse_yahoo(payload: dict) -> pd.DataFrame:
    result = (payload.get("chart", {}).get("result") or [])
    if not result:
        return pd.DataFrame(columns=BAR_COLUMNS)
    r0 = result[0]
    stamps = r0.get("timestamp") or []
    q = (r0.get("indicators", {}).get("quote") or [{}])[0]
    cols = {k: (q.get(k) or []) for k in ("open", "high", "low", "close", "volume")}
    rows = []
    for i, ts in enumerate(stamps):
        vals = []
        ok = True
        for k in ("open", "high", "low", "close", "volume"):
            arr = cols[k]
            if i >= len(arr) or arr[i] is None:
                ok = False
                break
            vals.append(float(arr[i]))
        if not ok:
            continue
        rows.append((pd.to_datetime(ts, unit="s") + pd.Timedelta(hours=9), *vals))
    return pd.DataFrame(rows, columns=BAR_COLUMNS)


def _fetch_chunk(symbol: str, interval: str, start_kst: datetime, end_kst: datetime):
    """한 구간 요청. 성공하면 DataFrame(빈 것도 가능), HTTP 오류면 None."""
    params = {"interval": interval, "period1": _to_epoch(start_kst), "period2": _to_epoch(end_kst)}
    res = requests.get(YAHOO_URL.format(symbol=symbol), params=params, headers=YAHOO_HEADERS,
                       timeout=YAHOO_TIMEOUT_SEC)
    if res.status_code != 200:
        return None
    return _parse_yahoo(res.json())


def fetch_yahoo_range(ticker: str, interval: str, start_kst: datetime, end_kst: datetime, warnings: list) -> pd.DataFrame:
    """[start, end] 를 interval 별 구간 단위로 쪼개 Yahoo 에서 받아 합쳐 반환(KST naive). 일부 실패는 warnings 에 남기고 계속."""
    chunk = timedelta(days=YAHOO_CHUNK_DAYS[interval])
    symbols = _yahoo_symbols(ticker)
    frames = []
    cur = start_kst
    while cur < end_kst:
        nxt = min(cur + chunk, end_kst)
        df = None
        for si, sym in enumerate(symbols):
            try:
                df = _fetch_chunk(sym, interval, cur, nxt)
            except Exception as e:   # 네트워크 오류 등
                warnings.append(f"Yahoo 조회 실패({sym} {cur:%m-%d}~{nxt:%m-%d}): {type(e).__name__}")
                df = None
            if df is not None and not df.empty:
                if si > 0:               # 코스닥(.KQ) 이 성공했으면 이후 구간은 그 심볼을 먼저 시도
                    symbols = [sym] + [s for s in symbols if s != sym]
                break
        if df is None:
            warnings.append(f"Yahoo 응답 없음/오류 구간: {cur:%Y-%m-%d}~{nxt:%Y-%m-%d} (1분봉은 최근 30일까지만 제공)")
        elif not df.empty:
            frames.append(df)
        cur = nxt
    if not frames:
        return pd.DataFrame(columns=BAR_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)


# ----------------------------------------------------------------------
# 캐시
# ----------------------------------------------------------------------
def _cache_paths(ticker: str, interval: str, fetch_date: str):
    base = os.path.join(cache_dir(), f"{str(ticker).upper()}_{interval}_{fetch_date}")
    return base + ".csv", base + ".meta.json"


def _floor_to_interval(dt: datetime, iv: timedelta) -> datetime:
    """dt 를 interval 경계로 내림(epoch 기준). '이미 마감됐어야 할 마지막 봉'의 끝 시각 계산용."""
    step = int(iv.total_seconds())
    sec = int((dt - datetime(1970, 1, 1)).total_seconds())
    return datetime(1970, 1, 1) + timedelta(seconds=sec - sec % step)


def _read_cache(ticker, interval, fetch_date, need_from: datetime, need_to: Optional[datetime] = None,
                ttl_sec: float = CACHE_TTL_SEC):
    """캐시를 재사용해도 되면 DataFrame, 아니면 None.

    재사용 조건(모두 만족): meta.from <= need_from, meta.to >= need_to(요청의 마지막 마감 봉 경계),
    생성 후 ttl_sec 이내(meta.created_ts, 없으면 csv 수정시각), 일부 구간 실패 표시(meta.partial) 없음.
    """
    csv_path, meta_path = _cache_paths(ticker, interval, fetch_date)
    try:
        if not (os.path.exists(csv_path) and os.path.exists(meta_path)):
            return None
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("partial"):
            return None
        if datetime.strptime(meta["from"], _META_FMT) > need_from:
            return None
        if need_to is not None and datetime.strptime(meta["to"], _META_FMT) < need_to:
            return None
        created = float(meta.get("created_ts") or os.path.getmtime(csv_path))
        age = time.time() - created
        if age < 0 or age > ttl_sec:
            return None
        df = pd.read_csv(csv_path, parse_dates=["time"])
        return df[BAR_COLUMNS] if not df.empty else pd.DataFrame(columns=BAR_COLUMNS)
    except Exception:
        return None


def _purge_old_cache(retention_sec: float = CACHE_RETENTION_SEC):
    """replay_cache 폴더 안의 직접 파일(*.csv, *.meta.json) 중 수정 후 retention_sec 지난 것을 삭제. 실패는 무시."""
    d = cache_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return
    cutoff = time.time() - retention_sec
    for n in names:
        if not (n.endswith(".csv") or n.endswith(".meta.json")):
            continue
        fp = os.path.join(d, n)
        try:
            if os.path.islink(fp) or not os.path.isfile(fp):
                continue
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        except OSError:
            pass


def _write_cache(ticker, interval, fetch_date, df: pd.DataFrame, fetch_from: datetime, fetch_to: datetime, warnings: list):
    csv_path, meta_path = _cache_paths(ticker, interval, fetch_date)
    try:
        os.makedirs(cache_dir(), exist_ok=True)
        _purge_old_cache()
        df[BAR_COLUMNS].to_csv(csv_path, index=False, date_format=_META_FMT)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"from": fetch_from.strftime(_META_FMT), "to": fetch_to.strftime(_META_FMT),
                       "created_ts": time.time()}, f)
    except Exception as e:
        warnings.append(f"Yahoo 캐시 저장 실패(무시): {type(e).__name__}")


# ----------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------
def _weekday_session_dates(spec: SessionSpec, start_dt: datetime, now: datetime) -> list:
    """start_dt 의 세션부터 now 의 세션까지 평일(월~금) 세션 날짜(date) 목록."""
    first = spec.session_start(start_dt).date()
    last = spec.session_start(now).date()
    out = []
    d = first
    while d <= last:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _session_dates_of(spec: SessionSpec, times: pd.Series) -> set:
    if times is None or len(times) == 0:
        return set()
    return set(spec.label_bars(times).dt.strftime("%Y-%m-%d"))


def _partial_sessions(spec: SessionSpec, store: pd.DataFrame, iv: timedelta) -> list:
    """적재분 세션 중 첫 봉이 (세션 시작 + PARTIAL_TOL_BARS*봉간격) 보다 늦은 세션 날짜(YYYY-MM-DD) 목록."""
    labels = spec.label_bars(store["time"])
    firsts = store["time"].groupby(labels.dt.strftime("%Y-%m-%d").values).min()
    tol = iv * PARTIAL_TOL_BARS
    out = []
    for lab, first in firsts.items():
        d = datetime.strptime(lab, "%Y-%m-%d").date()
        start = datetime.combine(d, spec.reset_on(d))
        if first.to_pydatetime() > start + tol:
            out.append(lab)
    return sorted(out)


def load_bars(ticker, interval, days, reset_time, market=None, now: Optional[datetime] = None):
    """리플레이용 봉 로드. (DataFrame[time,open,high,low,close,volume,source], meta dict) 반환.

    days   달력일(기간). interval 별 최대(1m=30, 5m/15m=60)를 넘으면 잘라내고 meta["warnings"] 에 남깁니다.
    now    기준 시각(KST naive). 생략하면 현재 시각. 리플레이 작업은 요청 시각(as_of)을 넘겨 결과를 재현 가능하게 합니다.
    봉이 하나도 없으면 빈 DataFrame 을 돌려줍니다(호출자가 no_data 처리).
    """
    interval = str(interval)
    if interval not in INTERVAL_MAX_DAYS:
        raise ValueError(f"지원하지 않는 봉 간격입니다: {interval} (1m, 5m, 15m 만 가능)")
    days = int(days)
    if days < 1:
        raise ValueError("조회 기간은 1일 이상이어야 합니다.")
    warnings = []
    max_days = INTERVAL_MAX_DAYS[interval]
    if days > max_days:
        warnings.append(f"{interval} 봉은 최대 {max_days}일까지만 조회됩니다. 요청 {days}일을 {max_days}일로 줄였습니다.")
        days = max_days

    ticker = str(ticker).upper().strip()
    now = now or datetime.now()
    spec = SessionSpec.for_market(infer_market(ticker, market), reset_time, ticker)
    # 첫 세션이 잘리지 않도록 '요청 시작 시각이 속한 세션의 시작'부터 쓴다(VWAP 은 세션 누적이라 부분 세션이면 값이 달라짐)
    start_dt = spec.session_start(now - timedelta(days=days))
    iv = timedelta(minutes=interval_minutes(interval))

    # 1) 적재분
    store = bars_store.read_range(ticker, interval, start_dt.date(), spec.session_start(now).date())
    if not store.empty:
        store = store[(store["time"] >= start_dt) & (store["time"] <= now)]
        mock_n = int((store["source"] == "mock").sum())
        if mock_n:
            warnings.append(f"적재분 중 모의(mock) 난수 봉 {mock_n}개는 제외했습니다.")
            store = store[store["source"] != "mock"]
        store = store.reset_index(drop=True)
    store_n = len(store)
    store_dates = _session_dates_of(spec, store["time"]) if store_n else set()

    partial = _partial_sessions(spec, store, iv) if store_n else []
    if partial:
        warnings.append(f"{PARTIAL_LABEL}: {', '.join(partial)} (첫 봉이 세션 시작보다 {PARTIAL_TOL_BARS}봉 넘게 늦음. Yahoo 로 보충하지 않음)")

    # 2) 빠진 세션 → Yahoo
    expected = _weekday_session_dates(spec, start_dt, now)
    missing = [d for d in expected if d.strftime("%Y-%m-%d") not in store_dates]
    yahoo = pd.DataFrame(columns=BAR_COLUMNS)
    if missing:
        fetch_from = datetime.combine(missing[0], spec.reset_on(missing[0]))
        fetch_to = min(now, datetime.combine(missing[-1] + timedelta(days=1), spec.reset_on(missing[-1] + timedelta(days=1))))
        fetch_date = now.strftime("%Y%m%d")
        need_to = _floor_to_interval(fetch_to, iv)   # 이미 마감됐어야 할 마지막 봉의 끝
        cached = _read_cache(ticker, interval, fetch_date, fetch_from, need_to)
        if cached is not None:
            yahoo = cached
        else:
            n_warn = len(warnings)
            yahoo = fetch_yahoo_range(ticker, interval, fetch_from, fetch_to, warnings)
            fetch_ok = len(warnings) == n_warn          # 일부 구간 실패(경고 발생) 결과는 캐시에 쓰지 않음
            if not yahoo.empty and fetch_ok:
                _write_cache(ticker, interval, fetch_date, yahoo, fetch_from, fetch_to, warnings)
        if not yahoo.empty:
            # 진행 중인 봉(마감 전)은 제외하고, 요청 구간만 사용
            yahoo = yahoo[(yahoo["time"] >= start_dt) & (yahoo["time"] + iv <= now)].reset_index(drop=True)
            yahoo["source"] = FETCHED_SOURCE
        else:
            yahoo = _empty_bars()

    # 3) 병합: time 중복은 적재분 우선
    parts = [p for p in (store, yahoo) if len(p)]
    if parts:
        merged = pd.concat(parts, ignore_index=True)
        merged["_src_rank"] = [0] * store_n + [1] * (len(merged) - store_n)
        merged = (merged.sort_values(["time", "_src_rank"], kind="mergesort")
                  .drop_duplicates(subset="time", keep="first")
                  .drop(columns="_src_rank"))
        for c in BAR_COLUMNS[1:]:
            merged[c] = pd.to_numeric(merged[c], errors="coerce")
        merged = merged.dropna(subset=BAR_COLUMNS[1:])
        merged = merged[merged["high"] >= merged["low"]]
        merged = merged.sort_values("time").reset_index(drop=True)
    else:
        merged = _empty_bars()

    yahoo_used = int((merged["source"] == FETCHED_SOURCE).sum()) if len(merged) else 0
    store_used = len(merged) - yahoo_used
    have_dates = _session_dates_of(spec, merged["time"]) if len(merged) else set()
    gaps = [d.strftime("%Y-%m-%d") for d in expected if d.strftime("%Y-%m-%d") not in have_dates]

    gaps = gaps + [f"{d}({PARTIAL_LABEL})" for d in partial]
    limits = (f"{interval} 봉은 최대 {max_days}일까지 조회됩니다. 적재분(Toss 원본)을 먼저 쓰고 빠진 평일 세션만 Yahoo 로 채웁니다. "
              "Yahoo 는 거래소·시간외 포함 범위와 호가 단위가 Toss 와 다를 수 있고, gaps 에는 공휴일 등 거래가 없던 날도 포함됩니다.")
    if partial:
        limits += f" '{PARTIAL_LABEL}'(적재분 첫 봉이 세션 시작보다 늦음)은 Yahoo 로 보충·병합하지 않아 VWAP 이 실제와 다를 수 있습니다."
    meta = {
        "bars": int(len(merged)),
        "sessions": len(have_dates),
        "from": merged["time"].iloc[0].strftime("%Y-%m-%d") if len(merged) else None,
        "to": merged["time"].iloc[-1].strftime("%Y-%m-%d") if len(merged) else None,
        "source_breakdown": {"bars_store": store_used, "yahoo": yahoo_used},
        "gaps": gaps,
        "limits": limits,
        "partial_sessions": partial,
        "warnings": warnings,
    }
    return merged, meta
