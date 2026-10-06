"""VWAP 세션 경계 계산 — 봇/전략/백테스터가 함께 쓰는 '단 하나의 기준' (ADR-0008).

[왜 필요한가]
기존 calculate_vwap 은
  (1) 달력 날짜가 바뀌면(KST 자정) 세션을 끊었고  -> 미국장 도중 00:00 에 VWAP/타겟/Dev Band 가 초기화 (F1)
  (2) 리셋 시각이 22:30 고정이었습니다          -> 서머타임이 끝나면(11월 첫째 일요일) 정규장 개장은 23:30 인데
                                                   개장 1시간 전(프리마켓)에 세션이 시작됨 (F2)

[세션 규칙] — 둘 다 "리셋 시각부터 다음 리셋 시각 직전까지"가 한 세션이고, 자정으로는 절대 끊지 않습니다.
- US_AUTO    : 미국 종목이고 reset_time 이 "22:30" 또는 "23:30"(= 미국 정규장 개장 시각의 두 가지 값)일 때.
               리셋 시각을 그날의 뉴욕 09:30 개장 시각(KST)으로 자동 결정합니다.
               서머타임(EDT) 기간 22:30, 표준시(EST) 기간 23:30. 서머타임 판정은 미국 규칙
               (2007~: 3월 둘째 일요일 ~ 11월 첫째 일요일)을 직접 계산합니다 — tzdata 가 없는 Termux 에서도 동일.
- RESET_TIME : 그 외(국내 종목, 또는 사용자가 22:30/23:30 이 아닌 리셋 시각을 직접 넣은 경우).
               설정된 reset_time(HH:MM, KST)을 매일 그대로 씁니다.

두 모드 모두 모든 봉(프리/애프터/주간거래 포함)을 VWAP 에 누적합니다 — 기존 동작(24시간 누적, 24시간 매매)과 같고,
정규장 밖 봉을 빼거나 매수를 막는 등의 매매 규칙 변경은 하지 않습니다. 바뀌는 것은 '세션이 언제 시작하나' 뿐입니다.

모든 시각은 'KST tz-naive datetime' 으로 다룹니다. tz-aware 입력은 to_kst_naive() 로 KST 로 변환하고,
tz 정보가 없는(naive) 입력은 이미 KST 라고 간주합니다(Yahoo 경로·기존 코드와 같은 가정).
"""
from datetime import datetime, timedelta, timezone, date, time as dtime

import pandas as pd

KST = timezone(timedelta(hours=9))

MODE_US_AUTO = "US_AUTO"
MODE_RESET = "RESET_TIME"

# 이 값으로 설정된 reset_time 은 '미국 정규장 개장'을 뜻하는 것으로 보고 서머타임 자동 전환 대상이 됩니다.
US_OPEN_KST_DST = "22:30"   # 서머타임(EDT, UTC-4) 기간 09:30 ET
US_OPEN_KST_STD = "23:30"   # 표준시(EST, UTC-5) 기간 09:30 ET
US_AUTO_RESET_VALUES = (US_OPEN_KST_DST, US_OPEN_KST_STD)


# ----------------------------------------------------------------------
# 시간대 유틸
# ----------------------------------------------------------------------
def to_kst_naive(series: pd.Series) -> pd.Series:
    """시각 시리즈를 KST tz-naive 로 변환. naive 입력은 이미 KST 로 간주."""
    s = series
    if not pd.api.types.is_datetime64_any_dtype(s):
        try:
            s = pd.to_datetime(s)
        except (ValueError, TypeError):
            # 서로 다른 오프셋이 섞인 문자열 등 → UTC 로 통일 후 아래에서 KST 변환
            s = pd.to_datetime(series, utc=True)
        if not pd.api.types.is_datetime64_any_dtype(s):  # 오프셋이 섞여 object 로 남은 경우
            s = pd.to_datetime(series, utc=True)
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert(KST).dt.tz_localize(None)
    return s


def to_kst_naive_dt(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(KST).replace(tzinfo=None)
    return dt


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """year-month 의 n번째 weekday(월=0..일=6) 날짜."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def us_is_dst_on_date(d: date) -> bool:
    """뉴욕 날짜 d 의 09:30 개장 시점이 서머타임(EDT)인지.
    전환은 일요일 02:00(현지)이라 09:30 과 겹치지 않으므로 날짜만으로 판정할 수 있습니다."""
    return _nth_weekday(d.year, 3, 6, 2) <= d < _nth_weekday(d.year, 11, 6, 1)


def us_open_kst_on(d: date) -> dtime:
    """KST 날짜 d 에 열리는 미국 정규장 개장 시각(KST).
    뉴욕 09:30 은 KST 로 같은 날짜의 22:30(EDT) 또는 23:30(EST) 이므로 KST 날짜 = 뉴욕 날짜."""
    return dtime(22, 30) if us_is_dst_on_date(d) else dtime(23, 30)


def interval_minutes(interval: str) -> int:
    try:
        if interval.endswith("m"):
            return max(1, int(interval[:-1]))
        if interval.endswith("h"):
            return int(interval[:-1]) * 60
        if interval.endswith("d"):
            return int(interval[:-1]) * 1440
    except Exception:
        pass
    return 1


def infer_market(ticker: str, market: str = None) -> str:
    m = str(market or "").upper()
    if m in ("US", "KR"):
        return m
    t = str(ticker or "").strip()
    return "KR" if (t.isdigit() and len(t) == 6) else "US"


def _parse_hhmm(value: str):
    try:
        h, m = map(int, str(value).strip().split(":"))
        return dtime(h, m)
    except Exception:
        return None


# ----------------------------------------------------------------------
# 세션 규칙
# ----------------------------------------------------------------------
class SessionSpec:
    def __init__(self, mode: str, reset_time: str = US_OPEN_KST_DST):
        self.mode = mode
        parsed = _parse_hhmm(reset_time)
        # 파싱 실패 시 기존 calculate_vwap 과 같은 22:30 폴백
        self._reset = parsed or dtime(22, 30)
        self.reset_time = self._reset.strftime("%H:%M")

    # --- 생성 ---
    @classmethod
    def us_auto(cls) -> "SessionSpec":
        return cls(MODE_US_AUTO)

    @classmethod
    def reset_time_mode(cls, reset_time: str) -> "SessionSpec":
        return cls(MODE_RESET, reset_time)

    @classmethod
    def for_market(cls, market: str, reset_time: str = US_OPEN_KST_DST, ticker: str = None) -> "SessionSpec":
        """미국 종목 + reset_time 이 22:30/23:30(정규장 개장) 이면 서머타임 자동, 그 외는 설정값 그대로."""
        norm = _parse_hhmm(reset_time)
        norm_s = norm.strftime("%H:%M") if norm else None
        if infer_market(ticker, market) == "US" and norm_s in US_AUTO_RESET_VALUES:
            return cls.us_auto()
        return cls.reset_time_mode(reset_time)

    @property
    def is_us_auto(self) -> bool:
        return self.mode == MODE_US_AUTO

    def reset_on(self, d: date) -> dtime:
        """KST 날짜 d 의 리셋(세션 시작) 시각."""
        return us_open_kst_on(d) if self.is_us_auto else self._reset

    # --- 현재 시각 기준 ---
    def current_bounds(self, now: datetime):
        """now 가 속한 세션의 [시작, 끝) — KST naive. 끝 = 다음 리셋 시각(서머타임 전환일엔 23h/25h)."""
        now = to_kst_naive_dt(now)
        d = now.date()
        start = datetime.combine(d, self.reset_on(d))
        if now < start:
            d -= timedelta(days=1)
            start = datetime.combine(d, self.reset_on(d))
        nd = d + timedelta(days=1)
        return start, datetime.combine(nd, self.reset_on(nd))

    def session_start(self, now: datetime) -> datetime:
        return self.current_bounds(now)[0]

    def session_key(self, now: datetime) -> str:
        """일 손실한도 등에 쓰는 '세션 날짜'(세션 시작 시각의 KST 날짜, YYYY-MM-DD).
        기존 bot.get_session_date 와 같은 형식."""
        return self.session_start(now).strftime("%Y-%m-%d")

    def bars_needed(self, now: datetime, interval: str, warmup: int = 150, margin: int = 30,
                    cap: int = 1600) -> int:
        """현재 세션 시작부터 지금까지의 봉 수 + 여유. 지표 워밍업을 위해 최소 warmup(기존 150), 최대 cap."""
        start = self.session_start(now)
        minutes = max(0.0, (to_kst_naive_dt(now) - start).total_seconds() / 60.0)
        need = int(minutes / interval_minutes(interval)) + margin
        return int(min(max(need, warmup), cap))

    def describe(self) -> str:
        if self.is_us_auto:
            return "미국장 자동(서머타임 22:30 / 표준시 23:30)"
        return f"리셋 {self.reset_time} 고정"

    def describe_bounds(self, now: datetime) -> str:
        s, e = self.current_bounds(now)
        return f"{s.strftime('%m-%d %H:%M')}~{e.strftime('%m-%d %H:%M')} KST"

    # --- 봉 라벨링 (벡터화) ---
    def label_bars(self, times_kst: pd.Series) -> pd.Series:
        """각 봉이 속한 세션의 시작 시각(datetime64, KST naive)을 반환. 인덱스는 0..n-1."""
        t = pd.Series(pd.to_datetime(times_kst).values)
        if t.empty:
            return pd.Series([], dtype="datetime64[ns]")
        day = t.dt.normalize()

        def reset_td(days: pd.Series) -> pd.Series:
            if not self.is_us_auto:
                return pd.Series(pd.Timedelta(hours=self._reset.hour, minutes=self._reset.minute), index=days.index)
            # 날짜별 서머타임 여부 → 22:30 / 23:30 (고유 날짜만 계산)
            uniq = {d: us_open_kst_on(d.date()) for d in days.unique()}
            return days.map(lambda d: pd.Timedelta(hours=uniq[d].hour, minutes=uniq[d].minute))

        start_today = day + reset_td(day)
        prev_day = day - pd.Timedelta(days=1)
        start_prev = prev_day + reset_td(prev_day)
        return start_today.where(t >= start_today, start_prev)
