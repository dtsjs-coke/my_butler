import pandas as pd
import numpy as np
from datetime import datetime
from core.vwap.session import SessionSpec, to_kst_naive

class VwapStrategy:
    @staticmethod
    def calculate_vwap(df: pd.DataFrame, reset_time_str: str = "22:30", session: SessionSpec = None) -> pd.DataFrame:
        """
        봉 데이터프레임(df)에 '세션 누적' VWAP 과 거래량가중 표준편차를 계산해 컬럼으로 추가합니다. (ADR-0008)

        Parameters:
        - df: 'time'(KST naive 또는 tz-aware), 'open','high','low','close','volume' 컬럼
        - reset_time_str: session 을 주지 않았을 때 쓰는 리셋 시각(HH:MM, KST). 이 값을 매일 그대로 사용
        - session: core.vwap.session.SessionSpec. 봇은 SessionSpec.for_market(...) 결과를 넘깁니다
          (미국 종목 + 리셋 22:30/23:30 이면 서머타임에 따라 22:30↔23:30 자동)

        수식(기존과 동일): VWAP = Σ(Close×Volume) / ΣVolume  (세션 시작부터 누적, 누적거래량 0 이면 종가)
                          stdev = sqrt( Σ Volume×(Close - 그 시점 VWAP)² / ΣVolume )

        ADR-0008 에서 바뀐 점: 세션은 '리셋 시각 ~ 다음 리셋 시각' 으로만 나뉩니다.
        예전처럼 달력 날짜(KST 자정)가 바뀌었다고 세션을 끊지 않습니다(미국장 도중 00:00 VWAP 초기화 버그 수정).

        추가 컬럼: vwap, vwap_stdev, rsi, adx, session_start(그 봉이 속한 세션의 시작 시각, KST naive)
        'time' 컬럼은 KST naive 로 정규화되어 반환됩니다(tz-aware 입력은 KST 로 변환).
        """
        if df.empty:
            return df

        df = df.copy()
        df['time'] = to_kst_naive(df['time'])
        df = df.sort_values('time').reset_index(drop=True)
        spec = session or SessionSpec.reset_time_mode(reset_time_str)

        # 각 봉이 속한 세션(시작 시각)으로 그룹화 — 자정 경계 없음
        sess_start = spec.label_bars(df['time'])
        df['session_start'] = sess_start.values
        grp = df['session_start']

        df['price_vol'] = df['close'] * df['volume']
        df['cum_pv'] = df['price_vol'].groupby(grp).cumsum()
        df['cum_vol'] = df['volume'].groupby(grp).cumsum()

        # 누적 거래량이 0인 에러 방지 처리 후 VWAP 계산
        df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_pv'] / df['cum_vol'].where(df['cum_vol'] > 0, 1.0), df['close'])

        # VWAP 표준편차 (Volume Weighted Standard Deviation) 계산
        df['price_vwap_diff_sq'] = df['volume'] * ((df['close'] - df['vwap']) ** 2)
        df['cum_diff_sq'] = df['price_vwap_diff_sq'].groupby(grp).cumsum()
        df['vwap_stdev'] = np.sqrt(np.where(df['cum_vol'] > 0,
                                            df['cum_diff_sq'] / df['cum_vol'].where(df['cum_vol'] > 0, 1.0), 0))

        # 보조 지표 계산 (RSI & ADX) — 받은 봉 전체로 계산 (기존과 동일)
        df['rsi'] = VwapStrategy.calculate_rsi(df, period=14)
        df['adx'] = VwapStrategy.calculate_adx(df, period=14)

        # 임시 컬럼 삭제
        df.drop(columns=['price_vol', 'cum_pv', 'cum_vol', 'price_vwap_diff_sq', 'cum_diff_sq'],
                inplace=True, errors='ignore')
        return df

    @staticmethod
    def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """RSI(상대강도지수)를 웰더스 EMA 기반으로 계산합니다."""
        if len(df) < period + 1:
            return pd.Series(50.0, index=df.index)
        
        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        
        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
        
        rs = avg_gain / (avg_loss + 1e-9)
        rsi = 100 - (100 / (1 + rs))
        return rsi

    @staticmethod
    def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """ADX(평균 방향성 지수)를 계산하여 반환합니다."""
        if len(df) < period * 2:
            return pd.Series(0.0, index=df.index)
            
        high = df['high']
        low = df['low']
        close = df['close']
        
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        up_move = high.diff()
        down_move = low.shift(1) - low
        
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        
        plus_dm.loc[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm.loc[(down_move > up_move) & (down_move > 0)] = down_move
        
        atr = tr.ewm(alpha=1/period, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr + 1e-9)
        minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / (atr + 1e-9)
        
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
        adx = dx.ewm(alpha=1/period, adjust=False).mean()
        return adx

    @staticmethod
    def get_signals(df: pd.DataFrame, 
                    n_percent: float, 
                    m_percent: float, 
                    x_percent: float, 
                    position_qty: float, 
                    entry_price: float,
                    use_adx_filter: bool = False,
                    adx_threshold: float = 25.0,
                    use_rsi_filter: bool = False,
                    rsi_threshold: float = 30.0,
                    use_vwap_band: bool = False,
                    vwap_band_sigma: float = 2.0) -> dict:
        """
        가장 최신 봉 데이터를 기반으로 진입/청산/손절 조건 및 지정가 타겟 가격을 연산합니다.
        
        Parameters:
        - df: calculate_vwap이 완료된 DataFrame (최소 1개 이상의 행 존재 필요)
        - n_percent: 매수 하방 이격 비율 (N%)
        - m_percent: 매도 상방 이격 비율 (M%)
        - x_percent: 손절 비율 (X%)
        - position_qty: 현재 보유 수량 (0이면 무포지션)
        - entry_price: 보유 중일 때의 평균 매수 단가
        - use_adx_filter: ADX 추세 필터 사용 여부
        - adx_threshold: ADX 임계값
        - use_rsi_filter: RSI 과매도 필터 사용 여부
        - rsi_threshold: RSI 임계값
        - use_vwap_band: VWAP 표준편차 밴드 사용 여부
        - vwap_band_sigma: 시그마 배수
        
        Returns dict:
        {
            "signal": "BUY" / "SELL" / "STOP_LOSS" / "HOLD" / "WAIT",
            "vwap": 최신 VWAP 가격,
            "current_price": 최신 종가,
            "target_buy_price": 매수지정가 타겟,
            "target_sell_price": 매도지정가 타겟,
            "stop_loss_price": 손절가,
            "adx": 최신 ADX 수치,
            "rsi": 최신 RSI 수치,
            "vwap_stdev": 최신 표준편차 수치
        }
        """
        if df.empty or 'vwap' not in df.columns:
            return {
                "signal": "WAIT", "vwap": 0.0, "current_price": 0.0,
                "reason_code": "DATA_UNAVAILABLE",
                "reason_text": "캔들/VWAP 데이터가 없어 판단 보류",
                "filters": {
                    "adx": {"enabled": bool(use_adx_filter), "value": 0.0, "threshold": float(adx_threshold), "blocking": False},
                    "rsi": {"enabled": bool(use_rsi_filter), "value": 50.0, "threshold": float(rsi_threshold), "blocking": False},
                },
            }

        latest = df.iloc[-1]
        current_price = float(latest['close'])
        vwap = float(latest['vwap'])

        # 타겟 가격 산출 (표준편차 밴드 또는 고정 퍼센트 방식 분기)
        if use_vwap_band and 'vwap_stdev' in latest:
            vwap_stdev = float(latest['vwap_stdev'])
            target_buy_price = vwap - (vwap_band_sigma * vwap_stdev)
            target_sell_price = vwap + (vwap_band_sigma * vwap_stdev)
        else:
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
                signal = "BUY"
                
                # ADX 추세 필터링 (ADX가 높으면 강력한 원웨이 추세장이므로 진입 보류)
                if use_adx_filter and 'adx' in latest:
                    adx = float(latest['adx'])
                    if adx >= adx_threshold:
                        signal = "WAIT"
                
                # RSI 과매도 필터링 (RSI가 threshold 이하로 낮아야만 매수 진입)
                if use_rsi_filter and 'rsi' in latest:
                    rsi = float(latest['rsi'])
                    if rsi > rsi_threshold:
                        signal = "WAIT"
            else:
                signal = "WAIT"

        adx_val = float(latest['adx']) if 'adx' in latest else 0.0
        rsi_val = float(latest['rsi']) if 'rsi' in latest else 50.0
        stdev_val = float(latest['vwap_stdev']) if 'vwap_stdev' in latest else 0.0

        # --- 판단 사유(관측성 전용 — 위의 signal 판정 결과를 바꾸지 않습니다) ---
        # filters[*].blocking: "필터가 켜져 있고, 현재 지표값이면 신규 매수 진입을 막는 상태"
        #   (보유 중이거나 현재가가 VWAP 위라 매수 판단 자체가 없을 때도 지표 상태 표시는 그대로 합니다)
        adx_blocking = bool(use_adx_filter) and 'adx' in latest and adx_val >= adx_threshold
        rsi_blocking = bool(use_rsi_filter) and 'rsi' in latest and rsi_val > rsi_threshold
        filters = {
            "adx": {"enabled": bool(use_adx_filter), "value": round(adx_val, 2),
                    "threshold": float(adx_threshold), "blocking": bool(adx_blocking)},
            "rsi": {"enabled": bool(use_rsi_filter), "value": round(rsi_val, 2),
                    "threshold": float(rsi_threshold), "blocking": bool(rsi_blocking)},
        }
        reason_code, reason_text = VwapStrategy._explain(
            signal, position_qty, current_price, vwap, target_buy_price, target_sell_price,
            stop_loss_price, adx_val, adx_threshold, adx_blocking, rsi_val, rsi_threshold, rsi_blocking)

        return {
            "signal": signal,
            "vwap": vwap,
            "current_price": current_price,
            "target_buy_price": target_buy_price,
            "target_sell_price": target_sell_price,
            "stop_loss_price": stop_loss_price,
            "adx": round(adx_val, 2),
            "rsi": round(rsi_val, 2),
            "vwap_stdev": round(stdev_val, 2),
            "reason_code": reason_code,
            "reason_text": reason_text,
            "filters": filters,
        }

    @staticmethod
    def _explain(signal, position_qty, current_price, vwap, target_buy_price, target_sell_price,
                 stop_loss_price, adx_val, adx_threshold, adx_blocking, rsi_val, rsi_threshold, rsi_blocking):
        """get_signals 의 판정 결과를 (reason_code, 한글 한 문장)으로 설명합니다.

        코드 목록 (전략 단계):
          보유:   STOP_LOSS / SELL_ARMED / HOLD
          무포지션: BUY_ARMED / FILTER_ADX / FILTER_RSI / ABOVE_VWAP
        (WAIT_START_TIME, BUDGET_SHORT, DAILY_LOSS_STOP, DATA_UNAVAILABLE, BOT_STOPPED 등은 봇이 덮어씁니다)
        """
        cp, vw = current_price, vwap
        if position_qty > 0:
            if signal == "STOP_LOSS":
                return "STOP_LOSS", f"현재가 {cp:.2f} ≤ 손절가 {stop_loss_price:.2f} → 시장가 손절"
            if signal == "SELL":
                if cp >= vw:
                    return "SELL_ARMED", f"현재가 {cp:.2f} ≥ VWAP {vw:.2f} (상향 돌파) → 매도 지정가 {target_sell_price:.2f}"
                return "SELL_ARMED", f"현재가 {cp:.2f} ≥ 매도 타겟 {target_sell_price:.2f} → 매도 지정가 {target_sell_price:.2f}"
            return "HOLD", f"현재가 {cp:.2f} < VWAP {vw:.2f}, 청산 대기 — 손절가 {stop_loss_price:.2f}"

        if signal == "BUY":
            return "BUY_ARMED", f"현재가 {cp:.2f} < VWAP {vw:.2f} → 매수 지정가 {target_buy_price:.2f} 대기"
        if cp < vw:
            # 가격 조건은 충족했지만 필터가 진입을 막은 경우 (ADX 를 먼저 표시)
            if adx_blocking:
                return "FILTER_ADX", f"ADX {adx_val:.1f} ≥ {adx_threshold:g} 강한 추세 → 진입 보류"
            if rsi_blocking:
                return "FILTER_RSI", f"RSI {rsi_val:.1f} > {rsi_threshold:g} 과매도 아님 → 진입 보류"
        return "ABOVE_VWAP", f"현재가 {cp:.2f} ≥ VWAP {vw:.2f} → 매수 대기 (VWAP 아래로 내려오면 진입)"
