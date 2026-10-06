"""전략 플러그인 공통 인터페이스 (ADR-0007 D1, 설계 문서 §3).

[무엇을 위한 것인가]
같은 매매 전략을 '실시간 봇 / 섀도우 / 리플레이(연구 엔진)' 세 곳에서 똑같은 코드로 돌리기 위한 약속입니다.
전략은 두 가지만 구현하면 됩니다.

  prepare(df, ctx)            봉 전체에 '과거만 보는' 지표를 한 번에 계산해 붙인 df 를 반환
  evaluate(df, j, pos, ctx)   prepare 결과의 j 행까지만 보고 이번 판단(StrategySignal)을 반환

[인과성 계약]  prepare 결과의 j 행 값은 j 이후 행이 바뀌거나 잘려도 변하면 안 됩니다(미래 정보 금지).
               evaluate(j) 도 j 행 이하만 읽어야 합니다. scripts/test_vwap_stage3_plugin.py T-P2 가 검증합니다.

[세션 기준]   세션 경계는 ADR-0008 의 core.vwap.session.SessionSpec 하나로만 정합니다.
               StrategyContext.session_spec() 이 봇(bot.py)과 같은 규칙(SessionSpec.for_market)으로 만들어 줍니다.

이번 단계(SR-1)에서 운영 REAL 봇은 이 인터페이스를 쓰지 않습니다(ADR-0007 D1 — REAL 연결은 4단계에서 별도 결정).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional

import pandas as pd

from core.vwap.session import SessionSpec

# 봇이 설정 dict 에서 읽는 전략 파라미터와 기본값 — bot.py _loop_step_body 의 config[...] / config.get(..., 기본값) 과 같음.
# (값이 None 이면 필수 키: bot 과 마찬가지로 없으면 KeyError)
_PARAM_SPEC = (
    ("n_percent", float, None),
    ("m_percent", float, None),
    ("x_percent", float, None),
    ("k_percent", float, None),
    ("initial_balance", float, None),
    ("max_daily_loss_limit", float, 5.0),
    ("use_adx_filter", bool, False),
    ("adx_threshold", float, 25.0),
    ("use_rsi_filter", bool, False),
    ("rsi_threshold", float, 30.0),
    ("use_vwap_band", bool, False),
    ("vwap_band_sigma", float, 2.0),
)

SIGNALS = ("BUY", "SELL", "STOP_LOSS", "HOLD", "WAIT")


def normalize_prefix(prefix: str) -> str:
    """설정 키 접두어. bot 과 같이 'VIRTUAL' 은 'virtual_1' 로 취급."""
    p = str(prefix or "").strip().lower()
    return "virtual_1" if p == "virtual" else p


@dataclass(frozen=True)
class StrategyContext:
    """전략이 판단할 때 필요한 '환경' — 종목/봉 간격/세션 규칙/파라미터. 한 번 만들면 바뀌지 않습니다(frozen).

    params 는 설정 dict 에서 접두어를 뗀 전략 파라미터(n/m/x/k_percent, 필터, 밴드 …, 타입 변환 완료)입니다.
    읽기 전용 매핑으로 보관되므로 실수로 바꿀 수 없습니다.
    """
    ticker: str
    market: str
    interval: str
    reset_time: str
    start_time: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    @classmethod
    def from_config(cls, config: dict, prefix: str) -> "StrategyContext":
        """VwapConfigManager.load_config() 결과와 모드 접두어('real', 'virtual_1' …)로 생성.
        필수 키·기본값·타입 변환은 bot.py _loop_step_body 와 같습니다."""
        p = normalize_prefix(prefix)
        params = {}
        for key, typ, default in _PARAM_SPEC:
            full = f"{p}_{key}"
            raw = config[full] if default is None else config.get(full, default)
            params[key] = typ(raw)
        return cls(
            ticker=config[f"{p}_ticker"],
            market=config[f"{p}_market"],
            interval=config[f"{p}_interval"],
            reset_time=config[f"{p}_reset_time"],
            start_time=config.get(f"{p}_start_time", ""),
            params=params,
        )

    def session_spec(self) -> SessionSpec:
        """봇과 같은 세션 규칙 (ADR-0008: 미국+22:30/23:30 → 서머타임 자동, 그 외 → reset_time 고정)."""
        return SessionSpec.for_market(self.market, self.reset_time, self.ticker)

    def param(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass(frozen=True)
class PositionView:
    """전략에게 보여주는 현재 포지션(읽기 전용).
    entry_price 는 비용 미반영 평균 체결가 — 운영 holdings.entry_price, 연구 엔진 Position.entry_raw 와 같은 의미."""
    qty: float = 0.0
    entry_price: float = 0.0
    bars_held: int = 0


@dataclass(frozen=True)
class StrategySignal:
    """전략의 한 번 판단 결과. 봇의 주문 매핑(9-1~9-4)과 연구 엔진 Decision 매핑이 이것 하나로 결정됩니다.

    signal      : BUY(지정가 매수 대기) / SELL(지정가 매도) / STOP_LOSS(시장가 청산) / HOLD / WAIT
    exit_market : 미래 전략의 시간·마감 청산용. True 면 signal 과 무관하게 시장가 청산. S0 는 항상 False
    indicators  : vwap, adx, rsi, vwap_stdev, current_price
    """
    signal: str
    reason_code: str
    reason_text: str
    target_buy_price: float
    target_sell_price: float
    stop_loss_price: float
    filters: Mapping[str, Any]
    indicators: Mapping[str, Any]
    exit_market: bool = False


class TradingStrategy(ABC):
    """전략 플러그인 기반 클래스. 전략 객체는 상태를 갖지 않는 것을 원칙으로 합니다
    (파라미터는 ctx.params 로 받음 → 실시간 설정 변경·리플레이 스냅샷이 모두 ctx 하나로 표현됨)."""

    name: str = "BASE"
    version: str = "0"

    @classmethod
    def from_config(cls, config: dict, prefix: str) -> "TradingStrategy":
        """설정에서 전략 인스턴스를 만듭니다. 기본 구현은 파라미터 없는 생성.
        (판단 파라미터는 StrategyContext.from_config(config, prefix).params 로 전달)"""
        return cls()

    @abstractmethod
    def prepare(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        """인과적 지표를 한 번에 계산해 반환 (미래 행을 참조하는 컬럼 금지).
        반환 df 는 time 오름차순, 0..n-1 인덱스여야 합니다."""

    @abstractmethod
    def evaluate(self, df: pd.DataFrame, j: int, pos: PositionView, ctx: StrategyContext,
                 now: Optional[datetime] = None) -> StrategySignal:
        """prepare 결과의 j 행까지만 보고 판단.

        now: '이 판단으로 낸 주문이 살아있게 되는 시각'(KST naive). 시각 규칙(거래 시작 대기 등)에 씁니다.
             실시간 호출자는 현재 시각을 넘기고, 생략하면 j 봉이 마감되는 시각(time[j] + 봉 간격)을 씁니다
             — 리플레이 엔진은 j 봉 판단을 j+1 봉에 체결시키므로 이 시각이 실시간 봇의 now 에 해당합니다.
        """
