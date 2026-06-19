import os
import time
import logging
import threading
import traceback
from datetime import datetime
from core.vwap_config_manager import VwapConfigManager
from core.vwap_broker import TossBroker, VirtualBroker
from core.vwap_strategy import VwapStrategy

# 로그 디렉토리 및 파일 설정
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_PATH = os.path.join(PROJECT_ROOT, "trading_bot.log")

def setup_logger():
    """트레이딩 봇 전용 파일 및 콘솔 로거를 설정합니다."""
    logger = logging.getLogger("vwap_bot")
    logger.setLevel(logging.INFO)
    
    # 중복 추가 방지
    if logger.handlers:
        return logger

    # 파일 핸들러 (UTF-8 인코딩)
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    
    # 콘솔 핸들러
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    
    # 포맷터 설정
    formatter = logging.Formatter('[%(asctime)s] %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()


class VWAPBot:
    def __init__(self):
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        
        # 봇 상태 캐시 (웹 대시보드 API 조회용)
        self.status_cache = {
            "is_running": False,
            "mode": "VIRTUAL",
            "ticker": "AAPL",
            "market": "US",
            "current_price": 0.0,
            "vwap": 0.0,
            "target_buy_price": 0.0,
            "target_sell_price": 0.0,
            "stop_loss_price": 0.0,
            "signal": "WAIT",
            "cash": 0.0,
            "holdings": {},
            "open_orders": [],
            "last_updated": ""
        }
        
        # 브로커 캐시
        self.real_broker = None
        self.virtual_broker = None

    def start(self):
        """트레이딩 봇 백그라운드 스레드를 시작합니다."""
        with self._lock:
            if self.running:
                logger.warning("트레이딩 봇이 이미 가동 중입니다.")
                return False
            
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            logger.info("⚡ 트레이딩 봇 백그라운드 엔진이 시작되었습니다.")
            return True

    def stop(self):
        """트레이딩 봇 백그라운드 스레드를 종료합니다."""
        with self._lock:
            if not self.running:
                logger.warning("트레이딩 봇이 작동 중이 아닙니다.")
                return False
            
            self.running = False
            logger.info("🛑 트레이딩 봇 백그라운드 엔진 정지 요청이 접수되었습니다.")
            return True

    def get_status(self) -> dict:
        """현재 봇 상태 캐시를 반환합니다."""
        self.status_cache["is_running"] = self.running
        return self.status_cache

    def _run_loop(self):
        """백그라운드 스레드에서 무한 루프로 실행되는 메인 봇 주기 실행부입니다."""
        logger.info("트레이딩 봇 루프 스레드가 기동되었습니다.")
        
        while self.running:
            try:
                self._loop_step()
            except Exception as e:
                logger.error(f"❌ 봇 루프 실행 중 에러 발생: {e}")
                logger.error(traceback.format_exc())
            
            # 1분 단위로 주기적 실행 (테스트 및 실시간 반응을 위해 폴링 딜레이)
            # 사용자가 강제 중지했을 때 즉각 루프를 탈출하기 위해 1초씩 나눠서 슬립 수행
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1)
                
        logger.info("트레이딩 봇 루프 스레드가 완전히 종료되었습니다.")

    def _loop_step(self):
        """한 주기의 전략 계산 및 주문 정정 작업을 수행합니다."""
        # 1. 설정 실시간 로드
        config = VwapConfigManager.load_config()
        
        mode = config["mode"]
        ticker = config["ticker"]
        market = config["market"]
        interval = config["interval"]
        n_percent = float(config["n_percent"])
        m_percent = float(config["m_percent"])
        x_percent = float(config["x_percent"])
        k_percent = float(config["k_percent"])
        reset_time = config["reset_time"]
        initial_balance = float(config["initial_balance"])
        max_investment_limit = float(config.get("max_investment_limit", 5000000.0))

        # 2. 브로커 초기화 및 스위칭
        # 실거래 브로커는 API 토큰 관리를 위해 1회 생성 유지
        if (not self.real_broker or 
            self.real_broker.client_id != config["toss_client_id"] or 
            self.real_broker.client_secret != config["toss_client_secret"] or 
            self.real_broker.account_seq != config["toss_account_seq"]):
            
            self.real_broker = TossBroker(
                client_id=config["toss_client_id"],
                client_secret=config["toss_client_secret"],
                account_seq=config["toss_account_seq"]
            )
            
        # 가상 거래 브로커
        if not self.virtual_broker or self.virtual_broker.initial_balance != initial_balance:
            self.virtual_broker = VirtualBroker(
                initial_balance=initial_balance,
                ticker_source_broker=self.real_broker
            )

        # 현재 실행 모드에 맞는 브로커 선택
        broker = self.real_broker if mode == "REAL" else self.virtual_broker

        logger.info(f"▶ [{mode} 모드] {ticker} ({market}) 전략 분석 주기 시작...")

        # 3. 최신 캔들 수집
        df = broker.get_candles(ticker, interval, 150)
        if df.empty:
            logger.error(f"[{ticker}] 캔들 데이터를 가져오지 못했습니다. 다음 주기에 재시도합니다.")
            return

        # 4. 실시간 VWAP 계산
        df = VwapStrategy.calculate_vwap(df, reset_time)
        latest_row = df.iloc[-1]
        current_price = float(latest_row['close'])
        high = float(latest_row['high'])
        low = float(latest_row['low'])

        # 5. 가상 브로커인 경우, 먼저 가상 주문 매칭 엔진 업데이트 수행
        if mode == "VIRTUAL":
            self.virtual_broker.update_simulation(ticker, current_price, high, low)

        # 6. 자산 및 포지션 조회
        balance = broker.get_balance()
        cash = balance["cash"]
        holdings = balance["holdings"]
        
        # 포지션 정보 파싱
        holding_info = holdings.get(ticker, {"qty": 0.0, "entry_price": 0.0})
        qty = holding_info["qty"]
        entry_price = holding_info["entry_price"]

        # 7. 전략 시그널 도출
        signals = VwapStrategy.get_signals(df, n_percent, m_percent, x_percent, qty, entry_price)
        signal = signals["signal"]
        vwap = signals["vwap"]
        target_buy_price = signals["target_buy_price"]
        target_sell_price = signals["target_sell_price"]
        stop_loss_price = signals["stop_loss_price"]

        logger.info(f"현재가: {current_price:.2f} | VWAP: {vwap:.2f} | 시그널: {signal}")
        if qty > 0:
            logger.info(f"보유량: {qty}주 | 평단가: {entry_price:.2f} | 청산타겟: {target_sell_price:.2f} | 손절가: {stop_loss_price:.2f}")
        else:
            logger.info(f"진입타겟: {target_buy_price:.2f}")

        # 8. 미체결 지정가 주문 조회
        open_orders = broker.get_open_orders(ticker)

        # 9. 주문 집행 및 Cancel & Replace 정정 메커니즘
        
        # 9-1. 손절 조건 판정 (STOP_LOSS)
        if signal == "STOP_LOSS":
            logger.warning(f"🚨 손절 기준선({stop_loss_price:.2f}) 하향 이탈! 즉시 시장가 청산을 시도합니다.")
            # 가상 모드 손절
            if mode == "VIRTUAL":
                self.virtual_broker.force_market_stop_loss(ticker, current_price)
            # 실거래 모드 손절
            else:
                # 미체결 주문 전체 취소
                for order in open_orders:
                    broker.cancel_order(order["order_id"])
                # 즉시 시장가 전량 매도 주문 제출
                order_id = broker.place_order(ticker, "SELL", 0.0, qty, "MARKET")
                if order_id:
                    logger.info(f"정상 시장가 손절 주문 제출 성공. (ID: {order_id})")
            return

        # 9-2. 매도 청산 시그널 (SELL)
        elif signal == "SELL":
            # 이미 들어간 매도 주문 검색
            sell_orders = [o for o in open_orders if o["side"] == "SELL"]
            
            if sell_orders:
                existing_order = sell_orders[0]
                # 기존 주문 가격과 새로운 타겟 매도가 비교
                if abs(existing_order["price"] - target_sell_price) > 0.01:
                    logger.info(f"🔄 VWAP 변동 감지! 매도 주문 정정 수행: {existing_order['price']:.2f} -> {target_sell_price:.2f}")
                    # Cancel & Replace
                    if broker.cancel_order(existing_order["order_id"]):
                        new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                        if new_id:
                            logger.info(f"매도 정정 주문 제출 완료. (ID: {new_id})")
                else:
                    logger.info("기존 매도 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
            else:
                # 신규 매도 지정가 주문 제출
                logger.info(f"📉 청산 조건 충족. 지정가 매도 주문 제출: {target_sell_price:.2f} @ {qty}주")
                new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                if new_id:
                    logger.info(f"매도 지정가 주문 제출 성공. (ID: {new_id})")

        # 9-3. 매수 진입 시그널 (BUY)
        elif signal == "BUY":
            # 이미 들어간 매수 주문 검색
            buy_orders = [o for o in open_orders if o["side"] == "BUY"]
            
            # 투자 가능 금액 산출
            invest_cash = cash * (k_percent / 100.0)
            
            # [안전장치 1] 최대 1회 투자 한도액 제한 적용
            if invest_cash > max_investment_limit:
                logger.info(f"🛡️ [안전장치] 가용 자금({invest_cash:.2f})이 최대 한도({max_investment_limit:.2f})를 초과하여 제한 적용합니다.")
                invest_cash = max_investment_limit
                
            # [안전장치 2] 미수/신용 거래 원천 금지 (순수 현금 예수금 이하로만 제한)
            if invest_cash > cash:
                logger.warning(f"🛡️ [안전장치] 가용 예산({invest_cash:.2f})이 보유 현금({cash:.2f})을 초과하여 보유 잔고로 제한합니다 (미수 방지).")
                invest_cash = cash
            
            # 미국 주식은 소수점 거래가 가능하나, 안전 및 소수점 호가 에러 방지를 위해 int단위 1주 단위로 처리
            buy_qty = int(invest_cash / target_buy_price)
            
            if buy_qty > 0:
                if buy_orders:
                    existing_order = buy_orders[0]
                    # 기존 주문의 단가 및 수량과 새로운 타겟 조건 비교
                    if abs(existing_order["price"] - target_buy_price) > 0.01 or int(existing_order["qty"]) != buy_qty:
                        logger.info(f"🔄 VWAP 변동 감지! 매수 주문 정정 수행: {existing_order['price']:.2f} -> {target_buy_price:.2f} (수량: {existing_order['qty']} -> {buy_qty})")
                        # Cancel & Replace
                        if broker.cancel_order(existing_order["order_id"]):
                            # 취소 후 잔고 재동기화 (VirtualBroker의 경우 취소 즉시 cash 락이 해제됨)
                            new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                            if new_id:
                                logger.info(f"매수 정정 주문 제출 완료. (ID: {new_id})")
                    else:
                        logger.info("기존 매수 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
                else:
                    # 신규 매수 지정가 주문 제출
                    logger.info(f"📈 매수 조건 감시 진입. 지정가 매수 주문 제출: {target_buy_price:.2f} @ {buy_qty}주")
                    new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                    if new_id:
                        logger.info(f"매수 지정가 주문 제출 성공. (ID: {new_id})")
            else:
                logger.warning(f"설정된 투자 비중({k_percent}%)에 따른 예산({invest_cash:.2f})이 최소 1주 가격({target_buy_price:.2f})보다 적어 매수 주문을 보류합니다.")

        # 9-4. 대기 및 포지션 유지 (HOLD / WAIT)
        else:
            # 타겟 영역을 벗어난 쓸모없는 미체결 주문이 남아있다면 정격 취소 처리하여 계좌 잠김 방지
            if open_orders:
                logger.info("전략 조건 외의 잔여 미체결 주문을 정리합니다.")
                for order in open_orders:
                    broker.cancel_order(order["order_id"])

        # 10. 웹 대시보드용 상태 캐시 업데이트 (스레드 세이프하게 복사)
        with self._lock:
            # 보유 주식 정보 직렬화
            serializable_holdings = {}
            for t, val in holdings.items():
                serializable_holdings[t] = {
                    "qty": round(val["qty"], 4),
                    "entry_price": round(val["entry_price"], 2)
                }

            self.status_cache = {
                "is_running": self.running,
                "mode": mode,
                "ticker": ticker,
                "market": market,
                "current_price": round(current_price, 2),
                "vwap": round(vwap, 2),
                "target_buy_price": round(target_buy_price, 2),
                "target_sell_price": round(target_sell_price, 2),
                "stop_loss_price": round(stop_loss_price, 2),
                "signal": signal,
                "cash": round(cash, 2),
                "holdings": serializable_holdings,
                "open_orders": broker.get_open_orders(ticker),
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
        logger.info(f"✓ {ticker} 분석 주기 완료.")
