import time
import uuid
import logging
import requests
import pandas as pd
from abc import ABC, abstractmethod
from datetime import datetime
from core.vwap.config_manager import VwapConfigManager
from core.vwap.trade_metrics import enrich_record


def _to_float(value):
    """토스 API의 decimal 문자열(예: "185.25")을 float로 변환합니다. None/빈값/파싱 실패는 None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT",
                    client_order_id: str = None) -> str:
        """주문을 제출합니다. client_order_id: (선택) 멱등성 키 — 지원하지 않는 브로커는 무시합니다.
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
    # (ADR-0008) 캔들 페이징 상한: 200봉 x 8 = 1600봉 (1m 기준 약 26.6시간 — 한 세션(최대 25시간) 전체를 덮음)
    MAX_CANDLE_PAGES = 8
    # (ADR-0008) 토스 캔들 timestamp 형식/페이징 지원 여부를 프로세스당 1회 로그로 남겨 운영에서 가정을 확인
    _candle_probe_logged = False

    def _log_candle_timestamp_probe(self, raw_candles: list, result: dict):
        """관측 전용 — 매매 동작과 무관. 첫 캔들 응답의 원본 timestamp 문자열과 현재 시각을 1회 기록합니다.
        timestamp 에 '+09:00' 같은 오프셋이 있으면 시간대가 확정되고, 오프셋이 없으면 코드는 KST 로 간주합니다
        (그 경우 최신 봉 시각이 현재 KST 와 몇 분 이내인지로 가정이 맞는지 판단할 수 있습니다)."""
        if TossBroker._candle_probe_logged or not raw_candles:
            return
        TossBroker._candle_probe_logged = True
        try:
            samples = [str(c.get("timestamp")) for c in (raw_candles[0], raw_candles[-1])]
            parsed = pd.to_datetime(samples[0])
            tz_note = f"tz={parsed.tzinfo}" if parsed.tzinfo is not None else "tz 없음(naive) → KST 로 간주"
            logging.getLogger("vwap_bot").info(
                f"[TossBroker] 캔들 timestamp 확인(1회): 첫/끝 원본={samples} ({tz_note}), "
                f"서버 현재시각={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}, "
                f"nextBefore={'있음' if result.get('nextBefore') else '없음'}, 봉 수={len(raw_candles)}")
        except Exception:
            pass

    def __init__(self, client_id: str, client_secret: str, account_seq: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.access_token = ""
        self.token_expiry = 0.0  # Unix timestamp
        self.base_url = "https://openapi.tossinvest.com"
        # 마지막 get_candles 의 출처("toss"/"yahoo"/"mock")와 '요청 범위를 다 받았는지' (관측·진입 보류 판단용)
        self.last_candles_source = ""
        self.last_candles_complete = True
        
        # 키가 설정되어 있지 않은 경우 Mock(가상) 모드로 자동 폴백(Fallback)하기 위한 플래그
        self.is_mock_only = not (self.client_id and self.client_secret)
        self.mock_mode = self.is_mock_only
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
        if self.is_mock_only:
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
                # 이전에 일시 장애로 mock 모드에 빠졌었더라도 토큰 재발급에 성공했으므로 정상 모드로 복귀
                self.mock_mode = False
                print("[TossBroker] OAuth 토큰이 성공적으로 갱신되었습니다.")
            else:
                logger = logging.getLogger("vwap_bot")
                err_msg = f"[TossBroker] 토큰 발급 실패 (HTTP {res.status_code}): {res.text}"
                logger.error(err_msg)
                # 발급 실패 시 시세 수집을 위해 임시로 Mock 모드 플래그 가동
                self.mock_mode = True
                if not self.is_mock_only:
                    raise Exception(err_msg)
        except Exception as e:
            logger = logging.getLogger("vwap_bot")
            logger.error(f"[TossBroker] 토큰 발급 예외 발생: {e}")
            self.mock_mode = True
            if not self.is_mock_only:
                raise e

    def get_balance(self) -> dict:
        self._ensure_token()
        if self.is_mock_only:
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
                err_msg = f"[TossBroker] get_balance (buying-power) API 에러 (HTTP {power_res.status_code}): {power_res.text}"
                logger.error(err_msg)
                raise Exception(err_msg)
            
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
                err_msg = f"[TossBroker] get_balance (holdings) API 에러 (HTTP {holdings_res.status_code}): {holdings_res.text}"
                logger.error(err_msg)
                raise Exception(err_msg)
                        
            return {"cash": cash, "holdings": holdings}
        except Exception as e:
            logger = logging.getLogger("vwap_bot")
            logger.error(f"[TossBroker] get_balance 예외 발생: {e}")
            # 하위 호환을 위해 기존 형태(cash 0, holdings 빈값)는 유지하되,
            # 호출자가 '진짜 0'과 '조회 실패'를 구분할 수 있도록 error 플래그를 함께 반환합니다.
            # (봇은 이 플래그가 있으면 해당 주기를 건너뜁니다 — 보유 0주로 오인해 체결 판정이 틀어지는 것 방지)
            return {"cash": 0.0, "holdings": {}, "error": True}

    def get_candles(self, ticker: str, interval: str = "1m", limit: int = 100) -> pd.DataFrame:
        self._ensure_token()
        self.last_candles_source = "yahoo" if not self.mock_mode else "mock"
        self.last_candles_complete = True
        
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

        try:
            # (ADR-0008) 세션 시작부터의 봉을 받기 위해 200봉 초과 요청은 `before`/`nextBefore` 로 페이징을 '시도'합니다.
            # [미확인 가정] 토스 candles API 가 응답 result.nextBefore 를 주고 요청 파라미터 before 를 받는다는 것.
            #  - nextBefore 가 없으면 첫 페이지(최대 200봉)만 반환 → 예전(150봉)과 비슷한 부분 VWAP 으로 '안전하게' 동작
            #  - before 를 무시하고 같은 봉을 돌려주면 '더 과거 봉이 안 늘었음'을 감지해 중단
            # 첫 페이지 실패/예외는 기존과 같이 Yahoo 폴백. 2페이지 이후 실패/예외는 받은 만큼만 반환합니다.
            res, frames, before, remaining, pages = None, [], None, int(limit), 0
            oldest = None
            page_error = None
            while remaining > 0 and pages < self.MAX_CANDLE_PAGES:
                params = {"symbol": ticker, "interval": interval, "count": min(remaining, 200)}
                if before:
                    params["before"] = before
                try:
                    res = requests.get(f"{self.base_url}/api/v1/candles", headers=headers, params=params, timeout=5)
                except Exception as page_exc:
                    if not frames:
                        raise
                    page_error = f"예외 {page_exc}"
                    break
                if res.status_code != 200:
                    if frames:
                        page_error = f"HTTP {res.status_code}"
                    break
                try:
                    result = res.json().get("result", {}) or {}
                    raw_candles = result.get("candles", []) or []
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
                except Exception as parse_exc:
                    # 2페이지 이후 파싱 실패: 이미 받은 봉은 버리지 않고 받은 만큼만 반환 (첫 페이지면 바깥 except → Yahoo 폴백)
                    if not frames:
                        raise
                    page_error = f"파싱 오류 {parse_exc}"
                    break
                if pages == 0:
                    self._log_candle_timestamp_probe(raw_candles, result)
                pages += 1
                if not candle_list:
                    break
                page_df = pd.DataFrame(candle_list)
                page_oldest = page_df["time"].min()
                if oldest is not None and not (page_oldest < oldest):
                    break  # before 가 무시되어 더 과거 봉이 오지 않음 → 페이징 중단
                oldest = page_oldest
                frames.append(page_df)
                remaining -= len(candle_list)
                before = result.get("nextBefore")
                if not before:
                    break
            if frames:
                df = pd.concat(frames, ignore_index=True)
                # before 가 inclusive 일 수 있어 페이지 경계 봉이 중복될 수 있음 → 시각 기준 중복 제거 후 오름차순
                df = df.drop_duplicates(subset="time", keep="first").sort_values(by="time").reset_index(drop=True)
                if len(df) > limit:
                    df = df.tail(limit).reset_index(drop=True)
                self.last_candles_source = "toss"
                self.last_candles_complete = page_error is None
                if page_error:
                    print(f"[TossBroker] get_candles {pages + 1}번째 페이지 실패({page_error}) — 받은 {len(df)}봉만 반환")
                return df
            if res is not None and res.status_code == 200:
                self.last_candles_source = "toss"
                return pd.DataFrame()
            if res is not None:
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

    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT",
                    client_order_id: str = None) -> str:
        """주문 생성 (POST /api/v1/orders).

        client_order_id: 멱등성 키(clientOrderId). 미지정 시 매번 새 uuid.
          토스 OpenAPI 1.2.19 사양: 같은 값으로 재요청하면 이전 주문 결과를 그대로 재반환(10분간 유효),
          같은 값에 다른 본문이면 422 idempotency-key-conflict. 따라서 '결과를 모르는' 실패(타임아웃·5xx·
          409 request-in-progress) 뒤 같은 내용으로 재시도할 때만 같은 키를 재사용해야 중복 접수를 막을 수 있습니다.

        호출 후 self.last_place_order_outcome:
          "ok"       — 접수 성공
          "rejected" — 서버가 명시적으로 거부(4xx, request-in-progress 제외). 새 키로 재시도해야 함
          "unknown"  — 접수 여부 불명(타임아웃/네트워크 예외/5xx/409 request-in-progress). 같은 키로 재시도 권장
        self.last_client_order_id: 이번 요청에 사용한 clientOrderId
        """
        self.last_place_order_outcome = "unknown"
        self.last_client_order_id = client_order_id or str(uuid.uuid4())
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
            "clientOrderId": self.last_client_order_id  # 멱등성 보장 키
        }
        if order_type == "LIMIT":
            data["price"] = price
            
        try:
            res = requests.post(f"{self.base_url}/api/v1/orders", headers=headers, json=data, timeout=5)
            if res.status_code == 200 or res.status_code == 201:
                order_id = res.json().get("result", {}).get("orderId", "")
                self.last_place_order_outcome = "ok" if order_id else "unknown"
                return order_id
            else:
                err_code = ""
                try:
                    err_code = (res.json().get("error") or {}).get("code", "")
                except Exception:
                    pass
                if res.status_code >= 500 or err_code == "request-in-progress":
                    self.last_place_order_outcome = "unknown"
                else:
                    self.last_place_order_outcome = "rejected"
                print(f"[TossBroker] place_order 주문 실패 (HTTP {res.status_code}, {err_code}): {res.text}")
                return ""
        except Exception as e:
            self.last_place_order_outcome = "unknown"
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
        # 마지막 미체결 조회의 실패 여부. 실패 시에도 하위 호환을 위해 []를 반환하므로,
        # 봇은 이 플래그로 "진짜 미체결 0건"과 "조회 실패"를 구분합니다.
        self.last_open_orders_failed = False
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
            if res.status_code != 200:
                print(f"[TossBroker] get_open_orders API 에러 (HTTP {res.status_code}): {res.text}")
                self.last_open_orders_failed = True
                return []
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
            self.last_open_orders_failed = True
            return []

    def get_order(self, order_id: str):
        """주문 상세 조회 (GET /api/v1/orders/{orderId}).

        토스증권 공식 OpenAPI(v1.2.19)에 정의된 엔드포인트로, 체결 완료·취소·거부 등
        모든 상태의 주문을 조회할 수 있습니다. 부분 체결은 평균 체결가·총 체결 수량으로 합산됩니다.

        Returns:
            성공 시 정규화된 dict:
                {"order_id", "ticker", "side", "order_type", "status", "price", "qty",
                 "filled_qty", "avg_fill_price", "commission", "filled_at", "canceled_at"}
                - status: PENDING / PARTIAL_FILLED / PENDING_CANCEL / PENDING_REPLACE /
                          FILLED / CANCELED / REJECTED / REPLACED / ... (알 수 없는 값도 그대로 전달)
                - avg_fill_price: 미체결이면 None
            실패(Mock 모드, 네트워크/인증 오류, 404 등) 시 None — 호출자는 폴백 판정을 해야 합니다.
        """
        if not order_id:
            return None
        try:
            self._ensure_token()
        except Exception as e:
            print(f"[TossBroker] get_order 토큰 확보 실패: {e}")
            return None
        if self.mock_mode:
            return None

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Tossinvest-Account": self.account_seq,
            "Content-Type": "application/json"
        }
        try:
            res = requests.get(f"{self.base_url}/api/v1/orders/{order_id}", headers=headers, timeout=5)
            if res.status_code != 200:
                print(f"[TossBroker] get_order API 에러 (HTTP {res.status_code}): {res.text}")
                return None
            o = res.json().get("result") or {}
            ex = o.get("execution") or {}
            status = str(o.get("status") or "").upper()
            if not status:
                return None
            filled_qty = _to_float(ex.get("filledQuantity"))
            return {
                "order_id": o.get("orderId", order_id),
                "ticker": o.get("symbol"),
                "side": o.get("side"),
                "order_type": o.get("orderType"),
                "status": status,
                "price": _to_float(o.get("price")),
                "qty": _to_float(o.get("quantity")),
                "filled_qty": filled_qty if filled_qty is not None else 0.0,
                "avg_fill_price": _to_float(ex.get("averageFilledPrice")),
                "commission": _to_float(ex.get("commission")),
                "filled_at": ex.get("filledAt"),
                "canceled_at": o.get("canceledAt"),
            }
        except Exception as e:
            print(f"[TossBroker] get_order 예외 발생: {e}")
            return None

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
        # (관측성) 주문 당시 판단 사유 스냅샷 {order_id: {"reason_code", "filters", "config_snapshot"}} — 봇이 주문 직후 채움
        self.order_meta = {}
        # (관측성) 거래 기록 직후 호출되는 콜백 fn(record) — 봇이 이벤트 로그/알림용으로 설정. 예외는 무시됨
        self.on_trade = None
        
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

    def place_order(self, ticker: str, side: str, price: float, qty: float, order_type: str = "LIMIT",
                    client_order_id: str = None) -> str:
        # client_order_id 는 가상 브로커에서 사용하지 않음 (인터페이스 호환용)
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
                self.order_meta.pop(order_id, None)
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

        avg_entry_before = {}  # (관측성) 체결 직전 평단 스냅샷
        for order, f_price in filled_orders:
            o_id = order["order_id"]
            side = order["side"]
            qty = order["qty"]
            avg_entry_before[o_id] = float(self.holdings.get(ticker, {}).get("entry_price", 0.0))
            
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
            self._enrich_and_add(trade_record, intended_price=order["price"],
                                 meta=self.order_meta.pop(o_id, None), entry_price=avg_entry_before.get(o_id))

    def _enrich_and_add(self, trade_record: dict, intended_price, meta=None, entry_price=None):
        """(관측성) 레코드에 슬리피지/보유시간/사유 등을 추가한 뒤 기존과 같이 기록하고 콜백을 호출합니다.
        보강 단계의 어떤 오류도 거래 기록 자체를 막지 않습니다."""
        try:
            prior = VwapConfigManager.load_trades(self.mode)
            enrich_record(trade_record, prior, intended_price, meta)
            trade_record["fill_source"] = "simulated"
            if entry_price is not None:
                trade_record["entry_price_snapshot"] = round(float(entry_price), 4)
        except Exception as e:
            print(f"[VirtualBroker] 거래 레코드 보강 실패(무시): {e}")
        recorded = VwapConfigManager.add_trade(trade_record, self.mode)
        if recorded and self.on_trade:
            try:
                self.on_trade(dict(trade_record))
            except Exception as e:
                print(f"[VirtualBroker] on_trade 콜백 실패(무시): {e}")
        return recorded

    def force_market_stop_loss(self, ticker: str, current_price: float, meta: dict = None):
        """손절 시그널 감지 시 즉시 가상 포지션을 시장가로 전량 매도 청산합니다.
        meta: (관측성, 선택) 청산 결정 당시의 {"reason_code", "filters", "config_snapshot"}"""
        if ticker not in self.holdings:
            return

        holding = self.holdings[ticker]
        qty = holding["qty"]
        avg_price = holding["entry_price"]
        
        pnl = (current_price - avg_price) * qty
        roi = (pnl / (avg_price * qty)) * 100.0 if avg_price > 0 and qty > 0 else 0.0

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
        # 가상 시장가 청산은 결정 시점 현재가로 체결되므로 슬리피지는 0 으로 기록됨
        self._enrich_and_add(trade_record, intended_price=current_price, meta=meta, entry_price=avg_price)
        print(f"🚨 [VirtualBroker] 손절 시장가 청산 집행 완료: {ticker} {qty}주 @ {current_price:.2f} (손익: {pnl:+.2f}, 수익률: {roi:+.2f}%)")
