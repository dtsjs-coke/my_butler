"""
engine.py — 룩어헤드 없는 봉 단위(bar-by-bar) 백테스트 엔진

[설계 원칙 — 왜 이렇게 만들었는지]
기존 core/vwap/backtester.py 에는 결과를 실제보다 좋게 만드는 문제가 있었습니다.
  (a) 수수료/슬리피지가 0 이었음
  (b) 매도 체결가를 max(target_sell, vwap) 으로 잡아 '더 유리한 쪽'을 골라줌
  (c) 손절이 정확히 손절가에 체결된다고 가정 (갭 하락 무시)
  (d) 장마감/시간 청산이 없어 포지션이 무한정 유지됨
이 엔진은 그 4가지를 전부 보수적(= 나에게 불리한 쪽)으로 바꿉니다.

[체결 규칙 (전부 보수적)]
1. 의사결정은 '직전에 마감된 봉(i-1)'까지의 정보로만 한다. 그 결과 주문이 봉 i에서 처리된다.
   => 미래 정보(look-ahead)가 원천적으로 들어갈 수 없다.
2. 지정가 매수: 봉의 low <= 지정가 일 때만 체결. 체결가는 '지정가' 그대로.
   단, 봉 시가가 이미 지정가보다 낮으면(= 갭) 시가에 체결된 것으로 본다.
   (그 봉에서 실제로 거래되지 않은 가격에 체결시키지 않기 위함)
3. 지정가 매도: 봉의 high >= 지정가 일 때만 체결. 체결가는 '지정가' 그대로.
   단, 봉 시가가 이미 지정가보다 높으면(= 갭) 시가에 체결.
4. 손절(스탑, 시장가): 봉 시가가 이미 손절가 이하면 '시가'에 체결(= 갭이면 시가, 더 나쁜 가격).
   아니면 손절가에 체결.
5. 시장가 청산(시간청산/장마감청산): 다음 봉의 '시가'에 체결.
6. 한 봉 안에서 손절과 지정가매도가 둘 다 가능해 보이면 항상 '손절'이 먼저 체결된 것으로 본다.
7. 청산이 일어난 봉에서는 같은 봉에 재진입하지 않는다.
8. [줄서기(queue) 보수화 옵션] limit_fill_buffer_pct > 0 이면, 가격이 지정가를 '스치기만' 해서는
   체결로 인정하지 않고 그만큼 더 뚫고 지나가야 체결로 본다.
   현실에서는 내 주문 앞에 다른 주문이 줄 서 있어 스치는 정도로는 체결이 안 되기 때문이다.
   기본값 0(= 스치면 체결)은 지정가 전략에 유리한 낙관적 가정이므로, 민감도 분석으로 꼭 확인할 것.

[비용 모델]
- 왕복 수수료 fee_roundtrip_pct (기본 0.20%) => 한쪽당 0.10% 를 현금에서 차감
- 왕복 슬리피지 slippage_roundtrip_pct (기본 0.05%) => 한쪽당 0.025% 를 체결가에 불리하게 반영
  (매수는 비싸게, 매도는 싸게)
- 따라서 기본값 기준 왕복 총비용 = 0.25%. 즉 진입가 대비 최소 0.25%는 먹어야 본전.

[포지션 사이징]
- 매 거래마다 '초기자본 x k_percent' 만큼의 고정 금액(notional)을 투입합니다(복리 아님).
  전략의 '엣지(edge)' 자체를 비교하는 게 목적이므로, 복리로 인해 순서 효과가 섞이는 걸 피합니다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------------------
# 비용 모델
# --------------------------------------------------------------------------------------
@dataclass
class CostModel:
    fee_roundtrip_pct: float = 0.20        # 왕복 수수료 (%)
    slippage_roundtrip_pct: float = 0.05   # 왕복 슬리피지 (%)
    limit_fill_buffer_pct: float = 0.0     # 지정가 체결 인정 버퍼 (%) — 줄서기 보수화용

    @property
    def fee_side(self) -> float:
        """한쪽(편도) 수수료 비율 (0.001 = 0.1%)"""
        return self.fee_roundtrip_pct / 2.0 / 100.0

    @property
    def slip_side(self) -> float:
        """한쪽(편도) 슬리피지 비율"""
        return self.slippage_roundtrip_pct / 2.0 / 100.0

    @property
    def roundtrip_frac(self) -> float:
        """왕복 총비용 비율 (0.0025 = 0.25%). 전략이 '최소 목표수익'을 정할 때 쓴다."""
        return (self.fee_roundtrip_pct + self.slippage_roundtrip_pct) / 100.0

    def buy_price(self, px: float) -> float:
        return px * (1.0 + self.slip_side)

    def sell_price(self, px: float) -> float:
        return px * (1.0 - self.slip_side)

    def fee(self, notional: float) -> float:
        return abs(notional) * self.fee_side


# --------------------------------------------------------------------------------------
# 주문/포지션/거래 기록
# --------------------------------------------------------------------------------------
@dataclass
class Decision:
    """전략이 '다음 봉에 이렇게 해달라'고 엔진에게 내는 지시."""
    buy_limit: Optional[float] = None    # 무포지션일 때 걸어둘 지정가 매수가
    sell_limit: Optional[float] = None   # 보유 중일 때 걸어둘 지정가 매도가
    stop_price: Optional[float] = None   # 보유 중일 때의 손절가
    exit_market: bool = False            # True면 다음 봉 시가에 무조건 시장가 청산
    note: str = ""


@dataclass
class Position:
    qty: float = 0.0
    entry_price: float = 0.0     # 비용 반영된 실효 매입단가
    entry_raw: float = 0.0       # 비용 반영 전 체결가
    entry_idx: int = -1
    entry_time: Optional[pd.Timestamp] = None
    high_since_entry: float = 0.0
    low_since_entry: float = 0.0
    bars_held: int = 0

    @property
    def is_long(self) -> bool:
        return self.qty > 0


@dataclass
class Trade:
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees: float
    net_pnl: float
    ret_pct: float          # 진입 명목금액 대비 순손익률 (%)
    bars_held: int
    exit_reason: str


@dataclass
class BacktestResult:
    name: str
    trades: List[Trade] = field(default_factory=list)
    equity: Optional[pd.Series] = None
    initial_equity: float = 0.0
    notional: float = 0.0
    bars: int = 0
    sessions: int = 0


# --------------------------------------------------------------------------------------
# 엔진 본체
# --------------------------------------------------------------------------------------
def run_backtest(
    df: pd.DataFrame,
    strategy,
    cost: CostModel,
    initial_equity: float = 100_000.0,
    k_percent: float = 10.0,
    warmup_bars: int = 30,
    allow_fractional_shares: bool = False,
) -> BacktestResult:
    """
    df       : indicators.add_indicators() 를 거친 DataFrame
    strategy : BaseStrategy 를 상속한 전략 객체
    cost     : CostModel
    """
    required = {"time", "open", "high", "low", "close", "volume", "session_date", "vwap"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"df에 필요한 컬럼이 없습니다: {sorted(missing)}")

    n = len(df)
    if n <= warmup_bars + 2:
        raise ValueError("봉 개수가 너무 적어 백테스트를 할 수 없습니다.")

    notional = initial_equity * (k_percent / 100.0)
    buf = cost.limit_fill_buffer_pct / 100.0

    # numpy 배열로 뽑아두면 루프가 훨씬 빠릅니다.
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    times = df["time"].to_numpy()
    sess = df["session_date"].to_numpy()

    pos = Position()
    cash = initial_equity
    trades: List[Trade] = []
    equity_curve = np.full(n, initial_equity, dtype=float)

    strategy.on_start(df, cost)
    prev_session = None

    for i in range(warmup_bars, n):
        # ---------- 세션 전환 처리 ----------
        if sess[i] != prev_session:
            strategy.on_session_start(i, df)
            prev_session = sess[i]

        # ---------- 1) 직전 마감봉(i-1)까지의 정보로 의사결정 ----------
        decision = strategy.decide(i - 1, df, pos)

        exited_this_bar = False

        # ---------- 2) 보유 중이면 청산 판정 ----------
        if pos.is_long:
            pos.bars_held += 1
            exit_raw = None
            reason = ""

            if decision.exit_market:
                # 시장가 청산 → 이 봉의 시가에 체결
                exit_raw, reason = o[i], decision.note or "MARKET_EXIT"

            elif decision.stop_price is not None and l[i] <= decision.stop_price:
                # 손절: 시가가 이미 손절가 아래면 시가(= 갭이면 시가), 아니면 손절가
                exit_raw = o[i] if o[i] <= decision.stop_price else decision.stop_price
                reason = "STOP"

            elif (decision.sell_limit is not None
                  and h[i] >= decision.sell_limit * (1.0 + buf)):
                # 지정가 매도: 기본은 지정가 체결. 시가가 이미 지정가 위로 갭업했으면 시가 체결.
                exit_raw = max(o[i], decision.sell_limit)
                reason = "LIMIT_SELL"

            if exit_raw is not None:
                exit_px = cost.sell_price(exit_raw)
                proceeds = exit_px * pos.qty
                fee = cost.fee(proceeds)
                cash += proceeds - fee

                entry_notional = pos.entry_price * pos.qty
                entry_fee = cost.fee(pos.entry_raw * pos.qty)
                gross = (exit_raw - pos.entry_raw) * pos.qty
                net = (exit_px - pos.entry_price) * pos.qty - fee - entry_fee
                trades.append(
                    Trade(
                        entry_time=pd.Timestamp(pos.entry_time),
                        exit_time=pd.Timestamp(times[i]),
                        entry_price=pos.entry_price,
                        exit_price=exit_px,
                        qty=pos.qty,
                        gross_pnl=gross,
                        fees=fee + entry_fee,
                        net_pnl=net,
                        ret_pct=(net / entry_notional * 100.0) if entry_notional > 0 else 0.0,
                        bars_held=pos.bars_held,
                        exit_reason=reason,
                    )
                )
                pos = Position()
                exited_this_bar = True
            else:
                pos.high_since_entry = max(pos.high_since_entry, h[i])
                pos.low_since_entry = min(pos.low_since_entry, l[i])

        # ---------- 3) 무포지션이면 진입 판정 ----------
        if (not pos.is_long) and (not exited_this_bar) and decision.buy_limit is not None:
            limit = decision.buy_limit
            if l[i] <= limit * (1.0 - buf):
                # 기본은 지정가 체결. 시가가 이미 지정가 아래로 갭하락했으면 시가 체결.
                fill_raw = min(o[i], limit)
                fill_px = cost.buy_price(fill_raw)
                qty = notional / fill_px
                if not allow_fractional_shares:
                    qty = float(int(qty))
                if qty > 0:
                    spend = fill_px * qty
                    fee = cost.fee(spend)
                    if spend + fee <= cash:
                        cash -= spend + fee
                        pos = Position(
                            qty=qty,
                            entry_price=fill_px,
                            entry_raw=fill_raw,
                            entry_idx=i,
                            entry_time=pd.Timestamp(times[i]),
                            high_since_entry=h[i],
                            low_since_entry=l[i],
                            bars_held=0,
                        )
                        strategy.on_entry(i, df, pos)

        equity_curve[i] = cash + (pos.qty * c[i])

    # ---------- 4) 데이터 끝에 포지션이 남아있으면 마지막 종가로 정리 ----------
    if pos.is_long:
        exit_raw = c[n - 1]
        exit_px = cost.sell_price(exit_raw)
        proceeds = exit_px * pos.qty
        fee = cost.fee(proceeds)
        cash += proceeds - fee
        entry_notional = pos.entry_price * pos.qty
        entry_fee = cost.fee(pos.entry_raw * pos.qty)
        net = (exit_px - pos.entry_price) * pos.qty - fee - entry_fee
        trades.append(
            Trade(
                entry_time=pd.Timestamp(pos.entry_time),
                exit_time=pd.Timestamp(times[n - 1]),
                entry_price=pos.entry_price,
                exit_price=exit_px,
                qty=pos.qty,
                gross_pnl=(exit_raw - pos.entry_raw) * pos.qty,
                fees=fee + entry_fee,
                net_pnl=net,
                ret_pct=(net / entry_notional * 100.0) if entry_notional > 0 else 0.0,
                bars_held=pos.bars_held,
                exit_reason="DATA_END",
            )
        )
        equity_curve[n - 1] = cash

    equity_curve[:warmup_bars] = initial_equity

    return BacktestResult(
        name=strategy.name,
        trades=trades,
        equity=pd.Series(equity_curve, index=pd.to_datetime(df["time"])),
        initial_equity=initial_equity,
        notional=notional,
        bars=n,
        sessions=int(df["session_date"].nunique()),
    )


# --------------------------------------------------------------------------------------
# 성과 지표
# --------------------------------------------------------------------------------------
def summarize(result: BacktestResult) -> dict:
    """백테스트 결과를 표로 만들기 좋은 dict 로 요약합니다."""
    trades = result.trades
    n = len(trades)
    base = {
        "전략": result.name,
        "거래수": n,
        "승률%": np.nan,
        "평균이익%": np.nan,
        "평균손실%": np.nan,
        "손익비": np.nan,
        "기대값%/거래": np.nan,
        "기대값$/거래": np.nan,
        "총순손익$": 0.0,
        "총수익률%": 0.0,
        "MDD%": 0.0,
        "PF": np.nan,
        "평균보유봉": np.nan,
        "t-stat": np.nan,
    }
    if n == 0:
        return base

    rets = np.array([t.ret_pct for t in trades], dtype=float)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    wins = rets > 0
    losses = ~wins

    gross_win = pnls[pnls > 0].sum()
    gross_loss = -pnls[pnls < 0].sum()

    eq = result.equity.to_numpy(float)
    peak = np.maximum.accumulate(eq)
    dd = np.where(peak > 0, (peak - eq) / peak * 100.0, 0.0)

    avg_win = rets[wins].mean() if wins.any() else 0.0
    avg_loss = rets[losses].mean() if losses.any() else 0.0

    sd = rets.std(ddof=1) if n > 1 else 0.0
    tstat = (rets.mean() / (sd / np.sqrt(n))) if sd > 0 else np.nan

    base.update(
        {
            "승률%": round(wins.mean() * 100.0, 1),
            "평균이익%": round(avg_win, 4),
            "평균손실%": round(avg_loss, 4),
            "손익비": round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else np.nan,
            "기대값%/거래": round(rets.mean(), 4),
            "기대값$/거래": round(pnls.mean(), 2),
            "총순손익$": round(pnls.sum(), 2),
            "총수익률%": round(pnls.sum() / result.initial_equity * 100.0, 3),
            "MDD%": round(dd.max(), 3),
            "PF": round(gross_win / gross_loss, 2) if gross_loss > 0 else np.nan,
            "평균보유봉": round(np.mean([t.bars_held for t in trades]), 1),
            "t-stat": round(tstat, 2) if not np.isnan(tstat) else np.nan,
        }
    )
    return base


def exit_reason_breakdown(result: BacktestResult) -> pd.DataFrame:
    """청산 사유별 건수/평균수익률 — 전략이 '어떻게 죽는지'를 보기 위한 표."""
    if not result.trades:
        return pd.DataFrame()
    rows = [{"사유": t.exit_reason, "수익률%": t.ret_pct} for t in result.trades]
    d = pd.DataFrame(rows)
    return (
        d.groupby("사유")
        .agg(건수=("수익률%", "size"), 평균수익률=("수익률%", "mean"), 합계기여=("수익률%", "sum"))
        .round(4)
        .sort_values("건수", ascending=False)
    )
