"""
VWAP REAL 봇 '신뢰 불가 캔들' 보호(ADR-0010) 검증 스크립트 — 네트워크/실제 Discord 전송/실주문 없이 실행됩니다.

검증 항목
  U1. REAL + 난수(mock) 봉 / 출처 불명("") / 키 미설정 Mock 브로커 → 주문·취소 0건, 사유 DATA_UNTRUSTED,
      warn 이벤트 1건(같은 키 억제), Discord 1회, 상태의 현재가를 난수 가격으로 덮지 않음, 기존 미체결 유지
  U2. REAL + 보유 + 난수 봉이 손절가 이탈 → 시장가 청산·취소·거래기록 없음(판단 보류), 손실한도(패닉)도 미발동.
      대조: 같은 봉을 출처 "toss" 로 주면 손절/패닉이 그대로 실행됨 (보호가 이 검사 때문에만 멈췄다는 증거)
  U3. REAL + toss / yahoo 봉 → 보호 검사를 끈 '기존 동작'과 주문 결과가 같음 (매수 진입, 보유 손절)
  U4. VIRTUAL + 난수 봉 → 보호 검사를 끈 '기존 동작'과 같음 (가상 매수 주문, 가상 손절 기록), DATA_UNTRUSTED 없음
  U5. 연속 N(=UNTRUSTED_STREAK_CRITICAL) 주기 → CRITICAL 1회(+Discord 1회), 이후 반복 안 함,
      신뢰 주기에서 0 으로 리셋, DATA_UNAVAILABLE 주기는 연속을 끊지 않음
  U6. TossBroker: 키가 있으면 mock_mode 플래그가 켜져 있어도(다른 스레드의 토큰 실패) 모의 분기로 가지 않음
      (가짜 주문ID·가짜 미체결 0건·난수 봉 없음), 토큰 발급 실패는 예외로 드러남, 키 미설정 동작은 그대로
  U7. 운영 data/ 해시 전후 동일 (하위 폴더 포함)
  U8. (M1, 2026-10-07) 신뢰/불가가 10분 안에 반복돼도 새 구간마다 첫 경고 이벤트가 기록됨. Discord 는 최소 간격
      (UNTRUSTED_EPISODE_ALERT_MIN_SEC) 안이면 미뤘다가 요약/CRITICAL/다음 구간 시작 알림에 횟수로 묶여 전달 (고정 시각)
  U9. (M2, 2026-10-07) CRITICAL 3주기째 1회 + 지속 시 UNTRUSTED_CRITICAL_REPEAT_SEC 마다 재알림
      (지속 시간·마지막 신뢰 보유 수량·원인), 새 구간은 다시 첫 CRITICAL (고정 시각)
  U10. (2026-10-08) 신뢰 불가 3주기 ↔ 신뢰 1주기 깜빡임: CRITICAL 이벤트는 구간마다 기록되지만 Discord CRITICAL 은
      마지막 발송 후 UNTRUSTED_CRITICAL_REPEAT_SEC 간격을 지킴(생략 횟수 묶음), start() 하면 간격 초기화 (고정 시각)

실행:  PYTHONUTF8=1 python scripts/test_vwap_untrusted_candles.py
"""
import os
import sys
import json
import shutil
import hashlib
import logging
import traceback
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

REAL_DATA_DIR = os.path.join(PROJECT_ROOT, "data")


def _hash_tree(path):
    """data/ 아래 모든 파일(하위 폴더 포함)의 sha256."""
    out = {}
    for root, _dirs, files in os.walk(path):
        for name in sorted(files):
            fp = os.path.join(root, name)
            with open(fp, "rb") as f:
                out[os.path.relpath(fp, path)] = hashlib.sha256(f.read()).hexdigest()
    return out


DATA_TREE_HASH_BEFORE = _hash_tree(REAL_DATA_DIR)

# 운영 로그 파일(trading_bot_*.log)에 쓰지 않도록 봇 로거를 먼저 등록
for _name in ["vwap_bot_virtual_1", "vwap_bot_real", "vwap_bot_virtual"]:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("      [bot-log] %(levelname)s %(message)s"))
    _lg.addHandler(_h)
    _lg.propagate = False
logging.getLogger("vwap_bot").setLevel(logging.CRITICAL)

# 1단계 하네스 재사용 (임시 DATA_DIR 패치, FakeTossBroker, make_candles, Patched, Discord no-op)
import test_vwap_reliability as h  # noqa: E402

import pandas as pd  # noqa: E402
import core.vwap.bot as bot_module  # noqa: E402
import core.vwap.broker as broker_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402
from core.vwap.session import SessionSpec  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap import events as ev  # noqa: E402

check = h.check
make_candles = h.make_candles
FakeTossBroker = h.FakeTossBroker
make_config = h.make_config
Patched = h.Patched

BASE = [100.0] * 30
BUY_CANDLES = BASE + [98.0]        # VWAP 아래 → BUY_ARMED
STOP_CANDLES = BASE + [90.0]       # 보유 10주 @100, x=2% → 손절가 98 이탈
HOLDING = {"cash": 100000.0, "holdings": {"TEST": {"qty": 10.0, "entry_price": 100.0}}}
FLAT = {"cash": 100000.0, "holdings": {}}


class SenderMock:
    def __init__(self):
        self.calls = []

    def __call__(self, message):
        self.calls.append(message)
        return True


def use_sender():
    m = SenderMock()
    ev.set_sender(m)
    ev.notifier.synchronous = True
    ev.notifier._last_sent.clear()
    return m


def read_events(mode="REAL"):
    path = ev.events_path(mode)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def fake_broker(source="toss", balance=None, open_orders=None, is_mock_only=False):
    f = FakeTossBroker("test_id", "test_secret", "1")
    f.last_candles_source = source
    f.is_mock_only = is_mock_only
    f.balance = json.loads(json.dumps(balance if balance is not None else FLAT))
    f.open_orders = [dict(o) for o in (open_orders or [])]
    return f


class LegacyGuard:
    """보호 검사를 끈 '기존 동작' 재현용: _untrusted_candles_cause 가 항상 None."""

    def __enter__(self):
        self._orig = VWAPBot._untrusted_candles_cause
        VWAPBot._untrusted_candles_cause = lambda self, broker: None
        return self

    def __exit__(self, *a):
        VWAPBot._untrusted_candles_cause = self._orig


def run_real(fake, candles_list, cfg=None, bot=None, baseline=None):
    cfg = cfg or make_config("real")
    with Patched(cfg, fake):
        bot = bot or h.new_real_bot()
        bot.real_broker = fake  # 같은 봇에 다른 가짜 브로커를 이어 붙일 때(봇은 키가 같으면 브로커를 재사용하므로)
        bot.running = True
        if baseline is not None:
            bot.daily_baseline_asset = baseline
            bot.last_baseline_date = SessionSpec.for_market("US", "22:30", "TEST").session_key(datetime.now())
        for c in candles_list:
            fake.candles = c if isinstance(c, pd.DataFrame) else make_candles(c)
            bot._loop_step()
    return bot


def strip_ids(placed):
    return [{k: v for k, v in p.items() if k != "order_id"} for p in placed]


# ---------------------------------------------------------------------------
def test_u1_block():
    print("\nU1. REAL + 신뢰 불가 캔들 → 매매 없음 + 이벤트 + Discord 1회")
    for src, cause in (("mock", "candles_mock"), ("", "candles_unknown"), ("weird", "candles_unknown")):
        h.reset_data_dir()
        sender = use_sender()
        old_buy = {"order_id": "B_OLD", "ticker": "TEST", "side": "BUY", "price": 97.0, "qty": 5.0}
        fake = fake_broker(src, FLAT, [old_buy])
        bot = run_real(fake, [BUY_CANDLES, BUY_CANDLES])
        st = bot.get_status()
        evs = read_events()
        unt = [e for e in evs if e["type"] == "ERROR" and e["reason_code"] == "DATA_UNTRUSTED"]
        check(f"[{src or '빈값'}] 주문 0건 + 취소 0건(기존 매수 미체결 B_OLD 유지)",
              fake.placed == [] and fake.canceled == [], f"placed={fake.placed} canceled={fake.canceled}")
        check(f"[{src or '빈값'}] 사유 DATA_UNTRUSTED, signal WAIT, 현재가를 난수 가격으로 덮지 않음",
              st["reason_code"] == "DATA_UNTRUSTED" and st["signal"] == "WAIT" and st["current_price"] == 0.0,
              f"{st['reason_code']} / {st['reason_text'][:60]} / cp={st['current_price']}")
        check(f"[{src or '빈값'}] warn 이벤트 2주기에 1건(같은 키 억제) + cause={cause}",
              len(unt) == 1 and unt[0]["level"] == "warn" and unt[0]["data"].get("cause") == cause
              and unt[0]["data"].get("candles_source") == src, str([(e['level'], e['data'].get('cause')) for e in unt]))
        dmsg = [m for m in sender.calls if "DATA_UNTRUSTED" in m or "신뢰 불가" in m]
        check(f"[{src or '빈값'}] Discord 1회", len(dmsg) == 1, f"{len(dmsg)}건")
        check(f"[{src or '빈값'}] 주문/체결 이벤트 없음",
              not any(e["type"] in ("ORDER_PLACED", "ORDER_REPLACED", "ORDER_CANCELED", "FILL", "STOP_LOSS", "PANIC")
                      for e in evs))

    # 키 미설정(시세 조회용 Mock) 브로커 — 시세는 실제 Yahoo 여도 잔고·주문이 가짜이므로 보류
    h.reset_data_dir()
    use_sender()
    fake = fake_broker("yahoo", FLAT, is_mock_only=True)
    bot = run_real(fake, [BUY_CANDLES])
    unt = [e for e in read_events() if e["reason_code"] == "DATA_UNTRUSTED" and e["type"] == "ERROR"]
    check("[키 미설정 Mock 브로커 + yahoo 봉] 주문 0건 + DATA_UNTRUSTED(cause=broker_mock_only)",
          fake.placed == [] and bot.get_status()["reason_code"] == "DATA_UNTRUSTED"
          and unt and unt[0]["data"].get("cause") == "broker_mock_only", str(fake.placed))
    check("[키 미설정] 실거래 추적 파일/거래 파일 미생성",
          not os.path.exists(os.path.join(h.TMP_DIR, "vwap_trades_real.json"))
          and not bot.tracked_open_orders)


# ---------------------------------------------------------------------------
def test_u2_stop_loss_held():
    print("\nU2. REAL + 보유 + 난수 봉 손절가 이탈 → 판단 보류(시장가 청산 안 함)")
    old_sell = {"order_id": "S_OLD", "ticker": "TEST", "side": "SELL", "price": 101.0, "qty": 10.0}

    h.reset_data_dir()
    use_sender()
    fake = fake_broker("mock", HOLDING, [old_sell])
    bot = run_real(fake, [STOP_CANDLES])
    evs = read_events()
    check("난수 봉 손절 이탈: 시장가 주문 0건, 기존 매도 S_OLD 취소 안 함",
          fake.placed == [] and fake.canceled == [], f"placed={fake.placed} canceled={fake.canceled}")
    check("난수 봉 손절 이탈: STOP_LOSS 이벤트/거래기록 없음, 사유 DATA_UNTRUSTED",
          not any(e["type"] == "STOP_LOSS" for e in evs) and VwapConfigManager.load_trades("REAL") == []
          and bot.get_status()["reason_code"] == "DATA_UNTRUSTED")

    # 대조: 같은 봉·같은 보유를 출처 toss 로 주면 기존대로 손절 실행
    h.reset_data_dir()
    use_sender()
    fake_t = fake_broker("toss", HOLDING, [old_sell])
    run_real(fake_t, [STOP_CANDLES])
    mk = [p for p in fake_t.placed if p["order_type"] == "MARKET"]
    check("대조(toss): 기존 매도 취소 → 시장가 10주 매도 (손절 경로 정상)",
          "S_OLD" in fake_t.canceled and len(mk) == 1 and mk[0]["qty"] == 10.0, f"placed={fake_t.placed}")

    # 손실한도(패닉): 기준자산 10000, 평가손실 (90-100)*10=-100 → 1% ≥ 한도 0.5%
    cfg = make_config("real", max_daily_loss_limit=0.5)
    h.reset_data_dir()
    use_sender()
    fake = fake_broker("mock", HOLDING)
    bot = run_real(fake, [STOP_CANDLES], cfg=cfg, baseline=10000.0)
    check("난수 봉: 손실한도(패닉) 판정도 보류 → 봇 계속 가동, 주문 0건, PANIC 이벤트 없음",
          bot.running is True and fake.placed == [] and not any(e["type"] == "PANIC" for e in read_events()))
    h.reset_data_dir()
    use_sender()
    fake_t = fake_broker("toss", HOLDING)
    bot_t = run_real(fake_t, [STOP_CANDLES], cfg=cfg, baseline=10000.0)
    check("대조(toss): 같은 조건이면 패닉 청산 + 봇 정지",
          bot_t.running is False and any(p["order_type"] == "MARKET" for p in fake_t.placed))


# ---------------------------------------------------------------------------
def test_u3_trusted_unchanged():
    print("\nU3. REAL + toss/yahoo 봉 → 기존 동작과 동일")
    results = {}
    for label, src, legacy in (("legacy", "toss", True), ("toss", "toss", False), ("yahoo", "yahoo", False)):
        h.reset_data_dir()
        use_sender()
        fake = fake_broker(src, FLAT)
        if legacy:
            with LegacyGuard():
                bot = run_real(fake, [BUY_CANDLES])
        else:
            bot = run_real(fake, [BUY_CANDLES])
        results[label] = (strip_ids(fake.placed), bot.get_status()["reason_code"])
    check("매수 진입: toss == yahoo == 기존(보호 검사 없음), BUY 지정가 1건, BUY_ARMED",
          results["toss"] == results["yahoo"] == results["legacy"] and len(results["toss"][0]) == 1
          and results["toss"][0][0]["side"] == "BUY" and results["toss"][1] == "BUY_ARMED", str(results))

    res2 = {}
    for label, src, legacy in (("legacy", "toss", True), ("toss", "toss", False), ("yahoo", "yahoo", False)):
        h.reset_data_dir()
        use_sender()
        fake = fake_broker(src, HOLDING)
        if legacy:
            with LegacyGuard():
                run_real(fake, [STOP_CANDLES])
        else:
            run_real(fake, [STOP_CANDLES])
        res2[label] = (strip_ids(fake.placed), [t["side"] for t in VwapConfigManager.load_trades("REAL")])
    check("보유 손절: toss == yahoo == 기존, 시장가 매도 1건 + STOP_LOSS 기록",
          res2["toss"] == res2["yahoo"] == res2["legacy"] and res2["toss"][1] == ["STOP_LOSS"], str(res2))


# ---------------------------------------------------------------------------
def run_virtual(source, candles, seed_trades=None, legacy=False):
    h.reset_data_dir()
    use_sender()
    if seed_trades:
        VwapConfigManager.save_trades(seed_trades, "VIRTUAL_1")
    fake = fake_broker(source)
    ctx = LegacyGuard() if legacy else None
    if ctx:
        ctx.__enter__()
    try:
        with Patched(make_config("virtual_1"), fake):
            bot = VWAPBot("VIRTUAL_1")
            bot.running = True
            fake.candles = make_candles(candles)
            bot._loop_step()
    finally:
        if ctx:
            ctx.__exit__()
    orders = [{k: o.get(k) for k in ("side", "price", "qty")} for o in bot.virtual_broker.open_orders]
    trades = [(t["side"], t["price"], t["qty"]) for t in VwapConfigManager.load_trades("VIRTUAL_1")]
    unt = [e for e in read_events("VIRTUAL_1") if e.get("reason_code") == "DATA_UNTRUSTED"]
    return orders, trades, bot.get_status()["reason_code"], unt


def test_u4_virtual_unchanged():
    print("\nU4. VIRTUAL + 난수(mock) 봉 → 기존과 동일 (가상 봇은 보호 대상 아님)")
    new = run_virtual("mock", BUY_CANDLES)
    old = run_virtual("mock", BUY_CANDLES, legacy=True)
    check("가상 매수: 결과 == 기존, BUY 가상 주문 1건, BUY_ARMED, DATA_UNTRUSTED 없음",
          new[:3] == old[:3] and len(new[0]) == 1 and new[2] == "BUY_ARMED" and new[3] == [], f"new={new[:3]} old={old[:3]}")
    seed = [{"trade_id": "seed", "timestamp": "2026-10-05 09:00:00", "ticker": "TEST",
             "side": "BUY", "price": 100.0, "qty": 10, "pnl": 0.0, "roi": 0.0}]
    new = run_virtual("mock", STOP_CANDLES, seed)
    old = run_virtual("mock", STOP_CANDLES, seed, legacy=True)
    check("가상 손절: 결과 == 기존, STOP_LOSS 가상 기록 @90",
          new[:3] == old[:3] and ("STOP_LOSS", 90.0, 10.0) in new[1] and new[3] == [], f"new={new[:3]}")


# ---------------------------------------------------------------------------
def test_u5_streak_critical():
    n = bot_module.UNTRUSTED_STREAK_CRITICAL
    print(f"\nU5. 연속 {n}주기 신뢰 불가 → CRITICAL 1회")
    h.reset_data_dir()
    sender = use_sender()
    fake = fake_broker("mock", HOLDING)
    bot = run_real(fake, [STOP_CANDLES] * (n - 1))
    crit = lambda: [e for e in read_events() if e["type"] == "CRITICAL" and e["reason_code"] == "DATA_UNTRUSTED"]  # noqa: E731
    check(f"{n - 1}주기까지는 CRITICAL 없음", crit() == [] and bot._untrusted_streak == n - 1, str(bot._untrusted_streak))
    run_real(fake, [STOP_CANDLES], bot=bot)
    c = crit()
    check(f"{n}주기째 CRITICAL 1건(level critical, streak={n}, 신뢰 주기 이력 없으면 보유 '확인 이력 없음')",
          len(c) == 1 and c[0]["level"] == "critical" and c[0]["data"].get("streak") == n
          and c[0]["data"].get("last_known_position_qty", "x") is None and "확인 이력 없음" in c[0]["message"],
          str(c[:1]))
    dcrit = [m for m in sender.calls if "CRITICAL" in m]
    check("CRITICAL Discord 1회", len(dcrit) == 1, f"{len(dcrit)}건")
    run_real(fake, [STOP_CANDLES, STOP_CANDLES], bot=bot)
    check("이후 계속돼도 CRITICAL 재발송 없음, 주문 0건", len(crit()) == 1 and fake.placed == [],
          f"crit={len(crit())} placed={fake.placed}")

    # 신뢰 주기(toss, 무보유 평탄) → 연속 0 으로 리셋
    fake2 = fake_broker("toss", FLAT)
    run_real(fake2, [BASE + [100.0]], bot=bot)
    check("신뢰 주기 후 연속 카운터 0", bot._untrusted_streak == 0, str(bot._untrusted_streak))

    # 신뢰 주기에서 보유 10주 확인 → 이후 CRITICAL 문구에 '마지막으로 확인된 보유 10주'
    fake_hold = fake_broker("toss", HOLDING)
    run_real(fake_hold, [BASE + [99.5]], bot=bot)  # 보유 중 VWAP 아래·손절가 위 → HOLD
    check("신뢰 주기(보유 10주 HOLD) 후 연속 0, 확인된 보유 10주 기억",
          bot._untrusted_streak == 0 and bot._last_trusted_position_qty == 10.0, bot.get_status()["reason_code"])

    # DATA_UNAVAILABLE(빈 캔들) 은 연속을 끊지 않음: 신뢰불가 (n-1) + 빈캔들 1 + 신뢰불가 1 → CRITICAL
    fake3 = fake_broker("mock", HOLDING)
    run_real(fake3, [STOP_CANDLES] * (n - 1), bot=bot)
    run_real(fake3, [make_candles(BASE).iloc[0:0]], bot=bot)
    check("빈 캔들 주기는 DATA_UNAVAILABLE, 연속 유지", bot.get_status()["reason_code"] == "DATA_UNAVAILABLE"
          and bot._untrusted_streak == n - 1, str(bot._untrusted_streak))
    run_real(fake3, [STOP_CANDLES], bot=bot)
    c = crit()
    check(f"다음 신뢰 불가 주기에서 다시 {n}에 도달 → CRITICAL 이벤트 2번째, 문구에 '확인된 보유 10주'",
          len(c) == 2 and "확인된 보유 10주" in c[-1]["message"] and c[-1]["data"].get("last_known_position_qty") == 10.0
          and fake3.placed == [], c[-1]["message"] if c else "")

    # start() 시 리셋
    bot.running = False
    bot._untrusted_streak = 2
    with Patched(make_config("real"), fake3):
        orig_run = bot._run_loop
        bot._run_loop = lambda gen=None: None
        try:
            bot.start()
        finally:
            bot._run_loop = orig_run
            bot.running = False
    check("start() 시 연속 카운터 0", bot._untrusted_streak == 0)


# ---------------------------------------------------------------------------
class FixedClock:
    """bot_module._monotonic 대체 — 테스트가 시각을 직접 정함 (실제 대기 없음)."""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class use_clock:
    def __init__(self, t=0.0):
        self.clock = FixedClock(t)

    def __enter__(self):
        self._orig = bot_module._monotonic
        bot_module._monotonic = self.clock
        return self.clock

    def __exit__(self, *a):
        bot_module._monotonic = self._orig


FLAT_CANDLES = BASE + [100.0]


def _untrusted_events():
    return [e for e in read_events() if e["type"] == "ERROR" and e["reason_code"] == "DATA_UNTRUSTED"]


def _start_events():
    return [e for e in _untrusted_events() if "새 신뢰 불가 구간 시작" in e["message"]]


def test_u8_episode_alerts():
    print("\nU8. (M1) 새 신뢰 불가 구간마다 첫 경고 이벤트 + Discord(최소 간격·요약) — 고정 시각")
    gap = bot_module.UNTRUSTED_EPISODE_ALERT_MIN_SEC
    h.reset_data_dir()
    sender = use_sender()
    d_start = lambda: [m for m in sender.calls if "새 신뢰 불가 구간 시작" in m]  # noqa: E731
    d_sum = lambda: [m for m in sender.calls if "구간 요약" in m]  # noqa: E731
    d_crit = lambda: [m for m in sender.calls if "CRITICAL" in m]  # noqa: E731
    bad = fake_broker("mock", FLAT)
    good = fake_broker("toss", FLAT)
    with use_clock(0.0) as clk:
        bot = run_real(bad, [FLAT_CANDLES])                      # t=0    구간1 시작
        check("t=0 구간1 시작: 이벤트 1 + Discord 1", len(_start_events()) == 1 and len(d_start()) == 1,
              f"ev={len(_start_events())} d={len(d_start())}")
        clk.t = 60; run_real(good, [FLAT_CANDLES], bot=bot)      # 신뢰 1주기
        clk.t = 120; run_real(bad, [FLAT_CANDLES], bot=bot)      # 10분 안 구간2 시작 (QA M1 재현 시나리오)
        s = _start_events()
        check("[M1 재현] 10분 안 구간2 시작: 첫 경고 이벤트가 누락되지 않음(이벤트 2건, episode=2)",
              len(s) == 2 and s[-1]["data"].get("episode") == 2 and s[-1]["data"].get("streak") == 1,
              str([(e['data'].get('episode'), e['data'].get('streak')) for e in s]))
        check(f"[M1 재현] 구간2 Discord 는 최소 간격({gap}s) 안이라 미룸(discord_deferred) — 폭주 방지",
              len(d_start()) == 1 and s[-1]["data"].get("discord_deferred") is True and bot._untrusted_alert_pending == 1)
        clk.t = 180; run_real(good, [FLAT_CANDLES], bot=bot)
        clk.t = 240; run_real(bad, [FLAT_CANDLES], bot=bot)      # 구간3 (깜빡임)
        clk.t = 300; run_real(good, [FLAT_CANDLES], bot=bot)     # 신뢰 — 아직 최소 간격 안 → 요약 보류
        check("깜빡임 3구간: 이벤트 3건, Discord 시작 1건, 요약 아직 없음",
              len(_start_events()) == 3 and len(d_start()) == 1 and d_sum() == [] and bot._untrusted_alert_pending == 2)
        clk.t = gap; run_real(good, [FLAT_CANDLES], bot=bot)     # 신뢰 + 최소 간격 경과 → 요약 1건
        check("최소 간격 경과 후 신뢰 주기: 요약 Discord 1건(생략 2회 포함), 미룬 횟수 0",
              len(d_sum()) == 1 and "2회" in d_sum()[0] and bot._untrusted_alert_pending == 0, str(d_sum()))
        clk.t = gap + 10; run_real(good, [FLAT_CANDLES], bot=bot)
        check("요약은 1회만 (다음 신뢰 주기에 재발송 없음)", len(d_sum()) == 1)

        # 요약 직후 구간4 시작(미룸) → 3주기 지속 → CRITICAL 에 미룬 1회가 묶여 전달
        n = bot_module.UNTRUSTED_STREAK_CRITICAL
        n_ev_before = len(_untrusted_events())
        for i in range(n):
            clk.t = gap + 60 + 60 * i
            run_real(bad, [FLAT_CANDLES], bot=bot)
        check("구간4 시작은 미룸, 같은 구간 2·3주기째 warn 은 키 억제(이벤트 +1만), Discord 시작 추가 없음",
              len(_untrusted_events()) == n_ev_before + 1 and len(d_start()) == 1,
              f"+{len(_untrusted_events()) - n_ev_before}")
        check("구간4 CRITICAL Discord 1건에 '생략된 … 구간 시작 1회' 포함 (미룬 알림이 사라지지 않음)",
              len(d_crit()) == 1 and "구간 시작 1회" in d_crit()[0] and bot._untrusted_alert_pending == 0, str(d_crit()))

        # 최소 간격이 지난 뒤 새 구간 → 즉시 Discord
        clk.t = gap * 3; run_real(good, [FLAT_CANDLES], bot=bot)
        clk.t = gap * 3 + 60; run_real(bad, [FLAT_CANDLES], bot=bot)
        check("마지막 알림 후 최소 간격 경과한 새 구간 → 시작 Discord 즉시(누적 2건), 미룸 표시 없음",
              len(d_start()) == 2 and not _start_events()[-1]["data"].get("discord_deferred"), f"{len(d_start())}")
    check("U8 전 과정 주문/취소 0건", bad.placed == [] and bad.canceled == [])


def test_u9_critical_repeat():
    n = bot_module.UNTRUSTED_STREAK_CRITICAL
    rep = bot_module.UNTRUSTED_CRITICAL_REPEAT_SEC
    print(f"\nU9. (M2) CRITICAL {n}주기째 1회 + 지속 시 {rep // 60}분마다 재알림 — 고정 시각")
    h.reset_data_dir()
    sender = use_sender()
    crit = lambda: [e for e in read_events() if e["type"] == "CRITICAL" and e["reason_code"] == "DATA_UNTRUSTED"]  # noqa: E731
    d_crit = lambda: [m for m in sender.calls if "CRITICAL" in m]  # noqa: E731
    bad = fake_broker("mock", HOLDING)
    with use_clock(0.0) as clk:
        bot = run_real(fake_broker("toss", HOLDING), [BASE + [99.5]])   # 신뢰 주기: 보유 10주 확인(HOLD)
        for i in range(n):                                               # t=60,120,180 → 3주기째 CRITICAL
            clk.t = 60 * (i + 1)
            run_real(bad, [STOP_CANDLES], bot=bot)
        c = crit()
        check(f"{n}주기째 CRITICAL 1건(critical_seq=1, '약 2분 경과', 보유 10주, 원인)",
              len(c) == 1 and c[0]["data"].get("critical_seq") == 1 and "약 2분 경과" in c[0]["message"]
              and "확인된 보유 10주" in c[0]["message"] and "난수(mock) 봉" in c[0]["message"], c[0]["message"] if c else "")
        t_crit = clk.t
        clk.t = 240; run_real(bad, [STOP_CANDLES], bot=bot)
        clk.t = t_crit + rep - 30; run_real(bad, [make_candles(BASE).iloc[0:0]], bot=bot)  # DATA_UNAVAILABLE
        check("중간 DATA_UNAVAILABLE 주기는 구간을 끊지 않음(ADR-0010 D3 그대로)",
              bot.get_status()["reason_code"] == "DATA_UNAVAILABLE" and bot._untrusted_streak > n)
        clk.t = t_crit + rep - 1; run_real(bad, [STOP_CANDLES], bot=bot)
        check(f"첫 CRITICAL 후 {rep}s 미만: 재알림 없음", len(crit()) == 1 and len(d_crit()) == 1)
        clk.t = t_crit + rep; run_real(bad, [STOP_CANDLES], bot=bot)
        c = crit()
        dur = rep + t_crit - 60  # 구간 시작 t=60 기준
        exp = f"{dur // 3600}시간 {(dur % 3600) // 60}분" if dur >= 3600 else f"약 {dur // 60}분"
        check(f"{rep}s 경과: 재알림 1회째(critical_seq=2) — 지속 시간 '{exp}', 마지막 신뢰 보유 10주, 원인 포함",
              len(c) == 2 and c[-1]["data"].get("critical_seq") == 2 and "[재알림 1회째]" in c[-1]["message"]
              and f"{exp} 지속" in c[-1]["message"] and "확인된 보유 10주" in c[-1]["message"]
              and "원인: 난수(mock) 봉" in c[-1]["message"] and c[-1]["data"].get("last_known_position_qty") == 10.0,
              c[-1]["message"] if c else "")
        check("재알림 Discord 도 발송(누적 2건, 알림기 60초 중복 억제에 걸리지 않음)", len(d_crit()) == 2, f"{len(d_crit())}")
        clk.t = t_crit + rep + 60; run_real(bad, [STOP_CANDLES], bot=bot)
        clk.t = t_crit + 2 * rep; run_real(bad, [STOP_CANDLES], bot=bot)
        c = crit()
        check("다시 30분 경과: 재알림 2회째(누적 CRITICAL 3건, 시간 단위 표기)",
              len(c) == 3 and "[재알림 2회째]" in c[-1]["message"] and "시간" in c[-1]["message"]
              and len(d_crit()) == 3, c[-1]["message"] if c else "")
        # 신뢰 주기 → 새 구간은 다시 3주기째 '첫' CRITICAL (재알림 번호 이어지지 않음)
        clk.t += 60; run_real(fake_broker("toss", HOLDING), [BASE + [99.5]], bot=bot)
        for _ in range(n):
            clk.t += 60
            run_real(bad, [STOP_CANDLES], bot=bot)
        c = crit()
        check("신뢰 주기 뒤 새 구간: 3주기째 첫 CRITICAL(critical_seq=1, '재알림' 아님)",
              len(c) == 4 and c[-1]["data"].get("critical_seq") == 1 and "재알림" not in c[-1]["message"])
    check("U9 전 과정 주문/취소 0건(손절 보류 유지)", bad.placed == [] and bad.canceled == [])



def test_u10_critical_flapping():
    n = bot_module.UNTRUSTED_STREAK_CRITICAL
    rep = bot_module.UNTRUSTED_CRITICAL_REPEAT_SEC
    print(f"\nU10. 깜빡임(불가 {n}주기 ↔ 신뢰 1주기) 중 CRITICAL Discord 는 {rep}s 간격 유지 — 고정 시각")
    h.reset_data_dir()
    sender = use_sender()
    crit = lambda: [e for e in read_events() if e["type"] == "CRITICAL" and e["reason_code"] == "DATA_UNTRUSTED"]  # noqa: E731
    d_crit = lambda: [m for m in sender.calls if "CRITICAL" in m]  # noqa: E731
    bad = fake_broker("mock", FLAT)
    good = fake_broker("toss", FLAT)
    sent_at = []
    with use_clock(0.0) as clk:
        bot = run_real(good, [FLAT_CANDLES])
        t = 0
        period = 60 * (n + 1)                       # 4분 주기 깜빡임
        cycles = (rep * 2) // period + 2            # 1시간 넘게
        for _ in range(cycles):
            for _ in range(n):
                t += 60; clk.t = t
                before = len(d_crit())
                run_real(bad, [FLAT_CANDLES], bot=bot)
                if len(d_crit()) > before:
                    sent_at.append(t)
            t += 60; clk.t = t
            run_real(good, [FLAT_CANDLES], bot=bot)
        c = crit()
        check(f"구간마다 CRITICAL 이벤트 기록({cycles}건, 이벤트는 줄이지 않음)", len(c) == cycles, f"{len(c)}")
        gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
        check(f"Discord CRITICAL 은 {rep}s 이상 간격(발송 {len(sent_at)}건, 간격 {gaps})",
              len(sent_at) >= 2 and all(g >= rep for g in gaps) and len(sent_at) == len(d_crit()))
        check(f"Discord 는 약 {rep // 60}분마다 1건(이전 동작은 {period // 60}분마다 → {cycles}건)",
              len(sent_at) <= (t // rep) + 1, f"{len(sent_at)}")
        sup = [e for e in c if e["data"].get("discord_suppressed")]
        check("생략된 CRITICAL 이벤트에는 discord_suppressed=true, 첫 CRITICAL 문구(재알림 아님) 유지",
              len(sup) == cycles - len(sent_at) and all("재알림" not in e["message"] for e in sup))
        check("간격 뒤 발송된 CRITICAL Discord 에 '생략된 CRITICAL N회' 묶음 표기",
              "생략된 CRITICAL" in d_crit()[1], d_crit()[1][:200] if len(d_crit()) > 1 else "")
        # start() 는 알림 상태를 초기화 → 다음 구간 CRITICAL 즉시 발송
        bot._reset_untrusted_alert_state()
        ev.notifier._last_sent.clear()  # 구간 번호가 1부터 다시 시작하므로 알림기 60초(실시간) 중복 키 억제를 비움
        before = len(d_crit())
        for _ in range(n):
            t += 60; clk.t = t
            run_real(bad, [FLAT_CANDLES], bot=bot)
        check("알림 상태 초기화(start) 후 첫 CRITICAL 은 간격과 무관하게 발송", len(d_crit()) == before + 1)
    check("U10 전 과정 주문/취소 0건", bad.placed == [] and bad.canceled == [])

# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _FakeRequests:
    """broker_module.requests 대체. 실제 네트워크 호출 없음."""

    def __init__(self, token_status=200):
        self.calls = []
        self.token_status = token_status

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url))
        if url.endswith("/api/v1/orders"):
            return _Resp(200, {"result": {"orders": []}})
        if url.endswith("/api/v1/candles"):
            return _Resp(500, {"error": {"code": "server"}})
        return _Resp(404, {})

    def post(self, url, headers=None, data=None, json=None, timeout=None):
        self.calls.append(("POST", url))
        if url.endswith("/oauth2/token"):
            if self.token_status == 200:
                return _Resp(200, {"access_token": "new", "expires_in": 3600})
            return _Resp(self.token_status, {"error": "invalid_client"})
        if url.endswith("/api/v1/orders"):
            return _Resp(200, {"result": {"orderId": "REAL-1"}})
        return _Resp(404, {})


def test_u6_broker():
    print("\nU6. TossBroker — mock_mode 플래그로 모의 분기 금지, 토큰 실패는 예외")
    orig_req = broker_module.requests
    orig_yahoo = broker_module.TossBroker._fetch_yahoo_candles
    broker_module.TossBroker._fetch_yahoo_candles = lambda self, *a, **k: pd.DataFrame()
    try:
        # (a) 키 있음 + 유효 토큰 + mock_mode=True (다른 스레드의 토큰 실패로 플래그만 켜진 상황)
        fr = _FakeRequests()
        broker_module.requests = fr
        b = broker_module.TossBroker("id", "secret", "1")
        b.access_token, b.token_expiry, b.mock_mode = "t", 4102444800.0, True
        oid = b.place_order("TEST", "SELL", 0.0, 1, "MARKET")
        check("(a) place_order: 가짜 'mock_order_' 아님 → 실제 주문 API 경로(모의 응답 REAL-1)",
              oid == "REAL-1" and ("POST", b.base_url + "/api/v1/orders") in fr.calls, f"oid={oid}")
        oo = b.get_open_orders("TEST")
        check("(a) get_open_orders: 가짜 '0건' 즉시 반환이 아니라 API 조회", ("GET", b.base_url + "/api/v1/orders") in fr.calls
              and oo == [] and b.last_open_orders_failed is False)
        check("(a) cancel_order: API 호출(모의 404 → False, 가짜 True 아님)", b.cancel_order("X") is False)
        df = b.get_candles("TEST", "1m", 5)
        check("(a) get_candles: 토스 500 + Yahoo 실패 → 빈 결과, 출처 ''(난수 봉 생성 안 함)",
              df.empty and b.last_candles_source == "", f"len={len(df)} src={b.last_candles_source!r}")

        # (b) 키 있음 + 토큰 발급 실패 → 예외로 드러남 (조용한 모의 전환 없음)
        fr = _FakeRequests(token_status=401)
        broker_module.requests = fr
        b2 = broker_module.TossBroker("id", "secret", "1")
        raised = []
        for name, fn in (("get_candles", lambda: b2.get_candles("TEST", "1m", 5)),
                         ("place_order", lambda: b2.place_order("TEST", "BUY", 1.0, 1)),
                         ("get_open_orders", lambda: b2.get_open_orders("TEST"))):
            try:
                fn()
                raised.append((name, False))
            except Exception:
                raised.append((name, True))
        check("(b) 토큰 실패: get_candles/place_order/get_open_orders 모두 예외", all(r for _, r in raised), str(raised))
        check("(b) 토큰 실패: 주문 API 호출 0회, mock_mode 표시 플래그는 True(대시보드용)",
              not any(u.endswith("/api/v1/orders") for _, u in fr.calls) and b2.mock_mode is True)
        check("(b) get_order: 토큰 실패 시 None (기존 동작)", b2.get_order("X") is None)

        # (c) 키 미설정 → 기존 모의 동작 그대로
        fr = _FakeRequests()
        broker_module.requests = fr
        b3 = broker_module.TossBroker("", "", "")
        df3 = b3.get_candles("AAPL", "1m", 4)
        check("(c) 키 미설정: 모의 주문ID + 난수 봉(출처 mock) + 네트워크 호출 0",
              b3.place_order("AAPL", "BUY", 1.0, 1).startswith("mock_order_") and len(df3) == 4
              and b3.last_candles_source == "mock" and fr.calls == [])
    finally:
        broker_module.requests = orig_req
        broker_module.TossBroker._fetch_yahoo_candles = orig_yahoo


# ---------------------------------------------------------------------------
def main():
    print("=" * 70)
    print(" VWAP REAL 신뢰 불가 캔들 보호(ADR-0010) 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    tests = [test_u1_block, test_u2_stop_loss_held, test_u3_trusted_unchanged, test_u4_virtual_unchanged,
             test_u5_streak_critical, test_u6_broker, test_u8_episode_alerts, test_u9_critical_repeat,
             test_u10_critical_flapping]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    ev.set_sender(lambda message: True)
    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    print("\nU7. 운영 data/ 보호")
    check("운영 data/ (하위 폴더 포함) 해시 전후 동일", _hash_tree(REAL_DATA_DIR) == DATA_TREE_HASH_BEFORE)

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
