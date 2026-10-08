"""
Butler 인증 경계(ADR-0011) 검증 스크립트 — Flask test_client 만 사용, 네트워크/Discord/Toss 호출 없음.

  A1. 토큰 미설정(빈 값/공백) → 토큰 경로 전부 401 (fail-closed)
  A2. 잘못된 토큰 401 / 올바른 토큰 200
  A3. 세션 쿠키로 대시보드 API 통과
  A4. /users/*, /subscriptions/* 는 세션만으로 접근 불가(토큰 전용)
  A5. /send: 로컬+헤더 없음+토큰 → 허용 / CF-Ray·Cf-Connecting-IP·X-Forwarded-For → 403 / 원격 주소 → 403 / 토큰 없음 → 401
  A6. 공개 페이지(/, /trains, /settlement, /liquor, /news) 미로그인 200 (2026-10-08 사용자 결정: / · /trains 공개로 변경)
  A7. 로그인 후 next 로 복귀, 외부 주소 next 는 무시(오픈 리다이렉트 없음)
  A8. 공개 페이지 API 권한: liquor 조회 공개/쓰기 admin, 뉴스 그룹 조회 공개/수정 세션·토큰, 정산 공개(검증·상한)
  A9. 모든 페이지 HTML 에 토큰 값 0건 (테스트 토큰, 로컬 .env 토큰, 과거 코드 기본값)
  A10. VWAP 로그인 쿠키 속성(HttpOnly, SameSite=Lax, Secure), 응답 본문에 세션 토큰 없음, admin 경로 회귀 없음
  A11. Flask 바인딩 127.0.0.1
  A12. 운영 data/ 해시 전후 동일
  A13. (D9) 로그인 시도 제한 — 키별 잠금·429·Retry-After·해제 시점·10분 창·전역 상한·로컬 예외·위조 키 폭주·가예약·메모리 상한·로그
  A14. (D10) 비밀번호 해시 — 새 형식 검증, 옛 형식 → 새 형식 자동 마이그레이션, 잘못된 형식, scrypt 불가 환경의 pbkdf2 대체, 비밀번호 변경
  A15. 정산 쓰기 보강 (chunked 상한, NaN/Infinity, 깊은 중첩, id)
  A16. (정정 2026-10-07) 기본 비밀번호 대체 제거 — 해시·.env 없음 → 401, 경고 1회, .env 복구·마이그레이션, 정상 해시 경로 불변

토큰 값은 어떤 경우에도 출력하지 않는다.
실행: PYTHONUTF8=1 venv312\\Scripts\\python.exe scripts\\test_auth_boundary.py
"""
import os
import re
import sys
import json
import shutil
import hashlib
import inspect
import tempfile
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
REAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _hash_tree(path):
    out = {}
    for root, _d, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


def _digest(tree):
    return hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()[:16]


DATA_BEFORE = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}

# 로컬 .env 의 실제 토큰(있다면)은 "HTML 에 없어야 한다"는 확인에만 쓰고 출력하지 않는다.
try:
    from dotenv import dotenv_values
    ENV_FILE_TOKEN = (dotenv_values(os.path.join(PROJECT_ROOT, ".env")).get("BUTLER_API_TOKEN") or "").strip()
except Exception:
    ENV_FILE_TOKEN = ""


# 과거 코드에 하드코딩돼 있던 공개 기본 토큰의 sha256(hex). 값 자체는 어디에도 두지 않고, git 이력에도 의존하지 않는다.
LEGACY_TOKEN_SHA256 = {
    "aa72dc45e1bac8b490000eec8e12bb709f6338972deb17bd76bbeb264ae3be58",
}
_QUOTED_RE = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'")
_TOKENLIKE_RE = re.compile(r"[A-Za-z0-9_\-.~+/=]{6,}")


def _candidate_strings(text):
    """텍스트에서 따옴표 안 문자열 리터럴 + 토큰 형태 문자열 + 공백 구분 조각을 모두 뽑는다."""
    out = set()
    for m in _QUOTED_RE.finditer(text):
        out.add(m.group(1) if m.group(1) is not None else m.group(2))
    out.update(_TOKENLIKE_RE.findall(text))
    out.update(text.split())
    return out


def find_hash_matches(text, hashes):
    """text 안 후보 문자열 중 sha256 이 hashes 와 일치하는 것의 개수(값은 반환/출력하지 않음)."""
    return sum(1 for c in _candidate_strings(text)
               if c and hashlib.sha256(c.encode("utf-8")).hexdigest() in hashes)


LEGACY_SCAN_FILES = ["api/flask_app.py", "api/auth.py", "utils/tunnel_manager.py", "butler_agent.py"]


def _legacy_scan_targets():
    paths = [os.path.join(PROJECT_ROOT, *f.split("/")) for f in LEGACY_SCAN_FILES]
    for root, _d, files in os.walk(os.path.join(PROJECT_ROOT, "api", "templates")):
        paths += [os.path.join(root, n) for n in sorted(files)]
    return paths


TEST_TOKEN = "test-" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]

# flask_app 의 load_dotenv 는 기존 환경변수를 덮어쓰지 않는다 → 테스트 값이 우선.
os.environ["BUTLER_API_TOKEN"] = TEST_TOKEN

# 네트워크 차단 가드(모든 import 이전에 설치): requests 를 통한 외부 호출을 실패시키고 횟수를 센다(마지막에 0건이어야 통과).
import requests  # noqa: E402
NET_CALLS = []


def _blocked_request(self, method, url, *a, **kw):
    NET_CALLS.append((method, str(url).split("?")[0][:60]))
    raise RuntimeError("network blocked in test_auth_boundary")


requests.sessions.Session.request = _blocked_request

# VWAP 하네스 재사용: 설정/거래 DATA_DIR 을 임시 폴더로 바꾸고 Discord 전송을 no-op 으로 (봇 자동 복구도 임시 설정 기준)
import test_vwap_reliability as h  # noqa: E402
# VwapConfigManager.load_config() 는 호출마다 load_dotenv(override=True) 로 로컬 .env 를 다시 읽는다
# (Toss 자격증명·BUTLER_API_TOKEN 이 환경변수로 되살아남) → 테스트 중에는 no-op 으로 막는다.
import core.vwap.config_manager as _cm_module  # noqa: E402
_cm_module.load_dotenv = lambda *a, **k: False
import api.flask_app as fa  # noqa: E402
from api import auth as butler_auth  # noqa: E402
from core.vwap.crypto import VwapCrypto  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402

# flask_app 의 load_dotenv 가 로컬 .env 의 Toss 자격증명을 환경변수로 올린다. VWAP 설정 로드는 환경변수를 우선하므로
# 그대로 두면 /vwap/api/status 가 실제 Toss 토큰 발급을 시도한다 → 제거해 실거래 API 경로가 꺼지게 한다.
for _k in ("TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ", "VWAP_ADMIN_PASSWORD"):
    os.environ.pop(_k, None)

TMP_ROOT = tempfile.mkdtemp(prefix="butler_auth_test_")
os.makedirs(os.path.join(TMP_ROOT, "data"), exist_ok=True)
fa.PROJECT_ROOT = TMP_ROOT  # 정산/키워드 그룹 파일을 임시 폴더로

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


def set_token(value):
    os.environ["BUTLER_API_TOKEN"] = value


def anon():
    return fa.app.test_client()


def admin():
    c = fa.app.test_client()
    c.set_cookie("vwap_session", VwapCrypto.generate_session_token("admin"))
    return c


TOK = lambda: {"X-Butler-Token": TEST_TOKEN}  # noqa: E731
FAKE_STATUS = {"battery": {"percentage": 50, "status": "x", "temperature": 30},
               "memory": {"percentage": 1, "used": 1, "total": 2}, "cpu": {"percentage": 1}}
FAKE_YAML = {"users": [{"id": "u1"}], "subscriptions": {"u1": []}}


class _Patches:
    """쓰기/시스템 호출이 운영 파일에 닿지 않게 막는다."""

    def __enter__(self):
        self.ps = [
            mock.patch.object(fa, "get_system_status_data", lambda: dict(FAKE_STATUS)),
            mock.patch.object(fa, "get_system_status_history", lambda: []),
            mock.patch.object(fa, "load_yaml", lambda path: json.loads(json.dumps(FAKE_YAML))),
            mock.patch.object(fa, "save_yaml", lambda path, data: None),
            mock.patch.object(fa, "save_keywords", lambda kws: None),
            mock.patch.object(fa, "save_queue", lambda q: None),
            mock.patch.object(fa, "load_keywords", lambda: ["kw1"]),
            mock.patch.object(fa.liquor_manager, "list_purchases_sorted", lambda: []),
            mock.patch.object(fa.liquor_manager, "load_dismissed_pairs", lambda: []),
            mock.patch.object(fa.liquor_manager, "create_purchase", lambda d: ({"id": "x"}, None)),
        ]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self.ps:
            p.stop()


# 2026-10-08 사용자 결정: GET /api/system_status 는 공개 → 토큰 경로 목록에서 제외 (PUBLIC_API_ROUTES 로 별도 검증)
TOKEN_ROUTES = [  # (method, path, json)
    ("GET", "/api/srt/queue", None),
    ("GET", "/api/keywords", None),
    ("GET", "/users/all", None),
    ("GET", "/users/u1", None),
    ("GET", "/subscriptions/all", None),
    ("GET", "/subscriptions/u1", None),
]


def call(client, method, path, js=None, headers=None, **kw):
    return client.open(path, method=method, json=js, headers=headers or {}, **kw)


def test_a1_fail_closed():
    print("\nA1. 토큰 미설정 → 토큰 경로 401")
    for empty in ("", "   "):
        set_token(empty)
        c = anon()
        codes = [call(c, m, p, j, {"X-Butler-Token": v}).status_code
                 for (m, p, j) in TOKEN_ROUTES for v in ("", " ", "x", TEST_TOKEN)]
        check(f"BUTLER_API_TOKEN={empty!r} → 모든 토큰 경로·모든 헤더 값에서 401 ({len(codes)}건)",
              set(codes) == {401}, f"{sorted(set(codes))}")
        r = call(c, "POST", "/send", {"content": "x"}, {"X-Butler-Token": ""})
        check(f"BUTLER_API_TOKEN={empty!r} → /send(로컬) 빈 헤더도 401", r.status_code == 401, r.status_code)
    os.environ.pop("BUTLER_API_TOKEN", None)
    r = call(anon(), "GET", "/users/all", None, {"X-Butler-Token": ""})
    check("환경변수 자체가 없음 → 401 (코드에 기본값 없음)", r.status_code == 401, r.status_code)
    set_token(TEST_TOKEN)
    src = inspect.getsource(butler_auth) + inspect.getsource(fa)
    check("코드에 토큰 기본값 없음: os.getenv/environ.get('BUTLER_API_TOKEN', <비어있지 않은 값>) 패턴 0건",
          not re.search(r'BUTLER_API_TOKEN"\s*,\s*"[^"]+"', src))
    # 해시 일치 로직 자가 검증(임의 문자열 사용): 리터럴·토큰 형태·주변 문맥 속 탐지, 근접 값은 미탐지
    probe = "probe-" + hashlib.sha256(os.urandom(16)).hexdigest()[:20]
    ph = {hashlib.sha256(probe.encode()).hexdigest()}
    check("자가 검증: 큰따옴표 리터럴 속 값 탐지", find_hash_matches(f'x = os.getenv("T", "{probe}")', ph) == 1)
    check("자가 검증: 작은따옴표 리터럴 속 값 탐지", find_hash_matches(f"x = '{probe}'", ph) == 1)
    check("자가 검증: 따옴표 없는 토큰 형태(HTML/주석) 탐지", find_hash_matches(f"<meta content={probe}> # {probe}", ph) >= 1)
    check("자가 검증: 근접 값(접미 추가/한 글자 부족)·무관 텍스트는 미탐지",
          find_hash_matches(f'a = "{probe}x"; b = "{probe[:-1]}"; c = "hello"', ph) == 0)
    with tempfile.TemporaryDirectory() as td:
        fp = os.path.join(td, "t.py")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(f'TOKEN = "{probe}"\n')
        with open(fp, encoding="utf-8") as f:
            check("자가 검증: 임시 파일에 넣은 값 탐지", find_hash_matches(f.read(), ph) == 1)
    targets = _legacy_scan_targets()
    missing = [os.path.relpath(t, PROJECT_ROOT) for t in targets if not os.path.isfile(t)]
    check(f"과거 공개 기본값 검사 대상 파일 존재 ({len(targets)}개: 소스 4 + 템플릿)", not missing and len(targets) >= 5, missing)
    total = 0
    for t in targets:
        if os.path.isfile(t):
            with open(t, encoding="utf-8", errors="replace") as f:
                total += find_hash_matches(f.read(), LEGACY_TOKEN_SHA256)
    check("과거 공개 기본값(해시 비교)이 flask_app.py / auth.py / tunnel_manager.py / butler_agent.py / 템플릿에 0건", total == 0, total)


def test_a2_token():
    print("\nA2. 잘못된 토큰 / 올바른 토큰")
    set_token(TEST_TOKEN)
    c = anon()
    bad = [call(c, m, p, j, {"X-Butler-Token": TEST_TOKEN + "x"}).status_code for (m, p, j) in TOKEN_ROUTES]
    bad2 = [call(c, m, p, j, {"X-Butler-Token": TEST_TOKEN[:-1]}).status_code for (m, p, j) in TOKEN_ROUTES]
    none = [call(c, m, p, j).status_code for (m, p, j) in TOKEN_ROUTES]
    good = [call(c, m, p, j, TOK()).status_code for (m, p, j) in TOKEN_ROUTES]
    check("잘못된 토큰(접미 추가/한 글자 부족) → 전부 401", set(bad + bad2) == {401}, f"{bad} {bad2}")
    check("헤더 없음 → 전부 401", set(none) == {401}, f"{none}")
    check("올바른 토큰 → 전부 200", set(good) == {200}, f"{good}")
    check("비교는 hmac.compare_digest 사용", "compare_digest" in inspect.getsource(butler_auth.has_valid_token))
    r = call(c, "POST", "/subscriptions/u1", [{"a": 1}], TOK())
    check("토큰으로 /subscriptions POST 200 (save_yaml mock)", r.status_code == 200, r.status_code)


def test_a3_session():
    print("\nA3. 세션 쿠키로 대시보드 API 통과")
    c = admin()
    # 2026-10-08 사용자 결정: system_status 는 공개이므로 세션 없이도 200 (아래 별도 검사). SRT 3종은 세션 필요.
    for m, p, j in [("GET", "/api/system_status", None), ("GET", "/api/srt/queue", None),
                    ("DELETE", "/api/srt/queue", {"user_id": "WEB_USER", "index": 99}),
                    ("GET", "/api/keywords", None), ("POST", "/api/keywords", {"keyword": "새키워드"}),
                    ("POST", "/api/keyword_groups", {"group_name": "G", "members": []}),
                    ("DELETE", "/api/keyword_groups", {"group_name": "G"})]:
        r = call(c, m, p, j)
        # DELETE /api/srt/queue 는 없는 인덱스라 404 (인증은 통과)
        ok = (404,) if (m, p) == ("DELETE", "/api/srt/queue") else (200,)
        check(f"세션 {m} {p} → 인증 통과 {ok}", r.status_code in ok, f"{r.status_code} {r.get_data(as_text=True)[:80]}")
    r = call(c, "POST", "/api/srt/reserve", {"dep": "a", "arr": "b"})
    check("세션 POST /api/srt/reserve → 인증 통과(400 missing_data)", r.status_code == 400, r.status_code)
    # 2026-10-08 사용자 결정: 시스템 상태는 로그인 없이 공개
    check("미인증 GET /api/system_status → 200 (2026-10-08 사용자 결정: 공개)", call(anon(), "GET", "/api/system_status").status_code == 200)
    # 2026-10-08 사용자 결정: SRT 대기열 조회·삭제·예매는 로그인 필요 (여행 일정 노출 방지)
    for m, p, j in [("GET", "/api/srt/queue", None),
                    ("DELETE", "/api/srt/queue", {"user_id": "WEB_USER", "index": 0}),
                    ("POST", "/api/srt/reserve", {"dep": "a", "arr": "b", "date": "20261010", "time": "000000"}),
                    ("POST", "/api/keywords", {"keyword": "x"}), ("GET", "/api/keywords", None),
                    ("POST", "/api/keyword_groups", {"group_name": "G", "members": []}),
                    ("DELETE", "/api/keyword_groups", {"group_name": "G"})]:
        r = call(anon(), m, p, j)
        check(f"미인증 {m} {p} → 401", r.status_code == 401, r.status_code)
    bad = fa.app.test_client()
    bad.set_cookie("vwap_session", "garbage")
    check("위조 세션 쿠키 → 401 (SRT 큐 조회; system_status 는 공개라 제외)", call(bad, "GET", "/api/srt/queue").status_code == 401)
    c2 = fa.app.test_client()
    with mock.patch("core.vwap.crypto.time.time", return_value=1_000_000.0):
        old = VwapCrypto.generate_session_token("admin")
    c2.set_cookie("vwap_session", old)
    check("만료된 세션(24h 초과) → 401", call(c2, "GET", "/api/srt/queue").status_code == 401)
    c3 = fa.app.test_client()
    c3.set_cookie("vwap_session", VwapCrypto.generate_session_token("guest"))
    check("admin 이 아닌 세션 → 401", call(c3, "GET", "/api/srt/queue").status_code == 401)


def test_a4_token_only():
    print("\nA4. /users, /subscriptions 는 세션만으로 불가")
    c = admin()
    codes = [call(c, m, p, j).status_code for (m, p, j) in TOKEN_ROUTES if p.startswith(("/users", "/subscriptions"))]
    codes += [call(c, "POST", "/users/u1", {"id": "u1"}).status_code, call(c, "POST", "/subscriptions/u1", []).status_code]
    check(f"세션만 → 401 ({len(codes)}건, GET·POST)", set(codes) == {401}, f"{codes}")
    c.environ_base["HTTP_X_BUTLER_TOKEN"] = TEST_TOKEN
    check("세션+토큰 → 200", call(c, "GET", "/users/all").status_code == 200)


class _FakeChannel:
    async def send(self, content):
        return None


class _FakeClient:
    loop = None

    def is_closed(self):
        return False

    def is_ready(self):
        return True

    def get_channel(self, cid):
        return _FakeChannel()


def _fake_rcts(coro, loop):
    coro.close()
    return None


def test_a5_send():
    print("\nA5. /send 로컬 전용")
    body = {"channel_id": 1, "content": "hello"}
    with mock.patch.object(fa, "discord_client", _FakeClient()), \
            mock.patch.object(fa.asyncio, "run_coroutine_threadsafe", side_effect=_fake_rcts) as rc:
        r = call(anon(), "POST", "/send", body, TOK())
        check("로컬(127.0.0.1) + 헤더 없음 + 토큰 → 200 (전송 1회, mock)", r.status_code == 200 and rc.call_count == 1,
              f"{r.status_code} {rc.call_count}")
        r = call(anon(), "POST", "/send", body, TOK(), environ_overrides={"REMOTE_ADDR": "::1"})
        check("로컬(::1) + 토큰 → 200", r.status_code == 200, r.status_code)
        for hname, hval in [("CF-Ray", "abc-ICN"), ("Cf-Connecting-IP", "198.51.100.7"),
                            ("X-Forwarded-For", "198.51.100.7"), ("Cdn-Loop", "cloudflare"), ("cf-ray", "")]:
            hd = dict(TOK())
            hd[hname] = hval
            r = call(anon(), "POST", "/send", body, hd)
            check(f"로컬이지만 {hname} 헤더 있음(값 {'빈' if not hval else '있음'}) → 403", r.status_code == 403, r.status_code)
        for addr in ("203.0.113.5", "192.168.0.10", "10.0.0.2"):
            r = call(anon(), "POST", "/send", body, TOK(), environ_overrides={"REMOTE_ADDR": addr})
            check(f"원격 주소 {addr} + 올바른 토큰 → 403", r.status_code == 403, r.status_code)
        r = call(anon(), "POST", "/send", body, {"X-Butler-Token": "wrong"}, environ_overrides={"REMOTE_ADDR": "203.0.113.5"})
        check("원격 + 틀린 토큰 → 403 (원격에는 토큰 판정 결과를 주지 않음)", r.status_code == 403, r.status_code)
        r = call(anon(), "POST", "/send", body)
        check("로컬 + 토큰 없음 → 401", r.status_code == 401, r.status_code)
        r = call(admin(), "POST", "/send", body)
        check("로컬 + 세션만 → 401", r.status_code == 401, r.status_code)
        check("401/403 경로에서는 전송되지 않음(누적 2회만)", rc.call_count == 2, rc.call_count)


# 2026-10-08 사용자 결정: / 와 /trains 도 공개 (이전에는 PRIVATE_PAGES 로 admin 세션 필요)
PRIVATE_PAGES = []
PUBLIC_PAGES = ["/", "/trains", "/settlement", "/liquor", "/news"]


def test_a6_pages():
    print("\nA6. 페이지 접근")
    for p in ("/", "/trains"):
        r = anon().get(p)
        check(f"미로그인 {p} → 200 (2026-10-08 사용자 결정: 공개)", r.status_code == 200, f"{r.status_code} {r.headers.get('Location')}")
        r = admin().get(p)
        check(f"로그인 {p} → 200", r.status_code == 200, r.status_code)
    r = anon().get("/trains?x=1&y=2")
    check("쿼리 포함 /trains 미로그인 → 200 (2026-10-08 사용자 결정)", r.status_code == 200, r.status_code)
    for p in PUBLIC_PAGES:
        r = anon().get(p)
        check(f"미로그인 공개 페이지 {p} → 200", r.status_code == 200, r.status_code)


def test_a7_next():
    print("\nA7. 로그인 후 next 복귀 / 오픈 리다이렉트 방지")
    c = admin()
    r = c.get("/vwap/?next=/trains")
    check("세션 + /vwap/?next=/trains → 302 /trains", r.status_code == 302 and r.headers["Location"].endswith("/trains")
          and "//" not in r.headers["Location"].split("://", 1)[-1], r.headers.get("Location"))
    r = c.get("/vwap/?next=/trains%3Fx%3D1")
    check("쿼리가 붙은 next 복원", r.status_code == 302 and r.headers["Location"].endswith("/trains?x=1"), r.headers.get("Location"))
    for evil in ["//evil.example", "https://evil.example/", "/\\evil.example", "javascript:alert(1)", "evil", "/%0d%0aSet-Cookie:x=1"]:
        r = c.get("/vwap/", query_string={"next": evil})
        loc = r.headers.get("Location", "")
        ok = r.status_code == 200 if evil != "/%0d%0aSet-Cookie:x=1" else (r.status_code in (200, 302) and "\n" not in loc and "\r" not in loc)
        check(f"next={evil!r} → 외부로 보내지 않음", ok and "evil" not in loc, f"{r.status_code} {loc}")
    for raw in ["/vwap/?next=%2F%2Fevil.example", "/vwap/?next=%0D%0A"]:
        r = c.get(raw)
        check(f"{raw} → 대시보드(200), 리다이렉트 없음", r.status_code == 200, f"{r.status_code} {r.headers.get('Location')}")
    r = anon().get("/vwap/?next=/trains")
    check("미로그인 /vwap/?next=... → 로그인 화면 200", r.status_code == 200 and "password" in r.get_data(as_text=True).lower())


def test_a8_public_api():
    print("\nA8. 공개 페이지 API 권한")
    a, s = anon(), admin()
    # liquor
    check("liquor 조회 GET 미인증 → 200", call(a, "GET", "/api/liquor_purchases").status_code == 200)
    for m, p, j in [("POST", "/api/liquor_purchases", {"x": 1}), ("PUT", "/api/liquor_purchases", {"id": "x"}),
                    ("DELETE", "/api/liquor_purchases", {"id": "x"}), ("POST", "/api/liquor_purchases/merge_key", {}),
                    ("POST", "/api/liquor_purchases/dismiss_suggestion", {}), ("POST", "/api/liquor_purchases/import", None)]:
        r1, r2 = call(a, m, p, j), call(a, m, p, j, TOK())
        check(f"liquor {m} {p} 미인증·토큰만 → 401 (admin 세션 전용 유지)", (r1.status_code, r2.status_code) == (401, 401),
              f"{r1.status_code} {r2.status_code}")
    r = call(s, "POST", "/api/liquor_purchases", {"x": 1})
    check("liquor 쓰기 admin 세션 → 200 (토큰 헤더 없이)", r.status_code == 200, r.status_code)
    # news
    check("뉴스 그룹 조회 GET /api/keyword_groups 미인증 → 200", call(a, "GET", "/api/keyword_groups").status_code == 200)
    for m in ("POST", "DELETE"):
        r = call(a, m, "/api/keyword_groups", {"group_name": "G", "members": []})
        check(f"뉴스 그룹 {m} 미인증 → 401", r.status_code == 401, r.status_code)
    r = call(a, "POST", "/api/keyword_groups", {"group_name": "T", "members": []}, TOK())
    check("뉴스 그룹 POST 토큰 → 200", r.status_code == 200, r.status_code)
    check("키워드 목록 GET /api/keywords 미인증 → 401 (관리 모달 전용)", call(a, "GET", "/api/keywords").status_code == 401)
    # settlement
    good = {"id": None, "title": "모임", "participants": ["유진", "재승"],
            "items": [{"id": "1", "name": "1차", "amount": 10000, "payer": "유진", "ratios": {"유진": 1, "재승": 1}, "note": ""}]}
    r = call(a, "POST", "/api/settlements", good)
    sid = (r.get_json() or {}).get("id")
    check("정산 POST 미인증 → 200 (공개 쓰기 유지)", r.status_code == 200 and sid, r.status_code)
    r = call(a, "GET", "/api/settlements")
    check("정산 GET 미인증 → 200, 방금 저장 1건", r.status_code == 200 and len(r.get_json()["settlements"]) == 1)
    upd = dict(good, id=sid, title="모임2")
    r = call(a, "POST", "/api/settlements", upd)
    check("정산 수정(같은 id) → 200, 건수 유지", r.status_code == 200 and len(call(a, "GET", "/api/settlements").get_json()["settlements"]) == 1)
    for label, payload in [
        ("제목 <script>", dict(good, title="<img src=x onerror=alert(1)>")),
        ("이름에 큰따옴표", dict(good, participants=['a" onfocus="x'])),
        ("메모에 작은따옴표", dict(good, items=[dict(good["items"][0], note="it's")])),
        ("ratios 키에 <", dict(good, items=[dict(good["items"][0], ratios={"<b>": 1})])),
        ("백틱", dict(good, title="`x`")),
        ("& 엔티티", dict(good, title="&#39;")),
        ("역슬래시", dict(good, title="a\\b")),
        ("개행", dict(good, title="a\nb")),
    ]:
        r = call(a, "POST", "/api/settlements", payload)
        check(f"정산 위험 문자 거부: {label} → 400 invalid_chars", r.status_code == 400 and r.get_json()["reason"] == "invalid_chars",
              f"{r.status_code} {r.get_data(as_text=True)[:60]}")
    for label, payload, reason in [
        ("id 비숫자", dict(good, id="x'); alert(1);//"), "invalid_id"),
        ("participants 타입", dict(good, participants="a,b"), "invalid_body"),
        ("항목 201개", dict(good, items=[{"id": str(i)} for i in range(201)]), "too_large"),
        ("제목 101자", dict(good, title="가" * 101), "invalid_chars"),
        ("본문 비JSON", None, "invalid_body"),
    ]:
        r = call(a, "POST", "/api/settlements", payload)
        check(f"정산 검증: {label} → 400 {reason}", r.status_code == 400 and r.get_json()["reason"] == reason,
              f"{r.status_code} {r.get_data(as_text=True)[:60]}")
    r = a.post("/api/settlements", data=json.dumps(dict(good, title="a" * 10)) + " " * (70 * 1024), content_type="application/json")
    check("정산 본문 64KB 초과 → 413", r.status_code == 413, r.status_code)
    with mock.patch.object(fa, "SETTLEMENT_MAX_SAVED", 1):
        r = call(a, "POST", "/api/settlements", good)
        check("보관 상한 도달 시 신규 저장 → 409 too_many_saved (기존 수정은 허용)", r.status_code == 409
              and call(a, "POST", "/api/settlements", upd).status_code == 200, r.status_code)
    r = call(a, "DELETE", "/api/settlements", {"id": sid})
    check("정산 DELETE 미인증 → 200, 0건", r.status_code == 200 and call(a, "GET", "/api/settlements").get_json()["settlements"] == [])
    leftovers = [n for n in os.listdir(os.path.join(TMP_ROOT, "data")) if n.endswith(".tmp")]
    check("원자적 쓰기: 임시 파일(.tmp) 잔존 없음", leftovers == [], leftovers)
    with mock.patch.object(fa, "SETTLEMENT_PUBLIC_WRITE", False):
        r1, r2 = call(a, "POST", "/api/settlements", good), call(s, "POST", "/api/settlements", good)
        r3 = call(a, "GET", "/api/settlements")
        check("스위치 SETTLEMENT_PUBLIC_WRITE=False → 미인증 쓰기 401, 세션 200, 조회는 공개 200",
              (r1.status_code, r2.status_code, r3.status_code) == (401, 200, 200), f"{r1.status_code} {r2.status_code} {r3.status_code}")


def test_a9_no_token_in_html():
    print("\nA9. 페이지 HTML 에 토큰 0건")
    secrets = [("테스트 토큰", TEST_TOKEN)]
    if ENV_FILE_TOKEN:
        secrets.append(("로컬 .env 토큰", ENV_FILE_TOKEN))
    print(f"      (비교 대상 {len(secrets)}종 + 과거 코드 기본값(해시): {', '.join(n for n, _ in secrets)} — 값은 출력하지 않음)")
    pages = []
    for p in PRIVATE_PAGES + PUBLIC_PAGES + ["/vwap/"]:
        pages.append(("세션", p, admin().get(p)))
    # 2026-10-08 사용자 결정: / · /trains 가 공개가 됐으므로 미로그인 HTML 도 토큰 0건 검사 대상 (PUBLIC_PAGES 에 포함)
    for p in PUBLIC_PAGES + ["/vwap/"]:
        pages.append(("미로그인", p, anon().get(p)))
    total = 0
    for who, p, r in pages:
        html = r.get_data(as_text=True)
        hits = [n for n, v in secrets if v and v in html]
        if find_hash_matches(html, LEGACY_TOKEN_SHA256):
            hits.append("과거 코드 기본값(해시)")
        total += len(hits)
        check(f"{who} {p} ({r.status_code}, {len(html)}B) 토큰 0건", r.status_code == 200 and not hits, f"{r.status_code} {hits}")
    check("전체 페이지 토큰 노출 합계 0", total == 0, total)


def test_a10_vwap_login_cookie():
    print("\nA10. VWAP 로그인 쿠키 속성 / admin 경로 회귀")
    cfg = VwapConfigManager.load_config()
    cfg["admin_password_hash"] = VwapCrypto.hash_password("test-pw")
    VwapConfigManager.save_config(cfg)
    c = fa.app.test_client()
    r = c.post("/vwap/login", json={"password": "wrong"})
    check("틀린 비밀번호 → 401, 쿠키 없음", r.status_code == 401 and not r.headers.getlist("Set-Cookie"))
    r = c.post("/vwap/login", json={"password": "test-pw"})
    sc = " ".join(r.headers.getlist("Set-Cookie"))
    body = r.get_json() or {}
    check("로그인 200", r.status_code == 200 and body.get("status") == "success", r.status_code)
    check("Set-Cookie: HttpOnly", "HttpOnly" in sc)
    check("Set-Cookie: SameSite=Lax", "SameSite=Lax" in sc)
    check("Set-Cookie: Secure", "Secure" in sc)
    check("Set-Cookie: Path=/ , Max-Age=86400", "Path=/" in sc and "Max-Age=86400" in sc)
    check("응답 본문에 세션 토큰 없음(쿠키로만 전달)", "token" not in body, list(body))
    m = re.search(r"vwap_session=([^;]+)", sc)
    s = fa.app.test_client()
    s.set_cookie("vwap_session", m.group(1) if m else "")
    check("발급된 쿠키로 비공개 페이지 / 200", s.get("/").status_code == 200)
    check("발급된 쿠키로 대시보드 API 200", s.get("/api/system_status").status_code == 200)
    st = s.get("/vwap/api/status")
    check("발급된 쿠키로 /vwap/api/status → 401 아님", st.status_code != 401, st.status_code)
    check("미인증 /vwap/api/status → 401", anon().get("/vwap/api/status").status_code == 401)
    check("미인증 /vwap/api/shadow/status → 401", anon().get("/vwap/api/shadow/status").status_code == 401)
    bearer = anon().get("/vwap/api/status", headers={"Authorization": "Bearer " + VwapCrypto.generate_session_token("admin")})
    check("VWAP admin_required 의 Bearer 경로 유지 → 401 아님", bearer.status_code != 401, bearer.status_code)
    check("API 토큰만으로 /vwap/api/status → 401 (VWAP 은 세션 전용 유지)", anon().get("/vwap/api/status", headers=TOK()).status_code == 401)
    r = s.post("/vwap/logout")
    sc2 = " ".join(r.headers.getlist("Set-Cookie"))
    check("로그아웃: 쿠키 만료 + 같은 속성(HttpOnly/SameSite/Secure)", r.status_code == 200 and "vwap_session=;" in sc2
          and "HttpOnly" in sc2 and "SameSite=Lax" in sc2 and "Secure" in sc2, sc2[:120])


def test_a11_bind():
    print("\nA11. Flask 바인딩")
    src = inspect.getsource(fa.run_flask)
    check("run_flask 는 host='127.0.0.1'", "host='127.0.0.1'" in src and "0.0.0.0" not in src)


# ----------------------------------------------------------------------------------------------------
# A13/A14 (ADR-0011 D9·D10): 로그인 시도 제한, 비밀번호 해시 강화·자동 마이그레이션
# ----------------------------------------------------------------------------------------------------
import logging  # noqa: E402
import api.vwap_api as va  # noqa: E402
import core.vwap.crypto as crypto_mod  # noqa: E402
from api import login_throttle as lt_mod  # noqa: E402

AUTH_LOG = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        AUTH_LOG.append(record.getMessage())


_auth_logger = logging.getLogger("butler_auth")
_auth_logger.addHandler(_ListHandler())
_auth_logger.setLevel(logging.INFO)
_auth_logger.propagate = False


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def _cfg_path():
    return _cm_module.CONFIG_PATH


def _set_stored_hash(value):
    cfg = VwapConfigManager.load_config()
    cfg["admin_password_hash"] = value
    VwapConfigManager.save_config(cfg)


def _file_hash():
    with open(_cfg_path(), "r", encoding="utf-8") as f:
        return json.load(f).get("admin_password_hash")


def _login(pw, ip=None, client=None):
    c = client or fa.app.test_client()
    headers = {"Cf-Connecting-IP": ip} if ip else {}
    return c.post("/vwap/login", json={"password": pw}, headers=headers)


def _is_429(r, retry=None):
    body = r.get_json(silent=True) or {}
    ok = (r.status_code == 429 and set(body) == {"status", "reason", "retry_after"}
          and body.get("status") == "failed" and body.get("reason") == "too_many_attempts"
          and isinstance(body.get("retry_after"), int) and r.headers.get("Retry-After") == str(body.get("retry_after"))
          and not r.headers.getlist("Set-Cookie"))
    if retry is not None:
        ok = ok and body.get("retry_after") == retry
    return ok


def _with_throttle(clock, **kw):
    va.login_throttle = lt_mod.LoginThrottle(clock=clock, **kw)
    return va.login_throttle


def test_a13_login_throttle():
    print("\nA13. 로그인 시도 제한 (고정 시계)")
    orig = va.login_throttle
    try:
        _set_stored_hash(VwapCrypto.hash_password("test-pw"))
        clk = FakeClock()
        _with_throttle(clk)
        ip = "203.0.113.5"
        codes = [_login("wrong", ip).status_code for _ in range(5)]
        check("같은 키 1~5회째 실패 → 모두 401", codes == [401] * 5, codes)
        r = _login("wrong", ip)
        check("6회째 → 429 too_many_attempts, retry_after=900, Retry-After 헤더 일치, 쿠키 없음", _is_429(r, 900),
              (r.status_code, r.get_json(silent=True), r.headers.get("Retry-After")))
        r = _login("test-pw", ip)
        check("잠금 중 올바른 비밀번호도 429 (검사하지 않음 → 정답 여부 노출 없음)", _is_429(r), r.status_code)
        check("다른 클라이언트 키는 영향 없음 → 200", _login("test-pw", "203.0.113.6").status_code == 200)
        clk.advance(899)
        check("잠금 899초 경과 → 여전히 429, retry_after=1", _is_429(_login("test-pw", ip), 1))
        clk.advance(1)
        r = _login("test-pw", ip)
        check("잠금 900초 경과(해제 시점) → 올바른 비밀번호 200", r.status_code == 200, r.status_code)
        codes = [_login("wrong", ip).status_code for _ in range(4)] + [_login("test-pw", ip).status_code]
        check("성공 후 카운터 초기화: 다시 4회 실패 + 성공 → 잠기지 않고 200", codes == [401] * 4 + [200], codes)

        # 창(10분) 밖 실패는 세지 않는다
        _with_throttle(clk)
        ip2 = "198.51.100.7"
        a = [_login("wrong", ip2).status_code for _ in range(4)]
        clk.advance(601)
        b = [_login("wrong", ip2).status_code for _ in range(4)]
        check("10분 창: 4회 실패 → 601초 후 4회 실패 → 잠기지 않음(모두 401)", a + b == [401] * 8, a + b)
        r = _login("wrong", ip2)
        check("창 안 5회째 실패는 401, 그다음은 429", r.status_code == 401 and _is_429(_login("test-pw", ip2)), r.status_code)

        # Cf-Connecting-IP 가 없으면 remote_addr 기준 (test_client 는 127.0.0.1)
        check("client_key: 헤더 우선 / 없으면 remote_addr / 위험 문자 치환·64자 제한",
              lt_mod.client_key({"Cf-Connecting-IP": "1.2.3.4"}, "127.0.0.1") == "1.2.3.4"
              and lt_mod.client_key({}, "127.0.0.1") == "127.0.0.1"
              and lt_mod.client_key({"Cf-Connecting-IP": "<x>\n" + "9" * 100}, "127.0.0.1") == ("?x??" + "9" * 60))

        # 전역 상한: 키 5개 x 4회 = 20회 실패(각 키는 잠기지 않음)
        clk2 = FakeClock(5000.0)
        _with_throttle(clk2)
        for i in range(5):
            for _ in range(4):
                _login("wrong", f"192.0.2.{i}")
        r = _login("wrong", "192.0.2.50")
        check("전역 실패 20회 도달 후 새 키 → 429, retry_after=30", _is_429(r, 30),
              (r.status_code, r.get_json(silent=True)))
        clk2.advance(30)
        check("30초 후 1회는 검사됨(틀림 → 401)", _login("wrong", "192.0.2.51").status_code == 401)
        r = _login("test-pw", "192.0.2.52")
        check("직후 다른 키의 올바른 비밀번호 → 429 (전역 제한 중엔 30초에 1회만 검사)", _is_429(r, 30), r.status_code)
        r = fa.app.test_client().post("/vwap/login", json={"password": "test-pw"})
        check("같은 기기 직접 요청(헤더 없음, 127.0.0.1)은 전역 제한 예외 → 200 (소유자 복구 경로)", r.status_code == 200, r.status_code)
        clk2.advance(30)
        check("다음 30초 슬롯에서 올바른 비밀번호 → 200", _login("test-pw", "192.0.2.53").status_code == 200)
        clk2.advance(601)
        r1, r2 = _login("wrong", "192.0.2.60"), _login("wrong", "192.0.2.61")
        check("전역 창(10분) 경과 → 제한 해제, 연속 요청 모두 검사(401, 401)",
              (r1.status_code, r2.status_code) == (401, 401), (r1.status_code, r2.status_code))
        check("전역 제한 시작/해제가 로그에 남음",
              any("전역 제한 시작" in m for m in AUTH_LOG) and any("전역 제한 해제" in m for m in AUTH_LOG))

        # 위조 헤더로 키를 계속 바꿔도 비밀번호 검사 횟수는 전역 상한에서 막힌다
        clk3 = FakeClock(9000.0)
        _with_throttle(clk3)
        calls = {"n": 0}
        real_verify = VwapCrypto.verify_password.__func__

        def counting_verify(cls, pw, stored):
            calls["n"] += 1
            return real_verify(cls, pw, stored)
        with mock.patch.object(VwapCrypto, "verify_password", classmethod(counting_verify)):
            res = [_login("guess%d" % i, "10.0.%d.%d" % (i // 250, i % 250)).status_code for i in range(100)]
        check(f"키 100개 위조 순간 폭주 → 비밀번호 검사 {calls['n']}회(전역 상한 20 이하), 나머지 429",
              calls["n"] <= 20 and res.count(429) == 100 - calls["n"], (calls["n"], res.count(429)))

        # 동시성: 같은 키 가예약 — acquire 만 6번(결과 보고 전) → 6번째는 거부
        t = lt_mod.LoginThrottle(clock=FakeClock())
        acq = [t.acquire("k")[0] for _ in range(6)]
        check("결과 보고 전 같은 키 동시 6건 → 5건만 검사 허용", acq == [True] * 5 + [False], acq)

        # 메모리 상한
        t = lt_mod.LoginThrottle(clock=FakeClock(), MAX_KEYS=50, GLOBAL_THRESHOLD=10 ** 6)
        for i in range(300):
            t.acquire(f"key{i}")
        check("클라이언트 키 저장 상한(MAX_KEYS) 유지", len(t._keys) <= 50, len(t._keys))
        try:
            lt_mod.LoginThrottle(NOPE=1)
            bad_kw = False
        except TypeError:
            bad_kw = True
        check("알 수 없는 설정 이름은 TypeError", bad_kw)

        # 로그: 실패는 남고 비밀번호는 남지 않음
        AUTH_LOG.clear()
        _with_throttle(FakeClock())
        secret_guess = "pw-should-not-be-logged-" + hashlib.sha256(os.urandom(8)).hexdigest()[:8]
        for _ in range(5):
            _login(secret_guess, "203.0.113.99")
        joined = "\n".join(AUTH_LOG)
        check("실패 로그 기록(client=키, 횟수, 잠금)", "client=203.0.113.99" in joined and "5/5" in joined and "900초 잠금" in joined,
              AUTH_LOG[-1:] if AUTH_LOG else "")
        check("로그에 입력 비밀번호 없음", secret_guess not in joined)
        check("이벤트 파일(vwap_events_*.jsonl)에 로그인 기록 안 함",
              not any("로그인" in open(os.path.join(_cm_module.DATA_DIR, f), encoding="utf-8").read()
                      for f in os.listdir(_cm_module.DATA_DIR) if f.startswith("vwap_events_")))
    finally:
        va.login_throttle = orig
        orig.reset()


def test_a14_password_hash():
    print("\nA14. 비밀번호 해시 형식 / 마이그레이션 / 대체 경로")
    orig = va.login_throttle
    va.login_throttle = lt_mod.LoginThrottle(clock=FakeClock(), PER_KEY_MAX=10 ** 6, GLOBAL_THRESHOLD=10 ** 6)
    try:
        VwapCrypto._reset_scrypt_probe()
        h1 = VwapCrypto.hash_password("test-pw")
        h2 = VwapCrypto.hash_password("test-pw")
        check("새 형식: scrypt$n=16384,r=8,p=2$<salt>$<hash>", re.match(r"^scrypt\$n=16384,r=8,p=2\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}$", h1 or "") is not None, (h1 or "")[:24])
        check("같은 비밀번호도 salt 가 달라 해시가 다름", h1 != h2)
        check("새 형식 검증 성공 → (True, False)", VwapCrypto.verify_password("test-pw", h1) == (True, False))
        check("새 형식 검증 실패 → (False, False)", VwapCrypto.verify_password("test-px", h1) == (False, False))
        legacy = VwapCrypto.legacy_hash_password("test-pw")
        check("옛 형식 검증 성공 → (True, 재해시 필요 True)", VwapCrypto.verify_password("test-pw", legacy) == (True, True))
        check("옛 형식 검증 실패 → (False, False)", VwapCrypto.verify_password("nope", legacy) == (False, False))
        check("빈/비문자열/과대 입력은 해시하지 않음(\"\")",
              VwapCrypto.hash_password("") == "" and VwapCrypto.hash_password(123) == "" and VwapCrypto.hash_password("x" * 1025) == "")
        src = inspect.getsource(VwapCrypto.verify_password)
        check("비교는 hmac.compare_digest (== 비교 없음)", src.count("compare_digest") >= 3 and "calc ==" not in src and "== expected" not in src)

        # 잘못된 형식 문자열
        salt = VwapCrypto._b64e(b"s" * 16)
        dk = VwapCrypto._b64e(b"d" * 32)
        bad = ["", None, 123, ["x"], "$$$", "scrypt$", "scrypt$n=16384,r=8$" + salt + "$" + dk,
               "scrypt$n=3,r=8,p=1$" + salt + "$" + dk, "scrypt$n=1073741824,r=8,p=1$" + salt + "$" + dk,
               "scrypt$n=16384,r=8,p=2$!!!!$????", "scrypt$n=16384,r=8,p=2,p=2$" + salt + "$" + dk,
               "scrypt$n=-1,r=8,p=1$" + salt + "$" + dk, "scrypt$n=16384,r=99,p=1$" + salt + "$" + dk,
               "pbkdf2_sha256$i=1$" + salt + "$" + dk, "pbkdf2_sha256$i=abc$" + salt + "$" + dk,
               "pbkdf2_sha256$i=99999999999$" + salt + "$" + dk, "pbkdf2_sha256$i=400000$" + salt + "$" + "QQ",
               "md5$x=1$" + salt + "$" + dk, "A" * 64, "a" * 63, "z" * 100000, "scrypt$n=16384,r=8,p=2$" + salt + "$" + dk + "$x"]
        results = []
        for b in bad:
            try:
                results.append(VwapCrypto.verify_password("test-pw", b))
            except Exception as e:  # noqa: BLE001
                results.append(("EXC", type(e).__name__))
        check(f"잘못된 형식 문자열 {len(bad)}종 → 예외 없이 (False, False)", all(x == (False, False) for x in results),
              [x for x in results if x != (False, False)])
        _set_stored_hash("scrypt$n=16384,r=8,p=2$!!!!$????")
        r = _login("test-pw")
        check("저장값이 손상돼도 로그인은 500 이 아닌 401", r.status_code == 401, r.status_code)

        # 비문자열 비밀번호 / 비JSON 본문
        _set_stored_hash(h1)
        r1 = fa.app.test_client().post("/vwap/login", json={"password": 123})
        r2 = fa.app.test_client().post("/vwap/login", json={"password": ["test-pw"]})
        r3 = fa.app.test_client().post("/vwap/login", data="password=test-pw", content_type="text/plain")
        r4 = fa.app.test_client().post("/vwap/login", json=["test-pw"])
        check("비문자열 비밀번호·비JSON 본문·배열 본문 → 401", [r.status_code for r in (r1, r2, r3, r4)] == [401] * 4,
              [r.status_code for r in (r1, r2, r3, r4)])

        # 자동 마이그레이션: 파일에 옛 형식
        _set_stored_hash(legacy)
        r = _login("test-pw")
        migrated = _file_hash()
        check("옛 형식으로 로그인 200", r.status_code == 200, r.status_code)
        check("로그인 직후 설정 파일 해시가 새 형식(scrypt$)으로 바뀜", isinstance(migrated, str) and migrated.startswith("scrypt$"), (migrated or "")[:10])
        check("바뀐 해시로 같은 비밀번호 검증 성공", VwapCrypto.verify_password("test-pw", migrated) == (True, False))
        check("다시 로그인 200, 해시는 그대로(재마이그레이션 없음)", _login("test-pw").status_code == 200 and _file_hash() == migrated)
        check("틀린 비밀번호는 401, 파일 그대로", _login("test-px").status_code == 401 and _file_hash() == migrated)
        leftovers = [f for f in os.listdir(os.path.dirname(_cfg_path())) if f.endswith(".tmp")]
        check("원자적 저장: 임시 파일 잔여 없음", leftovers == [], leftovers)
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            whole = json.load(f)
        check("마이그레이션은 해시 한 키만 바꿈(다른 설정 보존)", whole.get("virtual_1_ticker") == VwapConfigManager.load_config().get("virtual_1_ticker") and len(whole) > 10, len(whole))

        # 틀린 비밀번호로는 마이그레이션 없음
        _set_stored_hash(legacy)
        _login("test-px")
        check("옛 형식 + 틀린 비밀번호 → 파일 그대로(옛 형식)", _file_hash() == legacy)

        # 파일에 해시가 비어 있음 → .env 대체 해시(옛 형식). (정정 2026-10-07: 코드 기본 비밀번호 대체는 제거 — A16 참조)
        with mock.patch.dict(os.environ, {"VWAP_ADMIN_PASSWORD": "env-pw"}):
            _set_stored_hash("")
            check("파일 해시 비어 있고 .env 있으면 load_config 는 옛 형식 대체값", VwapCrypto._LEGACY_RE.match(VwapConfigManager.load_config()["admin_password_hash"]) is not None)
            r = _login("env-pw")
            check(".env VWAP_ADMIN_PASSWORD 대체값으로 로그인 200 → 파일에 새 형식 기록", r.status_code == 200 and (_file_hash() or "").startswith("scrypt$"), (r.status_code, (_file_hash() or "")[:8]))

        # load_config 는 느린 KDF 를 돌리지 않는다
        n = {"k": 0}
        with mock.patch.object(VwapCrypto, "_scrypt", classmethod(lambda cls, *a: n.__setitem__("k", n["k"] + 1) or b"")), \
                mock.patch.object(VwapCrypto, "_pbkdf2", staticmethod(lambda *a: n.__setitem__("k", n["k"] + 1) or b"")):
            for _ in range(5):
                VwapConfigManager.load_config()
        check("load_config 5회 호출 중 KDF 호출 0회", n["k"] == 0, n["k"])

        # 손상/없는 설정 파일 → (.env 가 있으면) 로그인은 되지만 파일을 덮어쓰지 않음. .env 도 없으면 A16 에서 401 확인
        with mock.patch.dict(os.environ, {"VWAP_ADMIN_PASSWORD": "env-pw"}):
            with open(_cfg_path(), "w", encoding="utf-8") as f:
                f.write("{broken")
            r = _login("env-pw")
            with open(_cfg_path(), "r", encoding="utf-8") as f:
                still = f.read()
            check("손상된 설정 파일: .env 대체값으로 로그인 200, 파일은 덮어쓰지 않음", r.status_code == 200 and still == "{broken", (r.status_code, still[:10]))
            os.remove(_cfg_path())
            r = _login("env-pw")
            check("설정 파일 없음: 로그인 200, 파일을 새로 만들지 않음", r.status_code == 200 and not os.path.exists(_cfg_path()))
        check("update_admin_password_hash: 그 사이 해시가 바뀌었으면 덮어쓰지 않음", (
            VwapConfigManager.save_config({"admin_password_hash": h1}) is None
            and VwapConfigManager.update_admin_password_hash(h2, expected_old=legacy) is False and _file_hash() == h1))
        _set_stored_hash(h1)

        # 비밀번호 변경 경로
        r = admin().post("/vwap/api/config", json={"new_admin_password": "new-pw-1"})
        nh = _file_hash()
        check("/vwap/api/config 비밀번호 변경 → 새 형식 저장", r.status_code == 200 and (nh or "").startswith("scrypt$"), (r.status_code, (nh or "")[:8]))
        check("변경한 비밀번호로 로그인 200, 옛 비밀번호 401", _login("new-pw-1").status_code == 200 and _login("test-pw").status_code == 401)
        r = admin().post("/vwap/api/config", json={"new_admin_password": 12345})
        check("비문자열 새 비밀번호 → 400, 해시 그대로", r.status_code == 400 and _file_hash() == nh, r.status_code)

        # scrypt 를 쓸 수 없는 환경 흉내 ①: hashlib.scrypt 없음
        with mock.patch.object(hashlib, "scrypt", None):
            VwapCrypto._reset_scrypt_probe()
            pb = VwapCrypto.hash_password("test-pw")
            check("scrypt 없음 → pbkdf2_sha256$i=400000$... 로 저장", re.match(r"^pbkdf2_sha256\$i=400000\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}$", pb or "") is not None, (pb or "")[:20])
            check("scrypt 없음: pbkdf2 검증 성공 → (True, False)", VwapCrypto.verify_password("test-pw", pb) == (True, False))
            check("scrypt 없음: pbkdf2 틀린 비밀번호 → (False, False)", VwapCrypto.verify_password("x", pb) == (False, False))
            check("scrypt 없음: 저장값이 scrypt 면 예외 없이 (False, False)", VwapCrypto.verify_password("test-pw", h1) == (False, False))
            check("scrypt 없음: 옛 형식 → (True, True)", VwapCrypto.verify_password("test-pw", legacy) == (True, True))
            _set_stored_hash(legacy)
            r = _login("test-pw")
            check("scrypt 없음: 옛 형식 로그인 → 200, pbkdf2 로 마이그레이션", r.status_code == 200 and (_file_hash() or "").startswith("pbkdf2_sha256$"))
        # ②: 함수는 있지만 호출이 실패(OpenSSL 미지원 빌드)
        def _raising(*a, **k):
            raise ValueError("unsupported")
        with mock.patch.object(hashlib, "scrypt", _raising):
            VwapCrypto._reset_scrypt_probe()
            check("scrypt 호출 실패 → 감지 False, pbkdf2 로 저장", not VwapCrypto.scrypt_available() and VwapCrypto.hash_password("a").startswith("pbkdf2_sha256$"))
        # ③: 작은 값은 되지만 실제 파라미터에서 실패(메모리 한도)
        real_scrypt = hashlib.scrypt

        def _small_only(pw, *, salt, n, r, p, dklen=64, maxmem=0):
            if n > 16:
                raise ValueError("memory limit exceeded")
            return real_scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=dklen)
        with mock.patch.object(hashlib, "scrypt", _small_only):
            VwapCrypto._reset_scrypt_probe()
            hh = VwapCrypto.hash_password("a")
            check("scrypt 실제 파라미터 실패 → 그 자리에서 pbkdf2 로 대체, 이후 감지 False",
                  hh.startswith("pbkdf2_sha256$") and VwapCrypto._scrypt_ok is False)
        VwapCrypto._reset_scrypt_probe()
        # scrypt 가 다시 가능해지면 pbkdf2 저장값은 다음 로그인 때 scrypt 로 올린다
        check("scrypt 가능 환경에서 pbkdf2 저장값 → (True, 재해시 True)", VwapCrypto.verify_password("test-pw", pb) == (True, True))
        _set_stored_hash(pb)
        r = _login("test-pw")
        check("pbkdf2 → scrypt 업그레이드 마이그레이션", r.status_code == 200 and (_file_hash() or "").startswith("scrypt$"))

        # 로컬 소요 시간(참고용, S9 는 이보다 6~8배 느리다고 가정)
        import time as _t
        t0 = _t.perf_counter(); VwapCrypto.verify_password("test-pw", h1); t_s = _t.perf_counter() - t0
        t0 = _t.perf_counter(); VwapCrypto.verify_password("test-pw", pb); t_p = _t.perf_counter() - t0
        print(f"      (참고: 이 PC 에서 scrypt 검증 {t_s:.3f}s, pbkdf2 검증 {t_p:.3f}s)")
        check("이 PC 에서 검증 1회 1초 미만", t_s < 1 and t_p < 1, (t_s, t_p))
    finally:
        VwapCrypto._reset_scrypt_probe()
        va.login_throttle = orig
        orig.reset()
        _set_stored_hash(VwapCrypto.hash_password("test-pw"))


def test_a16_no_default_password():
    """(ADR-0011 정정 2026-10-07) 코드 기본 비밀번호 대체 제거 — 해시도 .env 도 없으면 로그인 불가(fail-closed)."""
    print("\nA16. 기본 비밀번호 대체 제거 (해시 없음 + .env 없음 → 로그인 불가)")
    orig = va.login_throttle
    va.login_throttle = lt_mod.LoginThrottle(clock=FakeClock(), PER_KEY_MAX=10 ** 6, GLOBAL_THRESHOLD=10 ** 6)
    os.environ.pop("VWAP_ADMIN_PASSWORD", None)
    public_default = "admin" + "1234"  # 과거 코드의 공개 기본값(이제 어떤 경로로도 통하면 안 됨)

    def _warns():
        return [m for m in AUTH_LOG if "VWAP_ADMIN_PASSWORD" in m and "비활성화" in m]
    try:
        check("src: config_manager 에 기본 비밀번호 문자열 없음",
              public_default not in inspect.getsource(_cm_module))
        check("verify_password: 빈 해시는 어떤 입력과도 불일치(빈 비밀번호 포함)",
              all(VwapCrypto.verify_password(p, s) == (False, False)
                  for p in ("", public_default, " ", None) for s in ("", None, " ")))

        # 해시 없음 + .env 없음
        _set_stored_hash("")
        VwapConfigManager._admin_pw_unset_warned = False
        AUTH_LOG.clear()
        cfg = VwapConfigManager.load_config()
        check("load_config: 해시 없음 + .env 없음 → admin_password_hash == \"\"", cfg.get("admin_password_hash") == "",
              repr(cfg.get("admin_password_hash"))[:12])
        r1 = _login(public_default)
        r2 = _login("")
        r3 = fa.app.test_client().post("/vwap/login", json={})
        b1 = r1.get_json(silent=True) or {}
        check("공개 기본 비밀번호로 로그인 → 401", r1.status_code == 401, r1.status_code)
        check("빈 비밀번호·password 키 없음 → 401", r2.status_code == 401 and r3.status_code == 401, (r2.status_code, r3.status_code))
        check("응답은 틀린 비밀번호와 동일(invalid_password, '미설정' 비노출), 쿠키 미발급",
              b1 == {"status": "failed", "reason": "invalid_password"} and not r1.headers.getlist("Set-Cookie")
              and (r2.get_json(silent=True) or {}) == b1, b1)
        check("로그인 실패가 설정 파일 해시를 바꾸지 않음(여전히 \"\")", _file_hash() == "", repr(_file_hash())[:12])

        # 경고 로그: 봇 주기처럼 여러 번 불려도 1회
        for _ in range(10):
            VwapConfigManager.load_config()
        _login(public_default)
        w = _warns()
        check("미설정 경고 로그 1회(load_config 11회 + 로그인 4회)", len(w) == 1, len(w))
        check("경고 문구: .env VWAP_ADMIN_PASSWORD 설정 후 재시작 안내", w and "재시작" in w[0], w[:1])

        # 공백뿐인 .env 값도 미설정으로 취급
        with mock.patch.dict(os.environ, {"VWAP_ADMIN_PASSWORD": "   "}):
            check(".env 값이 공백뿐 → 미설정(\"\"), 공백 비밀번호 로그인 401",
                  VwapConfigManager.load_config()["admin_password_hash"] == "" and _login("   ").status_code == 401)
        # 파일 해시가 null/비문자열
        with open(_cfg_path(), "r", encoding="utf-8") as f:
            whole = json.load(f)
        for bad_val in (None, 0, []):
            whole["admin_password_hash"] = bad_val
            with open(_cfg_path(), "w", encoding="utf-8") as f:
                json.dump(whole, f)
            ok_bad = VwapConfigManager.load_config()["admin_password_hash"] == "" and _login(public_default).status_code == 401
            check(f"파일 해시 {bad_val!r} → \"\" 로 정규화, 기본 비밀번호 401", ok_bad)
        _set_stored_hash("")

        # 미설정 상태에서 설정 저장(대시보드) → 기본값 해시가 파일에 기록되지 않음
        r = admin().post("/vwap/api/config", json={"virtual_1_ticker": "AAPL"})
        check("미설정 상태 설정 저장 200, 파일 해시는 여전히 \"\"(기본값 해시 미기록)",
              r.status_code == 200 and _file_hash() == "" and _login(public_default).status_code == 401, (r.status_code, repr(_file_hash())[:12]))

        # .env 설정 → 그 비밀번호로 로그인 성공, 이후 마이그레이션
        with mock.patch.dict(os.environ, {"VWAP_ADMIN_PASSWORD": "env-recover-pw"}):
            AUTH_LOG.clear()
            cfg = VwapConfigManager.load_config()
            check(".env 설정 → load_config 는 옛 형식 대체값", VwapCrypto._LEGACY_RE.match(cfg["admin_password_hash"]) is not None)
            check(".env 상태에서도 기본 비밀번호·빈 비밀번호 401", _login(public_default).status_code == 401 and _login("").status_code == 401)
            r = _login("env-recover-pw")
            mig = _file_hash()
            check(".env 비밀번호로 로그인 200 → 파일에 새 형식(scrypt$) 기록", r.status_code == 200 and (mig or "").startswith("scrypt$"),
                  (r.status_code, (mig or "")[:8]))
            check(".env 설정 상태에서는 미설정 경고 없음", _warns() == [], len(_warns()))
        # .env 를 지워도 파일에 기록된 해시로 계속 로그인됨
        check(".env 제거 후에도 마이그레이션된 해시로 로그인 200, 기본 비밀번호 401",
              _login("env-recover-pw").status_code == 200 and _login(public_default).status_code == 401 and _file_hash() == mig)

        # 미설정 → 설정 → 다시 미설정: 전환 때 한 번 더 경고(매 주기 반복은 아님)
        AUTH_LOG.clear()
        _set_stored_hash("")
        for _ in range(5):
            VwapConfigManager.load_config()
        check("다시 미설정으로 바뀌면 경고 1회(전환당 1회)", len(_warns()) == 1, len(_warns()))

        # 미설정 상태에서 (기존 세션으로) 비밀번호 변경 → 그 비밀번호로 로그인
        r = admin().post("/vwap/api/config", json={"new_admin_password": "changed-pw"})
        check("미설정 상태에서 new_admin_password 변경 → 새 형식 저장, 로그인 200, 기본 비밀번호 401",
              r.status_code == 200 and (_file_hash() or "").startswith("scrypt$")
              and _login("changed-pw").status_code == 200 and _login(public_default).status_code == 401)

        # 정상 해시가 있는 기존 경로(운영 S9 와 같은 상태): .env 값과 무관하게 파일 해시가 이긴다, 경고 없음
        normal = VwapCrypto.hash_password("normal-pw")
        _set_stored_hash(normal)
        AUTH_LOG.clear()
        with mock.patch.dict(os.environ, {"VWAP_ADMIN_PASSWORD": "env-other"}):
            check("정상 해시 + .env 다른 값: load_config 해시 = 파일 해시", VwapConfigManager.load_config()["admin_password_hash"] == normal)
            ra, rb, rc = _login("normal-pw"), _login("env-other"), _login(public_default)
            check("정상 해시: 파일 비밀번호 200, .env 값 401, 기본 비밀번호 401",
                  (ra.status_code, rb.status_code, rc.status_code) == (200, 401, 401), (ra.status_code, rb.status_code, rc.status_code))
        check("정상 해시: 로그인 후에도 파일 해시 그대로(재해시 없음)", _file_hash() == normal)
        ra, rc = _login("normal-pw"), _login(public_default)
        check("정상 해시 + .env 없음: 200 / 기본 비밀번호 401", (ra.status_code, rc.status_code) == (200, 401))
        check("정상 해시 상태에서는 미설정 경고 없음", _warns() == [], len(_warns()))
        legacy_ok = VwapCrypto.legacy_hash_password("normal-pw")
        _set_stored_hash(legacy_ok)
        r = _login("normal-pw")
        check("정상 옛 형식 해시: 로그인 200 → 새 형식 마이그레이션(기존 경로 그대로)",
              r.status_code == 200 and (_file_hash() or "").startswith("scrypt$"))
    finally:
        os.environ.pop("VWAP_ADMIN_PASSWORD", None)
        va.login_throttle = orig
        orig.reset()
        _set_stored_hash(VwapCrypto.hash_password("test-pw"))


def test_a15_settlement_hardening():
    """QA M1·M2·m4·m5: chunked 상한 우회, NaN/Infinity, 깊은 중첩, 유니코드 숫자 id."""
    import io
    print("\nA15. 정산 쓰기 보강 (chunked 상한, NaN/Infinity, 깊은 중첩, id)")
    a = anon()
    good = {"id": None, "title": "모임", "participants": ["유진"], "items": []}
    path = "/api/settlements"
    before = len(call(a, "GET", path).get_json()["settlements"])

    # M1: Content-Length 없는 chunked 대용량 → 413, 저장 안 됨
    big = json.dumps(dict(good, title="a" * 10)).encode() + b" " * (2 * 1024 * 1024)
    r = a.post(path, input_stream=io.BytesIO(big), content_type="application/json")
    check("chunked(Content-Length 없음) 2MB → 413", r.status_code == 413, r.status_code)
    r = a.post(path, input_stream=io.BytesIO(json.dumps(good).encode()), content_type="application/json")
    check("chunked 소형 본문은 정상 저장 → 200", r.status_code == 200, f"{r.status_code} {r.get_data(as_text=True)[:60]}")
    sid = (r.get_json() or {}).get("id")
    call(a, "DELETE", path, {"id": sid})
    check("chunked 거부 후 저장 건수 변화 없음", len(call(a, "GET", path).get_json()["settlements"]) == before)

    # M2: NaN / Infinity / 1e999 → 400
    for label, raw in [("NaN", '{"title":"t","items":[{"amount":NaN}]}'),
                       ("Infinity", '{"title":"t","items":[{"amount":Infinity}]}'),
                       ("-Infinity", '{"title":"t","items":[{"amount":-Infinity}]}'),
                       ("1e999", '{"title":"t","items":[{"amount":1e999}]}'),
                       ("-1e999", '{"title":"t","items":[{"amount":-1e999}]}')]:
        r = a.post(path, data=raw, content_type="application/json")
        check(f"정산 {label} → 400", r.status_code == 400, f"{r.status_code} {r.get_data(as_text=True)[:60]}")
    check("NaN 거부 후 저장 건수 변화 없음", len(call(a, "GET", path).get_json()["settlements"]) == before)
    check("_settlement_value_ok 는 비유한 float 거부", not fa._settlement_value_ok(float("nan"))
          and not fa._settlement_value_ok(float("inf")) and fa._settlement_value_ok(1.5))

    # M2: 이미 저장된 비정상 데이터가 있어도 GET 은 유효한 JSON
    path_file = fa._settlement_file()
    os.makedirs(os.path.dirname(path_file), exist_ok=True)
    now = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path_file, "w", encoding="utf-8") as f:
        f.write('{"settlements": [{"id": "1", "title": "bad", "participants": [], "items": [{"amount": NaN}], "created_at": "%s"},'
                ' {"id": "2", "title": "inf", "participants": [], "items": [{"amount": Infinity}], "created_at": "%s"},'
                ' "not-a-dict",'
                ' {"id": "3", "title": "ok", "participants": [], "items": [{"amount": 5}], "created_at": "%s"}]}' % (now, now, now))
    r = call(a, "GET", path)
    body = r.get_data(as_text=True)
    try:
        parsed = json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except ValueError as e:
        parsed = None
        body = "invalid: %s" % e
    check("비정상 저장 데이터가 있어도 GET → 200 + 엄격한 JSON(NaN 없음)", r.status_code == 200 and parsed is not None, body[:80])
    check("비정상 레코드는 제외, 정상 레코드는 유지", parsed is not None and [x["id"] for x in parsed["settlements"]] == ["3"],
          parsed)
    r = call(a, "POST", path, good)
    check("비정상 데이터가 있어도 POST → 200", r.status_code == 200, r.status_code)
    with open(path_file, encoding="utf-8") as f:
        raw = f.read()
    check("재저장된 파일에 NaN/Infinity 없음", "NaN" not in raw and "Infinity" not in raw)
    with open(path_file, "w", encoding="utf-8") as f:
        f.write('{"settlements": []}')

    # m4: 깊은 중첩 → 400 (500 아님)
    for depth in (5000, 30000):
        deep = '{"title":"t","items":[' + "[" * depth + "]" * depth + "]}"
        r = a.post(path, data=deep, content_type="application/json")
        check(f"깊이 {depth} 중첩 JSON → 400", r.status_code == 400, r.status_code)
    r = a.delete(path, data="[" * 5000 + "]" * 5000, content_type="application/json")
    check("DELETE 깊은 중첩 → 400", r.status_code == 400, r.status_code)

    # m5: 유니코드 숫자 id
    for label, bad_id in [("아랍-인도 숫자", "١٢٣"), ("위첨자", "²"), ("전각 숫자", "１２")]:
        r = call(a, "POST", path, dict(good, id=bad_id))
        check(f"유니코드 숫자 id({label}) → 400 invalid_id",
              r.status_code == 400 and (r.get_json() or {}).get("reason") == "invalid_id", f"{r.status_code} {r.get_data(as_text=True)[:60]}")
    check("ASCII 숫자 id 는 여전히 통과(검증 단계)", fa._validate_settlement_payload(dict(good, id="123"))[0])


def main():
    print("=" * 70)
    print(" Butler 인증 경계(ADR-0011) 검증 — 네트워크 없음")
    print("=" * 70)
    tests = [test_a1_fail_closed, test_a2_token, test_a3_session, test_a4_token_only, test_a5_send,
             test_a6_pages, test_a7_next, test_a8_public_api, test_a9_no_token_in_html,
             test_a10_vwap_login_cookie, test_a11_bind, test_a13_login_throttle, test_a14_password_hash,
             test_a15_settlement_hardening, test_a16_no_default_password]
    with _Patches():
        for fn in tests:
            try:
                set_token(TEST_TOKEN)
                fn()
            except Exception:
                import traceback
                global FAIL
                FAIL += 1
                print(f"  [FAIL] {fn.__name__} 실행 중 예외")
                traceback.print_exc()

    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    after = _hash_tree(REAL_DATA_DIR) if os.path.isdir(REAL_DATA_DIR) else {}
    print("\nA12. 네트워크 / 운영 data/")
    check("외부 네트워크 호출 0건 (requests 차단 가드)", NET_CALLS == [], NET_CALLS)
    check(f"운영 data/ 해시 전후 동일 (파일 {len(DATA_BEFORE)}개, digest {_digest(DATA_BEFORE)} → {_digest(after)})",
          after == DATA_BEFORE, [k for k in set(after) | set(DATA_BEFORE) if after.get(k) != DATA_BEFORE.get(k)])
    print("\n" + "=" * 70)
    print(f" 결과: {PASS}/{PASS + FAIL} 통과")
    print("=" * 70)
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
