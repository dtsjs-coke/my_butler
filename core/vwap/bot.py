import os
import time
import logging
import threading
import traceback
from datetime import datetime
from core.vwap.config_manager import VwapConfigManager
from core.vwap.broker import TossBroker, VirtualBroker
from core.vwap.strategy import VwapStrategy

# 프로젝트 루트 경로
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(PROJECT_ROOT, "trading_bot_virtual.log")

def setup_logger(mode="VIRTUAL"):
    """트레이딩 봇 전용 파일 및 콘솔 로거를 설정합니다."""
    logger_name = f"vwap_bot_{mode.lower()}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    
    # 중복 추가 방지
    if logger.handlers:
        return logger

    log_path = os.path.join(PROJECT_ROOT, f"trading_bot_{mode.lower()}.log")

    # 파일 핸들러 (UTF-8 인코딩)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
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


class VWAPBot:
    def __init__(self, mode="VIRTUAL"):
        self.mode = mode.upper()  # "VIRTUAL" 또는 "REAL"
        self.running = False
        self.thread = None
        self._lock = threading.Lock()
        self.daily_baseline_asset = 0.0
        self.last_baseline_date = ""
        self.logger = setup_logger(self.mode)
        
        # 봇 상태 캐시 (웹 대시보드 API 조회용)
        self.status_cache = {
            "is_running": False,
            "mode": self.mode,
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
            "last_updated": "",
            "adx": 0.0,
            "rsi": 50.0,
            "vwap_stdev": 0.0
        }
        
        # 브로커 캐시
        self.real_broker = None
        self.virtual_broker = None
        
        # 실거래 주문 체결 감지를 위한 미체결 주문 목록 추적
        self.tracked_open_orders = {}  # {order_id: {"ticker": ticker, "side": side, "price": price, "qty": qty}}

    def start(self):
        """트레이딩 봇 백그라운드 스레드를 시작합니다."""
        with self._lock:
            if self.running:
                self.logger.warning(f"[{self.mode} 봇] 이미 가동 중입니다.")
                return False
            
            self.running = True
            self.daily_baseline_asset = 0.0
            self.last_baseline_date = ""
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            self.logger.info(f"⚡ [{self.mode} 봇] 백그라운드 엔진이 시작되었습니다.")
            return True

    def stop(self):
        """트레이딩 봇 백그라운드 스레드를 종료합니다."""
        with self._lock:
            if not self.running:
                self.logger.warning(f"[{self.mode} 봇] 작동 중이 아닙니다.")
                return False
            
            self.running = False
            self.logger.info(f"🛑 [{self.mode} 봇] 백그라운드 엔진 정지 요청이 접수되었습니다.")
            return True

    def get_status(self) -> dict:
        """현재 봇 상태 캐시를 반환합니다."""
        with self._lock:
            self.status_cache["is_running"] = self.running
            if not self.running:
                try:
                    config = VwapConfigManager.load_config()
                    mode = self.mode
                    if mode == "VIRTUAL":
                        mode = "VIRTUAL_1"
                    mode_prefix = mode.lower()
                    
                    ticker = config.get(f"{mode_prefix}_ticker", "AAPL")
                    market = config.get(f"{mode_prefix}_market", "US")
                    interval = config.get(f"{mode_prefix}_interval", "1m")
                    initial_balance = float(config.get(f"{mode_prefix}_initial_balance", 10000000.0))
                    
                    self.status_cache["ticker"] = ticker
                    self.status_cache["market"] = market
                    self.status_cache["interval"] = interval
                    
                    if self.mode.startswith("VIRTUAL"):
                        trades = VwapConfigManager.load_trades(self.mode)
                        cash = initial_balance
                        holdings = {}
                        for trade in trades:
                            t_ticker = trade.get("ticker")
                            t_side = trade.get("side")
                            t_price = trade.get("price", 0.0)
                            t_qty = trade.get("qty", 0.0)
                            
                            if t_side == "BUY":
                                cash -= (t_price * t_qty)
                                if t_ticker not in holdings:
                                    holdings[t_ticker] = {"qty": t_qty, "entry_price": t_price}
                                else:
                                    curr = holdings[t_ticker]
                                    total_qty = curr["qty"] + t_qty
                                    weighted_price = (curr["qty"] * curr["entry_price"] + t_qty * t_price) / total_qty
                                    holdings[t_ticker] = {"qty": total_qty, "entry_price": weighted_price}
                            elif t_side in ["SELL", "STOP_LOSS"]:
                                cash += (t_price * t_qty)
                                if t_ticker in holdings:
                                    curr = holdings[t_ticker]
                                    rem_qty = curr["qty"] - t_qty
                                    if rem_qty <= 0:
                                        holdings.pop(t_ticker, None)
                                    else:
                                        holdings[t_ticker]["qty"] = rem_qty
                                        
                        self.status_cache["cash"] = round(cash, 2)
                        self.status_cache["holdings"] = holdings
                        
                        if ticker in holdings:
                            if self.status_cache.get("current_price", 0.0) == 0.0:
                                self.status_cache["current_price"] = holdings[ticker]["entry_price"]
                        else:
                            self.status_cache["current_price"] = 0.0
                    else:
                        self.status_cache["cash"] = initial_balance
                        self.status_cache["holdings"] = {}
                        self.status_cache["current_price"] = 0.0
                except Exception as e:
                    pass
            return self.status_cache

    def _run_loop(self):
        """백그라운드 스레드에서 무한 루프로 실행되는 메인 봇 주기 실행부입니다."""
        self.logger.info(f"[{self.mode} 봇] 루프 스레드가 기동되었습니다.")
        
        while self.running:
            try:
                self._loop_step()
            except Exception as e:
                self.logger.error(f"❌ [{self.mode} 봇] 루프 실행 중 에러 발생: {e}")
                self.logger.error(traceback.format_exc())
            
            # 1분 단위로 주기적 실행
            for _ in range(60):
                if not self.running:
                    break
                time.sleep(1)
                
        self.logger.info(f"[{self.mode} 봇] 루프 스레드가 완전히 종료되었습니다.")

    def _loop_step(self):
        """한 주기의 전략 계산 및 주문 정정 작업을 수행합니다."""
        # 1. 설정 실시간 로드
        config = VwapConfigManager.load_config()
        
        mode = self.mode
        if mode == "VIRTUAL":
            mode = "VIRTUAL_1"
        mode_prefix = mode.lower()
        
        ticker = config[f"{mode_prefix}_ticker"]
        market = config[f"{mode_prefix}_market"]
        interval = config[f"{mode_prefix}_interval"]
        n_percent = float(config[f"{mode_prefix}_n_percent"])
        m_percent = float(config[f"{mode_prefix}_m_percent"])
        x_percent = float(config[f"{mode_prefix}_x_percent"])
        k_percent = float(config[f"{mode_prefix}_k_percent"])
        reset_time = config[f"{mode_prefix}_reset_time"]
        start_time = config.get(f"{mode_prefix}_start_time", "")
        initial_balance = float(config[f"{mode_prefix}_initial_balance"])
        max_daily_loss_limit = float(config.get(f"{mode_prefix}_max_daily_loss_limit", 5.0))
        
        # 보조 지표 파라미터 로드
        use_adx_filter = bool(config.get(f"{mode_prefix}_use_adx_filter", False))
        adx_threshold = float(config.get(f"{mode_prefix}_adx_threshold", 25.0))
        use_rsi_filter = bool(config.get(f"{mode_prefix}_use_rsi_filter", False))
        rsi_threshold = float(config.get(f"{mode_prefix}_rsi_threshold", 30.0))
        use_vwap_band = bool(config.get(f"{mode_prefix}_use_vwap_band", False))
        vwap_band_sigma = float(config.get(f"{mode_prefix}_vwap_band_sigma", 2.0))

        # 2. 브로커 초기화 및 스위칭
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
                ticker_source_broker=self.real_broker,
                mode=mode
            )

        # 현재 실행 모드에 맞는 브로커 선택
        broker = self.real_broker if mode == "REAL" else self.virtual_broker

        self.logger.info(f"▶ [{mode} 모드] {ticker} ({market}) 전략 분석 주기 시작...")

        # 3. 최신 캔들 수집
        df = broker.get_candles(ticker, interval, 150)
        if df.empty:
            self.logger.error(f"[{ticker}] 캔들 데이터를 가져오지 못했습니다. 다음 주기에 재시도합니다.")
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
        if balance is None or "cash" not in balance or "holdings" not in balance:
            self.logger.error(f"⚠️ [{ticker}] 자산 정보를 조회하지 못했습니다. 일시적인 API 에러일 수 있으므로 다음 주기에 재시도합니다.")
            return
            
        cash = balance["cash"]
        holdings = balance["holdings"]
        
        # 포지션 정보 파싱
        holding_info = holdings.get(ticker, {"qty": 0.0, "entry_price": 0.0})
        qty = holding_info["qty"]
        entry_price = holding_info["entry_price"]

        # 미체결 매수 주문에 묶인 거래 대기 금액 계산 (가상/실제 공통 적용)
        open_orders = broker.get_open_orders(ticker)
        pending_buy_value = sum(float(o["price"]) * float(o["qty"]) for o in open_orders if o["side"] == "BUY")

        # 당일 손실 한도(Panic Stop) 안전장치 검사
        stock_value = qty * current_price
        total_asset = cash + stock_value + pending_buy_value
        now_date = datetime.now().strftime("%Y-%m-%d")

        # 만약 실제 총자산이 0원 이하인 경우(통신 장애 또는 환전 전 등), 
        # 당일 기준 자산 설정 및 손실 감지(Panic Stop) 로직을 안전하게 건너뜁니다.
        if total_asset <= 0.0:
            self.logger.warning(f"⚠️ [{ticker}] 실제 자산 조회 결과가 0원 이하입니다. 일시적인 API 장애 또는 환전 대기 상태일 수 있으므로 손실 한도 검사를 건너뜁니다.")
        else:
            # baseline 자산이 미설정되었거나 날짜가 바뀌었을 때 갱신
            if self.daily_baseline_asset <= 0.0 or self.last_baseline_date != now_date:
                self.daily_baseline_asset = total_asset
                self.last_baseline_date = now_date
                self.logger.info(f"🎯 당일 기준 자산(Baseline)이 설정되었습니다: {self.daily_baseline_asset:.2f} ({now_date})")

            # 손실 감지 시 강제 청산
            if self.daily_baseline_asset > 0.0:
                loss_amount = self.daily_baseline_asset - total_asset
                loss_rate = (loss_amount / self.daily_baseline_asset) * 100.0
                
                if loss_rate >= max_daily_loss_limit:
                    self.logger.error(f"🚨🚨 [당일 손실 한도 초과] 당일 기준 자산({self.daily_baseline_asset:.2f}) 대비 손실률 {loss_rate:.2f}% 발생! (한도: {max_daily_loss_limit:.2f}%)")
                    self.logger.error(f"🚨 즉각 모든 미체결 주문 취소 및 보유 주식 전량 시장가 매도(Panic Sell & Stop)를 감행하고 봇을 강제 정지합니다.")
                    
                    # 미체결 주문 전체 취소
                    open_orders = broker.get_open_orders(ticker)
                    for order in open_orders:
                        if broker.cancel_order(order["order_id"]):
                            self.tracked_open_orders.pop(order["order_id"], None)
                        
                    # 보유 주식 시장가 전량 청산
                    if qty > 0:
                        if mode == "VIRTUAL":
                            self.virtual_broker.force_market_stop_loss(ticker, current_price)
                        else:
                            panic_order_id = broker.place_order(ticker, "SELL", 0.0, qty, "MARKET")
                            if panic_order_id:
                                panic_pnl = (current_price - entry_price) * qty
                                panic_roi = (panic_pnl / (entry_price * qty)) * 100.0 if entry_price > 0 else 0.0
                                VwapConfigManager.add_trade({
                                    "trade_id": panic_order_id,
                                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "ticker": ticker,
                                    "side": "STOP_LOSS",
                                    "price": current_price,
                                    "qty": qty,
                                    "pnl": round(panic_pnl, 2),
                                    "roi": round(panic_roi, 2)
                                }, "REAL")
                                self.logger.info(f"🚨 패닉스탑 시장가 청산 체결 기록 완료: {ticker} {qty}주 @ {current_price:.2f} (손익: {panic_pnl:+.2f})")
                            else:
                                self.logger.error(f"❌🚨 패닉스탑 시장가 청산 주문 제출에 실패했습니다! {ticker} {qty}주 보유 포지션이 그대로 남아있으니 즉시 수동으로 확인하세요.")

                    self.running = False
                    self.logger.error("🛑 당일 손실 한도 초과로 인해 봇 백그라운드 엔진이 정지(STOP)되었습니다.")
                    return

        # 7. 전략 시그널 도출
        signals = VwapStrategy.get_signals(
            df, n_percent, m_percent, x_percent, qty, entry_price,
            use_adx_filter=use_adx_filter,
            adx_threshold=adx_threshold,
            use_rsi_filter=use_rsi_filter,
            rsi_threshold=rsi_threshold,
            use_vwap_band=use_vwap_band,
            vwap_band_sigma=vwap_band_sigma
        )
        signal = signals["signal"]
        vwap = signals["vwap"]
        target_buy_price = signals["target_buy_price"]
        target_sell_price = signals["target_sell_price"]
        stop_loss_price = signals["stop_loss_price"]

        # 7-1. 거래 시작 시간(Start Time) 체크를 통한 대기 로직 적용
        is_waiting_for_start = False
        if start_time and start_time != reset_time:
            try:
                from datetime import timedelta
                # HH:MM 형식 검증 및 파싱
                reset_h, reset_m = map(int, reset_time.split(':'))
                start_h, start_m = map(int, start_time.split(':'))
                
                dt_now = datetime.now()
                # 오늘 기준 리셋 시각
                dt_reset_today = dt_now.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
                
                # 최근 리셋 시각 (T_reset)
                if dt_now >= dt_reset_today:
                    t_reset = dt_reset_today
                else:
                    t_reset = dt_reset_today - timedelta(days=1)
                
                # 최근 리셋 시각에 대응하는 거래 시작 시각 (T_start)
                t_start_temp = t_reset.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                if t_start_temp >= t_reset:
                    t_start = t_start_temp
                else:
                    t_start = t_start_temp + timedelta(days=1)
                
                # 현재 시각이 대기 시간 범위 [T_reset, T_start)에 있는 경우 대기 활성화
                if t_reset <= dt_now < t_start:
                    is_waiting_for_start = True
                    self.logger.info(
                        f"⏳ [거래 시작 대기] 현재 시각({dt_now.strftime('%H:%M:%S')})이 거래 시작 설정 시각({start_time}) 이전입니다. "
                        f"(최근 리셋: {t_reset.strftime('%m-%d %H:%M')}, 시작 예정: {t_start.strftime('%m-%d %H:%M')})"
                    )
            except Exception as ex:
                self.logger.error(f"❌ 거래 시작 시간 검증 중 에러 발생 (설정값: {start_time}): {ex}")

        if is_waiting_for_start:
            signal = "WAIT"
            self.logger.info(f"⏳ 대기 시간대이므로 전략 시그널을 WAIT로 강제하고 신규 매매를 보류합니다.")

        self.logger.info(f"현재가: {current_price:.2f} | VWAP: {vwap:.2f} | 시그널: {signal}")
        if qty > 0:
            self.logger.info(f"보유량: {qty}주 | 평단가: {entry_price:.2f} | 청산타겟: {target_sell_price:.2f} | 손절가: {stop_loss_price:.2f}")
        else:
            self.logger.info(f"진입타겟: {target_buy_price:.2f}")

        # 8. 미체결 지정가 주문 조회 및 실거래 체결 이력 트래킹
        open_orders = broker.get_open_orders(ticker)

        if mode == "REAL":
            # 8-1. 현재 오픈 주문 목록을 받아와서 누락된(체결된) 주문 감지
            current_open_ids = {o["order_id"] for o in open_orders}
            
            for prev_id, oinfo in list(self.tracked_open_orders.items()):
                # 추적 중이던 주문이 미체결 목록에서 사라진 경우 -> 실제 체결로 판정
                if prev_id not in current_open_ids:
                    # P&L 계산 (매도 주문 체결 시 매수 평단가 대비 수익 실현 계산)
                    pnl = 0.0
                    roi = 0.0
                    if oinfo["side"] == "SELL" and entry_price > 0:
                        pnl = (oinfo["price"] - entry_price) * oinfo["qty"]
                        roi = (pnl / (entry_price * oinfo["qty"])) * 100.0
                    
                    trade_record = {
                        "trade_id": prev_id,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": oinfo["ticker"],
                        "side": oinfo["side"],
                        "price": oinfo["price"],
                        "qty": oinfo["qty"],
                        "pnl": round(pnl, 2),
                        "roi": round(roi, 2)
                    }
                    VwapConfigManager.add_trade(trade_record, "REAL")
                    self.logger.info(f"🎉 [실거래 체결 감지] {oinfo['side']} 체결 완료: {oinfo['ticker']} {oinfo['qty']}주 @ {oinfo['price']:.2f} (손익: {pnl:+.2f})")
                    self.tracked_open_orders.pop(prev_id, None)

            # 8-2. 현재 오픈 주문 중 추적 리스트에 없는 항목 신규 등록
            for o in open_orders:
                if o["order_id"] not in self.tracked_open_orders:
                    self.tracked_open_orders[o["order_id"]] = {
                        "ticker": o["ticker"],
                        "side": o["side"],
                        "price": o["price"],
                        "qty": o["qty"]
                    }

        # 9. 주문 집행 및 Cancel & Replace 정정 메커니즘
        
        # 9-1. 손절 조건 판정 (STOP_LOSS)
        if signal == "STOP_LOSS":
            self.logger.warning(f"🚨 손절 기준선({stop_loss_price:.2f}) 하향 이탈! 즉시 시장가 청산을 시도합니다.")
            if mode == "VIRTUAL":
                self.virtual_broker.force_market_stop_loss(ticker, current_price)
            else:
                # 미체결 주문 전체 취소
                for order in open_orders:
                    if broker.cancel_order(order["order_id"]):
                        self.tracked_open_orders.pop(order["order_id"], None)
                # 즉시 시장가 전량 매도 주문 제출
                order_id = broker.place_order(ticker, "SELL", 0.0, qty, "MARKET")
                if order_id:
                    self.logger.info(f"정상 시장가 손절 주문 제출 성공. (ID: {order_id})")
                    sl_pnl = (current_price - entry_price) * qty
                    sl_roi = (sl_pnl / (entry_price * qty)) * 100.0 if entry_price > 0 else 0.0
                    VwapConfigManager.add_trade({
                        "trade_id": order_id,
                        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": ticker,
                        "side": "STOP_LOSS",
                        "price": current_price,
                        "qty": qty,
                        "pnl": round(sl_pnl, 2),
                        "roi": round(sl_roi, 2)
                    }, "REAL")
                else:
                    self.logger.error(f"❌🚨 시장가 손절 주문 제출에 실패했습니다! {ticker} {qty}주 보유 포지션이 그대로 남아있으니 즉시 수동으로 확인하세요.")
            return

        # 9-2. 매도 청산 시그널 (SELL)
        elif signal == "SELL":
            sell_orders = [o for o in open_orders if o["side"] == "SELL"]
            
            if sell_orders:
                existing_order = sell_orders[0]
                if abs(existing_order["price"] - target_sell_price) > 0.01:
                    self.logger.info(f"🔄 VWAP 변동 감지! 매도 주문 정정 수행: {existing_order['price']:.2f} -> {target_sell_price:.2f}")
                    if broker.cancel_order(existing_order["order_id"]):
                        self.tracked_open_orders.pop(existing_order["order_id"], None)
                        new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                        if new_id:
                            self.logger.info(f"매도 정정 주문 제출 완료. (ID: {new_id})")
                            if mode == "REAL":
                                self.tracked_open_orders[new_id] = {"ticker": ticker, "side": "SELL", "price": target_sell_price, "qty": qty}
                else:
                    self.logger.info("기존 매도 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
            else:
                self.logger.info(f"📉 청산 조건 충족. 지정가 매도 주문 제출: {target_sell_price:.2f} @ {qty}주")
                new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                if new_id:
                    self.logger.info(f"매도 지정가 주문 제출 성공. (ID: {new_id})")
                    if mode == "REAL":
                        self.tracked_open_orders[new_id] = {"ticker": ticker, "side": "SELL", "price": target_sell_price, "qty": qty}

        # 9-3. 매수 진입 시그널 (BUY)
        elif signal == "BUY":
            buy_orders = [o for o in open_orders if o["side"] == "BUY"]
            # 사용자가 설정한 기준 자본금(initial_balance)이 존재하고 0보다 크면 이를 기준으로 예산을 할당하고, 없으면 실제 현금을 기준으로 함
            base_balance = initial_balance if initial_balance > 0.0 else cash
            invest_cash = base_balance * (k_percent / 100.0)
            
            if invest_cash > cash:
                self.logger.warning(f"🛡️ [안전장치] 가용 예산({invest_cash:.2f})이 보유 현금({cash:.2f})을 초과하여 보유 잔고로 제한합니다 (미수 방지).")
                invest_cash = cash
            
            buy_qty = int(invest_cash / target_buy_price)
            
            if buy_qty > 0:
                if buy_orders:
                    existing_order = buy_orders[0]
                    if abs(existing_order["price"] - target_buy_price) > 0.01 or int(existing_order["qty"]) != buy_qty:
                        self.logger.info(f"🔄 VWAP 변동 감지! 매수 주문 정정 수행: {existing_order['price']:.2f} -> {target_buy_price:.2f} (수량: {existing_order['qty']} -> {buy_qty})")
                        if broker.cancel_order(existing_order["order_id"]):
                            self.tracked_open_orders.pop(existing_order["order_id"], None)
                            new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                            if new_id:
                                self.logger.info(f"매수 정정 주문 제출 완료. (ID: {new_id})")
                                if mode == "REAL":
                                    self.tracked_open_orders[new_id] = {"ticker": ticker, "side": "BUY", "price": target_buy_price, "qty": buy_qty}
                    else:
                        self.logger.info("기존 매수 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
                else:
                    self.logger.info(f"📈 매수 조건 감시 진입. 지정가 매수 주문 제출: {target_buy_price:.2f} @ {buy_qty}주")
                    new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                    if new_id:
                        self.logger.info(f"매수 지정가 주문 제출 성공. (ID: {new_id})")
                        if mode == "REAL":
                            self.tracked_open_orders[new_id] = {"ticker": ticker, "side": "BUY", "price": target_buy_price, "qty": buy_qty}
            else:
                self.logger.warning(f"설정된 투자 비중({k_percent}%)에 따른 예산({invest_cash:.2f})이 최소 1주 가격({target_buy_price:.2f})보다 적어 매수 주문을 보류합니다.")

        # 9-4. 대기 및 포지션 유지 (HOLD / WAIT)
        else:
            if open_orders:
                self.logger.info("전략 조건 외의 잔여 미체결 주문을 정리합니다.")
                for order in open_orders:
                    if broker.cancel_order(order["order_id"]):
                        self.tracked_open_orders.pop(order["order_id"], None)

        # 10. 웹 대시보드용 상태 캐시 업데이트 (스레드 세이프하게 복사)
        with self._lock:
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
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "adx": signals.get("adx", 0.0),
                "rsi": signals.get("rsi", 50.0),
                "vwap_stdev": signals.get("vwap_stdev", 0.0)
            }
            
        self.logger.info(f"✓ {ticker} 분석 주기 완료.")
