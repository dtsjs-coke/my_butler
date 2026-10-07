"""tunnel_manager / butler_agent 토큰 처리 테스트 (ADR-0011 D8).
네트워크·subprocess·실제 파일 쓰기 없이 mock으로만 검증한다.
실행: python netguard_run.py scripts/test_tunnel_butler_token.py
"""
import os
import sys
import tempfile
import types
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAKE_TOKEN = "unit-test-fake-token-xyz"
PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")


import requests  # noqa: E402
from utils import tunnel_manager as tm  # noqa: E402


class Resp:
    status_code = 200
    text = "ok"


def env_with(**kw):
    base = {k: v for k, v in os.environ.items() if k != "BUTLER_API_TOKEN"}
    base.update(kw)
    return mock.patch.dict(os.environ, base, clear=True)


print("== notify_via_butler ==")
with env_with(), mock.patch.object(requests, "post", return_value=Resp()) as post, \
        mock.patch.object(tm.time, "sleep"):
    r = tm.notify_via_butler("hi", channel_id=123)
    check("env 비면 False", r is False)
    check("env 비면 requests.post 0회", post.call_count == 0)
with env_with(BUTLER_API_TOKEN="   "), mock.patch.object(requests, "post", return_value=Resp()) as post:
    r = tm.notify_via_butler("hi", channel_id=123)
    check("공백뿐이면 post 0회", post.call_count == 0 and r is False)
with env_with(BUTLER_API_TOKEN=FAKE_TOKEN), mock.patch.object(requests, "post", return_value=Resp()) as post:
    r = tm.notify_via_butler("hi", channel_id=123)
    check("env 있으면 True", r is True)
    check("post 1회", post.call_count == 1)
    kw = post.call_args.kwargs
    check("X-Butler-Token 헤더", kw["headers"].get("X-Butler-Token") == FAKE_TOKEN)
    check("channel_id 유지", kw["json"]["channel_id"] == 123)

print("== update_subscription_manager_code ==")
written = {}
with tempfile.TemporaryDirectory() as td:
    os.makedirs(os.path.join(td, "src"))
    real_open = open

    def spy_open(path, mode="r", *a, **k):
        f = real_open(path, mode, *a, **k)
        return f

    with env_with(BUTLER_API_TOKEN=FAKE_TOKEN), mock.patch.object(tm, "SUB_MGR_PATH", td):
        ok = tm.update_subscription_manager_code("https://abc.trycloudflare.com")
    cfg = real_open(os.path.join(td, "src", "config.py"), encoding="utf-8").read()
    check("config 쓰기 성공", ok is True)
    check("config.py 에 토큰 0건", FAKE_TOKEN not in cfg and "TOKEN" not in cfg.upper())
    check("config.py 는 URL 한 줄", cfg.strip().splitlines() == ['BUTLER_API_URL = "https://abc.trycloudflare.com"'])
real_sub = os.path.join(os.path.dirname(ROOT), "subscription-manager")
check("기본 SUB_MGR_PATH 는 임시폴더가 아님(patch 필요성 확인)", tm.SUB_MGR_PATH == real_sub)

print("== git_push_changes ==")
calls = []


def fake_run(args, *a, **k):
    calls.append(list(args))
    m = mock.Mock()
    m.returncode = 1 if args[:2] == ["git", "push"] else 0
    m.stderr = "rejected"
    m.stdout = ""
    return m


with tempfile.TemporaryDirectory() as td:
    os.makedirs(os.path.join(td, ".git"))
    os.makedirs(os.path.join(td, "src"))
    with env_with(BUTLER_API_TOKEN=FAKE_TOKEN), mock.patch.object(tm, "SUB_MGR_PATH", td), \
            mock.patch.object(tm.subprocess, "run", side_effect=fake_run):
        res = tm.git_push_changes("https://abc.trycloudflare.com")
    flat = [a for c in calls for a in c]
    check("push 실패 시 False", res is False)
    check("--force 인자 0건", "--force" not in flat and "-f" not in flat)
    check("push 1회만 (폴백 없음)", sum(1 for c in calls if c[:2] == ["git", "push"]) == 1)
    adds = [c for c in calls if c[:2] == ["git", "add"]]
    check("git add . 없음", all("." not in c[2:] for c in adds) and len(adds) == 1)
    check("add 대상 2개 파일", adds and adds[0][2:] == ["src/config.py", "reboot_trigger.txt"])

print("== butler_agent ==")
# 무거운 의존성(core, discord 등)은 스텁으로 대체 (실제 import 없이 send_discord 만 검증)
stubs = {
    "utils.system_status": ["get_system_status_embed"],
    "core.ai.service": ["ask_gemini"],
    "config.constants": ["STATUS_CHANNEL_ID", "CHAT_CHANNEL_ID"],
    "core.agent_manager": ["load_agent_config", "save_agent_config", "add_pending_action"],
}
saved = {}
for name, attrs in stubs.items():
    saved[name] = sys.modules.get(name)
    m = types.ModuleType(name)
    for a in attrs:
        setattr(m, a, 1 if a.endswith("_ID") else (lambda *x, **k: {}))
    sys.modules[name] = m
for pkg in ("core", "core.ai"):
    if pkg not in sys.modules:
        saved[pkg] = None
        sys.modules[pkg] = types.ModuleType(pkg)
        sys.modules[pkg].__path__ = []
try:
    import importlib
    with env_with():
        sys.modules.pop("butler_agent", None)
        ba = importlib.import_module("butler_agent")
    agent = ba.ButlerAgent.__new__(ba.ButlerAgent)
    with env_with(), mock.patch.object(ba.requests, "post") as post:
        agent.send_discord("x", 5)
        check("agent: env 비면 post 0회", post.call_count == 0)
    with env_with(BUTLER_API_TOKEN=FAKE_TOKEN), mock.patch.object(ba.requests, "post") as post:
        agent.send_discord("x", 5)
        check("agent: env 있으면 헤더", post.call_count == 1 and
              post.call_args.kwargs["headers"].get("X-Butler-Token") == FAKE_TOKEN)
finally:
    for name, mod in saved.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    sys.modules.pop("butler_agent", None)

src = open(os.path.join(ROOT, "utils", "tunnel_manager.py"), encoding="utf-8").read()
src += open(os.path.join(ROOT, "butler_agent.py"), encoding="utf-8").read()
check("소스에 토큰 기본값 문자열 0건", "butler_v3_secret" not in src)

print(f"\n결과: PASS {PASS} / FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
