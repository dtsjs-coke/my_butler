"""
VWAP 봇 신뢰성 수정(1단계) 검증 스크립트 — 네트워크 없이 실행됩니다.

검증 항목
  1. 가상 봇(VIRTUAL_1)이 실제로 BUY 체결 -> SELL 체결, STOP_LOSS 기록까지 가는지
     (그리고 가상 손절이 실거래 파일 vwap_trades_real.json 에 새지 않는지)
  2. 실거래 체결 판정: 모의 응답(FILLED / CANCELED / 부분체결 후 CANCELED / PARTIAL_FILLED /
     조회 실패 -> 보유수량 diff 폴백 / 판정 불가)을 주입해 기록 결과 확인
  3. 시장가 청산(손절/패닉)의 체결가 기록 (order_api / assumed / 진행중 -> 추적)
  4. 추적 주문 영속화(재시작 후 복원)
  5. 세션 날짜 계산 경계 케이스 + 기존 start_time 로직과의 동치성
  6. 실거래 일 손실한도가 '봇 자본금' 기준으로 발동하는지 (계좌 현금이 커도)
  7. 미체결 조회 실패 시 해당 주기의 판정/주문을 보류하는지

안전장치
  - data 디렉터리를 임시 폴더로 바꿔치기(DATA_DIR/CONFIG_PATH 패치)하고, 설정 로드도 테스트용 dict로 대체합니다.
  - 봇 로거를 미리 등록해 운영 로그 파일(trading_bot_*.log)에 쓰지 않습니다.
  - 마지막에 실제 data/*.json 파일들의 해시가 테스트 전후로 같은지 확인합니다.

실행:  python scripts/test_vwap_reliability.py
"""
import os
import sys
import json
import shutil
import hashlib
import logging
import tempfile
import traceback
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pandas as pd

# ---------------------------------------------------------------------------
# 0. 운영 데이터 보호: 실제 data 폴더 해시 스냅샷 + 임시 DATA_DIR 패치
# ---------------------------------------------------------------------------
REAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _hash_dir(path):
    result = {}
    if not os.path.isdir(path):
        return result
    for name in sorted(os.listdir(path)):
        fp = os.path.join(path, name)
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                result[name] = hashlib.sha256(f.read()).hexdigest()
    return result


REAL_DATA_HASH_BEFORE = _hash_dir(REAL_DATA_DIR)

import core.vwap.config_manager as cm_module  # noqa: E402

TMP_DIR = tempfile.mkdtemp(prefix="vwap_reliability_test_")
cm_module.DATA_DIR = TMP_DIR
cm_module.CONFIG_PATH = os.path.join(TMP_DIR, "vwap_config.json")
cm_module.TRADES_PATH = os.path.join(TMP_DIR, "vwap_trades.json")

# 운영 로그 파일에 쓰지 않도록 봇 로거를 먼저 등록 (setup_logger는 핸들러가 있으면 파일 핸들러를 추가하지 않음)
for _name in ["vwap_bot_virtual_1", "vwap_bot_real"]:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("      [bot-log] %(levelname)s %(message)s"))
    _lg.addHandler(_h)
    _lg.propagate = False

import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.bot import VWAPBot, get_session_start, get_session_date  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap import events as _vwap_events  # noqa: E402

# (2단계 관측성 추가 이후) 테스트 중 실제 Discord(Butler /send)로 알림이 나가지 않도록 전송 함수를 no-op 으로 교체
_vwap_events.set_sender(lambda message: True)

# 테스트에서는 대기 없이 (재시도 '횟수' 동작만 검증; 실제 대기 시간 상한은 별도 계산으로 보고)
bot_module.MARKET_FILL_POLL_INTERVAL_SEC = 0
bot_module.MARKET_EXIT_RETRY_INTERVAL_SEC = 0
bot_module.CANCEL_CONFIRM_POLL_INTERVAL_SEC = 0

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def reset_data_dir():
    for name in os.listdir(TMP_DIR):
        os.remove(os.path.join(TMP_DIR, name))


def trades(mode):
    return VwapConfigManager.load_trades(mode)


# ---------------------------------------------------------------------------
# 테스트용 가짜 브로커 / 설정
# ---------------------------------------------------------------------------
BASE_TIME = datetime(2026, 10, 5, 10, 0)  # 리셋(22:30)·자정과 겹치지 않는 고정 시각대


def make_candles(closes, last_high=None, last_low=None):
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "time": BASE_TIME + timedelta(minutes=i),
            "open": c, "high": c, "low": c, "close": c, "volume": 1000.0,
        })
    if last_high is not None:
        rows[-1]["high"] = last_high
    if last_low is not None:
        rows[-1]["low"] = last_low
    return pd.DataFrame(rows)


class FakeTossBroker:
    """TossBroker 대체. 시세/잔고/미체결/주문상세를 시나리오대로 반환하고 호출을 기록합니다."""

    def __init__(self, client_id="", client_secret="", account_seq=""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.account_seq = account_seq
        self.mock_mode = False
        self.is_mock_only = False
        self.candles = make_candles([100.0] * 30)
        self.balance = {"cash": 0.0, "holdings": {}}
        self.open_orders = []
        self.open_orders_fail = False
        self.last_open_orders_failed = False
        self.order_details = {}      # order_id -> detail dict 또는 None
        self.placed = []
        self.canceled = []
        self.cancel_result = True
        self._seq = 0
        self.market_fail_times = 0     # 시장가 주문을 이 횟수만큼 거부(빈 ID 반환) — 409 opposite-pending 재현
        self.market_attempts = 0
        self.market_outcomes = []      # 실패 시 last_place_order_outcome 시퀀스 ("rejected"/"unknown"), 비면 "rejected"
        self.client_order_ids = []     # 시장가 주문에 사용된 clientOrderId 기록
        self.balance_seq = []          # 지정 시 get_balance 가 호출마다 앞에서부터 반환(마지막 값 유지)
        self.last_place_order_outcome = "ok"

    def get_candles(self, ticker, interval, limit):
        return self.candles.copy()

    def get_balance(self):
        if self.balance_seq:
            b = self.balance_seq.pop(0) if len(self.balance_seq) > 1 else self.balance_seq[0]
            return json.loads(json.dumps(b))
        return json.loads(json.dumps(self.balance))

    def get_open_orders(self, ticker):
        self.last_open_orders_failed = self.open_orders_fail
        if self.open_orders_fail:
            return []
        return [dict(o) for o in self.open_orders if o["ticker"] == ticker]

    def place_order(self, ticker, side, price, qty, order_type="LIMIT", client_order_id=None):
        if order_type == "MARKET":
            self.market_attempts += 1
            self.client_order_ids.append(client_order_id)
            if self.market_attempts <= self.market_fail_times:
                self.last_place_order_outcome = self.market_outcomes.pop(0) if self.market_outcomes else "rejected"
                return ""
        self.last_place_order_outcome = "ok"
        self._seq += 1
        oid = f"real_{self._seq}"
        self.placed.append({"order_id": oid, "ticker": ticker, "side": side, "price": price,
                            "qty": qty, "order_type": order_type})
        return oid

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        return self.cancel_result

    def get_order(self, order_id):
        d = self.order_details.get(order_id)
        if isinstance(d, list):  # 응답 시퀀스: 호출할 때마다 앞에서부터, 마지막 값은 유지
            return d.pop(0) if len(d) > 1 else d[0]
        return d

    def get_current_price(self, ticker):
        return float(self.candles.iloc[-1]["close"])

    def get_current_prices(self, tickers):
        return {t: self.get_current_price(t) for t in tickers}


def detail(status, filled_qty, avg_price, side="BUY", commission=0.1):
    return {"order_id": "x", "ticker": "TEST", "side": side, "order_type": "LIMIT", "status": status,
            "price": None, "qty": None, "filled_qty": filled_qty, "avg_fill_price": avg_price,
            "commission": commission, "filled_at": "2026-10-05T23:31:15.000+09:00", "canceled_at": None}


def make_config(prefix, **overrides):
    cfg = VwapConfigManager.get_default_config()
    base = {
        "ticker": "TEST", "market": "US", "interval": "1m",
        "n_percent": 1.0, "m_percent": 1.0, "x_percent": 2.0, "k_percent": 10.0,
        "reset_time": "22:30", "start_time": "", "initial_balance": 10000.0,
        "max_daily_loss_limit": 50.0,
        "use_adx_filter": False, "use_rsi_filter": False, "use_vwap_band": False,
    }
    base.update(overrides)
    for k, v in base.items():
        cfg[f"{prefix}_{k}"] = v
    cfg["toss_client_id"] = "test_id"
    cfg["toss_client_secret"] = "test_secret"
    cfg["toss_account_seq"] = "1"
    return cfg


class Patched:
    """VwapConfigManager.load_config 와 bot 모듈의 TossBroker 를 테스트용으로 교체."""

    def __init__(self, config, fake):
        self.config = config
        self.fake = fake

    def __enter__(self):
        self._orig_load = VwapConfigManager.load_config
        self._orig_toss = bot_module.TossBroker
        cfg = self.config
        VwapConfigManager.load_config = classmethod(lambda cls: dict(cfg))
        fake = self.fake
        bot_module.TossBroker = lambda client_id, client_secret, account_seq: fake
        return self

    def __exit__(self, *a):
        VwapConfigManager.load_config = self._orig_load
        bot_module.TossBroker = self._orig_toss


# ---------------------------------------------------------------------------
# 1. 가상 봇 BUY -> SELL, STOP_LOSS
# ---------------------------------------------------------------------------
def test_virtual_flow():
    print("\n1. [가상 봇] VIRTUAL_1 BUY 체결 -> SELL 체결 -> 기록")
    reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True

        # 주기1: 가격이 VWAP 아래 -> BUY 지정가 주문 (아직 체결 안 됨)
        fake.candles = make_candles([100.0] * 30 + [98.0])
        bot._loop_step()
        vb = bot.virtual_broker
        buy_orders = [o for o in vb.open_orders if o["side"] == "BUY"]
        check("주기1: 가상 BUY 지정가 주문 접수", len(buy_orders) == 1,
              f"open_orders={[(o['side'], o['price'], o['qty']) for o in vb.open_orders]}")
        buy_price = buy_orders[0]["price"] if buy_orders else 0

        # 주기2: 저가가 지정가 아래로 -> update_simulation 이 호출되어 체결돼야 함 (기존 버그: 호출 안 됨)
        fake.candles = make_candles([100.0] * 30 + [98.0, 98.0], last_low=buy_price - 0.5)
        bot._loop_step()
        t = trades("VIRTUAL_1")
        check("주기2: 가상 BUY 체결 기록(vwap_trades_virtual_1.json)", len(t) == 1 and t[0]["side"] == "BUY",
              f"trades={[(x['side'], x['price'], x['qty']) for x in t]}")
        check("주기2: 가상 보유 수량 반영", vb.holdings.get("TEST", {}).get("qty", 0) > 0, f"holdings={vb.holdings}")

        # 주기3: 가격이 VWAP 위로 -> SELL 지정가 주문
        fake.candles = make_candles([100.0] * 30 + [98.0, 98.0, 101.0])
        bot._loop_step()
        sell_orders = [o for o in vb.open_orders if o["side"] == "SELL"]
        check("주기3: 가상 SELL 지정가 주문 접수", len(sell_orders) == 1)
        sell_price = sell_orders[0]["price"] if sell_orders else 0

        # 주기4: 고가가 매도 지정가 이상 -> 체결
        fake.candles = make_candles([100.0] * 30 + [98.0, 98.0, 101.0, 101.0], last_high=sell_price + 0.5)
        bot._loop_step()
        t = trades("VIRTUAL_1")
        sells = [x for x in t if x["side"] == "SELL"]
        check("주기4: 가상 SELL 체결 기록 + 손익 양수", len(sells) == 1 and sells[0]["pnl"] > 0,
              f"sell={sells[0] if sells else None}")
        check("가상 경로에서 실거래 브로커 주문 호출 0회", len(fake.placed) == 0, f"placed={fake.placed}")

    print("\n1-b. [가상 봇] STOP_LOSS -> force_market_stop_loss 경로 (실거래 파일 오염 없음)")
    reset_data_dir()
    # 이전 실행에서 매수해둔 가상 포지션(평단 100, 10주)을 거래기록으로 시드
    VwapConfigManager.save_trades([{"trade_id": "v_seed", "timestamp": "2026-10-05 09:00:00", "ticker": "TEST",
                                    "side": "BUY", "price": 100.0, "qty": 10, "pnl": 0.0, "roi": 0.0}], "VIRTUAL_1")
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True
        fake.candles = make_candles([100.0] * 30 + [95.0])  # 손절가 98 하향 이탈
        bot._loop_step()
        t = trades("VIRTUAL_1")
        sl = [x for x in t if x["side"] == "STOP_LOSS"]
        check("가상 STOP_LOSS 가 가상 파일에 기록", len(sl) == 1 and sl[0]["price"] == 95.0 and sl[0]["qty"] == 10,
              f"sl={sl}")
        check("vwap_trades_real.json 미생성(실거래 파일 오염 없음)",
              not os.path.exists(os.path.join(TMP_DIR, "vwap_trades_real.json")))
        check("실거래 MARKET 주문 호출 0회", len(fake.placed) == 0)


# ---------------------------------------------------------------------------
# 2. 실거래 체결 판정
# ---------------------------------------------------------------------------
def new_real_bot():
    bot = VWAPBot("REAL")
    bot.tracked_open_orders = {}
    bot.last_holdings_qty = {}
    bot._holdings_snapshot_ready = True
    return bot


def test_real_reconcile():
    print("\n2. [실거래] 체결 판정 — 모의 응답 주입")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()

    # (a) FILLED BUY -> 실제 평균체결가로 기록
    bot._track_order("A", "TEST", "BUY", 50.0, 10, entry_price=0.0, vwap=50.6, target_price=50.0, signal="BUY")
    fake.order_details["A"] = detail("FILLED", 10, 49.95)
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 10, "entry_price": 49.95}}, 50.2)
    t = trades("REAL")
    check("(a) FILLED: 평균체결가 49.95·수량 10·fill_source=order_api 로 기록",
          len(t) == 1 and t[0]["price"] == 49.95 and t[0]["qty"] == 10 and t[0]["fill_source"] == "order_api"
          and t[0]["order_status"] == "FILLED" and t[0]["commission"] == 0.1,
          f"{ {k: t[0][k] for k in ('side','price','qty','fill_source','order_status','timestamp')} if t else None}")
    check("(a) 기존 필드(trade_id,timestamp,ticker,side,price,qty,pnl,roi) 유지",
          t and all(k in t[0] for k in ("trade_id", "timestamp", "ticker", "side", "price", "qty", "pnl", "roi")))
    check("(a) 추적 목록에서 제거", "A" not in bot.tracked_open_orders)

    # (b) CANCELED, 체결 0 -> 기록 없음
    bot._track_order("B", "TEST", "BUY", 49.0, 10)
    fake.order_details["B"] = detail("CANCELED", 0, None)
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 10, "entry_price": 49.95}}, 50.2)
    check("(b) CANCELED(체결 0): 거래 기록하지 않음", len(trades("REAL")) == 1 and "B" not in bot.tracked_open_orders)

    # (c) 부분 체결 후 취소 -> 체결된 4주만 기록
    bot._track_order("C", "TEST", "BUY", 49.0, 10)
    bot._cancel_order(fake, "C")
    check("(c) 취소 성공해도 즉시 추적 해제하지 않음(cancel_requested_at 표시)",
          "C" in bot.tracked_open_orders and bot.tracked_open_orders["C"]["cancel_requested_at"])
    fake.order_details["C"] = detail("CANCELED", 4, 48.9)
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 14, "entry_price": 49.6}}, 50.2)
    t = trades("REAL")
    check("(c) 부분체결 후 CANCELED: 체결수량 4주 @48.9 기록", len(t) == 2 and t[-1]["qty"] == 4 and t[-1]["price"] == 48.9,
          f"last={t[-1] if t else None}")

    # (d) 목록엔 없지만 상세상 PARTIAL_FILLED -> 계속 추적
    bot._track_order("D", "TEST", "BUY", 49.0, 10)
    fake.order_details["D"] = detail("PARTIAL_FILLED", 3, 49.0)
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 14, "entry_price": 49.6}}, 50.2)
    check("(d) PARTIAL_FILLED(진행중): 기록 없이 추적 유지", "D" in bot.tracked_open_orders and len(trades("REAL")) == 2)
    bot.tracked_open_orders.pop("D")

    # (e) SELL 체결 손익은 '주문 시점 평단 스냅샷'으로 (체결 후 보유 0 -> 평단 0 이어도)
    bot._track_order("E", "TEST", "SELL", 51.0, 10, entry_price=48.0, signal="SELL")
    fake.order_details["E"] = detail("FILLED", 10, 51.0, side="SELL")
    bot._reconcile_real_orders(fake, [], {}, 51.0)
    t = trades("REAL")
    check("(e) SELL 손익 = (51-48)*10 = 30 (스냅샷 평단 사용)", t[-1]["side"] == "SELL" and t[-1]["pnl"] == 30.0
          and t[-1]["entry_price_snapshot"] == 48.0, f"pnl={t[-1]['pnl']}, roi={t[-1]['roi']}")

    # (f) 상세조회 실패 -> 보유수량 diff 폴백
    reset_data_dir()
    bot = new_real_bot()
    bot.last_holdings_qty = {"TEST": 0.0}
    bot._track_order("F", "TEST", "BUY", 50.0, 10)
    fake.order_details["F"] = None
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 10, "entry_price": 50.0}}, 50.3)
    t = trades("REAL")
    check("(f) 조회 실패 + 보유 0->10: holdings_diff 로 10주 @주문가 기록",
          len(t) == 1 and t[0]["fill_source"] == "holdings_diff" and t[0]["qty"] == 10 and t[0]["price"] == 50.0,
          f"{t[0] if t else None}")

    # (g) 조회 실패 + 보유 변화 없음 -> 3주기 재시도 후 기록 없이 추적 종료
    bot._track_order("G", "TEST", "BUY", 49.0, 10)
    fake.order_details["G"] = None
    for i in range(3):
        still = "G" in bot.tracked_open_orders
        bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 10, "entry_price": 50.0}}, 50.3)
    check("(g) 판정 불가: 3회 재시도 후 기록 없이 추적 종료", "G" not in bot.tracked_open_orders and len(trades("REAL")) == 1)

    # (h) 영속화: 추적 주문이 파일로 저장되고 재시작(start 시 로드)으로 복원
    bot._track_order("H", "TEST", "SELL", 52.0, 10, entry_price=50.0, vwap=51.5, target_price=52.0, signal="SELL")
    path = os.path.join(TMP_DIR, "vwap_tracked_orders_real.json")
    check("(h) vwap_tracked_orders_real.json 저장됨", os.path.exists(path))
    bot2 = VWAPBot("REAL")
    bot2._load_tracked()
    h = bot2.tracked_open_orders.get("H", {})
    check("(h) 재시작 후 복원 + 스냅샷(평단/vwap/target/signal) 보존",
          h.get("entry_price") == 50.0 and h.get("vwap") == 51.5 and h.get("target_price") == 52.0 and h.get("signal") == "SELL",
          f"H={h}")


def test_market_exit():
    print("\n3. [실거래] 시장가 청산 체결가 기록")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    fake._seq = 0
    fake.order_details["real_1"] = detail("FILLED", 10, 95.5, side="SELL")
    bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 96.0, 99.0, 98.0, "STOP_LOSS")
    t = trades("REAL")
    check("체결 조회 성공: STOP_LOSS @실제체결가 95.5, pnl=-45, fill_source=order_api",
          len(t) == 1 and t[0]["side"] == "STOP_LOSS" and t[0]["price"] == 95.5 and t[0]["pnl"] == -45.0
          and t[0]["fill_source"] == "order_api", f"{t[0] if t else None}")

    fake.order_details["real_2"] = None
    bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 96.0, 99.0, 98.0, "PANIC_STOP")
    t = trades("REAL")
    check("체결 조회 불가: 봉 종가 96.0 으로 기록 + fill_source=assumed",
          len(t) == 2 and t[-1]["price"] == 96.0 and t[-1]["fill_source"] == "assumed", f"{t[-1]}")

    fake.order_details["real_3"] = detail("PENDING", 0, None, side="SELL")
    bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 96.0, 99.0, 98.0, "STOP_LOSS")
    check("아직 진행중: 기록하지 않고 추적 목록 등록(record_side=STOP_LOSS)",
          len(trades("REAL")) == 2 and bot.tracked_open_orders.get("real_3", {}).get("record_side") == "STOP_LOSS")
    fake.order_details["real_3"] = detail("FILLED", 10, 95.0, side="SELL")
    bot._reconcile_real_orders(fake, [], {}, 95.2)
    t = trades("REAL")
    check("다음 주기 정산: STOP_LOSS @95.0 기록", t[-1]["side"] == "STOP_LOSS" and t[-1]["price"] == 95.0
          and t[-1]["order_type"] == "MARKET", f"{t[-1]}")


# ---------------------------------------------------------------------------
# 4. 세션 날짜
# ---------------------------------------------------------------------------
def _old_t_reset(dt_now, reset_time):
    """수정 전 bot.py start_time 블록의 T_reset 계산 로직 사본 (동치성 비교용)."""
    reset_h, reset_m = map(int, reset_time.split(':'))
    dt_reset_today = dt_now.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
    if dt_now >= dt_reset_today:
        return dt_reset_today
    return dt_reset_today - timedelta(days=1)


def test_session_date():
    print("\n4. [세션 날짜] 경계 케이스")
    cases = [
        ("22:30", datetime(2026, 10, 5, 22, 29, 59), "2026-10-04"),
        ("22:30", datetime(2026, 10, 5, 22, 30, 0), "2026-10-05"),
        ("22:30", datetime(2026, 10, 6, 0, 30, 0), "2026-10-05"),   # 자정 넘어도 같은 세션
        ("22:30", datetime(2026, 10, 6, 5, 59, 0), "2026-10-05"),
        ("22:30", datetime(2026, 10, 6, 22, 29, 0), "2026-10-05"),
        ("22:30", datetime(2027, 1, 1, 1, 0, 0), "2026-12-31"),     # 연도 경계
        ("22:30", datetime(2026, 3, 1, 0, 10, 0), "2026-02-28"),    # 월 경계
        ("09:00", datetime(2026, 10, 5, 8, 59, 0), "2026-10-04"),
        ("09:00", datetime(2026, 10, 5, 9, 0, 0), "2026-10-05"),
        ("00:00", datetime(2026, 10, 5, 23, 59, 0), "2026-10-05"),
        ("00:00", datetime(2026, 10, 6, 0, 0, 0), "2026-10-06"),
        ("abc", datetime(2026, 10, 6, 1, 0, 0), "2026-10-06"),      # 잘못된 설정 -> 달력 날짜 폴백
    ]
    for reset, now, expected in cases:
        got = get_session_date(now, reset)
        check(f"reset={reset} now={now:%Y-%m-%d %H:%M:%S} -> {expected}", got == expected, f"got={got}")

    mismatches = 0
    start = datetime(2026, 10, 4, 0, 0)
    for reset in ["22:30", "09:00", "00:00", "23:59", "17:05"]:
        for m in range(0, 60 * 48, 7):
            now = start + timedelta(minutes=m, seconds=13)
            if get_session_start(now, reset) != _old_t_reset(now, reset):
                mismatches += 1
    check("get_session_start == 수정 전 start_time 의 T_reset 계산 (5개 리셋 x 48시간 격자)", mismatches == 0,
          f"mismatches={mismatches}")


# ---------------------------------------------------------------------------
# 5. 실거래 일 손실한도 (봇 자본금 기준) / 미체결 조회 실패 보류
# ---------------------------------------------------------------------------
def test_real_daily_loss():
    print("\n5. [실거래] 일 손실한도 — 봇 자본금 기준 발동")
    reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    # 계좌 현금은 100만 달러로 크지만, 봇 자본금(initial_balance)은 1000
    fake.balance = {"cash": 1_000_000.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}}
    cfg = make_config("real", initial_balance=1000.0, max_daily_loss_limit=5.0, x_percent=50.0)
    with Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot.running = True
        fake.candles = make_candles([100.0] * 31)
        fake.order_details = {}
        bot._loop_step()
        check("주기1: 기준자산 = 봇자본금 1000 + 평가손익 0", abs(bot.daily_baseline_asset - 1000.0) < 1e-6,
              f"baseline={bot.daily_baseline_asset}")
        check("주기1: 봇 정상 가동 유지", bot.running is True)

        fake.candles = make_candles([100.0] * 30 + [94.0])  # 평가손익 -60 -> 봇 기준 -6%
        # 주기1에서 SELL 지정가가 나갔을 수 있으므로, 패닉 시장가 주문이 받을 '다음' 주문 ID에 체결 응답을 주입
        fake.order_details[f"real_{fake._seq + 1}"] = detail("FILLED", 10, 93.9, side="SELL")
        placed_before = len(fake.placed)
        bot._loop_step()
        old_rate = 60.0 / (1_000_000.0 + 1000.0) * 100
        check("주기2: 손실 6% >= 한도 5% -> 패닉스탑 발동, 봇 정지", bot.running is False,
              f"(수정 전 계좌기준 손실률이었다면 {old_rate:.4f}% 로 미발동)")
        new_orders = fake.placed[placed_before:]
        check("패닉 시장가 매도 제출", len(new_orders) == 1 and new_orders[0]["order_type"] == "MARKET"
              and new_orders[0]["side"] == "SELL", f"{new_orders}")
        t = trades("REAL")
        check("패닉 청산 기록: STOP_LOSS @93.9(실제 체결가), order_api", len(t) == 1 and t[0]["price"] == 93.9
              and t[0]["fill_source"] == "order_api" and t[0]["signal"] == "PANIC_STOP", f"{t[0] if t else None}")

    print("\n5-b. [가상] 일 손실한도 합리성 — 가상 장부 기준")
    reset_data_dir()
    VwapConfigManager.save_trades([{"trade_id": "v_seed", "timestamp": "2026-10-05 09:00:00", "ticker": "TEST",
                                    "side": "BUY", "price": 100.0, "qty": 50, "pnl": 0.0, "roi": 0.0}], "VIRTUAL_1")
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1", initial_balance=10000.0, max_daily_loss_limit=5.0, x_percent=50.0), fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True
        fake.candles = make_candles([100.0] * 31)
        bot._loop_step()
        check("가상 기준자산 = 현금 5000 + 평가 5000 = 10000", abs(bot.daily_baseline_asset - 10000.0) < 1e-6,
              f"baseline={bot.daily_baseline_asset}")
        fake.candles = make_candles([100.0] * 30 + [88.0])  # 50주 x -12 = -600 -> -6%
        bot._loop_step()
        t = trades("VIRTUAL_1")
        check("가상 손실 6% -> 패닉: 가상 STOP_LOSS 기록 + 봇 정지, 실거래 주문 0",
              bot.running is False and any(x["side"] == "STOP_LOSS" for x in t) and len(fake.placed) == 0)


def test_open_orders_failure():
    print("\n6. [실거래] 미체결 조회 실패 시 판정/신규주문 보류")
    reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.balance = {"cash": 5000.0, "holdings": {}}
    cfg = make_config("real", initial_balance=1000.0)
    with Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot._load_tracked()
        bot.running = True
        bot._track_order("OLD", "TEST", "BUY", 99.0, 1)
        fake.open_orders_fail = True
        fake.candles = make_candles([100.0] * 30 + [98.0])
        bot._loop_step()
        check("조회 실패 주기: 새 주문 0건, 추적 주문 유지, 거래기록 0",
              len(fake.placed) == 0 and "OLD" in bot.tracked_open_orders and len(trades("REAL")) == 0)

        # 잔고 조회 실패(error 플래그) 주기도 건너뛰는지
        fake.open_orders_fail = False
        fake.balance = {"cash": 0.0, "holdings": {}, "error": True}
        bot._loop_step()
        check("잔고 조회 실패(error 플래그) 주기: 판정/주문 보류", len(fake.placed) == 0 and "OLD" in bot.tracked_open_orders)

        # 정상 주기: BUY 주문 제출 + 스냅샷과 함께 추적/저장
        fake.balance = {"cash": 5000.0, "holdings": {}}
        fake.open_orders = [{"order_id": "OLD", "ticker": "TEST", "side": "BUY", "price": 99.0, "qty": 1.0, "created_at": 0}]
        # 취소 요청 직후 PENDING_CANCEL -> 다음 조회에서 CANCELED (취소 종료 확인 후에만 재주문해야 함)
        fake.order_details["OLD"] = [detail("PENDING_CANCEL", 0, None), detail("CANCELED", 0, None)]
        bot._loop_step()
        saved = VwapConfigManager.load_tracked_orders("REAL")["orders"]
        new_ids = [p["order_id"] for p in fake.placed]
        check("정상 주기: 취소 종료(PENDING_CANCEL->CANCELED) 확인 후 정정 주문 제출 + 스냅샷 저장",
              len(new_ids) == 1 and new_ids[0] in saved and saved[new_ids[0]]["signal"] == "BUY"
              and saved[new_ids[0]]["vwap"] > 0 and saved["OLD"]["cancel_requested_at"],
              f"placed={fake.placed}, saved_keys={list(saved.keys())}")


# ---------------------------------------------------------------------------
# 7. QA 재검증 케이스
# ---------------------------------------------------------------------------
class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_market_exit_retry():
    print("\n7-a. [실거래] 취소 직후 시장가 매도 1회 거부(409 재현) -> 재시도 성공")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    bot._track_order("BUYO", "TEST", "BUY", 95.0, 5)
    fake.order_details["BUYO"] = [detail("PENDING_CANCEL", 0, None), detail("CANCELED", 0, None)]
    fake.balance = {"cash": 0.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}}
    fake.market_fail_times = 1
    fake.order_details["real_1"] = detail("FILLED", 10, 94.8, side="SELL")
    res = bot._real_liquidate(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS", ["BUYO"])
    t = trades("REAL")
    check("취소 종료 확인 후 시장가 2회째에 체결 -> result=filled, STOP_LOSS @94.8 기록",
          res["result"] == "filled" and fake.market_attempts == 2 and len(t) == 1 and t[0]["price"] == 94.8,
          f"res={res}, market_attempts={fake.market_attempts}")
    check("취소 요청이 시장가 매도보다 먼저", fake.canceled == ["BUYO"])

    print("\n7-a2. [실거래] 시장가가 체결 0 으로 REJECTED -> 남은 수량 재시도, 부분체결 합산")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    fake.balance_seq = [{"cash": 0.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}},
                        {"cash": 0.0, "holdings": {"TEST": {"qty": 6, "entry_price": 100.0}}}]
    fake.order_details["real_1"] = detail("REJECTED", 0, None, side="SELL")
    fake.order_details["real_2"] = detail("CANCELED", 4, 94.0, side="SELL")
    fake.order_details["real_3"] = detail("FILLED", 6, 93.5, side="SELL")
    res = bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS")
    t = trades("REAL")
    check("REJECTED -> 재시도 4주 체결 -> 남은 6주 재시도 체결 = filled, 기록 2건(4+6)",
          res["result"] == "filled" and [x["qty"] for x in t] == [4.0, 6.0] and fake.placed[-1]["qty"] == 6.0,
          f"res={res}, qtys={[x['qty'] for x in t]}, placed_qty={[p['qty'] for p in fake.placed]}")


def test_panic_all_fail():
    print("\n7-b. [실거래] 패닉스탑 시장가 재시도 모두 실패 -> 정지 + CRITICAL 로그")
    reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.balance = {"cash": 1_000_000.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}}
    cfg = make_config("real", initial_balance=1000.0, max_daily_loss_limit=5.0, x_percent=50.0)
    lh = ListHandler()
    logging.getLogger("vwap_bot_real").addHandler(lh)
    try:
        with Patched(cfg, fake):
            bot = VWAPBot("REAL")
            bot.running = True
            fake.candles = make_candles([100.0] * 31)
            bot._loop_step()
            fake.market_fail_times = 99
            fake.market_attempts = 0
            fake.candles = make_candles([100.0] * 30 + [94.0])
            bot._loop_step()
            crit = [r for r in lh.records if r.levelno == logging.CRITICAL]
            check("시장가 3회 시도 후 실패 -> 봇 정지", bot.running is False and fake.market_attempts == 3,
                  f"market_attempts={fake.market_attempts}")
            check("CRITICAL 로그(미청산 수량 포함) 출력", len(crit) == 1 and "미청산 10" in crit[0].getMessage(),
                  crit[0].getMessage()[:90] if crit else "없음")
            check("실패한 청산은 거래 기록 없음", len(trades("REAL")) == 0)
    finally:
        logging.getLogger("vwap_bot_real").removeHandler(lh)


def test_stop_loss_when_open_orders_fail():
    print("\n7-c. [실거래] 미체결 조회 실패 주기에도 STOP_LOSS 진행")
    reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.balance = {"cash": 5000.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}}
    cfg = make_config("real", initial_balance=1000.0, max_daily_loss_limit=50.0, x_percent=2.0)
    with Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot.running = True
        bot._holdings_snapshot_ready = True
        bot._track_order("PEND_SELL", "TEST", "SELL", 101.0, 10)
        fake.order_details["PEND_SELL"] = [detail("PENDING_CANCEL", 0, None), detail("CANCELED", 0, None)]
        fake.open_orders_fail = True
        fake.candles = make_candles([100.0] * 30 + [97.0])  # 손절가 98 하향
        fake.order_details["real_1"] = detail("FILLED", 10, 96.9, side="SELL")
        bot._loop_step()
        t = trades("REAL")
        check("조회 실패여도 추적 중 주문 취소 -> 시장가 손절 체결 기록",
              fake.canceled == ["PEND_SELL"] and len(fake.placed) == 1 and fake.placed[0]["order_type"] == "MARKET"
              and len(t) == 1 and t[0]["side"] == "STOP_LOSS" and t[0]["price"] == 96.9,
              f"canceled={fake.canceled}, placed={fake.placed}")


def test_corrupt_trades_file():
    print("\n7-d. [저장] 거래기록 json 손상 시 덮어쓰지 않음")
    reset_data_dir()
    path = os.path.join(TMP_DIR, "vwap_trades_real.json")
    corrupt = '[{"trade_id": "t1", "price": 1.0}, {"trade_id": "t2", "pri'
    with open(path, "w", encoding="utf-8") as f:
        f.write(corrupt)
    ok = VwapConfigManager.add_trade({"trade_id": "t3", "side": "BUY", "price": 2.0, "qty": 1}, "REAL")
    backups = [n for n in os.listdir(TMP_DIR) if n.startswith("vwap_trades_real.json.corrupt-")]
    backup_content = open(os.path.join(TMP_DIR, backups[0]), encoding="utf-8").read() if backups else ""
    check("손상 원본이 .corrupt-타임스탬프 로 원문 그대로 보존", len(backups) == 1 and backup_content == corrupt, f"{backups}")
    check("새 파일에는 이번 거래만 기록(원본 덮어쓰기 아님)", ok and [x["trade_id"] for x in trades("REAL")] == ["t3"])
    check("저장 시 임시파일(.tmp) 잔존 없음", not os.path.exists(path + ".tmp"))


def test_stop_start_single_loop():
    print("\n7-e. [스레드] stop -> 즉시 start 시 루프(주기 실행) 1개만")
    import threading as _th
    import time as _time
    bot = VWAPBot("VIRTUAL_1")
    state = {"active": 0, "max_active": 0, "threads": set(), "calls": 0}
    guard = _th.Lock()

    def fake_step():
        with guard:
            state["active"] += 1
            state["calls"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            state["threads"].add(_th.current_thread().name)
        _time.sleep(0.4)
        with guard:
            state["active"] -= 1

    bot._loop_step = fake_step
    bot.start()
    old_thread = bot.thread
    _time.sleep(0.1)          # 이전 세대가 주기 실행 중일 때
    bot.stop()
    bot.start()               # 즉시 재시작
    new_thread = bot.thread
    _time.sleep(1.8)          # 이전 세대가 1초 sleep 루프에서 세대 변경을 감지하고 종료할 시간
    bot.stop()
    check("동시에 실행된 주기 수 최대 1", state["max_active"] == 1, f"max_active={state['max_active']}")
    check("이전 세대 스레드 종료", not old_thread.is_alive())
    check("새 세대 스레드가 주기를 실행", state["calls"] >= 2 and new_thread is not old_thread,
          f"calls={state['calls']}")
    new_thread.join(timeout=3)
    check("stop 후 새 세대 스레드도 종료", not new_thread.is_alive())


def test_qa_round3():
    print("\n8-a. [실거래] 취소 확인 중 기존 SELL 4주 부분체결 -> 시장가 6주로 청산 성공 (N1)")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    bot._track_order("S1", "TEST", "SELL", 101.0, 10, entry_price=100.0, signal="SELL")
    fake.order_details["S1"] = [detail("PENDING_CANCEL", 2, 101.0, side="SELL"), detail("CANCELED", 4, 101.0, side="SELL")]
    fake.balance = {"cash": 0.0, "holdings": {"TEST": {"qty": 6, "entry_price": 100.0}}}  # 4주는 이미 팔림
    fake.order_details["real_1"] = detail("FILLED", 6, 95.0, side="SELL")
    res = bot._real_liquidate(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "PANIC_STOP", ["S1"])
    check("시장가 수량이 주기 시작 10주가 아니라 실제 보유 6주로 보정 -> filled",
          res["result"] == "filled" and len(fake.placed) == 1 and fake.placed[0]["qty"] == 6.0,
          f"res={res}, placed={[(p['qty'], p['order_type']) for p in fake.placed]}")
    bot._reconcile_real_orders(fake, [], {}, 95.0)
    t = trades("REAL")
    check("취소 전 체결된 SELL 4주 @101 은 다음 정산에서 SELL 로 기록(손익 +4), 청산 6주는 STOP_LOSS",
          sorted((x["side"], x["qty"]) for x in t) == [("SELL", 4.0), ("STOP_LOSS", 6.0)]
          and [x for x in t if x["side"] == "SELL"][0]["pnl"] == 4.0,
          f"{[(x['side'], x['qty'], x['price'], x['pnl']) for x in t]}")

    print("\n8-b. [실거래] 시장가가 초과수량으로 거부 -> 재시도 전 잔고 재조회로 수량 보정 후 성공 (N1)")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    fake.market_fail_times = 1
    fake.balance = {"cash": 0.0, "holdings": {"TEST": {"qty": 7, "entry_price": 100.0}}}
    fake.order_details["real_1"] = detail("FILLED", 7, 95.0, side="SELL")
    res = bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS")
    check("1회차 10주 거부 -> 2회차 7주로 체결", res["result"] == "filled" and fake.placed[-1]["qty"] == 7.0,
          f"res={res}")

    print("\n8-c. [실거래] 재시도 전 보유 0 확인 -> 청산 완료 처리 (N1)")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    fake.market_fail_times = 1
    fake.balance = {"cash": 0.0, "holdings": {}}
    res = bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS")
    check("보유 0 -> 추가 주문 없이 filled", res["result"] == "filled" and fake.market_attempts == 1, f"res={res}")

    print("\n8-d. [실거래] clientOrderId 재사용 규칙 (n1)")
    reset_data_dir()
    fake = FakeTossBroker()
    bot = new_real_bot()
    fake.balance = {"cash": 0.0, "holdings": {"TEST": {"qty": 10, "entry_price": 100.0}}}
    fake.market_fail_times = 2
    fake.market_outcomes = ["unknown", "rejected"]
    fake.order_details["real_1"] = detail("FILLED", 10, 95.0, side="SELL")
    res = bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS")
    ids = fake.client_order_ids
    check("접수 불명(unknown) 뒤에는 같은 키 재사용, 명시적 거부(rejected) 뒤에는 새 키",
          res["result"] == "filled" and len(ids) == 3 and ids[0] == ids[1] and ids[2] != ids[1]
          and all(i and len(i) <= 36 for i in ids),
          f"ids={ids}")

    print("\n8-e. [저장] 거래기록 손상 백업이 봇 로그에 ERROR 로 남음 (n3)")
    reset_data_dir()
    with open(os.path.join(TMP_DIR, "vwap_trades_real.json"), "w", encoding="utf-8") as f:
        f.write("{broken")
    lh = ListHandler()
    logging.getLogger("vwap_bot_real").addHandler(lh)
    try:
        bot = new_real_bot()
        bot._track_order("Z", "TEST", "BUY", 50.0, 1)
        ok = bot._record_real_fill("Z", bot.tracked_open_orders["Z"], 50.0, 1, "order_api", "FILLED", "avg_fill_price")
        errs = [r.getMessage() for r in lh.records if r.levelno == logging.ERROR and "거래기록 파일" in r.getMessage()]
        check("봇 로거 ERROR 에 손상/백업 경로 기록 + 거래는 새 파일에 기록",
              ok and len(errs) == 1 and ".corrupt-" in errs[0] and [x["trade_id"] for x in trades("REAL")] == ["Z"],
              errs[0][:100] if errs else "없음")
    finally:
        logging.getLogger("vwap_bot_real").removeHandler(lh)


def main():
    print("=" * 70)
    print(" VWAP 봇 신뢰성 수정 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % TMP_DIR)
    print("=" * 70)
    tests = [test_virtual_flow, test_real_reconcile, test_market_exit, test_session_date,
             test_real_daily_loss, test_open_orders_failure, test_market_exit_retry, test_panic_all_fail,
             test_stop_loss_when_open_orders_fail, test_corrupt_trades_file, test_stop_start_single_loop,
             test_qa_round3]
    for fn in tests:
        try:
            fn()
        except Exception:
            RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    shutil.rmtree(TMP_DIR, ignore_errors=True)
    after = _hash_dir(REAL_DATA_DIR)
    check("운영 data/ 디렉터리 파일 변경 없음 (테스트 전후 해시 동일)", after == REAL_DATA_HASH_BEFORE)

    passed = sum(1 for _, ok in RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
