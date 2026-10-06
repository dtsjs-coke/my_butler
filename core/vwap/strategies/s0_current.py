"""S0 — 현행 운영 전략(VwapStrategy)을 플러그인 인터페이스로 '감싼' 구현 (ADR-0007 D1).

재구현이 아니라 운영 코드를 그대로 호출합니다.
  prepare  = VwapStrategy.calculate_vwap(df, reset_time, session=ctx.session_spec())   ← bot.py 4단계와 같은 호출
  evaluate = VwapStrategy.get_signals(df.iloc[[j]], …)                                 ← bot.py 7단계와 같은 호출
             + 거래 시작 대기 중이고 rules.start_wait_blocks 가 True(무보유 또는 BUY)면 WAIT / WAIT_START_TIME 으로 덮어씀
               ← bot.py 7-1 과 같은 규칙 (ADR-0009: 보유 중 STOP_LOSS/SELL/HOLD 는 대기 중에도 그대로)

get_signals 는 넘겨받은 df 의 '마지막 행'만 읽으므로, 미리 계산한 df 의 j 행 하나를 넘기면
실시간 봇이 같은 봉을 마지막 행으로 받았을 때와 같은 결과가 O(1) 로 나옵니다(T-P1 검증).

알려진 차이(리플레이 결과 assumptions.not_modeled 에 표기): 실시간 봇은 매 주기 받은 봉 창(세션 시작부터, 최소 150봉)으로
RSI/ADX 를 새로 계산하므로 ewm 초기값이 창마다 다르고, 리플레이는 전체 구간으로 한 번 계산합니다.
VWAP·표준편차는 세션 누적이라 창이 세션 시작을 포함하면 같습니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from core.vwap.session import interval_minutes
from core.vwap.strategy import VwapStrategy
from core.vwap.strategies.base import PositionView, StrategyContext, StrategySignal, TradingStrategy
from core.vwap.strategies import rules


class S0CurrentStrategy(TradingStrategy):
    name = "S0_CURRENT"
    version = "1"

    def prepare(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        return VwapStrategy.calculate_vwap(df, ctx.reset_time, session=ctx.session_spec())

    def evaluate(self, df: pd.DataFrame, j: int, pos: PositionView, ctx: StrategyContext,
                 now: Optional[datetime] = None) -> StrategySignal:
        p = ctx.params
        raw = VwapStrategy.get_signals(
            df.iloc[[j]],
            p["n_percent"], p["m_percent"], p["x_percent"], pos.qty, pos.entry_price,
            use_adx_filter=p.get("use_adx_filter", False),
            adx_threshold=p.get("adx_threshold", 25.0),
            use_rsi_filter=p.get("use_rsi_filter", False),
            rsi_threshold=p.get("rsi_threshold", 30.0),
            use_vwap_band=p.get("use_vwap_band", False),
            vwap_band_sigma=p.get("vwap_band_sigma", 2.0),
        )
        signal = raw["signal"]
        reason_code = raw.get("reason_code") or ""
        reason_text = raw.get("reason_text") or ""

        # bot 7-1: 거래 시작 시각 대기 — 신규 진입만 WAIT 로 덮음 (ADR-0009: 보유 포지션의 손절/매도/유지는 그대로)
        if ctx.start_time and rules.start_wait_blocks(signal, pos.qty):
            if now is None:
                now = self.decision_time(df, j, ctx)
            session = ctx.session_spec()
            if rules.is_waiting_for_start(now, ctx.start_time, ctx.reset_time, session):
                sess_start = session.session_start(now)
                signal = "WAIT"
                reason_text = (f"거래 시작 시각 {ctx.start_time} 이전(세션 시작 {sess_start.strftime('%H:%M')}) "
                               f"→ 신규 매매 대기 (전략 판단: {reason_code or '-'})")
                reason_code = "WAIT_START_TIME"

        return StrategySignal(
            signal=signal,
            reason_code=reason_code,
            reason_text=reason_text,
            target_buy_price=float(raw.get("target_buy_price", 0.0)),
            target_sell_price=float(raw.get("target_sell_price", 0.0)),
            stop_loss_price=float(raw.get("stop_loss_price", 0.0)),
            filters=raw.get("filters") or {},
            indicators={
                "vwap": raw.get("vwap", 0.0),
                "adx": raw.get("adx", 0.0),
                "rsi": raw.get("rsi", 50.0),
                "vwap_stdev": raw.get("vwap_stdev", 0.0),
                "current_price": raw.get("current_price", 0.0),
            },
            exit_market=False,
        )

    @staticmethod
    def decision_time(df: pd.DataFrame, j: int, ctx: StrategyContext) -> datetime:
        """j 봉이 마감되는 시각 = 이 판단으로 낸 주문이 살아나는 시각 (time[j] + 봉 간격). 미래 행을 읽지 않음."""
        t = pd.Timestamp(df["time"].iloc[j]).to_pydatetime()
        return t + timedelta(minutes=interval_minutes(ctx.interval))
