"""VWAP 섀도우 봇 — 실거래(REAL)와 같은 봉·같은 설정으로 '이상적인(가상) 체결'을 동시에 돌려 비교합니다.
(3단계 §5, ADR-0007 / Q1(a): 섀도우는 REAL 이 실행 중일 때만 REAL 과 함께 동작)

[구성]
- StaticCandleBroker : 시세 소스. REAL 이 이번 주기에 받은 캔들 df 만 그대로 돌려줍니다. 네트워크·계좌·주문 메서드는
                       호출되면 예외(ShadowNetworkError)를 내고 호출 횟수를 셉니다(= 실주문 경로 없음을 테스트로 증명).
- ShadowBot          : VWAPBot 하위 클래스, mode "VIRTUAL_SHADOW". 가상 브로커(VirtualBroker)만 씁니다. 자체 스레드·start() 없음.
                       봉 적재 훅(ATTACH_BARS_STORE=False)·Discord 알림 없음. 설정은 REAL 설정을 virtual_shadow_* 키로 매핑해 사용.
- ShadowRunner       : REAL 의 post-cycle 훅(hook). REAL 의 _step_lock 안에서 '동기'로 섀도우 1주기를 실행합니다.
                       → 네트워크 I/O 금지, 예외·지연은 REAL 에 영향이 없도록 전부 내부에서 처리.
                       원장 재동기화(REAL BOT_START / 세션 시작 / 종목·기준자본 변경 / 비활성→활성), 상태 파일 저장.
- compare(days)      : REAL vs 섀도우 비교. 키는 cycle_id(= 주기 마지막 봉 시각). 지정가 체결은 '주문을 낸 주기',
                       시장가 청산은 '결정한 주기' 기준(두 쪽 모두 레코드의 cycle_id 가 그렇게 기록됨).

[섀도우가 건너뛰는 주기]  (건너뛰어도 REAL 에는 아무 영향 없음. 사유는 status 의 skip 에 남고 같은 사유 이벤트는 10분에 1회)
- shadow_enabled=false (매 주기 ctx config 로 확인 → 실시간 토글)
- REAL 이 실행 중이 아님(ctx["running"]=False): 패닉 자동정지 주기·사용자 정지 주기 (Q1(a))
- REAL 주기가 DATA_UNTRUSTED / LOOP_ERROR 로 끝남, 캔들이 비었거나 출처가 toss/yahoo 가 아님 (ADR-0010)
  (DATA_UNAVAILABLE 은 건너뛰지 않음: 캔들이 비었으면 NO_CANDLES 로 걸러지고, 잔고/미체결 조회 실패는 REAL 쪽 사정이라
   섀도우는 같은 봉으로 정상 판단·가상 체결을 계속함 — 건너뛰면 그 봉의 가상 체결 기회가 사라져 SHADOW_UNFILLED 오탐이 생김)
- REAL 판단 시각(ctx candles_asof)이 없음(NO_ASOF). 있으면 섀도우 본체의 now 를 그 시각으로 고정(VWAPBot._now 위임, SR-3)
- 기준 자본금 미설정(<=0), 섀도우 자체 패닉 정지 후 같은 세션의 남은 주기

[파일] data/vwap_shadow_state.json(.tmp), vwap_trades_virtual_shadow.json, vwap_events_virtual_shadow.jsonl(.1),
       trading_bot_virtual_shadow.log — 모두 기존 sync 제외 패턴에 포함됩니다.
"""
import copy
import json
import os
import threading
import time
import uuid
import logging
from datetime import datetime, timedelta

import pandas as pd

import core.vwap.config_manager as _cm
from core.vwap.config_manager import VwapConfigManager
from core.vwap.bot import VWAPBot, TRUSTED_CANDLE_SOURCES
from core.vwap.broker import Broker, VirtualBroker
from core.vwap.session import SessionSpec
from core.vwap import events as vwap_events

logger = logging.getLogger("vwap_bot")

SHADOW_MODE = "VIRTUAL_SHADOW"
STATE_FILENAME = "vwap_shadow_state.json"
# REAL 주기가 이 사유로 끝났으면 df 가 신뢰할 수 없거나 REAL 이 판단을 보류한 주기 → 섀도우도 건너뜀 (ADR-0010)
# DATA_UNAVAILABLE 은 제외(SR-3): 빈 캔들은 NO_CANDLES 로 따로 걸러지고, 잔고/미체결 조회 실패는 REAL 쪽 사정이므로
# 섀도우는 같은 봉으로 계속 돌아야 그 봉의 가상 체결 판정을 잃지 않음
SKIP_REASON_CODES = ("DATA_UNTRUSTED", "LOOP_ERROR")
SECRET_KEYS = ("toss_client_id", "toss_client_secret", "toss_account_seq", "admin_password_hash")
SKIP_EVENT_REPEAT_SEC = 600          # 같은 건너뜀 사유 ERROR 이벤트는 10분에 1회
PANIC_REASON_CODE = "DAILY_LOSS_STOP"         # REAL 패닉 청산 거래의 reason_code
COMPARE_MAX_PAIRS = 300
COMPARE_EVENT_LIMIT = 10_000_000


class ShadowNetworkError(RuntimeError):
    """섀도우 경로에서 네트워크/계좌/주문 메서드가 호출됐을 때(= 있어서는 안 되는 일)."""


# ======================================================================
# 시세 소스 (네트워크 없음)
# ======================================================================
class StaticCandleBroker(Broker):
    """REAL 이 이번 주기에 받은 캔들 df 를 그대로 돌려주는 시세 소스. VirtualBroker.source_broker 로 한 번만 연결되므로
    객체는 유지하고 set_candles 로 df 만 교체합니다."""

    def __init__(self):
        # bot 본체가 TossBroker 를 새로 만들지 않도록 설정(빈 문자열)과 같은 값으로 둠
        self.client_id = ""
        self.client_secret = ""
        self.account_seq = ""
        self.mock_mode = False
        self.is_mock_only = False
        self.last_candles_source = ""
        self.last_candles_complete = True
        self.df = pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        self.network_calls = 0        # 금지된 메서드 호출 시도 횟수 (정상 동작이면 항상 0)
        self.candle_requests = 0

    def set_candles(self, df, source: str):
        self.df = df
        self.last_candles_source = str(source or "")

    def _forbidden(self, name):
        self.network_calls += 1
        raise ShadowNetworkError(f"섀도우는 {name}() 를 호출할 수 없습니다 (네트워크/계좌/주문 금지)")

    def get_candles(self, ticker, interval, limit):
        self.candle_requests += 1
        return self.df.copy()

    def get_current_price(self, ticker):
        return float(self.df["close"].iloc[-1]) if len(self.df) else 0.0

    def get_current_prices(self, tickers):
        return {t: self.get_current_price(t) for t in tickers}

    def get_balance(self):
        self._forbidden("get_balance")

    def place_order(self, ticker, side, price, qty, order_type="LIMIT", client_order_id=None):
        self._forbidden("place_order")

    def cancel_order(self, order_id):
        self._forbidden("cancel_order")

    def get_open_orders(self, ticker):
        self._forbidden("get_open_orders")

    def get_order(self, order_id):
        self._forbidden("get_order")


# ======================================================================
# 섀도우 봇
# ======================================================================
def build_shadow_config(real_cfg: dict) -> dict:
    """REAL 설정(평탄 dict) 복사본 → 섀도우 설정. real_X → virtual_shadow_X, 비밀값은 빈 문자열, 알림 끔."""
    out = {}
    for k, v in (real_cfg or {}).items():
        if k.startswith("real_"):
            out["virtual_shadow_" + k[len("real_"):]] = copy.deepcopy(v)
        else:
            out[k] = copy.deepcopy(v)
    for k in SECRET_KEYS:
        out[k] = ""
    out["virtual_shadow_discord_notify"] = False
    out["virtual_discord_notify"] = False
    out["virtual_shadow_is_running"] = False
    return out


class ShadowBot(VWAPBot):
    """VWAPBot 의 매매 로직을 그대로 쓰되, 가상 브로커·정적 시세만 쓰는 섀도우. 스레드를 만들지 않습니다."""
    ATTACH_BARS_STORE = False   # 같은 봉을 두 번 적재하지 않도록 (REAL 훅이 이미 적재)

    def __init__(self):
        super().__init__(SHADOW_MODE)
        self._shadow_config = {}
        self.static_broker = StaticCandleBroker()
        self.real_broker = self.static_broker     # 본체가 TossBroker 를 만들지 않도록 미리 채움
        self._notify_enabled = False
        self._asof = None                          # 이번 주기 REAL 판단 시각 (러너가 주기 직전에 설정)

    def _now(self) -> datetime:
        """(SR-3) 판단 시각 = REAL 이 이번 주기 캔들을 요청한 시각(ctx candles_asof). 세션 날짜·거래 시작 대기·손실한도
        기준일이 REAL 과 같은 시각으로 계산되므로 세션 경계 직전 주기도 건너뛸 필요가 없습니다."""
        return self._asof if isinstance(self._asof, datetime) else datetime.now()

    # --- 훅 (3단계 §0-(b)) ---
    def _load_config(self) -> dict:
        return dict(self._shadow_config)

    def _refresh_notify_flag(self, config: dict = None):
        """섀도우는 Discord 알림이 없습니다."""
        self._notify_enabled = False
        return False

    def set_config(self, real_cfg: dict):
        self._shadow_config = build_shadow_config(real_cfg)

    def ensure_virtual_broker(self, initial_balance: float, force_new: bool = False):
        """VirtualBroker 를 만들어 둡니다(본체가 직접 만들면 거래기록으로 원장을 재구성하므로 미리 만들고 원장은 러너가 덮어씀).
        시세 소스는 항상 같은 StaticCandleBroker 객체."""
        vb = self.virtual_broker
        if force_new or vb is None or vb.initial_balance != initial_balance:
            self.virtual_broker = VirtualBroker(initial_balance=initial_balance, ticker_source_broker=self.static_broker,
                                                mode=SHADOW_MODE)
        return self.virtual_broker

    def start(self):
        """섀도우는 스레드를 갖지 않습니다. ShadowRunner.hook 이 REAL 주기 안에서 _loop_step 을 동기 호출합니다."""
        self.logger.warning("[VIRTUAL_SHADOW] 섀도우는 start() 로 기동할 수 없습니다 (REAL 훅 전용).")
        return False


# ======================================================================
# 러너 (REAL 훅)
# ======================================================================
def _json_default(o):
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    return str(o)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class ShadowRunner:
    def __init__(self, clock=None):
        self._clock = clock or datetime.now          # 테스트가 고정 시계를 주입
        self._lock = threading.RLock()               # status() (Flask 스레드) vs hook() (REAL 스레드)
        self.boot_id = uuid.uuid4().hex[:8]          # 프로세스 식별 — 재시작 후 같은 generation 번호로 오인하지 않기 위함
        self.bot = None
        self._disabled_seen = False                  # 비활성 주기를 지났으면 다시 켜질 때 재동기화
        self._halted_session = None                  # 섀도우 자체 패닉으로 멈춘 세션 날짜
        self._skip_event_last = {}                   # 사유 -> time.monotonic (이벤트 억제)
        self._ledger_snap = None                     # status() 용 원장 스냅샷 (훅 스레드가 락 안에서 교체)
        self._s = {                                  # 상태 (status API 용). 시작 시 상태 파일에서 일부 복원
            "real_generation": None, "boot_id": None, "session_date": "", "ticker": "", "initial_balance": None,
            "synced_at": "", "sync_reason": "", "last_cycle_id": "", "last_run_at": "", "last_duration_ms": None,
            "last_status": {}, "last_skip": None, "halted": False, "ledger_discarded": False,
        }
        self._loaded = False

    # ------------------------------------------------------------------ 상태 파일
    @staticmethod
    def state_path() -> str:
        return os.path.join(_cm.DATA_DIR, STATE_FILENAME)

    def _load_state_file(self) -> dict:
        try:
            with open(self.state_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            st = self._load_state_file()
            for k in ("real_generation", "boot_id", "session_date", "ticker", "initial_balance", "synced_at",
                      "sync_reason", "last_cycle_id", "last_run_at", "last_duration_ms", "last_status", "last_skip",
                      "ledger_discarded"):
                if k in st:
                    self._s[k] = st[k]
            self._file_ledger = {k: st.get(k) for k in ("cash", "holdings", "open_orders", "order_meta")}

    def _save_state(self):
        """원장 + 동기화 정보를 원자적으로 저장. 실패해도 예외를 밖으로 내지 않습니다."""
        try:
            vb = self.bot.virtual_broker if self.bot is not None else None
            with self._lock:
                data = {k: self._s.get(k) for k in ("real_generation", "boot_id", "session_date", "ticker",
                                                    "initial_balance", "synced_at", "sync_reason", "last_cycle_id",
                                                    "last_run_at", "last_duration_ms", "last_status", "last_skip",
                                                    "ledger_discarded")}
            if vb is not None:
                data.update({"cash": vb.cash, "holdings": vb.holdings, "open_orders": vb.open_orders,
                             "order_meta": vb.order_meta})
            else:
                data.update({k: (getattr(self, "_file_ledger", {}) or {}).get(k) for k in
                             ("cash", "holdings", "open_orders", "order_meta")})
            path = self.state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=_json_default)
            os.replace(tmp, path)
        except Exception as e:
            try:
                logger.warning(f"[섀도우] 상태 파일 저장 실패(무시): {e}")
            except Exception:
                pass

    # ------------------------------------------------------------------ 봇 생성/복원
    def _ensure_bot(self, initial_balance: float):
        """섀도우 봇을 (처음이면) 만들고, 같은 기준자본의 상태 파일이 있으면 원장을 복원합니다.
        복원은 거래기록으로 재구성한 원장 대신 상태 파일을 쓰기 위함입니다(재동기화분은 거래기록에 없음)."""
        self._ensure_loaded()
        if self.bot is None:
            bot = ShadowBot()
            vb = bot.ensure_virtual_broker(initial_balance, force_new=True)
            st = self._load_state_file()
            if st and st.get("initial_balance") == initial_balance and isinstance(st.get("holdings"), dict) \
                    and st.get("cash") is not None:
                try:
                    vb.cash = float(st["cash"])
                    vb.holdings = copy.deepcopy(st["holdings"])
                    vb.open_orders = copy.deepcopy(st.get("open_orders") or [])
                    vb.order_meta = copy.deepcopy(st.get("order_meta") or {})
                except Exception:
                    pass
            self.bot = bot
        return self.bot

    # ------------------------------------------------------------------ 건너뜀 기록
    def _note_skip(self, reason: str, ctx: dict, text: str = ""):
        cid = (ctx or {}).get("cycle_id") or ""
        with self._lock:
            self._s["last_skip"] = {"reason": reason, "text": text, "at": _fmt(self._clock()), "cycle_id": cid}
        now = time.monotonic()
        last = self._skip_event_last.get(reason)
        if last is not None and now - last < SKIP_EVENT_REPEAT_SEC:
            return
        self._skip_event_last[reason] = now
        if reason == "DISABLED":
            return   # 사용자가 끈 것은 이벤트 불필요
        vwap_events.append_event(SHADOW_MODE, "ERROR", "warn", f"SHADOW_SKIP_{reason}",
                                 f"섀도우가 이 주기를 건너뜀: {text or reason}", {"cycle_id": cid, "reason": reason})

    # ------------------------------------------------------------------ 훅
    def hook(self, ctx: dict):
        """REAL 봇 post-cycle 훅. REAL 의 _step_lock 안에서 동기 실행됩니다. 어떤 예외도 밖으로 내지 않습니다."""
        t0 = time.perf_counter()
        try:
            self._ensure_loaded()   # 상태 파일을 먼저 읽어 둬야 _save_state 가 저장된 원장을 빈 값으로 덮지 않음
            self._run(ctx)
        except Exception as e:
            try:
                logger.warning(f"[섀도우] 훅 처리 중 예외(무시, 매매 영향 없음): {type(e).__name__}: {e}")
                self._note_skip("EXCEPTION", ctx, f"{type(e).__name__}: {str(e)[:120]}")
            except Exception:
                pass
        finally:
            with self._lock:
                self._s["last_duration_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
                self._s["last_run_at"] = _fmt(self._clock())
            self._save_state()

    def _run(self, ctx: dict):
        cfg = ctx.get("config") or {}
        # 1) 실시간 토글: 등록 시점이 아니라 매 주기 ctx config 로 확인
        if not bool(cfg.get("shadow_enabled", True)):
            self._disabled_seen = True
            self._note_skip("DISABLED", ctx, "shadow_enabled=false")
            return
        # 2) Q1(a): REAL 이 실행 중일 때만. 패닉 자동정지 주기·사용자 정지 주기는 running=False 로 들어옴
        if not ctx.get("running", True):
            self._note_skip("REAL_NOT_RUNNING", ctx, "REAL 이 정지/패닉 정지 상태인 주기")
            return
        # 3) ADR-0010: REAL 이 신뢰할 수 없거나 비어 있는 시세로 끝낸 주기, 판단을 보류한 주기
        rc = ctx.get("reason_code") or ""
        if rc in SKIP_REASON_CODES:
            self._note_skip(f"REAL_{rc}", ctx, f"REAL 주기 사유 {rc}")
            return
        df = ctx.get("df")
        if df is None or len(df) == 0:
            self._note_skip("NO_CANDLES", ctx, "캔들이 비어 있음")
            return
        src = str(ctx.get("candles_source") or "").strip().lower()
        if src not in TRUSTED_CANDLE_SOURCES:
            self._note_skip("UNTRUSTED_CANDLES", ctx, f"캔들 출처 '{src}' 는 신뢰 불가")
            return

        ticker = ctx.get("ticker") or cfg.get("real_ticker") or ""
        market = ctx.get("market") or cfg.get("real_market") or "US"
        reset_time = ctx.get("reset_time") or cfg.get("real_reset_time") or "22:30"
        try:
            initial_balance = float(cfg.get("real_initial_balance") or 0.0)
        except (TypeError, ValueError):
            initial_balance = 0.0
        if not ticker or initial_balance <= 0:
            self._note_skip("NO_BASE_CAPITAL", ctx, "종목 또는 기준 자본금(real_initial_balance)이 설정되지 않음")
            return

        # 4) 시계 고정(SR-3): REAL 의 판단 시각 = ctx["candles_asof"]. 섀도우 본체의 now 를 이 값으로 고정합니다
        #    (ShadowBot._now). 그래서 세션 리셋/거래 시작 시각 경계 직전 주기도 REAL 과 같은 시각으로 판단합니다.
        asof = ctx.get("candles_asof")
        if not isinstance(asof, datetime):
            self._note_skip("NO_ASOF", ctx, "REAL 판단 시각(candles_asof)이 없어 같은 시각으로 판단할 수 없음")
            return
        session = SessionSpec.for_market(market, reset_time, ticker)

        # 5) 재동기화 판단
        session_date = session.session_key(asof)
        bot = self._ensure_bot(initial_balance)
        with self._lock:
            st = dict(self._s)
        reason = None
        if st.get("real_generation") is None:
            reason = "FIRST_SYNC"
        elif ctx.get("generation") != st.get("real_generation") or st.get("boot_id") != self.boot_id:
            reason = "BOT_START"
        elif st.get("session_date") != session_date:
            reason = "SESSION_START"
        elif st.get("ticker") != ticker or st.get("initial_balance") != initial_balance:
            reason = "CONFIG_CHANGE"
        elif self._disabled_seen:
            reason = "REENABLED"
        if reason is None and self._halted_session == session_date:
            self._note_skip("SHADOW_HALTED", ctx, "섀도우가 자체 손실한도로 정지됨 (다음 세션/REAL 재가동 때 재개)")
            return
        if reason is not None:
            pos = ctx.get("position")
            if pos is None:
                self._note_skip("POSITION_UNKNOWN", ctx, "REAL 보유 조회 실패 — 재동기화를 다음 주기로 미룸")
                return
            self._resync(bot, ctx, reason, ticker, initial_balance, session_date, pos)

        # 6) 섀도우 1주기 (동기, 네트워크 없음)
        # 실주문 경로 방어(실행 전): 시세 소스·가상 브로커 원천이 모두 같은 정적 브로커여야만 실행
        if not self._broker_ok(bot):
            self._discard_bot()
            self._note_skip("BROKER_SWAPPED", ctx, "섀도우 시세 소스가 정적 브로커가 아니어서 섀도우를 폐기함(보호)")
            return
        bot.static_broker.set_candles(df, src)
        bot.set_config(cfg)
        bot.running = True
        bot._asof = asof
        try:
            bot._loop_step()
        except Exception as e:
            # _loop_step 이 _finish_cycle(error) 로 LOOP_ERROR 이벤트를 이미 남김. REAL 에는 전파하지 않음
            logger.warning(f"[섀도우] 주기 실행 예외(무시): {type(e).__name__}: {e}")
        finally:
            bot._asof = None
            try:
                bot._log_trade_file_warnings()
            except Exception:
                pass
        # 실주문 경로 방어(실행 후): 시세 소스가 정적 브로커가 아니면(= 본체가 TossBroker 를 만들었다면) 즉시 폐기
        if not self._broker_ok(bot):
            self._discard_bot()
            self._note_skip("BROKER_SWAPPED", ctx, "섀도우 시세 소스가 바뀌어 섀도우를 폐기함(보호)")
            return
        if not bot.running:   # 섀도우 자체 일 손실한도 패닉 → 같은 세션의 남은 주기는 쉼
            self._halted_session = session_date
        with self._lock:
            sc = bot.status_cache or {}
            self._s["last_cycle_id"] = ctx.get("cycle_id") or ""
            self._s["last_status"] = {"reason_code": sc.get("reason_code") or "", "reason_text": sc.get("reason_text") or "",
                                      "signal": sc.get("signal") or ""}
            self._s["halted"] = self._halted_session == session_date
            self._s["last_skip"] = None
            self._s["ledger_discarded"] = False
            self._ledger_snap = self._snapshot(bot)

    def _discard_bot(self):
        """섀도우 봇 폐기. 다음 주기에 새 봇을 만들면 반드시 REAL 보유로 다시 맞추도록(FIRST_SYNC) 동기화 기록도 지웁니다."""
        self.bot = None
        with self._lock:
            self._s["real_generation"] = None
            self._s["ledger_discarded"] = True       # 폐기됨(다음 동기화 대기) — status 는 position/cash 를 null 로 내림
            self._ledger_snap = None
            self._file_ledger = {k: None for k in ("cash", "holdings", "open_orders", "order_meta")}   # 오래된 원장 재저장 방지

    @staticmethod
    def _broker_ok(bot) -> bool:
        """섀도우가 정적 시세 + 가상 브로커만 쓰는 상태인지. bot 본체가 TossBroker 를 만들었거나 가상 브로커의 시세 원천이
        바뀌었으면 False → 러너가 섀도우를 폐기합니다."""
        sb = bot.static_broker
        vb = bot.virtual_broker
        return (type(sb) is StaticCandleBroker and bot.real_broker is sb
                and (vb is None or (type(vb) is VirtualBroker and vb.source_broker is sb)))

    @staticmethod
    def _snapshot(bot):
        """status() 용 원장 복사본 (훅 스레드에서 생성 → Flask 스레드는 이 복사본만 읽음)."""
        vb = bot.virtual_broker if bot is not None else None
        if vb is None:
            return None
        return {"cash": float(vb.cash), "holdings": copy.deepcopy(vb.holdings or {})}

    def _resync(self, bot: ShadowBot, ctx: dict, reason: str, ticker: str, initial_balance: float,
                session_date: str, pos: dict):
        """섀도우 원장을 REAL 의 실제 보유·평단·기준자본으로 맞춥니다. cash = 기준자본 − 보유수량×평단. 미체결 주문은 비움."""
        if bot.virtual_broker is None or bot.virtual_broker.initial_balance != initial_balance:
            bot.ensure_virtual_broker(initial_balance, force_new=True)
        vb = bot.virtual_broker
        prev = {"cash": round(float(vb.cash), 4),
                "holdings": {t: {"qty": float(h.get("qty", 0.0)), "entry_price": float(h.get("entry_price", 0.0))}
                             for t, h in (vb.holdings or {}).items()},
                "open_orders": len(vb.open_orders or [])}
        qty = float(pos.get("qty") or 0.0)
        entry = float(pos.get("entry_price") or 0.0)
        vb.cash = float(initial_balance) - (qty * entry if qty > 0 else 0.0)
        # (SR-3) REAL 보유 원가가 기준자본보다 크면 섀도우 현금이 음수가 됩니다. 0 으로 자르지 않습니다 —
        # 'REAL 봇 자본 = 기준자본 - 보유 원가'를 그대로 반영해야 청산 후 현금(= 기준자본 + 손익)이 REAL 과 맞습니다.
        # 음수 현금이면 섀도우는 신규 매수를 못 하지만(BUDGET_SHORT / 가상 주문 잔고 부족), 보유 중에는 전략이 BUY 를 내지 않으므로
        # 청산 전까지 REAL 과 판단이 같습니다. 대신 SHADOW_SYNC 를 warn + over_allocated=true 로 남겨 원인을 보이게 합니다.
        over_allocated = vb.cash < 0
        vb.holdings = {ticker: {"qty": qty, "entry_price": entry}} if qty > 0 else {}
        vb.open_orders = []
        vb.order_meta = {}
        bot.daily_baseline_asset = 0.0     # 손실한도 기준 자산도 REAL 처럼 새로 잡음
        bot.last_baseline_date = ""
        self._halted_session = None
        self._disabled_seen = False
        now_s = _fmt(self._clock())
        with self._lock:
            self._s.update({"real_generation": ctx.get("generation"), "boot_id": self.boot_id,
                            "session_date": session_date, "ticker": ticker, "initial_balance": initial_balance,
                            "synced_at": now_s, "sync_reason": reason, "halted": False, "ledger_discarded": False})
        with self._lock:
            self._ledger_snap = self._snapshot(bot)
        msg = f"섀도우 원장을 REAL 보유로 재동기화 ({reason}): {ticker} {qty:g}주 @ {entry:.2f}, 현금 {vb.cash:.2f}"
        if over_allocated:
            msg += " — REAL 보유 원가가 기준 자본금보다 커서 섀도우 현금이 음수(청산 전까지 신규 매수 없음)"
        vwap_events.append_event(
            SHADOW_MODE, "SHADOW_SYNC", "warn" if over_allocated else "info", reason, msg,
            {"reason": reason, "ticker": ticker, "generation": ctx.get("generation"), "session_date": session_date,
             "initial_balance": initial_balance, "cycle_id": ctx.get("cycle_id") or "",
             "real": {"qty": qty, "entry_price": entry}, "shadow_before": prev,
             "shadow_after": {"cash": round(vb.cash, 4), "qty": qty, "entry_price": entry},
             "over_allocated": over_allocated})
        self._save_state()

    # ------------------------------------------------------------------ 상태 조회 (Flask 스레드)
    def status(self, real_running: bool = None, enabled: bool = None) -> dict:
        self._ensure_loaded()
        with self._lock:
            s = copy.deepcopy(self._s)
            snap = copy.deepcopy(self._ledger_snap)
        pos, cash, ticker = {"qty": 0.0, "entry_price": 0.0}, None, s.get("ticker") or ""
        if snap is not None:
            # 훅 스레드가 락 안에서 만든 복사본만 읽음 (주기 실행 중인 가상 브로커 dict 를 직접 순회하지 않음)
            h = (snap.get("holdings") or {}).get(ticker) or {}
            pos = {"qty": float(h.get("qty", 0.0) or 0.0), "entry_price": round(float(h.get("entry_price", 0.0) or 0.0), 4)}
            cash = round(float(snap["cash"]), 2)
        elif s.get("ledger_discarded"):
            # 폐기 후 다음 동기화 대기 중: 오래된 현금/보유를 보이지 않도록 null (UI 는 null 을 '-' 로 표시)
            pos, cash = None, None
        else:
            led = getattr(self, "_file_ledger", {}) or {}
            h = ((led.get("holdings") or {}).get(ticker)) or {}
            pos = {"qty": float(h.get("qty", 0.0) or 0.0), "entry_price": round(float(h.get("entry_price", 0.0) or 0.0), 4)}
            cash = round(float(led["cash"]), 2) if led.get("cash") is not None else None
        ls = s.get("last_status") or {}
        return {
            "enabled": bool(enabled) if enabled is not None else True,
            "following": "REAL",
            "real_running": bool(real_running) if real_running is not None else False,
            "last_cycle_id": s.get("last_cycle_id") or "",
            "last_run_at": s.get("last_run_at") or "",
            "last_duration_ms": s.get("last_duration_ms"),
            "synced_at": s.get("synced_at") or "",
            "sync_reason": s.get("sync_reason") or "",
            "ticker": ticker,
            "session_date": s.get("session_date") or "",
            "position": pos,
            "cash": cash,
            "reason_code": ls.get("reason_code") or "",
            "reason_text": ls.get("reason_text") or "",
            "signal": ls.get("signal") or "",
            "halted": bool(s.get("halted")),
            "skip": s.get("last_skip"),
            "ledger_discarded": bool(s.get("ledger_discarded")),     # (추가 필드) true 면 폐기됨 — 다음 동기화 대기
        }


# ======================================================================
# 비교 (REAL vs 섀도우)
# ======================================================================
VERDICTS = ("MATCH", "PRICE_GAP", "REAL_UNFILLED", "SHADOW_UNFILLED", "BOTH_UNFILLED", "REAL_ONLY_ORDER",
            "SHADOW_ONLY_ORDER")


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_ts(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _load_side(mode: str, cutoff_s: str):
    """한 모드의 주문(이벤트)·체결(거래 레코드)을 키 (cycle_id, side)로 모읍니다. cycle_id 없는 레코드는 unkeyed 로 센다."""
    orders, fills, unkeyed = {}, {}, []
    try:
        evs = vwap_events.read_events(mode, limit=COMPARE_EVENT_LIMIT, types=("ORDER_PLACED", "ORDER_REPLACED"))
    except Exception:
        evs = []
    for ev in reversed(evs):    # 과거 → 최신 (같은 키는 최신 주문으로 덮음)
        d = ev.get("data") or {}
        cid = d.get("cycle_id") or ""
        side = d.get("side")
        if not cid or side not in ("BUY", "SELL") or cid < cutoff_s:
            continue
        if str(d.get("order_type", "LIMIT")).upper() == "MARKET":
            key = (cid, "STOP_LOSS")
            # 시장가의 '의도 가격' = 결정 시점 현재가(이벤트 data.current_price, 없으면 data.intended_price)
            ip = _f(d.get("current_price"))
            if ip is None or ip <= 0:
                ip = _f(d.get("intended_price"))
            o = orders.setdefault(key, {"order_id": d.get("order_id"), "order_price": ip if ip and ip > 0 else None,
                                        "qty": _f(d.get("qty")), "reason_code": ev.get("reason_code") or ""})
            o["order_id"] = d.get("order_id") or o["order_id"]
        else:
            orders[(cid, side)] = {"order_id": d.get("order_id"), "order_price": _f(d.get("price")),
                                   "qty": _f(d.get("qty")), "reason_code": ev.get("reason_code") or ""}
    try:
        trades = VwapConfigManager.load_trades(mode)
    except Exception:
        trades = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        cid = t.get("cycle_id") or ""
        side = str(t.get("side") or "").upper()
        if not cid:
            unkeyed.append(t)
            continue
        if cid < cutoff_s or side not in ("BUY", "SELL", "STOP_LOSS"):
            continue
        fills.setdefault((cid, side), []).append(t)
    # 이벤트가 회전/유실돼 주문 이벤트가 없어도 체결 레코드가 있으면 주문이 있었던 것
    for key, recs in fills.items():
        o_ex = orders.get(key)
        if o_ex is not None and o_ex.get("order_price") is None:
            # 시장가 주문 이벤트에 가격이 없으면 체결 레코드의 intended_price(결정 시점 가격)로 보완
            for r in recs:
                ip = _f(r.get("intended_price"))
                if ip is not None and ip > 0:
                    o_ex["order_price"] = ip
                    break
        if key not in orders:
            r = recs[0]
            op = _f(r.get("order_price"))
            if op is None or op <= 0:      # REAL 시장가 레코드는 order_price=0.0 → 결정 시점 가격(intended_price)으로 대체
                op = _f(r.get("intended_price"))
            orders[key] = {"order_id": r.get("trade_id"), "order_price": op,
                           "qty": _f(r.get("order_qty")) or _f(r.get("qty")), "reason_code": r.get("reason_code") or ""}
    return orders, fills, unkeyed


def _agg_fill(recs):
    """한 키의 체결 레코드(부분체결·시장가 재시도 포함)를 수량 가중 평균가로 합칩니다."""
    q = sum(_f(r.get("qty"), 0.0) for r in recs)
    if q <= 0:
        return None
    px = sum(_f(r.get("price"), 0.0) * _f(r.get("qty"), 0.0) for r in recs) / q
    last = max((r.get("timestamp") or "" for r in recs))
    return {"price": px, "qty": q, "filled_at": last}


def _side_obj(order, fill):
    if order is None:
        return None
    fp = fill["price"] if fill else None
    return {"order_id": order.get("order_id"),
            "order_price": round(order["order_price"], 4) if order.get("order_price") is not None else None,
            "fill_price": round(fp, 4) if fp is not None else None,
            "filled_at": fill["filled_at"] if fill else None,
            "qty": fill["qty"] if fill else order.get("qty")}


def _mode_summary(fills: dict, shadow_fee_pct: float = None):
    """키가 있는 체결 레코드로 모드별 요약. shadow_fee_pct 가 있으면 섀도우(추정 수수료), 없으면 REAL(레코드 commission)."""
    recs = [r for lst in fills.values() for r in lst]
    exits = [r for r in recs if str(r.get("side")).upper() in ("SELL", "STOP_LOSS")]
    # 거래수/승률은 '청산 결정' 단위 — pairs 와 같은 키 (cycle_id, side) 로 체결(재시도·부분체결)을 합침. 키 없는 레코드는 이미 제외됨.
    decisions = [lst for (_cid, sd), lst in fills.items() if sd in ("SELL", "STOP_LOSS") and lst]
    pnl = sum(_f(r.get("pnl"), 0.0) for r in recs)
    if shadow_fee_pct is None:
        fee = sum(_f(r.get("commission"), 0.0) for r in recs)
    else:
        fee = sum(_f(r.get("price"), 0.0) * _f(r.get("qty"), 0.0) for r in exits) * shadow_fee_pct / 100.0
    wins = sum(1 for lst in decisions if sum(_f(r.get("pnl"), 0.0) for r in lst) > 0)
    slips = [_f(r.get("slippage_pct")) for r in recs if _f(r.get("slippage_pct")) is not None]
    return {"trades": len(decisions), "fills": len(recs), "net_pnl": round(pnl - fee, 2),
            "win_rate_pct": round(wins / len(decisions) * 100.0, 2) if decisions else 0.0,
            "avg_slippage_pct": round(sum(slips) / len(slips), 4) if slips else 0.0}


def compare(days: int = 7, now: datetime = None, config: dict = None) -> dict:
    """REAL 과 섀도우를 cycle_id 로 짝지어 비교합니다 (설계 §5.4).

    키 (cycle_id, side): 지정가는 '주문을 낸 주기'(체결 레코드의 cycle_id 가 그 주기), 시장가 손절은 '결정한 주기'.
    판정은 설계 표 그대로이며, REAL 패닉 청산(reason_code=DAILY_LOSS_STOP)처럼 섀도우가 Q1(a) 로 일부러 건너뛴 주기의
    REAL 단독 주문은 REAL_ONLY_ORDER 대신 excluded 로 따로 셉니다(오탐 방지).
    """
    now = now or datetime.now()
    cfg = config if config is not None else VwapConfigManager.load_config()
    tol = _f(cfg.get("shadow_price_tolerance_pct"), 0.05)
    fee_pct = _f(cfg.get("shadow_fee_roundtrip_pct"), 0.2)
    cutoff_s = _fmt(now - timedelta(days=days))

    r_orders, r_fills, r_unkeyed = _load_side("REAL", cutoff_s)
    s_orders, s_fills, s_unkeyed = _load_side(SHADOW_MODE, cutoff_s)

    verdicts = {v: 0 for v in VERDICTS}
    excluded = {"REAL_PANIC": 0}
    pairs = []
    tickers = set()
    for key in set(r_orders) | set(s_orders):
        cid, side = key
        ro, so = r_orders.get(key), s_orders.get(key)
        rf = _agg_fill(r_fills.get(key, [])) if ro else None
        sf = _agg_fill(s_fills.get(key, [])) if so else None
        for lst in (r_fills.get(key, []), s_fills.get(key, [])):
            for r in lst:
                if r.get("ticker"):
                    tickers.add(r.get("ticker"))
        gap_pct, time_gap = None, None
        excluded_reason = None
        if ro and so:
            if rf and sf:
                gap_pct = (sf["price"] - rf["price"]) / rf["price"] * 100.0 if rf["price"] else None
                t1, t2 = _parse_ts(rf["filled_at"]), _parse_ts(sf["filled_at"])
                time_gap = round((t2 - t1).total_seconds(), 1) if t1 and t2 else None
                verdict = "MATCH" if (gap_pct is not None and abs(gap_pct) <= tol + 1e-9) else "PRICE_GAP"
            elif sf and not rf:
                verdict = "REAL_UNFILLED"
            elif rf and not sf:
                verdict = "SHADOW_UNFILLED"
            else:
                verdict = "BOTH_UNFILLED"
        elif ro:
            verdict = "REAL_ONLY_ORDER"
            if (ro.get("reason_code") == PANIC_REASON_CODE) or any(
                    r.get("reason_code") == PANIC_REASON_CODE for r in r_fills.get(key, [])):
                excluded_reason = "REAL_PANIC"
        else:
            verdict = "SHADOW_ONLY_ORDER"
        if excluded_reason:
            excluded[excluded_reason] += 1
        else:
            verdicts[verdict] += 1
        if verdict == "BOTH_UNFILLED":
            continue
        pair = {"cycle_id": cid, "side": side, "verdict": verdict,
                "real": _side_obj(ro, rf), "shadow": _side_obj(so, sf),
                "price_gap_pct": round(gap_pct, 4) if gap_pct is not None else None, "time_gap_sec": time_gap}
        if excluded_reason:
            pair["excluded_reason"] = excluded_reason
        pairs.append(pair)
    pairs.sort(key=lambda p: (p["cycle_id"], p["side"]), reverse=True)

    both = verdicts["MATCH"] + verdicts["PRICE_GAP"]
    real_sum = _mode_summary(r_fills)
    shadow_sum = _mode_summary(s_fills, shadow_fee_pct=fee_pct)
    shadow_sum["fee_roundtrip_pct"] = fee_pct
    real_sum["unkeyed_fills"] = sum(1 for t in r_unkeyed if str(t.get("timestamp") or "") >= cutoff_s)       # cycle_id 없는 REAL 체결(3단계 이전·앱에서 낸 주문): 비교·요약에서 제외
    return {
        "ticker": cfg.get("real_ticker") or "",
        "tickers": sorted(tickers),
        "period": {"from": cutoff_s, "to": _fmt(now), "days": days},
        "summary": {
            "real": real_sum, "shadow": shadow_sum, "verdicts": verdicts,
            "match_rate_pct": round(verdicts["MATCH"] / both * 100.0, 2) if both else None,
            "net_pnl_gap": round(shadow_sum["net_pnl"] - real_sum["net_pnl"], 2),
            "excluded": excluded,
            "definitions": {"trades": "청산 결정 수(같은 주기·같은 방향의 재시도 체결은 1건)", "fills": "전체 체결 수",
                            "net_pnl": "pnl - 수수료 (REAL: 레코드 commission, 섀도우: 청산 금액 x shadow_fee_roundtrip_pct)",
                            "net_pnl_gap": "섀도우 - REAL", "price_gap_pct": "(섀도우 체결가 - REAL 체결가) / REAL 체결가 x 100",
                            "scope": "cycle_id 가 있는 레코드만 (3단계 이후)"},
        },
        "pairs": pairs[:COMPARE_MAX_PAIRS],
    }
