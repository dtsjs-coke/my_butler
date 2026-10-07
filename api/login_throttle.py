"""
VWAP admin 로그인(/vwap/login) 시도 제한 (ADR-0011 D9).

두 겹으로 막는다.
  1. 클라이언트별 잠금: 같은 클라이언트 키가 PER_KEY_WINDOW 안에 PER_KEY_MAX 번 틀리면 LOCKOUT 동안 잠근다.
     클라이언트 키 = `Cf-Connecting-IP` 헤더(터널 요청은 원격 주소가 모두 127.0.0.1 이라서), 없으면 remote_addr.
  2. 전역 제한: 헤더는 위조될 수 있으므로 키와 무관하게 전체 실패 수를 센다. GLOBAL_WINDOW 안의 실패가
     GLOBAL_THRESHOLD 이상이면, 그동안은 GLOBAL_INTERVAL 초에 한 번만 비밀번호를 검사한다(나머지는 429).
     같은 기기에서 직접 붙은 요청(127.0.0.1/::1 + 터널 헤더 없음)은 전역 제한에서 빼서 소유자의 복구 경로로 둔다
     (클라이언트별 잠금은 그대로 받는다).

잠겨 있는(또는 전역 제한에 걸린) 동안에는 비밀번호를 아예 검사하지 않는다. 올바른 비밀번호도 429.
  검사해 주면 '틀림 → 429, 맞음 → 200' 으로 정답이 드러나 잠금이 의미가 없어지기 때문이다.

상태는 프로세스 메모리에만 둔다(재시작하면 초기화). 실패가 일어날 때마다 디스크에 쓰지 않고,
재시작 자체가 소유자의 비상 해제 수단이 된다. 외부 공격자는 원격으로 서버를 재시작시킬 수 없다.

동시성: 검사를 허용할 때 그 시도를 '실패'로 먼저 기록해 두고(가예약), 성공하면 지운다.
  같은 키로 동시에 요청을 많이 보내도 검사 전에 한도가 적용된다.
"""
import logging
import math
import re
import threading
import time
from collections import deque

logger = logging.getLogger("butler_auth")

CLIENT_IP_HEADER = "Cf-Connecting-IP"
_KEY_SAFE_RE = re.compile(r"[^0-9A-Za-z:.\-]")


def client_key(headers, remote_addr) -> str:
    """요청의 클라이언트 키. 로그에도 쓰므로 안전한 문자만 남기고 64자로 자른다."""
    raw = (headers.get(CLIENT_IP_HEADER) or "").strip() or (remote_addr or "") or "unknown"
    return _KEY_SAFE_RE.sub("?", raw)[:64]


class LoginThrottle:
    PER_KEY_MAX = 5            # 같은 키 실패 허용 횟수
    PER_KEY_WINDOW = 600       # 10분
    LOCKOUT = 900              # 15분 잠금
    GLOBAL_THRESHOLD = 20      # 전체 실패가 10분에 20회 이상이면 전역 제한 시작
    GLOBAL_WINDOW = 600
    GLOBAL_INTERVAL = 30       # 전역 제한 중에는 30초에 한 번만 검사
    MAX_KEYS = 2000            # 위조 헤더로 키를 무한히 만들어 메모리를 채우지 못하게

    def __init__(self, clock=time.monotonic, **overrides):
        for k, v in overrides.items():
            if not hasattr(LoginThrottle, k) or not k.isupper():
                raise TypeError(f"unknown setting: {k}")
            setattr(self, k, v)
        self._clock = clock
        self._lock = threading.Lock()
        self._keys = {}            # key -> {"fails": deque[ts], "locked_until": float, "last": float}
        self._global = deque()     # 실패(가예약 포함) 시각
        self._last_check = None    # 마지막으로 비밀번호 검사를 허용한 시각(전역 제한 간격 계산용)
        self._global_active = False

    # -- 내부 -------------------------------------------------------------
    def _prune(self, now):
        g = self._global
        while g and g[0] <= now - self.GLOBAL_WINDOW:
            g.popleft()

    def _prune_key(self, st, now):
        f = st["fails"]
        while f and f[0] <= now - self.PER_KEY_WINDOW:
            f.popleft()

    def _evict(self, now):
        if len(self._keys) <= self.MAX_KEYS:
            return
        for k in list(self._keys):
            st = self._keys[k]
            self._prune_key(st, now)
            if not st["fails"] and st["locked_until"] <= now:
                del self._keys[k]
        while len(self._keys) > self.MAX_KEYS:
            oldest = min(self._keys, key=lambda k: self._keys[k]["last"])
            del self._keys[oldest]

    @staticmethod
    def _secs(delta):
        return max(1, int(math.ceil(delta)))

    # -- 공개 API ---------------------------------------------------------
    def acquire(self, key, exempt_global=False):
        """비밀번호 검사를 해도 되는지 판단한다.

        Returns: (allowed, retry_after_sec, ticket)
          allowed=False 면 retry_after 초 뒤에 다시 시도해야 한다(429).
          allowed=True 면 검사 후 반드시 report(key, ticket, success) 를 부른다.
        """
        now = self._clock()
        with self._lock:
            self._prune(now)
            st = self._keys.get(key)
            if st is not None:
                self._prune_key(st, now)
                if st["locked_until"] > now:
                    return False, self._secs(st["locked_until"] - now), None

            global_hot = len(self._global) >= self.GLOBAL_THRESHOLD
            if global_hot and not self._global_active:
                self._global_active = True
                logger.warning(f"[로그인] 전역 제한 시작: 최근 {self.GLOBAL_WINDOW}초 실패 {len(self._global)}회 "
                               f"-> {self.GLOBAL_INTERVAL}초에 1회만 검사")
            elif not global_hot and self._global_active:
                self._global_active = False
                logger.warning("[로그인] 전역 제한 해제")
            if global_hot and not exempt_global and self._last_check is not None:
                wait = self._last_check + self.GLOBAL_INTERVAL - now
                if wait > 0:
                    return False, self._secs(wait), None

            if st is None:
                st = self._keys[key] = {"fails": deque(), "locked_until": 0.0, "last": now}
                self._evict(now)
            # 가예약: 성공하면 report 에서 지운다
            st["fails"].append(now)
            st["last"] = now
            self._global.append(now)
            self._last_check = now
            if len(st["fails"]) >= self.PER_KEY_MAX:
                st["locked_until"] = now + self.LOCKOUT
            return True, 0, now

    def report(self, key, ticket, success):
        """검사 결과를 반영한다. 성공이면 그 키의 실패 기록·잠금을 지우고 전역 카운트에서도 이번 시도를 뺀다."""
        with self._lock:
            st = self._keys.get(key)
            if success:
                try:
                    self._global.remove(ticket)
                except ValueError:
                    pass
                self._keys.pop(key, None)
                return
            fails = len(st["fails"]) if st else 0
            locked = bool(st and st["locked_until"] > self._clock())
        logger.warning(f"[로그인] 실패 client={key} 최근 {self.PER_KEY_WINDOW}초 {fails}/{self.PER_KEY_MAX}회"
                       + (f" -> {self.LOCKOUT}초 잠금" if locked else ""))

    def reset(self):
        """(테스트·운영 비상용) 모든 상태 초기화."""
        with self._lock:
            self._keys.clear()
            self._global.clear()
            self._last_check = None
            self._global_active = False
