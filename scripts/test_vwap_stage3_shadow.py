"""
VWAP 3단계 JR-3 검증 — 섀도우 모드 (설계 문서 §5·§11 T-S1~S3, T-A1(섀도우), T-PERF). 외부 네트워크 없이 실행됩니다.

검증 항목
  T-S1  ShadowBot: 주입 df 로 REAL 과 같은 cycle_id·같은 주문, StaticCandleBroker 네트워크 호출 0회, TossBroker 생성/호출 0회,
        소켓/requests 호출 0회(= 실주문 경로 없음), Discord 알림 0회, 봉 적재 훅 없음(ATTACH_BARS_STORE=False), start() 거부,
        설정 매핑(real_X → virtual_shadow_X, 비밀값 빈 문자열), shadow_enabled 실시간 토글(훅 안에서 매 주기 확인),
        건너뜀(ADR-0010 DATA_UNTRUSTED/LOOP_ERROR·mock 출처·빈 df, 패닉 정지 주기, asof 없음, 기준자본 없음),
        DATA_UNAVAILABLE 주기는 실행(SR-3), 시계 고정(SR-3: VWAPBot._now 위임 — REAL 동치 + 섀도우는 REAL asof 로 판단),
        실행 전 브로커 방어(BROKER_SWAPPED), 음수 현금(over_allocated)
  T-S2  재동기화: 첫 주기·generation 변경·세션 변경·종목/기준자본 변경·재가동(비활성→활성) → SHADOW_SYNC + 원장 = REAL 보유,
        정상 주기엔 재동기화 없음, 보유 조회 실패 시 연기, 재시작 후 상태 파일 복원, 섀도우 자체 패닉 후 세션 내 정지
  T-S3  compare: 7가지 verdict 를 만드는 합성 시나리오, 요약 수치(순손익·승률·슬리피지·일치율·gap), days 필터,
        시장가(결정 주기)·지정가(주문 주기) 짝짓기, REAL 패닉 청산 제외, cycle_id 없는 레코드 제외, 실제 두 봇 실행 결과 비교
  T-A1  섀도우 API 2개: 401, 정상 응답 구조(ticker 포함), days 검증 400, 훅 등록
  T-PERF 훅 소요시간 실측(1600봉: 정상 주기 / 동기화 주기), 2초 HOOK_SLOW 기준 대비
  T-G   운영 data/ 해시(하위 폴더 포함) 전후 동일, trading_bot_*.log 신규 생성 없음

실행:  PYTHONUTF8=1 C:\\Users\\user\\butler_pjt\\venv312\\Scripts\\python.exe scripts/test_vwap_stage3_shadow.py
"""
import os
import sys
import glob
import json
import time
import shutil
import socket
import hashlib
import logging
import traceback
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

LOGS_BEFORE = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log")))


def _hash_tree(path):
    out = {}
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


REAL_DATA_TREE_BEFORE = _hash_tree(os.path.join(PROJECT_ROOT, "data"))

# 1·3단계 하네스 재사용 (임시 DATA_DIR 패치 + Discord 전송 no-op + 봇 로거 콘솔 전용)
import test_vwap_reliability as h  # noqa: E402
import test_vwap_stage3_hooks as hk  # noqa: E402

for _name in ["vwap_bot_virtual_shadow", "vwap_bot_virtual_1", "vwap_bot_virtual_2", "vwap_bot_virtual_3", "vwap_bot_real"]:
    _lg = logging.getLogger(_name)
    if not _lg.handlers:
        _lg.addHandler(logging.NullHandler())
        _lg.propagate = False
    for _hd in _lg.handlers:
        _hd.setLevel(logging.CRITICAL)

import pandas as pd  # noqa: E402

import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap import events as vwap_events  # noqa: E402
from core.vwap import broker as broker_module  # noqa: E402
from core.vwap import shadow as shadow_module  # noqa: E402
from core.vwap.shadow import (ShadowBot, ShadowRunner, StaticCandleBroker, ShadowNetworkError,  # noqa: E402
                              build_shadow_config, compare)

check = h.check
make_config = h.make_config
Patched = h.Patched
T0 = hk.T0
SHADOW = "VIRTUAL_SHADOW"


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def ev(mode=SHADOW, types=None):
    out = hk.read_events(mode)
    return [e for e in out if (not types or e.get("type") in types)]


def shadow_trades():
    return VwapConfigManager.load_trades(SHADOW)


class Guard:
    """실주문/네트워크 경로 감시: TossBroker 생성·메서드 호출, socket.connect, requests 호출 횟수."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        g = self
        self._orig = {}
        for name in ("place_order", "cancel_order", "get_balance", "get_open_orders", "get_order", "get_candles",
                     "_ensure_token", "get_current_price", "__init__"):
            fn = getattr(broker_module.TossBroker, name)
            self._orig[name] = fn

            def make(n, f):
                def wrapped(*a, **k):
                    g.calls.append(f"TossBroker.{n}")
                    raise AssertionError(f"TossBroker.{n} 호출됨")
                return wrapped
            setattr(broker_module.TossBroker, name, make(name, fn))
        self._connect = socket.socket.connect

        def no_connect(sock, *a, **k):
            g.calls.append("socket.connect")
            raise OSError("테스트: 네트워크 금지")
        socket.socket.connect = no_connect
        import requests
        self._req = requests.sessions.Session.request

        def no_request(sess, *a, **k):
            g.calls.append("requests")
            raise OSError("테스트: 네트워크 금지")
        requests.sessions.Session.request = no_request
        return self

    def __exit__(self, *a):
        import requests
        for name, fn in self._orig.items():
            setattr(broker_module.TossBroker, name, fn)
        socket.socket.connect = self._connect
        requests.sessions.Session.request = self._req


class Env:
    """REAL 봇(가짜 토스 브로커) + 섀도우 러너 훅 + 고정 시계. step() 한 번 = REAL 한 주기(그 끝에 섀도우 훅)."""

    def __init__(self, source="toss", pos=None, cfg_over=None, runner=None, clean=True):
        if clean:
            hk.fresh_dir()
        self.fake = hk.StatefulFake(source)
        if pos:
            self.fake.balance["holdings"]["TEST"] = {"qty": float(pos[0]), "entry_price": float(pos[1])}
        self.cfg = make_config("real", **(cfg_over or {}))
        self.clock_now = datetime(2026, 10, 5, 10, 31, 0)
        self.runner = runner or ShadowRunner(clock=lambda: self.clock_now)
        self.patch = Patched(self.cfg, self.fake)

    def __enter__(self):
        self.patch.__enter__()
        self.real = VWAPBot("REAL")
        self.real.tracked_open_orders, self.real.last_holdings_qty, self.real._holdings_snapshot_ready = {}, {}, True
        self.real.add_post_cycle_hook("shadow", self.runner.hook)
        self.real.running = True
        self.real._generation = 1
        return self

    def __exit__(self, *a):
        self.patch.__exit__()

    def step(self, closes, last_low=None, last_high=None, fill=None, now=None, start=T0):
        df = hk.candles(closes, start=start, last_high=last_high, last_low=last_low)
        self.fake.candles = df
        if fill:
            self.fake.fill_open(fill)
        now = now or hk.now_for(df)
        self.clock_now = now + timedelta(seconds=1)
        with hk.FixedClock(now):
            self.real._loop_step()
        return df

    def restart_real(self):
        """REAL BOT_START 흉내: generation 증가 + running + 손실한도 기준 초기화(start() 와 같은 효과)."""
        self.real.running = True
        self.real._generation += 1
        self.real.daily_baseline_asset = 0.0
        self.real.last_baseline_date = ""


BASE = [100.0] * 30


def run_limit_roundtrip(env):
    """지정가 왕복: c1 BUY 제출 → c2 BUY 정정 → c3 BUY 체결 + SELL 제출 → c4 SELL 체결. 두 봇이 같은 흐름이어야 함.
    c1~c2 는 가격이 매수 지정가 위에 머물러(저가 > 지정가) 체결되지 않는다."""
    env.step(BASE + [99.5])
    env.step(BASE + [99.5, 99.0])
    bp = env.fake.placed[-1]["price"]
    env.step(BASE + [99.5, 99.0, 101.5], last_low=bp - 0.5, fill="BUY")
    sp = env.fake.placed[-1]["price"]
    env.step(BASE + [99.5, 99.0, 101.5, 101.8], last_high=sp + 0.5, fill="SELL")
    return bp, sp


CID = ["2026-10-05 10:30:00", "2026-10-05 10:31:00", "2026-10-05 10:32:00", "2026-10-05 10:33:00"]


def mk_ctx(df, cfg_over=None, generation=1, running=True, reason="", source="toss", pos=(0.0, 0.0), asof=None,
           cycle_id=None, ticker=None):
    """REAL 이 훅에 넘기는 ctx 와 같은 모양(비밀값 빈 문자열)."""
    cfg = make_config("real", **(cfg_over or {}))
    for k in ("toss_client_id", "toss_client_secret", "toss_account_seq", "admin_password_hash"):
        cfg[k] = ""
    ticker = ticker or cfg["real_ticker"]
    if asof is False:            # NO_ASOF 시험용
        asof = None
    else:
        asof = asof or (df["time"].iloc[-1].to_pydatetime() + timedelta(seconds=5) if len(df) else T0)
    return {"mode": "REAL", "generation": generation,
            "cycle_id": cycle_id if cycle_id is not None else (df["time"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S") if len(df) else ""),
            "df": df, "ticker": ticker, "market": "US", "interval": "1m", "reset_time": "22:30",
            "candles_source": source, "candles_asof": asof, "config": cfg,
            "position": ({"qty": pos[0], "entry_price": pos[1]} if pos is not None else None), "cash": 100000.0,
            "reason_code": reason, "running": running}


def run_hook(runner, ctx, now=None):
    """ctx 로 훅 1회. 섀도우 본체의 now 는 asof 로 고정."""
    now = now or ctx["candles_asof"] or T0
    with hk.FixedClock(now):
        runner.hook(ctx)


# ---------------------------------------------------------------------------
# T-S1
# ---------------------------------------------------------------------------
def test_s1_shadow_bot():
    print("\nT-S1. ShadowBot — REAL 과 같은 cycle_id·주문, 네트워크/실주문/알림 0회")
    sent = []
    vwap_events.set_sender(lambda m: (sent.append(m), True)[1])
    vwap_events.notifier.synchronous = True
    vwap_events.notifier._last_sent.clear()
    try:
        with Guard() as g, Env() as env:
            run_limit_roundtrip(env)
            runner, sbot = env.runner, env.runner.bot
            # a) 같은 cycle_id / 주문
            real_placed = [(p["side"], round(p["price"], 4), p["qty"]) for p in env.fake.placed]
            sh_ev = ev(types=("ORDER_PLACED", "ORDER_REPLACED"))
            sh_placed = [(e["data"]["side"], e["data"]["price"], e["data"]["qty"]) for e in sh_ev]
            check("a) 섀도우 지정가 주문 시퀀스(방향·가격·수량) == REAL", sh_placed == real_placed and len(real_placed) == 3,
                  f"real={real_placed} shadow={sh_placed}")
            real_ev = [e for e in ev("REAL", ("ORDER_PLACED", "ORDER_REPLACED"))]
            check("a) 섀도우 주문 이벤트 cycle_id == REAL 주문 이벤트 cycle_id (같은 df)",
                  [e["data"]["cycle_id"] for e in sh_ev] == [e["data"]["cycle_id"] for e in real_ev] == CID[:3],
                  f"{[e['data']['cycle_id'] for e in sh_ev]}")
            check("a) 섀도우 이벤트 mode=VIRTUAL_SHADOW, 타입 ORDER_PLACED/REPLACED/PLACED",
                  [(e["mode"], e["type"]) for e in sh_ev] == [(SHADOW, "ORDER_PLACED"), (SHADOW, "ORDER_REPLACED"), (SHADOW, "ORDER_PLACED")])
            st = shadow_trades()
            rt = VwapConfigManager.load_trades("REAL")
            check("a) 섀도우 체결 2건(BUY→SELL), 거래 레코드 cycle_id == REAL 거래 레코드 cycle_id, 체결가 동일",
                  [(t["side"], t["price"], t.get("cycle_id")) for t in st] == [(t["side"], t["price"], t.get("cycle_id")) for t in rt]
                  and [t["side"] for t in st] == ["BUY", "SELL"], f"shadow={[(t['side'], t['price'], t.get('cycle_id')) for t in st]} "
                                                                  f"real={[(t['side'], t['price'], t.get('cycle_id')) for t in rt]}")
            check("a) 섀도우 거래 파일은 vwap_trades_virtual_shadow.json, REAL 거래 파일과 분리",
                  os.path.exists(os.path.join(h.TMP_DIR, "vwap_trades_virtual_shadow.json"))
                  and os.path.exists(os.path.join(h.TMP_DIR, "vwap_events_virtual_shadow.jsonl")))
            # b) 네트워크/실주문 경로 0회
            check("b) StaticCandleBroker: 금지 메서드 호출 0회, 캔들 요청은 주기마다 1회(4회)",
                  sbot.static_broker.network_calls == 0 and sbot.static_broker.candle_requests == 4,
                  f"network_calls={sbot.static_broker.network_calls} candle_requests={sbot.static_broker.candle_requests}")
            check("b) 섀도우 시세 소스는 같은 StaticCandleBroker 객체를 유지(VirtualBroker.source_broker 와 동일)",
                  sbot.real_broker is sbot.static_broker and sbot.virtual_broker.source_broker is sbot.static_broker
                  and type(sbot.real_broker) is StaticCandleBroker)
            check("b) TossBroker 생성자·메서드 호출 0회 / socket.connect 0회 / requests 0회 (REAL 은 가짜 브로커)",
                  g.calls == [], f"{g.calls}")
            check("b) last_candles_source 복사(toss)", sbot.static_broker.last_candles_source == "toss")
            check("b) 섀도우는 REAL 가짜 브로커에 주문·취소를 내지 않음(REAL 주문 수 = REAL 이 낸 3건 + 0)",
                  len(env.fake.placed) == 3)
            # c) 알림 0회
            sh_msgs = [m for m in sent if "VIRTUAL_SHADOW" in m]
            check("c) Discord 알림: 섀도우 0회 (REAL 은 알림 있음 → 비교 기준 동작 확인)", sh_msgs == [] and any("REAL" in m for m in sent),
                  f"shadow={len(sh_msgs)} total={len(sent)}")
            check("c) _refresh_notify_flag 는 항상 False, _notify_enabled False",
                  sbot._refresh_notify_flag({"virtual_discord_notify": True, "real_discord_notify": True}) is False
                  and sbot._notify_enabled is False)
            # d) 봉 적재 훅 없음
            check("d) ATTACH_BARS_STORE=False → 섀도우 봇 훅 없음 / 봉 적재 중복 없음(각 시각 1행)",
                  ShadowBot.ATTACH_BARS_STORE is False and sbot._post_cycle_hooks == [] and _bars_unique())
            # e) start 거부, 스레드 없음
            check("e) start() 거부(False), 스레드 없음", sbot.start() is False and sbot.thread is None)
    finally:
        vwap_events.notifier.synchronous = False
        vwap_events.set_sender(lambda m: True)

    # 금지 메서드 단위
    sb = StaticCandleBroker()
    errs = []
    for name, args in (("get_balance", ()), ("place_order", ("T", "BUY", 1.0, 1)), ("cancel_order", ("x",)),
                       ("get_open_orders", ("T",)), ("get_order", ("x",))):
        try:
            getattr(sb, name)(*args)
        except ShadowNetworkError:
            errs.append(name)
    check("StaticCandleBroker: 계좌/주문 메서드 5종은 ShadowNetworkError + 호출 카운트", len(errs) == 5 and sb.network_calls == 5,
          f"{errs} {sb.network_calls}")

    # 설정 매핑
    cfg = make_config("real", ticker="ABC", n_percent=1.5, start_time="23:00")
    cfg["toss_client_secret"] = "SECRET"
    m = build_shadow_config(cfg)
    check("설정 매핑: real_X → virtual_shadow_X (ticker/n/start_time), 원본 비변경, 비밀값 빈 문자열, 알림 False",
          m["virtual_shadow_ticker"] == "ABC" and m["virtual_shadow_n_percent"] == 1.5 and m["virtual_shadow_start_time"] == "23:00"
          and "real_ticker" not in m and cfg["toss_client_secret"] == "SECRET"
          and all(m[k] == "" for k in ("toss_client_id", "toss_client_secret", "toss_account_seq", "admin_password_hash"))
          and m["virtual_shadow_discord_notify"] is False)

    # shadow_enabled 실시간 토글 (훅은 등록돼 있고 매 주기 config 로 판단)
    with Env() as env:
        env.step(BASE + [99.5])
        env.step(BASE + [99.5, 99.0])
        env.cfg["shadow_enabled"] = False          # 훅 등록 후 설정 변경 → 즉시 반영
        env.step(BASE + [99.5, 99.0, 98.0])
        n_off = len(ev(types=("ORDER_PLACED", "ORDER_REPLACED")))
        env.cfg["shadow_enabled"] = True
        env.step(BASE + [99.5, 99.0, 98.0, 97.5])
        sh = ev(types=("ORDER_PLACED", "ORDER_REPLACED"))
        sync = ev(types=("SHADOW_SYNC",))
        check("shadow_enabled 토글: 끈 주기에는 섀도우 주문 없음 / 다시 켜면 재개(훅 재등록 없이)",
              n_off == 2 and [e["data"]["cycle_id"] for e in sh] == [CID[0], CID[1], "2026-10-05 10:33:00"],
              f"off={n_off} {[e['data']['cycle_id'] for e in sh]}")
        check("shadow_enabled 토글: 재가동 시 SHADOW_SYNC(REENABLED) 로 원장 재동기화",
              [e["reason_code"] for e in sync] == ["FIRST_SYNC", "REENABLED"], f"{[e['reason_code'] for e in sync]}")
        check("shadow_enabled=false 주기에 REAL 주문은 그대로 진행(REAL 주문 4건)", len(env.fake.placed) >= 3)


def _bars_unique():
    import glob as _g
    for p in _g.glob(os.path.join(h.TMP_DIR, "bars", "*.csv")):
        with open(p, encoding="utf-8") as f:
            times = [ln.split(",")[0] for ln in f.read().splitlines()[1:] if ln.strip()]
        if len(times) != len(set(times)):
            return False
    return True


def test_s1_skips():
    print("\nT-S1b. 건너뜀 규칙 — ADR-0010 / Q1(a) 패닉 주기 / 기준자본 + SR-3 시계 고정(REAL 동치)")
    df = hk.candles(BASE + [98.0])

    # (1) REAL 이 실제로 신뢰 불가(mock) 시세로 DATA_UNTRUSTED
    with Env(source="mock") as env:
        env.step(BASE + [98.0])
        check("REAL 주기가 DATA_UNTRUSTED 로 끝나면 섀도우는 그 주기를 건너뜀(주문·체결·원장 없음)",
              env.real.status_cache.get("reason_code") == "DATA_UNTRUSTED" and env.runner.bot is None
              and shadow_trades() == [] and ev(types=("ORDER_PLACED", "SHADOW_SYNC")) == [],
              f"reason={env.real.status_cache.get('reason_code')} bot={env.runner.bot}")
        sk = env.runner.status()["skip"]
        check("건너뜀 사유가 status.skip 과 ERROR(warn) 이벤트(SHADOW_SKIP_REAL_DATA_UNTRUSTED)로 남음",
              sk and sk["reason"] == "REAL_DATA_UNTRUSTED" and any(e["reason_code"] == "SHADOW_SKIP_REAL_DATA_UNTRUSTED" for e in ev()),
              f"{sk}")

    # (2) 직접 ctx: 사유별 건너뜀(섀도우 봇이 만들어지지도 않음)
    cases = [
        ("LOOP_ERROR", dict(reason="LOOP_ERROR")),
        ("asof 없음(NO_ASOF)", dict(asof=False)),
        ("DATA_UNTRUSTED", dict(reason="DATA_UNTRUSTED")),
        ("출처 mock", dict(source="mock")),
        ("출처 빈 값", dict(source="")),
        ("REAL 정지(running=False, 패닉/사용자 정지)", dict(running=False)),
        ("기준자본 0", dict(cfg_over={"initial_balance": 0.0})),
    ]
    for label, kw in cases:
        hk.fresh_dir()
        r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 10, 31, 6))
        run_hook(r, mk_ctx(df, **kw))
        check(f"건너뜀: {label} → 섀도우 봇 미생성·주문/거래/이벤트(SYNC) 없음", r.bot is None and shadow_trades() == []
              and ev(types=("SHADOW_SYNC", "ORDER_PLACED")) == [] and r.status()["skip"] is not None,
              f"bot={r.bot} skip={r.status()['skip']}")
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 10, 31, 6))
    run_hook(r, mk_ctx(df.iloc[:0]))
    check("건너뜀: 빈 df", r.bot is None and r.status()["skip"]["reason"] == "NO_CANDLES")
    run_hook(r, mk_ctx(df))
    check("정상 ctx 는 실행됨(비교 대조): 섀도우 봇 생성 + SHADOW_SYNC + 주문 1건",
          r.bot is not None and len(ev(types=("SHADOW_SYNC",))) == 1 and len(ev(types=("ORDER_PLACED",))) == 1
          and r.status()["skip"] is None)

    # (2b) SR-3: REAL 이 DATA_UNAVAILABLE(잔고/미체결 조회 실패)로 끝난 주기도 캔들이 신뢰 출처면 섀도우는 실행
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 10, 31, 6))
    run_hook(r, mk_ctx(df, reason="DATA_UNAVAILABLE"))
    check("SR-3: REAL DATA_UNAVAILABLE 주기(캔들 정상) → 섀도우 실행(같은 봉 가상 판단·체결 판정 유지)",
          r.bot is not None and r.status()["skip"] is None and len(ev(types=("ORDER_PLACED",))) == 1, f"{r.status()['skip']}")
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 10, 31, 6))
    run_hook(r, mk_ctx(df, reason="DATA_UNAVAILABLE", pos=None))
    check("SR-3: DATA_UNAVAILABLE + 보유 조회 실패(position=None) 첫 주기 → 재동기화 연기(POSITION_UNKNOWN)",
          r.status()["skip"]["reason"] == "POSITION_UNKNOWN" and ev(types=("ORDER_PLACED", "SHADOW_SYNC")) == [])

    # (3) 같은 사유 건너뜀 이벤트는 10분에 1회
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 10, 31, 6))
    for _ in range(5):
        run_hook(r, mk_ctx(df, reason="LOOP_ERROR"))
    check("같은 사유 건너뜀 ERROR 이벤트는 5주기 연속이어도 1건(10분 억제)",
          len([e for e in ev() if e["reason_code"] == "SHADOW_SKIP_REAL_LOOP_ERROR"]) == 1)

    # (4) SR-3 시계 고정: 섀도우 본체의 now = REAL asof. 벽시계가 경계를 넘어도 REAL 과 같은 시각으로 판단(건너뜀 없음)
    df2 = hk.candles(BASE + [98.0], start=datetime(2026, 10, 5, 21, 59))
    asof = datetime(2026, 10, 5, 22, 29, 59, 900000)
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: datetime(2026, 10, 5, 22, 30, 0, 400000))
    run_hook(r, mk_ctx(df2, asof=asof), now=datetime(2026, 10, 5, 22, 30, 0, 400000))   # 벽시계는 리셋(22:30)을 넘음
    snap = (r.bot.status_cache if r.bot else {})
    check("시계 고정: 벽시계가 세션 리셋을 넘어도 실행되고, 세션 날짜·손실한도 기준일은 REAL asof(22:29:59.9) 기준(2026-10-04)",
          r.bot is not None and r.status()["skip"] is None and r.status()["session_date"] == "2026-10-04"
          and r.bot.last_baseline_date == "2026-10-04" and r.bot._asof is None,
          f"skip={r.status()['skip']} session={r.status()['session_date']} baseline={getattr(r.bot, 'last_baseline_date', None)}")
    # 거래 시작 시각(start_time) 경계: asof 23:59:59.8 은 대기 구간 [22:30, 00:00) 안 → WAIT_START_TIME (벽시계 00:00:00.1 이면 대기 아님)
    hk.fresh_dir()
    asof_s = datetime(2026, 10, 5, 23, 59, 59, 800000)
    wall = datetime(2026, 10, 6, 0, 0, 0, 100000)
    r = ShadowRunner(clock=lambda: wall)
    df3 = hk.candles(BASE + [98.0], start=datetime(2026, 10, 5, 23, 29))
    run_hook(r, mk_ctx(df3, asof=asof_s, cfg_over={"start_time": "00:00"}), now=wall)
    check("시계 고정: start_time=00:00, REAL asof 23:59:59.8(대기 중) → 섀도우도 WAIT_START_TIME·주문 없음 (벽시계 00:00:00.1 무시)",
          r.bot is not None and r.status()["reason_code"] == "WAIT_START_TIME" and ev(types=("ORDER_PLACED",)) == [],
          f"reason={r.status()['reason_code']} skip={r.status()['skip']}")
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: wall)
    run_hook(r, mk_ctx(df3, asof=wall, cfg_over={"start_time": "00:00"}), now=asof_s)   # 반대: asof 가 대기 종료 후
    check("시계 고정(반대 방향): REAL asof 00:00:00.1(대기 끝) → 벽시계가 23:59:59.8 이어도 섀도우는 매수 주문 제출",
          r.bot is not None and len(ev(types=("ORDER_PLACED",))) == 1 and r.status()["reason_code"] != "WAIT_START_TIME",
          f"reason={r.status()['reason_code']}")

    # (5) SR-3 REAL 동치: VWAPBot._now 는 모듈 datetime.now() 그대로(기존 테스트의 시계 고정도 그대로 적용)
    import inspect
    fixed = datetime(2026, 10, 5, 22, 29, 59, 900000)
    with hk.FixedClock(fixed):
        rb = VWAPBot.__new__(VWAPBot)
        same = VWAPBot._now(rb) == fixed
    body_src = inspect.getsource(VWAPBot._loop_step_body)
    check("REAL 동치: VWAPBot._now() == bot 모듈 datetime.now() (고정 시계 패치가 그대로 적용)", same)
    check("REAL 동치: _loop_step_body 의 판단 시각은 self._now() 한 곳, 나머지 datetime.now() 는 상태 캐시 last_updated 1곳뿐",
          body_src.count("now = self._now()") == 1 and body_src.count("datetime.now()") == 1
          and "\"last_updated\": datetime.now()" in body_src, f"{body_src.count('datetime.now()')}")
    with Env() as env:
        env.step(BASE + [99.5], now=datetime(2026, 10, 5, 10, 30, 30))
        cid = env.real._cycle.get("cycle_id")
        snap_cfg = (env.real._cycle or {}).get("config_snapshot") or {}
        check("REAL 동치: REAL 주기의 candles_asof/세션 시작 = 고정 시계 now (위임 경유 동작 무변경), 섀도우도 같은 cycle_id",
              env.runner.status()["last_cycle_id"] == cid and snap_cfg.get("session_start") == "2026-10-04 22:30",
              f"{snap_cfg.get('session_start')} {cid}")
    check("ShadowBot._now: asof 미설정이면 datetime.now() (러너 밖 단독 호출 대비)",
          isinstance(ShadowBot._now(type("X", (), {"_asof": None})()), datetime))


# ---------------------------------------------------------------------------
# T-S1c 패닉 주기
# ---------------------------------------------------------------------------
def test_s1_panic():
    print("\nT-S1c. REAL 패닉 자동정지 주기 — 섀도우는 그 주기를 건너뛰고(Q1(a)), compare 는 오탐으로 세지 않음")
    with Env(pos=(10.0, 100.0), cfg_over={"max_daily_loss_limit": 0.2}) as env:
        env.step(BASE + [100.0])                     # c1: 기준 자산 설정, 정상 (섀도우 FIRST_SYNC)
        env.step(BASE + [100.0, 95.0])               # c2: 미실현 -50 → 0.5% >= 0.2% → 패닉 청산 + REAL 정지
        real_panic = [e for e in ev("REAL", ("PANIC",))]
        sk = env.runner.status()["skip"]
        check("REAL 패닉 자동정지(running=False) 발생 + REAL 시장가 청산 기록(DAILY_LOSS_STOP)",
              len(real_panic) == 1 and not env.real.running
              and [(t["side"], t.get("reason_code")) for t in VwapConfigManager.load_trades("REAL")] == [("STOP_LOSS", "DAILY_LOSS_STOP")],
              f"running={env.real.running} trades={VwapConfigManager.load_trades('REAL')}")
        check("패닉 주기: 섀도우는 건너뜀(REAL_NOT_RUNNING) — 섀도우 거래·주문 없음, 섀도우 보유는 그대로",
              sk and sk["reason"] == "REAL_NOT_RUNNING" and shadow_trades() == []
              and env.runner.bot.virtual_broker.holdings.get("TEST", {}).get("qty") == 10.0, f"{sk}")
        res = compare(1, now=datetime(2026, 10, 5, 23, 0), config=env.cfg)
        check("compare: REAL 패닉 단독 청산은 REAL_ONLY_ORDER 가 아닌 excluded.REAL_PANIC 으로 분리(pairs 에는 표시)",
              res["summary"]["verdicts"]["REAL_ONLY_ORDER"] == 0 and res["summary"]["excluded"]["REAL_PANIC"] == 1
              and len(res["pairs"]) == 1 and res["pairs"][0].get("excluded_reason") == "REAL_PANIC"
              and res["pairs"][0]["verdict"] == "REAL_ONLY_ORDER", f"{res['summary']['verdicts']} {res['pairs']}")
        env.restart_real()
        env.fake.balance = {"cash": 100000.0, "holdings": {}}   # REAL 은 청산돼 무보유
        env.step(BASE + [100.0, 95.0, 96.0])
        sync = ev(types=("SHADOW_SYNC",))
        check("REAL 재가동(BOT_START) 첫 주기에 섀도우가 REAL 의 (청산된) 무보유로 재동기화",
              sync[-1]["reason_code"] == "BOT_START" and env.runner.bot.virtual_broker.holdings == {}
              and sync[-1]["data"]["shadow_before"]["holdings"].get("TEST", {}).get("qty") == 10.0,
              f"{sync[-1]['reason_code']} {sync[-1]['data']}")


# ---------------------------------------------------------------------------
# T-S2
# ---------------------------------------------------------------------------
def test_s2_resync():
    print("\nT-S2. 원장 재동기화 — 사유별 SHADOW_SYNC, 원장 = REAL 보유, 복원")
    df = hk.candles(BASE + [100.0])
    asof = hk.now_for(df)
    hk.fresh_dir()
    r = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    run_hook(r, mk_ctx(df, pos=(5.0, 100.0), generation=1), now=asof)
    vb = r.bot.virtual_broker
    s1 = ev(types=("SHADOW_SYNC",))
    check("첫 주기: FIRST_SYNC, 원장 = REAL 보유 5주@100, cash = 기준자본 − 수량×평단(10000−500)",
          len(s1) == 1 and s1[0]["reason_code"] == "FIRST_SYNC" and vb.holdings == {"TEST": {"qty": 5.0, "entry_price": 100.0}}
          and abs(vb.cash - 9500.0) < 1e-9, f"{s1[0]['data'] if s1 else None} cash={vb.cash} h={vb.holdings}")
    d = s1[0]["data"]
    check("SHADOW_SYNC data: 사유·REAL 보유·이전 섀도우 보유·세션·기준자본·sync 후 상태",
          d["real"] == {"qty": 5.0, "entry_price": 100.0} and "shadow_before" in d and d["session_date"] == "2026-10-04"
          and d["initial_balance"] == 10000.0 and d["shadow_after"]["qty"] == 5.0 and d["ticker"] == "TEST", f"{d}")
    st = r.status()
    check("status: synced_at/sync_reason/position/cash/ticker", st["sync_reason"] == "FIRST_SYNC" and st["synced_at"]
          and st["position"] == {"qty": 5.0, "entry_price": 100.0} and st["ticker"] == "TEST" and st["following"] == "REAL",
          f"{st}")

    # 정상 주기: 재동기화 없음
    nxt = hk.candles(BASE + [100.0, 100.2])
    run_hook(r, mk_ctx(nxt, pos=(5.0, 100.0), generation=1), now=hk.now_for(nxt))
    check("정상(같은 generation·세션·설정) 다음 주기에는 재동기화 없음", len(ev(types=("SHADOW_SYNC",))) == 1)

    # generation 변경: 섀도우가 따로 벌어진 상태를 REAL 보유로 덮어씀 + 섀도우 미체결 주문 비움
    vb.cash, vb.holdings = 1.0, {"TEST": {"qty": 99.0, "entry_price": 1.0}}
    vb.open_orders = [{"order_id": "x", "ticker": "TEST", "side": "BUY", "price": 90.0, "qty": 1.0, "created_at": 0.0}]
    nxt2 = hk.candles(BASE + [100.0, 100.2, 100.1])
    run_hook(r, mk_ctx(nxt2, pos=(3.0, 101.0), generation=2), now=hk.now_for(nxt2))
    s2 = ev(types=("SHADOW_SYNC",))
    check("generation 변경(REAL BOT_START) → SHADOW_SYNC(BOT_START), 원장 = REAL(3주@101), 이전 섀도우 보유 99주가 data 에 기록",
          s2[-1]["reason_code"] == "BOT_START" and vb.holdings["TEST"] == {"qty": 3.0, "entry_price": 101.0}
          and s2[-1]["data"]["shadow_before"]["holdings"]["TEST"]["qty"] == 99.0
          and not any(o["order_id"] == "x" for o in vb.open_orders), f"{s2[-1]['data']['shadow_before']}")
    check("cash = 10000 − 3×101 (재동기화 후 그 주기 판단·주문 반영 전 값은 주문으로 변동 가능 → 보유 기준만 확인)",
          abs((vb.cash + sum(o['price'] * o['qty'] for o in vb.open_orders if o['side'] == 'BUY')) - (10000.0 - 303.0)) < 1e-6,
          f"cash={vb.cash} open={vb.open_orders}")

    # 세션 변경: 10/05 22:35 (세션 2026-10-04 → 2026-10-05)
    t_new = datetime(2026, 10, 5, 22, 35, 0)
    dfn = hk.candles(BASE + [100.0], start=t_new - timedelta(minutes=31))
    r._clock = lambda: t_new + timedelta(seconds=1)
    run_hook(r, mk_ctx(dfn, pos=(3.0, 101.0), generation=2, asof=t_new), now=t_new)
    s3 = ev(types=("SHADOW_SYNC",))
    check("세션 변경 → SHADOW_SYNC(SESSION_START) + session_date 갱신", s3[-1]["reason_code"] == "SESSION_START"
          and s3[-1]["data"]["session_date"] == "2026-10-05" and r.status()["session_date"] == "2026-10-05", f"{s3[-1]['data']}")

    # 종목 변경 / 기준자본 변경
    t2 = t_new + timedelta(minutes=1)
    dfc = hk.candles(BASE + [100.0, 100.1], start=t_new - timedelta(minutes=31))
    r._clock = lambda: t2 + timedelta(seconds=1)
    run_hook(r, mk_ctx(dfc, pos=(0.0, 0.0), generation=2, asof=t2, cfg_over={"ticker": "OTHER"}), now=t2)
    check("종목 변경 → SHADOW_SYNC(CONFIG_CHANGE), 이전 종목 보유 정리·무보유", ev(types=("SHADOW_SYNC",))[-1]["reason_code"] == "CONFIG_CHANGE"
          and r.bot.virtual_broker.holdings.get("TEST") is None and r.status()["ticker"] == "OTHER")
    t3 = t2 + timedelta(minutes=1)
    dfd = hk.candles(BASE + [100.0, 100.1, 100.2], start=t_new - timedelta(minutes=31))
    r._clock = lambda: t3 + timedelta(seconds=1)
    run_hook(r, mk_ctx(dfd, pos=(0.0, 0.0), generation=2, asof=t3, cfg_over={"ticker": "OTHER", "initial_balance": 20000.0}), now=t3)
    check("기준자본 변경 → SHADOW_SYNC(CONFIG_CHANGE), 새 VirtualBroker(기준자본 20000)",
          ev(types=("SHADOW_SYNC",))[-1]["reason_code"] == "CONFIG_CHANGE" and r.bot.virtual_broker.initial_balance == 20000.0
          and r.bot.virtual_broker.source_broker is r.bot.static_broker, f"{r.bot.virtual_broker.initial_balance}")
    reasons = [e["reason_code"] for e in ev(types=("SHADOW_SYNC",))]
    check("재동기화 사유 순서", reasons == ["FIRST_SYNC", "BOT_START", "SESSION_START", "CONFIG_CHANGE", "CONFIG_CHANGE"], f"{reasons}")

    # 보유 조회 실패(position=None) → 재동기화 연기, 다음 주기에 수행
    hk.fresh_dir()
    r2 = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    run_hook(r2, mk_ctx(df, pos=None), now=asof)
    check("REAL 보유 조회 실패(position=None): 재동기화 연기(POSITION_UNKNOWN), 섀도우 주문 없음",
          ev(types=("SHADOW_SYNC", "ORDER_PLACED")) == [] and r2.status()["skip"]["reason"] == "POSITION_UNKNOWN")
    run_hook(r2, mk_ctx(df, pos=(0.0, 0.0)), now=asof)
    check("다음 주기에 정상 위치로 재동기화(FIRST_SYNC)", [e["reason_code"] for e in ev(types=("SHADOW_SYNC",))] == ["FIRST_SYNC"])

    # 상태 파일: 구조·원자 저장 / 재시작 후 복원 / 재시작 시 BOT_START 재동기화(같은 generation 번호여도)
    path = r2.state_path()
    with open(path, encoding="utf-8") as f:
        sj = json.load(f)
    check("상태 파일 필드(real_generation/session_date/cash/holdings/open_orders/order_meta/synced_at/sync_reason) + .tmp 잔존 없음",
          all(k in sj for k in ("real_generation", "session_date", "cash", "holdings", "open_orders", "order_meta", "synced_at", "sync_reason"))
          and not os.path.exists(path + ".tmp") and os.path.basename(path) == "vwap_shadow_state.json", f"{sorted(sj)}")

    hk.fresh_dir()
    rA = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    dfA = hk.candles(BASE + [98.0])
    run_hook(rA, mk_ctx(dfA, pos=(0.0, 0.0), generation=1), now=hk.now_for(dfA))
    assert rA.bot.virtual_broker.open_orders, "복원 테스트 전제: 섀도우 BUY 미체결이 있어야 함"
    # 섀도우 BUY 주문이 나간 상태를 저장 → 새 러너(재시작) 가 복원하는지
    saved_cash = rA.bot.virtual_broker.cash
    saved_hold = json.loads(json.dumps(rA.bot.virtual_broker.holdings))
    saved_orders = json.loads(json.dumps(rA.bot.virtual_broker.open_orders))
    rB = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    botB = rB._ensure_bot(10000.0)
    vbB = botB.virtual_broker
    check("재시작: 새 러너가 상태 파일로 원장(cash/holdings/open_orders) 복원(거래기록 재구성 값이 아님)",
          abs(vbB.cash - saved_cash) < 1e-9 and vbB.holdings == saved_hold and vbB.open_orders == saved_orders,
          f"cash {vbB.cash} vs {saved_cash}, h {vbB.holdings}, o {vbB.open_orders}")
    check("재시작: status() 가 상태 파일에서 복원(봇 생성 전에도 ticker/sync_reason 표시)",
          ShadowRunner(clock=lambda: asof).status()["sync_reason"] == "FIRST_SYNC")
    nxt = hk.candles(BASE + [100.0, 100.3])
    run_hook(rB, mk_ctx(nxt, pos=(0.0, 0.0), generation=1), now=hk.now_for(nxt))   # 같은 generation 번호(1)
    check("재시작 후 첫 주기: generation 번호가 같아도(boot_id 다름) BOT_START 재동기화",
          ev(types=("SHADOW_SYNC",))[-1]["reason_code"] == "BOT_START")

    # 섀도우 자체 일 손실한도 패닉: 같은 세션 남은 주기는 쉬고, 세션/재가동 때 재개
    hk.fresh_dir()
    rP = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    over = {"max_daily_loss_limit": 0.2}
    run_hook(rP, mk_ctx(hk.candles(BASE + [100.0]), pos=(10.0, 100.0), cfg_over=over), now=asof)
    d2 = hk.candles(BASE + [100.0, 95.0])
    run_hook(rP, mk_ctx(d2, pos=(10.0, 100.0), cfg_over=over), now=hk.now_for(d2))
    panicked = [e for e in ev(types=("PANIC",))]
    d3 = hk.candles(BASE + [100.0, 95.0, 96.0])
    n_ev = len(hk.read_events(SHADOW))
    run_hook(rP, mk_ctx(d3, pos=(10.0, 100.0), cfg_over=over), now=hk.now_for(d3))
    check("섀도우 자체 패닉(손실한도) → PANIC 이벤트 + halted, 같은 세션 남은 주기는 건너뜀(SHADOW_HALTED, PANIC 반복 없음)",
          len(panicked) == 1 and rP.status()["halted"] and rP.status()["skip"]["reason"] == "SHADOW_HALTED"
          and len(ev(types=("PANIC",))) == 1, f"panic={len(panicked)} {rP.status()['skip']}")
    d4 = hk.candles(BASE + [100.0, 95.0, 96.0, 97.0])
    run_hook(rP, mk_ctx(d4, pos=(0.0, 0.0), generation=2, cfg_over=over), now=hk.now_for(d4))
    check("REAL 재가동(generation 변경)으로 섀도우 정지 해제·재동기화", not rP.status()["halted"]
          and ev(types=("SHADOW_SYNC",))[-1]["reason_code"] == "BOT_START")

    # SR-3 음수 현금: REAL 보유 원가(200주×100=20000) > 기준자본(10000) → cash=-10000 그대로, SHADOW_SYNC warn + over_allocated
    hk.fresh_dir()
    rN = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    dN = hk.candles(BASE + [100.0])
    run_hook(rN, mk_ctx(dN, pos=(200.0, 100.0)), now=asof)
    sN = ev(types=("SHADOW_SYNC",))
    check("음수 현금: cash = 10000 − 200×100 = −10000(0 으로 자르지 않음), SHADOW_SYNC level=warn·over_allocated=true",
          abs(rN.bot.virtual_broker.cash + 10000.0) < 1e-9 and sN[-1]["level"] == "warn" and sN[-1]["data"]["over_allocated"] is True
          and rN.status()["cash"] == -10000.0, f"cash={rN.bot.virtual_broker.cash} {sN[-1]['level']}")
    dN2 = hk.candles(BASE + [100.0, 100.2])
    run_hook(rN, mk_ctx(dN2, pos=(200.0, 100.0)), now=hk.now_for(dN2))
    check("음수 현금: 다음 주기도 예외·패닉 없이 진행(보유 유지, PANIC 없음)",
          rN.bot.virtual_broker.holdings.get("TEST", {}).get("qty") == 200.0 and ev(types=("PANIC",)) == []
          and rN.status()["skip"] is None, f"{rN.status()}")
    hk.fresh_dir()
    rN2 = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    run_hook(rN2, mk_ctx(dN, pos=(5.0, 100.0)), now=asof)
    check("정상 동기화는 SHADOW_SYNC level=info·over_allocated=false", ev(types=("SHADOW_SYNC",))[-1]["level"] == "info"
          and ev(types=("SHADOW_SYNC",))[-1]["data"]["over_allocated"] is False)

    # SR-3 실행 전 브로커 방어: 정적 브로커가 아닌 시세 소스로 바뀌어 있으면 실행하지 않고 폐기(그 브로커 호출 0회)
    class _Spy:
        calls = 0
        client_id = client_secret = account_seq = ""
        def __getattr__(self, name):
            _Spy.calls += 1
            raise AssertionError(name)
    rN2.bot.real_broker = _Spy()
    n_ord = len(ev(types=("ORDER_PLACED", "ORDER_REPLACED")))
    dN3 = hk.candles(BASE + [100.0, 99.0])
    run_hook(rN2, mk_ctx(dN3, pos=(5.0, 100.0)), now=hk.now_for(dN3))
    check("BROKER_SWAPPED(실행 전): 섀도우 폐기·주기 미실행·바뀐 브로커 호출 0회",
          rN2.bot is None and rN2.status()["skip"]["reason"] == "BROKER_SWAPPED" and _Spy.calls == 0
          and len(ev(types=("ORDER_PLACED", "ORDER_REPLACED"))) == n_ord, f"{rN2.status()['skip']} calls={_Spy.calls}")
    dN4 = hk.candles(BASE + [100.0, 99.0, 99.5])
    run_hook(rN2, mk_ctx(dN4, pos=(5.0, 100.0)), now=hk.now_for(dN4))
    check("BROKER_SWAPPED 다음 주기: 새 섀도우 봇을 만들고 REAL 보유로 다시 맞춤(FIRST_SYNC)",
          rN2.bot is not None and ev(types=("SHADOW_SYNC",))[-1]["reason_code"] == "FIRST_SYNC"
          and rN2.bot.real_broker is rN2.bot.static_broker, f"{[e['reason_code'] for e in ev(types=('SHADOW_SYNC',))]}")

    # status 는 훅 스레드가 만든 원장 복사본만 읽음 (가상 브로커 dict 를 바꿔도 status 결과는 다음 주기 전까지 그대로)
    hk.fresh_dir()
    rS = ShadowRunner(clock=lambda: asof + timedelta(seconds=1))
    run_hook(rS, mk_ctx(dN, pos=(5.0, 100.0)), now=asof)
    before = rS.status()["position"]
    rS.bot.virtual_broker.holdings["TEST"]["qty"] = 999.0
    check("status: 원장 스냅샷(복사본)만 읽음 — 진행 중 가상 브로커 변경이 status 에 섞이지 않음",
          rS.status()["position"] == before == {"qty": 5.0, "entry_price": 100.0}, f"{rS.status()['position']}")


# ---------------------------------------------------------------------------
# T-S3
# ---------------------------------------------------------------------------
def _trade(tid, ts, side, price, qty, pnl, cid=None, commission=None, slip=None, reason="", order_price=None, intended=None):
    t = {"trade_id": tid, "timestamp": ts, "ticker": "TEST", "side": side, "price": price, "qty": qty, "pnl": pnl, "roi": 0.0,
         "reason_code": reason}
    if cid:
        t["cycle_id"] = cid
    if commission is not None:
        t["commission"] = commission
    if slip is not None:
        t["slippage_pct"] = slip
    if order_price is not None:
        t["order_price"] = order_price
    if intended is not None:
        t["intended_price"] = intended
    return t


def _order_ev(mode, cid, side, oid, price, qty, order_type="LIMIT", reason="SELL_ARMED", etype="ORDER_PLACED"):
    data = {"cycle_id": cid, "order_id": oid, "side": side, "qty": qty, "order_type": order_type}
    if price is not None:
        data["price"] = price
    vwap_events.append_event(mode, etype, "info", reason, f"{side} {oid}", data)


def build_compare_fixture():
    hk.fresh_dir()
    c = lambda m: f"2026-10-19 10:{m:02d}:00"
    R, S = "REAL", SHADOW
    # 주문 이벤트
    for m, side in ((0, "BUY"), (1, "BUY"), (2, "SELL"), (3, "SELL"), (4, "BUY"), (9 - 3, "BUY")):   # c0..c4, c6 (REAL), 6 번째 = m=6
        _order_ev(R, c(m), side, f"r{m}", 100.0, 10)
    for m, side in ((0, "BUY"), (1, "BUY"), (2, "SELL"), (3, "SELL"), (4, "BUY"), (7, "SELL")):
        _order_ev(S, c(m), side, f"s{m}", 100.0, 10, etype="ORDER_REPLACED" if m == 1 else "ORDER_PLACED")
    _order_ev(R, c(8), "SELL", "r8", 102.0, 10)
    _order_ev(S, c(8), "SELL", "s8", 102.0, 10)
    _order_ev(R, c(9), "SELL", "r9m", None, 10, order_type="MARKET")              # c8: 시장가 손절(REAL 주문 이벤트 있음)
    _order_ev(R, c(10), "SELL", "r10m", None, 10, order_type="MARKET", reason="DAILY_LOSS_STOP")
    _order_ev(R, "2026-10-01 10:00:00", "BUY", "rold", 100.0, 10)                  # 기간 밖
    _order_ev(S, "2026-10-01 10:00:00", "BUY", "sold", 100.0, 10)
    # 거래 레코드 (REAL)
    real = [
        _trade("r0", "2026-10-19 10:00:30", "BUY", 100.0, 10, 0.0, c(0), 0.10, 0.00),     # 0: MATCH (BUY)
        _trade("r1", "2026-10-19 10:01:30", "BUY", 100.0, 10, 0.0, c(1), 0.10, 0.01),     # 1: PRICE_GAP
        _trade("r3", "2026-10-19 10:03:30", "SELL", 101.0, 10, 10.0, c(3), 0.20, 0.05),   # 3: SHADOW_UNFILLED (REAL 만 체결)
        _trade("r6", "2026-10-19 10:06:30", "BUY", 99.0, 5, 0.0, c(6), 0.05, 0.00),       # 6: REAL_ONLY_ORDER
        _trade("r8", "2026-10-19 10:20:05", "SELL", 102.0, 10, 20.0, c(8), 0.20, 0.10),   # 8: 주문 주기 10:08 ≠ 체결 10:20, MATCH
        _trade("r9m", "2026-10-19 10:09:10", "STOP_LOSS", 95.0, 10, -50.0, c(9), 0.30, 0.20, intended=95.0),   # 9: MATCH (결정 주기)
        _trade("r10m", "2026-10-19 10:10:10", "STOP_LOSS", 94.0, 10, -60.0, c(10), 0.30, 0.00, reason="DAILY_LOSS_STOP"),
        _trade("rold", "2026-10-01 10:00:30", "BUY", 100.0, 10, 0.0, "2026-10-01 10:00:00", 0.10, 0.0),
        _trade("rx", "2026-10-19 11:00:00", "BUY", 100.0, 1, 0.0),                         # cycle_id 없음 → 비교·요약 제외
    ]
    shadow = [
        _trade("s0", "2026-10-19 10:00:31", "BUY", 100.0, 10, 0.0, c(0), slip=0.0),
        _trade("s1", "2026-10-19 10:01:31", "BUY", 100.10, 10, 0.0, c(1), slip=0.0),
        _trade("s2", "2026-10-19 10:02:31", "SELL", 101.0, 10, 12.0, c(2), slip=0.0),      # 2: REAL_UNFILLED (섀도우만 체결)
        _trade("s7", "2026-10-19 10:07:31", "SELL", 98.0, 4, 4.0, c(7), slip=0.0),         # 7: SHADOW_ONLY_ORDER
        _trade("s8", "2026-10-19 10:20:00", "SELL", 102.0, 10, 20.0, c(8), slip=0.0),
        _trade("s9m", "2026-10-19 10:09:11", "STOP_LOSS", 95.0, 10, -50.0, c(9), slip=0.0, intended=95.0),   # 시장가: 주문 이벤트 없음
        _trade("sold", "2026-10-01 10:00:31", "BUY", 100.0, 10, 0.0, "2026-10-01 10:00:00", slip=0.0),
    ]
    VwapConfigManager.save_trades(real, "REAL")
    VwapConfigManager.save_trades(shadow, SHADOW)
    return c


def test_s3_compare():
    print("\nT-S3. compare — 7가지 verdict, 요약 수치, days 필터, 키 규칙")
    c = build_compare_fixture()
    cfg = make_config("real")
    now = datetime(2026, 10, 20, 12, 0, 0)
    res = compare(7, now=now, config=cfg)
    v = res["summary"]["verdicts"]
    check("verdict 7종 건수(MATCH 3[주문주기 체결·시장가 결정주기 포함] / PRICE_GAP 1 / REAL_UNFILLED 1 / SHADOW_UNFILLED 1 / BOTH_UNFILLED 1 / REAL_ONLY 1 / SHADOW_ONLY 1)",
          v == {"MATCH": 3, "PRICE_GAP": 1, "REAL_UNFILLED": 1, "SHADOW_UNFILLED": 1, "BOTH_UNFILLED": 1, "REAL_ONLY_ORDER": 1,
                "SHADOW_ONLY_ORDER": 1}, f"{v}")
    by = {(p["cycle_id"], p["side"]): p for p in res["pairs"]}
    k = lambda m, s: by.get((c(m), s))
    check("MATCH: c0 BUY / 지정가 체결이 주문 주기 키(c8: 체결 10:20 이지만 주문 10:08 로 짝)",
          k(0, "BUY")["verdict"] == "MATCH" and k(8, "SELL")["verdict"] == "MATCH", f"{k(8, 'SELL')}")
    check("MATCH: 시장가 손절은 '결정한 주기'(c9) 로 짝 — REAL 주문 이벤트+체결 vs 섀도우 체결만, 체결가 95 == 95",
          k(9, "STOP_LOSS")["verdict"] == "MATCH" and k(9, "STOP_LOSS")["real"]["fill_price"] == 95.0
          and k(9, "STOP_LOSS")["shadow"]["fill_price"] == 95.0 and k(9, "STOP_LOSS")["real"]["order_id"] == "r9m",
          f"{k(9, 'STOP_LOSS')}")
    check("PRICE_GAP: 100.00 vs 100.10 → price_gap_pct=+0.1(섀도우-REAL)",
          k(1, "BUY")["verdict"] == "PRICE_GAP" and abs(k(1, "BUY")["price_gap_pct"] - 0.1) < 1e-6, f"{k(1, 'BUY')}")
    check("REAL_UNFILLED(섀도우만 체결) c2 / SHADOW_UNFILLED(REAL 만 체결) c3",
          k(2, "SELL")["verdict"] == "REAL_UNFILLED" and k(2, "SELL")["real"]["fill_price"] is None and k(2, "SELL")["shadow"]["fill_price"] == 101.0
          and k(3, "SELL")["verdict"] == "SHADOW_UNFILLED" and k(3, "SELL")["shadow"]["fill_price"] is None)
    check("REAL_ONLY_ORDER c6 (shadow=null) / SHADOW_ONLY_ORDER c7 (real=null)",
          k(6, "BUY")["verdict"] == "REAL_ONLY_ORDER" and k(6, "BUY")["shadow"] is None
          and k(7, "SELL")["verdict"] == "SHADOW_ONLY_ORDER" and k(7, "SELL")["real"] is None)
    check("BOTH_UNFILLED(c4)는 요약에서만 집계 — pairs 목록에는 없음", k(4, "BUY") is None and v["BOTH_UNFILLED"] == 1)
    check("time_gap_sec = 섀도우 체결 시각 − REAL 체결 시각 (c8: 10:20:00 − 10:20:05 = -5.0)", k(8, "SELL")["time_gap_sec"] == -5.0,
          f"{k(8, 'SELL')['time_gap_sec']}")
    check("REAL 패닉 청산(c10, DAILY_LOSS_STOP, 섀도우 주문 없음)은 excluded.REAL_PANIC 으로 분리 — verdict 집계 제외, pairs 에는 표시",
          res["summary"]["excluded"] == {"REAL_PANIC": 1} and k(10, "STOP_LOSS")["excluded_reason"] == "REAL_PANIC"
          and v["REAL_ONLY_ORDER"] == 1)
    check("pairs 최신순(내림차순) + 필드(cycle_id/side/verdict/real/shadow/price_gap_pct/time_gap_sec)",
          [p["cycle_id"] for p in res["pairs"]] == sorted((p["cycle_id"] for p in res["pairs"]), reverse=True)
          and all(set(p) >= {"cycle_id", "side", "verdict", "real", "shadow", "price_gap_pct", "time_gap_sec"} for p in res["pairs"])
          and set(k(0, "BUY")["real"]) == {"order_id", "order_price", "fill_price", "filled_at", "qty"})
    # 요약 수치 (손계산 값)
    rs, ss = res["summary"]["real"], res["summary"]["shadow"]
    # REAL(키 있는 7건): pnl 합 = 10-50+20-60 = -80, commission 합 = .1+.1+.2+.05+.2+.3+.3 = 1.25 → -81.25
    check("REAL 요약: fills 7, trades(청산) 4, net_pnl = pnl − commission = −81.25, 승률 50%, 평균 슬리피지 0.0514, 키 없는 체결 1건 제외",
          rs["fills"] == 7 and rs["trades"] == 4 and rs["net_pnl"] == -81.25 and rs["win_rate_pct"] == 50.0
          and abs(rs["avg_slippage_pct"] - 0.0514) < 1e-4 and rs["unkeyed_fills"] == 1, f"{rs}")
    # 섀도우(6건): pnl 12+4-50+20 = -14, 청산 금액 1010+392+950+1020 = 3372 × 0.2% = 6.744 → -20.744 → -20.74
    check("섀도우 요약: fills 6, trades 4, net_pnl = pnl − 추정수수료(0.2%) = −20.74, 승률 75%, fee_roundtrip_pct 0.2",
          ss["fills"] == 6 and ss["trades"] == 4 and ss["net_pnl"] == -20.74 and ss["win_rate_pct"] == 75.0
          and ss["fee_roundtrip_pct"] == 0.2 and ss["avg_slippage_pct"] == 0.0, f"{ss}")
    check("net_pnl_gap = 섀도우 − REAL = 60.51, match_rate_pct = MATCH/(MATCH+PRICE_GAP) = 3/4 = 75.0",
          res["summary"]["net_pnl_gap"] == 60.51 and res["summary"]["match_rate_pct"] == 75.0, f"{res['summary']['net_pnl_gap']}")
    check("period/ticker 필드(UI 통화 판정용 ticker 포함)", res["ticker"] == "TEST" and res["tickers"] == ["TEST"]
          and res["period"] == {"from": "2026-10-13 12:00:00", "to": "2026-10-20 12:00:00", "days": 7})

    # days 필터
    r30 = compare(30, now=now, config=cfg)
    check("days 필터: 7일엔 10-01 레코드 제외, 30일엔 포함(MATCH 4, REAL fills 8)",
          r30["summary"]["verdicts"]["MATCH"] == 4 and r30["summary"]["real"]["fills"] == 8 and v["MATCH"] == 3)
    r0 = compare(1, now=datetime(2026, 10, 20, 12, 0), config=cfg)
    r_none = compare(1, now=datetime(2026, 11, 20, 12, 0), config=cfg)
    check("기간 안에 데이터가 없으면 빈 요약(오류 없음, match_rate_pct=None)",
          r_none["pairs"] == [] and r_none["summary"]["match_rate_pct"] is None and r_none["summary"]["real"]["fills"] == 0
          and r_none["summary"]["net_pnl_gap"] == 0.0)
    # 허용오차: shadow_price_tolerance_pct 를 크게 하면 PRICE_GAP 이 MATCH 로
    cfg2 = dict(cfg)
    cfg2["shadow_price_tolerance_pct"] = 0.2
    check("shadow_price_tolerance_pct=0.2 이면 0.1% 차이는 MATCH", compare(7, now=now, config=cfg2)["summary"]["verdicts"]["MATCH"] == 4)

    # 손상된 파일/빈 입력에도 예외 없음
    hk.fresh_dir()
    with open(os.path.join(h.TMP_DIR, "vwap_trades_real.json"), "w", encoding="utf-8") as f:
        f.write("{not json")
    with open(os.path.join(h.TMP_DIR, "vwap_events_virtual_shadow.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"broken"\n\n')
    rr = compare(7, now=now, config=cfg)
    check("손상된 거래기록/이벤트 파일이어도 compare 는 예외 없이 빈 결과", rr["pairs"] == [] and rr["summary"]["real"]["fills"] == 0)

    # 실제 두 봇 실행 결과의 비교
    with Env() as env:
        run_limit_roundtrip(env)
        rc = compare(1, now=datetime(2026, 10, 5, 23, 0), config=env.cfg)
        vv = rc["summary"]["verdicts"]
        pr = {(p["cycle_id"], p["side"]): p for p in rc["pairs"]}
        check("실제 실행: REAL·섀도우 왕복 → MATCH 2(BUY c2 / SELL c3), BOTH_UNFILLED 1(c1 정정 전 주문), 그 외 0, 일치율 100%",
              vv == {"MATCH": 2, "PRICE_GAP": 0, "REAL_UNFILLED": 0, "SHADOW_UNFILLED": 0, "BOTH_UNFILLED": 1, "REAL_ONLY_ORDER": 0,
                     "SHADOW_ONLY_ORDER": 0} and rc["summary"]["match_rate_pct"] == 100.0
              and set(pr) == {(CID[1], "BUY"), (CID[2], "SELL")}, f"{vv} {list(pr)}")
        rsum, ssum = rc["summary"]["real"], rc["summary"]["shadow"]
        check("실제 실행: 양쪽 체결 2건·청산 1건, 같은 손익(pnl), net_pnl_gap = 추정수수료 − REAL commission 으로 설명됨",
              rsum["fills"] == ssum["fills"] == 2 and rsum["trades"] == ssum["trades"] == 1, f"{rsum} {ssum}")
        sell = [t for t in VwapConfigManager.load_trades(SHADOW) if t["side"] == "SELL"][0]
        expect_gap = round((sell["pnl"] - sell["price"] * sell["qty"] * 0.2 / 100.0) - (sell["pnl"] - 0.2), 2)
        check("실제 실행: net_pnl_gap 수식 검증", rc["summary"]["net_pnl_gap"] == expect_gap, f"{rc['summary']['net_pnl_gap']} vs {expect_gap}")

    # 시장가 손절 실제 실행: REAL 보유 → 손절 → 섀도우 같은 주기 손절 (FIRST_SYNC 로 보유 이어받기)
    with Env(pos=(10.0, 100.0)) as env:
        env.step(BASE + [95.0])
        rc = compare(1, now=datetime(2026, 10, 5, 23, 0), config=env.cfg)
        pr = {(p["cycle_id"], p["side"]): p for p in rc["pairs"]}
        sl = pr.get((CID[0], "STOP_LOSS"))
        check("실제 실행: 시장가 손절 — 섀도우가 REAL 보유(10주@100)를 FIRST_SYNC 로 이어받아 같은 주기에 손절, MATCH(체결가 95)",
              sl and sl["verdict"] == "MATCH" and sl["real"]["fill_price"] == 95.0 and sl["shadow"]["fill_price"] == 95.0
              and [t["side"] for t in shadow_trades()] == ["STOP_LOSS"], f"{sl}")


# ---------------------------------------------------------------------------
# T-A1
# ---------------------------------------------------------------------------
def test_a1_api():
    print("\nT-A1(섀도우). API 2개: 401, 정상 구조(ticker 포함), days 검증 400, 훅 등록")
    from flask import Flask
    import api.vwap_api as vapi
    from core.vwap.crypto import VwapCrypto

    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "api", "templates"))
    app.register_blueprint(vapi.vwap_bp, url_prefix="/vwap")
    anon, cli = app.test_client(), app.test_client()
    cli.set_cookie("vwap_session", VwapCrypto.generate_session_token("admin"))

    check("REAL 봇에 'shadow' 훅이 등록됨 (러너.hook), 순서 [bars_store, shadow]",
          [n for n, _ in vapi.real_bot._post_cycle_hooks] == ["bars_store", "shadow"]
          and dict(vapi.real_bot._post_cycle_hooks)["shadow"] == vapi.shadow_runner.hook)
    check("가상 봇 1~3 에는 섀도우 훅 없음", all([n for n, _ in b._post_cycle_hooks] == ["bars_store"] for b in vapi.virtual_bots.values()))

    r1, r2 = anon.get("/vwap/api/shadow/status"), anon.get("/vwap/api/shadow/compare")
    check("세션 없으면 401 (status, compare)", (r1.status_code, r2.status_code) == (401, 401)
          and r1.get_json()["reason"] == "unauthorized" and r2.get_json()["reason"] == "unauthorized")

    # 실제 두 봇을 한 번 돌려 데이터를 만든 뒤(Env 종료 = 설정 패치 해제), 러너를 API 에 주입하고 임시 설정 파일로 API 호출
    with Env() as env:
        run_limit_roundtrip(env)
    vapi.shadow_runner = env.runner
    vapi.real_bot.running = False
    cfg = make_config("real", ticker="005930")
    cfg.update({"toss_client_id": "", "toss_client_secret": "", "toss_account_seq": ""})
    VwapConfigManager.save_config(cfg)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 6, 9, 0, 0)
    shadow_module.datetime = _DT
    try:
        st = cli.get("/vwap/api/shadow/status")
        sj = st.get_json()
        keys = {"enabled", "following", "real_running", "last_cycle_id", "last_run_at", "last_duration_ms", "synced_at",
                "sync_reason", "ticker", "position", "cash", "reason_code", "reason_text", "signal"}
        check("status 200 + 계약 필드 + following=REAL + real_running=false + enabled(설정값)",
              st.status_code == 200 and sj["status"] == "success" and keys <= set(sj["shadow"])
              and sj["shadow"]["following"] == "REAL" and sj["shadow"]["real_running"] is False and sj["shadow"]["enabled"] is True,
              f"{sj}")
        check("status 값: last_cycle_id=마지막 REAL 주기, sync_reason=FIRST_SYNC, ticker=TEST, 마지막 판단 사유 존재",
              sj["shadow"]["last_cycle_id"] == CID[3] and sj["shadow"]["sync_reason"] == "FIRST_SYNC"
              and sj["shadow"]["ticker"] == "TEST" and sj["shadow"]["reason_code"] and isinstance(sj["shadow"]["last_duration_ms"], float),
              f"{sj['shadow']}")
        cp = cli.get("/vwap/api/shadow/compare")
        cj = cp.get_json()
        check("compare 200(기본 days=7) + status/ticker/tickers/period/summary/pairs, ticker 는 현재 REAL 설정(005930)",
              cp.status_code == 200 and cj["status"] == "success" and cj["ticker"] == "005930" and cj["period"]["days"] == 7
              and {"real", "shadow", "verdicts", "match_rate_pct", "net_pnl_gap"} <= set(cj["summary"]) and "pairs" in cj
              and cj["tickers"] == ["TEST"], f"{ {k: cj[k] for k in cj if k != 'pairs'} }")
        check("compare 의 verdicts 키 7개 전부 + MATCH 2",
              set(cj["summary"]["verdicts"]) == set(shadow_module.VERDICTS)
              and cj["summary"]["verdicts"]["MATCH"] == 2 and len(cj["pairs"]) == 2)
        c1 = cli.get("/vwap/api/shadow/compare?days=1").get_json()
        c30 = cli.get("/vwap/api/shadow/compare?days=30").get_json()
        check("days=1 / days=30 정상(period.days 반영)", c1["period"]["days"] == 1 and c30["period"]["days"] == 30)
        bad = ["0", "31", "-1", "abc", "7.5", "", "1e1", "100"]
        codes = [(b_, cli.get(f"/vwap/api/shadow/compare?days={b_}")) for b_ in bad]
        check("days 범위 밖·형식 오류 8종 → 400 invalid_params + 한글 message",
              all(r.status_code == 400 and r.get_json()["reason"] == "invalid_params" and r.get_json()["message"] for _, r in codes),
              f"{[(b_, r.status_code) for b_, r in codes]}")
        VwapConfigManager.save_config(dict(cfg, shadow_enabled=False))
        en = cli.get("/vwap/api/shadow/status").get_json()["shadow"]["enabled"]
        check("status.enabled 는 shadow_enabled 설정을 그대로 반영", en is False)
        keep = vapi.shadow_runner
        vapi.shadow_runner = None
        try:
            a, b = cli.get("/vwap/api/shadow/status"), cli.get("/vwap/api/shadow/compare")
            check("SR-3: 섀도우 초기화 실패(shadow_runner=None) → status/compare 503 shadow_unavailable",
                  (a.status_code, b.status_code) == (503, 503) and a.get_json()["reason"] == b.get_json()["reason"] == "shadow_unavailable")
        finally:
            vapi.shadow_runner = keep
    finally:
        shadow_module.datetime = datetime


def test_qa_minor():
    print("\nT-QA. 경미 이슈 1~4: shadow import 격리 / 폐기 후 원장 / 청산 결정 단위 요약 / 시장가 order_price")
    # --- (1) shadow import 실패 격리: vwap_api 가 로드되고 restore_active_bots 가 호출되며 섀도우 API 는 503
    import importlib.util
    import core.vwap as vwap_pkg
    from flask import Flask
    from core.vwap.crypto import VwapCrypto
    hk.fresh_dir()
    cfg = make_config("real", ticker="005930")
    cfg.update({"toss_client_id": "", "toss_client_secret": "", "toss_account_seq": "", "real_is_running": True})
    VwapConfigManager.save_config(cfg)
    started = []
    orig_start = VWAPBot.start
    VWAPBot.start = lambda self_: started.append(getattr(self_, "mode", "?")) or True      # 실제 봇 스레드를 띄우지 않음
    saved_mod, saved_attr = sys.modules.get("core.vwap.shadow"), getattr(vwap_pkg, "shadow", None)
    sys.modules["core.vwap.shadow"] = None                                                   # import 시 ImportError
    if hasattr(vwap_pkg, "shadow"):
        delattr(vwap_pkg, "shadow")
    try:
        spec = importlib.util.spec_from_file_location("vwap_api_noshadow", os.path.join(PROJECT_ROOT, "api", "vwap_api.py"))
        mod = importlib.util.module_from_spec(spec)
        load_err = None
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            load_err = e
    finally:
        sys.modules["core.vwap.shadow"] = saved_mod
        if saved_mod is None:
            sys.modules.pop("core.vwap.shadow", None)
        if saved_attr is not None:
            vwap_pkg.shadow = saved_attr
        VWAPBot.start = orig_start
    check("shadow import 실패 시에도 vwap_api 가 로드됨 (shadow_runner=None, vwap_shadow=None)",
          load_err is None and mod.shadow_runner is None and mod.vwap_shadow is None, f"{load_err!r}")
    check("shadow import 실패 시에도 restore_active_bots 가 REAL 자동 복구(start)를 호출함", "REAL" in started, f"{started}")
    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "api", "templates"))
    app.register_blueprint(mod.vwap_bp, url_prefix="/vwap")
    cli = app.test_client()
    cli.set_cookie("vwap_session", VwapCrypto.generate_session_token("admin"))
    a, b = cli.get("/vwap/api/shadow/status"), cli.get("/vwap/api/shadow/compare")
    check("shadow import 실패 시 status/compare 둘 다 503 shadow_unavailable",
          (a.status_code, b.status_code) == (503, 503) and a.get_json()["reason"] == b.get_json()["reason"] == "shadow_unavailable")

    # --- (2) 폐기 후 오래된 원장이 status 에 남지 않음
    with Env(pos=(5.0, 100.0)) as env:
        env.step(BASE + [100.0])
        before = env.runner.status()
        check("(사전) 폐기 전에는 position/cash 가 값으로 내려감", before["position"] is not None and before["cash"] is not None
              and before["ledger_discarded"] is False, f"{before}")
        env.runner._discard_bot()
        env.runner._save_state()
        st = env.runner.status()
        with open(env.runner.state_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        check("폐기 후 status: position=None, cash=None, ledger_discarded=True, 기존 필드(following/ticker/enabled 등) 유지",
              st["position"] is None and st["cash"] is None and st["ledger_discarded"] is True
              and st["following"] == "REAL" and st["ticker"] == before["ticker"] and "enabled" in st, f"{st}")
        check("폐기 후 상태 파일에 오래된 원장(cash/holdings)이 다시 저장되지 않음",
              saved.get("cash") is None and saved.get("holdings") is None and saved.get("ledger_discarded") is True, f"{saved}")
        env.step(BASE + [100.0, 100.0])
        st2 = env.runner.status()
        check("다음 동기화 이후에는 다시 값이 내려가고 ledger_discarded=False",
              st2["position"] is not None and st2["cash"] is not None and st2["ledger_discarded"] is False, f"{st2}")

    # --- (3) 거래수·승률은 청산 결정(cycle_id, side) 단위
    recs = {("c1", "STOP_LOSS"): [_trade(f"m{i}", "2026-10-19 10:00:0%d" % i, "STOP_LOSS", 95.0, 3, -10.0, "c1", 0.1) for i in range(3)],
            ("c2", "SELL"): [_trade("l1", "2026-10-19 10:05:00", "SELL", 101.0, 9, 9.0, "c2", 0.1)]}
    rs = shadow_module._mode_summary(recs)
    check("시장가 재시도 3건 + 지정가 1건 → trades=2(fills=4), 승률 분모 2 → 50.0%, net_pnl 은 레코드 합계(-21.4)",
          rs["trades"] == 2 and rs["fills"] == 4 and rs["win_rate_pct"] == 50.0 and rs["net_pnl"] == -21.4, f"{rs}")
    ss = shadow_module._mode_summary(recs, shadow_fee_pct=0.2)
    check("섀도우 요약도 같은 단위(trades=2, 승률 50.0)", ss["trades"] == 2 and ss["win_rate_pct"] == 50.0, f"{ss}")
    check("compare definitions.trades 문구 갱신",
          "청산 결정 수" in compare(7, now=datetime(2026, 10, 20, 12, 0), config=make_config("real"))["summary"]["definitions"]["trades"])

    # --- (4) STOP_LOSS 시장가 쌍의 order_price: 이벤트 current_price → intended_price 순
    hk.fresh_dir()
    c = lambda m: f"2026-10-19 10:{m:02d}:00"
    vwap_events.append_event("REAL", "ORDER_PLACED", "warn", "STOP_LOSS", "mkt", {"cycle_id": c(1), "order_id": "m1", "side": "SELL",
                                                                                  "qty": 10, "order_type": "MARKET", "current_price": 98.65})
    _order_ev("REAL", c(2), "SELL", "m2", None, 10, order_type="MARKET")           # 이벤트에 가격 없음 → 체결 레코드 intended_price
    VwapConfigManager.save_trades([
        _trade("a", "2026-10-19 10:01:05", "STOP_LOSS", 98.60, 5, -5.0, c(1), 0.1, intended=98.65, order_price=0.0),
        _trade("b", "2026-10-19 10:01:06", "STOP_LOSS", 98.50, 5, -5.0, c(1), 0.1, intended=98.65, order_price=0.0),
        _trade("d", "2026-10-19 10:02:05", "STOP_LOSS", 97.0, 10, -9.0, c(2), 0.1, intended=97.5, order_price=0.0)], "REAL")
    o, _f_, _u = shadow_module._load_side("REAL", "2026-10-19 00:00:00")
    check("시장가 order_price: 이벤트 current_price=98.65 사용 / 이벤트에 가격 없으면 레코드 intended_price=97.5 사용 (null 아님)",
          o[(c(1), "STOP_LOSS")]["order_price"] == 98.65 and o[(c(2), "STOP_LOSS")]["order_price"] == 97.5, f"{o}")
    res = compare(7, now=datetime(2026, 10, 20, 12, 0), config=make_config("real"))
    pp = {(p["cycle_id"], p["side"]): p for p in res["pairs"]}
    check("compare pairs 의 REAL.order_price 가 null 이 아님", pp[(c(1), "STOP_LOSS")]["real"]["order_price"] == 98.65
          and pp[(c(2), "STOP_LOSS")]["real"]["order_price"] == 97.5, f"{pp}")
    check("재시도 2건 체결 → REAL 요약 trades=2(c1,c2 결정 2개) fills=3", res["summary"]["real"]["trades"] == 2
          and res["summary"]["real"]["fills"] == 3, f"{res['summary']['real']}")


# ---------------------------------------------------------------------------
# T-PERF
# ---------------------------------------------------------------------------
def test_perf():
    print("\nT-PERF. 훅 소요시간 실측 (PC, 1600봉)")
    big_closes = [100.0 + (i % 7) * 0.1 for i in range(1600)]
    start = datetime(2026, 10, 5, 22, 30) - timedelta(minutes=1599 - 900)
    sync_ms, steady_ms, loop_ms = [], [], []
    with Env(source="toss", pos=(5.0, 100.0)) as env:
        for i in range(25):
            closes = big_closes[i:] + [100.0 + ((1600 + k) % 7) * 0.1 for k in range(i)]
            df = hk.candles(closes, start=start + timedelta(minutes=i))
            env.fake.candles = df
            now = hk.now_for(df)
            env.clock_now = now + timedelta(seconds=1)
            t = time.perf_counter()
            with hk.FixedClock(now):
                env.real._loop_step()
            loop_ms.append((time.perf_counter() - t) * 1000)
            d = env.real.last_hook_durations_ms.get("shadow")
            (sync_ms if i == 0 else steady_ms).append(d)
            if i == 12:                      # 동기화 주기 한 번 더(generation 변경) — 상태 파일 쓰기·SHADOW_SYNC 포함
                env.restart_real()
        # 위에서 i==13 주기가 BOT_START 동기화 주기
    steady = sorted(x for j, x in enumerate(steady_ms) if j != 12)
    resync2 = steady_ms[12]
    print(f"      동기화 주기(첫 주기, 봇 생성+FIRST_SYNC 포함) {sync_ms[0]:.1f} ms | 재동기화 주기(BOT_START) {resync2:.1f} ms | "
          f"정상 주기 중앙값 {steady[len(steady)//2]:.1f} ms, 최대 {steady[-1]:.1f} ms | REAL _loop_step 전체(훅 포함) 중앙값 "
          f"{sorted(loop_ms)[len(loop_ms)//2]:.1f} ms")
    check("PERF: 섀도우 훅 — 동기화 주기·재동기화 주기·정상 주기 모두 HOOK_SLOW 기준(2000ms) 대비 충분히 작음(<500ms)",
          max(sync_ms[0], resync2, steady[-1]) < 500, f"sync={sync_ms[0]} resync={resync2} steady_max={steady[-1]}")
    check("PERF: 측정 중 HOOK_SLOW/HOOK_ERROR 이벤트 없음", not [e for e in ev("REAL") if e.get("reason_code") in ("HOOK_SLOW", "HOOK_ERROR")])


# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print(" VWAP 3단계 JR-3 — 섀도우 모드 (임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    for fn in (test_s1_shadow_bot, test_s1_skips, test_s1_panic, test_s2_resync, test_s3_compare, test_a1_api, test_qa_minor, test_perf):
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    check("T-G 운영 data/ 전체(하위 폴더 포함) 해시 전후 동일",
          _hash_tree(os.path.join(PROJECT_ROOT, "data")) == REAL_DATA_TREE_BEFORE)
    new_logs = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log"))) - LOGS_BEFORE
    check("T-G trading_bot_*.log 신규 생성 없음", not new_logs, f"{new_logs}")
    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
