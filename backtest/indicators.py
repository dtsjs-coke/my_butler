"""
indicators.py — 백테스트 하네스가 쓰는 지표 계산 (전부 '과거만 보고' 계산되도록 작성)

여기서 계산하는 지표는 모두 "그 봉이 끝난 시점까지의 정보"만 사용합니다.
미래 데이터를 참조하는(look-ahead) 계산은 없습니다.

- vwap        : 거래일(session_date)별로 리셋되는 누적 거래량가중평균가
- vwap_stdev  : 같은 세션 누적 기준의 거래량가중 표준편차 (운영 strategy.py와 동일 수식)
- atr         : Wilder ATR (세션 경계에서는 전일 종가 갭을 TR에 반영하지 않음)
- adx         : Wilder ADX
- rsi         : Wilder RSI
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame, atr_period: int = 14, adx_period: int = 14,
                   rsi_period: int = 14) -> pd.DataFrame:
    """표준 컬럼(time, open, high, low, close, volume, session_date)을 가진 df에 지표를 붙여 반환."""
    out = df.copy().reset_index(drop=True)

    grp = out.groupby("session_date", sort=False)

    # ---- 세션별 누적 VWAP ----
    pv = out["close"] * out["volume"]
    cum_pv = pv.groupby(out["session_date"]).cumsum()
    cum_v = out["volume"].groupby(out["session_date"]).cumsum()
    out["vwap"] = np.where(cum_v > 0, cum_pv / cum_v.replace(0, np.nan), out["close"])
    out["vwap"] = out["vwap"].astype(float).ffill()

    # ---- 세션별 거래량가중 표준편차 (운영 strategy.py와 같은 수식) ----
    diff_sq = out["volume"] * (out["close"] - out["vwap"]) ** 2
    cum_diff = diff_sq.groupby(out["session_date"]).cumsum()
    out["vwap_stdev"] = np.sqrt(np.where(cum_v > 0, cum_diff / cum_v.replace(0, np.nan), 0.0))
    out["vwap_stdev"] = pd.Series(out["vwap_stdev"], index=out.index).fillna(0.0)

    # ---- True Range (세션 첫 봉은 갭을 무시하고 high-low만 사용) ----
    prev_close = out["close"].shift(1)
    is_new_session = out["session_date"] != out["session_date"].shift(1)
    tr1 = out["high"] - out["low"]
    tr2 = (out["high"] - prev_close).abs()
    tr3 = (out["low"] - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    tr = tr.where(~is_new_session, tr1)
    out["atr"] = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean()

    # ---- ADX (Wilder) ----
    up_move = out["high"].diff()
    down_move = out["low"].shift(1) - out["low"]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=out.index).where(~is_new_session, 0.0)
    minus_dm = pd.Series(minus_dm, index=out.index).where(~is_new_session, 0.0)

    atr_adx = tr.ewm(alpha=1.0 / adx_period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0 / adx_period, adjust=False).mean() / (atr_adx + 1e-9)
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / adx_period, adjust=False).mean() / (atr_adx + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    out["adx"] = dx.ewm(alpha=1.0 / adx_period, adjust=False).mean()
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    # ---- RSI (Wilder) ----
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / rsi_period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    out["rsi"] = 100 - (100 / (1 + rs))

    # ---- 세션 내 위치 정보 (장 마감 전 강제청산 판단용) ----
    out["bar_in_session"] = grp.cumcount()
    session_len = out.groupby("session_date")["close"].transform("size")
    out["bars_to_session_end"] = session_len - 1 - out["bar_in_session"]
    out["session_len"] = session_len

    out = out.fillna({"atr": 0.0, "adx": 0.0, "rsi": 50.0})
    return out
