"""
strategies.py — 비교 대상 전략 후보 S0 ~ S3

모든 전략은 BaseStrategy 를 상속하며, `decide(j, df, pos)` 에서
"직전에 마감된 봉 j(= i-1)까지의 정보만" 보고 다음 봉에 낼 주문을 반환합니다.
엔진이 j+1 봉에 그 주문을 체결시키므로 룩어헤드가 구조적으로 불가능합니다.

S0 : 현행 운영 로직 재현 (기준선)
S1 : 현행 + 결함 제거 (원가 이하 익절 금지 / 추격매수 차단 / 이격 부족 시 거래정지 /
                       ATR 손절 / N봉 시간청산 / 장마감 전 청산)
S2 : 추세추종 + VWAP 필터 (VWAP 위 + ADX 임계 + VWAP 눌림목 진입, 진입가 기준 ATR 트레일링)
S3 : 횡보 판정(ADX 낮음) 시에만 허용하는 VWAP 밴드 평균회귀
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from backtest.engine import CostModel, Decision, Position


class BaseStrategy:
    name = "BASE"

    def on_start(self, df: pd.DataFrame, cost: CostModel) -> None:
        self.cost = cost
        self._c = df["close"].to_numpy(float)
        self._h = df["high"].to_numpy(float)
        self._l = df["low"].to_numpy(float)
        self._vwap = df["vwap"].to_numpy(float)
        self._stdev = df["vwap_stdev"].to_numpy(float)
        self._atr = df["atr"].to_numpy(float)
        self._adx = df["adx"].to_numpy(float)
        self._rsi = df["rsi"].to_numpy(float)
        self._to_end = df["bars_to_session_end"].to_numpy(int)
        self._bar_in_sess = df["bar_in_session"].to_numpy(int)

    def on_session_start(self, i: int, df: pd.DataFrame) -> None:
        pass

    def on_entry(self, i: int, df: pd.DataFrame, pos: Position) -> None:
        pass

    def decide(self, j: int, df: pd.DataFrame, pos: Position) -> Decision:
        raise NotImplementedError


# ======================================================================================
# S0 — 현행 운영 로직 재현 (기준선)
# ======================================================================================
class S0_Current(BaseStrategy):
    """
    core/vwap/strategy.py + bot.py 의 동작을 그대로 재현합니다.

    무포지션:
      - 종가 < VWAP 이면 VWAP*(1-N%) 에 지정가 매수 (VWAP이 움직이면 취소 후 재주문 = 추격)
      - 종가 >= VWAP 이면 주문 전부 취소 (여기서 역선택이 발생)
    보유 중:
      - 종가 <= 진입가*(1-X%) 이면 시장가 손절
      - 종가 >= VWAP 이면 VWAP*(1+M%) 에 지정가 매도 (역시 VWAP 따라 계속 정정)
      - 그 외(HOLD)에는 걸어둔 매도 주문을 '취소'한다  <-- 운영 코드 9-4 else 분기 그대로
    손절 외 시간청산/장마감청산 없음 (운영 봇은 24시간 돌며 오버나잇 보유).
    """

    def __init__(self, n_percent=1.0, m_percent=1.0, x_percent=2.0, name="S0_현행재현"):
        self.n = n_percent
        self.m = m_percent
        self.x = x_percent
        self.name = name

    def decide(self, j: int, df: pd.DataFrame, pos: Position) -> Decision:
        c = self._c[j]
        vwap = self._vwap[j]

        if pos.is_long:
            stop = round(pos.entry_raw * (1.0 - self.x / 100.0), 2)
            if c <= stop:
                return Decision(exit_market=True, note="STOP_LOSS_MKT")
            target_sell = round(vwap * (1.0 + self.m / 100.0), 2)
            if c >= vwap or c >= target_sell:
                return Decision(sell_limit=target_sell)
            return Decision()  # HOLD -> 주문 취소 (아무 주문도 없음)

        if c < vwap:
            return Decision(buy_limit=round(vwap * (1.0 - self.n / 100.0), 2))
        return Decision()


# ======================================================================================
# S1 — 현행 + 결함 제거
# ======================================================================================
class S1_Fixed(BaseStrategy):
    """
    S0와 같은 '이동 VWAP 아래 매수' 아이디어는 유지하되, 비용 구조상의 결함을 제거합니다.

      (1) 원가 이하 익절 금지 : 매도 타겟의 하한을 '진입가 x (1 + 왕복비용 x min_profit_mult)' 로 고정
      (2) 추격매수 차단       : 같은 세션 안에서 매수 지정가는 내려가기만 하고 올라가지 않음.
                                또 지정가가 직전 종가보다 높으면(사실상 시장가 매수) 주문하지 않음.
      (3) 이격 부족 시 정지   : (VWAP - 매수지정가)/매수지정가 < 왕복비용 x edge_gate_mult 이면 진입 금지
      (4) ATR 손절            : 진입 시점 ATR 기준 고정 손절 (진입가 - atr_stop_mult x ATR)
      (5) N봉 시간청산        : max_hold_bars 봉 넘게 물려 있으면 시장가 청산
      (6) 장마감 전 청산      : 세션 종료 eod_exit_bars 봉 전부터는 시장가 청산 (오버나잇 금지)
      (7) 매도 주문 상시 유지 : S0처럼 HOLD 때 주문을 취소하지 않음
    """

    def __init__(
        self,
        n_percent=1.0,
        m_percent=1.0,
        min_profit_mult=1.5,
        atr_stop_mult=1.5,
        max_hold_bars=30,
        eod_exit_bars=5,
        edge_gate_mult=3.0,
        sell_anchor="vwap_m",   # "vwap_m" = VWAP*(1+M%), "vwap" = VWAP 복귀
        name="S1_결함제거",
    ):
        self.n = n_percent
        self.m = m_percent
        self.min_profit_mult = min_profit_mult
        self.atr_stop_mult = atr_stop_mult
        self.max_hold_bars = max_hold_bars
        self.eod_exit_bars = eod_exit_bars
        self.edge_gate_mult = edge_gate_mult
        self.sell_anchor = sell_anchor
        self.name = name
        self._session_buy_limit: Optional[float] = None
        self._entry_atr = 0.0

    def on_session_start(self, i: int, df: pd.DataFrame) -> None:
        self._session_buy_limit = None

    def on_entry(self, i: int, df: pd.DataFrame, pos: Position) -> None:
        self._entry_atr = float(self._atr[pos.entry_idx - 1]) if pos.entry_idx > 0 else 0.0
        self._session_buy_limit = None  # 체결됐으니 리셋

    def decide(self, j: int, df: pd.DataFrame, pos: Position) -> Decision:
        c = self._c[j]
        vwap = self._vwap[j]
        rt = self.cost.roundtrip_frac

        if pos.is_long:
            # (6) 장마감 전 강제청산
            if self._to_end[j] <= self.eod_exit_bars:
                return Decision(exit_market=True, note="EOD_EXIT")
            # (5) 시간청산
            if pos.bars_held >= self.max_hold_bars:
                return Decision(exit_market=True, note="TIME_EXIT")

            # (4) ATR 손절 (진입 시점 ATR 고정)
            atr = self._entry_atr if self._entry_atr > 0 else self._atr[j]
            stop = pos.entry_raw - self.atr_stop_mult * atr

            # (1) 원가 이하 익절 금지
            floor = pos.entry_raw * (1.0 + self.min_profit_mult * rt)
            anchor = vwap * (1.0 + self.m / 100.0) if self.sell_anchor == "vwap_m" else vwap
            target_sell = max(anchor, floor)
            return Decision(sell_limit=round(target_sell, 2), stop_price=round(stop, 2))

        # ---- 무포지션: 진입 판정 ----
        if self._to_end[j] <= self.eod_exit_bars:
            return Decision()  # 마감 직전에는 신규 진입 금지

        if c >= vwap:
            return Decision()

        limit = round(vwap * (1.0 - self.n / 100.0), 2)

        # (2) 추격매수 차단 : 지정가가 직전 종가 위면 사실상 시장가 매수 → 금지
        if limit >= c:
            return Decision()
        # (2) 같은 세션 안에서 지정가는 내려가기만
        if self._session_buy_limit is not None:
            limit = min(limit, self._session_buy_limit)
        self._session_buy_limit = limit

        # (3) 이격이 비용 대비 충분치 않으면 거래 정지
        if limit <= 0 or (vwap - limit) / limit < self.edge_gate_mult * rt:
            return Decision()

        return Decision(buy_limit=limit)


# ======================================================================================
# S2 — 추세추종 + VWAP 필터
# ======================================================================================
class S2_TrendPullback(BaseStrategy):
    """
    '가격이 VWAP 위에 있고 추세(ADX)가 살아있을 때, VWAP까지 눌릴 때 산다'

    진입 : 직전 종가 > VWAP  AND  ADX >= adx_threshold  -> VWAP 가격에 지정가 매수
    청산 : 진입가 기준 ATR 트레일링 스탑 (한 번 올라간 스탑은 내려오지 않음)
           + 시간청산(max_hold_bars) + 장마감 전 청산
    지정가 익절 없음 — 추세를 끝까지 태우는 게 목적.
    """

    def __init__(
        self,
        adx_threshold=20.0,
        atr_trail_mult=2.0,
        max_hold_bars=60,
        eod_exit_bars=5,
        name="S2_추세추종",
    ):
        self.adx_threshold = adx_threshold
        self.atr_trail_mult = atr_trail_mult
        self.max_hold_bars = max_hold_bars
        self.eod_exit_bars = eod_exit_bars
        self.name = name
        self._trail = None
        self._entry_atr = 0.0

    def on_session_start(self, i: int, df: pd.DataFrame) -> None:
        self._trail = None

    def on_entry(self, i: int, df: pd.DataFrame, pos: Position) -> None:
        self._entry_atr = float(self._atr[pos.entry_idx - 1]) if pos.entry_idx > 0 else 0.0
        atr = self._entry_atr if self._entry_atr > 0 else float(self._atr[i])
        self._trail = pos.entry_raw - self.atr_trail_mult * atr

    def decide(self, j: int, df: pd.DataFrame, pos: Position) -> Decision:
        c = self._c[j]
        vwap = self._vwap[j]
        atr = self._atr[j]

        if pos.is_long:
            if self._to_end[j] <= self.eod_exit_bars:
                return Decision(exit_market=True, note="EOD_EXIT")
            if pos.bars_held >= self.max_hold_bars:
                return Decision(exit_market=True, note="TIME_EXIT")

            candidate = pos.high_since_entry - self.atr_trail_mult * atr
            if self._trail is None:
                self._trail = pos.entry_raw - self.atr_trail_mult * (
                    self._entry_atr if self._entry_atr > 0 else atr
                )
            self._trail = max(self._trail, candidate)   # 트레일링: 올라가기만
            return Decision(stop_price=round(self._trail, 2))

        # ---- 무포지션 ----
        if self._to_end[j] <= self.eod_exit_bars:
            return Decision()
        if not (c > vwap and self._adx[j] >= self.adx_threshold):
            return Decision()
        if atr <= 0:
            return Decision()
        # VWAP 눌림목에 지정가 매수. 지정가가 종가보다 위면(= 이미 VWAP 아래) 진입 안 함.
        limit = round(vwap, 2)
        if limit >= c:
            return Decision()
        return Decision(buy_limit=limit)


# ======================================================================================
# S3 — 횡보 판정 시에만 허용하는 VWAP 밴드 평균회귀
# ======================================================================================
class S3_RangeBand(BaseStrategy):
    """
    '추세가 없을 때(ADX 낮음)만' VWAP - sigma x 표준편차 밴드 하단에서 매수하고 VWAP 복귀를 노립니다.
    추세장에서 밴드 하단을 받아치다 깨지는 걸 ADX 필터로 막는 게 핵심.

    진입 : ADX < adx_range_threshold  AND  밴드 하단까지의 이격이 왕복비용 x edge_gate_mult 이상
    청산 : max(VWAP, 진입가x(1+최소수익)) 지정가 매도 + ATR 손절 + 시간청산 + 장마감 청산
    """

    def __init__(
        self,
        sigma=1.5,
        adx_range_threshold=20.0,
        min_profit_mult=1.5,
        atr_stop_mult=1.5,
        max_hold_bars=30,
        eod_exit_bars=5,
        edge_gate_mult=3.0,
        name="S3_횡보밴드",
    ):
        self.sigma = sigma
        self.adx_range_threshold = adx_range_threshold
        self.min_profit_mult = min_profit_mult
        self.atr_stop_mult = atr_stop_mult
        self.max_hold_bars = max_hold_bars
        self.eod_exit_bars = eod_exit_bars
        self.edge_gate_mult = edge_gate_mult
        self.name = name
        self._session_buy_limit: Optional[float] = None
        self._entry_atr = 0.0

    def on_session_start(self, i: int, df: pd.DataFrame) -> None:
        self._session_buy_limit = None

    def on_entry(self, i: int, df: pd.DataFrame, pos: Position) -> None:
        self._entry_atr = float(self._atr[pos.entry_idx - 1]) if pos.entry_idx > 0 else 0.0
        self._session_buy_limit = None

    def decide(self, j: int, df: pd.DataFrame, pos: Position) -> Decision:
        c = self._c[j]
        vwap = self._vwap[j]
        rt = self.cost.roundtrip_frac

        if pos.is_long:
            if self._to_end[j] <= self.eod_exit_bars:
                return Decision(exit_market=True, note="EOD_EXIT")
            if pos.bars_held >= self.max_hold_bars:
                return Decision(exit_market=True, note="TIME_EXIT")
            atr = self._entry_atr if self._entry_atr > 0 else self._atr[j]
            stop = pos.entry_raw - self.atr_stop_mult * atr
            floor = pos.entry_raw * (1.0 + self.min_profit_mult * rt)
            target = max(vwap, floor)
            return Decision(sell_limit=round(target, 2), stop_price=round(stop, 2))

        if self._to_end[j] <= self.eod_exit_bars:
            return Decision()
        if self._adx[j] >= self.adx_range_threshold:
            return Decision()   # 추세장이면 평균회귀 금지

        sd = self._stdev[j]
        if sd <= 0:
            return Decision()
        limit = round(vwap - self.sigma * sd, 2)
        if limit <= 0 or limit >= c:
            return Decision()
        if self._session_buy_limit is not None:
            limit = min(limit, self._session_buy_limit)
        self._session_buy_limit = limit
        if (vwap - limit) / limit < self.edge_gate_mult * rt:
            return Decision()
        return Decision(buy_limit=limit)
