"""VWAP 전략 플러그인 패키지 (ADR-0007 D1). 이번 단계는 S0(현행 운영 전략 래퍼)만 제공합니다.

사용 예 (리플레이):
    from core.vwap.strategies import S0CurrentStrategy, StrategyContext
    strategy = S0CurrentStrategy.from_config(cfg, "real")
    ctx = StrategyContext.from_config(cfg, "real")
"""
from core.vwap.strategies.base import (  # noqa: F401
    PositionView,
    StrategyContext,
    StrategySignal,
    TradingStrategy,
    normalize_prefix,
)
from core.vwap.strategies.s0_current import S0CurrentStrategy  # noqa: F401

__all__ = ["PositionView", "StrategyContext", "StrategySignal", "TradingStrategy", "normalize_prefix",
           "S0CurrentStrategy"]
