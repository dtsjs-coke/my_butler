"""
data.py — 백테스트용 과거 분봉 데이터 로더 (yfinance 기반, 디스크 캐시 포함)

[이 파일이 하는 일]
1) yfinance에서 미국 주식/ETF의 분봉(1m, 5m 등)을 내려받습니다.
2) yfinance의 기간 제한을 우회하기 위해 7일씩 잘라서 여러 번 요청하고 이어붙입니다.
3) 내려받은 데이터를 CSV로 캐시해서, 다시 실행할 때 네트워크를 타지 않게 합니다.
4) 정규장(09:30~16:00 ET) 봉만 남기고, 백테스트 엔진이 쓰는 표준 컬럼으로 정리합니다.

[yfinance 데이터 제약 — 반드시 알고 있어야 하는 한계]
- 1m(1분봉): 최근 30일 이내만 제공. 게다가 한 번의 요청은 최대 8일치까지.
             => 실질적으로 확보 가능한 건 "최근 약 4주(영업일 19~22일)" 수준.
- 2m/5m/15m/30m/60m: 최근 60일 이내.
- 1h: 최근 730일.
즉, 분봉 전략을 수년치로 검증하는 건 yfinance만으로는 불가능합니다.
표본이 작다는 것 = 통계적 유의성이 낮다는 것이므로, 결과 해석 시 반드시 감안해야 합니다.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import List, Optional

import pandas as pd

# 캐시 폴더: backtest/data_cache/
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data_cache")

# 미국 정규장 시간 (ET 기준)
US_REGULAR_OPEN = dt.time(9, 30)
US_REGULAR_CLOSE = dt.time(16, 0)

# interval별 yfinance 최대 조회 가능 일수 (yfinance/야후 파이낸스 공식 제약)
MAX_LOOKBACK_DAYS = {
    "1m": 30,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "1h": 730,
}

# 한 번의 요청으로 받을 수 있는 최대 일수
MAX_CHUNK_DAYS = {
    "1m": 7,
    "2m": 59,
    "5m": 59,
    "15m": 59,
    "30m": 59,
    "60m": 729,
    "1h": 729,
}


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance가 (지표, 티커) 2단 컬럼(MultiIndex)으로 주는 경우를 1단으로 펴줍니다."""
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    return df


def _download_chunk(ticker: str, start: dt.date, end: dt.date, interval: str) -> Optional[pd.DataFrame]:
    """yfinance에서 [start, end) 구간의 봉 하나를 내려받습니다. 실패하면 None."""
    import yfinance as yf

    try:
        df = yf.download(
            ticker,
            start=start.isoformat(),
            end=end.isoformat(),
            interval=interval,
            progress=False,
            auto_adjust=False,   # 분봉은 어차피 배당/분할 조정이 의미 없으므로 원본 가격 사용
            prepost=False,       # 정규장만 (프리/애프터 마켓 제외)
            threads=False,
        )
    except Exception as exc:  # 네트워크/레이트리밋 등
        print(f"  [경고] {ticker} {start}~{end} 다운로드 실패: {type(exc).__name__}: {exc}")
        return None

    if df is None or len(df) == 0:
        return None
    return _flatten_columns(df)


def fetch_intraday(
    ticker: str,
    interval: str = "1m",
    lookback_days: Optional[int] = None,
    use_cache: bool = True,
    cache_dir: str = CACHE_DIR,
) -> pd.DataFrame:
    """
    분봉 데이터를 내려받아 표준 형태의 DataFrame으로 반환합니다.

    반환 컬럼: time(tz-naive ET), open, high, low, close, volume, session_date
    - time 은 미국 동부시간(ET) 기준의 tz 정보 없는 datetime 입니다.
    - session_date 는 그 봉이 속한 '거래일'(ET 기준 날짜)입니다. VWAP 리셋 기준이 됩니다.

    Parameters
    ----------
    ticker : 예) "SPY", "QQQ"
    interval : "1m", "5m", "15m" 등
    lookback_days : 며칠치를 가져올지. None이면 interval별 최대치.
    use_cache : True면 캐시 CSV가 있을 때 네트워크를 타지 않습니다.
    """
    if interval not in MAX_LOOKBACK_DAYS:
        raise ValueError(f"지원하지 않는 interval 입니다: {interval}")

    max_days = MAX_LOOKBACK_DAYS[interval]
    if lookback_days is None or lookback_days > max_days:
        lookback_days = max_days

    os.makedirs(cache_dir, exist_ok=True)
    # 캐시 파일명에 '오늘 날짜'를 넣어서, 날이 바뀌면 자동으로 새로 받도록 함
    today = dt.date.today()
    cache_path = os.path.join(
        cache_dir, f"{ticker}_{interval}_{lookback_days}d_{today.isoformat()}.csv"
    )

    if use_cache and os.path.exists(cache_path):
        df = pd.read_csv(cache_path, parse_dates=["time"])
        df["session_date"] = pd.to_datetime(df["session_date"]).dt.date
        print(f"  [캐시] {ticker} {interval}: {len(df)}봉 (from {os.path.basename(cache_path)})")
        return df

    chunk_days = MAX_CHUNK_DAYS[interval]
    end = today + dt.timedelta(days=1)  # end는 배타적이므로 내일로 잡아 오늘까지 포함
    earliest = today - dt.timedelta(days=lookback_days)

    frames: List[pd.DataFrame] = []
    cursor_end = end
    while cursor_end > earliest:
        cursor_start = max(cursor_end - dt.timedelta(days=chunk_days), earliest)
        chunk = _download_chunk(ticker, cursor_start, cursor_end, interval)
        if chunk is not None and len(chunk) > 0:
            frames.append(chunk)
        cursor_end = cursor_start

    if not frames:
        raise RuntimeError(f"{ticker} {interval} 데이터를 한 건도 받지 못했습니다.")

    raw = pd.concat(frames).sort_index()
    raw = raw[~raw.index.duplicated(keep="first")]

    # 인덱스는 tz-aware(ET 또는 UTC)로 오므로 ET로 변환한 뒤 tz 정보를 떼어냅니다.
    idx = raw.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    raw.index = idx

    out = pd.DataFrame(
        {
            "time": raw.index,
            "open": raw["Open"].astype(float).values,
            "high": raw["High"].astype(float).values,
            "low": raw["Low"].astype(float).values,
            "close": raw["Close"].astype(float).values,
            "volume": raw["Volume"].astype(float).values,
        }
    )

    # 정규장(09:30 <= t < 16:00)만 남김
    tt = out["time"].dt.time
    out = out[(tt >= US_REGULAR_OPEN) & (tt < US_REGULAR_CLOSE)].copy()

    # 결측/이상치 제거
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[out["high"] >= out["low"]]
    out = out.sort_values("time").reset_index(drop=True)

    out["session_date"] = out["time"].dt.date

    # 봉이 너무 적은 날(데이터 누락일 가능성)은 제외 — 하루 봉 수 중앙값의 30% 미만이면 버림
    counts = out.groupby("session_date").size()
    if len(counts) > 2:
        threshold = counts.median() * 0.30
        keep_days = set(counts[counts >= threshold].index)
        dropped = sorted(set(counts.index) - keep_days)
        if dropped:
            print(f"  [정리] 봉 수가 비정상적으로 적어 제외한 날: {dropped}")
        out = out[out["session_date"].isin(keep_days)].reset_index(drop=True)

    out.to_csv(cache_path, index=False)
    print(
        f"  [수집] {ticker} {interval}: {len(out)}봉, "
        f"{out['session_date'].min()} ~ {out['session_date'].max()} "
        f"({out['session_date'].nunique()} 영업일)"
    )
    return out


def split_in_out_sample(df: pd.DataFrame, is_ratio: float = 0.6):
    """
    거래일(session_date) 단위로 in-sample / out-of-sample을 나눕니다.
    (봉 단위로 자르면 하루가 반으로 쪼개져 VWAP 누적이 깨지므로 반드시 '날짜' 기준으로 자릅니다.)

    Returns: (is_df, oos_df, is_days, oos_days)
    """
    days = sorted(df["session_date"].unique())
    n_is = max(1, int(round(len(days) * is_ratio)))
    is_days = days[:n_is]
    oos_days = days[n_is:]
    is_df = df[df["session_date"].isin(is_days)].reset_index(drop=True)
    oos_df = df[df["session_date"].isin(oos_days)].reset_index(drop=True)
    return is_df, oos_df, is_days, oos_days
