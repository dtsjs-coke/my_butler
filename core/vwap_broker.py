import time
import uuid
import logging
import requests
import pandas as pd
from abc import ABC, abstractmethod
from datetime import datetime
from core.vwap_config_manager import VwapConfigManager

class Broker(ABC):
    @abstractmethod
    def get_balance(self) -> dict:
        """잔고 및 보유 주식 목록을 반환합니다.
        Returns:
            {"cash": float, "holdings": {ticker: {"qty": float, "entry_price": float}}}
        """
        pass

    @abstractmethod
    def get_candles(self, ticker: str, interval: str, limit: int) -> pd.DataFrame:
        """최신 OHLCV 캔들 데이터프레임을 반환합니다.
        Returns:
            DataFrame with ['time', 'open', 'high', 'low', 'close', 'volume'] columns
        """
        pass

    @abstractmethod
    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT") -> str:
        """주문을 제출합니다.
        Returns:
            order_id (str)
        """
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """주문을 취소합니다.
        Returns:
            success (bool)
        """
        pass

    @abstractmethod
    def get_open_orders(self, ticker: str) -> list:
        """미체결 지정가 주문 목록을 반환합니다.
        Returns:
            [{"order_id": str, "ticker": str, "side": str, "price": float, "qty": float, "created_at": float}]
        """
        pass

    @abstractmethod
    def get_current_price(self, ticker: str) -> float:
        """현재가를 반환합니다."""
        pass

    @abstractmethod
    def get_current_prices(self, tickers: list) -> dict:
        """여러 종목의 현재가를 일괄 조회하여 반환합니다.
        Returns:
            {ticker: price}
        """
        pass


class TossBroker(Broker):
    def __init__(self, client_id: str, client_secret: str, account_seq: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.access_token = ""
        self.token_expiry = 0.0  # Unix timestamp
        self.base_url = "https://openapi.tossinvest.com"
        
        # 키가 설정되어 있지 않은 경우 Mock(가상) 모드로 자동 폴백(Fallback)하기 위한 플래그
        self.mock_mode = not (self.client_id and self.client_secret)
        if self.mock_mode:
            print("[TossBroker] API Key 누락으로 인해 시세 조회용 가상 Mock 모드로 작동합니다.")

    def _fetch_yahoo_candles(self, ticker: str, interval: str, limit: int = 100) -> pd.DataFrame:
        """야후 파이낸스 차트 API로부터 무료 실시간/지연 분봉 데이터를 수집합니다."""
        ticker_clean = ticker.upper().strip()
        
        # 한국 주식 포맷팅 (6자리 숫자)
        if ticker_clean.isdigit() and len(ticker_clean) == 6:
            yahoo_ticker = f"{ticker_clean}.KS"
            market = "KR"
        else:
            yahoo_ticker = ticker_clean
            market = "US"
            
        # interval에 맞는 적절한 range 탐색
        range_map = {
            "1m": "2d",
            "5m": "5d",
            "15m": "5d"
        }
        y_range = range_map.get(interval, "2d")
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?interval={interval}&range={y_range}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        
        try:
            res = requests.get(url, headers=headers, timeout=5)
            # 한국 주식인데 코스피(.KS)로 실패한 경우 코스닥(.KQ)으로 재시도
            if res.status_code != 200 and market == "KR" and yahoo_ticker.endswith(".KS"):
                yahoo_ticker = f"{ticker_clean}.KQ"
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_ticker}?interval={interval}&range={y_range}"
                res = requests.get(url, headers=headers, timeout=5)
                
            if res.status_code == 200:
                res_data = res.json()
                chart_data = res_data.get("chart", {}).get("result", [])
                if not chart_data:
                    return pd.DataFrame()
                
                result = chart_data[0]
                timestamps = result.get("timestamp", [])
                indicators = result.get("indicators", {}).get("quote", [{}])[0]
                
                opens = indicators.get("open", [])
                highs = indicators.get("high", [])
                lows = indicators.get("low", [])
                closes = indicators.get("close", [])
                volumes = indicators.get("volume", [])
                
                candle_list = []
                for i in range(len(timestamps)):
                    # None 값이 끼어있는 경우가 있으므로 필터링
                    if (i >= len(opens) or i >= len(highs) or i >= len(lows) or 
                        i >= len(closes) or i >= len(volumes) or
                        opens[i] is None or highs[i] is None or 
                        lows[i] is None or closes[i] is None or 
                        volumes[i] is None):
                        continue
                    
                    candle_list.append({
                        "time": pd.to_datetime(timestamps[i], unit='s') + pd.Timedelta(hours=9),
                        "open": float(opens[i]),
                        "high": float(highs[i]),
                        "low": float(lows[i]),
                        "close": float(closes[i]),
                        "volume": float(volumes[i])
                    })
                    
                df = pd.DataFrame(candle_list)
                if not df.empty:
                    return df.tail(limit).reset_index(drop=True)
            else:
                print(f"[TossBroker Yahoo fallback] API Error (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            print(f"[TossBroker Yahoo fallback] Exception: {e}")
            
        return pd.DataFrame()

    def _ensure_token(self):
        """액세스 토큰의 유효성을 검사하고 만료 5분 전이면 재발급받습니다."""
        if self.mock_mode:
            return
        
        now = time.time()
        if self.access_token and (self.token_expiry - now > 300):
            return  # 유효함
        
        url = f"{self.base_url}/oauth2/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        
        try:
            res = requests.post(url, headers=headers, data=data, timeout=5)
            if res.status_code == 200:
                res_data = res.json()
                self.access_token = res_data.get("access_token")
                expires_in = float(res_data.get("expires_in", 3600))
                self.token_expiry = time.time() + expires_in
                print("[TossBroker] OAuth 토큰이 성공적으로 갱신되었습니다.")
            else:
                logger = logging.getLogger("vwap_bot")
                logger.error(f"[TossBroker] 토큰 발급 실패 (HTTP {res.status_code}): {res.text}")
                # 발급 실패 시 시세 수집을 위해 임시로 Mock 모드 플래그 가동
                self.mock_mode = True
        except Exception as e:
            logger = logging.getLogger("vwap_bot")
            logger.error(f"[TossBroker] 토큰 발급 예외 발생: {e}")
            self.mock_mode = True

    def get_balance(self) -> dict:
        self._ensure_token()
        if self.mock_mode:
            # Mock 데이터 반환
            return {"cash": 10000000.0, "holdings": {}}
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Tossinvest-Account": self.account_seq,
            "Content-Type": "application/json"
        }
        
        # 현재 실거래 종목 설정을 통해 통화 결정
        try:
            config = VwapConfigManager.load_config()
            real_ticker = config.get("real_ticker", "AAPL").strip()
            is_kr = real_ticker.isdigit() and len(real_ticker) == 6
            currency = "KRW" if is_kr else "USD"
        except Exception:
            currency = "USD"
            
        try:
            # 1. 예수금 조회
            cash = 0.0
            power_res = requests.get(
                f"{self.base_url}/api/v1/buying-power",
                headers=headers,
                params={"currency": currency},
                timeout=5
            )
            if power_res.status_code == 200:
                cash = float(power_res.json().get("result", {}).get("cashBuyingPower", 0.0))
            else:
                logger = logging.getLogger("vwap_bot")
                logger.error(f"[TossBroker] get_balance (buying-power) API 에러 (HTTP {power_res.status_code}): {power_res.text}")
            
            # 2. 보유 종목 조회
            holdings = {}
            holdings_res = requests.get(f"{self.base_url}/api/v1/holdings", headers=headers, timeout=5)
            if holdings_res.status_code == 200:
                res_json = holdings_res.json()
                items = res_json.get("result", {}).get("items", [])
                for item in items:
                    ticker = item.get("symbol")
                    qty = float(item.get("quantity", 0.0))
                    entry_price = float(item.get("averagePurchasePrice", 0.0))
                    if qty > 0:
                        holdings[ticker] = {"qty": qty, "entry_price": entry_price}
            else:
                logger = logging.getLogger("vwap_bot")
                logger.error(f"[TossBroker] get_balance (holdings) API 에러 (HTTP {holdings_res.status_code}): {holdings_res.text}")
                        
            return {"cash": cash, "holdings": holdings}
        except Exception as e:
            logger = logging.getLogger("vwap_bot")
            logger.error(f"[TossBroker] get_balance 예외 발생: {e}")
            return {"cash": 0.0, "holdings": {}}

    def get_candles(self, ticker: str, interval: str = "1m", limit: int = 100) -> pd.DataFrame:
        self._ensure_token()
        
        # 1. 5분봉/15분봉이거나 mock_mode 인 경우에는 야후 파이낸스로 스위칭(토스 API는 1m/1d만 지원)
        is_unsupported_interval = interval not in ["1m", "1d"]
        if self.mock_mode or is_unsupported_interval:
            df = self._fetch_yahoo_candles(ticker, interval, limit)
            if not df.empty:
                return df
            
            # 가상 모드인데 야후 API도 실패하면 난수 폴백 처리
            if self.mock_mode:
                now = datetime.now()
                times = [now - pd.Timedelta(minutes=i) for i in range(limit)]
                times.reverse()
                
                base_price = 80000.0 if ticker.isdigit() else 180.0
                prices = []
                curr = base_price
                for i in range(limit):
                    import random
                    change = random.uniform(-0.002, 0.002)
                    curr = curr * (1 + change)
                    prices.append(curr)
                    
                data = {
                    "time": times,
                    "open": [p * 0.999 for p in prices],
                    "high": [p * 1.002 for p in prices],
                    "low": [p * 0.998 for p in prices],
                    "close": prices,
                    "volume": [float(int(1000 * (1 + i % 5))) for i in range(limit)]
                }
                return pd.DataFrame(data)

        # 2. 실거래 모드 & 1m/1d인 경우에는 토스 공식 실시간 API 호출
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        params = {
            "symbol": ticker,
            "interval": interval,
            "count": min(limit, 200) # 최대 200개 제한
        }
        
        try:
            res = requests.get(f"{self.base_url}/api/v1/candles", headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                raw_candles = res.json().get("result", {}).get("candles", [])
                
                candle_list = []
                for c in raw_candles:
                    candle_list.append({
                        "time": pd.to_datetime(c.get("timestamp")),
                        "open": float(c.get("openPrice")),
                        "high": float(c.get("highPrice")),
                        "low": float(c.get("lowPrice")),
                        "close": float(c.get("closePrice")),
                        "volume": float(c.get("volume"))
                    })
                df = pd.DataFrame(candle_list)
                # 시간 순서가 안 맞을 경우 오름차순 정렬
                if not df.empty and "time" in df.columns:
                    df = df.sort_values(by="time").reset_index(drop=True)
                return df
            else:
                print(f"[TossBroker] get_candles API 에러 (HTTP {res.status_code}): {res.text}")
                # 실거래 모드에서도 토스 API 에러 시 최종 폴백으로 야후 파이낸스 한번 더 시도
                df = self._fetch_yahoo_candles(ticker, interval, limit)
                if not df.empty:
                    return df
                return pd.DataFrame()
        except Exception as e:
            print(f"[TossBroker] get_candles 예외 발생: {e}")
            # 예외 시 야후 파이낸스 폴백
            df = self._fetch_yahoo_candles(ticker, interval, limit)
            if not df.empty:
                return df
            return pd.DataFrame()

    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT") -> str:
        self._ensure_token()
        if self.mock_mode:
            print(f"[TossBroker MOCK] {ticker} {side} {qty}주 주문 성공 (가격: {price})")
            return f"mock_order_{uuid.uuid4().hex[:8]}"
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Tossinvest-Account": self.account_seq,
            "Content-Type": "application/json"
        }
        
        # 공식 OpenAPI에서는 symbol, orderType 필드명을 사용하며
        # MARKET 주문일 경우 price가 전달되면 안 됨
        data = {
            "symbol": ticker,
            "side": side,
            "quantity": qty,
            "orderType": order_type,
            "clientOrderId": str(uuid.uuid4())  # 멱등성 보장 키
        }
        if order_type == "LIMIT":
            data["price"] = price
            
        try:
            res = requests.post(f"{self.base_url}/api/v1/orders", headers=headers, json=data, timeout=5)
            if res.status_code == 200 or res.status_code == 201:
                return res.json().get("result", {}).get("orderId", "")
            else:
                print(f"[TossBroker] place_order 주문 실패 (HTTP {res.status_code}): {res.text}")
                return ""
        except Exception as e:
            print(f"[TossBroker] place_order 예외 발생: {e}")
            return ""

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_token()
        if self.mock_mode:
            print(f"[TossBroker MOCK] 주문 취소 성공 (ID: {order_id})")
            return True
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Tossinvest-Account": self.account_seq,
            "Content-Type": "application/json"
        }
        try:
            # 공식 API는 DELETE가 아닌 POST /api/v1/orders/{orderId}/cancel 이며 빈 JSON body 필요
            res = requests.post(f"{self.base_url}/api/v1/orders/{order_id}/cancel", headers=headers, json={}, timeout=5)
            return res.status_code == 200 or res.status_code == 204
        except Exception as e:
            print(f"[TossBroker] cancel_order 예외 발생: {e}")
            return False

    def get_open_orders(self, ticker: str) -> list:
        self._ensure_token()
        if self.mock_mode:
            return []
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Tossinvest-Account": self.account_seq,
            "Content-Type": "application/json"
        }
        params = {"symbol": ticker, "status": "OPEN"}
        try:
            res = requests.get(f"{self.base_url}/api/v1/orders", headers=headers, params=params, timeout=5)
            if res.status_code == 200:
                orders = []
                res_json = res.json()
                raw_orders = res_json.get("result", {}).get("orders", [])
                for o in raw_orders:
                    orders.append({
                        "order_id": o.get("orderId"),
                        "ticker": o.get("symbol"),
                        "side": o.get("side"),
                        "price": float(o.get("price")) if o.get("price") is not None else 0.0,
                        "qty": float(o.get("quantity")) if o.get("quantity") is not None else 0.0,
                        "created_at": time.time()  # 표준화
                    })
                return orders
            return []
        except Exception as e:
            print(f"[TossBroker] get_open_orders 예외: {e}")
            return []

    def get_current_price(self, ticker: str) -> float:
        self._ensure_token()
        if self.mock_mode:
            # 야후 파이낸스 최신 1분봉의 종가를 활용
            df = self.get_candles(ticker, "1m", 1)
            return float(df.iloc[-1]['close']) if not df.empty else 0.0

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        try:
            res = requests.get(f"{self.base_url}/api/v1/prices", headers=headers, params={"symbols": ticker}, timeout=5)
            if res.status_code == 200:
                results = res.json().get("result", [])
                if results:
                    return float(results[0].get("lastPrice", 0.0))
            else:
                print(f"[TossBroker] get_current_price API 에러 (HTTP {res.status_code}): {res.text}")
            
            # API 호출 실패 시 get_candles(야후 파이낸스 포함)의 최신 종가로 폴백
            df = self.get_candles(ticker, "1m", 1)
            return float(df.iloc[-1]['close']) if not df.empty else 0.0
        except Exception as e:
            print(f"[TossBroker] get_current_price 예외 발생: {e}")
            try:
                df = self.get_candles(ticker, "1m", 1)
                return float(df.iloc[-1]['close']) if not df.empty else 0.0
            except Exception:
                return 0.0

    def get_current_prices(self, tickers: list) -> dict:
        if not tickers:
            return {}
            
        self._ensure_token()
        
        # Mock 모드일 경우 각 ticker별 get_current_price 순회
        if self.mock_mode:
            return {t: self.get_current_price(t) for t in tickers}
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        # 콤마로 연결
        symbols_str = ",".join(tickers)
        try:
            res = requests.get(f"{self.base_url}/api/v1/prices", headers=headers, params={"symbols": symbols_str}, timeout=5)
            if res.status_code == 200:
                results = res.json().get("result", [])
                prices_map = {}
                for idx, item in enumerate(results):
                    # symbol 필드로 우선 매핑
                    sym = item.get("symbol")
                    price = float(item.get("lastPrice", 0.0))
                    if sym:
                        prices_map[sym] = price
                    elif idx < len(tickers):
                        # 만약 symbol 필드가 없으면 순서대로 매핑
                        prices_map[tickers[idx]] = price
                
                # 혹시 조회 누락된 종목이 있다면 개별 폴백 처리
                for t in tickers:
                    if t not in prices_map or prices_map[t] <= 0:
                        prices_map[t] = self.get_current_price(t)
                return prices_map
            else:
                print(f"[TossBroker] get_current_prices API 에러 (HTTP {res.status_code}): {res.text}")
        except Exception as e:
            print(f"[TossBroker] get_current_prices 예외 발생: {e}")
            
        # 실패 시 개별 폴백
        return {t: self.get_current_price(t) for t in tickers}


class VirtualBroker(Broker):
    def __init__(self, initial_balance: float, ticker_source_broker: Broker, mode: str = "VIRTUAL"):
        """
        가상 거래를 위해 인메모리 장부를 구동하는 브로커 클래스입니다.
        시세(Candles, Price) 조회를 위해 실제 TossBroker(혹은 Mock 모드)를 내부에서 레퍼런스합니다.
        """
        self.source_broker = ticker_source_broker
        self.initial_balance = initial_balance
        self.mode = mode.upper()
        self.cash = initial_balance
        self.holdings = {}       # {ticker: {"qty": float, "entry_price": float}}
        self.open_orders = []    # [{"order_id": str, "ticker": str, "side": str, "price": float, "qty": float, "created_at": float}]
        
        # 이전 실행 시의 거래 기록을 복원하여 가상 평가자산을 유지할 수 있도록 함
        self._sync_balance_from_trades()

    def _sync_balance_from_trades(self):
        """로컬 vwap_trades.json 이력으로부터 가상 잔고 및 보유 종목 상태를 재구축합니다."""
        trades = VwapConfigManager.load_trades(self.mode)
        self.cash = self.initial_balance
        self.holdings = {}
        
        for trade in trades:
            ticker = trade.get("ticker")
            side = trade.get("side")
            price = trade.get("price", 0.0)
            qty = trade.get("qty", 0.0)
            
            if side == "BUY":
                self.cash -= (price * qty)
                if ticker not in self.holdings:
                    self.holdings[ticker] = {"qty": qty, "entry_price": price}
                else:
                    curr = self.holdings[ticker]
                    total_qty = curr["qty"] + qty
                    weighted_price = (curr["qty"] * curr["entry_price"] + qty * price) / total_qty
                    self.holdings[ticker] = {"qty": total_qty, "entry_price": weighted_price}
            elif side == "SELL" or side == "STOP_LOSS":
                self.cash += (price * qty)
                if ticker in self.holdings:
                    curr = self.holdings[ticker]
                    rem_qty = curr["qty"] - qty
                    if rem_qty <= 0:
                        self.holdings.pop(ticker, None)
                    else:
                        self.holdings[ticker]["qty"] = rem_qty

    def get_balance(self) -> dict:
        return {
            "cash": self.cash,
            "holdings": self.holdings
        }

    def get_candles(self, ticker: str, interval: str, limit: int) -> pd.DataFrame:
        # 시세 데이터는 실제 브로커(또는 Mock 시세 소스)로부터 투명하게 조달
        return self.source_broker.get_candles(ticker, interval, limit)

    def get_current_price(self, ticker: str) -> float:
        return self.source_broker.get_current_price(ticker)

    def get_current_prices(self, tickers: list) -> dict:
        return self.source_broker.get_current_prices(tickers)

    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT") -> str:
        order_id = f"v_order_{uuid.uuid4().hex[:8]}"
        
        # 매수 주문의 경우, 잔고 초과 거래 방지(Hold Cash)
        if side == "BUY":
            req_cash = price * qty
            if self.cash < req_cash:
                print(f"[VirtualBroker] 잔고 부족으로 매수 가상 주문 반려 (잔고: {self.cash:.2f}, 필요: {req_cash:.2f})")
                return ""
            # 가상 캐시 즉시 락(차감)
            self.cash -= req_cash

        order = {
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "price": price,
            "qty": qty,
            "created_at": time.time()
        }
        self.open_orders.append(order)
        print(f"[VirtualBroker] 가상 주문 접수 완료: {side} {ticker} {qty}주 (지정가: {price})")
        return order_id

    def cancel_order(self, order_id: str) -> bool:
        for idx, o in enumerate(self.open_orders):
            if o["order_id"] == order_id:
                # 매수 주문 취소의 경우, 락된 캐시 복원
                if o["side"] == "BUY":
                    self.cash += (o["price"] * o["qty"])
                self.open_orders.pop(idx)
                print(f"[VirtualBroker] 가상 주문 취소 완료 (ID: {order_id})")
                return True
        return False

    def get_open_orders(self, ticker: str) -> list:
        return [o for o in self.open_orders if o["ticker"] == ticker]

    def update_simulation(self, ticker: str, current_price: float, high: float, low: float):
        """
        매 주기마다 현재 실시간 고가/저가/종가를 기준으로 가상 주문의 체결 여부를 판단하는 매칭 엔진입니다.
        체결 성공 시 거래 기록(vwap_trades.json)을 영구 보존하고, 포지션 상태를 갱신합니다.
        """
        filled_orders = []
        for o in list(self.open_orders):
            if o["ticker"] != ticker:
                continue

            order_price = o["price"]
            order_qty = o["qty"]
            side = o["side"]
            
            is_filled = False
            fill_price = order_price

            if side == "BUY":
                # 매수 지정가 주문: 저가(low)가 지정가 이하인 경우 체결
                if low <= order_price:
                    is_filled = True
                    # 체결가는 주문 단가로 체정
                    fill_price = order_price
            elif side == "SELL":
                # 매도 지정가 주문: 고가(high)가 지정가 이상인 경우 체결
                if high >= order_price:
                    is_filled = True
                    fill_price = order_price

            if is_filled:
                filled_orders.append((o, fill_price))
                self.open_orders.remove(o)

        for order, f_price in filled_orders:
            o_id = order["order_id"]
            side = order["side"]
            qty = order["qty"]
            
            pnl = 0.0
            roi = 0.0
            
            if side == "BUY":
                # 보유종목 갱신
                if ticker not in self.holdings:
                    self.holdings[ticker] = {"qty": qty, "entry_price": f_price}
                else:
                    curr = self.holdings[ticker]
                    total_qty = curr["qty"] + qty
                    weighted_price = (curr["qty"] * curr["entry_price"] + qty * f_price) / total_qty
                    self.holdings[ticker] = {"qty": total_qty, "entry_price": weighted_price}
                
                print(f"🎉 [VirtualBroker] 매수 체결 성공: {ticker} {qty}주 @ {f_price:.2f}")
                
            elif side == "SELL":
                # 매도 정산
                if ticker in self.holdings:
                    avg_price = self.holdings[ticker]["entry_price"]
                    pnl = (f_price - avg_price) * qty
                    roi = (pnl / (avg_price * qty)) * 100.0 if avg_price > 0 else 0.0
                    
                    self.cash += (f_price * qty)
                    
                    rem_qty = self.holdings[ticker]["qty"] - qty
                    if rem_qty <= 0:
                        self.holdings.pop(ticker, None)
                    else:
                        self.holdings[ticker]["qty"] = rem_qty
                else:
                    # 무차입 공매도 불가 원칙이나 예외 복구용
                    self.cash += (f_price * qty)
                
                print(f"🎉 [VirtualBroker] 매도 체결 성공: {ticker} {qty}주 @ {f_price:.2f} (손익: {pnl:+.2f}, 수익률: {roi:+.2f}%)")

            # 체결 이력 JSON 파일에 영구 추가
            trade_record = {
                "trade_id": o_id,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ticker": ticker,
                "side": side,
                "price": f_price,
                "qty": qty,
                "pnl": round(pnl, 2),
                "roi": round(roi, 2)
            }
            VwapConfigManager.add_trade(trade_record, self.mode)

    def force_market_stop_loss(self, ticker: str, current_price: float):
        """손절 시그널 감지 시 즉시 가상 포지션을 시장가로 전량 매도 청산합니다."""
        if ticker not in self.holdings:
            return

        holding = self.holdings[ticker]
        qty = holding["qty"]
        avg_price = holding["entry_price"]
        
        pnl = (current_price - avg_price) * qty
        roi = (pnl / (avg_price * qty)) * 100.0
        
        # 미체결 가상 주문 전체 취소
        for o in list(self.open_orders):
            if o["ticker"] == ticker:
                self.cancel_order(o["order_id"])

        # 현금 정산
        self.cash += (current_price * qty)
        self.holdings.pop(ticker, None)
        
        # 이력 기록
        trade_record = {
            "trade_id": f"sl_order_{uuid.uuid4().hex[:8]}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": ticker,
            "side": "STOP_LOSS",
            "price": current_price,
            "qty": qty,
            "pnl": round(pnl, 2),
            "roi": round(roi, 2)
        }
        VwapConfigManager.add_trade(trade_record, self.mode)
        print(f"🚨 [VirtualBroker] 손절 시장가 청산 집행 완료: {ticker} {qty}주 @ {current_price:.2f} (손익: {pnl:+.2f}, 수익률: {roi:+.2f}%)")
