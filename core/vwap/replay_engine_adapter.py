"""전략 플러그인 → 연구 백테스트 엔진(backtest.engine) 연결 어댑터 (ADR-0007 D1·D2, 설계 문서 §3·§4.3).

backtest.engine.run_backtest 는 전략에게 name / on_start / on_session_start / on_entry / decide(j, df, pos) 를 요구하고,
df 에 session_date·vwap 컬럼이 있어야 합니다. 이 모듈이 두 가지를 제공합니다.

  build_engine_frame(strategy, bars, ctx)  봉 → strategy.prepare → session_date 부착 (엔진 입력 df)
  PluginEngineAdapter(strategy, ctx)       StrategySignal → 엔진 Decision 변환 (실시간 봇 주문 매핑과 같음)

  BUY        → Decision(buy_limit=target_buy)              (봇 9-3: 지정가 매수)
  SELL       → Decision(sell_limit=target_sell)            (봇 9-2: 지정가 매도)
  STOP_LOSS  → Decision(exit_market=True, "STOP_LOSS_MKT") (봇 9-1: 시장가 손절)
  HOLD/WAIT  → Decision()                                   (봇 9-4: 미체결 전부 취소, 새 주문 없음)
  exit_market=True (미래 전략) → Decision(exit_market=True, note=reason_code)

세션 날짜는 ADR-0008 SessionSpec(봇과 같은 규칙)의 '세션 시작일(YYYY-MM-DD)'입니다 — bot.get_session_date(고정 reset_time)
가 아닙니다(설계 문서 §3 의 해당 문장은 ADR-0008 로 대체됨).

backtest/indicators.py·data.py 는 import 하지 않습니다(ADR-0007 D2).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

from backtest.engine import Decision
from core.vwap.strategies.base import PositionView, StrategyContext, StrategySignal, TradingStrategy

STOP_LOSS_NOTE = "STOP_LOSS_MKT"


def add_session_date(df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
    """각 봉의 세션 날짜(세션 시작 시각의 KST 날짜) 컬럼 session_date 를 붙여 반환. = SessionSpec.session_key(봉 시각)."""
    out = df.copy()
    starts = ctx.session_spec().label_bars(out["time"])
    out["session_date"] = pd.Series(starts.values, index=out.index).dt.strftime("%Y-%m-%d")
    return out


def build_engine_frame(strategy: TradingStrategy, bars: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
    """원본 봉(time, open, high, low, close, volume) → 엔진 입력 df."""
    prepared = strategy.prepare(bars, ctx)
    return add_session_date(prepared.reset_index(drop=True), ctx)


def signal_to_decision(sig: StrategySignal) -> Decision:
    if sig.exit_market:
        return Decision(exit_market=True, note=sig.reason_code or "MARKET_EXIT")
    if sig.signal == "BUY":
        return Decision(buy_limit=sig.target_buy_price)
    if sig.signal == "SELL":
        return Decision(sell_limit=sig.target_sell_price)
    if sig.signal == "STOP_LOSS":
        return Decision(exit_market=True, note=STOP_LOSS_NOTE)
    return Decision()


class PluginEngineAdapter:
    """backtest.engine.run_backtest 에 넘기는 전략 객체. 판단은 전부 플러그인 evaluate 에 위임합니다."""

    def __init__(self, strategy: TradingStrategy, ctx: StrategyContext):
        self.strategy = strategy
        self.ctx = ctx
        self.name = f"{strategy.name}@v{strategy.version}"
        self.last_signal: Optional[StrategySignal] = None

    # --- backtest.engine 이 호출하는 훅 ---
    def on_start(self, df: pd.DataFrame, cost) -> None:
        self.cost = cost

    def on_session_start(self, i: int, df: pd.DataFrame) -> None:
        pass

    def on_entry(self, i: int, df: pd.DataFrame, pos) -> None:
        pass

    def decide(self, j: int, df: pd.DataFrame, pos, now: Optional[datetime] = None) -> Decision:
        """엔진이 '직전 마감봉 j' 로 호출. pos 는 backtest.engine.Position (entry_raw = 비용 미반영 체결가)."""
        view = PositionView(qty=float(pos.qty), entry_price=float(pos.entry_raw), bars_held=int(pos.bars_held))
        sig = self.strategy.evaluate(df, j, view, self.ctx, now=now)
        self.last_signal = sig
        return signal_to_decision(sig)
