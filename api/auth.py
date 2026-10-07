"""
Butler 대시보드/API 인증 경계 (ADR-0011).

인증 수단은 두 가지다.
  1. admin 세션 쿠키(`vwap_session`) — 사람이 브라우저로 쓸 때. VWAP 로그인(/vwap/)에서 발급.
  2. API 토큰(`X-Butler-Token` 헤더) — 기계(구독 앱, 터널 알림, 로컬 스크립트)가 쓸 때.
     값은 .env 의 BUTLER_API_TOKEN 하나뿐이며, 코드에 기본값을 두지 않는다(fail-closed).
     비어 있으면 어떤 토큰도 통과하지 못한다.

데코레이터
  - session_or_token : 대시보드 API. 세션 쿠키 또는 토큰.
  - token_only       : 기계 전용 API(/users/*, /subscriptions/*). 세션만으로는 거부.
  - local_send_only  : /send. 같은 기기에서 직접 온 요청(127.0.0.1/::1, 터널 헤더 없음) + 토큰.
  - page_login_required : 비공개 HTML 페이지. 세션이 없으면 /vwap/?next=<원래 경로> 로 보낸다.
"""
import hmac
import os
from functools import wraps
from urllib.parse import quote, urlsplit

from flask import jsonify, redirect, request

from core.vwap.crypto import VwapCrypto

SESSION_COOKIE = "vwap_session"
SESSION_MAX_AGE = 86400  # 24시간 (VWAP admin_required 와 동일)
TOKEN_HEADER = "X-Butler-Token"
LOGIN_PATH = "/vwap/"

# 같은 기기의 프로세스가 직접 붙었을 때만 /send 를 허용한다.
LOCAL_ADDRS = frozenset({"127.0.0.1", "::1"})
# cloudflared 는 127.0.0.1 로 접속하므로 원격 주소만으로는 터널 요청을 구분할 수 없다.
# Cloudflare 엣지가 붙이는 헤더가 하나라도 있으면 '외부에서 터널을 거쳐 온 요청'으로 본다.
PROXY_HEADERS = ("Cf-Connecting-IP", "CF-Ray", "Cdn-Loop", "X-Forwarded-For")


def configured_token():
    """요청 시점에 환경변수에서 읽는다(기본값 없음). 비어 있으면 '' — 토큰 인증 전면 거부."""
    return (os.environ.get("BUTLER_API_TOKEN") or "").strip()


def has_valid_token():
    expected = configured_token()
    if not expected:
        return False
    supplied = request.headers.get(TOKEN_HEADER) or ""
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def has_admin_session():
    token = request.cookies.get(SESSION_COOKIE, "")
    return VwapCrypto.verify_session_token(token, max_age_seconds=SESSION_MAX_AGE)


def is_direct_local_request():
    if request.remote_addr not in LOCAL_ADDRS:
        return False
    return not any(h in request.headers for h in PROXY_HEADERS)


def _unauthorized():
    return jsonify({"status": "failed", "reason": "unauthorized"}), 401


def session_or_token(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if has_admin_session() or has_valid_token():
            return f(*args, **kwargs)
        return _unauthorized()
    return decorated


def token_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if has_valid_token():
            return f(*args, **kwargs)
        return _unauthorized()
    return decorated


def local_send_only(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        # 위치 검사를 먼저 한다: 외부 요청에는 토큰이 맞는지 여부조차 알려주지 않는다(항상 403).
        if not is_direct_local_request():
            return jsonify({"status": "failed", "reason": "forbidden"}), 403
        if not has_valid_token():
            return _unauthorized()
        return f(*args, **kwargs)
    return decorated


def safe_next(value):
    """로그인 후 돌아갈 경로를 같은 사이트의 상대 경로로만 제한한다(오픈 리다이렉트 방지)."""
    if not value or not isinstance(value, str) or len(value) > 512:
        return None
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def login_redirect():
    target = request.path
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "replace")
    return redirect(LOGIN_PATH + "?next=" + quote(target, safe="/"))


def page_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if has_admin_session():
            return f(*args, **kwargs)
        return login_redirect()
    return decorated


def set_session_cookie(response, value):
    response.set_cookie(SESSION_COOKIE, value, max_age=SESSION_MAX_AGE, path="/",
                        httponly=True, samesite="Lax", secure=True)


def clear_session_cookie(response):
    response.set_cookie(SESSION_COOKIE, "", expires=0, path="/",
                        httponly=True, samesite="Lax", secure=True)
