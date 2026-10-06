"""
VWAP 3단계 SR-1 검증 — 전략 플러그인 동치·인과성 (ADR-0007 D1, 설계 문서 §3·§11 T-P1~P4). 네트워크 없이 실행됩니다.

검증 항목
  T-P0  StrategyContext.from_config 가 bot.py 와 같은 키·기본값·타입으로 파라미터를 읽는지
  T-P1  S0CurrentStrategy.evaluate == VwapStrategy.get_signals (같은 봉 창)
        a) 무작위 경로 200개 × ADX/RSI 필터 × Dev Band on/off (8조합) × 무작위 포지션 — 신호/타겟/손절/사유/필터/지표 전부 일치
        b) 미리 계산한 df 의 j 행 판단 == j 까지 자른 df 의 마지막 행 판단 (get_signals 가 마지막 행만 읽는다는 전제 확인)
        c) 실제 봇(VIRTUAL_1) _loop_step 결과(status 의 signal/타겟/사유)와 플러그인 판단이 같음 (시각 고정)
  T-P2  인과성 — j 이후 행을 훼손/절단해도 prepare 결과 j 행(vwap/stdev/rsi/adx/session_start)과 evaluate(j)가 불변
  T-P3  엔진 동치 — 합성 데이터(세션 경계 일치, 필터·밴드 off)에서
        run_backtest(PluginEngineAdapter(S0)) 거래 목록·자산곡선 == run_backtest(backtest.strategies.S0_Current)
        + 거래 시작 대기(start_time) 구간에는 진입이 없고, 보유 포지션 청산은 일어남 (ADR-0009)
        + start_time 사용 시 결과 == 'S0_Current + 대기 중 무보유 매수 차단' (연구 엔진과의 차이가 의도된 것임을 명시)
  T-P4  rules.is_waiting_for_start == 실제 봇 7-1 동작 (US 자동/고정, 국내, 서머타임 전환일, 자정 경계, 잘못된 형식)
  T-P5  (ADR-0009) 거래 시작 대기 중 포지션 보호 — 실제 봇 == 플러그인(보유/무보유), 가상봇·REAL mock 에서
        보유 손절 시장가 청산 / 무보유 BUY 차단 / 미체결 매수·매도 정리
  T-G   운영 data/ 해시 전후 동일, trading_bot_*.log 신규 생성 없음

실행:  PYTHONUTF8=1 python scripts/test_vwap_stage3_plugin.py
"""
import os
import sys
import glob
import time
import shutil
import logging
import traceback
from datetime import datetime, timedelta, date

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

LOGS_BEFORE = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log")))

# 1단계 하네스 재사용: import 시점에 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + Discord 전송 no-op
# (vwap_bot_virtual_1 / vwap_bot_real 로거를 콘솔 전용으로 미리 등록 → 운영 로그 파일 생성 없음)
import test_vwap_reliability as h  # noqa: E402

for _name in ["vwap_bot_virtual_1", "vwap_bot_real"]:
    for _hd in logging.getLogger(_name).handlers:
        _hd.setLevel(logging.CRITICAL)   # 봇 INFO/ERROR 로그로 출력이 넘치지 않게 (setup_logger 가 로거 레벨은 INFO 로 되돌림)

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402
from core.vwap.config_manager import VwapConfigManager  # noqa: E402
from core.vwap.session import SessionSpec  # noqa: E402
from core.vwap.strategy import VwapStrategy  # noqa: E402
from core.vwap.strategies import S0CurrentStrategy, StrategyContext, PositionView  # noqa: E402
from core.vwap.strategies import rules  # noqa: E402
from core.vwap.replay_engine_adapter import (  # noqa: E402
    PluginEngineAdapter, build_engine_frame, signal_to_decision)
from backtest.engine import run_backtest, CostModel, Decision  # noqa: E402
from backtest.indicators import add_indicators  # noqa: E402  (테스트 비교 기준으로만 사용. 운영 코드는 import 안 함)
from backtest.strategies import S0_Current  # noqa: E402

check = h.check
make_config = h.make_config
FakeTossBroker = h.FakeTossBroker
Patched = h.Patched

S0 = S0CurrentStrategy()
# 봇 '주문 단계'(9-x)가 전략 사유를 덮어쓰는 사유 코드 — 전략·대기 판단(플러그인 범위) 밖이라 사유 비교에서만 제외
BOT_ORDER_STAGE_REASONS = ("BUDGET_SHORT",)
FILTER_COMBOS = [(a, r, b) for a in (False, True) for r in (False, True) for b in (False, True)]


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
class FixedClock:
    """bot 모듈의 datetime.now() 를 고정 시각으로."""

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


def synth_bars(rng, start, n, step_min=1, base=100.0, mean_revert=0.05, vol=0.15):
    """평균회귀 + 잡음의 결정적 OHLCV. time 은 KST naive, 봉 간격 step_min 분."""
    times = pd.date_range(start, periods=n, freq=f"{step_min}min")
    x = np.empty(n)
    level = base
    for i in range(n):
        level += mean_revert * (base - level) + rng.normal(0, vol)
        x[i] = level
    close = np.round(np.maximum(x, 1.0), 2)
    open_ = np.round(np.r_[close[0], close[:-1]] + rng.normal(0, vol * 0.3, n), 2)
    hi = np.round(np.maximum(open_, close) + np.abs(rng.normal(0, vol * 0.6, n)), 2)
    lo = np.round(np.minimum(open_, close) - np.abs(rng.normal(0, vol * 0.6, n)), 2)
    volume = np.round(rng.uniform(100, 5000, n))
    return pd.DataFrame({"time": times, "open": open_, "high": hi, "low": lo, "close": close, "volume": volume})


def ctx_of(market="US", ticker="TEST", reset="22:30", start="", interval="1m", **params):
    base = dict(n_percent=1.0, m_percent=1.0, x_percent=2.0, k_percent=10.0, initial_balance=10000.0,
                max_daily_loss_limit=5.0, use_adx_filter=False, adx_threshold=25.0, use_rsi_filter=False,
                rsi_threshold=30.0, use_vwap_band=False, vwap_band_sigma=2.0)
    base.update(params)
    return StrategyContext(ticker=ticker, market=market, interval=interval, reset_time=reset, start_time=start,
                           params=base)


def raw_signals(df, ctx, qty, entry):
    p = ctx.params
    return VwapStrategy.get_signals(df, p["n_percent"], p["m_percent"], p["x_percent"], qty, entry,
                                    use_adx_filter=p["use_adx_filter"], adx_threshold=p["adx_threshold"],
                                    use_rsi_filter=p["use_rsi_filter"], rsi_threshold=p["rsi_threshold"],
                                    use_vwap_band=p["use_vwap_band"], vwap_band_sigma=p["vwap_band_sigma"])


def _same(a, b):
    """NaN 끼리는 같다고 보는 동등 비교 (첫 봉 RSI 는 diff() 때문에 NaN — 양쪽 다 NaN 이면 같은 값)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, float) and isinstance(b, float) and np.isnan(a) and np.isnan(b):
        return True
    return a == b


def sig_diff(sig, raw):
    """StrategySignal 과 get_signals dict 의 불일치 필드 목록 (빈 목록이면 일치)."""
    bad = []
    pairs = [("signal", sig.signal, raw["signal"]),
             ("reason_code", sig.reason_code, raw.get("reason_code")),
             ("reason_text", sig.reason_text, raw.get("reason_text")),
             ("target_buy_price", sig.target_buy_price, raw.get("target_buy_price")),
             ("target_sell_price", sig.target_sell_price, raw.get("target_sell_price")),
             ("stop_loss_price", sig.stop_loss_price, raw.get("stop_loss_price")),
             ("filters", dict(sig.filters), raw.get("filters"))]
    for k in ("vwap", "adx", "rsi", "vwap_stdev", "current_price"):
        pairs.append((k, sig.indicators[k], raw.get(k)))
    for name, a, b in pairs:
        if not _same(a, b):
            bad.append(f"{name}: {a!r} != {b!r}")
    return bad


CTX_VARIANTS = [
    dict(market="US", ticker="TEST", reset="22:30"),      # US_AUTO
    dict(market="US", ticker="TEST", reset="21:00"),      # 고정 리셋
    dict(market="KR", ticker="005930", reset="09:00"),    # 국내 고정
]


# ---------------------------------------------------------------------------
# T-P0
# ---------------------------------------------------------------------------
def test_context_from_config():
    print("\nT-P0. StrategyContext.from_config — bot.py 와 같은 파라미터 해석")
    cfg = make_config("real", n_percent="0.7", m_percent=1, use_adx_filter=1, adx_threshold="20",
                      use_vwap_band=True, vwap_band_sigma="1.5", start_time="23:00")
    ctx = StrategyContext.from_config(cfg, "REAL")
    p = ctx.params
    check("접두어 대소문자 무관, 타입 변환(float/bool) bot 과 동일",
          p["n_percent"] == 0.7 and p["m_percent"] == 1.0 and p["use_adx_filter"] is True
          and p["adx_threshold"] == 20.0 and p["vwap_band_sigma"] == 1.5 and ctx.start_time == "23:00",
          str(dict(p)))
    check("세션 규칙 = SessionSpec.for_market(market, reset_time, ticker) (US 22:30 → US_AUTO)",
          ctx.session_spec().mode == SessionSpec.for_market("US", "22:30", "TEST").mode == "US_AUTO")
    cfg_v = make_config("virtual_1", ticker="005930", market="KR", reset_time="09:00")
    ctx_v = StrategyContext.from_config(cfg_v, "VIRTUAL")
    check("'VIRTUAL' 접두어 → virtual_1 (bot 과 동일)", ctx_v.ticker == "005930" and ctx_v.reset_time == "09:00")
    cfg_missing = dict(cfg)
    for k in ("real_use_rsi_filter", "real_rsi_threshold", "real_max_daily_loss_limit", "real_start_time"):
        cfg_missing.pop(k, None)
    ctx_m = StrategyContext.from_config(cfg_missing, "real")
    check("선택 키 누락 시 bot 과 같은 기본값 (rsi 30.0, 손실한도 5.0, start_time '')",
          ctx_m.params["rsi_threshold"] == 30.0 and ctx_m.params["use_rsi_filter"] is False
          and ctx_m.params["max_daily_loss_limit"] == 5.0 and ctx_m.start_time == "")
    try:
        ctx.params["n_percent"] = 9  # type: ignore[index]
        immutable = False
    except TypeError:
        immutable = True
    check("ctx.params 는 읽기 전용", immutable)


# ---------------------------------------------------------------------------
# T-P1
# ---------------------------------------------------------------------------
def test_p1_signal_equivalence():
    print("\nT-P1. S0CurrentStrategy.evaluate == VwapStrategy.get_signals (같은 창)")
    rng = np.random.default_rng(20261007)
    n_paths = 200
    total = mism_a = mism_b = 0
    first_bad = ""
    signals_seen = set()
    for k in range(n_paths):
        cv = CTX_VARIANTS[k % len(CTX_VARIANTS)]
        # 리셋·자정을 지나는 다양한 시작 시각
        start = datetime(2026, 10, 28, 20, 0) + timedelta(minutes=int(rng.integers(0, 6 * 24 * 60)))
        n = int(rng.integers(40, 400))
        bars = synth_bars(rng, start, n, vol=float(rng.uniform(0.05, 0.4)))
        for use_adx, use_rsi, use_band in FILTER_COMBOS:
            ctx = ctx_of(**cv, n_percent=float(rng.choice([0.2, 0.5, 1.0])),
                         m_percent=float(rng.choice([0.2, 0.5, 1.0])), x_percent=float(rng.choice([0.5, 1.0, 2.0])),
                         use_adx_filter=use_adx, adx_threshold=float(rng.choice([15.0, 25.0, 40.0])),
                         use_rsi_filter=use_rsi, rsi_threshold=float(rng.choice([30.0, 50.0, 70.0])),
                         use_vwap_band=use_band, vwap_band_sigma=float(rng.choice([1.0, 2.0])))
            last_close = float(bars["close"].iloc[-1])
            r = rng.random()
            if r < 0.4:
                qty, entry = 0.0, 0.0
            elif r < 0.7:
                qty, entry = float(rng.integers(1, 50)), last_close * float(rng.uniform(0.97, 1.03))
            else:
                qty, entry = float(rng.integers(1, 50)), last_close * float(rng.uniform(1.0, 1.06))  # 손절 근처
            pos = PositionView(qty=qty, entry_price=entry)

            # a) 실시간 봇과 같은 창: 봇은 받은 창 전체로 calculate_vwap → get_signals(마지막 행)
            df_bot = VwapStrategy.calculate_vwap(bars, ctx.reset_time,
                                                 session=SessionSpec.for_market(ctx.market, ctx.reset_time, ctx.ticker))
            raw = raw_signals(df_bot, ctx, qty, entry)
            prepared = S0.prepare(bars, ctx)
            sig = S0.evaluate(prepared, len(prepared) - 1, pos, ctx)
            total += 1
            signals_seen.add(sig.signal)
            bad = sig_diff(sig, raw)
            if bad:
                mism_a += 1
                first_bad = first_bad or f"path {k} combo {(use_adx, use_rsi, use_band)}: {bad[:3]}"

            # b) 미리 계산한 df 의 j 행 == j 까지 자른 prepared 의 마지막 행
            j = int(rng.integers(0, len(prepared)))
            sig_j = S0.evaluate(prepared, j, pos, ctx)
            raw_j = raw_signals(prepared.iloc[: j + 1], ctx, qty, entry)
            if sig_diff(sig_j, raw_j):
                mism_b += 1
    check(f"a) 같은 창 판단 일치: {total - mism_a}/{total} (경로 {n_paths} × 필터·밴드 8조합)", mism_a == 0, first_bad)
    check(f"b) 사전계산 j 행 판단 == 잘린 창 마지막 행 판단: {total - mism_b}/{total}", mism_b == 0)
    check("커버리지: BUY/SELL/STOP_LOSS/HOLD/WAIT 신호가 모두 나옴", signals_seen >= {"BUY", "SELL", "STOP_LOSS", "HOLD", "WAIT"},
          str(sorted(signals_seen)))

    # c) 실제 봇 1주기와 대조 (무포지션 가상봇, 시각 고정, 거래 시작 대기 포함)
    rng = np.random.default_rng(77)
    n_cases = mism_c = n_overrode = 0
    bad_c = ""
    cases = []
    for k in range(48):
        cv = CTX_VARIANTS[k % len(CTX_VARIANTS)]
        use_adx, use_rsi, use_band = FILTER_COMBOS[k % 8]
        now = datetime(2026, 10, 29, 21, 0) + timedelta(minutes=int(rng.integers(0, 5 * 24 * 60)))
        start_time = ["", "23:00", "00:30", "10:00"][k % 4]
        cases.append((cv, use_adx, use_rsi, use_band, now, start_time))
    for cv, use_adx, use_rsi, use_band, now, start_time in cases:
        # 봇이 '현재 진행 중 봉'까지 받은 상황: 마지막 봉 시각 = now 의 분 단위 내림
        last = now.replace(second=0, microsecond=0)
        bars = synth_bars(rng, last - timedelta(minutes=199), 200, vol=0.3)
        over = dict(ticker=cv["ticker"], market=cv["market"], reset_time=cv["reset"], start_time=start_time,
                    use_adx_filter=use_adx, adx_threshold=25.0, use_rsi_filter=use_rsi, rsi_threshold=50.0,
                    use_vwap_band=use_band, vwap_band_sigma=1.0, k_percent=50.0, initial_balance=1_000_000.0)
        cfg = make_config("virtual_1", **over)
        fake = FakeTossBroker("test_id", "test_secret", "1")
        fake.candles = bars
        h.reset_data_dir()
        with FixedClock(now), Patched(cfg, fake):
            bot = VWAPBot("VIRTUAL_1")
            bot.running = True
            bot._loop_step()
        st = bot.status_cache
        ctx = StrategyContext.from_config(cfg, "virtual_1")
        sig = S0.evaluate(S0.prepare(bars, ctx), len(bars) - 1, PositionView(), ctx, now=now)
        n_cases += 1
        exp = (st.get("signal"), st.get("target_buy_price"), st.get("target_sell_price"), st.get("stop_loss_price"),
               bool(st.get("waiting_for_start")))
        got = (sig.signal, round(sig.target_buy_price, 2), round(sig.target_sell_price, 2),
               round(sig.stop_loss_price, 2), sig.reason_code == "WAIT_START_TIME")
        # 사유는 항상 비교. 예외는 '봇 주문 단계가 사유를 덮어쓴 경우'뿐 (QA 경미 지적 반영 — 2026-10-07 범위 축소):
        #   BUDGET_SHORT = 9-3 에서 예산 부족으로 매수 보류 → 플러그인 판단이 BUY 일 때만 정당한 덮어쓰기
        st_reason = st.get("reason_code")
        bot_overrode = st_reason in BOT_ORDER_STAGE_REASONS and sig.signal == "BUY"
        reason_ok = (st_reason == sig.reason_code) or bot_overrode
        n_overrode += bot_overrode
        if exp != got or not reason_ok:
            mism_c += 1
            bad_c = bad_c or f"{now} {cv} start={start_time}: bot={exp}/{st.get('reason_code')} plugin={got}/{sig.reason_code}"
    check(f"c) 실제 봇 _loop_step 결과와 플러그인 판단·사유 일치: {n_cases - mism_c}/{n_cases} "
          f"(봇 주문 단계 BUDGET_SHORT 덮어쓰기 {n_overrode}건만 사유 비교 제외)", mism_c == 0, bad_c)


# ---------------------------------------------------------------------------
# T-P2
# ---------------------------------------------------------------------------
COLS = ["vwap", "vwap_stdev", "rsi", "adx", "session_start", "close"]


def _row_equal(a, b, j):
    for c in COLS:
        va, vb = a[c].iloc[j], b[c].iloc[j]
        if not (va == vb or (isinstance(va, float) and np.isnan(va) and np.isnan(vb))):
            return f"{c}: {va!r} != {vb!r}"
    return ""


def test_p2_causality():
    print("\nT-P2. 인과성 — j 이후 행 훼손/절단에도 j 행 지표·판단 불변")
    rng = np.random.default_rng(4242)
    n_paths, bad_corrupt, bad_trunc, bad_eval = 60, 0, 0, 0
    first = ""
    for k in range(n_paths):
        cv = CTX_VARIANTS[k % len(CTX_VARIANTS)]
        start = datetime(2026, 10, 30, 18, 0) + timedelta(minutes=int(rng.integers(0, 4 * 24 * 60)))
        bars = synth_bars(rng, start, int(rng.integers(120, 600)), vol=0.25)
        ctx = ctx_of(**cv, start="23:00" if k % 2 else "", use_adx_filter=bool(k % 3 == 0), use_rsi_filter=bool(k % 4 == 0),
                     use_vwap_band=bool(k % 5 == 0), rsi_threshold=50.0)
        full = S0.prepare(bars, ctx)
        for _ in range(5):
            j = int(rng.integers(28, len(bars) - 1))   # ADX 는 28봉 미만 창이면 0 (아래 설명) → 28 이상만
            # (1) 훼손: j 이후 OHLCV 를 무작위로 바꿈 (시각은 유지)
            corrupt = bars.copy()
            m = len(bars) - (j + 1)
            for c in ("open", "high", "low", "close"):
                corrupt.loc[j + 1:, c] = corrupt.loc[j + 1:, c].to_numpy() * rng.uniform(0.5, 1.5, m)
            corrupt.loc[j + 1:, "volume"] = rng.uniform(1, 1e6, m)
            pc = S0.prepare(corrupt, ctx)
            d1 = _row_equal(full, pc, j)
            # (2) 절단: j 까지만
            pt = S0.prepare(bars.iloc[: j + 1].reset_index(drop=True), ctx)
            d2 = _row_equal(full, pt, j)
            # (3) evaluate(j) 불변 (보유/무보유 둘 다)
            for pos in (PositionView(), PositionView(qty=10, entry_price=float(bars["close"].iloc[j]) * 1.01)):
                a, b, c = (S0.evaluate(x, j, pos, ctx) for x in (full, pc, pt))
                if not (a == b == c):
                    bad_eval += 1
            bad_corrupt += bool(d1)
            bad_trunc += bool(d2)
            first = first or d1 or d2
    total = n_paths * 5
    check(f"훼손: prepare j 행 불변 {total - bad_corrupt}/{total}", bad_corrupt == 0, first)
    check(f"절단(j+1 ≥ 28행): prepare j 행 불변 {total - bad_trunc}/{total}", bad_trunc == 0, first)
    check(f"evaluate(j) 불변 (보유/무보유) {total * 2 - bad_eval}/{total * 2}", bad_eval == 0)
    # 문서화용 사실 확인: 전체 행 수가 28 미만이면 ADX 가 0, 15 미만이면 RSI 가 50 으로 채워짐(운영 calculate_* 의 길이 조건).
    short = S0.prepare(synth_bars(rng, datetime(2026, 10, 5, 10, 0), 20), ctx_of())
    check("참고: 20행 창은 ADX=0 (길이 조건 — 리플레이는 전체 구간을 한 번에 prepare 하므로 영향 없음)",
          bool((short["adx"] == 0).all()))
    # evaluate 기본 now 는 j 봉 마감 시각 (미래 행을 읽지 않음)
    b = synth_bars(rng, datetime(2026, 10, 5, 22, 0), 10, step_min=5)
    pb = S0.prepare(b, ctx_of(interval="5m"))
    check("evaluate 기본 now = time[j] + 봉 간격 (5m)",
          S0CurrentStrategy.decision_time(pb, 3, ctx_of(interval="5m")) == datetime(2026, 10, 5, 22, 20))


# ---------------------------------------------------------------------------
# T-P3
# ---------------------------------------------------------------------------
def _trade_tuple(t):
    return (pd.Timestamp(t.entry_time), pd.Timestamp(t.exit_time), t.entry_price, t.exit_price, t.qty,
            t.gross_pnl, t.fees, t.net_pnl, t.ret_pct, t.bars_held, t.exit_reason)


def test_p3_engine_equivalence():
    print("\nT-P3. 엔진 동치 — PluginEngineAdapter(S0) vs backtest.strategies.S0_Current")
    scenarios = []
    # (ctx 변형, 시작, 봉 수, 간격)
    scenarios.append((dict(market="US", ticker="TEST", reset="22:30"), datetime(2026, 10, 29, 20, 0), 6 * 288, 5))   # DST 종료 포함
    scenarios.append((dict(market="US", ticker="TEST", reset="22:30"), datetime(2027, 3, 12, 20, 0), 4 * 288, 5))    # DST 시작 포함
    scenarios.append((dict(market="KR", ticker="005930", reset="09:00"), datetime(2026, 10, 5, 8, 0), 3 * 1440, 1))
    scenarios.append((dict(market="US", ticker="TEST", reset="21:00"), datetime(2026, 10, 5, 20, 0), 8 * 96, 15))
    params = [(1.0, 1.0, 2.0), (0.3, 0.5, 1.0), (0.1, 0.2, 0.5), (2.0, 1.0, 3.0)]
    costs = [CostModel(), CostModel(0.0, 0.0, 0.0), CostModel(0.2, 0.05, 0.02)]

    rng = np.random.default_rng(31337)
    runs = mism = vwap_mism = n_trades = 0
    reasons = {}
    first = ""
    for sc_i, (cv, start, n, step) in enumerate(scenarios):
        for seed_i in range(2):
            bars = synth_bars(rng, start, n, step_min=step, vol=0.25)
            for (nn, mm, xx) in params:
                ctx = ctx_of(**cv, interval=f"{step}m", n_percent=nn, m_percent=mm, x_percent=xx)
                frame = build_engine_frame(S0, bars, ctx)
                # 기준: 같은 세션 라벨을 준 원본 봉 → backtest.indicators.add_indicators → S0_Current
                ref_in = bars.copy()
                ref_in["session_date"] = frame["session_date"].to_numpy()
                ref = add_indicators(ref_in)
                if not (np.array_equal(ref["vwap"].to_numpy(), frame["vwap"].to_numpy())
                        and np.array_equal(ref["vwap_stdev"].to_numpy(), frame["vwap_stdev"].to_numpy())):
                    vwap_mism += 1
                for cost in costs:
                    r_ref = run_backtest(ref, S0_Current(nn, mm, xx), cost, initial_equity=10_000.0, k_percent=50.0)
                    r_plg = run_backtest(frame, PluginEngineAdapter(S0, ctx), cost, initial_equity=10_000.0, k_percent=50.0)
                    runs += 1
                    ta = [_trade_tuple(t) for t in r_ref.trades]
                    tb = [_trade_tuple(t) for t in r_plg.trades]
                    eq_ok = np.array_equal(r_ref.equity.to_numpy(), r_plg.equity.to_numpy())
                    if ta != tb or not eq_ok:
                        mism += 1
                        if not first:
                            k = next((i for i, (a, b) in enumerate(zip(ta, tb)) if a != b), min(len(ta), len(tb)))
                            first = (f"scenario {sc_i} params {(nn, mm, xx)}: 거래수 {len(ta)} vs {len(tb)}, "
                                     f"첫 불일치 #{k}: {ta[k] if k < len(ta) else None} vs {tb[k] if k < len(tb) else None}")
                    n_trades += len(ta)
                    for t in r_plg.trades:
                        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    check(f"세션 VWAP/표준편차 열 일치 (운영 calculate_vwap vs 연구 add_indicators): {runs // len(costs) - vwap_mism}/"
          f"{runs // len(costs)}", vwap_mism == 0)
    check(f"거래 목록·자산곡선 완전 일치: {runs - mism}/{runs} 실행 (총 거래 {n_trades}건)", mism == 0, first)
    check("커버리지: 지정가 매도(LIMIT_SELL)·시장가 손절(STOP_LOSS_MKT) 청산이 모두 발생",
          reasons.get("LIMIT_SELL", 0) > 0 and reasons.get("STOP_LOSS_MKT", 0) > 0, str(reasons))
    check("세션 경계: DST 종료 시나리오에서 11/01 세션이 23:30 에 시작 (session_date 라벨)",
          _first_bar_of_session(scenarios[0], rng) == datetime(2026, 11, 2, 23, 30))

    # 신호 → Decision 매핑 단위 확인
    from core.vwap.strategies.base import StrategySignal
    mk = lambda s, em=False: StrategySignal(s, "X", "", 99.0, 101.0, 97.0, {}, {}, em)  # noqa: E731
    d = [signal_to_decision(mk(s)) for s in ("BUY", "SELL", "STOP_LOSS", "HOLD", "WAIT")]
    d_em = signal_to_decision(mk("HOLD", True))
    check("매핑: BUY→buy_limit, SELL→sell_limit, STOP_LOSS→시장가(STOP_LOSS_MKT), HOLD/WAIT→무주문, exit_market→시장가(사유)",
          d[0].buy_limit == 99.0 and d[0].sell_limit is None and d[1].sell_limit == 101.0 and d[1].buy_limit is None
          and d[2].exit_market and d[2].note == "STOP_LOSS_MKT"
          and all(x.buy_limit is None and x.sell_limit is None and not x.exit_market for x in d[3:])
          and d_em.exit_market and d_em.note == "X")

    # 거래 시작 대기: 대기 구간 [T_reset, T_start) 에 진입이 없어야 함 (판단 시각 = j 봉 마감 = 진입 봉 시작)
    bars = synth_bars(np.random.default_rng(5), datetime(2026, 10, 5, 20, 0), 4 * 1440, step_min=1, vol=0.25)
    ctx_w = ctx_of(market="US", ticker="TEST", reset="22:30", start="23:30", n_percent=0.1, m_percent=0.1, x_percent=1.0)
    ctx_nw = ctx_of(market="US", ticker="TEST", reset="22:30", start="", n_percent=0.1, m_percent=0.1, x_percent=1.0)
    spec = ctx_w.session_spec()
    res_w = run_backtest(build_engine_frame(S0, bars, ctx_w), PluginEngineAdapter(S0, ctx_w), CostModel(),
                         initial_equity=10_000.0, k_percent=50.0)
    res_nw = run_backtest(build_engine_frame(S0, bars, ctx_nw), PluginEngineAdapter(S0, ctx_nw), CostModel(),
                          initial_equity=10_000.0, k_percent=50.0)
    in_window = lambda t: rules.is_waiting_for_start(t.to_pydatetime(), "23:30", "22:30", spec)  # noqa: E731
    w_entries = [t.entry_time for t in res_w.trades if in_window(t.entry_time)]
    nw_entries = [t.entry_time for t in res_nw.trades if in_window(t.entry_time)]
    check(f"거래 시작 23:30 설정 시 22:30~23:29 진입 0건 (대기 없음 설정에서는 {len(nw_entries)}건)",
          len(w_entries) == 0 and len(nw_entries) > 0, str(w_entries[:3]))

    # (ADR-0009, 의도된 차이) 대기 구간에도 '보유 포지션 청산'은 일어나야 함 — 전 세션에서 넘어온 포지션의 손절/매도 보호.
    # 연구 엔진 S0_Current 는 start_time 을 모델링하지 않으므로(backtest/ 는 검증 하네스라 변경 안 함), start_time 을 쓰면
    # 두 엔진 결과가 달라지는 것이 정상입니다. 그 차이가 '무보유 시 신규 진입 차단' 하나뿐임을 아래에서 확인합니다.
    w_exits = [t for t in res_w.trades if in_window(t.exit_time)]
    check(f"(ADR-0009) 대기 구간 중 보유 포지션 청산 발생 {len(w_exits)}건 "
          f"(지정가 매도 {sum(t.exit_reason == 'LIMIT_SELL' for t in w_exits)}, "
          f"시장가 손절 {sum(t.exit_reason == 'STOP_LOSS_MKT' for t in w_exits)})",
          len(w_exits) > 0, str(w_exits[:2]))
    ctx_ws = ctx_of(market="US", ticker="TEST", reset="22:30", start="23:30", n_percent=0.1, m_percent=0.5, x_percent=0.2)
    res_ws = run_backtest(build_engine_frame(S0, bars, ctx_ws), PluginEngineAdapter(S0, ctx_ws), CostModel(),
                          initial_equity=10_000.0, k_percent=50.0)
    ws_stops = [t for t in res_ws.trades if in_window(t.exit_time) and t.exit_reason == "STOP_LOSS_MKT"]
    check(f"(ADR-0009) 손절폭 0.2% 설정에서 대기 구간 중 시장가 손절(STOP_LOSS_MKT) {len(ws_stops)}건 발생, 대기 구간 진입 "
          f"{sum(in_window(t.entry_time) for t in res_ws.trades)}건",
          len(ws_stops) > 0 and not any(in_window(t.entry_time) for t in res_ws.trades), str(ws_stops[:1]))

    ref_in = bars.copy()
    ref_in["session_date"] = build_engine_frame(S0, bars, ctx_w)["session_date"].to_numpy()
    ref = add_indicators(ref_in)
    r_ref_plain = run_backtest(ref, S0_Current(0.1, 0.1, 1.0), CostModel(), initial_equity=10_000.0, k_percent=50.0)
    r_ref_wait = run_backtest(ref, _S0CurrentEntryBlocked(0.1, 0.1, 1.0, "23:30", "22:30", spec, 1), CostModel(),
                              initial_equity=10_000.0, k_percent=50.0)
    same = ([_trade_tuple(t) for t in r_ref_wait.trades] == [_trade_tuple(t) for t in res_w.trades]
            and np.array_equal(r_ref_wait.equity.to_numpy(), res_w.equity.to_numpy()))
    check("(ADR-0009) start_time 사용 시 플러그인 == 'S0_Current + 대기 중 무보유 매수만 차단' (거래·자산곡선 완전 일치)",
          same, f"거래 {len(r_ref_wait.trades)} vs {len(res_w.trades)}")
    differs = [_trade_tuple(t) for t in r_ref_plain.trades] != [_trade_tuple(t) for t in res_w.trades]
    check(f"(의도된 차이) 대기 미모델 S0_Current 와는 결과가 다름: 거래 {len(r_ref_plain.trades)}건 vs "
          f"{len(res_w.trades)}건 — 차이 원인은 대기 구간 신규 진입 차단뿐(위 항목)", differs)


class _S0CurrentEntryBlocked(S0_Current):
    """테스트 전용 기준: 연구 엔진 S0_Current 에 ADR-0009 대기 규칙(무보유일 때 매수 지정가만 취소)만 얹은 것.
    판단 시각 = j 봉 마감(time[j] + 봉 간격) — 플러그인 evaluate 기본 now 와 같음. backtest/ 원본은 수정하지 않음."""

    def __init__(self, n, m, x, start_time, reset_time, spec, step_min):
        super().__init__(n, m, x)
        self.start_time, self.reset_time, self.spec, self.step = start_time, reset_time, spec, step_min

    def decide(self, j, df, pos):
        d = super().decide(j, df, pos)
        if not pos.is_long and d.buy_limit is not None:
            now = pd.Timestamp(df["time"].iloc[j]).to_pydatetime() + timedelta(minutes=self.step)
            if rules.is_waiting_for_start(now, self.start_time, self.reset_time, self.spec):
                return Decision()
        return d


def _first_bar_of_session(sc, rng):
    cv, start, n, step = sc
    bars = synth_bars(np.random.default_rng(1), start, n, step_min=step)
    frame = build_engine_frame(S0, bars, ctx_of(**cv, interval=f"{step}m"))
    sess = frame[frame["session_date"] == "2026-11-02"]
    return pd.Timestamp(sess["time"].iloc[0]).to_pydatetime() if len(sess) else None


# ---------------------------------------------------------------------------
# T-P4
# ---------------------------------------------------------------------------
def test_p4_start_rule():
    print("\nT-P4. rules.is_waiting_for_start == 실제 봇 7-1 (시각 고정 _loop_step)")
    ctxs = [("TEST", "US", "22:30"), ("TEST", "US", "23:30"), ("TEST", "US", "21:00"), ("005930", "KR", "09:00")]
    starts = ["", "22:30", "23:30", "23:00", "22:00", "21:30", "00:30", "09:30", "08:00", "9:5", "25:00", "abc", "23:30:00"]
    days = [date(2026, 10, 30), date(2026, 11, 1), date(2026, 11, 2), date(2027, 3, 14)]
    hhmm = [(21, 29), (21, 59), (22, 29), (22, 30), (22, 45), (23, 0), (23, 29), (23, 30), (23, 59),
            (0, 0), (0, 30), (8, 59), (9, 0), (9, 30)]
    fake = FakeTossBroker("test_id", "test_secret", "1")
    flat_n = 41
    total = bad = waits = 0
    first = ""
    t0 = time.time()
    h.reset_data_dir()
    bot = VWAPBot("VIRTUAL_1")
    for ticker, market, reset in ctxs:
        for st in starts:
            cfg = make_config("virtual_1", ticker=ticker, market=market, reset_time=reset, start_time=st)
            spec = SessionSpec.for_market(market, reset, ticker)
            for d in days:
                for hh, mm in hhmm:
                    now = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm, second=20)
                    last = now.replace(second=0)
                    fake.candles = pd.DataFrame({
                        "time": pd.date_range(last - timedelta(minutes=flat_n - 1), periods=flat_n, freq="1min"),
                        "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000.0})
                    with FixedClock(now), Patched(cfg, fake):
                        bot.running = True
                        bot._loop_step()
                    got_bot = bool(bot.status_cache.get("waiting_for_start"))
                    got_rule = rules.is_waiting_for_start(now, st, reset, spec)
                    total += 1
                    waits += got_rule
                    if got_bot != got_rule:
                        bad += 1
                        first = first or f"{market} reset={reset} start={st!r} now={now}: bot={got_bot} rule={got_rule}"
    check(f"봇 7-1 과 규칙 함수 일치: {total - bad}/{total} (대기 True {waits}건, {time.time() - t0:.1f}s)",
          bad == 0 and waits > 0, first)

    # 대표 사례 (사람이 읽는 기대값)
    US = SessionSpec.for_market("US", "22:30", "TEST")
    D = datetime
    cases = [
        (D(2026, 10, 30, 22, 45), "23:00", "22:30", US, True, "서머타임 22:45, 시작 23:00 → 대기"),
        (D(2026, 10, 31, 0, 10), "00:30", "22:30", US, True, "자정 넘김 00:10, 시작 00:30 → 대기"),
        (D(2026, 10, 31, 0, 30), "00:30", "22:30", US, False, "00:30 정각 → 대기 끝"),
        (D(2026, 11, 2, 23, 40), "23:00", "22:30", US, False, "표준시 23:40, 시작 23:00(개장 30분 전) → 대기 없음"),
        (D(2026, 11, 2, 23, 40), "22:00", "22:30", US, True, "표준시 23:40, 시작 22:00(개장 90분 전) → 다음날까지 대기(기존 규칙)"),
        (D(2026, 11, 2, 23, 40), "abc", "22:30", US, False, "형식 오류 → 대기 없음"),
        (D(2026, 11, 2, 23, 40), "23:30", "22:30", US, False, "시작 = 실제 세션 시작(23:30) → 규칙 미적용"),
    ]
    ok = all(rules.is_waiting_for_start(n_, s_, r_, sp_) == exp for n_, s_, r_, sp_, exp, _ in cases)
    check("대표 사례 7건 (자정 경계·DST 보정·형식 오류)", ok,
          "; ".join(lbl for n_, s_, r_, sp_, exp, lbl in cases if rules.is_waiting_for_start(n_, s_, r_, sp_) != exp))

    # (ADR-0009) 플러그인 evaluate: 대기 중 보유 STOP_LOSS 는 덮지 않고, 무보유 판단만 WAIT/WAIT_START_TIME 로 덮음
    ctx = ctx_of(market="US", ticker="TEST", reset="22:30", start="23:00")
    bars = synth_bars(np.random.default_rng(3), datetime(2026, 10, 30, 22, 30), 20)
    pb = S0.prepare(bars, ctx)
    t_in = datetime(2026, 10, 30, 22, 36)
    sig_stop = S0.evaluate(pb, 5, PositionView(qty=10, entry_price=float(bars["close"].iloc[5]) * 1.5), ctx, now=t_in)
    check("(ADR-0009) 대기 중 보유 STOP_LOSS 는 그대로 STOP_LOSS (2026-10-07 이전: WAIT 로 덮였음)",
          sig_stop.signal == "STOP_LOSS" and sig_stop.reason_code != "WAIT_START_TIME", sig_stop.reason_text)
    sig_flat = S0.evaluate(pb, 5, PositionView(), ctx, now=t_in)
    check("(ADR-0009) 대기 중 무보유 판단은 WAIT/WAIT_START_TIME (전략 판단은 사유 문구에 보존)",
          sig_flat.signal == "WAIT" and sig_flat.reason_code == "WAIT_START_TIME" and "전략 판단:" in sig_flat.reason_text,
          sig_flat.reason_text)
    check("rules.start_wait_blocks 진리표: 무보유 → 덮음, 보유 STOP_LOSS/SELL/HOLD → 그대로, 보유 중 BUY(이론상) → 덮음",
          rules.start_wait_blocks("BUY", 0) and rules.start_wait_blocks("WAIT", 0)
          and not any(rules.start_wait_blocks(s, 10) for s in ("STOP_LOSS", "SELL", "HOLD", "WAIT"))
          and rules.start_wait_blocks("BUY", 10))


# ---------------------------------------------------------------------------
# T-P5 (ADR-0009) 거래 시작 대기 중 포지션 보호
# ---------------------------------------------------------------------------
def _bars_until(now, closes, last_high=None, last_low=None):
    """now 의 분 단위 내림 시각을 마지막 봉으로 하는 1분봉 (OHLC = 종가, 거래량 1000)."""
    last = now.replace(second=0, microsecond=0)
    n = len(closes)
    df = pd.DataFrame({"time": pd.date_range(last - timedelta(minutes=n - 1), periods=n, freq="1min"),
                       "open": closes, "high": closes, "low": closes, "close": closes, "volume": 1000.0})
    if last_high is not None:
        df.loc[n - 1, "high"] = last_high
    if last_low is not None:
        df.loc[n - 1, "low"] = last_low
    return df


def _seed_virtual_position(qty, price, ticker="TEST"):
    VwapConfigManager.save_trades([{"trade_id": "v_seed", "timestamp": "2026-10-29 10:00:00", "ticker": ticker,
                                    "side": "BUY", "price": float(price), "qty": qty, "pnl": 0.0, "roi": 0.0}],
                                  "VIRTUAL_1")


def _virtual_cycle(now, cfg, candles, seed=None):
    h.reset_data_dir()
    if seed:
        _seed_virtual_position(*seed)
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.candles = candles
    with FixedClock(now), Patched(cfg, fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True
        bot._loop_step()
    return bot, fake


def _real_cycle(now, cfg, candles, holdings, open_orders=(), details=None, open_orders_fail=False, track=()):
    h.reset_data_dir()
    fake = FakeTossBroker("test_id", "test_secret", "1")
    fake.candles = candles
    fake.balance = {"cash": 5000.0, "holdings": holdings}
    fake.open_orders = [dict(o) for o in open_orders]
    fake.order_details.update(details or {})
    fake.open_orders_fail = open_orders_fail
    with FixedClock(now), Patched(cfg, fake):
        bot = VWAPBot("REAL")
        bot.tracked_open_orders = {}
        bot.last_holdings_qty = {}
        bot._holdings_snapshot_ready = True
        for o in track:
            bot._track_order(o["order_id"], "TEST", o["side"], o["price"], o["qty"])
        bot.running = True
        bot._loop_step()
    return bot, fake


def test_p5_start_wait_protection():
    print("\nT-P5. (ADR-0009) 거래 시작 대기 중 — 보유 포지션 손절/매도는 평소대로, 무보유 신규 매수만 차단")
    D = datetime
    NOW = D(2026, 10, 30, 22, 45)            # 서머타임 중: 세션 22:30 시작, 거래 시작 23:30 → 대기 구간
    WAIT_CFG = dict(reset_time="22:30", start_time="23:30")
    flat100 = [100.0] * 30
    hold = {"TEST": {"qty": 10, "entry_price": 100.0}}

    # a) 동치: 실제 봇(VIRTUAL_1) 판단 == 플러그인 판단 — 대기 구간 안/밖 × 무보유/보유 (보유 판단이 대기에 안 덮이는지 포함)
    rng = np.random.default_rng(909)
    variants = [(dict(market="US", ticker="TEST", reset="22:30"), "23:30", D(2026, 10, 30, 22, 30)),
                (dict(market="US", ticker="TEST", reset="21:00"), "22:00", D(2026, 11, 3, 21, 0)),
                (dict(market="KR", ticker="005930", reset="09:00"), "10:00", D(2026, 10, 6, 9, 0))]
    n_cases = bad = 0
    first = ""
    held_in_window = set()
    flat_in_window = set()
    for k in range(72):
        cv, st_time, sess0 = variants[k % 3]
        now = sess0 + timedelta(minutes=int(rng.integers(1, 120)), seconds=20)   # 절반가량 대기 구간 안
        bars = synth_bars(rng, now.replace(second=0) - timedelta(minutes=199), 200, vol=0.3)
        last_close = float(bars["close"].iloc[-1])
        r = rng.random()
        if r < 0.3:
            qty, entry = 0, 0.0
        elif r < 0.65:
            qty, entry = int(rng.integers(1, 20)), round(last_close * float(rng.uniform(0.97, 1.03)), 2)
        else:
            qty, entry = int(rng.integers(1, 20)), round(last_close * float(rng.uniform(1.0, 1.06)), 2)  # 손절 근처
        cfg = make_config("virtual_1", ticker=cv["ticker"], market=cv["market"], reset_time=cv["reset"],
                          start_time=st_time, k_percent=50.0, initial_balance=1_000_000.0)
        bot, _ = _virtual_cycle(now, cfg, bars, seed=(qty, entry, cv["ticker"]) if qty else None)
        st = bot.status_cache
        ctx = StrategyContext.from_config(cfg, "virtual_1")
        sig = S0.evaluate(S0.prepare(bars, ctx), len(bars) - 1, PositionView(qty=float(qty), entry_price=entry), ctx, now=now)
        waiting = rules.is_waiting_for_start(now, st_time, cv["reset"], ctx.session_spec())
        n_cases += 1
        st_reason = st.get("reason_code")
        reason_ok = st_reason == sig.reason_code or (st_reason in BOT_ORDER_STAGE_REASONS and sig.signal == "BUY")
        if (st.get("signal"), bool(st.get("waiting_for_start"))) != (sig.signal, waiting) or not reason_ok:
            bad += 1
            first = first or (f"{now} {cv} start={st_time} qty={qty} entry={entry}: "
                              f"bot={st.get('signal')}/{st_reason}/{st.get('waiting_for_start')} "
                              f"plugin={sig.signal}/{sig.reason_code}/{waiting}")
        if waiting:
            (held_in_window if qty else flat_in_window).add(sig.signal)
    check(f"a) 실제 봇 == 플러그인 (신호·사유·대기 여부): {n_cases - bad}/{n_cases}", bad == 0, first)
    check("   커버리지: 대기 중 보유 판단에 STOP_LOSS·SELL·HOLD 가 모두 나오고, 대기 중 무보유 판단은 WAIT 뿐",
          held_in_window >= {"STOP_LOSS", "SELL", "HOLD"} and flat_in_window == {"WAIT"},
          f"보유={sorted(held_in_window)}, 무보유={sorted(flat_in_window)}")

    # b) 가상봇: 대기 중 보유(10주 @100) + 손절가(98) 이탈 → 시장가 청산 기록
    cfg_v = make_config("virtual_1", **WAIT_CFG)
    bot, fake = _virtual_cycle(NOW, cfg_v, _bars_until(NOW, flat100 + [95.0]), seed=(10, 100.0))
    st = bot.status_cache
    sl = [x for x in h.trades("VIRTUAL_1") if x["side"] == "STOP_LOSS"]
    check("b) [가상] 대기 중 보유 손절 이탈 → STOP_LOSS 시장가 청산 기록 (95.0 x 10), 보유 0",
          len(sl) == 1 and sl[0]["price"] == 95.0 and sl[0]["qty"] == 10 and not bot.virtual_broker.holdings.get("TEST")
          and st.get("signal") == "STOP_LOSS" and st.get("waiting_for_start") is True,
          f"sl={sl}, status={st.get('signal')}/{st.get('reason_code')}/{st.get('waiting_for_start')}")
    check("   [가상] 실거래 주문 호출 0회", len(fake.placed) == 0)

    # c) 가상봇: 대기 중 무보유 + VWAP 아래(평소라면 BUY) → 매수 주문 없음
    bot, _ = _virtual_cycle(NOW, cfg_v, _bars_until(NOW, flat100 + [98.0]))
    st = bot.status_cache
    check("c) [가상] 대기 중 무보유 BUY 차단 → 가상 미체결 0건, WAIT/WAIT_START_TIME",
          len(bot.virtual_broker.open_orders) == 0 and st.get("signal") == "WAIT"
          and st.get("reason_code") == "WAIT_START_TIME", f"open={bot.virtual_broker.open_orders}")
    bot_nw, _ = _virtual_cycle(NOW, make_config("virtual_1"), _bars_until(NOW, flat100 + [98.0]))
    check("   (대조) 같은 시세·대기 없음 설정에서는 가상 BUY 지정가 주문 1건",
          [o["side"] for o in bot_nw.virtual_broker.open_orders] == ["BUY"])

    # d) 가상봇: 대기 중 보유 + VWAP 위 → 지정가 매도 주문
    bot, _ = _virtual_cycle(NOW, cfg_v, _bars_until(NOW, flat100 + [101.0]), seed=(10, 100.0))
    check("d) [가상] 대기 중 보유 + VWAP 위 → SELL 지정가 주문 (10주)",
          [(o["side"], o["qty"]) for o in bot.virtual_broker.open_orders] == [("SELL", 10)]
          and bot.status_cache.get("signal") == "SELL", str(bot.virtual_broker.open_orders))

    # e) REAL mock: 대기 중 보유 + 손절 이탈 + 기존 미체결 매도 → 매도 취소 확인 후 시장가 손절
    cfg_r = make_config("real", initial_balance=1000.0, **WAIT_CFG)
    old_sell = {"order_id": "OLD_SELL", "ticker": "TEST", "side": "SELL", "price": 101.0, "qty": 10.0, "created_at": 0}
    det = lambda: {"OLD_SELL": [h.detail("PENDING_CANCEL", 0, None, side="SELL"),  # noqa: E731  (호출마다 새 응답 시퀀스)
                                h.detail("CANCELED", 0, None, side="SELL")],
                   "real_1": h.detail("FILLED", 10, 94.9, side="SELL")}
    bot, fake = _real_cycle(NOW, cfg_r, _bars_until(NOW, flat100 + [95.0]), hold, [old_sell], det(), track=[old_sell])
    t = h.trades("REAL")
    check("e) [REAL] 대기 중 보유 손절 이탈 → 기존 매도 취소 → 시장가 매도 10주 → STOP_LOSS @94.9 기록",
          fake.canceled == ["OLD_SELL"] and [(p["side"], p["order_type"], p["qty"]) for p in fake.placed] == [("SELL", "MARKET", 10)]
          and len(t) == 1 and t[0]["side"] == "STOP_LOSS" and t[0]["price"] == 94.9
          and bot.status_cache.get("waiting_for_start") is True,
          f"canceled={fake.canceled}, placed={fake.placed}, trades={t}")
    actions_wait = (list(fake.canceled), [(p["side"], p["order_type"], p["qty"]) for p in fake.placed])
    _, fake_nw = _real_cycle(NOW, make_config("real", initial_balance=1000.0), _bars_until(NOW, flat100 + [95.0]),
                             hold, [old_sell], det(), track=[old_sell])
    check("   (대조) 대기 없음 설정과 주문 동작 동일 (보유 중에는 대기가 손절에 영향 없음)",
          actions_wait == (fake_nw.canceled, [(p["side"], p["order_type"], p["qty"]) for p in fake_nw.placed]),
          f"{actions_wait} vs {(fake_nw.canceled, fake_nw.placed)}")

    # f) REAL mock: 대기 중 + 미체결 조회 실패 + 손절 이탈 → 추적 중 매도 취소 후 시장가 손절 (조회 실패여도 손절 우선)
    bot, fake = _real_cycle(NOW, cfg_r, _bars_until(NOW, flat100 + [95.0]), hold, [], det(),
                            open_orders_fail=True, track=[old_sell])
    check("f) [REAL] 대기 중 + 미체결 조회 실패에도 손절 진행 (추적 매도 취소 → 시장가)",
          fake.canceled == ["OLD_SELL"] and [(p["side"], p["order_type"]) for p in fake.placed] == [("SELL", "MARKET")]
          and len(h.trades("REAL")) == 1, f"canceled={fake.canceled}, placed={fake.placed}")

    # g) REAL mock: 대기 중 무보유 + VWAP 아래 + 남아 있던 매수 미체결 → 신규 매수 없음, 기존 매수 취소(9-4)
    stale_buy = {"order_id": "OLD_BUY", "ticker": "TEST", "side": "BUY", "price": 97.0, "qty": 1.0, "created_at": 0}
    bot, fake = _real_cycle(NOW, cfg_r, _bars_until(NOW, flat100 + [98.0]), {}, [stale_buy])
    check("g) [REAL] 대기 중 무보유 BUY 차단 → 신규 주문 0건, 남은 매수 미체결 취소, WAIT_START_TIME",
          fake.placed == [] and fake.canceled == ["OLD_BUY"] and bot.status_cache.get("reason_code") == "WAIT_START_TIME",
          f"placed={fake.placed}, canceled={fake.canceled}")

    # h) REAL mock: 대기 중 보유 + VWAP 위(SELL) + 부분체결 잔량 매수 미체결 → 매수 잔량 취소(9-2-0) + 지정가 매도
    bot, fake = _real_cycle(NOW, cfg_r, _bars_until(NOW, flat100 + [101.0]), hold, [stale_buy])
    check("h) [REAL] 대기 중 보유 SELL → 남은 매수 미체결 취소 + 지정가 매도 10주 제출",
          fake.canceled == ["OLD_BUY"] and [(p["side"], p["order_type"], p["qty"]) for p in fake.placed] == [("SELL", "LIMIT", 10)],
          f"canceled={fake.canceled}, placed={fake.placed}")

    # i) REAL mock: 대기 중 보유 + HOLD(손절가와 VWAP 사이) + 기존 매도 미체결 → 취소(9-4, 대기 없음과 동일)
    bot, fake = _real_cycle(NOW, cfg_r, _bars_until(NOW, flat100 + [99.0]), hold, [old_sell], track=[old_sell])
    _, fake_nw = _real_cycle(NOW, make_config("real", initial_balance=1000.0), _bars_until(NOW, flat100 + [99.0]),
                             hold, [old_sell], track=[old_sell])
    check("i) [REAL] 대기 중 보유 HOLD → 기존 매도 미체결 취소, 신규 주문 없음 (대기 없음 설정과 동일)",
          bot.status_cache.get("signal") == "HOLD" and fake.canceled == ["OLD_SELL"] and fake.placed == []
          and fake_nw.canceled == ["OLD_SELL"] and fake_nw.placed == [],
          f"signal={bot.status_cache.get('signal')}, canceled={fake.canceled}, placed={fake.placed}")


# ---------------------------------------------------------------------------
def perf_note():
    """참고 실측 (판정 아님): 1m 2만 봉 prepare + run_backtest 소요 시간."""
    bars = synth_bars(np.random.default_rng(9), datetime(2026, 9, 1, 0, 0), 20000, vol=0.2)
    ctx = ctx_of(market="US", ticker="TEST", reset="22:30", interval="1m", use_adx_filter=True, use_vwap_band=True)
    t0 = time.perf_counter()
    frame = build_engine_frame(S0, bars, ctx)
    t1 = time.perf_counter()
    res = run_backtest(frame, PluginEngineAdapter(S0, ctx), CostModel(), initial_equity=10_000.0, k_percent=10.0)
    t2 = time.perf_counter()
    print(f"\n  [INFO] 성능 참고(PC): 1m 20,000봉 prepare {t1 - t0:.2f}s + run_backtest {t2 - t1:.2f}s, 거래 {len(res.trades)}건")


def main():
    print("=" * 70)
    print(" VWAP 3단계 SR-1 — 전략 플러그인 동치/인과성 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    tests = [test_context_from_config, test_p1_signal_equivalence, test_p2_causality,
             test_p3_engine_equivalence, test_p4_start_rule, test_p5_start_wait_protection, perf_note]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    print("\nT-G. 운영 데이터 보호")
    check("운영 data/ 디렉터리 파일 변경 없음 (테스트 전후 해시 동일)", h._hash_dir(h.REAL_DATA_DIR) == h.REAL_DATA_HASH_BEFORE)
    new_logs = set(glob.glob(os.path.join(PROJECT_ROOT, "trading_bot_*.log"))) - LOGS_BEFORE
    check("trading_bot_*.log 신규 생성 없음", not new_logs, str(sorted(new_logs)))

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
