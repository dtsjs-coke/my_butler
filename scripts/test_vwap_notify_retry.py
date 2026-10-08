"""
VWAP 알림 연결 재시도 검증 — requests.post / time.sleep 은 mock, 실제 네트워크·대기 없음.

  R1. 연결 거부 2회 후 성공 → 전달 1회, WARNING 없음 (대기 2s, 4s)
  R2. 계속 거부 → POST 4회(최초+재시도 3), 대기 2/4/8s, 최종 WARNING 1회
  R3. HTTP 4xx 재시도 0회; 5xx/ReadTimeout 은 3초 후 1회 재시도
  R4. notify() 호출 스레드가 재시도 대기 동안 블로킹되지 않음 (워커 스레드에서 수행)
  R5. 중복 억제(같은 key) 유지

실행: PYTHONUTF8=1 venv312\\Scripts\\python.exe scripts\\test_vwap_notify_retry.py
"""
import os
import sys
import time
import hashlib
import logging
import threading
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
REAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _hash_tree(path):
    out = {}
    for root, _d, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


BEFORE = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}

os.environ["STATUS_CHANNEL_ID"] = "111"
os.environ["BUTLER_API_TOKEN"] = "dummy-test-token"
os.environ.pop("VWAP_CHANNEL_ID", None)

import requests  # noqa: E402
from core.vwap import events as ev  # noqa: E402
from utils import tunnel_manager as tm  # noqa: E402

PASS = FAIL = 0
REAL_SLEEP = time.sleep  # time.sleep 은 mock 대상이므로 폴링용 원본 보관


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = "x"


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def run(post_side_effects, sync=True):
    """notifier(sender=None → 기본 sender) 로 알림 1건. (post 호출수, sleep 목록, WARNING 목록) 반환."""
    h = ListHandler()
    ev.logger.addHandler(h)
    old_level = ev.logger.level
    ev.logger.setLevel(logging.DEBUG)
    sleeps = []
    post = mock.Mock(side_effect=post_side_effects)
    n = ev.DiscordNotifier(sender=None)
    n.synchronous = sync
    try:
        with mock.patch.object(tm.requests, "post", post), \
                mock.patch.object(tm.time, "sleep", side_effect=lambda s: sleeps.append(s)):
            n.notify(("k", id(post)), "[VWAP REAL] test")
            if not sync:
                for _ in range(100):
                    if post.call_count and not n._queue.unfinished_tasks and n._queue.empty():
                        REAL_SLEEP(0.05)
                        break
                    REAL_SLEEP(0.05)
    finally:
        ev.logger.removeHandler(h)
        ev.logger.setLevel(old_level)
    warns = [r for r in h.records if r.levelno >= logging.WARNING]
    return post.call_count, sleeps, warns


def main():
    CE = requests.exceptions.ConnectionError

    print("\nR1. 연결 거부 2회 후 성공")
    calls, sleeps, warns = run([CE("refused"), CE("refused"), Resp(200)])
    check("POST 3회 (성공 1회 전달)", calls == 3, str(calls))
    check("대기 2s,4s", sleeps == [2, 4], str(sleeps))
    check("WARNING 없음", not warns, str([w.getMessage() for w in warns]))

    print("\nR2. 계속 거부")
    calls, sleeps, warns = run([CE("refused")] * 10)
    check("POST 4회 (최초 + 재시도 3)", calls == 4, str(calls))
    check("대기 2,4,8 (합 14s ≤ 15s)", sleeps == [2, 4, 8] and sum(sleeps) <= 15, str(sleeps))
    check("최종 WARNING 1회", len(warns) == 1 and "Discord 전송 실패" in warns[0].getMessage(),
          str([w.getMessage() for w in warns]))

    print("\nR3. HTTP 4xx 는 재시도 없음")
    for code in (400, 401, 404):
        calls, sleeps, warns = run([Resp(code)] * 5)
        check(f"HTTP {code}: POST 1회, 대기 없음, WARNING 1회",
              calls == 1 and sleeps == [] and len(warns) == 1, f"{calls} {sleeps} {len(warns)}")

    print("\nR3b. HTTP 5xx / 타임아웃: 3초 후 1회 재시도")
    RT = requests.exceptions.ReadTimeout
    calls, sleeps, warns = run([Resp(500), Resp(200)])
    check("5xx 1회 후 성공: POST 2회, 대기 [3], WARNING 없음", calls == 2 and sleeps == [3] and not warns,
          f"{calls} {sleeps} {len(warns)}")
    for code in (500, 503):
        calls, sleeps, warns = run([Resp(code)] * 5)
        check(f"HTTP {code} 지속: POST 2회, 대기 [3], WARNING 1회", calls == 2 and sleeps == [3] and len(warns) == 1,
              f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([RT("t"), Resp(200)])
    check("ReadTimeout 1회 후 성공: POST 2회, WARNING 없음", calls == 2 and sleeps == [3] and not warns,
          f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([RT("t")] * 5)
    check("ReadTimeout 지속: POST 2회, WARNING 1회", calls == 2 and sleeps == [3] and len(warns) == 1,
          f"{calls} {sleeps} {len(warns)}")

    print("\nR3d. 연결 재시도 후에도 5xx/타임아웃 1회 재시도 (카운터 분리)")
    calls, sleeps, warns = run([CE("r"), Resp(500), Resp(200)])
    check("거부→5xx→200: POST 3회, sleep [2,3], WARNING 없음", calls == 3 and sleeps == [2, 3] and not warns,
          f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([CE("r"), RT("t"), Resp(200)])
    check("거부→ReadTimeout→200: POST 3회, sleep [2,3]", calls == 3 and sleeps == [2, 3] and not warns,
          f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([CE("r")] * 3 + [Resp(500), Resp(200)])
    check("거부 3회→5xx→200: POST 5회, sleep [2,4,8,3]", calls == 5 and sleeps == [2, 4, 8, 3] and not warns,
          f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([CE("r")] * 3 + [Resp(500), Resp(500)])
    check("거부 3회→5xx→5xx: POST 5회, WARNING 1회", calls == 5 and sleeps == [2, 4, 8, 3] and len(warns) == 1,
          f"{calls} {sleeps} {len(warns)}")
    calls, sleeps, warns = run([CE("r")] * 3 + [Resp(404)] * 3)
    check("거부 3회→404: POST 4회, 추가 재시도 없음, WARNING 1회", calls == 4 and sleeps == [2, 4, 8] and len(warns) == 1,
          f"{calls} {sleeps} {len(warns)}")
    post = mock.Mock(side_effect=[CE("r")] * 6)
    sl = []
    with mock.patch.object(tm.requests, "post", post), mock.patch.object(tm.time, "sleep", side_effect=sl.append):
        r = tm.notify_via_butler("x", retries=3, retry_delay=5)
    check("기본 호출자 회귀: 연결 거부도 retries(3)만큼, sleep [5,5]", r is False and post.call_count == 3 and sl == [5, 5],
          f"{post.call_count} {sl}")

    print("\nR3c. 기본 호출자(connect_retry_delays 없음)는 기존 동작 유지 (4xx 도 retries 만큼 재시도)")
    post = mock.Mock(side_effect=[Resp(404)] * 5)
    with mock.patch.object(tm.requests, "post", post), mock.patch.object(tm.time, "sleep"):
        r = tm.notify_via_butler("x", retries=3, retry_delay=0)
    check("POST 3회, False", r is False and post.call_count == 3, str(post.call_count))

    print("\nR4. 호출 스레드 비블로킹 (비동기 모드, 재시도 대기 중)")
    release = threading.Event()
    seen_threads = []
    post = mock.Mock(side_effect=[CE("refused"), Resp(200)])
    n = ev.DiscordNotifier(sender=None)

    def slow_sleep(s):
        seen_threads.append(threading.current_thread().name)
        release.wait(5)

    with mock.patch.object(tm.requests, "post", post), mock.patch.object(tm.time, "sleep", side_effect=slow_sleep):
        t0 = time.monotonic()
        ok = n.notify(("k", "async"), "[VWAP REAL] async test")
        elapsed = time.monotonic() - t0
        check("notify() 즉시 반환 (<0.5s)", ok is True and elapsed < 0.5, f"{elapsed:.3f}s")
        for _ in range(100):
            if seen_threads:
                break
            REAL_SLEEP(0.02)
        check("재시도 대기는 vwap-discord-notifier 데몬 스레드에서",
              seen_threads and seen_threads[0] == "vwap-discord-notifier", str(seen_threads))
        check("워커는 데몬 스레드", n._worker is not None and n._worker.daemon)
        release.set()
        for _ in range(100):
            if post.call_count >= 2:
                break
            REAL_SLEEP(0.02)
        check("재시도 후 POST 2회", post.call_count == 2, str(post.call_count))

    print("\nR5. 중복 억제 유지")
    post = mock.Mock(side_effect=[Resp(200)] * 3)
    n = ev.DiscordNotifier(sender=None)
    n.synchronous = True
    with mock.patch.object(tm.requests, "post", post):
        a = n.notify("same", "m1")
        b = n.notify("same", "m2")
    check("두 번째는 억제, POST 1회", a is True and b is False and post.call_count == 1,
          f"{a} {b} {post.call_count}")

    print("\nR6. 설정값")
    check("CONNECT_RETRY_DELAYS == (2,4,8)", ev.CONNECT_RETRY_DELAYS == (2, 4, 8))

    after = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}
    check("data/ 운영 파일 무변경", BEFORE == after)

    print(f"\n결과: PASS {PASS} / FAIL {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
