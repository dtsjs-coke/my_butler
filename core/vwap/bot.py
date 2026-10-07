import os
import copy
import time
import uuid
import logging
import threading
import traceback
from datetime import datetime, timedelta
from core.vwap.config_manager import VwapConfigManager
from core.vwap.broker import TossBroker, VirtualBroker
from core.vwap.strategy import VwapStrategy
from core.vwap.session import SessionSpec
from core.vwap import events as vwap_events
from core.vwap import bars_store
from core.vwap.trade_metrics import enrich_record

# 프로젝트 루트 경로
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_PATH = os.path.join(PROJECT_ROOT, "trading_bot_virtual.log")

# 토스 OpenAPI 주문 상태 중 "아직 진행 중"인 것들 (GET /api/v1/orders?status=OPEN 그룹과 동일)
OPEN_ORDER_STATUSES = {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}
# 미체결 목록에서 사라진 주문의 상세 조회가 연속 실패할 때, 판정을 포기하기 전까지 재시도할 주기 수
MAX_ORDER_LOOKUP_RETRIES = 3
# 시장가 청산 직후 체결 결과를 확인하기 위한 상세 조회 횟수/간격(초)
MARKET_FILL_POLL_ATTEMPTS = 3
MARKET_FILL_POLL_INTERVAL_SEC = 0.5
# 시장가 청산 주문이 거부/미체결 종료됐을 때 재시도 횟수와 간격(초)
# (최악의 경우 대략 횟수 x (폴링 1초 + 간격 1초) = 수 초 이내로 루프 스레드를 점유)
MARKET_EXIT_MAX_ATTEMPTS = 3
MARKET_EXIT_RETRY_INTERVAL_SEC = 1.0
# 취소 요청 후 실제 종료(CANCELED 등)를 확인하는 폴링 횟수/간격(초).
# 토스 취소는 비동기(PENDING_CANCEL)라, 확인 전에 재주문하면 반대방향 주문 409 등으로 거부될 수 있음
CANCEL_CONFIRM_POLL_ATTEMPTS = 3
CANCEL_CONFIRM_POLL_INTERVAL_SEC = 0.5

# (관측성) 연속 N 주기 조회 실패(DATA_UNAVAILABLE/루프 예외) 시 ERROR 이벤트 + Discord 1회
ERROR_STREAK_NOTIFY = 5
# (관측성) 같은 키의 ERROR 이벤트(주문 거부 등)는 이 시간(초) 안에 한 번만 기록
ERROR_EVENT_REPEAT_SEC = 600
# (3단계 §5.1) post-cycle 훅 하나의 소요시간 경고 기준(초). 넘으면 ERROR(warn) 이벤트 (같은 훅은 10분에 1회)
HOOK_WARN_SEC = 2.0
# 훅 ctx["config"] 에서 비우는 비밀값 키 (훅은 비밀값이 필요 없음 — 실수로 기록/전송되는 것 방지)
HOOK_SECRET_KEYS = ("toss_client_id", "toss_client_secret", "toss_account_seq", "admin_password_hash")
# (ADR-0010) REAL 봇이 매매 판단에 쓸 수 있는 캔들 출처. 그 외("mock"=난수 봉, ""=출처 불명)는 판단 전체 보류
TRUSTED_CANDLE_SOURCES = ("toss", "yahoo")
# (ADR-0010) 신뢰 불가 캔들로 판단을 보류한 주기가 연속 이 횟수에 이르면 CRITICAL(손절 보호 공백) 이벤트 + Discord
UNTRUSTED_STREAK_CRITICAL = 3
# (ADR-0010) 이 사유들은 '아직 신뢰할 판단을 못 함' — 신뢰 불가 연속 횟수를 끊지 않음 (그 외 사유가 나오면 0 으로)
_UNTRUSTED_GAP_CODES = ("DATA_UNTRUSTED", "DATA_UNAVAILABLE", "LOOP_ERROR")
# (ADR-0010 정정) 신뢰 불가 '구간 시작' Discord 알림의 최소 간격(초). 이 안에 새 구간이 또 시작되면(신뢰/불가 깜빡임)
# 이벤트는 매번 기록하되 Discord 는 미루고, 다음 알림(구간 시작/CRITICAL/정상 복귀 요약)에 횟수를 묶어 보냄
UNTRUSTED_EPISODE_ALERT_MIN_SEC = 600
# (ADR-0010 정정) 신뢰 불가가 계속되면 첫 CRITICAL 이후 이 간격(초)마다 CRITICAL 재알림
UNTRUSTED_CRITICAL_REPEAT_SEC = 1800


def _monotonic() -> float:
    """알림 억제/재알림 간격 계산용 시각. 테스트가 고정 시각으로 바꿔 끼울 수 있게 함수로 둠."""
    return time.monotonic()


def _fmt_duration(sec: float) -> str:
    """경과 시간(초)을 '약 N분' / 'N시간 M분' 한글로."""
    m = int(max(0.0, sec) // 60)
    if m < 60:
        return f"약 {m}분"
    return f"{m // 60}시간 {m % 60}분"


def get_session_start(now: datetime, reset_time: str) -> datetime:
    """[하위 호환용] reset_time(HH:MM) 고정 기준 세션 시작. 봇 본체는 ADR-0008 이후 core/vwap/session.SessionSpec 을
    사용합니다(미국 종목 + 리셋 22:30/23:30 이면 서머타임 자동, 그 외는 이 함수와 같은 고정 reset_time 규칙).

    예) reset_time="22:30" (미국장)
        - 10/05 23:10 -> 10/05 22:30
        - 10/06 01:00 -> 10/05 22:30  (자정을 넘겨도 같은 세션)
        - 10/06 22:29 -> 10/05 22:30
    reset_time 형식이 잘못되면 ValueError 등이 발생합니다.
    """
    reset_h, reset_m = map(int, reset_time.split(':'))
    dt_reset_today = now.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
    if now >= dt_reset_today:
        return dt_reset_today
    return dt_reset_today - timedelta(days=1)


def get_session_date(now: datetime, reset_time: str) -> str:
    """now가 속한 VWAP 세션의 '세션 날짜'(세션 시작일, YYYY-MM-DD)를 반환합니다.
    reset_time 파싱에 실패하면 달력 날짜로 폴백합니다."""
    try:
        return get_session_start(now, reset_time).strftime("%Y-%m-%d")
    except Exception:
        return now.strftime("%Y-%m-%d")


def _format_api_time(iso_str) -> str:
    """토스 API의 ISO 8601 시각(KST)을 기존 거래기록 형식(YYYY-MM-DD HH:MM:SS, 서버 로컬시각)으로 변환합니다.
    변환 불가 시 빈 문자열."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

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
        self._vwap_partial_warned = ""  # (ADR-0008) 부분 VWAP 경고를 세션당 1회만 남기기 위한 키
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
            "vwap_stdev": 0.0,
            # --- 관측성 필드 ---
            "reason_code": "BOT_STOPPED",
            "reason_text": "봇이 정지 상태입니다.",
            "filters": {},
            "waiting_for_start": False,
            "entry_price": 0.0,
            "position_qty": 0.0,
        }

        # --- 관측성(이벤트 로그/알림) 상태 — 매매 판단에는 쓰이지 않음 ---
        self._cycle = None              # 현재 주기의 판단 사유/스냅샷 (_loop_step 에서 생성)
        self._last_signal_key = None    # 직전 주기 (signal, reason_code) — SIGNAL_CHANGE 중복 억제
        self._fail_streak = 0           # 연속 조회 실패 주기 수
        self._untrusted_streak = 0      # (ADR-0010, REAL) 신뢰 불가 캔들로 판단 보류한 연속 주기 수
        self._last_trusted_position_qty = None  # (ADR-0010, 알림 문구용) 신뢰 주기에서 마지막으로 확인한 보유 수량
        self._reset_untrusted_alert_state()
        self._error_last = {}          # ERROR 이벤트 키별 마지막 기록 시각 (반복 억제)
        self._notify_enabled = (self.mode == "REAL")  # 설정 real/virtual_discord_notify 로 매 주기 갱신
        self._stop_note = ""            # 정지 사유 (패닉 자동정지 등) — 정지 상태 reason_text 에 표시

        # 브로커 캐시
        self.real_broker = None
        self.virtual_broker = None
        
        # 실거래 주문 체결 감지를 위한 미체결 주문 목록 추적 (REAL 전용, data/vwap_tracked_orders_real.json 에 영속화)
        # {order_id: {"ticker", "side", "price", "qty", "order_type", "entry_price"(주문 시점 평단 스냅샷),
        #             "vwap", "target_price", "signal", "record_side", "submitted_at", "cancel_requested_at",
        #             "lookup_failures", "vanished_base_qty"}}
        self.tracked_open_orders = {}
        # 직전 주기의 종목별 보유수량 (체결 조회 API 실패 시 보유수량 diff 폴백용, REAL 전용, 함께 영속화)
        self.last_holdings_qty = {}
        self._holdings_snapshot_ready = False

        # stop() 직후 start() 시 이전 루프 스레드와 새 스레드가 동시에 주문을 내지 않도록:
        # - _generation: start()마다 증가, 이전 세대 루프는 이를 보고 스스로 종료
        # - _step_lock: 한 번에 하나의 _loop_step만 실행 (이전 세대의 진행 중 주기가 끝날 때까지 새 세대가 대기)
        self._generation = 0
        self._step_lock = threading.Lock()

        # (3단계 §5.1) 주기 종료 후 훅 [(name, fn(ctx))]. 매매 결정·주문이 모두 끝난 뒤 실행되며 예외·지연은 격리됩니다.
        # 봉 적재(bars_store.hook)는 모든 모드에 기본 등록. 섀도우 훅은 api/vwap_api.py 에서 REAL 에만 등록(Phase C).
        self._post_cycle_hooks = []
        self.last_hook_durations_ms = {}   # (관측) 마지막 주기의 훅별 소요시간
        if self.ATTACH_BARS_STORE:
            self.add_post_cycle_hook("bars_store", bars_store.hook)

    # 하위 클래스(ShadowBot 등)가 봉 적재 훅을 붙이지 않으려면 False 로 재정의
    ATTACH_BARS_STORE = True

    def _load_config(self) -> dict:
        """(3단계 §0-(b)) 주기 설정 로드 위임 지점. 기본은 VwapConfigManager.load_config() 그대로.
        ShadowBot 이 오버라이드해 REAL 설정을 섀도우 키로 매핑해 돌려줍니다."""
        return VwapConfigManager.load_config()

    def _now(self) -> datetime:
        """(3단계 SR-3, ADR-0007 메모) 주기 판단 시각 위임 지점. 기본은 datetime.now() 그대로(REAL·가상 동작 무변경).
        ShadowBot 이 오버라이드해 REAL 의 판단 시각(ctx candles_asof)을 돌려줍니다 — 세션 경계에서 두 봇의 시각이 갈리지 않게."""
        return datetime.now()

    def add_post_cycle_hook(self, name: str, fn) -> bool:
        """주기 종료 훅 등록 (같은 이름이 있으면 교체). fn(ctx) 의 반환값은 무시되고 예외는 봇 밖으로 나가지 않습니다."""
        if not callable(fn):
            return False
        self._post_cycle_hooks = [(n, f) for n, f in self._post_cycle_hooks if n != name] + [(name, fn)]
        return True

    def remove_post_cycle_hook(self, name: str) -> bool:
        before = len(self._post_cycle_hooks)
        self._post_cycle_hooks = [(n, f) for n, f in self._post_cycle_hooks if n != name]
        return len(self._post_cycle_hooks) != before

    def _is_virtual(self, mode: str = None) -> bool:
        """VIRTUAL / VIRTUAL_1~3 등 가상 모드 여부."""
        return (mode or self.mode).upper().startswith("VIRTUAL")

    def start(self):
        """트레이딩 봇 백그라운드 스레드를 시작합니다."""
        with self._lock:
            if self.running:
                self.logger.warning(f"[{self.mode} 봇] 이미 가동 중입니다.")
                return False

            self.running = True
            self.daily_baseline_asset = 0.0
            self.last_baseline_date = ""
            self._generation += 1
            # 추적 주문 복원(REAL)은 새 루프 스레드가 _step_lock 을 잡은 뒤 수행합니다
            # (이전 세대 주기가 아직 돌고 있을 수 있으므로)
            self._last_signal_key = None
            self._fail_streak = 0
            self._untrusted_streak = 0
            self._reset_untrusted_alert_state()
            self._stop_note = ""
            # (관측성) 첫 주기 이벤트보다 먼저 남도록 스레드 시작 전에 기록 (예외는 _emit 내부에서 처리)
            self._refresh_notify_flag()
            self._emit("BOT_START", "info", "", f"{self.mode} 봇 가동 시작", {"generation": self._generation})
            self.thread = threading.Thread(target=self._run_loop, args=(self._generation,), daemon=True)
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
            self._stop_note = "사용자 요청으로 정지되었습니다."
            self.logger.info(f"🛑 [{self.mode} 봇] 백그라운드 엔진 정지 요청이 접수되었습니다.")
        self._refresh_notify_flag()
        self._emit("BOT_STOP", "info", "BOT_STOPPED", f"{self.mode} 봇 정지 (사용자 요청)", {"by": "user"})
        return True

    # ------------------------------------------------------------------
    # 관측성: 이벤트 로그 / Discord 알림 / 판단 사유 (매매 동작에는 영향 없음, 모든 예외 내부 처리)
    # ------------------------------------------------------------------
    def _refresh_notify_flag(self, config: dict = None):
        """설정의 real_discord_notify / virtual_discord_notify 로 이 봇의 알림 여부를 갱신합니다."""
        try:
            cfg = config if config is not None else self._load_config()
            if self.mode == "REAL":
                self._notify_enabled = bool(cfg.get("real_discord_notify", True))
            else:
                self._notify_enabled = bool(cfg.get("virtual_discord_notify", False))
        except Exception:
            pass

    def _emit(self, etype: str, level: str, reason_code: str, message: str, data: dict = None,
              notify: bool = None, dedup_extra=None):
        """이벤트 1건 기록 + (설정/종류에 따라) Discord 알림. 절대 예외를 밖으로 던지지 않습니다.
        notify=None 이면 events.NOTIFY_TYPES 에 속한 종류만 알림. 시장가 청산 주문 등은 호출부가 False 지정."""
        try:
            ev = vwap_events.append_event(self.mode, etype, level, reason_code, message, data)
            if ev is None:  # 파일 기록 실패해도 알림은 시도
                ev = {"mode": vwap_events.normalize_mode(self.mode), "type": etype, "level": level,
                      "reason_code": reason_code, "message": message, "data": data or {}}
            if notify is None:
                notify = etype in vwap_events.NOTIFY_TYPES
            if notify and self._notify_enabled:
                key = (vwap_events.normalize_mode(self.mode), etype, reason_code or "", dedup_extra)
                vwap_events.notifier.notify(key, vwap_events.format_discord_message(ev))
        except Exception as e:
            try:
                self.logger.warning(f"[관측성] 이벤트 처리 실패(무시): {e}")
            except Exception:
                pass

    def _emit_error(self, key: str, message: str, data: dict = None, level: str = "warn",
                    reason_code: str = "", notify: bool = False):
        """같은 key 의 ERROR 이벤트는 ERROR_EVENT_REPEAT_SEC 안에 한 번만 기록합니다 (반복 오류 묶기)."""
        now = _monotonic()
        last = self._error_last.get(key)
        if last is not None and now - last < ERROR_EVENT_REPEAT_SEC:
            return False
        self._error_last[key] = now
        self._emit("ERROR", level, reason_code, message, data, notify=notify)
        return True

    def _set_reason(self, code: str, text: str, signal: str = None):
        """이번 주기의 최종 판단 사유를 지정합니다 (나중 호출이 앞의 것을 덮어씀)."""
        if self._cycle is None:
            return
        self._cycle["reason_code"] = code
        self._cycle["reason_text"] = text
        if signal is not None:
            self._cycle["signal"] = signal

    def _order_meta(self) -> dict:
        """주문/거래 레코드에 남길 '주문 당시' 판단 스냅샷."""
        c = self._cycle or {}
        return {
            "reason_code": c.get("reason_code") or "",
            "filters": c.get("filters") or {},
            "config_snapshot": c.get("config_snapshot") or {},
            "cycle_id": c.get("cycle_id") or "",
        }

    def _reason_data(self, **extra) -> dict:
        """이벤트 data 에 공통으로 넣을 현재 주기 수치."""
        c = self._cycle or {}
        d = dict(c.get("values") or {})
        if c.get("reason_text"):
            d["reason_text"] = c.get("reason_text")
        if c.get("cycle_id"):
            d["cycle_id"] = c.get("cycle_id")
        d.update({k: v for k, v in extra.items() if v is not None})
        return d

    def _on_order_placed(self, broker, kind: str, side: str, order_id: str, price: float, qty: float,
                         old_price: float = None, old_qty: float = None):
        """지정가 신규/정정 주문 제출 성공 후 호출 (이벤트 기록 + 가상 브로커 주문 메타 등록)."""
        try:
            meta = self._order_meta()
            if hasattr(broker, "order_meta") and isinstance(getattr(broker, "order_meta"), dict):
                broker.order_meta[order_id] = meta
            c = self._cycle or {}
            verb = "정정" if kind == "ORDER_REPLACED" else "제출"
            msg = f"{'매수' if side == 'BUY' else '매도'} 지정가 {verb}: {price:.2f} x {qty:g}주"
            if kind == "ORDER_REPLACED" and old_price is not None:
                msg += f" (기존 {old_price:.2f} x {old_qty:g}주)"
            self._emit(kind, "info", c.get("reason_code") or "", msg,
                       self._reason_data(order_id=order_id, side=side, price=round(float(price), 4), qty=qty,
                                         order_type="LIMIT", old_price=old_price, old_qty=old_qty),
                       dedup_extra=order_id)
        except Exception as e:
            self.logger.warning(f"[관측성] 주문 이벤트 처리 실패(무시): {e}")

    def _on_order_place_failed(self, side: str, price: float, qty: float):
        outcome = ""
        try:
            broker = self.real_broker if self.mode == "REAL" else self.virtual_broker
            outcome = getattr(broker, "last_place_order_outcome", "") or ""
        except Exception:
            pass
        self._emit_error(f"PLACE_FAIL:{side}", f"{'매수' if side == 'BUY' else '매도'} 지정가 주문 제출 실패 ({price:.2f} x {qty:g}주)",
                         self._reason_data(side=side, price=price, qty=qty, outcome=outcome),
                         reason_code="ORDER_REJECTED")

    def _on_replace_held(self, existing_order: dict, cancel_result: str):
        """정정을 위한 취소가 확인되지 않아 재주문을 보류한 경우 (ORDER_CANCELED, warn)."""
        self._emit("ORDER_CANCELED", "warn", (self._cycle or {}).get("reason_code") or "",
                   f"정정용 취소 결과 '{cancel_result}' → 이번 주기 재주문 보류 "
                   f"({existing_order.get('side')} {float(existing_order.get('price') or 0):.2f})",
                   self._reason_data(order_id=existing_order.get("order_id"), side=existing_order.get("side"),
                                     price=existing_order.get("price"), qty=existing_order.get("qty"),
                                     cancel_result=cancel_result))

    def _on_virtual_trade(self, record: dict):
        """VirtualBroker 가 거래를 기록한 직후 호출 (가상봇 FILL 이벤트)."""
        side = record.get("side")
        self._emit("FILL", "info", record.get("reason_code") or "",
                   f"가상 체결: {side} {record.get('ticker')} {record.get('qty')}주 @ {float(record.get('price') or 0):.2f}",
                   self._fill_data(record), dedup_extra=record.get("trade_id"))

    @staticmethod
    def _fill_data(record: dict) -> dict:
        keys = ("trade_id", "ticker", "side", "price", "qty", "pnl", "roi", "fill_source", "order_type",
                "order_price", "intended_price", "slippage", "slippage_pct", "holding_minutes", "reason_code", "commission",
                "cycle_id")
        return {k: record.get(k) for k in keys if record.get(k) is not None}

    def _finish_cycle(self, error: Exception = None):
        """주기 종료 시: 상태 캐시에 판단 사유 반영, SIGNAL_CHANGE(변화 시에만), 연속 실패 ERROR 처리."""
        try:
            c = self._cycle or {}
            if error is not None:
                self._set_reason("LOOP_ERROR", f"주기 실행 중 오류 → 이번 주기 보류: {type(error).__name__}: {str(error)[:150]}", "WAIT")
                self._emit_error(f"LOOP:{type(error).__name__}:{str(error)[:80]}",
                                 f"루프 실행 중 예외: {type(error).__name__}: {str(error)[:200]}",
                                 {"exception": type(error).__name__}, reason_code="LOOP_ERROR")

            code = c.get("reason_code")
            if not code:
                return

            # 연속 조회 실패 추적 (같은 오류 반복은 1회만 알림)
            if code in ("DATA_UNAVAILABLE", "LOOP_ERROR"):
                self._fail_streak += 1
                if self._fail_streak == ERROR_STREAK_NOTIFY:
                    self._emit("ERROR", "error", code,
                               f"{ERROR_STREAK_NOTIFY}주기 연속 조회/실행 실패 — 봇이 매매 판단을 못 하고 있습니다",
                               self._reason_data(streak=self._fail_streak), notify=True)
            elif code != "DATA_UNTRUSTED":  # (ADR-0010) 신뢰 불가 주기는 별도 연속 카운터(_untrusted_streak)로 다룸
                self._fail_streak = 0
            if code not in _UNTRUSTED_GAP_CODES:
                self._untrusted_streak = 0
                self._flush_untrusted_summary()

            values = c.get("values") or {}
            with self._lock:
                upd = {
                    "reason_code": code,
                    "reason_text": c.get("reason_text") or "",
                    "waiting_for_start": bool(c.get("waiting_for_start", False)),
                    "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                if c.get("filters") is not None:
                    upd["filters"] = c["filters"]
                if c.get("signal"):
                    upd["signal"] = c["signal"]
                for k in ("current_price", "vwap", "adx", "rsi", "stop_loss_price", "entry_price", "position_qty",
                          "session_label", "vwap_full_session"):
                    if k in values:
                        upd[k] = values[k]
                self.status_cache.update(upd)

            key = (c.get("signal"), code)
            if key != self._last_signal_key:
                prev = self._last_signal_key
                self._last_signal_key = key
                self._emit("SIGNAL_CHANGE", "info", code,
                           f"판단 변경: {c.get('reason_text') or code}",
                           self._reason_data(signal=c.get("signal"),
                                             prev_signal=prev[0] if prev else None,
                                             prev_reason_code=prev[1] if prev else None))
        except Exception as e:
            try:
                self.logger.warning(f"[관측성] 주기 마무리 처리 실패(무시): {e}")
            except Exception:
                pass

    def get_status(self) -> dict:
        """현재 봇 상태 캐시를 반환합니다."""
        with self._lock:
            self.status_cache["is_running"] = self.running
            if not self.running:
                try:
                    config = self._load_config()
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
                # (관측성) 정지 상태 표시
                try:
                    h = (self.status_cache.get("holdings") or {}).get(self.status_cache.get("ticker"), {})
                    self.status_cache["position_qty"] = float(h.get("qty", 0.0) or 0.0)
                    self.status_cache["entry_price"] = round(float(h.get("entry_price", 0.0) or 0.0), 2) if h else 0.0
                except Exception:
                    pass
                self.status_cache["reason_code"] = "BOT_STOPPED"
                self.status_cache["reason_text"] = self._stop_note or "봇이 정지 상태입니다."
                self.status_cache["waiting_for_start"] = False
            return self.status_cache

    # ------------------------------------------------------------------
    # 실거래(REAL) 주문 추적 / 체결 판정
    # ------------------------------------------------------------------
    def _load_tracked(self):
        """재시작 시 이전에 추적하던 주문과 직전 보유수량 스냅샷을 파일에서 복원합니다."""
        data = VwapConfigManager.load_tracked_orders("REAL")
        self.tracked_open_orders = data.get("orders", {}) or {}
        self.last_holdings_qty = {k: float(v) for k, v in (data.get("last_qty", {}) or {}).items()}
        # 파일에서 읽은 스냅샷은 오래됐을 수 있지만 '알려진 기준'으로는 사용합니다 (fill_source로 근거가 남음)
        self._holdings_snapshot_ready = bool(self.last_holdings_qty) or bool(self.tracked_open_orders)
        if self.tracked_open_orders:
            self.logger.info(f"📂 이전 실행에서 추적 중이던 주문 {len(self.tracked_open_orders)}건을 복원했습니다. 첫 주기에 체결 여부를 재판정합니다.")

    def _save_tracked(self):
        if self.mode != "REAL":
            return
        VwapConfigManager.save_tracked_orders({
            "orders": self.tracked_open_orders,
            "last_qty": self.last_holdings_qty,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }, "REAL")

    def _track_order(self, order_id: str, ticker: str, side: str, price: float, qty: float,
                     order_type: str = "LIMIT", entry_price: float = 0.0, vwap: float = 0.0,
                     target_price: float = 0.0, signal: str = "", record_side: str = None,
                     meta: dict = None, intended_price: float = None):
        """제출한(또는 발견한) 실거래 주문을 추적 목록에 등록하고 즉시 저장합니다.
        entry_price는 주문 시점의 평단가 스냅샷으로, 매도 체결 손익 계산에 사용됩니다
        (체결 후에는 보유가 0이 되어 평단가를 다시 조회할 수 없기 때문).
        meta / intended_price: (관측성) 주문 당시 판단 사유 스냅샷과 슬리피지 기준가(기본: 주문 단가)."""
        if self.mode != "REAL" or not order_id:
            return
        meta = meta if meta is not None else self._order_meta()
        self.tracked_open_orders[order_id] = {
            "ticker": ticker,
            "side": side,
            "price": float(price or 0.0),
            "qty": float(qty or 0.0),
            "order_type": order_type,
            "entry_price": float(entry_price or 0.0),
            "vwap": round(float(vwap or 0.0), 4),
            "target_price": round(float(target_price or 0.0), 4),
            "signal": signal,
            "record_side": record_side or side,
            "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cancel_requested_at": None,
            "lookup_failures": 0,
            "vanished_base_qty": None,
            # --- 관측성 (거래 레코드 보강용) ---
            "intended_price": round(float(intended_price if intended_price is not None else (price or 0.0)), 4),
            "reason_code": meta.get("reason_code", ""),
            "filters": meta.get("filters", {}),
            "config_snapshot": meta.get("config_snapshot", {}),
            "cycle_id": meta.get("cycle_id", "") or "",
        }
        self._save_tracked()

    def _cancel_order(self, broker, order_id: str) -> bool:
        """주문 취소. REAL에서는 취소 성공해도 추적 목록에서 바로 빼지 않고 '취소 요청됨'으로 표시만 합니다.
        취소 직전에 부분 체결됐을 수 있으므로, 다음 주기의 상세 조회로 체결 수량을 확인한 뒤 정리합니다."""
        ok = broker.cancel_order(order_id)
        if ok and self.mode == "REAL" and order_id in self.tracked_open_orders:
            self.tracked_open_orders[order_id]["cancel_requested_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_tracked()
        return ok

    def _record_real_fill(self, order_id: str, info: dict, fill_price: float, fill_qty: float,
                          fill_source: str, order_status: str, price_basis: str,
                          commission=None, filled_at=None):
        """실거래 체결을 vwap_trades_real.json 에 기록합니다.
        기존 필드(trade_id,timestamp,ticker,side,price,qty,pnl,roi)는 그대로 두고 판정 근거 필드만 추가합니다."""
        # 같은 주문이 (기록 직후 저장 전 재시작 등으로) 두 번 기록되지 않도록 trade_id 중복 확인
        # (이미 있으면 '기록된 것'으로 보고 True — 호출부가 추적을 정리하도록)
        existing_trades = VwapConfigManager.load_trades("REAL")
        if any(t.get("trade_id") == order_id for t in existing_trades):
            self.logger.warning(f"⚠️ 주문({order_id})은 이미 거래기록에 있어 중복 기록하지 않습니다.")
            return True

        pnl = 0.0
        roi = 0.0
        entry_snapshot = float(info.get("entry_price") or 0.0)
        if info.get("side") == "SELL" and entry_snapshot > 0 and fill_qty > 0:
            pnl = (fill_price - entry_snapshot) * fill_qty
            roi = (pnl / (entry_snapshot * fill_qty)) * 100.0

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "trade_id": order_id,
            "timestamp": _format_api_time(filled_at) or now_str,
            "ticker": info.get("ticker"),
            "side": info.get("record_side") or info.get("side"),
            "price": round(float(fill_price), 4),
            "qty": float(fill_qty),
            "pnl": round(pnl, 2),
            "roi": round(roi, 2),
            # --- 이하 추가 필드 (판정 근거 / 주문 시점 스냅샷) ---
            "fill_source": fill_source,          # "order_api" | "holdings_diff" | "assumed"
            "price_basis": price_basis,          # "avg_fill_price" | "order_price" | "current_price"
            "order_status": order_status,        # 토스 주문 상태 (판정 불가 시 "UNKNOWN")
            "order_type": info.get("order_type", "LIMIT"),
            "order_price": info.get("price"),
            "order_qty": info.get("qty"),
            "commission": commission,
            "entry_price_snapshot": entry_snapshot,
            "signal": info.get("signal"),
            "vwap": info.get("vwap"),
            "target_price": info.get("target_price"),
            "submitted_at": info.get("submitted_at"),
            "detected_at": now_str,
        }
        # (관측성) slippage/slippage_pct/holding_minutes/reason_code/filters/config_snapshot 추가.
        # 보강 실패가 체결 기록을 막지 않도록 예외는 삼킵니다.
        try:
            intended = info.get("intended_price")
            if intended is None:
                intended = info.get("price")
            enrich_record(record, existing_trades, intended,
                          {"reason_code": info.get("reason_code"), "filters": info.get("filters"),
                           "config_snapshot": info.get("config_snapshot"), "cycle_id": info.get("cycle_id")})
        except Exception as e:
            self.logger.warning(f"[관측성] 거래 레코드 보강 실패(무시): {e}")
        recorded = VwapConfigManager.add_trade(record, "REAL")
        self._log_trade_file_warnings()
        if not recorded:
            self.logger.error(f"❌ 주문({order_id}) 체결 기록을 거래기록 파일에 쓰지 못했습니다. 추적을 유지하고 다음 주기에 재시도합니다.")
            return False
        self.logger.info(
            f"🎉 [실거래 체결 기록] {record['side']} {record['ticker']} {fill_qty}주 @ {fill_price:.2f} "
            f"(손익: {pnl:+.2f}, 근거: {fill_source}/{order_status})"
        )
        self._emit("FILL", "info", record.get("reason_code") or "",
                   f"체결: {record['side']} {record['ticker']} {fill_qty:g}주 @ {fill_price:.2f} (손익 {pnl:+.2f})",
                   self._fill_data(record), dedup_extra=order_id)
        return True

    def _reconcile_real_orders(self, broker, open_orders: list, holdings: dict, current_price: float):
        """추적 중인 주문 중 미체결 목록에서 사라진 것의 실제 결과를 판정합니다.

        1순위: 주문 상세 조회(GET /api/v1/orders/{id}) — 상태와 실제 체결가/체결수량 사용 (fill_source="order_api")
        2순위: 상세 조회 실패 시 보유수량 변화로 교차검증 (fill_source="holdings_diff")
        판정 불가가 MAX_ORDER_LOOKUP_RETRIES 주기 지속되면 기록 없이 추적 종료(수동 확인 필요 로그).
        취소/거부(체결수량 0)로 판정되면 거래를 기록하지 않습니다.
        """
        current_open_ids = {o["order_id"] for o in open_orders}
        changed = False
        diff_used = {}  # 같은 주기에 여러 주문이 같은 보유 변화량을 중복으로 가져가지 않도록

        for oid, info in list(self.tracked_open_orders.items()):
            if oid in current_open_ids:
                # 목록 지연 등으로 잠시 사라졌다 다시 보이면 폴백 상태 초기화
                if info.get("lookup_failures") or info.get("vanished_base_qty") is not None:
                    info["lookup_failures"] = 0
                    info["vanished_base_qty"] = None
                    changed = True
                continue

            t = info.get("ticker")
            if info.get("vanished_base_qty") is None and self._holdings_snapshot_ready:
                # 사라지기 직전 주기의 보유수량을 diff 기준으로 고정
                info["vanished_base_qty"] = float(self.last_holdings_qty.get(t, 0.0))
                changed = True

            detail = broker.get_order(oid) if hasattr(broker, "get_order") else None

            if detail is not None:
                status = detail.get("status", "")
                if status in OPEN_ORDER_STATUSES:
                    # 목록엔 없지만 상세상 아직 진행 중 → 계속 추적
                    continue
                filled_qty = float(detail.get("filled_qty") or 0.0)
                if filled_qty > 0:
                    avg_price = detail.get("avg_fill_price")
                    if avg_price is not None and avg_price > 0:
                        fill_price, basis = avg_price, "avg_fill_price"
                    elif info.get("price", 0.0) > 0:
                        fill_price, basis = info["price"], "order_price"
                    else:
                        fill_price, basis = current_price, "current_price"
                    if not self._record_real_fill(oid, info, fill_price, filled_qty, "order_api", status, basis,
                                                  commission=detail.get("commission"), filled_at=detail.get("filled_at")):
                        continue  # 기록 실패 -> 추적 유지, 다음 주기 재시도
                else:
                    self.logger.info(f"ℹ️ [주문 종료] {info.get('side')} {t} 주문({oid})이 체결 없이 종료되었습니다 (상태: {status}). 거래 기록하지 않습니다.")
                # 기록 직후 바로 추적 해제 + 저장 (중간에 꺼져도 같은 주문을 재기록하지 않도록)
                self.tracked_open_orders.pop(oid, None)
                self._save_tracked()
                continue

            # --- 상세 조회 실패: 보유수량 diff 폴백 ---
            info["lookup_failures"] = int(info.get("lookup_failures") or 0) + 1
            changed = True
            base = info.get("vanished_base_qty")
            if base is not None:
                now_qty = float(holdings.get(t, {}).get("qty", 0.0))
                delta = now_qty - float(base) - diff_used.get(t, 0.0)
                order_qty = float(info.get("qty") or 0.0)
                if info.get("side") == "BUY":
                    filled_qty = min(order_qty, max(delta, 0.0))
                else:
                    filled_qty = min(order_qty, max(-delta, 0.0))
                if filled_qty > 0:
                    if info.get("price", 0.0) > 0:
                        fill_price, basis = info["price"], "order_price"
                    else:
                        fill_price, basis = current_price, "current_price"
                    self.logger.warning(f"⚠️ [체결 판정 폴백] 주문({oid}) 상세 조회 실패 → 보유수량 변화({base}→{now_qty})로 체결 {filled_qty}주 판정.")
                    if not self._record_real_fill(oid, info, fill_price, filled_qty, "holdings_diff", "UNKNOWN", basis):
                        continue  # 기록 실패 -> 추적 유지
                    diff_used[t] = diff_used.get(t, 0.0) + (filled_qty if info.get("side") == "BUY" else -filled_qty)
                    self.tracked_open_orders.pop(oid, None)
                    self._save_tracked()
                    continue

            if info["lookup_failures"] >= MAX_ORDER_LOOKUP_RETRIES:
                self.logger.error(
                    f"❌ [체결 판정 불가] 주문({oid}, {info.get('side')} {t} {info.get('qty')}주 @ {info.get('price')}) — "
                    f"상세 조회 {info['lookup_failures']}회 실패, 보유수량 변화도 없음. 미체결 종료로 보고 추적을 중단합니다. "
                    f"토스 앱에서 실제 체결 여부를 수동 확인하세요."
                )
                self.tracked_open_orders.pop(oid, None)
            else:
                self.logger.warning(f"⚠️ 주문({oid}) 결과 판정 보류 ({info['lookup_failures']}/{MAX_ORDER_LOOKUP_RETRIES}) — 다음 주기에 재조회합니다.")

        # 다음 주기 diff 기준이 될 보유수량 스냅샷 갱신
        new_snapshot = {tk: float(v.get("qty", 0.0)) for tk, v in holdings.items()}
        if new_snapshot != self.last_holdings_qty or not self._holdings_snapshot_ready:
            self.last_holdings_qty = new_snapshot
            changed = True
        self._holdings_snapshot_ready = True

        if changed:
            self._save_tracked()

    def _cancel_and_wait(self, broker, order_id: str) -> str:
        """주문 취소를 요청하고, 실제로 종료됐는지 짧게 확인합니다 (토스 취소는 비동기 PENDING_CANCEL).

        Returns:
            "confirmed"   — 체결 없이 종료 확인(또는 가상 브로커처럼 즉시 취소되는 경우). 재주문해도 안전.
            "filled"      — 취소 전에 (일부라도) 체결됨. 보유수량이 바뀌었으므로 이번 주기 재주문 금지.
            "unconfirmed" — 취소 요청은 됐지만 종료 확인 실패(아직 PENDING_CANCEL 또는 조회 불가).
            "failed"      — 취소 요청 자체 실패(이미 체결/취소 등). 재주문 금지.
        총 대기 시간은 최대 CANCEL_CONFIRM_POLL_ATTEMPTS x CANCEL_CONFIRM_POLL_INTERVAL_SEC 초.
        """
        ok = self._cancel_order(broker, order_id)
        if not ok:
            return "failed"
        if self.mode != "REAL" or not hasattr(broker, "get_order"):
            return "confirmed"
        for attempt in range(CANCEL_CONFIRM_POLL_ATTEMPTS):
            detail = broker.get_order(order_id)
            if detail is not None and detail.get("status") not in OPEN_ORDER_STATUSES:
                return "filled" if float(detail.get("filled_qty") or 0.0) > 0 else "confirmed"
            if attempt < CANCEL_CONFIRM_POLL_ATTEMPTS - 1 and CANCEL_CONFIRM_POLL_INTERVAL_SEC > 0:
                time.sleep(CANCEL_CONFIRM_POLL_INTERVAL_SEC)
        self.logger.warning(f"⏳ 주문({order_id}) 취소 요청 후 종료 확인이 안 됐습니다 (아직 PENDING_CANCEL 이거나 조회 불가).")
        return "unconfirmed"

    def _submit_real_market_exit(self, broker, ticker: str, qty: float, entry_price: float,
                                 current_price: float, vwap: float, target_price: float, signal_label: str) -> dict:
        """실거래 시장가 전량 청산(손절/패닉) 주문을 내고, 가능하면 체결 조회로 실제 체결가를 기록합니다.
        주문 거부(예: 취소가 아직 반영 안 돼 409 opposite-pending-order-exists)나 체결 0 종료 시
        MARKET_EXIT_MAX_ATTEMPTS 회까지 MARKET_EXIT_RETRY_INTERVAL_SEC 간격으로 남은 수량을 재시도합니다.

        Returns: {"result": ..., "order_id": 마지막 주문ID, "remaining": 미청산 수량}
            result:
              "filled"   — 전량 체결 확인(order_api)
              "pending"  — 주문은 접수됐고 아직 진행 중 → 추적 목록 등록, 다음 주기/재시작 시 정산
              "assumed"  — 주문은 접수됐으나 체결 조회 불가 → 봉 종가로 가정 기록(fill_source=assumed)
              "failed"   — 재시도까지 모두 실패(주문 거부/체결 0). 포지션 잔존 가능
        """
        remaining = float(qty)
        last_order_id = ""
        client_order_id = None
        reuse_key = False
        for attempt in range(1, MARKET_EXIT_MAX_ATTEMPTS + 1):
            if attempt > 1:
                # 재시도 전 실제 보유수량으로 보정 (취소 전 부분체결 등으로 보유가 줄었으면 초과 수량 주문은 거부되므로)
                actual = self._refresh_position_qty(broker, ticker)
                if actual is not None:
                    if actual <= 1e-9:
                        self.logger.info(f"ℹ️ 재시도 전 잔고 확인: {ticker} 보유 0주 — 청산 완료로 처리합니다.")
                        return {"result": "filled", "order_id": last_order_id, "remaining": 0.0}
                    if actual < remaining - 1e-9:
                        self.logger.info(f"ℹ️ 재시도 전 잔고 확인: 남은 수량 {remaining} -> 실제 보유 {actual} 로 보정합니다.")
                        remaining = actual
                        reuse_key = False  # 주문 본문(수량)이 바뀌면 같은 키 재사용 불가 (422 idempotency-key-conflict)

            # 멱등성 키: 직전 시도의 접수 여부가 '불명'(타임아웃/5xx/request-in-progress)이고 같은 수량일 때만 재사용.
            # 명시적 거부(예: 409 opposite-pending-order-exists) 뒤에는 새 키 사용
            # (거부 결과가 키에 묶여 10분간 재반환될 가능성을 피하기 위함).
            if not reuse_key or not client_order_id:
                client_order_id = f"vwap-{uuid.uuid4().hex[:24]}"
            try:
                order_id = broker.place_order(ticker, "SELL", 0.0, remaining, "MARKET", client_order_id=client_order_id)
                outcome = getattr(broker, "last_place_order_outcome", "ok" if order_id else "rejected")
            except Exception as e:
                self.logger.error(f"❌ 시장가 청산 주문 제출 중 예외: {e}")
                order_id, outcome = "", "unknown"
            reuse_key = (not order_id) and outcome == "unknown"

            if order_id:
                last_order_id = order_id
                info = {
                    "ticker": ticker, "side": "SELL", "price": 0.0, "qty": remaining, "order_type": "MARKET",
                    "entry_price": float(entry_price or 0.0), "vwap": round(float(vwap or 0.0), 4),
                    "target_price": round(float(target_price or 0.0), 4), "signal": signal_label,
                    "record_side": "STOP_LOSS", "submitted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    # (관측성) 시장가 청산의 슬리피지 기준가 = 청산 결정 시점 현재가(봉 종가)
                    "intended_price": round(float(current_price or 0.0), 4),
                    **self._order_meta(),
                }
                # 시장가 청산 주문 이벤트 (알림은 STOP_LOSS/PANIC 이벤트가 이미 보내므로 생략)
                self._emit("ORDER_PLACED", "warn", (self._cycle or {}).get("reason_code") or signal_label,
                           f"시장가 청산 주문 제출: {ticker} {remaining:g}주 ({signal_label}, 시도 {attempt}/{MARKET_EXIT_MAX_ATTEMPTS})",
                           self._reason_data(order_id=order_id, side="SELL", qty=remaining, order_type="MARKET",
                                             current_price=current_price, attempt=attempt),
                           notify=False)

                detail = None
                for poll in range(MARKET_FILL_POLL_ATTEMPTS):
                    detail = broker.get_order(order_id) if hasattr(broker, "get_order") else None
                    if detail is not None and detail.get("status") not in OPEN_ORDER_STATUSES:
                        break
                    if poll < MARKET_FILL_POLL_ATTEMPTS - 1 and MARKET_FILL_POLL_INTERVAL_SEC > 0:
                        time.sleep(MARKET_FILL_POLL_INTERVAL_SEC)

                if detail is None:
                    self.logger.warning(f"⚠️ 시장가 청산 주문({order_id}) 체결 조회 불가 → 봉 종가 {current_price:.2f}로 가정 기록합니다 (fill_source=assumed).")
                    if not self._record_real_fill(order_id, info, current_price, remaining, "assumed", "UNKNOWN", "current_price"):
                        self._track_order(order_id, ticker, "SELL", 0.0, remaining, order_type="MARKET", entry_price=entry_price,
                                          vwap=vwap, target_price=target_price, signal=signal_label, record_side="STOP_LOSS",
                                          intended_price=current_price)
                    return {"result": "assumed", "order_id": order_id, "remaining": 0.0}

                if detail.get("status") in OPEN_ORDER_STATUSES:
                    self.logger.warning(f"⏳ 시장가 청산 주문({order_id})이 아직 진행 중({detail.get('status')}) — 추적 목록에 등록하고 체결 확인 후 기록합니다.")
                    self._track_order(order_id, ticker, "SELL", 0.0, remaining, order_type="MARKET", entry_price=entry_price,
                                      vwap=vwap, target_price=target_price, signal=signal_label, record_side="STOP_LOSS",
                                          intended_price=current_price)
                    return {"result": "pending", "order_id": order_id, "remaining": remaining}

                filled_qty = float(detail.get("filled_qty") or 0.0)
                if filled_qty > 0:
                    avg_price = detail.get("avg_fill_price")
                    if avg_price is not None and avg_price > 0:
                        fill_price, basis = avg_price, "avg_fill_price"
                    else:
                        fill_price, basis = current_price, "current_price"
                    if not self._record_real_fill(order_id, info, fill_price, filled_qty, "order_api", detail.get("status"), basis,
                                                  commission=detail.get("commission"), filled_at=detail.get("filled_at")):
                        self._track_order(order_id, ticker, "SELL", 0.0, remaining, order_type="MARKET", entry_price=entry_price,
                                          vwap=vwap, target_price=target_price, signal=signal_label, record_side="STOP_LOSS",
                                          intended_price=current_price)
                    remaining = max(remaining - filled_qty, 0.0)
                if remaining <= 1e-9:
                    return {"result": "filled", "order_id": order_id, "remaining": 0.0}
                self.logger.warning(
                    f"⚠️ 시장가 청산 주문({order_id})이 {detail.get('status')} 로 종료(체결 {filled_qty}주). "
                    f"남은 {remaining}주 재시도 ({attempt}/{MARKET_EXIT_MAX_ATTEMPTS})"
                )
            else:
                self.logger.warning(
                    f"⚠️ 시장가 청산 주문 제출 실패 ({attempt}/{MARKET_EXIT_MAX_ATTEMPTS}, 결과: {outcome}) — "
                    f"{'같은 clientOrderId 로' if reuse_key else '새 clientOrderId 로'} 재시도합니다."
                )

            if attempt < MARKET_EXIT_MAX_ATTEMPTS and MARKET_EXIT_RETRY_INTERVAL_SEC > 0:
                time.sleep(MARKET_EXIT_RETRY_INTERVAL_SEC)

        return {"result": "failed", "order_id": last_order_id, "remaining": remaining}

    def _real_liquidate(self, broker, ticker: str, qty: float, entry_price: float, current_price: float,
                        vwap: float, target_price: float, signal_label: str, cancel_ids: list) -> dict:
        """실거래 전량 청산: 미체결 주문을 취소하고 종료를 확인한 뒤 시장가 매도(재시도 포함)."""
        for oid in cancel_ids:
            result = self._cancel_and_wait(broker, oid)
            if result in ("unconfirmed", "failed"):
                self.logger.warning(f"⚠️ 청산 전 주문({oid}) 취소 결과: {result} — 그대로 시장가 매도를 시도합니다(거부 시 재시도).")
            elif result == "filled":
                self.logger.info(f"ℹ️ 청산 전 주문({oid})이 취소 전에 (일부) 체결됐습니다 — 잔고를 다시 확인해 매도 수량을 보정합니다.")
        if cancel_ids:
            # 취소 확인 중 기존 SELL 이 부분/전량 체결됐을 수 있으므로 실제 보유수량으로 갱신 (조회 실패 시 기존 값 유지)
            actual = self._refresh_position_qty(broker, ticker)
            if actual is not None and abs(actual - float(qty)) > 1e-9:
                self.logger.info(f"ℹ️ 청산 수량 보정: 주기 시작 시 {qty}주 -> 현재 보유 {actual}주")
                qty = actual
        if qty <= 0:
            self.logger.info(f"ℹ️ {ticker} 보유 0주 — 시장가 청산이 필요 없습니다 (청산 완료로 처리).")
            return {"result": "filled", "order_id": "", "remaining": 0.0}
        return self._submit_real_market_exit(broker, ticker, qty, entry_price, current_price, vwap, target_price, signal_label)

    def _refresh_position_qty(self, broker, ticker: str):
        """잔고를 다시 조회해 해당 종목의 실제 보유수량을 반환합니다. 조회 실패 시 None."""
        try:
            bal = broker.get_balance()
        except Exception as e:
            self.logger.warning(f"⚠️ 잔고 재조회 실패: {e}")
            return None
        if not bal or bal.get("error") or "holdings" not in bal:
            return None
        return float((bal["holdings"].get(ticker) or {}).get("qty", 0.0))

    def _log_trade_file_warnings(self):
        """거래기록 파일 손상/쓰기 실패 알림(config_manager)을 이 봇의 로그에 ERROR 로 남깁니다."""
        try:
            for msg in VwapConfigManager.pop_trade_file_warnings(self.mode):
                self.logger.error(f"❌ [거래기록 파일] {msg}")
        except Exception:
            pass

    def _liquidation_cancel_ids(self, ticker: str, open_orders: list, open_orders_failed: bool) -> list:
        """청산 전에 취소할 주문 ID 목록. 미체결 조회가 실패했으면 추적 중인 해당 종목 주문으로 대신합니다."""
        if not open_orders_failed:
            return [o["order_id"] for o in open_orders]
        return [oid for oid, info in self.tracked_open_orders.items()
                if info.get("ticker") == ticker and info.get("order_type", "LIMIT") != "MARKET"]

    def _is_current_generation(self, gen) -> bool:
        return self.running and gen == self._generation

    def _run_loop(self, gen=None):
        """백그라운드 스레드에서 무한 루프로 실행되는 메인 봇 주기 실행부입니다.
        gen: 이 스레드의 세대 번호. start()가 다시 호출되면 세대가 바뀌어 이 루프는 스스로 종료됩니다."""
        if gen is None:
            gen = self._generation
        self.logger.info(f"[{self.mode} 봇] 루프 스레드가 기동되었습니다. (세대 {gen})")

        if self.mode == "REAL":
            with self._step_lock:
                if self._is_current_generation(gen):
                    self._load_tracked()

        while self._is_current_generation(gen):
            with self._step_lock:
                # 락을 기다리는 동안 stop/start 가 있었을 수 있으므로 재확인
                if not self._is_current_generation(gen):
                    break
                try:
                    self._loop_step()
                except Exception as e:
                    self.logger.error(f"❌ [{self.mode} 봇] 루프 실행 중 에러 발생: {e}")
                    self.logger.error(traceback.format_exc())
                finally:
                    # 가상 브로커 등에서 발생한 거래기록 파일 손상 알림도 봇 로그에 남김
                    self._log_trade_file_warnings()

            # 1분 단위로 주기적 실행
            for _ in range(60):
                if not self._is_current_generation(gen):
                    break
                time.sleep(1)

        self.logger.info(f"[{self.mode} 봇] 루프 스레드가 완전히 종료되었습니다. (세대 {gen})")

    def _loop_step(self):
        """한 주기 실행 + (관측성) 주기 종료 시 판단 사유 반영/SIGNAL_CHANGE/연속실패 처리.
        본체(_loop_step_body)의 동작과 예외 전파는 기존과 동일합니다.
        (3단계 §5.1) 본체의 주문·체결 판정과 _finish_cycle 이 모두 끝난 뒤 post-cycle 훅을 실행합니다(정상·예외 경로 모두).
        훅 실행부는 예외를 밖으로 내지 않으므로 본체 예외의 전파는 그대로입니다."""
        self._cycle = {"signal": None, "reason_code": None, "reason_text": "", "filters": None,
                       "values": {}, "config_snapshot": {}, "waiting_for_start": False,
                       "cycle_id": "", "hook": {}}
        try:
            self._loop_step_body()
        except Exception as e:
            self._finish_cycle(error=e)
            self._run_post_cycle_hooks()
            raise
        else:
            self._finish_cycle()
            self._run_post_cycle_hooks()

    # ------------------------------------------------------------------
    # (3단계 §5.1) post-cycle 훅 — 매매 판단·주문과 무관. 모든 예외·지연을 여기서 격리합니다.
    # ------------------------------------------------------------------
    def _hook_note(self, **kv):
        """이번 주기 훅 컨텍스트에 값 기록(본체에서 호출). 어떤 경우에도 예외를 내지 않습니다."""
        try:
            if self._cycle is not None:
                self._cycle.setdefault("hook", {}).update(kv)
        except Exception:
            pass

    @staticmethod
    def _candles_source_of(broker) -> str:
        """시세를 실제로 가져온 브로커의 last_candles_source. VirtualBroker 는 시세 원천(source_broker)을 봅니다."""
        try:
            src = getattr(broker, "last_candles_source", None)
            if src is None:
                src = getattr(getattr(broker, "source_broker", None), "last_candles_source", None)
            return str(src or "")
        except Exception:
            return ""

    def _untrusted_candles_cause(self, broker):
        """(ADR-0010, REAL 전용) 이번 주기 시세를 매매 판단에 쓰면 안 되는 이유. 써도 되면 None.
        Returns: None 또는 (cause, source, 설명)
          - "broker_mock_only": 토스 키 미설정 → 브로커가 시세 조회용 Mock (잔고는 가짜, 주문은 가짜 ID 만 돌려줌)
          - "candles_mock"    : 난수 봉 (키 미설정 + Yahoo 실패)
          - "candles_unknown" : 출처가 비었거나 알 수 없는 값 (출처를 남기지 않는 브로커 포함)"""
        src = self._candles_source_of(broker)
        if bool(getattr(broker, "is_mock_only", False)):
            return ("broker_mock_only", src, "토스 API 키 미설정(모의 브로커 — 잔고·주문이 가짜)")
        if src in TRUSTED_CANDLE_SOURCES:
            return None
        if src == "mock":
            return ("candles_mock", src, "난수(mock) 봉")
        return ("candles_unknown", src, f"출처 불명('{src}')" if src else "출처 불명(빈 값)")

    def _reset_untrusted_alert_state(self):
        """(ADR-0010 정정) 신뢰 불가 알림용 관측 상태 초기화 (생성자/start). 매매 판단에는 쓰이지 않음."""
        self._untrusted_episode_seq = 0          # 신뢰 불가 구간 일련번호 (Discord 중복 억제 키 분리용)
        self._untrusted_episode_started = None   # 현재 구간 첫 주기 시각(_monotonic)
        self._untrusted_critical_last = None     # 현재 구간 마지막 CRITICAL 시각
        self._untrusted_critical_count = 0       # 현재 구간 CRITICAL 횟수 (1=첫 알림, 2~=재알림)
        self._untrusted_alert_last = None        # 마지막 '구간 시작/요약' Discord 시각
        self._untrusted_alert_pending = 0        # 최소 간격 때문에 Discord 를 미룬 구간 시작 횟수
        self._untrusted_pending_since = None     # 미룬 첫 구간 시작 시각

    def _take_untrusted_pending_note(self) -> str:
        """미룬 구간 시작 횟수를 알림 문구로 꺼내고 0 으로 돌립니다 (없으면 빈 문자열)."""
        k = self._untrusted_alert_pending
        if k <= 0:
            return ""
        since = self._untrusted_pending_since
        self._untrusted_alert_pending = 0
        self._untrusted_pending_since = None
        span = f" (최근 {_fmt_duration(_monotonic() - since)})" if since is not None else ""
        return f" · 알림 간격 제한으로 생략된 신뢰 불가 구간 시작 {k}회{span}"

    def _flush_untrusted_summary(self):
        """(ADR-0010 정정) 신뢰 주기에서 호출: 미룬 구간 시작 알림이 있고 최소 간격이 지났으면 요약 1건을 보냅니다.
        짧은 구간(CRITICAL 전 회복)이 깜빡이다 멈춰도 사람에게 한 번은 전달되게 하는 장치입니다."""
        if self._untrusted_alert_pending <= 0:
            return
        now = _monotonic()
        if self._untrusted_alert_last is not None and now - self._untrusted_alert_last < UNTRUSTED_EPISODE_ALERT_MIN_SEC:
            return
        k = self._untrusted_alert_pending
        note = self._take_untrusted_pending_note()
        self._untrusted_alert_last = now
        self._emit("ERROR", "warn", "DATA_UNTRUSTED",
                   f"REAL 시세 신뢰 불가 구간 요약 — 현재는 신뢰 시세로 정상 판단 중{note}",
                   {"summary": True, "deferred_episodes": k, "episode": self._untrusted_episode_seq},
                   notify=True, dedup_extra=("untrusted_summary", self._untrusted_episode_seq))

    def _on_untrusted_candles(self, ticker: str, cause: str, src: str, desc: str):
        """(ADR-0010) 신뢰 불가 시세 주기 처리. 매매/주문 호출은 하지 않습니다.
        알림 정책(2026-10-07 정정, M1/M2):
          - 구간(연속 구간) 시작 주기: warn 이벤트를 반드시 기록(키 억제 무시). Discord 는 직전 구간 시작/요약 알림 후
            UNTRUSTED_EPISODE_ALERT_MIN_SEC 가 지났으면 즉시, 아니면 미뤄서 다음 알림에 횟수로 묶음
          - 같은 구간의 이후 주기: 같은 키 warn 이벤트 10분 1회(ERROR_EVENT_REPEAT_SEC), Discord 없음
          - 연속 UNTRUSTED_STREAK_CRITICAL 주기째 CRITICAL 1회, 이후 계속되면 UNTRUSTED_CRITICAL_REPEAT_SEC 마다 재알림
            (지속 시간·마지막 신뢰 보유 수량·원인 포함)"""
        now = _monotonic()
        self._untrusted_streak += 1
        n = self._untrusted_streak
        if n == 1:
            self._untrusted_episode_seq += 1
            self._untrusted_episode_started = now
            self._untrusted_critical_last = None
            self._untrusted_critical_count = 0
        seq = self._untrusted_episode_seq
        started = self._untrusted_episode_started if self._untrusted_episode_started is not None else now
        duration = now - started
        last_qty = self._last_trusted_position_qty  # 신뢰 주기에서 마지막으로 확인한 보유 수량 (없으면 None)
        qty_note = f"마지막으로 확인된 보유 {last_qty:g}주" if last_qty is not None else "이번 가동 중 보유 수량 확인 이력 없음"
        text = (f"{ticker} 시세 신뢰 불가 — {desc} → 이번 주기 매매 판단 전체 보류 "
                f"(신규 매수·지정가 매도/정정·손절·손실한도 판정 안 함, 기존 미체결 유지) [연속 {n}주기]")
        self._set_reason("DATA_UNTRUSTED", text, "WAIT")
        self.logger.error(f"🧪 [REAL 보호] {text}")
        data = self._reason_data(ticker=ticker, candles_source=src or "", cause=cause, streak=n)
        data["last_known_position_qty"] = last_qty
        data["episode"] = seq
        data["duration_sec"] = round(duration, 1)

        key = f"DATA_UNTRUSTED:{cause}:{src}"
        if n == 1:
            # M1: 새 구간의 첫 경고는 이전 구간의 10분 키 억제에 걸리지 않게 직접 기록
            self._error_last[key] = now
            throttled = (self._untrusted_alert_last is not None
                         and now - self._untrusted_alert_last < UNTRUSTED_EPISODE_ALERT_MIN_SEC)
            msg = f"REAL 시세 신뢰 불가({desc}) — 매매 판단 보류 (새 신뢰 불가 구간 시작)"
            start_data = dict(data)
            if throttled:
                if self._untrusted_alert_pending == 0:
                    self._untrusted_pending_since = now
                self._untrusted_alert_pending += 1
                start_data["discord_deferred"] = True
            else:
                msg += self._take_untrusted_pending_note()
                self._untrusted_alert_last = now
            self._emit("ERROR", "warn", "DATA_UNTRUSTED", msg, start_data,
                       notify=not throttled, dedup_extra=("untrusted_start", seq))
        else:
            self._emit_error(key, f"REAL 시세 신뢰 불가({desc}) — 매매 판단 보류 (구간 지속, {_fmt_duration(duration)})",
                             data, level="warn", reason_code="DATA_UNTRUSTED", notify=False)

        # M2: CRITICAL 첫 알림 + 지속 시 재알림
        first_crit = (n == UNTRUSTED_STREAK_CRITICAL)
        repeat_crit = (n > UNTRUSTED_STREAK_CRITICAL and self._untrusted_critical_last is not None
                       and now - self._untrusted_critical_last >= UNTRUSTED_CRITICAL_REPEAT_SEC)
        if first_crit or repeat_crit:
            self._untrusted_critical_count += 1
            self._untrusted_critical_last = now
            k = self._untrusted_critical_count
            crit_data = dict(data)
            crit_data["critical_seq"] = k
            if first_crit:
                head = f"{UNTRUSTED_STREAK_CRITICAL}주기 연속 시세 신뢰 불가({desc}, {_fmt_duration(duration)} 경과)"
            else:
                head = (f"[재알림 {k - 1}회째] 시세 신뢰 불가 {_fmt_duration(duration)} 지속"
                        f"(연속 {n}주기, 원인: {desc})")
            self._emit("CRITICAL", "critical", "DATA_UNTRUSTED",
                       f"{head} — 손절 판정이 멈춰 있습니다. "
                       f"보유 포지션이 있으면 토스 앱에서 직접 확인하세요 ({qty_note})"
                       f"{self._take_untrusted_pending_note()}",
                       crit_data, notify=True, dedup_extra=("untrusted_critical", seq, k))

    def _build_hook_ctx(self) -> dict:
        """훅에 넘길 이번 주기 컨텍스트(공통 원본). 훅마다 _run_post_cycle_hooks 가 복사본을 만듭니다."""
        c = self._cycle or {}
        h = c.get("hook") or {}
        cfg = h.get("config")
        if isinstance(cfg, dict):
            cfg = dict(cfg)
            for k in HOOK_SECRET_KEYS:
                if k in cfg:
                    cfg[k] = ""
        else:
            cfg = {}
        pos, cash = None, None
        try:
            if h.get("position_raw") is not None:
                q, ep = h["position_raw"]
                pos = {"qty": float(q or 0.0), "entry_price": float(ep or 0.0)}
            if h.get("cash_raw") is not None:
                cash = float(h["cash_raw"])
        except Exception:
            pos, cash = None, None
        return {
            "mode": self.mode,
            "generation": self._generation,
            "cycle_id": c.get("cycle_id") or "",
            "df": h.get("df"),                        # 이번 주기 캔들 원본 (calculate_vwap 이전)
            "ticker": h.get("ticker"),
            "market": h.get("market"),
            "interval": h.get("interval"),
            "reset_time": h.get("reset_time"),
            "candles_source": h.get("candles_source") or "",
            "candles_asof": h.get("candles_asof"),    # 캔들 요청 직전 시각 (이 시각 이전에 끝난 봉은 '마감'이 확실)
            "config": cfg,                            # 이번 주기 설정 dict(평탄 키, 비밀값은 빈 문자열)
            "position": pos,                          # {"qty","entry_price"} | None (잔고 조회 실패/전)
            "cash": cash,                             # float | None
            "reason_code": c.get("reason_code") or "",
            "running": bool(self.running),
        }

    def _run_post_cycle_hooks(self):
        """등록된 훅을 순서대로 동기 실행. 훅마다 ctx/df/config 를 복사해 넘기고, 예외·지연은 ERROR(warn) 이벤트로만 남깁니다.
        이 함수는 절대 예외를 밖으로 던지지 않습니다."""
        try:
            hooks = list(self._post_cycle_hooks)
            if not hooks:
                return
            base = self._build_hook_ctx()
        except Exception as e:
            try:
                self.logger.warning(f"[훅] 컨텍스트 생성 실패(무시): {e}")
            except Exception:
                pass
            return
        durations = {}
        for name, fn in hooks:
            t0 = time.monotonic()
            try:
                ctx = dict(base)
                df = base.get("df")
                ctx["df"] = df.copy() if df is not None and hasattr(df, "copy") else None
                ctx["config"] = copy.deepcopy(base.get("config") or {})
                ctx["position"] = dict(base["position"]) if isinstance(base.get("position"), dict) else None
                fn(ctx)
            except Exception as e:
                try:
                    self.logger.warning(f"[훅] '{name}' 실행 중 예외(무시, 매매 영향 없음): {type(e).__name__}: {e}")
                    self._emit_error(f"HOOK_ERROR:{name}:{type(e).__name__}",
                                     f"주기 후처리 훅 '{name}' 예외(매매 영향 없음): {type(e).__name__}: {str(e)[:150]}",
                                     {"hook": name, "exception": type(e).__name__}, reason_code="HOOK_ERROR")
                except Exception:
                    pass
            elapsed = time.monotonic() - t0
            durations[name] = round(elapsed * 1000.0, 1)
            if elapsed > HOOK_WARN_SEC:
                try:
                    self._emit_error(f"HOOK_SLOW:{name}",
                                     f"주기 후처리 훅 '{name}' 소요 {elapsed:.2f}초 > {HOOK_WARN_SEC:g}초 (루프 주기 지연)",
                                     {"hook": name, "elapsed_sec": round(elapsed, 3), "limit_sec": HOOK_WARN_SEC},
                                     reason_code="HOOK_SLOW")
                except Exception:
                    pass
        self.last_hook_durations_ms = durations

    def _loop_step_body(self):
        """한 주기의 전략 계산 및 주문 정정 작업을 수행합니다."""
        # 1. 설정 실시간 로드
        config = self._load_config()

        mode = self.mode
        if mode == "VIRTUAL":
            mode = "VIRTUAL_1"
        mode_prefix = mode.lower()
        self._refresh_notify_flag(config)
        
        ticker = config[f"{mode_prefix}_ticker"]
        market = config[f"{mode_prefix}_market"]
        interval = config[f"{mode_prefix}_interval"]
        n_percent = float(config[f"{mode_prefix}_n_percent"])
        m_percent = float(config[f"{mode_prefix}_m_percent"])
        x_percent = float(config[f"{mode_prefix}_x_percent"])
        k_percent = float(config[f"{mode_prefix}_k_percent"])
        reset_time = config[f"{mode_prefix}_reset_time"]
        # (ADR-0008) 세션 경계의 단일 기준. 미국 종목 + 리셋 22:30/23:30 → 서머타임에 맞춰 22:30↔23:30 자동,
        # 그 외(국내 종목, 다른 리셋 시각) → 설정한 reset_time 고정. 어느 쪽이든 자정으로는 세션을 끊지 않음.
        session = SessionSpec.for_market(market, reset_time, ticker)
        now = self._now()
        # (3단계 §5.1, 관측 전용) 훅 컨텍스트 — 매매 판단에는 쓰지 않음
        self._hook_note(config=config, ticker=ticker, market=market, interval=interval, reset_time=reset_time)
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

        # (관측성) 주문 당시 설정 요약 — 거래 레코드 config_snapshot 으로 남김 (비밀값 제외)
        self._cycle["config_snapshot"] = {
            "ticker": ticker, "interval": interval, "n_percent": n_percent, "m_percent": m_percent,
            "x_percent": x_percent, "k_percent": k_percent, "use_vwap_band": use_vwap_band,
            "vwap_band_sigma": vwap_band_sigma, "use_adx_filter": use_adx_filter, "adx_threshold": adx_threshold,
            "use_rsi_filter": use_rsi_filter, "rsi_threshold": rsi_threshold, "reset_time": reset_time,
            "session_rule": session.mode, "session_start": session.session_start(now).strftime("%Y-%m-%d %H:%M"),
            "start_time": start_time, "max_daily_loss_limit": max_daily_loss_limit, "initial_balance": initial_balance,
        }

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
        # (관측성) 가상 체결 기록 시 FILL 이벤트를 남기도록 콜백 연결
        if self._is_virtual(mode) and self.virtual_broker is not None:
            self.virtual_broker.on_trade = self._on_virtual_trade

        self.logger.info(f"▶ [{mode} 모드] {ticker} ({market}) 전략 분석 주기 시작...")

        # 3. 최신 캔들 수집
        # (ADR-0008) 현재 세션 시작부터의 봉 + 여유(최소 기존 150봉, 최대 1600봉). 200봉 초과는 TossBroker 가 페이징.
        # 예전에는 항상 150봉만 받아, 세션이 2.5시간(1분봉)을 넘으면 VWAP 이 '최근 150봉 누적'으로 잘려 있었음.
        candle_count = session.bars_needed(now, interval)
        df = broker.get_candles(ticker, interval, candle_count)
        # (3단계 §5.1, 관측 전용) 캔들 원본·출처·요청 직전 시각(now)을 훅 컨텍스트에 기록. 매매 판단에는 쓰지 않음
        self._hook_note(df=df, candles_source=self._candles_source_of(broker), candles_asof=now)
        if df.empty:
            self.logger.error(f"[{ticker}] 캔들 데이터를 가져오지 못했습니다. 다음 주기에 재시도합니다.")
            self._set_reason("DATA_UNAVAILABLE", f"{ticker} 캔들(시세) 조회 실패 → 이번 주기 판단 보류", "WAIT")
            return

        # 3-1. (ADR-0010) REAL 보호: 캔들 출처가 "toss"/"yahoo" 가 아니거나(난수 봉·출처 불명) 브로커가 키 미설정 Mock 이면
        # 이 주기에는 아무 매매 판단도 하지 않습니다 — 신규 매수, 지정가 매도/정정, 미체결 정리, 손절, 손실한도(패닉) 전부.
        # 손절도 보류하는 이유: 난수 봉 가격은 실제 시세와 무관해, 그 값으로 손절가 이탈을 판정하면 실제 포지션을
        # 근거 없이 시장가로 청산할 수 있음. 대신 연속 N주기면 CRITICAL 로 사람에게 알림. 가상 봇은 이 검사를 하지 않음.
        if mode == "REAL":
            untrusted = self._untrusted_candles_cause(broker)
            if untrusted is not None:
                self._on_untrusted_candles(ticker, *untrusted)
                return

        # 4. 실시간 VWAP 계산
        df = VwapStrategy.calculate_vwap(df, reset_time, session=session)
        latest_row = df.iloc[-1]
        # (3단계 §5.1) cycle_id = 이번 주기 마지막 봉 시각. 주문 메타(_order_meta)·이벤트에 남아 섀도우와 같은 봉을 짝짓는 키
        try:
            self._cycle["cycle_id"] = latest_row['time'].strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                self._cycle["cycle_id"] = str(latest_row['time'])[:19]
            except Exception:
                pass
        # (관측 전용, 매매 판단 무관) 이번 VWAP 이 세션 시작부터의 봉을 모두 포함하는지.
        # 여유분(+30봉)을 더 요청하므로 정상이면 받은 첫 봉이 세션 시작 이전. 그렇지 않으면 페이징 실패 등으로 잘린 것.
        vwap_full_session = bool(df['time'].iloc[0] <= latest_row['session_start'])
        if not vwap_full_session and self._vwap_partial_warned != str(latest_row['session_start']):
            self._vwap_partial_warned = str(latest_row['session_start'])
            self.logger.warning(f"⚠️ VWAP 이 세션 시작({latest_row['session_start']})부터의 봉을 다 포함하지 못함 "
                                f"(요청 {candle_count}봉, 수신 {len(df)}봉, 첫 봉 {df['time'].iloc[0]}) — 이번 세션 VWAP 은 부분 누적값")
        current_price = float(latest_row['close'])
        high = float(latest_row['high'])
        low = float(latest_row['low'])
        self._cycle["values"] = {"ticker": ticker, "current_price": round(current_price, 2),
                                 "vwap": round(float(latest_row.get('vwap', 0.0)), 2)}

        # 5. 가상 브로커인 경우, 먼저 가상 주문 매칭 엔진 업데이트 수행
        # (가상 봇은 "VIRTUAL_1/2/3" 으로 생성되므로 접두어로 판정)
        if self._is_virtual(mode):
            self.virtual_broker.update_simulation(ticker, current_price, high, low)

        # 6. 자산 및 포지션 조회
        balance = broker.get_balance()
        if balance is None or "cash" not in balance or "holdings" not in balance or balance.get("error"):
            self.logger.error(f"⚠️ [{ticker}] 자산 정보를 조회하지 못했습니다. 일시적인 API 에러일 수 있으므로 다음 주기에 재시도합니다.")
            self._set_reason("DATA_UNAVAILABLE", f"{ticker} 잔고/보유 조회 실패 → 이번 주기 판단 보류", "WAIT")
            return

        cash = balance["cash"]
        holdings = balance["holdings"]

        # 포지션 정보 파싱
        holding_info = holdings.get(ticker, {"qty": 0.0, "entry_price": 0.0})
        qty = holding_info["qty"]
        entry_price = holding_info["entry_price"]
        try:
            self._last_trusted_position_qty = float(qty)  # (ADR-0010, 관측 전용) 신뢰 불가 CRITICAL 알림 문구용
        except Exception:
            pass
        self._cycle["values"].update({"position_qty": float(qty),
                                      "entry_price": round(float(entry_price), 2) if qty > 0 else 0.0})
        self._hook_note(position_raw=(qty, entry_price), cash_raw=cash)  # (관측 전용) 변환은 훅 실행부에서

        # 미체결 매수 주문에 묶인 거래 대기 금액 계산 (가상/실제 공통 적용)
        # 이 주기의 미체결 목록은 여기서 한 번만 조회해 패닉/체결판정/주문집행에 재사용합니다.
        open_orders = broker.get_open_orders(ticker)
        open_orders_failed = (mode == "REAL") and bool(getattr(broker, "last_open_orders_failed", False))
        pending_buy_value = sum(float(o["price"]) * float(o["qty"]) for o in open_orders if o["side"] == "BUY")

        # 당일 손실 한도(Panic Stop) 안전장치 검사
        stock_value = qty * current_price
        # 기준일은 달력 날짜(KST 자정)가 아니라 VWAP reset_time 기준 '세션 날짜'
        # (미국장 22:30 리셋이면 자정을 넘겨도 같은 세션으로 취급)
        now_date = session.session_key(now)

        # 손실률 계산의 기준 자산(equity) 산정
        if self._is_virtual(mode):
            # 가상: 가상 장부(현금은 봇 전용 initial_balance 기반) 자체가 봇 전용 자산
            total_asset = cash + stock_value + pending_buy_value
            equity_basis = "가상장부"
        elif initial_balance > 0.0:
            # 실거래: 계좌 전체 현금이 아니라 '봇 기준 자본금 + 봇 실현손익 + 봇 종목 평가손익'으로 계산.
            # (계좌 현금이 크면 봇 손실이 희석돼 한도가 사실상 발동하지 않던 문제 해결)
            realized_pnl = sum(float(t.get("pnl", 0.0) or 0.0) for t in VwapConfigManager.load_trades("REAL"))
            unrealized_pnl = (current_price - entry_price) * qty if (qty > 0 and entry_price > 0) else 0.0
            total_asset = initial_balance + realized_pnl + unrealized_pnl
            equity_basis = "봇자본금"
        else:
            # 실거래인데 기준 자본금 미설정: 기존처럼 계좌 기준(희석될 수 있음)
            total_asset = cash + stock_value + pending_buy_value
            equity_basis = "계좌전체"

        # 만약 기준 자산이 0 이하인 경우(통신 장애 또는 환전 전 등),
        # 당일 기준 자산 설정 및 손실 감지(Panic Stop) 로직을 안전하게 건너뜁니다.
        if total_asset <= 0.0:
            self.logger.warning(f"⚠️ [{ticker}] 손실한도 기준 자산({equity_basis})이 0 이하입니다. 일시적인 API 장애 또는 환전 대기 상태일 수 있으므로 손실 한도 검사를 건너뜁니다.")
        else:
            # baseline 자산이 미설정되었거나 세션 날짜가 바뀌었을 때 갱신
            if self.daily_baseline_asset <= 0.0 or self.last_baseline_date != now_date:
                self.daily_baseline_asset = total_asset
                self.last_baseline_date = now_date
                self.logger.info(f"🎯 세션 기준 자산(Baseline, {equity_basis})이 설정되었습니다: {self.daily_baseline_asset:.2f} (세션: {now_date}, 기준: {session.describe()})")

            # 손실 감지 시 강제 청산
            if self.daily_baseline_asset > 0.0:
                loss_amount = self.daily_baseline_asset - total_asset
                loss_rate = (loss_amount / self.daily_baseline_asset) * 100.0

                if loss_rate >= max_daily_loss_limit:
                    self.logger.error(f"🚨🚨 [당일 손실 한도 초과] 세션 기준 자산({self.daily_baseline_asset:.2f}, {equity_basis}) 대비 손실률 {loss_rate:.2f}% 발생! (한도: {max_daily_loss_limit:.2f}%)")
                    self.logger.error(f"🚨 즉각 모든 미체결 주문 취소 및 보유 주식 전량 시장가 매도(Panic Sell & Stop)를 감행하고 봇을 강제 정지합니다.")
                    panic_text = (f"세션 기준자산 {self.daily_baseline_asset:.2f} 대비 손실률 {loss_rate:.2f}% ≥ 한도 "
                                  f"{max_daily_loss_limit:.2f}% → 전량 청산 후 봇 정지")
                    self._set_reason("DAILY_LOSS_STOP", panic_text, "STOP_LOSS")
                    self._emit("PANIC", "critical", "DAILY_LOSS_STOP", "당일 손실 한도 초과 — 패닉 청산 및 봇 정지",
                               self._reason_data(loss_rate=round(loss_rate, 2), limit=max_daily_loss_limit,
                                                 baseline=round(self.daily_baseline_asset, 2),
                                                 total_asset=round(total_asset, 2), equity_basis=equity_basis,
                                                 qty=float(qty), open_orders=len(open_orders)))

                    if self._is_virtual(mode):
                        # 미체결 가상 주문 전체 취소 후 가상 강제청산
                        for order in open_orders:
                            self._cancel_order(broker, order["order_id"])
                        if qty > 0:
                            self.virtual_broker.force_market_stop_loss(ticker, current_price, meta=self._order_meta())
                    else:
                        # 미체결 취소 -> 취소 종료 확인 -> 시장가 전량 매도(거부 시 재시도)
                        cancel_ids = self._liquidation_cancel_ids(ticker, open_orders, open_orders_failed)
                        res = self._real_liquidate(broker, ticker, qty, entry_price, current_price, 0.0, 0.0,
                                                   "PANIC_STOP", cancel_ids)
                        if res["result"] == "failed":
                            self.logger.critical(
                                f"🚨🚨🚨 [CRITICAL] 패닉스탑 시장가 청산이 {MARKET_EXIT_MAX_ATTEMPTS}회 재시도 후에도 실패했습니다! "
                                f"{ticker} 미청산 {res['remaining']}주가 남아있습니다. 봇은 정지하므로 토스 앱에서 즉시 수동 청산하세요. "
                                f"(마지막 주문 ID: {res['order_id'] or '없음'})"
                            )
                            self._emit("CRITICAL", "critical", "DAILY_LOSS_STOP",
                                       f"패닉 청산 {MARKET_EXIT_MAX_ATTEMPTS}회 재시도 실패 — {ticker} 미청산 {res['remaining']:g}주, 즉시 수동 청산 필요",
                                       self._reason_data(remaining=res["remaining"], last_order_id=res["order_id"] or ""))
                        else:
                            self.logger.info(f"🚨 패닉스탑 청산 처리 결과: {res['result']} (ID: {res['order_id']}, 미청산 {res['remaining']}주)")

                    self.running = False
                    self._stop_note = f"당일 손실 한도 초과로 자동 정지: {panic_text}"
                    self.logger.error("🛑 당일 손실 한도 초과로 인해 봇 백그라운드 엔진이 정지(STOP)되었습니다.")
                    self._emit("BOT_STOP", "error", "DAILY_LOSS_STOP", f"{self.mode} 봇 자동 정지 (당일 손실 한도 초과)",
                               self._reason_data(by="daily_loss_limit"))
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
        # (관측성) 전략 단계 판단 사유 — 아래에서 봇 단계 사유(WAIT_START_TIME/BUDGET_SHORT 등)가 덮어쓸 수 있음
        self._cycle["filters"] = signals.get("filters")
        self._cycle["values"].update({
            "vwap": round(float(vwap), 2), "adx": signals.get("adx", 0.0), "rsi": signals.get("rsi", 50.0),
            "target_buy_price": round(float(target_buy_price), 2), "target_sell_price": round(float(target_sell_price), 2),
            "stop_loss_price": round(float(stop_loss_price), 2),
        })
        self._set_reason(signals.get("reason_code") or "", signals.get("reason_text") or "", signal)

        # 7-1. 거래 시작 시간(Start Time) 체크를 통한 대기 로직 적용
        is_waiting_for_start = False
        sess_start = session.session_start(now)
        if start_time and start_time != reset_time and start_time != sess_start.strftime("%H:%M"):
            try:
                # HH:MM 형식 검증 및 파싱
                start_h, start_m = map(int, start_time.split(':'))

                dt_now = now
                # 최근 세션 시작 시각 (T_reset) — 손실한도 세션 날짜와 같은 SessionSpec 기준
                t_reset = sess_start

                # 최근 세션 시작에 대응하는 거래 시작 시각 (T_start)
                t_start_temp = t_reset.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                if t_start_temp >= t_reset:
                    t_start = t_start_temp
                elif session.is_us_auto and t_reset - t_start_temp <= timedelta(hours=1):
                    # (ADR-0008) 서머타임 종료로 개장이 22:30→23:30 으로 늦어져 시작 시각(예: 23:00)이
                    # 개장보다 '최대 1시간' 앞서게 된 경우: 다음날로 넘기면 하루 가까이 대기하므로 '대기 없음'으로 처리
                    t_start = t_reset
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
            self._cycle["waiting_for_start"] = True
        # (ADR-0009) 대기 중 막는 것은 '신규 진입'뿐. 보유 포지션의 손절(STOP_LOSS)·매도(SELL)·유지(HOLD)는 평소대로 처리.
        # (이전에는 보유 중 STOP_LOSS 까지 WAIT 로 덮어, 전 세션에서 넘어온 포지션이 대기 구간 동안 손절 보호를 못 받았음)
        # 조건은 core/vwap/strategies/rules.start_wait_blocks 와 동치 (T-P5 가 검증).
        if is_waiting_for_start and (qty <= 0 or signal == "BUY"):
            signal = "WAIT"
            self.logger.info(f"⏳ 대기 시간대이므로 전략 시그널을 WAIT로 강제하고 신규 매매를 보류합니다.")
            self._set_reason("WAIT_START_TIME",
                             f"거래 시작 시각 {start_time} 이전(세션 시작 {sess_start.strftime('%H:%M')}) → 신규 매매 대기 "
                             f"(전략 판단: {signals.get('reason_code') or '-'})", "WAIT")
        elif is_waiting_for_start:
            self.logger.info(f"⏳ 거래 시작 대기 중이지만 보유 포지션({qty}주) 관리는 계속합니다 (시그널 {signal} 그대로 처리, 신규 매수만 보류).")

        # (ADR-0008, 관측 전용) 대시보드/상태 API 에 현재 VWAP 세션 기준을 노출. 매매 판단에는 쓰지 않음.
        session_label = f"{session.describe()} {session.describe_bounds(now)}"
        self._cycle["values"].update({"session_label": session_label, "vwap_full_session": vwap_full_session})

        self.logger.info(f"현재가: {current_price:.2f} | VWAP: {vwap:.2f} | 시그널: {signal}")
        if qty > 0:
            self.logger.info(f"보유량: {qty}주 | 평단가: {entry_price:.2f} | 청산타겟: {target_sell_price:.2f} | 손절가: {stop_loss_price:.2f}")
        else:
            self.logger.info(f"진입타겟: {target_buy_price:.2f}")

        # 8. 실거래 체결 이력 트래킹 (미체결 목록은 위에서 조회한 open_orders 재사용)
        if mode == "REAL" and open_orders_failed:
            # 미체결 조회 자체가 실패했으면(빈 목록 반환) 모든 추적 주문이 '사라진' 것으로 오인되고,
            # 기존 주문이 없는 줄 알고 중복 주문을 낼 수 있으므로 체결 판정과 '신규 지정가 주문'은 보류합니다.
            # 단, 손절(STOP_LOSS)은 포지션 보호가 우선이므로 아래에서 그대로 진행합니다.
            self.logger.error(f"⚠️ [{ticker}] 미체결 주문 조회에 실패했습니다. 체결 판정과 신규 지정가 주문은 이번 주기에 보류합니다 (손절은 진행).")
        elif mode == "REAL":
            # 8-1. 추적 주문 중 미체결 목록에서 사라진 것의 실제 결과(체결/부분체결/취소) 판정
            self._reconcile_real_orders(broker, open_orders, holdings, current_price)

            # 8-2. 현재 오픈 주문 중 추적 리스트에 없는 항목 신규 등록 (재시작 직후, 앱에서 직접 낸 주문 등)
            for o in open_orders:
                if o["order_id"] not in self.tracked_open_orders:
                    self._track_order(o["order_id"], o["ticker"], o["side"], o["price"], o["qty"],
                                      order_type="LIMIT", entry_price=entry_price, vwap=vwap,
                                      target_price=0.0, signal="ADOPTED",
                                      meta={"reason_code": "ADOPTED", "filters": {}, "config_snapshot": {}})

        # 9. 주문 집행 및 Cancel & Replace 정정 메커니즘

        # 9-1. 손절 조건 판정 (STOP_LOSS)
        if signal == "STOP_LOSS":
            self.logger.warning(f"🚨 손절 기준선({stop_loss_price:.2f}) 하향 이탈! 즉시 시장가 청산을 시도합니다.")
            self._emit("STOP_LOSS", "warn", "STOP_LOSS", f"손절 발동 — {ticker} {qty:g}주 시장가 청산 시도",
                       self._reason_data(qty=float(qty), entry_price=round(float(entry_price), 2)))
            if self._is_virtual(mode):
                # VirtualBroker는 MARKET 주문 체결을 지원하지 않으므로 전용 강제청산 경로 사용
                self.virtual_broker.force_market_stop_loss(ticker, current_price, meta=self._order_meta())
            else:
                # 미체결 취소 -> 취소 종료 확인 -> 시장가 전량 매도(거부 시 재시도, 가능하면 실제 체결가로 기록)
                cancel_ids = self._liquidation_cancel_ids(ticker, open_orders, open_orders_failed)
                res = self._real_liquidate(broker, ticker, qty, entry_price, current_price, vwap, stop_loss_price,
                                           "STOP_LOSS", cancel_ids)
                if res["result"] == "failed":
                    self.logger.error(
                        f"❌🚨 시장가 손절이 {MARKET_EXIT_MAX_ATTEMPTS}회 재시도 후에도 실패했습니다! {ticker} 미청산 {res['remaining']}주. "
                        f"다음 주기에 다시 시도하지만 즉시 수동 확인을 권장합니다."
                    )
                    self._emit("CRITICAL", "critical", "STOP_LOSS",
                               f"시장가 손절 {MARKET_EXIT_MAX_ATTEMPTS}회 재시도 실패 — {ticker} 미청산 {res['remaining']:g}주 (다음 주기 재시도, 수동 확인 권장)",
                               self._reason_data(remaining=res["remaining"], last_order_id=res["order_id"] or ""))
                else:
                    self.logger.info(f"시장가 손절 처리 결과: {res['result']} (ID: {res['order_id']})")
            return

        # 미체결 조회 실패 주기에는 신규/정정 지정가 주문을 내지 않음 (중복 주문 방지)
        if open_orders_failed:
            self._set_reason("DATA_UNAVAILABLE",
                             f"미체결 주문 조회 실패 → 이번 주기 신규/정정 주문 보류 (전략 판단: {self._cycle.get('reason_code') or '-'})")
            return

        # 9-2-0. (ADR-0009) 거래 시작 대기 중 보유 포지션을 매도(SELL)하는 주기에는, 남아 있는 매수 미체결(부분 체결 잔량 등)이
        # 체결되면 대기 중 '신규 진입'이 되므로 먼저 취소합니다. (HOLD/WAIT 는 9-4, STOP_LOSS 는 청산 경로가 이미 전부 취소)
        if is_waiting_for_start and signal == "SELL":
            for order in [o for o in open_orders if o["side"] == "BUY"]:
                ok = self._cancel_order(broker, order["order_id"])
                self._emit("ORDER_CANCELED", "info" if ok else "warn", "WAIT_START_TIME",
                           f"거래 시작 대기 중 잔여 매수 주문 취소{'' if ok else ' 요청 실패'}: "
                           f"{float(order.get('price') or 0):.2f} x {float(order.get('qty') or 0):g}주",
                           self._reason_data(order_id=order.get("order_id"), side="BUY",
                                             price=order.get("price"), qty=order.get("qty"), cancel_ok=bool(ok)))

        # 9-2. 매도 청산 시그널 (SELL)
        if signal == "SELL":
            sell_orders = [o for o in open_orders if o["side"] == "SELL"]
            
            if sell_orders:
                existing_order = sell_orders[0]
                if abs(existing_order["price"] - target_sell_price) > 0.01:
                    self.logger.info(f"🔄 VWAP 변동 감지! 매도 주문 정정 수행: {existing_order['price']:.2f} -> {target_sell_price:.2f}")
                    cancel_result = self._cancel_and_wait(broker, existing_order["order_id"])
                    if cancel_result != "confirmed":
                        self.logger.info(f"기존 매도 주문 취소 결과 '{cancel_result}' — 이번 주기에는 재주문하지 않습니다.")
                        self._on_replace_held(existing_order, cancel_result)
                    else:
                        new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                        if new_id:
                            self.logger.info(f"매도 정정 주문 제출 완료. (ID: {new_id})")
                            self._track_order(new_id, ticker, "SELL", target_sell_price, qty, entry_price=entry_price,
                                              vwap=vwap, target_price=target_sell_price, signal=signal)
                            self._on_order_placed(broker, "ORDER_REPLACED", "SELL", new_id, target_sell_price, qty,
                                                  old_price=existing_order["price"], old_qty=existing_order["qty"])
                        else:
                            self._on_order_place_failed("SELL", target_sell_price, qty)
                else:
                    self.logger.info("기존 매도 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
            else:
                self.logger.info(f"📉 청산 조건 충족. 지정가 매도 주문 제출: {target_sell_price:.2f} @ {qty}주")
                new_id = broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
                if new_id:
                    self.logger.info(f"매도 지정가 주문 제출 성공. (ID: {new_id})")
                    self._track_order(new_id, ticker, "SELL", target_sell_price, qty, entry_price=entry_price,
                                      vwap=vwap, target_price=target_sell_price, signal=signal)
                    self._on_order_placed(broker, "ORDER_PLACED", "SELL", new_id, target_sell_price, qty)
                else:
                    self._on_order_place_failed("SELL", target_sell_price, qty)

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
                        cancel_result = self._cancel_and_wait(broker, existing_order["order_id"])
                        if cancel_result != "confirmed":
                            self.logger.info(f"기존 매수 주문 취소 결과 '{cancel_result}' — 이번 주기에는 재주문하지 않습니다.")
                            self._on_replace_held(existing_order, cancel_result)
                        else:
                            new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                            if new_id:
                                self.logger.info(f"매수 정정 주문 제출 완료. (ID: {new_id})")
                                self._track_order(new_id, ticker, "BUY", target_buy_price, buy_qty, entry_price=entry_price,
                                                  vwap=vwap, target_price=target_buy_price, signal=signal)
                                self._on_order_placed(broker, "ORDER_REPLACED", "BUY", new_id, target_buy_price, buy_qty,
                                                      old_price=existing_order["price"], old_qty=existing_order["qty"])
                            else:
                                self._on_order_place_failed("BUY", target_buy_price, buy_qty)
                    else:
                        self.logger.info("기존 매수 지정가 주문의 단가가 타겟 가격과 부합하여 유지합니다.")
                else:
                    self.logger.info(f"📈 매수 조건 감시 진입. 지정가 매수 주문 제출: {target_buy_price:.2f} @ {buy_qty}주")
                    new_id = broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
                    if new_id:
                        self.logger.info(f"매수 지정가 주문 제출 성공. (ID: {new_id})")
                        self._track_order(new_id, ticker, "BUY", target_buy_price, buy_qty, entry_price=entry_price,
                                          vwap=vwap, target_price=target_buy_price, signal=signal)
                        self._on_order_placed(broker, "ORDER_PLACED", "BUY", new_id, target_buy_price, buy_qty)
                    else:
                        self._on_order_place_failed("BUY", target_buy_price, buy_qty)
            else:
                self.logger.warning(f"설정된 투자 비중({k_percent}%)에 따른 예산({invest_cash:.2f})이 최소 1주 가격({target_buy_price:.2f})보다 적어 매수 주문을 보류합니다.")
                self._set_reason("BUDGET_SHORT",
                                 f"예산 {invest_cash:.2f} < 1주 {target_buy_price:.2f} → 매수 보류 (투자비중 {k_percent:g}%, 현금 {cash:.2f})")

        # 9-4. 대기 및 포지션 유지 (HOLD / WAIT)
        else:
            if open_orders:
                self.logger.info("전략 조건 외의 잔여 미체결 주문을 정리합니다.")
                for order in open_orders:
                    ok = self._cancel_order(broker, order["order_id"])
                    self._emit("ORDER_CANCELED", "info" if ok else "warn", (self._cycle or {}).get("reason_code") or "",
                               f"전략 조건 외 미체결 {'매수' if order.get('side') == 'BUY' else '매도'} 주문 취소"
                               f"{'' if ok else ' 요청 실패'}: {float(order.get('price') or 0):.2f} x {float(order.get('qty') or 0):g}주",
                               self._reason_data(order_id=order.get("order_id"), side=order.get("side"),
                                                 price=order.get("price"), qty=order.get("qty"), cancel_ok=bool(ok)))

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
                "vwap_stdev": signals.get("vwap_stdev", 0.0),
                # --- 관측성 필드 (주기 종료 시 _finish_cycle 이 최종 사유로 한 번 더 갱신) ---
                "reason_code": self._cycle.get("reason_code") or "",
                "reason_text": self._cycle.get("reason_text") or "",
                "filters": self._cycle.get("filters") or {},
                "waiting_for_start": bool(is_waiting_for_start),
                "session_label": session_label,
                "vwap_full_session": bool(vwap_full_session),
                "entry_price": round(float(entry_price), 2) if qty > 0 else 0.0,
                "position_qty": float(qty),
            }
            
        self.logger.info(f"✓ {ticker} 분석 주기 완료.")
