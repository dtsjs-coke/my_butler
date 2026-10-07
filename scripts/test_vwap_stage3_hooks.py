"""
VWAP 3단계 SR-2 검증 — REAL/가상 공통 post-cycle 훅, cycle_id, 봉 적재 연결 (설계 문서 §5.1·§6·§11 T-H1/T-H2).
네트워크 없이 실행됩니다.

검증 항목
  T-H1  훅 격리
        a) 예외를 던지는 훅 + 3초 지연 훅 + ctx 를 훼손하는 훅이 있어도 REAL 주문(placed)/취소(canceled) 시퀀스,
           거래 기록, 추적 주문, 최종 신호가 '훅 없음'과 완전히 같음 (가상 VIRTUAL_1 도 동일)
        b) 지연 경고(HOOK_SLOW) ERROR 이벤트 1회, 예외(HOOK_ERROR) ERROR 이벤트 1회(10분 억제), 알림 0회
        c) 훅은 그 주기의 주문·체결 판정·_finish_cycle 이 모두 끝난 뒤 실행 (훅 시점 주문 수 == 주기 종료 후 주문 수)
        d) 본체 예외 주기에도 훅 실행 + 원래 예외가 그대로 전파, 캔들 조회 실패 주기에도 훅 실행(df 비어 있음)
        e) ctx 필드: mode/generation/cycle_id/market/평탄 bars_store_enabled/비밀값 빈 문자열/position/cash/candles_source/asof
        f) 훅 등록·교체·제거 API, ATTACH_BARS_STORE=False 하위 클래스는 봉 적재 훅 없음
  T-H2  cycle_id 가 추적 주문·REAL 거래 레코드(지정가·시장가)·ORDER_PLACED/FILL 이벤트·가상 거래 레코드에 기록됨
  T-H3  봉 적재 연결(REAL/가상 공통): 마감 봉만, 세션 마지막 봉(asof 기준), ctx market 사용, mock/출처불명/비활성 시 미적재,
        두 봇이 같은 종목을 적재해도 중복 0
  T-H4  TossBroker.last_candles_source — mock_mode 에서 Yahoo 성공이면 "yahoo", 난수 봉이면 "mock" (네트워크 mock)
  T-PERF 훅 소요시간 실측 (1600봉: 첫 적재/정상 주기), 훅 유무에 따른 _loop_step 소요시간
  T-G   운영 data/ 해시 전후 동일(하위 폴더 포함), trading_bot_*.log 신규 생성 없음

실행:  PYTHONUTF8=1 python scripts/test_vwap_stage3_hooks.py
"""
import os
import sys
import glob
import json
import time
import shutil
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

# 1단계 하네스 재사용: 임시 DATA_DIR 패치 + Discord 전송 no-op + 봇 로거 콘솔 전용
import test_vwap_reliability as h  # noqa: E402

for _name in ["vwap_bot_virtual_1", "vwap_bot_real", "vwap_bot_virtual_2"]:
    _lg = logging.getLogger(_name)
    if not _lg.handlers:
        _lg.addHandler(logging.NullHandler())
        _lg.propagate = False
    for _hd in _lg.handlers:
        _hd.setLevel(logging.CRITICAL)

import pandas as pd  # noqa: E402

import core.vwap.bot as bot_module  # noqa: E402
import core.vwap.config_manager as cm_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap import bars_store, events as vwap_events  # noqa: E402
from core.vwap import broker as broker_module  # noqa: E402

check = h.check
make_config = h.make_config
detail = h.detail
Patched = h.Patched

T0 = datetime(2026, 10, 5, 10, 0)  # 1분봉 시작 (KST). 미국장 22:30 리셋 기준 세션 2026-10-04


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
class FixedClock:
    """bot 모듈의 datetime.now() 고정."""

    def __init__(self, fixed):
        self.fixed = fixed

    def __enter__(self):
        self._orig = bot_module.datetime
        fixed = self.fixed

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        bot_module.datetime = _DT
        return self

    def __exit__(self, *a):
        bot_module.datetime = self._orig


def fresh_dir():
    """임시 DATA_DIR 비우기(하위 폴더 포함) + bars_store 메모리 상태 초기화 + 알림 억제 상태 초기화."""
    for name in os.listdir(h.TMP_DIR):
        p = os.path.join(h.TMP_DIR, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
    bars_store._reset_state()


def candles(closes, start=T0, last_high=None, last_low=None):
    rows = [{"time": start + timedelta(minutes=i), "open": c, "high": c, "low": c, "close": c, "volume": 1000.0}
            for i, c in enumerate(closes)]
    if last_high is not None:
        rows[-1]["high"] = last_high
    if last_low is not None:
        rows[-1]["low"] = last_low
    return pd.DataFrame(rows)


def now_for(df, extra_sec=5):
    """마지막 봉이 '진행 중'인 시각 (마지막 봉 시각 + extra_sec)."""
    return df["time"].iloc[-1].to_pydatetime() + timedelta(seconds=extra_sec)


class StatefulFake(h.FakeTossBroker):
    """주문을 내면 미체결에 올리고, 취소하면 CANCELED 상세를 돌려주는 REAL 가짜 브로커. 시장가는 즉시 FILLED."""

    def __init__(self, source="toss"):
        super().__init__("test_id", "test_secret", "1")
        self.last_candles_source = source
        self.balance = {"cash": 100000.0, "holdings": {}}

    def place_order(self, ticker, side, price, qty, order_type="LIMIT", client_order_id=None):
        oid = super().place_order(ticker, side, price, qty, order_type, client_order_id)
        if oid:
            if order_type == "MARKET":
                px = float(self.candles.iloc[-1]["close"])
                self.order_details[oid] = detail("FILLED", qty, px, side=side)
                held = self.balance["holdings"].get(ticker)
                if held:
                    held["qty"] = max(0.0, held["qty"] - qty)
                    if held["qty"] <= 0:
                        self.balance["holdings"].pop(ticker, None)
            else:
                self.open_orders.append({"order_id": oid, "ticker": ticker, "side": side,
                                         "price": float(price), "qty": float(qty)})
        return oid

    def cancel_order(self, order_id):
        self.canceled.append(order_id)
        self.open_orders = [o for o in self.open_orders if o["order_id"] != order_id]
        if order_id not in self.order_details:
            self.order_details[order_id] = detail("CANCELED", 0, None)
        return self.cancel_result

    def fill_open(self, side):
        """해당 방향 미체결 1건을 전량 체결 처리(목록에서 제거 + 상세 FILLED + 잔고 반영)."""
        for o in list(self.open_orders):
            if o["side"] == side:
                self.open_orders.remove(o)
                self.order_details[o["order_id"]] = detail("FILLED", o["qty"], o["price"], side=side)
                held = self.balance["holdings"].setdefault(o["ticker"], {"qty": 0.0, "entry_price": 0.0})
                if side == "BUY":
                    held["entry_price"] = o["price"]
                    held["qty"] += o["qty"]
                    self.balance["cash"] -= o["price"] * o["qty"]
                else:
                    held["qty"] -= o["qty"]
                    self.balance["cash"] += o["price"] * o["qty"]
                    if held["qty"] <= 0:
                        self.balance["holdings"].pop(o["ticker"], None)
                return o
        return None


# 시나리오 주기 (REAL): BUY 제출 → BUY 정정 → BUY 체결 + SELL 제출 → 손절(매도 취소 + 시장가)
SCENARIO = [
    ([100.0] * 30 + [98.0], None),
    ([100.0] * 30 + [98.0, 97.0], None),
    ([100.0] * 30 + [98.0, 97.0, 101.5], "BUY"),
    ([100.0] * 30 + [98.0, 97.0, 101.5, 90.0], None),
]


def run_real_scenario(hooks=None, keep_bars_hook=True, cfg_overrides=None):
    """REAL 봇으로 SCENARIO 실행. hooks: [(name, fn)] 추가 등록. 결과 dict 반환."""
    fresh_dir()
    fake = StatefulFake()
    cfg = make_config("real", **(cfg_overrides or {}))
    out = {"hook_seen": []}
    with Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
        if not keep_bars_hook:
            bot.remove_post_cycle_hook("bars_store")
        for name, fn in (hooks or []):
            bot.add_post_cycle_hook(name, fn)
        bot.running = True
        for closes, fill_side in SCENARIO:
            fake.candles = candles(closes)
            if fill_side:
                fake.fill_open(fill_side)
            with FixedClock(now_for(fake.candles)):
                bot._loop_step()
            out["hook_seen"].append(bot.last_hook_durations_ms)
        out.update({
            "bot": bot, "fake": fake,
            "placed": [(p["side"], round(p["price"], 4), p["qty"], p["order_type"]) for p in fake.placed],
            "canceled": list(fake.canceled),
            "trades": [(t["side"], t["price"], t["qty"], t.get("cycle_id")) for t in VwapConfigManager.load_trades("REAL")],
            "tracked": sorted(bot.tracked_open_orders.keys()),
            "signal": bot.status_cache.get("signal"),
            "reason": bot.status_cache.get("reason_code"),
            "events": read_events("REAL"),
        })
    return out


def read_events(mode):
    path = os.path.join(h.TMP_DIR, f"vwap_events_{mode.lower()}.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def bar_files():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(bars_store.bars_dir(), "*.csv")))


def bar_rows(name):
    with open(os.path.join(bars_store.bars_dir(), name), encoding="utf-8") as f:
        return [ln.strip().split(",") for ln in f.read().splitlines()[1:] if ln.strip()]


# ---------------------------------------------------------------------------
# T-H1 훅 격리
# ---------------------------------------------------------------------------
def test_h1_isolation():
    print("\nT-H1. 훅 격리 — 예외/3초 지연/ctx 훼손 훅이 있어도 REAL 주문 시퀀스 불변")

    base = run_real_scenario(keep_bars_hook=False)
    check("기준(훅 없음) 시나리오가 의도대로 진행: BUY 제출·정정, SELL 제출, 손절 시장가",
          [p[0] + "/" + p[3] for p in base["placed"]] == ["BUY/LIMIT", "BUY/LIMIT", "SELL/LIMIT", "SELL/MARKET"]
          and len(base["canceled"]) == 2 and [t[0] for t in base["trades"]] == ["BUY", "STOP_LOSS"],
          f"placed={base['placed']} canceled={base['canceled']} trades={base['trades']}")

    calls = {"slow": 0, "boom": 0, "mut": 0}
    seen_at_hook = []

    def slow_hook(ctx):
        calls["slow"] += 1
        if calls["slow"] == 1:
            time.sleep(3.0)

    def boom_hook(ctx):
        calls["boom"] += 1
        raise RuntimeError("hook boom")

    def mutating_hook(ctx):
        calls["mut"] += 1
        ctx["df"].drop(ctx["df"].index, inplace=True)       # df 복사본 훼손
        ctx["config"]["real_n_percent"] = 99.0              # 설정 복사본 훼손
        if ctx.get("position"):
            ctx["position"]["qty"] = -1
        ctx["cycle_id"] = "x"

    def order_count_hook(ctx):
        seen_at_hook.append(len(fake_ref[0].placed) if fake_ref else -1)

    fake_ref = []
    # order_count_hook 은 실행 중인 fake 를 봐야 하므로 첫 주기 전에 참조를 채움
    orig_init = StatefulFake.__init__

    def capture_init(self, *a, **k):
        orig_init(self, *a, **k)
        fake_ref[:] = [self]

    StatefulFake.__init__ = capture_init
    try:
        t0 = time.monotonic()
        withh = run_real_scenario(hooks=[("boom", boom_hook), ("slow", slow_hook), ("mut", mutating_hook),
                                         ("count", order_count_hook)])
        elapsed = time.monotonic() - t0
    finally:
        StatefulFake.__init__ = orig_init

    for key in ("placed", "canceled", "trades", "tracked", "signal", "reason"):
        check(f"a) REAL {key} 가 훅 없음과 동일", withh[key] == base[key], f"훅있음={withh[key]} 훅없음={base[key]}")
    check("a) 모든 훅이 매 주기 호출됨(4주기)", calls == {"slow": 4, "boom": 4, "mut": 4}, f"{calls}")
    check("a) 3초 지연 훅이 실제로 주기를 지연시킴(동기 실행) — 측정", elapsed >= 3.0, f"시나리오 전체 {elapsed:.2f}초")

    ev = read_events("REAL")
    slow_ev = [e for e in ev if e.get("type") == "ERROR" and e.get("reason_code") == "HOOK_SLOW"]
    boom_ev = [e for e in ev if e.get("type") == "ERROR" and e.get("reason_code") == "HOOK_ERROR"]
    check("b) 지연 경고 ERROR(HOOK_SLOW) 이벤트 정확히 1회", len(slow_ev) == 1,
          f"{[(e.get('message'), e.get('level')) for e in slow_ev]}")
    check("b) 예외 훅 ERROR(HOOK_ERROR) 이벤트 1회(4주기 반복 → 10분 억제)", len(boom_ev) == 1,
          f"{[e.get('message') for e in boom_ev]}")
    check("b) 훅 경고 이벤트 level=warn", all(e.get("level") == "warn" for e in slow_ev + boom_ev))
    check("a) 훅 ERROR 이벤트를 빼면 이벤트 종류 시퀀스가 훅 없음과 같음",
          [e.get("type") for e in ev if e.get("reason_code") not in ("HOOK_SLOW", "HOOK_ERROR")]
          == [e.get("type") for e in base["events"]],
          "")

    check("c) 훅 시점 주문 수 == 각 주기 종료 후 누적 주문 수 (주문이 모두 끝난 뒤 실행)",
          seen_at_hook == [1, 2, 3, 4], f"{seen_at_hook}")

    # d) 본체 예외 주기 / 캔들 조회 실패 주기
    fresh_dir()
    fake = StatefulFake()
    got = []
    with Patched(make_config("real"), fake):
        bot = VWAPBot("REAL")
        bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
        bot.add_post_cycle_hook("probe", lambda ctx: got.append(ctx))
        bot.running = True
        fake.candles = candles([100.0] * 30 + [98.0])

        def bad_balance():
            raise ValueError("balance exploded")

        fake.get_balance = bad_balance
        raised = None
        with FixedClock(now_for(fake.candles)):
            try:
                bot._loop_step()
            except Exception as e:
                raised = e
        check("d) 본체 예외가 그대로 전파(ValueError)", isinstance(raised, ValueError) and "exploded" in str(raised),
              f"{raised!r}")
        check("d) 본체 예외 주기에도 훅 실행 + reason_code=LOOP_ERROR + df/cycle_id 전달",
              len(got) == 1 and got[0]["reason_code"] == "LOOP_ERROR" and len(got[0]["df"]) == 31
              and got[0]["cycle_id"] == "2026-10-05 10:30:00" and got[0]["position"] is None and got[0]["cash"] is None,
              f"{[(c['reason_code'], c['cycle_id'], c['position']) for c in got]}")
        fake.candles = fake.candles.iloc[:0]
        with FixedClock(now_for(candles([1.0]))):
            bot._loop_step()
        check("d) 캔들 조회 실패(DATA_UNAVAILABLE) 주기에도 훅 실행, df 비어 있음, cycle_id 빈 값",
              len(got) == 2 and got[1]["reason_code"] == "DATA_UNAVAILABLE" and len(got[1]["df"]) == 0
              and got[1]["cycle_id"] == "", f"{got[-1]['reason_code'] if got else None}")

    # e) ctx 필드
    fresh_dir()
    fake = StatefulFake()
    got = []
    cfg = make_config("real", market="US")
    with Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
        bot.add_post_cycle_hook("probe", lambda ctx: got.append(ctx))
        bot.running = True
        bot._generation = 7
        fake.balance = {"cash": 5000.0, "holdings": {"TEST": {"qty": 3.0, "entry_price": 99.5}}}
        fake.candles = candles([100.0] * 30 + [100.2])
        nowt = now_for(fake.candles)
        with FixedClock(nowt):
            bot._loop_step()
    c = got[0] if got else {}
    expect_keys = {"mode", "generation", "cycle_id", "df", "ticker", "market", "interval", "reset_time",
                   "candles_source", "candles_asof", "config", "position", "cash", "reason_code", "running"}
    check("e) ctx 키 집합", set(c.keys()) == expect_keys, f"{sorted(c.keys())}")
    check("e) mode/generation/ticker/market/interval/reset_time",
          (c.get("mode"), c.get("generation"), c.get("ticker"), c.get("market"), c.get("interval"), c.get("reset_time"))
          == ("REAL", 7, "TEST", "US", "1m", "22:30"), f"{[c.get(k) for k in ('mode','generation','ticker','market')]}")
    check("e) config 는 평탄 dict 이고 bars_store_enabled 키 존재(True)",
          isinstance(c.get("config"), dict) and c["config"].get("bars_store_enabled") is True
          and "real_ticker" in c["config"])
    check("e) config 비밀값(toss_client_id/secret/account_seq, admin_password_hash)은 빈 문자열",
          all(c["config"].get(k) == "" for k in ("toss_client_id", "toss_client_secret", "toss_account_seq"))
          and c["config"].get("admin_password_hash", "") == "")
    check("e) 원본 설정은 훅 때문에 바뀌지 않음", cfg["toss_client_secret"] == "test_secret")
    check("e) position/cash/candles_source/asof/cycle_id",
          c.get("position") == {"qty": 3.0, "entry_price": 99.5} and c.get("cash") == 5000.0
          and c.get("candles_source") == "toss" and c.get("candles_asof") == nowt
          and c.get("cycle_id") == "2026-10-05 10:30:00",
          f"pos={c.get('position')} cash={c.get('cash')} src={c.get('candles_source')} asof={c.get('candles_asof')}")
    check("e) df 는 캔들 원본(vwap 등 계산 컬럼 없음)", list(c["df"].columns) == ["time", "open", "high", "low", "close", "volume"])

    # f) 등록 API
    with Patched(make_config("real"), StatefulFake()):
        b = VWAPBot("REAL")
        names0 = [n for n, _ in b._post_cycle_hooks]
        b.add_post_cycle_hook("x", lambda ctx: 1)
        b.add_post_cycle_hook("x", lambda ctx: 2)
        names1 = [n for n, _ in b._post_cycle_hooks]
        rm = b.remove_post_cycle_hook("x")
        bad = b.add_post_cycle_hook("y", "not callable")

        class NoBars(VWAPBot):
            ATTACH_BARS_STORE = False

        nb = NoBars("VIRTUAL_2")
        v1 = VWAPBot("VIRTUAL_1")
    check("f) 기본 등록 훅 = [bars_store] (REAL/가상 공통)",
          names0 == ["bars_store"] and [n for n, _ in v1._post_cycle_hooks] == ["bars_store"])
    check("f) 같은 이름은 교체(중복 등록 없음), 제거, 호출 불가 객체 거부",
          names1 == ["bars_store", "x"] and rm and not bad and [n for n, _ in b._post_cycle_hooks] == ["bars_store"])
    check("f) ATTACH_BARS_STORE=False 하위 클래스(섀도우용)는 훅 없음", nb._post_cycle_hooks == [])

    # 가상(VIRTUAL_1)도 훅 유무에 따라 주문·거래가 같은지
    def run_virtual(with_hooks):
        fresh_dir()
        fake = StatefulFake()
        with Patched(make_config("virtual_1"), fake):
            bot = VWAPBot("VIRTUAL_1")
            if with_hooks:
                bot.add_post_cycle_hook("boom", boom_hook)
                bot.add_post_cycle_hook("mut", mutating_hook)
            else:
                bot.remove_post_cycle_hook("bars_store")
            bot.running = True
            seq = [([100.0] * 30 + [98.0], None), ([100.0] * 30 + [98.0, 98.0], 97.0),
                   ([100.0] * 30 + [98.0, 98.0, 101.0], None), ([100.0] * 30 + [98.0, 98.0, 101.0, 101.0], "H")]
            for closes, lo in seq:
                vb = bot.virtual_broker
                if lo == "H":
                    sp = [o for o in vb.open_orders if o["side"] == "SELL"][0]["price"]
                    fake.candles = candles(closes, last_high=sp + 0.5)
                elif lo is not None:
                    bp = [o for o in vb.open_orders if o["side"] == "BUY"][0]["price"]
                    fake.candles = candles(closes, last_low=bp - 0.5)
                else:
                    fake.candles = candles(closes)
                with FixedClock(now_for(fake.candles)):
                    bot._loop_step()
            return ([(t["side"], t["price"], t["qty"]) for t in VwapConfigManager.load_trades("VIRTUAL_1")],
                    len(fake.placed))

    v_base, v_hooks = run_virtual(False), run_virtual(True)
    check("a) 가상(VIRTUAL_1) 거래 기록이 훅 유무와 무관하게 동일 + 실거래 주문 0회",
          v_base == v_hooks and v_base[1] == 0 and [t[0] for t in v_base[0]] == ["BUY", "SELL"], f"{v_base} vs {v_hooks}")


# ---------------------------------------------------------------------------
# T-H2 cycle_id 기록
# ---------------------------------------------------------------------------
def test_h2_cycle_id():
    print("\nT-H2. cycle_id 가 추적 주문·거래 레코드·이벤트에 기록")
    fresh_dir()
    fake = StatefulFake()
    tracked_snap = []
    with Patched(make_config("real"), fake):
        bot = VWAPBot("REAL")
        bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
        bot.add_post_cycle_hook("snap", lambda ctx: tracked_snap.append(
            {k: v.get("cycle_id") for k, v in bot.tracked_open_orders.items()}))
        bot.running = True
        for closes, fill_side in SCENARIO:
            fake.candles = candles(closes)
            if fill_side:
                fake.fill_open(fill_side)
            with FixedClock(now_for(fake.candles)):
                bot._loop_step()
    cid = ["2026-10-05 10:30:00", "2026-10-05 10:31:00", "2026-10-05 10:32:00", "2026-10-05 10:33:00"]
    check("추적 주문 cycle_id = 주문을 낸 주기의 마지막 봉 시각",
          tracked_snap[0] == {"real_1": cid[0]} and tracked_snap[1] == {"real_1": cid[0], "real_2": cid[1]}
          and tracked_snap[2].get("real_3") == cid[2], f"{tracked_snap[:3]}")
    tr = VwapConfigManager.load_trades("REAL")
    check("REAL 지정가 BUY 체결 레코드 cycle_id = 그 주문을 낸 주기(정정 주문 cycle 2), 체결 판정 주기 아님",
          tr and tr[0]["side"] == "BUY" and tr[0].get("cycle_id") == cid[1], f"{[(t['side'], t.get('cycle_id')) for t in tr]}")
    check("REAL 시장가 STOP_LOSS 레코드 cycle_id = 손절 결정 주기",
          len(tr) == 2 and tr[1]["side"] == "STOP_LOSS" and tr[1].get("cycle_id") == cid[3])
    ev = read_events("REAL")
    placed = [e for e in ev if e.get("type") in ("ORDER_PLACED", "ORDER_REPLACED")]
    check("ORDER_PLACED/REPLACED 이벤트 data.cycle_id (지정가 3건 + 시장가 1건)",
          [(e["type"], e["data"].get("cycle_id")) for e in placed]
          == [("ORDER_PLACED", cid[0]), ("ORDER_REPLACED", cid[1]), ("ORDER_PLACED", cid[2]), ("ORDER_PLACED", cid[3])],
          f"{[(e['type'], e['data'].get('cycle_id')) for e in placed]}")
    fills = [e for e in ev if e.get("type") == "FILL"]
    check("FILL 이벤트 data.cycle_id = 거래 레코드 cycle_id (BUY=주문 주기, STOP_LOSS=결정 주기)",
          [(e["data"].get("side"), e["data"].get("cycle_id")) for e in fills] == [("BUY", cid[1]), ("STOP_LOSS", cid[3])],
          f"{[(e['data'].get('side'), e['data'].get('cycle_id')) for e in fills]}")

    # 가상
    fresh_dir()
    fake = StatefulFake()
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True
        fake.candles = candles([100.0] * 30 + [98.0])
        with FixedClock(now_for(fake.candles)):
            bot._loop_step()
        bp = bot.virtual_broker.open_orders[0]["price"]
        fake.candles = candles([100.0] * 30 + [98.0, 98.0], last_low=bp - 0.5)
        with FixedClock(now_for(fake.candles)):
            bot._loop_step()
    vt = VwapConfigManager.load_trades("VIRTUAL_1")
    vev = read_events("VIRTUAL_1")
    check("가상 거래 레코드 cycle_id = 주문 주기", vt and vt[0].get("cycle_id") == cid[0], f"{vt}")
    check("가상 ORDER_PLACED·FILL 이벤트 cycle_id",
          [e["data"].get("cycle_id") for e in vev if e.get("type") in ("ORDER_PLACED", "FILL")] == [cid[0], cid[0]],
          f"{[(e['type'], e['data'].get('cycle_id')) for e in vev if e.get('type') in ('ORDER_PLACED', 'FILL')]}")


# ---------------------------------------------------------------------------
# T-H3 봉 적재 연결
# ---------------------------------------------------------------------------
def _one_cycle(mode, cfg, df, nowt, source="toss", bot=None, fake=None):
    fake = fake or StatefulFake(source)
    fake.candles = df
    with Patched(cfg, fake):
        bot = bot or VWAPBot(mode)
        if mode == "REAL":
            bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
        bot.running = True
        with FixedClock(nowt):
            bot._loop_step()
    return bot, fake


def test_h3_bars():
    print("\nT-H3. 봉 적재 연결 (REAL/가상 공통)")
    fresh_dir()
    df = candles([100.0] * 31)  # 10:00 ~ 10:30
    bot, fake = _one_cycle("REAL", make_config("real"), df, now_for(df, 5))
    files = bar_files()
    rows = bar_rows(files[0]) if files else []
    check("REAL 주기 → 마감 봉 30개 적재(진행 중 10:30 봉 제외), source=toss",
          files == ["TEST_1m_2026-10-04.csv"] and len(rows) == 30 and rows[-1][0] == "2026-10-05 10:29:00"
          and {r[-1] for r in rows} == {"toss"}, f"files={files} rows={len(rows)}")

    # 세션 마지막 봉: 다음 봉이 오지 않은 채 시간이 흐름 → asof 가 마지막 봉 + 1분 + 60초를 넘으면 저장
    _one_cycle("REAL", make_config("real"), df, df["time"].iloc[-1].to_pydatetime() + timedelta(seconds=60 + 59),
               bot=bot, fake=fake)
    check("경계 직후(+1분59초 < +2분)에는 마지막 봉 미저장", len(bar_rows(files[0])) == 30)
    _one_cycle("REAL", make_config("real"), df, df["time"].iloc[-1].to_pydatetime() + timedelta(seconds=120),
               bot=bot, fake=fake)
    rows = bar_rows(files[0])
    check("asof ≥ 봉시각+1분+60초 → 세션 마지막 봉(10:30)도 저장, 중복 0",
          len(rows) == 31 and rows[-1][0] == "2026-10-05 10:30:00" and len({r[0] for r in rows}) == 31)

    # 가상 봇이 같은 종목·간격을 이어서 받아도 중복 없음, 출처는 시세 원천 브로커의 값
    df2 = candles([100.0] * 36)
    _one_cycle("VIRTUAL_1", make_config("virtual_1"), df2, now_for(df2, 5), source="yahoo")
    rows = bar_rows(files[0])
    check("가상(VIRTUAL_1)도 적재(공통 경로) + REAL 과 같은 종목이면 이어 쓰기만(중복 0), 출처=yahoo",
          len(rows) == 35 and len({r[0] for r in rows}) == 35 and rows[-1][-1] == "yahoo" and rows[0][-1] == "toss",
          f"rows={len(rows)}")

    # ctx market 사용: 티커 'ABC'(형식 추론=US) + 설정 market=KR + 리셋 22:30, 표준시(11/10) 22:40~23:10 봉
    fresh_dir()
    dfk = candles([50.0] * 31, start=datetime(2026, 11, 10, 22, 40))
    _one_cycle("REAL", make_config("real", ticker="ABC", market="KR"), dfk, now_for(dfk, 5))
    check("ctx market=KR 사용 → 리셋 22:30 고정 세션(2026-11-10). (티커 추론 US 자동이면 11-09 로 갈렸을 것)",
          bar_files() == ["ABC_1m_2026-11-10.csv"], f"{bar_files()}")
    fresh_dir()
    _one_cycle("REAL", make_config("real", ticker="ABC", market="US"), dfk, now_for(dfk, 5))
    check("대조: market=US 면 서머타임 종료 후 23:30 개장 → 같은 봉(22:40~23:09)이 11-09 세션으로 감",
          bar_files() == ["ABC_1m_2026-11-09.csv"], f"{bar_files()}")

    for src, label in (("mock", "mock(난수 봉)"), ("", "출처 불명(빈 값)")):
        fresh_dir()
        _one_cycle("REAL", make_config("real"), df, now_for(df, 5), source=src)
        check(f"출처 {label} → 적재 안 함", bar_files() == [], f"{bar_files()}")
    fresh_dir()
    cfg_off = make_config("real")
    cfg_off["bars_store_enabled"] = False
    _one_cycle("REAL", cfg_off, df, now_for(df, 5))
    check("bars_store_enabled=false(평탄 키) → 적재 안 함", bar_files() == [])

    # 쓰기 실패(폴더 자리에 파일)여도 매매 주기는 정상
    fresh_dir()
    with open(os.path.join(h.TMP_DIR, "bars"), "w") as f:
        f.write("not a dir")
    b, fk = _one_cycle("REAL", make_config("real"), candles([100.0] * 30 + [98.0]), now_for(candles([100.0] * 31), 5))
    check("봉 적재 실패(쓰기 불가)에도 주기 정상 — BUY 주문 제출, 훅 예외 이벤트 없음(내부에서 삼킴)",
          [p["side"] for p in fk.placed] == ["BUY"]
          and not [e for e in read_events("REAL") if e.get("reason_code") == "HOOK_ERROR"])
    os.remove(os.path.join(h.TMP_DIR, "bars"))

    # 직접 호출 단위: asof 규칙
    fresh_dir()
    d = candles([1.0] * 5)
    last = d["time"].iloc[-1].to_pydatetime()
    n1 = bars_store.append_closed_bars("UNIT", "1m", d, "22:30", "toss", market="US", asof=last + timedelta(seconds=119))
    n2 = bars_store.append_closed_bars("UNIT", "1m", d, "22:30", "toss", market="US", asof=last + timedelta(seconds=120))
    fresh_dir()
    n3 = bars_store.append_closed_bars("UNIT", "5m", candles([1.0]), "22:30", "toss", asof=T0 + timedelta(minutes=6))
    n4 = bars_store.append_closed_bars("UNIT2", "1m", d, "22:30", "toss")
    check("append_closed_bars asof: 경계 전 4봉, 경계 후 마지막 1봉 추가, 5m 1행도 asof 로 저장, asof 없으면 기존(마지막 제외)",
          (n1, n2, n3, n4) == (4, 1, 1, 4), f"{(n1, n2, n3, n4)}")
    tz = d.copy()
    tz["time"] = tz["time"].dt.tz_localize("Asia/Seoul").dt.tz_convert("UTC")
    fresh_dir()
    n5 = bars_store.append_closed_bars("UNIT", "1m", tz, "22:30", "toss",
                                       asof=pd.Timestamp(last + timedelta(seconds=120)).tz_localize("Asia/Seoul"))
    check("tz-aware 봉·asof 도 KST 로 비교", n5 == 5, f"{n5}")


# ---------------------------------------------------------------------------
# T-H4 last_candles_source
# ---------------------------------------------------------------------------
def test_h4_candles_source():
    print("\nT-H4. TossBroker.last_candles_source (네트워크 mock)")
    b = broker_module.TossBroker("", "", "")  # 키 없음 → mock_mode
    orig = broker_module.TossBroker._fetch_yahoo_candles
    try:
        broker_module.TossBroker._fetch_yahoo_candles = lambda self, t, i, l=100: candles([1.0] * 3)
        df = b.get_candles("AAPL", "1m", 3)
        s1 = b.last_candles_source
        broker_module.TossBroker._fetch_yahoo_candles = lambda self, t, i, l=100: pd.DataFrame()
        df2 = b.get_candles("AAPL", "1m", 5)
        s2 = b.last_candles_source
    finally:
        broker_module.TossBroker._fetch_yahoo_candles = orig
    check("mock_mode + Yahoo 성공 → 'yahoo' (반환 df 그대로)", s1 == "yahoo" and len(df) == 3, s1)
    check("mock_mode + Yahoo 실패 → 난수 봉 'mock' (반환 동작 그대로 limit 행)", s2 == "mock" and len(df2) == 5, s2)


# ---------------------------------------------------------------------------
# T-PERF
# ---------------------------------------------------------------------------
def test_perf():
    print("\nT-PERF. 훅 소요시간 실측 (PC)")
    fresh_dir()
    big = candles([100.0 + (i % 7) * 0.1 for i in range(1600)], start=datetime(2026, 10, 5, 22, 30) - timedelta(minutes=1599 - 900))
    ctx = {"df": big, "ticker": "PERF", "market": "US", "interval": "1m", "reset_time": "22:30",
           "candles_source": "toss", "config": {"bars_store_enabled": True},
           "candles_asof": big["time"].iloc[-1].to_pydatetime() + timedelta(seconds=5)}
    t = time.perf_counter()
    n_first = bars_store.hook(dict(ctx, df=big.copy()))
    first_ms = (time.perf_counter() - t) * 1000
    nxt = pd.concat([big.iloc[1:], candles([100.0], start=big["time"].iloc[-1] + timedelta(minutes=1))], ignore_index=True)
    steady = []
    for _ in range(20):
        t = time.perf_counter()
        bars_store.hook(dict(ctx, df=nxt.copy(), candles_asof=nxt["time"].iloc[-1].to_pydatetime() + timedelta(seconds=5)))
        steady.append((time.perf_counter() - t) * 1000)
    bars_store._reset_state()
    t = time.perf_counter()
    bars_store.hook(dict(ctx, df=nxt.copy()))
    restore_ms = (time.perf_counter() - t) * 1000
    steady.sort()
    print(f"      1600봉 첫 적재 {n_first}행: {first_ms:.1f} ms | 정상 주기(새 봉 0~1개) 중앙값 {steady[10]:.1f} ms, "
          f"최대 {steady[-1]:.1f} ms | 재시작 직후(파일 복원) {restore_ms:.1f} ms")
    check("PERF: 1600봉 첫 적재 < 2초(훅 경고 기준), 정상 주기 < 200ms",
          first_ms < 2000 and steady[-1] < 200, f"first={first_ms:.1f}ms steady_max={steady[-1]:.1f}ms")

    # 봇 주기 전체: 훅 있음/없음 (1600봉, REAL mock)
    def loop_ms(with_bars):
        fresh_dir()
        fake = StatefulFake()
        fake.candles = big
        out = []
        with Patched(make_config("real"), fake):
            bot = VWAPBot("REAL")
            bot.tracked_open_orders, bot.last_holdings_qty, bot._holdings_snapshot_ready = {}, {}, True
            if not with_bars:
                bot.remove_post_cycle_hook("bars_store")
            bot.running = True
            for _ in range(5):
                with FixedClock(now_for(big)):
                    t = time.perf_counter()
                    bot._loop_step()
                    out.append(((time.perf_counter() - t) * 1000, dict(bot.last_hook_durations_ms)))
        return out

    on, off = loop_ms(True), loop_ms(False)
    on_ms = sorted(x[0] for x in on[1:])
    off_ms = sorted(x[0] for x in off[1:])
    print(f"      _loop_step(1600봉) 훅 없음 중앙값 {off_ms[len(off_ms)//2]:.1f} ms / bars_store 훅 포함 중앙값 "
          f"{on_ms[len(on_ms)//2]:.1f} ms | 훅 측정값(첫 주기·이후) {on[0][1]} / {on[-1][1]}")
    check("PERF: 봇 주기에서 bars_store 훅 측정값이 last_hook_durations_ms 에 기록됨",
          "bars_store" in on[0][1] and on[0][1]["bars_store"] < 2000)


def main():
    print("=" * 70)
    print(" VWAP 3단계 SR-2 — post-cycle 훅 / cycle_id / 봉 적재 연결 (임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    for fn in (test_h1_isolation, test_h2_cycle_id, test_h3_bars, test_h4_candles_source, test_perf):
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
