"""
VWAP 봇 관측성(2단계) 검증 스크립트 — 네트워크/실제 Discord 전송 없이 실행됩니다.

검증 항목
  1. 판단 사유(reason_code/reason_text/filters) — 전략 단계 각 분기 + signal 값 불변
  2. 봇 단계 사유 — WAIT_START_TIME / BUDGET_SHORT / DAILY_LOSS_STOP / DATA_UNAVAILABLE / BOT_STOPPED
  3. SIGNAL_CHANGE 중복 억제 (변화가 있을 때만 기록)
  4. 이벤트 JSONL — 기록/최신순 조회/type 필터/손상 줄 무시/회전(.1)/쓰기 실패 무해/동시 쓰기
  5. GET /vwap/api/events (Flask test client, 세션 검증 mock) — 인증, limit 상한, types, mode 검증
  6. GET /vwap/api/status 신규 필드 + trades 목록의 신규 필드, POST config 의 discord_notify 저장
  7. 거래 레코드 slippage 부호(불리하면 +), holding_minutes, reason_code/filters/config_snapshot
  8. Discord 알림 — mock 전송 함수 호출 횟수, 60초 중복 억제, 가상봇 기본 OFF, 설정 OFF, 연속 실패 1회, 전송 예외 무해
  9. 운영 data/ 해시 전후 동일

실행:  python scripts/test_vwap_transparency.py
"""
import os
import sys
import json
import shutil
import logging
import tempfile
import threading
import traceback
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# 운영 로그 파일(trading_bot_*.log)에 쓰지 않도록 모든 봇 로거를 먼저 등록
for _name in ["vwap_bot_virtual_1", "vwap_bot_virtual_2", "vwap_bot_virtual_3", "vwap_bot_real", "vwap_bot_virtual"]:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("      [bot-log] %(levelname)s %(message)s"))
    _lg.addHandler(_h)
    _lg.propagate = False
logging.getLogger("vwap_bot").setLevel(logging.ERROR)

# 1단계 테스트의 하네스(임시 DATA_DIR 패치, FakeTossBroker, make_candles, Patched 등)를 재사용.
# import 시점에 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + Discord 전송 no-op 이 적용됩니다.
import test_vwap_reliability as h  # noqa: E402

from core.vwap import events as ev  # noqa: E402
from core.vwap.strategy import VwapStrategy  # noqa: E402
from core.vwap.trade_metrics import compute_slippage, holding_minutes_for_exit  # noqa: E402
import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402

check = h.check
TMP_DIR = h.TMP_DIR
make_candles = h.make_candles
FakeTossBroker = h.FakeTossBroker
make_config = h.make_config
Patched = h.Patched


class SenderMock:
    def __init__(self, raise_exc=False):
        self.calls = []
        self.raise_exc = raise_exc

    def __call__(self, message):
        self.calls.append(message)
        if self.raise_exc:
            raise RuntimeError("discord down")
        return True


def use_sender(mock):
    ev.set_sender(mock)
    ev.notifier.synchronous = True
    ev.notifier._last_sent.clear()


def read_jsonl(mode):
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


def types_of(mode):
    return [e["type"] for e in read_jsonl(mode)]


def sig(df_closes, qty=0.0, entry=0.0, **kw):
    df = VwapStrategy.calculate_vwap(make_candles(df_closes), "22:30")
    return VwapStrategy.get_signals(df, 1.0, 1.0, 2.0, qty, entry, **kw)


# ---------------------------------------------------------------------------
def test_strategy_reasons():
    print("\n1. [전략] reason_code / reason_text / filters")
    base = [100.0] * 30
    s = sig(base + [101.0])
    check("ABOVE_VWAP: 현재가가 VWAP 위", s["signal"] == "WAIT" and s["reason_code"] == "ABOVE_VWAP"
          and "101.00" in s["reason_text"] and "VWAP" in s["reason_text"], s["reason_text"])
    s = sig(base + [98.0])
    check("BUY_ARMED: VWAP 아래 + 지정가 수치 포함", s["signal"] == "BUY" and s["reason_code"] == "BUY_ARMED"
          and f"{s['target_buy_price']:.2f}" in s["reason_text"], s["reason_text"])
    s = sig(base + [98.0], use_adx_filter=True, adx_threshold=0.0)
    check("FILTER_ADX: ADX 필터가 진입 보류 + filters.adx.blocking", s["signal"] == "WAIT" and s["reason_code"] == "FILTER_ADX"
          and s["filters"]["adx"]["enabled"] and s["filters"]["adx"]["blocking"] and "ADX" in s["reason_text"], s["reason_text"])
    s = sig(base + [98.0], use_rsi_filter=True, rsi_threshold=-1.0)
    check("FILTER_RSI: RSI 필터가 진입 보류", s["signal"] == "WAIT" and s["reason_code"] == "FILTER_RSI"
          and s["filters"]["rsi"]["blocking"] and "RSI" in s["reason_text"], s["reason_text"])
    check("filters 구조(enabled/value/threshold/blocking)",
          all(set(s["filters"][k].keys()) == {"enabled", "value", "threshold", "blocking"} for k in ("adx", "rsi")))
    s = sig(base + [99.0], qty=10, entry=100.0)
    check("HOLD: 보유 중 VWAP 아래 + 손절가 표시", s["signal"] == "HOLD" and s["reason_code"] == "HOLD"
          and f"{s['stop_loss_price']:.2f}" in s["reason_text"], s["reason_text"])
    s = sig(base + [101.0], qty=10, entry=100.0)
    check("SELL_ARMED: VWAP 상향 돌파 → 매도 지정가", s["signal"] == "SELL" and s["reason_code"] == "SELL_ARMED"
          and f"{s['target_sell_price']:.2f}" in s["reason_text"], s["reason_text"])
    s = sig(base + [96.9], qty=10, entry=100.0)
    check("STOP_LOSS: 현재가 ≤ 손절가", s["signal"] == "STOP_LOSS" and s["reason_code"] == "STOP_LOSS"
          and "96.90" in s["reason_text"] and "98.00" in s["reason_text"], s["reason_text"])
    s = VwapStrategy.get_signals(make_candles([]).iloc[0:0], 1, 1, 2, 0, 0)
    check("빈 데이터: DATA_UNAVAILABLE + 기존 키 유지", s["reason_code"] == "DATA_UNAVAILABLE" and s["signal"] == "WAIT"
          and "vwap" in s)


# ---------------------------------------------------------------------------
def run_cycles(bot, fake, closes_list):
    for closes in closes_list:
        fake.candles = make_candles(closes)
        bot._loop_step()


def test_bot_reasons():
    print("\n2. [봇] 봇 단계 사유 덮어쓰기 + status 신규 필드")
    use_sender(SenderMock())
    base = [100.0] * 30

    # WAIT_START_TIME: 리셋 1시간 전, 시작 1시간 후 → 지금은 대기 구간
    h.reset_data_dir()
    now = datetime.now()
    reset_t = (now - timedelta(hours=1)).strftime("%H:%M")
    start_t = (now + timedelta(hours=1)).strftime("%H:%M")
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1", reset_time=reset_t, start_time=start_t), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        run_cycles(bot, fake, [base + [98.0]])
        st = bot.get_status()
        check("WAIT_START_TIME + waiting_for_start=True", st["reason_code"] == "WAIT_START_TIME"
              and st["waiting_for_start"] is True and start_t in st["reason_text"], st["reason_text"])
        check("대기 중 주문 없음(매매 동작 불변)", len(bot.virtual_broker.open_orders) == 0)

    # BUDGET_SHORT
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1", k_percent=0.1, initial_balance=10000.0), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        run_cycles(bot, fake, [base + [98.0]])
        st = bot.get_status()
        check("BUDGET_SHORT: 예산 < 1주 가격", st["reason_code"] == "BUDGET_SHORT" and "예산 10.00" in st["reason_text"],
              st["reason_text"])

    # BUY_ARMED + status 신규 필드 전부 존재
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        run_cycles(bot, fake, [base + [98.0]])
        st = bot.get_status()
        need = ["reason_code", "reason_text", "filters", "stop_loss_price", "adx", "rsi", "waiting_for_start",
                "entry_price", "position_qty", "signal", "vwap", "current_price"]
        check("status 신규 필드 모두 존재 (가동 중)", all(k in st for k in need), str([k for k in need if k not in st]))
        check("가동 중 BUY_ARMED, position_qty=0, entry_price=0",
              st["reason_code"] == "BUY_ARMED" and st["position_qty"] == 0 and st["entry_price"] == 0.0, st["reason_text"])

    # DATA_UNAVAILABLE (캔들 실패 / 잔고 실패 / 미체결 조회 실패)
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("real"), fake):
        bot = h.new_real_bot(); bot.running = True
        fake.candles = make_candles(base).iloc[0:0]
        bot._loop_step()
        check("DATA_UNAVAILABLE: 캔들 조회 실패", bot.status_cache["reason_code"] == "DATA_UNAVAILABLE"
              and "캔들" in bot.status_cache["reason_text"], bot.status_cache["reason_text"])
        fake.candles = make_candles(base + [98.0])
        fake.balance = {"cash": 0.0, "holdings": {}, "error": True}
        bot._loop_step()
        check("DATA_UNAVAILABLE: 잔고 조회 실패", bot.status_cache["reason_code"] == "DATA_UNAVAILABLE"
              and "잔고" in bot.status_cache["reason_text"])
        fake.balance = {"cash": 100000.0, "holdings": {}}
        fake.open_orders_fail = True
        bot._loop_step()
        check("DATA_UNAVAILABLE: 미체결 조회 실패 시 주문 보류 + 사유", bot.status_cache["reason_code"] == "DATA_UNAVAILABLE"
              and "미체결" in bot.status_cache["reason_text"] and len(fake.placed) == 0, bot.status_cache["reason_text"])

    # DAILY_LOSS_STOP (가상: 평단 100 x 50주, 가격 50 → 손실 25% > 한도 5%)
    h.reset_data_dir()
    VwapConfigManager.save_trades([{"trade_id": "seed", "timestamp": "2026-10-05 09:00:00", "ticker": "TEST",
                                    "side": "BUY", "price": 100.0, "qty": 50, "pnl": 0.0, "roi": 0.0}], "VIRTUAL_1")
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1", max_daily_loss_limit=5.0, initial_balance=10000.0), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        bot.daily_baseline_asset = 10000.0
        bot.last_baseline_date = bot_module.get_session_date(datetime.now(), "22:30")
        run_cycles(bot, fake, [[50.0] * 31])
        st = bot.get_status()
        evs = types_of("VIRTUAL_1")
        check("DAILY_LOSS_STOP: 봇 정지 + 정지 상태는 BOT_STOPPED + 사유에 손실한도",
              bot.running is False and st["reason_code"] == "BOT_STOPPED" and "손실 한도" in st["reason_text"],
              st["reason_text"])
        check("PANIC/BOT_STOP 이벤트 기록 + SIGNAL_CHANGE 사유 DAILY_LOSS_STOP",
              "PANIC" in evs and "BOT_STOP" in evs and any(e["type"] == "SIGNAL_CHANGE" and e["reason_code"] == "DAILY_LOSS_STOP"
                                                          for e in read_jsonl("VIRTUAL_1")), str(evs))
        sl = [t for t in VwapConfigManager.load_trades("VIRTUAL_1") if t["side"] == "STOP_LOSS"]
        check("패닉 가상 청산 레코드 reason_code=DAILY_LOSS_STOP", sl and sl[0].get("reason_code") == "DAILY_LOSS_STOP")

    # BOT_STOPPED (한 번도 가동 안 한 봇)
    with Patched(make_config("real"), FakeTossBroker("test_id", "test_secret", "1")):
        st = VWAPBot("REAL").get_status()
        check("BOT_STOPPED: 정지 상태 status", st["reason_code"] == "BOT_STOPPED" and st["waiting_for_start"] is False
              and st["position_qty"] == 0, st["reason_text"])


# ---------------------------------------------------------------------------
def test_signal_change_dedup():
    print("\n3. [이벤트] SIGNAL_CHANGE 는 signal/reason_code 가 바뀔 때만")
    use_sender(SenderMock())
    h.reset_data_dir()
    base = [100.0] * 30
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        run_cycles(bot, fake, [base + [101.0], base + [101.0, 101.2], base + [101.0, 101.2, 101.1]])
        sc = [e for e in read_jsonl("VIRTUAL_1") if e["type"] == "SIGNAL_CHANGE"]
        check("같은 ABOVE_VWAP 3주기 → SIGNAL_CHANGE 1건", len(sc) == 1 and sc[0]["reason_code"] == "ABOVE_VWAP", str(len(sc)))
        run_cycles(bot, fake, [base + [101.0, 101.2, 101.1, 97.0]])
        sc = [e for e in read_jsonl("VIRTUAL_1") if e["type"] == "SIGNAL_CHANGE"]
        check("BUY_ARMED 로 바뀌면 1건 추가 (총 2건) + prev 기록", len(sc) == 2 and sc[1]["reason_code"] == "BUY_ARMED"
              and sc[1]["data"].get("prev_reason_code") == "ABOVE_VWAP")
        evs = read_jsonl("VIRTUAL_1")
        placed = [e for e in evs if e["type"] == "ORDER_PLACED"]
        check("가상 매수 주문 ORDER_PLACED 이벤트(주문가/수량 포함)", len(placed) == 1 and placed[0]["data"].get("price")
              and placed[0]["data"].get("qty"), str(placed[0]["data"] if placed else None))
        e0 = evs[0]
        check("이벤트 스키마(ts/mode/type/level/reason_code/message/data)",
              set(e0.keys()) == {"ts", "mode", "type", "level", "reason_code", "message", "data"} and e0["mode"] == "VIRTUAL_1"
              and os.path.basename(ev.events_path("VIRTUAL_1")) == "vwap_events_virtual_1.jsonl")


# ---------------------------------------------------------------------------
def test_event_file():
    print("\n4. [이벤트 파일] 기록/조회/손상 줄/회전/쓰기 실패/동시성")
    h.reset_data_dir()
    for i in range(5):
        ev.append_event("REAL", "FILL" if i % 2 == 0 else "ORDER_PLACED", "info", "X", f"m{i}", {"i": i})
    with open(ev.events_path("REAL"), "a", encoding="utf-8") as f:
        f.write("{broken json\n")
    ev.append_event("REAL", "STOP_LOSS", "warn", "STOP_LOSS", "m5", {"i": 5})
    got = ev.read_events("REAL", limit=100)
    check("손상 줄 무시 + 최신순", [e["data"]["i"] for e in got] == [5, 4, 3, 2, 1, 0], str([e["data"]["i"] for e in got]))
    got = ev.read_events("REAL", limit=100, types=["FILL", "STOP_LOSS"])
    check("types 필터", [e["data"]["i"] for e in got] == [5, 4, 2, 0])
    check("limit", len(ev.read_events("REAL", limit=2)) == 2)

    orig = ev.MAX_EVENT_FILE_BYTES
    try:
        ev.MAX_EVENT_FILE_BYTES = 2000
        h.reset_data_dir()
        for i in range(60):
            ev.append_event("VIRTUAL_2", "SIGNAL_CHANGE", "info", "R", "x" * 50, {"i": i})
        p = ev.events_path("VIRTUAL_2")
        size_ok = os.path.getsize(p) < 2000 + 400
        check("크기 초과 시 .1 로 회전 (.1 하나만, 현재 파일 상한 근처)",
              os.path.exists(p + ".1") and not os.path.exists(p + ".2") and size_ok,
              f"cur={os.path.getsize(p)}, .1={os.path.getsize(p + '.1') if os.path.exists(p + '.1') else None}")
        got = ev.read_events("VIRTUAL_2", limit=500)
        idx = [e["data"]["i"] for e in got]
        cur_n = len(open(p, encoding="utf-8").read().splitlines())
        check("회전 후에도 최신순 연속 조회 (현재 파일 + .1 이어서)",
              idx == list(range(59, 59 - len(idx), -1)) and len(idx) > cur_n,
              f"n={len(idx)}, 현재파일={cur_n}줄")
    finally:
        ev.MAX_EVENT_FILE_BYTES = orig

    # 쓰기 실패: DATA_DIR 을 '파일' 경로로 바꿔 makedirs 실패 유도 → 예외 없이 None
    import core.vwap.config_manager as cm
    saved = cm.DATA_DIR
    blocker = os.path.join(TMP_DIR, "not_a_dir")
    with open(blocker, "w") as f:
        f.write("x")
    try:
        cm.DATA_DIR = os.path.join(blocker, "sub")
        r = ev.append_event("REAL", "ERROR", "error", "", "fail", {})
        check("쓰기 실패해도 예외 없이 None", r is None)
    except Exception as e:
        check("쓰기 실패해도 예외 없이 None", False, repr(e))
    finally:
        cm.DATA_DIR = saved

    h.reset_data_dir()
    def writer(n):
        for i in range(50):
            ev.append_event("VIRTUAL_3", "FILL", "info", "", f"t{n}-{i}", {"n": n, "i": i})
    ths = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    [t.start() for t in ths]; [t.join() for t in ths]
    lines = open(ev.events_path("VIRTUAL_3"), encoding="utf-8").read().splitlines()
    ok = all(json.loads(l) for l in lines)
    check("8스레드 x 50건 동시 기록 → 400줄 모두 유효 JSON", len(lines) == 400 and ok, str(len(lines)))


# ---------------------------------------------------------------------------
def make_client():
    from flask import Flask
    import api.vwap_api as vapi
    app = Flask("vwap_test")
    app.register_blueprint(vapi.vwap_bp, url_prefix="/vwap")
    return app.test_client(), vapi


def test_api():
    print("\n5~6. [API] /vwap/api/events, /vwap/api/status, /vwap/api/config")
    use_sender(SenderMock())
    h.reset_data_dir()
    cfg = make_config("real")
    cfg["toss_client_id"] = ""; cfg["toss_client_secret"] = ""; cfg["toss_account_seq"] = ""
    fake = FakeTossBroker("", "", "")
    from core.vwap.crypto import VwapCrypto
    orig_verify = VwapCrypto.verify_session_token
    with Patched(cfg, fake):
        client, vapi = make_client()
        r = client.get("/vwap/api/events?mode=REAL")
        check("세션 없으면 401 (admin_required 적용)", r.status_code == 401)
        VwapCrypto.verify_session_token = classmethod(lambda cls, token, max_age_seconds=86400: True)
        try:
            for i in range(600):
                ev.append_event("REAL", "FILL" if i % 3 == 0 else "SIGNAL_CHANGE", "info", "", f"m{i}", {"i": i})
            r = client.get("/vwap/api/events?mode=REAL")
            j = r.get_json()
            check("기본 limit 100, 최신순", r.status_code == 200 and j["status"] == "success" and len(j["events"]) == 100
                  and j["events"][0]["data"]["i"] == 599)
            j = client.get("/vwap/api/events?mode=REAL&limit=100000").get_json()
            check("limit 상한 500", len(j["events"]) == 500)
            j = client.get("/vwap/api/events?mode=REAL&limit=5&types=FILL").get_json()
            check("types=FILL 필터", len(j["events"]) == 5 and all(e["type"] == "FILL" for e in j["events"]))
            j = client.get("/vwap/api/events?mode=real&limit=abc").get_json()
            check("limit 파싱 실패 → 기본 100, mode 소문자 허용", len(j["events"]) == 100 and j["mode"] == "REAL")
            r = client.get("/vwap/api/events?mode=HACK")
            check("잘못된 mode → 400", r.status_code == 400)
            j = client.get("/vwap/api/events?mode=VIRTUAL_2").get_json()
            check("이벤트 없는 모드 → 빈 목록", j["status"] == "success" and j["events"] == [])

            # status: 가상봇 1주기 + 체결 → trades 신규 필드
            h.reset_data_dir()
            vbot = vapi.virtual_bots["VIRTUAL_1"]
            vbot.virtual_broker = None
            vbot.running = True
            vcfg = make_config("virtual_1")
            vcfg["toss_client_id"] = ""; vcfg["toss_client_secret"] = ""
            with Patched(vcfg, fake):
                fake.candles = make_candles([100.0] * 30 + [98.0]); vbot._loop_step()
                bp = vbot.virtual_broker.open_orders[0]["price"]
                fake.candles = make_candles([100.0] * 30 + [98.0, 98.0], last_low=bp - 0.5); vbot._loop_step()
                j = client.get("/vwap/api/status").get_json()
            vbot.running = False
            need = ["reason_code", "reason_text", "filters", "stop_loss_price", "adx", "rsi", "waiting_for_start",
                    "entry_price", "position_qty"]
            rb = j["real_bot"]
            check("status.real_bot 신규 필드 + BOT_STOPPED", all(k in rb for k in need) and rb["reason_code"] == "BOT_STOPPED",
                  str([k for k in need if k not in rb]))
            v1 = j["virtual_bots"]["VIRTUAL_1"]
            check("status.virtual_bots.VIRTUAL_1 (가동 중) 신규 필드 + 보유 반영",
                  all(k in v1 for k in need) and v1["position_qty"] > 0 and v1["entry_price"] > 0, f"{v1.get('reason_code')} qty={v1.get('position_qty')}")
            rt = j["metrics"]["virtual_1"]["recent_trades"]
            tf = ["slippage", "slippage_pct", "holding_minutes", "reason_code", "filters", "config_snapshot"]
            check("status trades 목록이 신규 필드를 그대로 내려줌", rt and all(k in rt[-1] for k in tf)
                  and rt[-1]["reason_code"] == "BUY_ARMED" and rt[-1]["config_snapshot"].get("n_percent") == 1.0,
                  str({k: rt[-1].get(k) for k in tf}) if rt else "없음")

            # config POST: discord_notify 키 허용 + bool 변환 (임시 CONFIG_PATH 에 저장)
            r = client.post("/vwap/api/config", json={"real_discord_notify": "false", "virtual_discord_notify": True})
            saved = json.load(open(os.path.join(TMP_DIR, "vwap_config.json"), encoding="utf-8"))
            check("config 저장 허용키 + bool 변환", r.status_code == 200 and saved.get("real_discord_notify") is False
                  and saved.get("virtual_discord_notify") is True)
            d = VwapConfigManager.get_default_config()
            check("기본값 real_discord_notify=True, virtual_discord_notify=False",
                  d["real_discord_notify"] is True and d["virtual_discord_notify"] is False)
        finally:
            VwapCrypto.verify_session_token = orig_verify


# ---------------------------------------------------------------------------
def test_trade_records():
    print("\n7. [거래 레코드] slippage 부호 / holding_minutes / 주문 당시 사유")
    check("BUY 불리(비싸게 체결) → +", compute_slippage("BUY", 100.5, 100.0) == (0.5, 0.5))
    check("BUY 유리(싸게 체결) → -", compute_slippage("BUY", 99.5, 100.0)[0] == -0.5)
    check("SELL 불리(싸게 체결) → +", compute_slippage("SELL", 99.5, 100.0)[0] == 0.5)
    check("STOP_LOSS 불리 → +", compute_slippage("STOP_LOSS", 95.0, 96.0)[0] == 1.0)
    check("의도가 없음 → None", compute_slippage("BUY", 100.0, 0) == (None, None))
    prior = [{"ticker": "T", "side": "BUY", "qty": 5, "timestamp": "2026-10-05 10:00:00"},
             {"ticker": "T", "side": "SELL", "qty": 5, "timestamp": "2026-10-05 10:30:00"},
             {"ticker": "T", "side": "BUY", "qty": 3, "timestamp": "2026-10-05 11:00:00"},
             {"ticker": "T", "side": "BUY", "qty": 2, "timestamp": "2026-10-05 11:10:00"}]
    check("holding_minutes: 재진입 첫 매수부터 (90.0분)", holding_minutes_for_exit(prior, "T", "2026-10-05 12:30:00") == 90.0)

    use_sender(SenderMock())
    h.reset_data_dir()
    bot = h.new_real_bot()
    bot._cycle = {"reason_code": "BUY_ARMED", "filters": {"adx": {"enabled": False}}, "config_snapshot": {"n_percent": 1.0},
                  "values": {}}
    bot._track_order("B1", "TEST", "BUY", 50.0, 10, vwap=50.6, target_price=50.0, signal="BUY")
    fake = FakeTossBroker()
    fake.order_details["B1"] = h.detail("FILLED", 10, 50.10)
    bot._reconcile_real_orders(fake, [], {"TEST": {"qty": 10, "entry_price": 50.1}}, 50.2)
    bot._cycle = {"reason_code": "SELL_ARMED", "filters": {}, "config_snapshot": {"m_percent": 1.0}, "values": {}}
    bot._track_order("S1", "TEST", "SELL", 51.0, 10, entry_price=50.1, signal="SELL")
    fake.order_details["S1"] = [dict(h.detail("FILLED", 10, 51.20, side="SELL"), filled_at="2026-10-05T23:46:15.000+09:00")]
    bot._reconcile_real_orders(fake, [], {}, 51.2)
    t = VwapConfigManager.load_trades("REAL")
    b, s_ = t[0], t[1]
    check("실거래 BUY: slippage=+0.10(불리), reason_code/filters/config_snapshot 스냅샷",
          abs(b["slippage"] - 0.10) < 1e-9 and b["slippage_pct"] > 0 and b["reason_code"] == "BUY_ARMED"
          and b["filters"] == {"adx": {"enabled": False}} and b["config_snapshot"] == {"n_percent": 1.0}
          and b["holding_minutes"] is None, str({k: b.get(k) for k in ("slippage", "slippage_pct", "reason_code")}))
    check("실거래 SELL: slippage=-0.20(유리), holding_minutes=15.0, 기존 필드 유지",
          abs(s_["slippage"] + 0.20) < 1e-9 and s_["holding_minutes"] == 15.0 and s_["reason_code"] == "SELL_ARMED"
          and all(k in s_ for k in ("trade_id", "timestamp", "pnl", "roi", "fill_source", "vwap", "order_price")),
          str({k: s_.get(k) for k in ("slippage", "holding_minutes", "timestamp")}))
    fills = [e for e in read_jsonl("REAL") if e["type"] == "FILL"]
    check("FILL 이벤트 2건 (trade 요약 data)", len(fills) == 2 and fills[1]["data"]["trade_id"] == "S1"
          and "slippage" in fills[1]["data"] and fills[1]["data"]["fill_source"] == "order_api")

    # 시장가 손절: 의도가 = 결정 시점 현재가
    h.reset_data_dir()
    bot = h.new_real_bot()
    fake = FakeTossBroker()
    fake.order_details["real_1"] = h.detail("FILLED", 10, 94.5, side="SELL")
    bot._submit_real_market_exit(fake, "TEST", 10, 100.0, 95.0, 99.0, 98.0, "STOP_LOSS")
    t = VwapConfigManager.load_trades("REAL")
    check("시장가 손절 slippage = 현재가 95.0 - 체결 94.5 = +0.5", t and t[0]["side"] == "STOP_LOSS"
          and abs(t[0]["slippage"] - 0.5) < 1e-9 and t[0]["intended_price"] == 95.0, str(t[0] if t else None))

    # 가상 매도 레코드
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        base = [100.0] * 30
        fake.candles = make_candles(base + [98.0]); bot._loop_step()
        bp = bot.virtual_broker.open_orders[0]["price"]
        fake.candles = make_candles(base + [98.0, 98.0], last_low=bp - 0.5); bot._loop_step()
        fake.candles = make_candles(base + [98.0, 98.0, 101.0]); bot._loop_step()
        sp = [o for o in bot.virtual_broker.open_orders if o["side"] == "SELL"][0]["price"]
        fake.candles = make_candles(base + [98.0, 98.0, 101.0, 101.0], last_high=sp + 0.5); bot._loop_step()
        vt = VwapConfigManager.load_trades("VIRTUAL_1")
        sell = [x for x in vt if x["side"] == "SELL"][0]
        check("가상 SELL 레코드: slippage=0(지정가 체결), holding_minutes 숫자, SELL_ARMED, fill_source=simulated",
              sell["slippage"] == 0 and isinstance(sell["holding_minutes"], float) and sell["reason_code"] == "SELL_ARMED"
              and sell["fill_source"] == "simulated", str({k: sell.get(k) for k in ("slippage", "holding_minutes", "reason_code")}))
        check("가상 FILL 이벤트 2건", len([e for e in read_jsonl("VIRTUAL_1") if e["type"] == "FILL"]) == 2)


# ---------------------------------------------------------------------------
def test_discord():
    print("\n8. [Discord] mock 전송 — 대상/중복 억제/기본값/연속 실패/예외 무해")
    base = [100.0] * 30

    m = SenderMock(); use_sender(m)
    h.reset_data_dir()
    with Patched(make_config("real"), FakeTossBroker("test_id", "test_secret", "1")):
        bot = VWAPBot("REAL")
        bot._run_loop = lambda gen=None: None  # 실제 루프 스레드는 돌리지 않음
        bot.start(); bot.stop()
    check("REAL BOT_START/BOT_STOP → 2회 전송", len(m.calls) == 2 and "BOT_START" in m.calls[0], str(m.calls))

    m = SenderMock(); use_sender(m)
    bot = h.new_real_bot()
    for _ in range(3):
        bot._emit("STOP_LOSS", "warn", "STOP_LOSS", "손절", {"current_price": 96.9})
    check("같은 type+reason 60초 내 3회 → 1회 전송", len(m.calls) == 1)
    bot._emit("CRITICAL", "critical", "STOP_LOSS", "실패", {})
    bot._emit("SIGNAL_CHANGE", "info", "HOLD", "x", {})
    bot._emit("ORDER_REPLACED", "info", "BUY_ARMED", "x", {})
    check("CRITICAL 은 전송, SIGNAL_CHANGE/ORDER_REPLACED 는 미전송", len(m.calls) == 2)
    bot._emit("FILL", "info", "BUY_ARMED", "a", {}, dedup_extra="t1")
    bot._emit("FILL", "info", "BUY_ARMED", "b", {}, dedup_extra="t2")
    bot._emit("FILL", "info", "BUY_ARMED", "b", {}, dedup_extra="t2")
    check("서로 다른 체결(trade_id)은 각각 전송, 같은 체결은 억제", len(m.calls) == 4)
    ev.notifier._last_sent = {k: v - 61 for k, v in ev.notifier._last_sent.items()}
    bot._emit("STOP_LOSS", "warn", "STOP_LOSS", "손절", {})
    check("60초 경과 후 같은 알림 재전송", len(m.calls) == 5)
    check("메시지에 계좌번호/시크릿 없음 + 사유 포함",
          all("test_secret" not in c and "toss_account" not in c for c in m.calls)
          and any("사유" in c for c in m.calls), m.calls[0][:80])

    # 실제 주기: REAL 지정가 매수 → ORDER_PLACED 1회
    m = SenderMock(); use_sender(m)
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.balance = {"cash": 100000.0, "holdings": {}}
    with Patched(make_config("real"), fake):
        bot = h.new_real_bot(); bot.running = True
        fake.candles = make_candles(base + [98.0]); bot._loop_step()
    check("REAL 주기 ORDER_PLACED → 1회 전송(사유 BUY_ARMED 포함)", len(m.calls) == 1 and "ORDER_PLACED" in m.calls[0]
          and "VWAP" in m.calls[0], m.calls[0] if m.calls else "없음")

    # 가상봇 기본 OFF
    m = SenderMock(); use_sender(m)
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("virtual_1"), fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        fake.candles = make_candles(base + [98.0]); bot._loop_step()
        bot._run_loop = lambda gen=None: None
        bot.running = False; bot.start(); bot.stop()
    check("가상봇 기본 OFF → 0회 (이벤트 파일에는 기록)", len(m.calls) == 0 and "ORDER_PLACED" in types_of("VIRTUAL_1"))

    # 설정으로 REAL 알림 OFF
    m = SenderMock(); use_sender(m)
    h.reset_data_dir()
    cfg = make_config("real"); cfg["real_discord_notify"] = False
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.balance = {"cash": 100000.0, "holdings": {}}
    with Patched(cfg, fake):
        bot = h.new_real_bot(); bot.running = True
        fake.candles = make_candles(base + [98.0]); bot._loop_step()
    check("real_discord_notify=False → 0회", len(m.calls) == 0)

    # 연속 조회 실패: 12주기 실패 → ERROR 알림 정확히 1회
    m = SenderMock(); use_sender(m)
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    with Patched(make_config("real"), fake):
        bot = h.new_real_bot(); bot.running = True
        fake.candles = make_candles(base).iloc[0:0]
        for _ in range(12):
            bot._loop_step()
        errs = [e for e in read_jsonl("REAL") if e["type"] == "ERROR"]
        check(f"{bot_module.ERROR_STREAK_NOTIFY}주기 연속 실패 시 ERROR 1회 (12주기 실패에도 1회)",
              len(m.calls) == 1 and "ERROR" in m.calls[0] and len(errs) == 1, f"calls={len(m.calls)}, errs={len(errs)}")
        sc = [e for e in read_jsonl("REAL") if e["type"] == "SIGNAL_CHANGE"]
        check("연속 실패 동안 SIGNAL_CHANGE 1건만", len(sc) == 1)

    # 전송 함수 예외 + 비동기(워커 스레드) 경로 — 봇은 영향 없음
    m = SenderMock(raise_exc=True); ev.set_sender(m); ev.notifier.synchronous = False; ev.notifier._last_sent.clear()
    bot = h.new_real_bot()
    try:
        bot._emit("CRITICAL", "critical", "X", "테스트", {})
        import time as _t
        for _ in range(50):
            if m.calls:
                break
            _t.sleep(0.02)
        check("전송 예외/비동기 워커 경로에서도 _emit 정상 반환 + 전송 시도됨", len(m.calls) == 1)
    except Exception as e:
        check("전송 예외/비동기 워커 경로에서도 _emit 정상 반환 + 전송 시도됨", False, repr(e))
    ev.notifier.synchronous = True


def main():
    print("=" * 70)
    print(" VWAP 봇 관측성(2단계) 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % TMP_DIR)
    print("=" * 70)
    tests = [test_strategy_reasons, test_bot_reasons, test_signal_change_dedup, test_event_file,
             test_api, test_trade_records, test_discord]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    ev.set_sender(lambda message: True)
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    after = h._hash_dir(h.REAL_DATA_DIR)
    check("운영 data/ 디렉터리 파일 변경 없음 (테스트 전후 해시 동일)", after == h.REAL_DATA_HASH_BEFORE)

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
