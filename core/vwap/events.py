"""VWAP 봇 관측성(Observability) 모듈 — 이벤트 로그(JSONL) 기록/조회 + Discord 알림.

이 모듈은 '기록과 알림'만 합니다. 매매 판단/주문에는 어떤 영향도 주지 않으며,
여기서 발생하는 모든 예외는 내부에서 삼켜(로그만 남김) 봇 루프를 멈추지 않습니다.

[이벤트 로그]
  파일: data/vwap_events_{mode}.jsonl  (mode 소문자: real, virtual_1, virtual_2, virtual_3)
  한 줄 = 하나의 JSON 객체:
    {"ts": "YYYY-MM-DD HH:MM:SS", "mode": "REAL", "type": "FILL", "level": "info",
     "reason_code": "SELL_ARMED", "message": "한글 설명", "data": {...}}
  - 파일이 MAX_EVENT_FILE_BYTES 를 넘으면 `<파일>.1` 로 회전(기존 .1 은 덮어씀) — 최대 약 2배 용량만 유지.
  - 손상된 줄(쓰는 도중 꺼짐 등)은 읽을 때 건너뜁니다.
  - 파일별 Lock 으로 스레드 안전 (봇 스레드 여러 개 + Flask 요청 스레드).

[Discord 알림]
  새 전송 경로를 만들지 않고 기존 utils/tunnel_manager.notify_via_butler 를 재사용합니다.
  (같은 프로세스의 Flask `/send` 엔드포인트 → asyncio.run_coroutine_threadsafe 로 Discord 루프에 위임,
   STATUS_CHANNEL_ID 채널, SecurityChecker 민감정보 필터 적용)
  - 전송은 전용 데몬 워커 스레드 + 크기 제한 큐로 처리해 봇 루프를 절대 막지 않습니다(큐가 차면 버림).
  - 같은 (mode, type, reason_code[, dedup_extra]) 알림은 NOTIFY_DEDUP_SEC 초 안에 한 번만 보냅니다.
"""
import os
import json
import time
import queue
import logging
import threading
from datetime import datetime

import core.vwap.config_manager as _cm

logger = logging.getLogger("vwap_bot")

EVENT_TYPES = {
    "BOT_START", "BOT_STOP", "SIGNAL_CHANGE", "ORDER_PLACED", "ORDER_REPLACED", "ORDER_CANCELED",
    "FILL", "STOP_LOSS", "PANIC", "ERROR", "CRITICAL",
    "SHADOW_SYNC",  # 3단계: 섀도우 봇이 REAL 보유 상태로 재동기화됨
}
EVENT_LEVELS = {"info", "warn", "error", "critical"}
VALID_MODES = {"REAL", "VIRTUAL_1", "VIRTUAL_2", "VIRTUAL_3", "VIRTUAL_SHADOW"}  # VIRTUAL_SHADOW: 3단계 섀도우 봇

MAX_EVENT_FILE_BYTES = 2 * 1024 * 1024  # 2MB 초과 시 .1 로 회전
NOTIFY_DEDUP_SEC = 60                   # 같은 type+reason 알림 중복 억제 시간
NOTIFY_QUEUE_MAX = 50                   # 전송 대기 큐 상한 (초과분은 버림 — 봇을 막지 않기 위해)

# REAL 모드에서 Discord 로 보낼 이벤트 종류 (ORDER_REPLACED/ORDER_CANCELED/SIGNAL_CHANGE 는 빈번하므로 제외)
NOTIFY_TYPES = {"ORDER_PLACED", "FILL", "STOP_LOSS", "PANIC", "CRITICAL", "ERROR", "BOT_START", "BOT_STOP"}


def normalize_mode(mode: str) -> str:
    m = str(mode or "").upper()
    return "VIRTUAL_1" if m == "VIRTUAL" else m


def events_path(mode: str) -> str:
    """이벤트 파일 경로. DATA_DIR 은 호출 시점에 읽습니다(테스트에서 임시 폴더로 바꿔치기 가능)."""
    return os.path.join(_cm.DATA_DIR, f"vwap_events_{normalize_mode(mode).lower()}.jsonl")


_path_locks = {}
_path_locks_guard = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _path_locks_guard:
        lk = _path_locks.get(path)
        if lk is None:
            lk = _path_locks[path] = threading.Lock()
        return lk


def _json_safe(value):
    """json.dumps 가 못 다루는 값(numpy 수치 등)을 기본 타입으로 바꿉니다."""
    try:
        if hasattr(value, "item"):
            return value.item()
    except Exception:
        pass
    return str(value)


def append_event(mode: str, etype: str, level: str = "info", reason_code: str = "",
                 message: str = "", data: dict = None) -> dict:
    """이벤트 한 줄을 기록합니다. 실패해도 예외를 던지지 않고 None 을 반환합니다.
    Returns: 기록한 이벤트 dict (실패 시 None)"""
    try:
        event = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": normalize_mode(mode),
            "type": str(etype),
            "level": level if level in EVENT_LEVELS else "info",
            "reason_code": reason_code or "",
            "message": message or "",
            "data": data or {},
        }
        line = json.dumps(event, ensure_ascii=False, default=_json_safe) + "\n"
        path = events_path(mode)
        with _lock_for(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) >= MAX_EVENT_FILE_BYTES:
                    os.replace(path, path + ".1")  # 하나만 유지 (기존 .1 은 덮어씀)
            except FileNotFoundError:
                pass
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return event
    except Exception as e:
        try:
            logger.warning(f"[VWAP 이벤트] 기록 실패 (무시하고 계속): {e}")
        except Exception:
            pass
        return None


def _read_lines(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


def read_events(mode: str, limit: int = 100, types=None) -> list:
    """최신순으로 이벤트를 최대 limit 개 반환합니다. types(iterable)가 있으면 해당 type 만.
    손상된 줄은 건너뜁니다. 현재 파일에서 부족하면 회전된 .1 파일까지 읽습니다."""
    type_set = {str(t).upper() for t in types} if types else None
    path = events_path(mode)
    result = []
    for src in (path, path + ".1"):  # 최신 파일 → (부족하면) 회전된 이전 파일
        with _lock_for(path):
            lines = _read_lines(src)
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            if type_set and str(ev.get("type", "")).upper() not in type_set:
                continue
            result.append(ev)
            if len(result) >= limit:
                return result
    return result


# ----------------------------------------------------------------------
# Discord 알림
# ----------------------------------------------------------------------
def _default_sender(message: str):
    """기존 Butler 알림 경로 재사용 (utils/tunnel_manager.notify_via_butler).
    지연 import — 테스트/단독 실행 시 불필요한 의존을 피하고, import 실패도 봇에 영향 없게."""
    from utils.tunnel_manager import notify_via_butler
    return notify_via_butler(message, retries=2, retry_delay=3)


class DiscordNotifier:
    """봇 스레드에서 호출해도 즉시 반환되는 비동기 알림기.

    - 중복 억제: 같은 key 는 NOTIFY_DEDUP_SEC 안에 1회
    - 전송: 데몬 워커 스레드 1개가 큐에서 꺼내 sender(message) 호출, 예외는 로그만
    - sender 는 테스트에서 교체 가능 (set_sender)
    """

    def __init__(self, sender=None, dedup_sec: float = NOTIFY_DEDUP_SEC):
        self.sender = sender or _default_sender
        self.dedup_sec = dedup_sec
        self._last_sent = {}
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=NOTIFY_QUEUE_MAX)
        self._worker = None
        self.synchronous = False  # 테스트용: True 면 큐를 거치지 않고 즉시 sender 호출

    def should_send(self, key) -> bool:
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and now - last < self.dedup_sec:
                return False
            self._last_sent[key] = now
            # 오래된 키 정리 (메모리 누수 방지)
            if len(self._last_sent) > 500:
                cutoff = now - self.dedup_sec
                self._last_sent = {k: v for k, v in self._last_sent.items() if v >= cutoff}
            return True

    def notify(self, key, message: str) -> bool:
        """알림을 큐에 넣습니다. 중복 억제되었거나 큐가 가득 차면 False."""
        try:
            if not self.should_send(key):
                return False
            if self.synchronous:
                self._safe_send(message)
                return True
            self._ensure_worker()
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            logger.warning("[VWAP 알림] 전송 대기열이 가득 차 알림 1건을 버렸습니다.")
            return False
        except Exception as e:
            logger.warning(f"[VWAP 알림] 알림 등록 실패 (무시): {e}")
            return False

    def _safe_send(self, message: str):
        try:
            ok = self.sender(message)
            if ok is False:
                logger.warning("[VWAP 알림] Discord 전송 실패 (Butler /send 응답 실패).")
        except Exception as e:
            logger.warning(f"[VWAP 알림] Discord 전송 예외 (무시): {e}")

    def _ensure_worker(self):
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._run, name="vwap-discord-notifier", daemon=True)
                self._worker.start()

    def _run(self):
        while True:
            message = self._queue.get()
            self._safe_send(message)


notifier = DiscordNotifier()


def set_sender(sender):
    """Discord 전송 함수를 교체합니다 (테스트용 mock 주입)."""
    notifier.sender = sender or _default_sender


def format_discord_message(event: dict) -> str:
    """이벤트를 짧은 한글 Discord 메시지로 만듭니다. 계좌번호/비밀값은 이벤트에 넣지 않으므로 포함되지 않습니다."""
    d = event.get("data") or {}
    head = f"[VWAP {event.get('mode')}] {event.get('type')}"
    lines = [f"**{head}** {event.get('message', '')}".strip()]
    reason_text = d.get("reason_text")
    if reason_text:
        lines.append(f"사유: {reason_text}")
    elif event.get("reason_code"):
        lines.append(f"사유코드: {event.get('reason_code')}")
    nums = []
    for key, label in (("ticker", "종목"), ("side", "구분"), ("price", "가격"), ("qty", "수량"),
                       ("current_price", "현재가"), ("vwap", "VWAP"), ("pnl", "손익"),
                       ("slippage", "슬리피지"), ("fill_source", "근거")):
        val = d.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, float):
            val = f"{val:.2f}" if key != "qty" else f"{val:g}"
        nums.append(f"{label} {val}")
    if nums:
        lines.append(" | ".join(nums))
    return "\n".join(lines)[:1500]
