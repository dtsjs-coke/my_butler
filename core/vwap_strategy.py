import pandas as pd
import numpy as np
from datetime import datetime

class VwapStrategy:
    @staticmethod
    def calculate_vwap(df: pd.DataFrame, reset_time_str: str = "22:30") -> pd.DataFrame:
        """
        주어진 봉 데이터프레임(df)에 당일 누적 VWAP을 계산하여 새로운 컬럼 'vwap'으로 추가하여 반환합니다.
        
        Parameters:
        - df: 'time' (YYYY-MM-DD HH:MM:SS 포맷 또는 datetime), 'open', 'high', 'low', 'close', 'volume' 컬럼을 갖는 DataFrame.
        - reset_time_str: VWAP 누적을 리셋할 시간 (HH:MM 포맷)
        
        수식:
        - VWAP = sum(Close * Volume) / sum(Volume) (당일 리셋 시점부터 누적)
        """
        if df.empty:
            return df

        df = df.copy()
        
        # 'time' 컬럼을 datetime형태로 변환
        if not pd.api.types.is_datetime64_any_dtype(df['time']):
            df['time'] = pd.to_datetime(df['time'])

        # 시간순으로 정렬 보장
        df = df.sort_values('time').reset_index(drop=True)
        
        # 계산의 편의를 위해 'typical_price' (고가, 저가, 종가의 평균) 혹은 'close'를 사용
        # 사용자 수식에 맞추기 위해 'close'를 대표가격으로 사용합니다.
        df['price_vol'] = df['close'] * df['volume']
        
        # 각 봉별로 당일 리셋 시점을 기준으로 그룹화(Group)하기 위한 'session_id' 생성
        # 예: 2026-06-19 22:30:00 가 세션의 시작이면, 그 이전 봉들과 이후 봉들을 구분
        # 세션 경계(리셋 시간)를 만날 때마다 카운터를 올려 세션 그룹 ID를 부여합니다.
        session_ids = []
        current_session = 0
        
        # 리셋 시간 파싱 (시, 분)
        try:
            reset_h, reset_m = map(int, reset_time_str.split(':'))
        except Exception:
            reset_h, reset_m = 22, 30  # 파싱 실패 시 기본 미국장 시작
            
        for i, row in df.iterrows():
            t = row['time']
            # 이전 봉과 날짜가 달라졌거나, 동일 날짜 내에서 리셋 시각을 막 경과한 시점인지 판별
            if i > 0:
                prev_t = df.loc[i - 1, 'time']
                
                # 날짜 경계선이 지난 경우
                if t.date() != prev_t.date():
                    current_session += 1
                # 날짜는 같으나 리셋 시각을 넘은 시점 (이전 봉은 리셋 시각 전, 현재 봉은 리셋 시각 이후인 경우)
                else:
                    # 리셋 기준 시간 생성
                    boundary_time = t.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
                    if prev_t < boundary_time <= t:
                        current_session += 1
                        
            session_ids.append(current_session)
            
        df['session_id'] = session_ids
        
        # 세션(당일 장 시작 세션)별 누적합 계산
        df['cum_pv'] = df.groupby('session_id')['price_vol'].cumsum()
        df['cum_vol'] = df.groupby('session_id')['volume'].cumsum()
        
        # 누적 거래량이 0인 에러 방지 처리 후 VWAP 계산
        df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_pv'] / df['cum_vol'], df['close'])
        
        # 임시 컬럼 삭제
        df.drop(columns=['price_vol', 'session_id', 'cum_pv', 'cum_vol'], inplace=True, errors='ignore')
        
        return df

    @staticmethod
    def get_signals(df: pd.DataFrame, 
                    n_percent: float, 
                    m_percent: float, 
                    x_percent: float, 
                    position_qty: float, 
                    entry_price: float) -> dict:
        """
        가장 최신 봉 데이터를 기반으로 진입/청산/손절 조건 및 지정가 타겟 가격을 연산합니다.
        
        Parameters:
        - df: calculate_vwap이 완료된 DataFrame (최소 1개 이상의 행 존재 필요)
        - n_percent: 매수 하방 이격 비율 (N%)
        - m_percent: 매도 상방 이격 비율 (M%)
        - x_percent: 손절 비율 (X%)
        - position_qty: 현재 보유 수량 (0이면 무포지션)
        - entry_price: 보유 중일 때의 평균 매수 단가
        
        Returns dict:
        {
            "signal": "BUY" / "SELL" / "STOP_LOSS" / "HOLD" / "WAIT",
            "vwap": 최신 VWAP 가격,
            "current_price": 최신 종가,
            "target_buy_price": 매수지정가 타겟,
            "target_sell_price": 매도지정가 타겟,
            "stop_loss_price": 손절가
        }
        """
        if df.empty or 'vwap' not in df.columns:
            return {"signal": "WAIT", "vwap": 0.0, "current_price": 0.0}

        latest = df.iloc[-1]
        current_price = float(latest['close'])
        vwap = float(latest['vwap'])

        # 타겟 가격 산출
        target_buy_price = vwap * (1.0 - n_percent / 100.0)
        target_sell_price = vwap * (1.0 + m_percent / 100.0)
        
        # 1원/0.01달러 단위 정밀도를 위한 라운딩 (필요시 호출부에서 호가 단위로 보정 가능)
        target_buy_price = round(target_buy_price, 2)
        target_sell_price = round(target_sell_price, 2)

        stop_loss_price = 0.0
        signal = "WAIT"

        if position_qty > 0:
            # 포지션 보유 중인 경우 -> 청산 또는 손절 조건 체크
            stop_loss_price = entry_price * (1.0 - x_percent / 100.0)
            stop_loss_price = round(stop_loss_price, 2)

            # 1. 손절 조건 우선 판정
            if current_price <= stop_loss_price:
                signal = "STOP_LOSS"
            # 2. 청산 조건: VWAP 상향 돌파 또는 M% 상방 이격 타겟가 도달
            elif current_price >= vwap or current_price >= target_sell_price:
                signal = "SELL"
            else:
                signal = "HOLD"
        else:
            # 포지션이 없는 경우 -> 매수 조건 체크
            # 현재가가 VWAP보다 아래에 위치해 있을 때 매수 조건 감시
            if current_price < vwap:
                # 실제로 현재가가 타겟 매수가 이하로 떨어졌을 때 진입 (혹은 주문 감시)
                # 봇 루프에서는 매수 준비를 위해 'Target Buy Price'에 지정가를 계속 배치함.
                signal = "BUY"
            else:
                signal = "WAIT"

        return {
            "signal": signal,
            "vwap": vwap,
            "current_price": current_price,
            "target_buy_price": target_buy_price,
            "target_sell_price": target_sell_price,
            "stop_loss_price": stop_loss_price
        }
