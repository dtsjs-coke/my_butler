"""
VWAP 알림 채널 분리 검증 스크립트 — requests.post 는 mock, 실제 Discord/네트워크 전송 없음.

  N1. VWAP_CHANNEL_ID 설정 → VWAP 이벤트 알림 payload 의 channel_id 가 그 값
  N2. VWAP_CHANNEL_ID 미설정/0 → STATUS_CHANNEL_ID 로 전송
  N3. channel_id 없이 notify_via_butler 호출(기존 호출자, 예: 터널 알림) → STATUS_CHANNEL_ID
  N4. 잘못된 형식의 VWAP_CHANNEL_ID 환경변수 → constants 가 죽지 않고 0
  N5. 섀도우 봇 설정은 여전히 알림 꺼짐
  N6. notify_via_butler(channel_id=...) 명시 지정이 우선

실행: PYTHONUTF8=1 venv312\\Scripts\\python.exe scripts\\test_vwap_notify_channel.py
"""
import os
import sys
import hashlib
import importlib
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
os.environ.pop("VWAP_CHANNEL_ID", None)

from config import constants  # noqa: E402
from core.vwap import events as ev  # noqa: E402
from core.vwap import shadow  # noqa: E402
from utils import tunnel_manager as tm  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class FakeResp:
    status_code = 200
    text = "ok"


def run_vwap_event(vwap_id):
    """VWAP 알림기를 기본 sender(notify_via_butler)로 동작시키고 POST payload 를 수집."""
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        return FakeResp()

    n = ev.DiscordNotifier(sender=None)
    n.synchronous = True
    with mock.patch.object(constants, "VWAP_CHANNEL_ID", vwap_id), \
            mock.patch.object(tm.requests, "post", side_effect=fake_post):
        n.notify(("k", vwap_id), "[VWAP REAL] FILL test")
    return calls


def main():
    print("\nN1. VWAP_CHANNEL_ID 설정 → 그 채널")
    calls = run_vwap_event(222)
    check("POST 1회", len(calls) == 1, str(len(calls)))
    check("channel_id == 222", calls and calls[0]["json"]["channel_id"] == 222, str(calls))
    check("/send 로 전송", calls and calls[0]["url"].endswith("/send"))

    print("\nN2. 미설정/0 → STATUS_CHANNEL_ID")
    for v in (0, None):
        calls = run_vwap_event(v)
        check(f"VWAP_CHANNEL_ID={v} → 111", calls and calls[0]["json"]["channel_id"] == 111, str(calls))

    print("\nN3. channel_id 없이 notify_via_butler (기존 호출자) → STATUS")
    calls = []
    with mock.patch.object(tm.requests, "post",
                           side_effect=lambda url, json=None, headers=None, timeout=None: (calls.append(json) or FakeResp())):
        with mock.patch.object(constants, "VWAP_CHANNEL_ID", 222):
            ok = tm.notify_via_butler("터널 테스트", retries=1, retry_delay=0)
    check("성공 반환", ok is True)
    check("channel_id == 111 (VWAP 설정 무관)", calls and calls[0]["channel_id"] == 111, str(calls))

    print("\nN4. 잘못된 형식 환경변수 → 0")
    old = os.environ.get("VWAP_CHANNEL_ID")
    os.environ["VWAP_CHANNEL_ID"] = "abc!"
    try:
        c2 = importlib.reload(constants)
        check("VWAP_CHANNEL_ID == 0", c2.VWAP_CHANNEL_ID == 0, str(c2.VWAP_CHANNEL_ID))
        os.environ["VWAP_CHANNEL_ID"] = "333"
        c2 = importlib.reload(constants)
        check("정상 값 333 파싱", c2.VWAP_CHANNEL_ID == 333, str(c2.VWAP_CHANNEL_ID))
    finally:
        if old is None:
            os.environ.pop("VWAP_CHANNEL_ID", None)
        else:
            os.environ["VWAP_CHANNEL_ID"] = old
        importlib.reload(constants)

    print("\nN5. 섀도우 알림 꺼짐 유지")
    src = open(os.path.join(PROJECT_ROOT, "core", "vwap", "shadow.py"), encoding="utf-8").read()
    check("virtual_shadow_discord_notify False 유지", 'out["virtual_shadow_discord_notify"] = False' in src)
    check("virtual_discord_notify False 유지", 'out["virtual_discord_notify"] = False' in src)

    print("\nN6. channel_id 명시 지정")
    calls = []
    with mock.patch.object(tm.requests, "post",
                           side_effect=lambda url, json=None, headers=None, timeout=None: (calls.append(json) or FakeResp())):
        tm.notify_via_butler("x", retries=1, retry_delay=0, channel_id=444)
    check("channel_id == 444", calls and calls[0]["channel_id"] == 444, str(calls))

    after = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}
    print("\n[data/ 불변]")
    check("data/ 해시 동일", BEFORE == after)

    print(f"\n결과: PASS {PASS} / FAIL {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
