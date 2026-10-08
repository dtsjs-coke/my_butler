"""
로그인 전역 슬롯 우대(ADR-0011 P5)와 VWAP API mode 화이트리스트 검증 — 네트워크/Discord/Toss 호출 없음.

  L1. 전역 제한 중 공격자가 30초 슬롯을 매번 먼저 가져가도, 로그인 성공 이력이 있는 키(신뢰 키)는 바로 검사받아 200
  L2. 신뢰 키 검사는 공용 슬롯을 소비하지 않음(직후 다른 키의 슬롯 판단이 그대로) / 신뢰 키 실패도 전역 카운트에 들어감
  L3. 신뢰 키도 키별 잠금(5회)은 그대로, 잠기면 신뢰 회수 / TTL 만료 / 보관 상한 / reset 초기화
  L4. 신뢰 이력 없는 키는 기존과 같음(전역 제한 중 30초 1회)
  M1. POST /vwap/api/reset-trades: 허용 모드(REAL, VIRTUAL, VIRTUAL_1~3)만 초기화, 그 밖(경로 조작·빈 값·숫자·SHADOW)은 400,
      임시 DATA_DIR 밖에 파일이 생기지 않음
  M2. GET /vwap/api/logs: 허용 모드 밖은 400
  M3. VwapConfigManager 거래기록 함수 심층 방어: 위험한 mode 는 저장/추가 False, 로드 [] (파일 생성 없음)
  M9. 운영 data/ 해시 전후 동일, 외부 네트워크 호출 0건

실행: PYTHONUTF8=1 venv312\\Scripts\\python.exe scripts\\test_login_priority_and_mode.py
"""
import os
import sys
import json
import shutil
import hashlib
import tempfile
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (PROJECT_ROOT, SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
REAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _hash_tree(path):
    out = {}
    for root, _d, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


DATA_BEFORE = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}

import requests  # noqa: E402
NET_CALLS = []


def _blocked_request(self, method, url, *a, **kw):
    NET_CALLS.append((method, str(url).split("?")[0][:60]))
    raise RuntimeError("network blocked in test")


requests.sessions.Session.request = _blocked_request

import test_vwap_reliability as h  # noqa: E402  (임시 DATA_DIR 패치, Discord no-op)
import core.vwap.config_manager as cm  # noqa: E402
cm.load_dotenv = lambda *a, **k: False  # 로컬 .env 재로드(실제 Toss 자격증명) 차단
for _k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ", "VWAP_ADMIN_PASSWORD"):
    os.environ.pop(_k, None)

from flask import Flask  # noqa: E402
import api.vwap_api as va  # noqa: E402
from api import login_throttle as lt  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  -- {detail}")


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _heat(t, clk, n=20):
    """서로 다른 키로 실패 n회 → 전역 제한 진입."""
    for i in range(n):
        ok, _, tk = t.acquire(f"atk-{i}")
        if ok:
            t.report(f"atk-{i}", tk, False)
        clk.advance(0.01)


def test_throttle_priority():
    print("\nL1~L4. 전역 제한 중 로그인 성공 이력 키 우대 (고정 시계)")
    clk = FakeClock()
    t = lt.LoginThrottle(clock=clk)
    owner = "198.51.100.10"
    ok, _, tk = t.acquire(owner)
    t.report(owner, tk, True)           # 평소 로그인 성공 → 신뢰 키
    _heat(t, clk)
    # 공격자가 매 슬롯 시작에 먼저 들어온다
    ok, _, tk = t.acquire("atk-new-1")
    t.report("atk-new-1", tk, False)
    check("공격자가 방금 슬롯을 가져감 → 다른 미신뢰 키는 429", t.acquire("203.0.113.77")[0] is False)
    ok, _, tk = t.acquire(owner)
    check("L1 신뢰 키(과거 성공)는 슬롯을 기다리지 않고 검사 허용", ok is True)
    t.report(owner, tk, True)
    # L2: 신뢰 키 검사가 공용 슬롯을 소비하지 않음
    clk.advance(29)
    ok, retry, _ = t.acquire("203.0.113.78")
    check("L2 신뢰 키 검사 후에도 공용 슬롯은 공격자 시각 기준(29초 뒤 retry_after=1)", ok is False and retry == 1, (ok, retry))
    clk.advance(1)
    ok, _, tk = t.acquire("203.0.113.78")
    check("L2 공용 슬롯 30초 뒤 정상 개방", ok is True)
    t.report("203.0.113.78", tk, False)
    g_before = len(t._global)
    ok, _, tk = t.acquire(owner)
    t.report(owner, tk, False)
    check("L2 신뢰 키 실패도 전역 실패 수에 들어감", len(t._global) == g_before + 1)
    # L3: 신뢰 키도 키별 잠금
    for _ in range(4):
        ok, _, tk = t.acquire(owner)
        if ok:
            t.report(owner, tk, False)
    ok, retry, _ = t.acquire(owner)
    check("L3 신뢰 키도 5회 실패면 잠금(429, 900초)", ok is False and retry == t.LOCKOUT, (ok, retry))
    check("L3 잠긴 키는 신뢰 회수", owner not in t._trusted)
    # L4: 미신뢰 키는 기존과 같음
    clk.advance(31)
    a = t.acquire("192.0.2.200")
    b = t.acquire("192.0.2.201")
    check("L4 미신뢰 키는 전역 제한 중 30초에 1건만 검사", a[0] is True and b[0] is False and b[1] == 30, (a, b))

    # TTL 만료
    clk2 = FakeClock()
    t2 = lt.LoginThrottle(clock=clk2, TRUSTED_TTL=100)
    ok, _, tk = t2.acquire("o")
    t2.report("o", tk, True)
    _heat(t2, clk2)
    t2.acquire("x")  # 공용 슬롯 사용
    check("TTL 안: 신뢰 키 허용", t2.acquire("o")[0] is True)
    t2._keys.pop("o", None)
    clk2.advance(101)
    t2.acquire("y")  # 공용 슬롯 다시 사용(전역 실패는 아직 창 안)
    check("TTL 지나면 우대 없음(429)", t2.acquire("o")[0] is False and "o" not in t2._trusted)

    # 보관 상한 / reset
    t3 = lt.LoginThrottle(clock=FakeClock(), TRUSTED_MAX=3)
    for i in range(10):
        ok, _, tk = t3.acquire(f"k{i}")
        t3.report(f"k{i}", tk, True)
    check("신뢰 키 보관 상한 유지(최근 성공 3개)", list(t3._trusted) == ["k7", "k8", "k9"], list(t3._trusted))
    t3.reset()
    check("reset() 이 신뢰 키도 비움", t3._trusted == {})
    check("새 설정 이름(TRUSTED_TTL/TRUSTED_MAX)은 오버라이드 허용, 모르는 이름은 TypeError",
          lt.LoginThrottle(TRUSTED_MAX=1).TRUSTED_MAX == 1 and _raises_type_error())


def _raises_type_error():
    try:
        lt.LoginThrottle(NOPE=1)
    except TypeError:
        return True
    return False


def _app():
    app = Flask(__name__)
    app.register_blueprint(va.vwap_bp, url_prefix="/vwap")
    return app.test_client()


def _files_outside(tmp_data):
    """임시 DATA_DIR 의 부모 폴더에 생긴 vwap_trades 파일(경로 조작 결과) 목록."""
    parent = os.path.dirname(tmp_data)
    return [n for n in os.listdir(parent) if n.startswith("vwap_trades")] + \
           [n for n in os.listdir(PROJECT_ROOT) if n.startswith("vwap_trades")]


def test_mode_whitelist():
    print("\nM1~M3. mode 화이트리스트")
    h.reset_data_dir()
    c = _app()
    with mock.patch.object(va.VwapCrypto, "verify_session_token", lambda *a, **k: True):
        r = c.post("/vwap/api/reset-trades", json={"mode": "REAL"})
        check("인증 없이(검증 함수만 패치) 경로 확인: REAL → 200", r.status_code == 200, r.status_code)
        ok_codes = {m: c.post("/vwap/api/reset-trades", json={"mode": m}).status_code
                    for m in ("VIRTUAL", "virtual_1", "VIRTUAL_2", "VIRTUAL_3")}
        check("허용 모드(VIRTUAL→VIRTUAL_1, 소문자 포함) 200", set(ok_codes.values()) == {200}, ok_codes)
        created = sorted(n for n in os.listdir(cm.DATA_DIR) if n.startswith("vwap_trades_"))
        check("허용 모드만 임시 DATA_DIR 에 파일 생성", created == ["vwap_trades_real.json", "vwap_trades_virtual_1.json",
                                                         "vwap_trades_virtual_2.json", "vwap_trades_virtual_3.json"], created)
        bad = ["../../evil", "..\\..\\evil", "REAL/../../x", "VIRTUAL_4", "VIRTUAL_SHADOW", "", " REAL", 5, None, ["REAL"],
               "REAL\x00", "a" * 300]
        codes = []
        for m in bad:
            r = c.post("/vwap/api/reset-trades", json={"mode": m})
            codes.append((repr(m)[:20], r.status_code, (r.get_json(silent=True) or {}).get("reason")))
        check(f"허용 밖 mode {len(bad)}종 → 모두 400 invalid_mode", all(x[1] == 400 and x[2] == "invalid_mode" for x in codes), codes)
        r = c.post("/vwap/api/reset-trades", data="[1,2]", content_type="application/json")
        r2 = c.post("/vwap/api/reset-trades", data="not json", content_type="text/plain")
        check("본문이 dict 가 아니거나 JSON 아님 → 기본값 VIRTUAL_1 로 처리(500 아님)",
              r.status_code == 200 and r2.status_code == 200, (r.status_code, r2.status_code))
        check("경로 조작 결과 파일 없음(임시 폴더 상위·프로젝트 루트)", _files_outside(cm.DATA_DIR) == [], _files_outside(cm.DATA_DIR))
        lc = {m: c.get("/vwap/api/logs", query_string={"mode": m}).status_code
              for m in ("REAL", "VIRTUAL", "VIRTUAL_SHADOW", "../../x", "VIRTUAL_9")}
        check("M2 /api/logs: 허용 모드 200, 그 밖 400",
              lc == {"REAL": 200, "VIRTUAL": 200, "VIRTUAL_SHADOW": 200, "../../x": 400, "VIRTUAL_9": 400}, lc)
    r = c.post("/vwap/api/reset-trades", json={"mode": "REAL"})
    check("세션 없으면 여전히 401(인증 경계 회귀 없음)", r.status_code == 401, r.status_code)

    before = set(os.listdir(cm.DATA_DIR))
    res = (VwapConfigManager.save_trades([], "../x"), VwapConfigManager.add_trade({}, "a/b"),
           VwapConfigManager.load_trades("..\\y"), VwapConfigManager.save_trades([], 7))
    check("M3 config_manager: 위험 mode → save False / add False / load [] / 비문자 False, 파일 생성 없음",
          res == (False, False, [], False) and set(os.listdir(cm.DATA_DIR)) == before, res)
    check("M3 정상 mode 회귀 없음(add→load)",
          VwapConfigManager.add_trade({"x": 1}, "VIRTUAL_2") and VwapConfigManager.load_trades("VIRTUAL_2")[-1] == {"x": 1})


def main():
    print("=" * 70)
    print(" 로그인 슬롯 우대(P5) / mode 화이트리스트 검증 — 네트워크 없음")
    print("=" * 70)
    for fn in (test_throttle_priority, test_mode_whitelist):
        try:
            fn()
        except Exception:
            import traceback
            global FAIL
            FAIL += 1
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()
    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    print("\nM9. 네트워크 / 운영 data/")
    check("외부 네트워크 호출 0건", NET_CALLS == [], NET_CALLS)
    after = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}
    check(f"운영 data/ 해시 전후 동일(파일 {len(DATA_BEFORE)}개)", after == DATA_BEFORE,
          [k for k in set(after) | set(DATA_BEFORE) if after.get(k) != DATA_BEFORE.get(k)])
    print("\n" + "=" * 70)
    print(f" 결과: {PASS}/{PASS + FAIL} 통과")
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
