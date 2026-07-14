import pandas as pd
import numpy as np
from core.vwap.strategy import VwapStrategy

class VwapBacktester:
    @staticmethod
    def run(broker, ticker: str, interval: str, n_percent: float, m_percent: float, x_percent: float, initial_balance: float,
            k_percent: float = 10.0,
            use_adx_filter: bool = False, adx_threshold: float = 25.0,
            use_rsi_filter: bool = False, rsi_threshold: float = 30.0,
            use_vwap_band: bool = False, vwap_band_sigma: float = 2.0) -> dict:
        """
        주어진 과거 캔들 데이터를 바탕으로 LIMIT VWAP 매매 전략을 가상 시뮬레이션하여 백테스트 리포트를 생성합니다.
        실제 트레이딩 봇(VWAPBot)과 동일한 VwapStrategy.get_signals()를 재사용하여, 예산 배분(k_percent)과
        ADX/RSI 필터, VWAP 밴드 설정까지 실봇 동작과 일치시킵니다.
        """
        # 과거 150개 분봉 수집 (Toss API 또는 가상 생성 캔들)
        df = broker.get_candles(ticker, interval, limit=150)
        if df.empty or len(df) < 10:
            raise ValueError("백테스트를 위한 과거 캔들 데이터가 충분하지 않습니다. (최소 10개 이상 필요)")

        # VWAP 계산 (기본 리셋 기준 시각은 22:30 적용)
        df = VwapStrategy.calculate_vwap(df, reset_time_str="22:30")

        cash = initial_balance
        qty = 0.0
        entry_price = 0.0
        total_trades = 0
        win_trades = 0
        peak_asset = initial_balance
        max_drawdown = 0.0

        # 시뮬레이션 타임스탬프 루프
        # 인덱스 1부터 시작하여 직전 봉까지의 데이터로 계산된 시그널을 기준으로 지정가 타겟가를 삼음 (룩어헤드 방지)
        for i in range(1, len(df)):
            curr_row = df.iloc[i]

            close = float(curr_row['close'])
            high = float(curr_row['high'])
            low = float(curr_row['low'])

            # 실봇과 동일한 신호 엔진으로 직전 봉까지의 데이터 기준 타겟가/시그널 산출
            signals = VwapStrategy.get_signals(
                df.iloc[:i], n_percent, m_percent, x_percent, qty, entry_price,
                use_adx_filter=use_adx_filter, adx_threshold=adx_threshold,
                use_rsi_filter=use_rsi_filter, rsi_threshold=rsi_threshold,
                use_vwap_band=use_vwap_band, vwap_band_sigma=vwap_band_sigma
            )
            entry_signal = signals["signal"]
            vwap = signals["vwap"]
            target_buy_price = signals["target_buy_price"]
            target_sell_price = signals["target_sell_price"]
            stop_loss_price = signals["stop_loss_price"]

            # 포지션이 없는 경우 -> 매수 진입 시도 (ADX/RSI 필터에 의해 시그널이 WAIT로 바뀌면 진입 보류)
            if qty == 0:
                # 당일 저가(low)가 매수 지정 타겟 이하로 떨어졌다면 매수 체결
                if entry_signal == "BUY" and low <= target_buy_price:
                    # 실봇과 동일하게 k_percent 비율만큼만 예산 투입 (보유 현금 한도 내로 제한)
                    base_balance = initial_balance if initial_balance > 0.0 else cash
                    invest_cash = base_balance * (k_percent / 100.0)
                    if invest_cash > cash:
                        invest_cash = cash
                    buy_qty = int(invest_cash / target_buy_price)
                    if buy_qty > 0:
                        qty = buy_qty
                        cash -= (qty * target_buy_price)
                        entry_price = target_buy_price

            # 포지션이 있는 경우 -> 청산 또는 손절 시도
            else:
                # 1. 손절 검사
                if low <= stop_loss_price:
                    cash += (qty * stop_loss_price)
                    pnl = (stop_loss_price - entry_price) * qty
                    qty = 0.0
                    entry_price = 0.0
                    total_trades += 1

                # 2. 청산 검사 (상방 이격 또는 VWAP 상향 돌파)
                elif high >= target_sell_price or high >= vwap:
                    # 체결가는 더 유리한 조건인 target_sell_price로 처리
                    fill_price = max(target_sell_price, vwap)
                    cash += (qty * fill_price)
                    pnl = (fill_price - entry_price) * qty

                    if pnl > 0:
                        win_trades += 1

                    qty = 0.0
                    entry_price = 0.0
                    total_trades += 1

            # 매 타임스텝마다 총 평가 자산 평가
            current_asset = cash + (qty * close)
            if current_asset > peak_asset:
                peak_asset = current_asset

            drawdown = ((peak_asset - current_asset) / peak_asset) * 100.0 if peak_asset > 0 else 0.0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # 미청산 포지션 강제 청산 처리 (최종 수익률 계산을 위해 종가 기준 청산)
        if qty > 0:
            cash += (qty * df.iloc[-1]['close'])
            pnl = (df.iloc[-1]['close'] - entry_price) * qty
            if pnl > 0:
                win_trades += 1
            qty = 0.0
            total_trades += 1

        final_asset = cash
        roi = ((final_asset - initial_balance) / initial_balance) * 100.0
        win_rate = (win_trades / total_trades * 100.0) if total_trades > 0 else 0.0

        return {
            "initial_balance": round(initial_balance, 2),
            "final_asset": round(final_asset, 2),
            "roi": round(roi, 2),
            "mdd": round(max_drawdown, 2),
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2)
        }
