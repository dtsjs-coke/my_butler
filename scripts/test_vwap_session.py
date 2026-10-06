"""
VWAP 세션 경계 수정(ADR-0008) 검증 스크립트 — 네트워크 없이 실행됩니다.

검증 항목
  1. 미국 서머타임 판정 / 개장 시각(KST) — 2026-11-01 종료, 2027-03-14 시작 경계 전후
  2. 세션 규칙 선택(for_market) — 미국+22:30/23:30 → 자동, 그 외 → 설정값 고정(하위 호환)
  3. 현재 세션 [시작, 끝) / 세션 날짜(session_key) — DST 경계, 주말, 23h/25h 세션
  4. calculate_vwap
     4-1. KST 자정을 넘겨도 VWAP 누적이 이어짐 (F1 재현값 103.0 vs 기존 106.0)
     4-2. 서머타임 종료 후 23:30 에 새 세션 시작, 22:30~23:29 봉은 직전 세션에 이어 붙음 (F2)
     4-3. 정규장 밖 봉(프리/애프터/주간거래)은 '제외되지 않고' 누적됨 — 기존 동작 유지 확인
     4-4. 하위 호환: 자정을 넘지 않는 데이터는 기존(배포본) 알고리즘과 수치 동일 (고정 리셋 모드 / 국내 종목)
     4-5. tz-aware 시각(UTC 'Z', +09:00) 입력 → KST 로 변환되어 naive KST 입력과 결과 동일
  5. bars_needed — 세션 시작부터의 봉 수 + 여유, 최소 150 / 최대 1600
  6. TossBroker.get_candles 페이징 (requests mock) — nextBefore 이어받기, nextBefore 없음, before 무시, 2페이지 실패
  7. 봇 통합 (시각 고정, FakeTossBroker)
     7-1. 자정을 넘겨도 일 손실한도 기준자산(세션 날짜) 유지, 서머타임 종료 후 23:30 에 새 세션
     7-2. 요청 봉 수 = 세션 시작부터 + 여유
     7-3. 거래 시작 시각(start_time) — 서머타임 중 대기 동작 유지, 종료 후 '하루 대기' 갇힘 없음
  8. 운영 data/ 해시 전후 동일

실행:  python scripts/test_vwap_session.py
"""
import os
import sys
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

for _name in ["vwap_bot_virtual_1", "vwap_bot_virtual_2", "vwap_bot_virtual_3", "vwap_bot_real", "vwap_bot_virtual"]:
    _lg = logging.getLogger(_name)
    _lg.setLevel(logging.WARNING)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("      [bot-log] %(levelname)s %(message)s"))
    _lg.addHandler(_h)
    _lg.propagate = False
logging.getLogger("vwap_bot").setLevel(logging.ERROR)

# 1단계 하네스 재사용: import 시점에 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + Discord 전송 no-op
import test_vwap_reliability as h  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.vwap import session as sess_mod  # noqa: E402
from core.vwap.session import SessionSpec, MODE_US_AUTO, MODE_RESET  # noqa: E402
from core.vwap.strategy import VwapStrategy  # noqa: E402
import core.vwap.broker as broker_module  # noqa: E402
import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.bot import VWAPBot  # noqa: E402

check = h.check
FakeTossBroker = h.FakeTossBroker
make_config = h.make_config
Patched = h.Patched


# ---------------------------------------------------------------------------
# 도우미
# ---------------------------------------------------------------------------
def candles_between(start, end, closes=None, step_min=1, volume=1000.0):
    """[start, end] 1분(기본) 간격 봉. closes 미지정 시 100 부근의 결정적 값."""
    times = []
    t = start
    while t <= end:
        times.append(t)
        t += timedelta(minutes=step_min)
    n = len(times)
    if closes is None:
        rng = np.random.default_rng(7)
        closes = list(100.0 + np.cumsum(rng.normal(0, 0.05, n)))
    vols = [volume * (1 + (i % 5)) for i in range(n)]
    return pd.DataFrame({"time": times, "open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": vols})


def old_calculate_vwap(df, reset_time_str="22:30"):
    """배포본(2026-10-05 22:10, 수정 전) calculate_vwap 의 VWAP/stdev 부분을 그대로 옮긴 비교 기준."""
    df = df.copy()
    if not pd.api.types.is_datetime64_any_dtype(df['time']):
        df['time'] = pd.to_datetime(df['time'])
    df = df.sort_values('time').reset_index(drop=True)
    df['price_vol'] = df['close'] * df['volume']
    session_ids, current = [], 0
    try:
        reset_h, reset_m = map(int, reset_time_str.split(':'))
    except Exception:
        reset_h, reset_m = 22, 30
    for i, row in df.iterrows():
        t = row['time']
        if i > 0:
            prev_t = df.loc[i - 1, 'time']
            if t.date() != prev_t.date():
                current += 1
            else:
                boundary = t.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
                if prev_t < boundary <= t:
                    current += 1
        session_ids.append(current)
    df['session_id'] = session_ids
    df['cum_pv'] = df.groupby('session_id')['price_vol'].cumsum()
    df['cum_vol'] = df.groupby('session_id')['volume'].cumsum()
    df['vwap'] = np.where(df['cum_vol'] > 0, df['cum_pv'] / df['cum_vol'], df['close'])
    df['d'] = df['volume'] * ((df['close'] - df['vwap']) ** 2)
    df['cd'] = df.groupby('session_id')['d'].cumsum()
    df['vwap_stdev'] = np.sqrt(np.where(df['cum_vol'] > 0, df['cd'] / df['cum_vol'], 0))
    return df


def manual_vwap(df_part):
    return float((df_part['close'] * df_part['volume']).sum() / df_part['volume'].sum())


class FixedClock:
    """bot 모듈의 datetime.now() 를 고정 시각으로 바꿉니다."""

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


US = SessionSpec.for_market("US", "22:30")


# ---------------------------------------------------------------------------
def test_dst_helpers():
    print("\n1. [DST] 미국 서머타임 판정 / 개장 시각(KST)")
    cases = [
        (date(2026, 10, 30), True, "22:30"),   # 금, 서머타임 마지막 거래일
        (date(2026, 10, 31), True, "22:30"),   # 토
        (date(2026, 11, 1), False, "23:30"),   # 일, 전환일(11월 첫째 일요일)
        (date(2026, 11, 2), False, "23:30"),   # 월, 표준시 첫 거래일
        (date(2027, 3, 13), False, "23:30"),   # 토
        (date(2027, 3, 14), True, "22:30"),    # 일, 시작일(3월 둘째 일요일)
        (date(2026, 3, 8), True, "22:30"),
        (date(2026, 3, 7), False, "23:30"),
    ]
    bad = [(d, sess_mod.us_is_dst_on_date(d), sess_mod.us_open_kst_on(d).strftime("%H:%M"))
           for d, dst, op in cases
           if sess_mod.us_is_dst_on_date(d) != dst or sess_mod.us_open_kst_on(d).strftime("%H:%M") != op]
    check("DST 판정/개장 시각 8개 날짜", not bad, str(bad))

    # zoneinfo 가 있는 환경이면 2026~2030 전체 날짜를 교차검증 (없으면 생략 — Termux 는 tzdata 가 없을 수 있음)
    try:
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
        mism = []
        d = date(2026, 1, 1)
        while d <= date(2030, 12, 31):
            off = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ny).utcoffset()
            if (off == timedelta(hours=-4)) != sess_mod.us_is_dst_on_date(d):
                mism.append(d)
            d += timedelta(days=1)
        check("zoneinfo(America/New_York) 와 2026~2030 전 날짜 일치", not mism, f"불일치 {len(mism)}일")
    except Exception as e:
        print(f"  [SKIP] zoneinfo 교차검증 생략: {type(e).__name__}")


def test_for_market():
    print("\n2. [규칙 선택] for_market — 미국+22:30/23:30 자동, 그 외 고정(하위 호환)")
    check("US + 22:30 → US_AUTO", SessionSpec.for_market("US", "22:30").mode == MODE_US_AUTO)
    check("US + 23:30 → US_AUTO", SessionSpec.for_market("US", "23:30").mode == MODE_US_AUTO)
    s = SessionSpec.for_market("US", "21:00")
    check("US + 21:00(사용자 지정) → RESET_TIME 21:00 고정", s.mode == MODE_RESET and s.reset_time == "21:00")
    s = SessionSpec.for_market("KR", "22:30")
    check("KR + 22:30 → RESET_TIME (국내는 자동 전환 없음)", s.mode == MODE_RESET and s.reset_time == "22:30")
    check("market 미지정 + 6자리 숫자 티커 → RESET_TIME", SessionSpec.for_market(None, "22:30", "005930").mode == MODE_RESET)
    check("market 미지정 + 영문 티커 → US_AUTO", SessionSpec.for_market(None, "22:30", "AAPL").mode == MODE_US_AUTO)
    s = SessionSpec.for_market("US", "abc")
    check("잘못된 reset_time → 고정 22:30 폴백(기존 calculate_vwap 과 동일)", s.mode == MODE_RESET and s.reset_time == "22:30")


def test_bounds():
    print("\n3. [세션 경계] current_bounds / session_key")
    D = datetime
    cases = [
        # (now, expected_start, expected_end)
        (D(2026, 10, 5, 23, 10), D(2026, 10, 5, 22, 30), D(2026, 10, 6, 22, 30)),
        (D(2026, 10, 6, 1, 0), D(2026, 10, 5, 22, 30), D(2026, 10, 6, 22, 30)),     # 자정 넘김 → 같은 세션
        (D(2026, 10, 6, 22, 29), D(2026, 10, 5, 22, 30), D(2026, 10, 6, 22, 30)),
        (D(2026, 10, 30, 23, 0), D(2026, 10, 30, 22, 30), D(2026, 10, 31, 22, 30)),  # 금, DST
        (D(2026, 11, 1, 22, 45), D(2026, 10, 31, 22, 30), D(2026, 11, 1, 23, 30)),  # 전환일: 25시간 세션
        (D(2026, 11, 2, 23, 0), D(2026, 11, 1, 23, 30), D(2026, 11, 2, 23, 30)),    # 표준시: 23:00 은 아직 직전 세션
        (D(2026, 11, 2, 23, 45), D(2026, 11, 2, 23, 30), D(2026, 11, 3, 23, 30)),
        (D(2026, 11, 3, 0, 30), D(2026, 11, 2, 23, 30), D(2026, 11, 3, 23, 30)),
        (D(2027, 3, 14, 22, 0), D(2027, 3, 13, 23, 30), D(2027, 3, 14, 22, 30)),    # 시작일: 23시간 세션
        (D(2027, 3, 14, 22, 40), D(2027, 3, 14, 22, 30), D(2027, 3, 15, 22, 30)),
    ]
    bad = [(n, US.current_bounds(n)) for n, s, e in cases if US.current_bounds(n) != (s, e)]
    check("US_AUTO 경계 10개 시각 (DST 종료/시작, 23h/25h 세션)", not bad, str(bad))
    check("session_key: 10/06 01:00 → 2026-10-05 (자정 넘김)", US.session_key(D(2026, 10, 6, 1, 0)) == "2026-10-05")
    check("session_key: 11/02 23:10(표준시, 개장 전) → 2026-11-01", US.session_key(D(2026, 11, 2, 23, 10)) == "2026-11-01")
    check("session_key: 11/02 23:40 → 2026-11-02", US.session_key(D(2026, 11, 2, 23, 40)) == "2026-11-02")

    # 고정 모드는 기존 bot.get_session_start/get_session_date 와 48시간 x 5개 리셋 격자에서 완전히 동일
    mism = 0
    for reset in ["22:30", "23:30", "09:00", "00:00", "21:15"]:
        spec = SessionSpec.reset_time_mode(reset)
        t = D(2026, 10, 5, 0, 0)
        while t < D(2026, 10, 7, 0, 0):
            if spec.session_start(t) != bot_module.get_session_start(t, reset) or \
                    spec.session_key(t) != bot_module.get_session_date(t, reset):
                mism += 1
            t += timedelta(minutes=15)
    check("RESET_TIME 모드 == 기존 get_session_start/get_session_date (5 리셋 x 48h)", mism == 0, f"불일치 {mism}")

    # label_bars(벡터) == current_bounds(스칼라) 일치 — DST 종료 주간 1분 격자
    times = pd.Series(pd.date_range("2026-10-30 20:00", "2026-11-03 02:00", freq="1min"))
    lab = US.label_bars(times)
    scal = pd.Series([US.session_start(t.to_pydatetime()) for t in times])
    check("label_bars == session_start (DST 종료 주간 1분 격자 전체)", (lab.values == pd.to_datetime(scal).values).all())


def test_calculate_vwap():
    print("\n4. [calculate_vwap] 자정 연속성 / DST / 정규장 밖 봉 / 하위 호환 / 시간대")
    D = datetime
    # 4-1. 설계문서 §8 F1 재현: 23:59(100, 1주) → 00:00(106, 1주). 기대 103.0, 기존 106.0
    df = pd.DataFrame({"time": [D(2026, 10, 5, 23, 59), D(2026, 10, 6, 0, 0)],
                       "open": [100.0, 106.0], "high": [100.0, 106.0], "low": [100.0, 106.0],
                       "close": [100.0, 106.0], "volume": [1.0, 1.0]})
    new = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    old = old_calculate_vwap(df, "22:30")
    check("F1 재현: 00:00 봉 VWAP 새 코드 103.0 (기존 106.0)",
          abs(new['vwap'].iloc[-1] - 103.0) < 1e-9 and abs(old['vwap'].iloc[-1] - 106.0) < 1e-9,
          f"new={new['vwap'].iloc[-1]}, old={old['vwap'].iloc[-1]}")
    new_fixed = VwapStrategy.calculate_vwap(df, "22:30")  # session 미지정(고정 모드)도 자정에 끊지 않음
    check("session 미지정(고정 22:30)도 자정에 끊지 않음", abs(new_fixed['vwap'].iloc[-1] - 103.0) < 1e-9)

    # 22:30 ~ 02:00 연속 1분봉: 모든 봉의 VWAP = 22:30 부터의 누적과 일치
    df = candles_between(D(2026, 10, 5, 22, 30), D(2026, 10, 6, 2, 0))
    out = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    exp = (df['close'] * df['volume']).cumsum() / df['volume'].cumsum()
    check("22:30~02:00 VWAP = 22:30 부터 단일 누적 (최대오차 < 1e-9)",
          float(np.abs(out['vwap'].values - exp.values).max()) < 1e-9)
    i0 = int(out.index[out['time'] == D(2026, 10, 6, 0, 0)][0])
    check("00:00 봉 전후로 VWAP 이 연속 (00:00 VWAP != 00:00 종가)",
          abs(out['vwap'].iloc[i0] - out['close'].iloc[i0]) > 1e-6 and abs(out['vwap'].iloc[i0] - exp.iloc[i0]) < 1e-9)
    # stdev 도 자정에 리셋되지 않음 (같은 수식, 단일 그룹)
    d2 = df['volume'] * (df['close'] - exp) ** 2
    exp_sd = np.sqrt(d2.cumsum() / df['volume'].cumsum())
    check("Dev Band 표준편차도 자정 연속 (최대오차 < 1e-9)", float(np.abs(out['vwap_stdev'].values - exp_sd.values).max()) < 1e-9)

    # 4-2. 서머타임 종료 후: 11/02(월) 22:00~23:59. 23:30 봉에서 새 세션
    df = candles_between(D(2026, 11, 2, 22, 0), D(2026, 11, 2, 23, 59))
    out = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    j = int(out.index[out['time'] == D(2026, 11, 2, 23, 30)][0])
    check("표준시: 23:30 봉이 새 세션 첫 봉 (VWAP == 종가)", abs(out['vwap'].iloc[j] - out['close'].iloc[j]) < 1e-12)
    check("표준시: 22:30 봉은 새 세션이 아님 (기존 고정 22:30 이면 여기서 리셋됨)",
          out['session_start'].iloc[int(out.index[out['time'] == D(2026, 11, 2, 22, 30)][0])] == pd.Timestamp(2026, 11, 1, 23, 30))
    check("표준시: 23:31~23:59 VWAP = 23:30 부터 누적",
          abs(out['vwap'].iloc[-1] - manual_vwap(df.iloc[j:])) < 1e-9)
    old = old_calculate_vwap(df, "22:30")
    k = int(old.index[old['time'] == D(2026, 11, 2, 22, 30)][0])
    check("(대조) 기존 코드는 표준시에도 22:30 에 리셋", abs(old['vwap'].iloc[k] - old['close'].iloc[k]) < 1e-12)
    # 서머타임 중 금요일: 22:30 에 새 세션
    df = candles_between(D(2026, 10, 30, 22, 0), D(2026, 10, 30, 23, 0))
    out = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    j = int(out.index[out['time'] == D(2026, 10, 30, 22, 30)][0])
    check("서머타임: 22:30 봉이 새 세션 첫 봉", abs(out['vwap'].iloc[j] - out['close'].iloc[j]) < 1e-12
          and out['session_start'].iloc[j] == pd.Timestamp(2026, 10, 30, 22, 30))

    # 4-3. 정규장 밖 봉은 제외하지 않음: 서머타임 10/06 05:00~22:29(애프터·주간·프리) 는 10/05 22:30 세션에 누적
    df = candles_between(D(2026, 10, 5, 22, 30), D(2026, 10, 6, 22, 29), step_min=5)
    out = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    check("정규장 밖 봉도 VWAP 누적에 포함 (22:29 VWAP = 직전 22:30 부터 전체 누적)",
          abs(out['vwap'].iloc[-1] - manual_vwap(df)) < 1e-9 and out['session_start'].nunique() == 1)

    # 4-4. 하위 호환: 자정을 넘지 않는 데이터는 기존 알고리즘과 동일
    df = candles_between(D(2026, 10, 5, 10, 0), D(2026, 10, 5, 23, 59))   # 22:30 리셋을 가로지름, 자정 미포함
    a = VwapStrategy.calculate_vwap(df, "22:30")
    b = old_calculate_vwap(df, "22:30")
    check("고정 22:30: 자정 미포함 데이터 → 기존과 VWAP/stdev 동일",
          float(np.abs(a['vwap'].values - b['vwap'].values).max()) < 1e-9
          and float(np.abs(a['vwap_stdev'].values - b['vwap_stdev'].values).max()) < 1e-9)
    a = VwapStrategy.calculate_vwap(df, "22:30", session=US)   # 10/05 은 서머타임 → 22:30 리셋, 기존과 동일
    check("US_AUTO(서머타임 기간): 자정 미포함 데이터 → 기존 22:30 고정과 동일",
          float(np.abs(a['vwap'].values - b['vwap'].values).max()) < 1e-9)
    # 국내 종목: 3거래일 09:00~15:30 (장중 데이터는 자정을 포함하지 않음) → 기존과 동일
    parts = [candles_between(D(2026, 10, d, 9, 0), D(2026, 10, d, 15, 30)) for d in (5, 6, 7)]
    df = pd.concat(parts, ignore_index=True)
    kr = SessionSpec.for_market("KR", "09:00", "005930")
    a = VwapStrategy.calculate_vwap(df, "09:00", session=kr)
    b = old_calculate_vwap(df, "09:00")
    check("국내(고정 09:00) 3거래일 → 기존과 VWAP 동일, 날마다 09:00 리셋",
          float(np.abs(a['vwap'].values - b['vwap'].values).max()) < 1e-9 and a['session_start'].nunique() == 3)
    check("반환 컬럼: 기존 컬럼(vwap, vwap_stdev, rsi, adx) 유지 + session_start, 임시 컬럼 없음",
          {"vwap", "vwap_stdev", "rsi", "adx", "session_start"} <= set(a.columns)
          and not ({"price_vol", "cum_pv", "cum_vol", "session_id"} & set(a.columns)))
    check("빈 DataFrame → 그대로 반환", VwapStrategy.calculate_vwap(df.iloc[0:0], "22:30", session=US).empty)

    # 4-5. tz-aware 입력: UTC('Z') / +09:00 문자열 → KST 로 변환 후 동일 결과
    df = candles_between(D(2026, 10, 5, 23, 0), D(2026, 10, 6, 1, 0))
    base = VwapStrategy.calculate_vwap(df, "22:30", session=US)
    df_utc = df.copy()
    df_utc['time'] = [(t - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ") for t in df['time']]
    df_kst = df.copy()
    df_kst['time'] = [t.strftime("%Y-%m-%dT%H:%M:%S+09:00") for t in df['time']]
    o1 = VwapStrategy.calculate_vwap(df_utc, "22:30", session=US)
    o2 = VwapStrategy.calculate_vwap(df_kst, "22:30", session=US)
    check("UTC 'Z' 문자열 입력 → KST naive 로 변환, VWAP 동일",
          (o1['time'].values == base['time'].values).all() and float(np.abs(o1['vwap'] - base['vwap']).max()) < 1e-12)
    check("+09:00 문자열 입력 → 동일", (o2['time'].values == base['time'].values).all()
          and float(np.abs(o2['vwap'] - base['vwap']).max()) < 1e-12)
    df_aware = df.copy()
    df_aware['time'] = pd.to_datetime(df_utc['time'])   # datetime64[ns, UTC]
    o3 = VwapStrategy.calculate_vwap(df_aware, "22:30", session=US)
    check("datetime64[UTC] 입력 → 동일", float(np.abs(o3['vwap'] - base['vwap']).max()) < 1e-12
          and o3['time'].dt.tz is None)


def test_bars_needed():
    print("\n5. [bars_needed] 세션 시작부터 + 여유(30), 최소 150 / 최대 1600")
    D = datetime
    c = [
        (US.bars_needed(D(2026, 10, 5, 23, 30), "1m"), 150),     # 60분+30 → 최소 150
        (US.bars_needed(D(2026, 10, 6, 3, 30), "1m"), 330),      # 300분+30
        (US.bars_needed(D(2026, 10, 6, 21, 30), "1m"), 1410),    # 23시간
        (US.bars_needed(D(2026, 11, 1, 23, 0), "1m"), 1500),     # 25시간 세션(10/31 22:30~11/01 23:30) 24.5h+30
        (US.bars_needed(D(2026, 11, 1, 23, 0), "1m", cap=1000), 1000),  # 상한 적용
        (US.bars_needed(D(2026, 10, 6, 3, 30), "5m"), 150),      # 60봉+30 → 최소 150
        (US.bars_needed(D(2026, 10, 6, 21, 30), "5m"), 306),     # 276+30
    ]
    check("bars_needed 7개 케이스", all(a == b for a, b in c), str(c))


# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


def _toss_page(end_time, n, next_before=True):
    """end_time 부터 과거로 n 개(최신순) 토스 형식 봉. timestamp 는 +09:00 ISO 문자열."""
    cs = []
    for i in range(n):
        t = end_time - timedelta(minutes=i)
        cs.append({"timestamp": t.strftime("%Y-%m-%dT%H:%M:%S+09:00"), "openPrice": "100", "highPrice": "100",
                   "lowPrice": "100", "closePrice": "100", "volume": "10"})
    res = {"candles": cs}
    if next_before and n:
        res["nextBefore"] = cs[-1]["timestamp"]   # inclusive 가정 → 경계 봉 중복은 drop_duplicates 로 제거
    return {"result": res}


class _FakeRequests:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        return self.handler(len(self.calls), dict(params or {}))

    def post(self, *a, **k):
        raise AssertionError("토큰 발급 호출 금지(테스트)")


def _toss_broker():
    b = broker_module.TossBroker("id", "secret", "1")
    b.access_token = "t"
    b.token_expiry = 4102444800.0  # 2100년 — _ensure_token 네트워크 호출 방지
    b.mock_mode = False
    return b


def test_broker_paging():
    print("\n6. [TossBroker.get_candles] 페이징 (requests mock, 네트워크 없음)")
    orig_req = broker_module.requests
    orig_yahoo = broker_module.TossBroker._fetch_yahoo_candles
    yahoo_calls = []
    broker_module.TossBroker._fetch_yahoo_candles = lambda self, *a, **k: (yahoo_calls.append(a), pd.DataFrame())[1]
    end = datetime(2026, 10, 6, 3, 0)
    try:
        # (a) nextBefore 로 이어받기: 450봉 요청 → 200 + 200 + 50, 경계 중복 제거
        def h_ok(i, p):
            cur = end if "before" not in p else datetime.strptime(p["before"][:19], "%Y-%m-%dT%H:%M:%S")
            return _Resp(200, _toss_page(cur, p["count"]))
        fr = _FakeRequests(h_ok)
        broker_module.requests = fr
        b = _toss_broker()
        broker_module.TossBroker._candle_probe_logged = False
        df = b.get_candles("AAPL", "1m", 450)
        check("(a) 450봉 요청 → 3회 호출, count 200/200/50, 2·3회차에 before 전달",
              [c["count"] for c in fr.calls] == [200, 200, 50] and "before" not in fr.calls[0]
              and all("before" in c for c in fr.calls[1:]), str(fr.calls))
        check("(a) 결과 오름차순·중복 없음·최신 봉 포함, source=toss, complete=True",
              df['time'].is_monotonic_increasing and df['time'].is_unique and len(df) == 448
              and b.last_candles_source == "toss" and b.last_candles_complete is True, f"len={len(df)}")
        check("(a) 캔들 timestamp 확인 로그 1회 플래그 설정", broker_module.TossBroker._candle_probe_logged is True)

        # (b) nextBefore 없음 → 첫 페이지만 (기존과 비슷한 부분 데이터로 안전 동작)
        fr = _FakeRequests(lambda i, p: _Resp(200, _toss_page(end, p["count"], next_before=False)))
        broker_module.requests = fr
        df = _toss_broker().get_candles("AAPL", "1m", 450)
        check("(b) nextBefore 없음 → 1회 호출, 200봉 반환", len(fr.calls) == 1 and len(df) == 200)

        # (c) before 를 무시(항상 같은 최신 페이지) → 2회차에서 '더 과거 봉 없음' 감지 후 중단
        fr = _FakeRequests(lambda i, p: _Resp(200, _toss_page(end, p["count"])))
        broker_module.requests = fr
        df = _toss_broker().get_candles("AAPL", "1m", 1000)
        check("(c) before 무시 API → 2회 호출 후 중단, 200봉", len(fr.calls) == 2 and len(df) == 200, f"calls={len(fr.calls)}")

        # (d) 2페이지 HTTP 500 → 받은 200봉 반환, complete=False, Yahoo 폴백 안 함
        yahoo_calls.clear()
        fr = _FakeRequests(lambda i, p: _Resp(200, _toss_page(end, p["count"])) if i == 1 else _Resp(500, {"e": 1}))
        broker_module.requests = fr
        b = _toss_broker()
        df = b.get_candles("AAPL", "1m", 450)
        check("(d) 2페이지 실패 → 200봉 반환, complete=False, Yahoo 폴백 없음",
              len(df) == 200 and b.last_candles_complete is False and not yahoo_calls)

        # (e) 2페이지 예외(타임아웃) → 받은 200봉 반환 (예전 코드였다면 전체를 버리고 Yahoo 폴백)
        def h_exc(i, p):
            if i == 1:
                return _Resp(200, _toss_page(end, p["count"]))
            raise TimeoutError("timeout")
        fr = _FakeRequests(h_exc)
        broker_module.requests = fr
        b = _toss_broker()
        df = b.get_candles("AAPL", "1m", 450)
        check("(e) 2페이지 예외 → 200봉 반환, complete=False, Yahoo 폴백 없음",
              len(df) == 200 and b.last_candles_complete is False and not yahoo_calls)

        # (e2) 2페이지 응답 JSON 파싱 예외 → 받은 200봉 반환 (QA 경미 이슈 #1)
        class _BadJsonResp(_Resp):
            def json(self):
                raise ValueError("bad json")
        yahoo_calls.clear()
        fr = _FakeRequests(lambda i, p: _Resp(200, _toss_page(end, p["count"])) if i == 1 else _BadJsonResp(200, {"x": 1}))
        broker_module.requests = fr
        b = _toss_broker()
        df = b.get_candles("AAPL", "1m", 450)
        check("(e2) 2페이지 JSON 파싱 예외 → 200봉 반환, complete=False, Yahoo 폴백 없음",
              len(df) == 200 and b.last_candles_complete is False and b.last_candles_source == "toss" and not yahoo_calls,
              f"len={len(df)}, yahoo={len(yahoo_calls)}")

        # (e3) 2페이지 봉 필드 None(closePrice) → 받은 200봉 반환
        def h_nullfield(i, p):
            cur = end if "before" not in p else datetime.strptime(p["before"][:19], "%Y-%m-%dT%H:%M:%S")
            pg = _toss_page(cur, p["count"])
            if i > 1:
                pg["result"]["candles"][3]["closePrice"] = None
            return _Resp(200, pg)
        yahoo_calls.clear()
        fr = _FakeRequests(h_nullfield)
        broker_module.requests = fr
        b = _toss_broker()
        df = b.get_candles("AAPL", "1m", 450)
        check("(e3) 2페이지 봉 필드 None → 200봉 반환, complete=False, Yahoo 폴백 없음",
              len(df) == 200 and b.last_candles_complete is False and b.last_candles_source == "toss" and not yahoo_calls,
              f"len={len(df)}, yahoo={len(yahoo_calls)}")

        # (f) 첫 페이지 실패 → 기존처럼 Yahoo 폴백
        fr = _FakeRequests(lambda i, p: _Resp(500, {"e": 1}))
        broker_module.requests = fr
        _toss_broker().get_candles("AAPL", "1m", 150)
        check("(f) 첫 페이지 HTTP 500 → Yahoo 폴백 시도(기존 동작)", len(yahoo_calls) == 1)

        # (g) 150봉 이하 요청은 1회 호출(count=요청 수) — 예전과 같은 호출 형태
        fr = _FakeRequests(lambda i, p: _Resp(200, _toss_page(end, p["count"])))
        broker_module.requests = fr
        df = _toss_broker().get_candles("AAPL", "1m", 150)
        check("(g) 150봉 요청 → 1회 호출 count=150, before 없음",
              len(fr.calls) == 1 and fr.calls[0] == {"symbol": "AAPL", "interval": "1m", "count": 150} and len(df) == 150)
    finally:
        broker_module.requests = orig_req
        broker_module.TossBroker._fetch_yahoo_candles = orig_yahoo


# ---------------------------------------------------------------------------
class RecordingFake(FakeTossBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.requested = []

    def get_candles(self, ticker, interval, limit):
        self.requested.append(limit)
        return self.candles.copy()


def _bot_cycle(now, config, fake, candles):
    fake.candles = candles
    with FixedClock(now), Patched(config, fake):
        bot = VWAPBot("VIRTUAL_1")
        bot.running = True
        bot._loop_step()
        return bot


def test_bot_integration():
    print("\n7. [봇 통합] 손실한도 세션 날짜 / 요청 봉 수 / 거래 시작 시각 (시각 고정)")
    D = datetime
    flat = lambda end: candles_between(end - timedelta(minutes=40), end, closes=[100.0] * 41)  # noqa: E731

    # 7-1. 자정 넘김: 같은 봇 인스턴스로 23:50 → 00:10 → 기준자산 유지
    h.reset_data_dir()
    fake = RecordingFake("test_id", "test_secret", "1")
    cfg = make_config("virtual_1", max_daily_loss_limit=50.0)
    with Patched(cfg, fake):
        bot = VWAPBot("VIRTUAL_1"); bot.running = True
        for t in (D(2026, 10, 5, 23, 50), D(2026, 10, 6, 0, 10)):
            fake.candles = flat(t)
            with FixedClock(t):
                bot._loop_step()
            if t.hour == 23:
                first = bot.last_baseline_date
        check("자정 넘김: 세션 날짜 2026-10-05 유지 (기준자산 재설정 없음)",
              first == "2026-10-05" and bot.last_baseline_date == "2026-10-05", f"{first} -> {bot.last_baseline_date}")
        check("요청 봉 수: 23:50 → 80분+30=110, 00:10 → 100분+30=130 → 둘 다 최소 150",
              fake.requested == [150, 150], str(fake.requested))
        # 표준시: 11/02 23:10 → 23:40 사이에 새 세션
        dates = []
        for t in (D(2026, 11, 2, 23, 10), D(2026, 11, 2, 23, 40)):
            fake.candles = flat(t)
            with FixedClock(t):
                bot._loop_step()
            dates.append(bot.last_baseline_date)
        check("표준시: 23:10 은 2026-11-01 세션, 23:40 은 2026-11-02 세션 (23:30 경계)",
              dates == ["2026-11-01", "2026-11-02"], str(dates))

    # 7-2. 세션 진행 중 요청 봉 수 (03:30 → 300분 + 30)
    h.reset_data_dir()
    fake = RecordingFake("test_id", "test_secret", "1")
    _bot_cycle(D(2026, 10, 6, 3, 30), make_config("virtual_1"), fake, flat(D(2026, 10, 6, 3, 30)))
    check("03:30 요청 봉 수 = 330", fake.requested == [330], str(fake.requested))
    h.reset_data_dir()
    fake = RecordingFake("test_id", "test_secret", "1")
    _bot_cycle(D(2026, 10, 6, 3, 30), make_config("virtual_1", ticker="005930", market="KR", reset_time="09:00"),
               fake, flat(D(2026, 10, 6, 3, 30)))
    check("국내(고정 09:00) 03:30 요청 봉 수 = 18.5h → 1110+30 = 1140", fake.requested == [1140], str(fake.requested))

    # 7-3. 거래 시작 시각 — 서머타임 중: 리셋 22:30, 시작 23:00, 현재 22:45 → 대기
    h.reset_data_dir()
    bot = _bot_cycle(D(2026, 10, 30, 22, 45), make_config("virtual_1", start_time="23:00"),
                     RecordingFake("test_id", "test_secret", "1"), flat(D(2026, 10, 30, 22, 45)))
    st = bot.get_status()
    check("서머타임 22:45, 시작 23:00 → WAIT_START_TIME (기존 동작 유지)",
          st["reason_code"] == "WAIT_START_TIME" and st["waiting_for_start"] is True, st["reason_text"])
    # 표준시: 개장 23:30, 시작 23:00 (개장보다 30분 앞) → 23:40 에 다음날까지 대기하지 않음
    h.reset_data_dir()
    bot = _bot_cycle(D(2026, 11, 2, 23, 40), make_config("virtual_1", start_time="23:00"),
                     RecordingFake("test_id", "test_secret", "1"), flat(D(2026, 11, 2, 23, 40)))
    st = bot.get_status()
    check("표준시 23:40, 시작 23:00 → 대기 없음 (하루 대기 갇힘 방지)",
          st["reason_code"] != "WAIT_START_TIME" and st["waiting_for_start"] is False, st["reason_code"])
    # 표준시: 시작 00:30 → 23:40 은 대기 (일반 케이스는 그대로)
    h.reset_data_dir()
    bot = _bot_cycle(D(2026, 11, 2, 23, 40), make_config("virtual_1", start_time="00:30"),
                     RecordingFake("test_id", "test_secret", "1"), flat(D(2026, 11, 2, 23, 40)))
    st = bot.get_status()
    check("표준시 23:40, 시작 00:30 → WAIT_START_TIME", st["reason_code"] == "WAIT_START_TIME", st["reason_code"])
    # 고정 모드(사용자 지정 리셋 21:00)는 서머타임 보정 없이 기존 규칙 그대로: 시작 20:30 → 다음날 20:30 까지 대기
    h.reset_data_dir()
    bot = _bot_cycle(D(2026, 11, 2, 21, 10), make_config("virtual_1", reset_time="21:00", start_time="20:30"),
                     RecordingFake("test_id", "test_secret", "1"), flat(D(2026, 11, 2, 21, 10)))
    st = bot.get_status()
    check("고정 21:00, 시작 20:30 → 기존 규칙대로 대기(자동 보정은 US_AUTO 에만)",
          st["reason_code"] == "WAIT_START_TIME", st["reason_code"])
    check("status 에 session_label 노출 (관측 전용)", "21:00" in str(st.get("session_label", "")), str(st.get("session_label")))


def main():
    print("=" * 70)
    print(" VWAP 세션 경계(ADR-0008) 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    tests = [test_dst_helpers, test_for_market, test_bounds, test_calculate_vwap, test_bars_needed,
             test_broker_paging, test_bot_integration]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    after = h._hash_dir(h.REAL_DATA_DIR)
    check("운영 data/ 디렉터리 파일 변경 없음 (테스트 전후 해시 동일)", after == h.REAL_DATA_HASH_BEFORE)

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
