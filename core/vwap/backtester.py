import pandas as pd
import numpy as np
from core.vwap.strategy import VwapStrategy

class VwapBacktester:
    @staticmethod
    def run(broker, ticker: str, interval: str, n_percent: float, m_percent: float, x_percent: float, initial_balance: float) -> dict:
        """
        주어진 과거 캔들 데이터를 바탕으로 LIMIT VWAP 매매 전략을 가상 시뮬레이션하여 백테스트 리포트를 생성합니다.
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
        # 인덱스 1부터 시작하여 직전 봉의 VWAP을 기준으로 지정가 타겟가를 삼음
        for i in range(1, len(df)):
            prev_row = df.iloc[i - 1]
            curr_row = df.iloc[i]
            
            close = float(curr_row['close'])
            high = float(curr_row['high'])
            low = float(curr_row['low'])
            
            # 직전 봉 기준으로 도출된 타겟 지정가
            vwap = float(prev_row['vwap'])
            target_buy_price = vwap * (1.0 - n_percent / 100.0)
            target_sell_price = vwap * (1.0 + m_percent / 100.0)
            stop_loss_price = entry_price * (1.0 - x_percent / 100.0)

            # 포지션이 없는 경우 -> 매수 진입 시도
            if qty == 0:
                # 당일 저가(low)가 매수 지정 타겟 이하로 떨어졌다면 매수 체결
                if low <= target_buy_price:
                    # 전량 매수 규칙
                    qty = int(cash / target_buy_price)
                    if qty > 0:
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
