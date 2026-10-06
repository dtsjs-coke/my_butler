"""
VWAP 3단계(섀도우 / 리플레이 / 봉 적재) 검증 스크립트 — 네트워크 없이 실행됩니다.
설계: docs/vwap_stage3_design.md §11

이 파일은 여러 작업자가 섹션별로 채웁니다. main() 의 tests 목록에 함수를 추가하세요.
  [JR-1] T-B1  bars_store  (이 파일의 'bars_store' 섹션)
  [SR-1] T-P*, T-H*  (시니어가 별도 섹션으로 추가)

T-B1 검증 항목
  1. 마감된 봉만 저장(마지막 행 제외)
  2. 150봉 겹치는 입력 10회 반복 → 중복 0
  3. 세션 날짜 분할 — SessionSpec.session_key 기준(자정으로 끊지 않음), 서머타임 종료 후 23:30 경계
  4. 재시작(메모리 초기화) 후 파일에서 복원
  5. 손상 줄 무시(읽기) + 줄바꿈 없이 끝난 파일에도 새 행이 오염되지 않음
  6. 쓰기 실패 무해(예외 없음, 성공한 지점까지만 진행 표시, 복구 후 재시도 가능)
  7. 동시 기록 — 중복 없음
  8. hook(ctx) — 비활성/빈 df/이상한 ctx 에서도 예외 없음, source 기록
  9. read_range — 기간 필터, 중복 제거(마지막 값 유지), 정렬
  10. 운영 data/ 해시 전후 동일

실행:  python scripts/test_vwap_stage3.py
"""
import os
import sys
import shutil
import threading
import traceback
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# 1단계 하네스 재사용: import 시점에 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + Discord 전송 no-op
import test_vwap_reliability as h  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.vwap import bars_store  # noqa: E402

check = h.check
D = datetime


# ---------------------------------------------------------------------------
# bars_store 도우미
# ---------------------------------------------------------------------------
def bars_df(start, n, step_min=1, base=100.0):
    """start 부터 step_min 간격 n 개 봉. close 는 시각에서 결정적으로 계산 → 같은 시각이면 항상 같은 값."""
    rows = []
    for i in range(n):
        t = start + timedelta(minutes=i * step_min)
        c = base + (t.hour * 60 + t.minute) * 0.001 + t.day * 0.01
        rows.append({"time": t, "open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1000.0 + i})
    return pd.DataFrame(rows)


def fresh_bars_env():
    """임시 DATA_DIR 의 bars 폴더와 메모리 상태 초기화. (h.reset_data_dir 은 파일만 지우므로 하위 폴더는 직접 정리)"""
    shutil.rmtree(bars_store.bars_dir(), ignore_errors=True)
    bp = os.path.join(h.TMP_DIR, "bars")
    if os.path.isfile(bp):
        os.remove(bp)
    bars_store._reset_state()


def bar_files():
    d = bars_store.bars_dir()
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def file_time_lines(name):
    """파일의 데이터 줄(헤더 제외)의 time 문자열 목록."""
    with open(os.path.join(bars_store.bars_dir(), name), "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    return [ln.split(",")[0] for ln in lines[1:]]


def total_rows():
    return sum(len(file_time_lines(n)) for n in bar_files())


# ---------------------------------------------------------------------------
# T-B1: bars_store
# ---------------------------------------------------------------------------
def test_bars_store():
    print("\n[T-B1] bars_store")

    # 1. 마감된 봉만 — 마지막 행 제외
    fresh_bars_env()
    df = bars_df(D(2026, 10, 6, 23, 0), 5)  # 23:00 ~ 23:04
    n = bars_store.append_closed_bars("AAPL", "1m", df, "22:30", "toss")
    times = [t for name in bar_files() for t in file_time_lines(name)]
    check("1. 5봉 입력 → 4봉 저장(마지막 행 제외)", n == 4 and len(times) == 4, f"written={n} rows={len(times)}")
    check("1. 저장된 마지막 봉은 23:03 (23:04 는 진행 중으로 간주)", times[-1] == "2026-10-06 23:03:00", times[-1])
    check("1. 헤더·파일명 형식",
          bar_files() == ["AAPL_1m_2026-10-06.csv"]
          and open(os.path.join(bars_store.bars_dir(), bar_files()[0]), encoding="utf-8").readline().strip()
          == "time,open,high,low,close,volume,source", str(bar_files()))
    check("1. 1행 입력은 아무것도 쓰지 않음", bars_store.append_closed_bars("AAPL", "1m", df.iloc[:1], "22:30", "toss") == 0)
    check("1. 빈/None 입력은 예외 없이 0",
          bars_store.append_closed_bars("AAPL", "1m", df.iloc[:0], "22:30", "toss") == 0
          and bars_store.append_closed_bars("AAPL", "1m", None, "22:30", "toss") == 0)

    # 2. 150봉 겹치는 입력 10회 → 중복 0
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 22, 30), 150 + 9 * 20)  # 총 330봉
    written = 0
    for k in range(10):
        window = full.iloc[k * 20: k * 20 + 150]  # 창을 20봉씩 밀며 150봉씩 겹쳐 입력
        written += bars_store.append_closed_bars("AAPL", "1m", window, "22:30", "toss")
    times = [t for name in bar_files() for t in file_time_lines(name)]
    check("2. 중복 없음", len(times) == len(set(times)), f"rows={len(times)} unique={len(set(times))}")
    check("2. 마지막 창의 마지막 행만 빼고 전부 저장 (329봉)", len(times) == 329 and written == 329,
          f"rows={len(times)} written={written}")
    check("2. 시간 순서 유지", times == sorted(times))
    # 같은 입력 반복 → 추가 기록 0
    again = bars_store.append_closed_bars("AAPL", "1m", full.iloc[180:330], "22:30", "toss")
    check("2. 동일 입력 재호출 → 0행 기록", again == 0 and total_rows() == 329, f"again={again}")

    # 3. 세션 날짜 분할 (SessionSpec.session_key 기준, 자정 기준 아님)
    fresh_bars_env()
    # 서머타임(개장 22:30): 10-06 22:00 ~ 10-07 00:30 → 22:00~22:29 는 10-05 세션, 22:30~ 은 10-06 세션(자정 넘어도 유지)
    df = bars_df(D(2026, 10, 6, 22, 0), 151)  # ~ 00:30 (마지막 행은 제외되어 00:29 까지)
    bars_store.append_closed_bars("AAPL", "1m", df, "22:30", "toss")
    check("3. 서머타임: 22:30 전/후로 파일 분할, 자정에서는 분할 안 함",
          bar_files() == ["AAPL_1m_2026-10-05.csv", "AAPL_1m_2026-10-06.csv"], str(bar_files()))
    a = file_time_lines("AAPL_1m_2026-10-05.csv")
    b = file_time_lines("AAPL_1m_2026-10-06.csv")
    check("3. 10-05 세션 = 22:00~22:29 (30봉)", len(a) == 30 and a[0].endswith("22:00:00") and a[-1].endswith("22:29:00"),
          f"{len(a)} {a[:1]} {a[-1:]}")
    check("3. 10-06 세션 = 22:30~00:29, 자정 넘는 봉 포함 (120봉)",
          len(b) == 120 and b[0] == "2026-10-06 22:30:00" and b[-1] == "2026-10-07 00:29:00" and
          any(t.startswith("2026-10-07 00:00") for t in b), f"{len(b)} {b[:1]} {b[-1:]}")
    # 서머타임 종료 후(표준시, 개장 23:30): 11-03 22:45 ~ 23:45 → 23:30 전 = 11-02 세션, 이후 = 11-03 세션
    fresh_bars_env()
    df = bars_df(D(2026, 11, 3, 22, 45), 61)  # 22:45 ~ 23:45
    bars_store.append_closed_bars("AAPL", "1m", df, "22:30", "toss")
    check("3. 표준시: 23:30 경계로 분할(22:45~23:29 → 11-02, 23:30~ → 11-03)",
          bar_files() == ["AAPL_1m_2026-11-02.csv", "AAPL_1m_2026-11-03.csv"], str(bar_files()))
    check("3. 표준시: 11-02 파일 45봉, 11-03 파일 15봉(23:30~23:44)",
          len(file_time_lines("AAPL_1m_2026-11-02.csv")) == 45 and len(file_time_lines("AAPL_1m_2026-11-03.csv")) == 15)
    # 국내 종목(6자리 숫자): 설정된 리셋 시각 고정 (09:00)
    fresh_bars_env()
    df = bars_df(D(2026, 10, 6, 8, 50), 21, base=70000.0)  # 08:50 ~ 09:10
    bars_store.append_closed_bars("005930", "1m", df, "09:00", "toss")
    check("3. 국내 종목은 고정 리셋 시각(09:00) 기준 분할", bar_files() == ["005930_1m_2026-10-05.csv", "005930_1m_2026-10-06.csv"],
          str(bar_files()))

    # 4. 재시작 후 복원
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 23, 0), 100)
    bars_store.append_closed_bars("AAPL", "1m", full.iloc[:60], "22:30", "toss")  # 59봉 저장
    bars_store._reset_state()  # 프로세스 재시작 시뮬레이션
    r1 = bars_store.append_closed_bars("AAPL", "1m", full.iloc[:60], "22:30", "toss")
    check("4. 재시작 후 같은 입력 재호출 → 0행 (파일에서 last_saved 복원)", r1 == 0 and total_rows() == 59, f"r1={r1}")
    bars_store._reset_state()
    r2 = bars_store.append_closed_bars("AAPL", "1m", full, "22:30", "toss")
    times = [t for name in bar_files() for t in file_time_lines(name)]
    check("4. 재시작 후 새 봉만 append (총 99봉, 중복 0)", r2 == 40 and len(times) == 99 == len(set(times)),
          f"r2={r2} rows={len(times)}")
    # 종목/간격이 다르면 서로 독립
    r3 = bars_store.append_closed_bars("TSLA", "1m", full, "22:30", "toss")
    r4 = bars_store.append_closed_bars("AAPL", "5m", full, "22:30", "toss")
    check("4. 종목·간격별 last_saved 독립", r3 == 99 and r4 == 99, f"r3={r3} r4={r4}")

    # 5. 손상 줄
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 23, 0), 40)
    bars_store.append_closed_bars("AAPL", "1m", full.iloc[:20], "22:30", "toss")  # 19봉
    path = os.path.join(bars_store.bars_dir(), "AAPL_1m_2026-10-06.csv")
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write("garbage,line,here\n")
        f.write("2026-10-06 23:19:00,1.0,2.0,0.5,abc,10,toss\n")   # 숫자 손상
        f.write("2026-10-06 23:20:00,1.0,2.0")                     # 쓰다 꺼짐(줄바꿈 없음)
    bars_store._reset_state()
    got = bars_store.read_range("AAPL", "1m", "2026-10-06", "2026-10-06")
    check("5. 손상 줄은 읽을 때 건너뜀 (정상 19봉만)", len(got) == 19, f"len={len(got)}")
    r = bars_store.append_closed_bars("AAPL", "1m", full, "22:30", "toss")
    got = bars_store.read_range("AAPL", "1m", "2026-10-06", "2026-10-06")
    check("5. 손상 뒤 새 봉이 오염되지 않고 정상 기록 (19+20=39봉)", r == 20 and len(got) == 39, f"r={r} len={len(got)}")
    check("5. 읽은 time 중복 없음·오름차순", got["time"].is_unique and got["time"].is_monotonic_increasing)

    # 6. 쓰기 실패 무해
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 23, 0), 30)
    open(os.path.join(h.TMP_DIR, "bars"), "w").close()  # bars 가 '파일'이라 폴더 생성 불가
    try:
        r = bars_store.append_closed_bars("AAPL", "1m", full, "22:30", "toss")
        raised = False
    except Exception:
        r, raised = -1, True
    check("6. 폴더 생성 실패 → 예외 없이 0 반환", (not raised) and r == 0, f"r={r}")
    os.remove(os.path.join(h.TMP_DIR, "bars"))
    os.makedirs(bars_store.bars_dir())
    os.makedirs(os.path.join(bars_store.bars_dir(), "AAPL_1m_2026-10-06.csv"))  # 같은 이름의 폴더 → open 실패
    try:
        r = bars_store.append_closed_bars("AAPL", "1m", full, "22:30", "toss")
        raised = False
    except Exception:
        r, raised = -1, True
    check("6. 파일 쓰기 실패 → 예외 없이 0 반환", (not raised) and r == 0, f"r={r}")
    shutil.rmtree(os.path.join(bars_store.bars_dir(), "AAPL_1m_2026-10-06.csv"))
    r = bars_store.append_closed_bars("AAPL", "1m", full, "22:30", "toss")
    check("6. 실패 때 last_saved 가 전진하지 않아 복구 후 같은 봉을 다시 저장함 (29봉)", r == 29 and total_rows() == 29, f"r={r}")
    # 경고 로그 10분 1회 제한
    warns = []
    orig = bars_store.logger.warning
    bars_store.logger.warning = lambda msg, *a, **k: warns.append(msg)
    try:
        bars_store._reset_state()
        for _ in range(5):
            bars_store._warn("테스트 경고")
        check("6. 경고 로그는 10분에 1회만 (5회 호출 → 1건)", len(warns) == 1, f"warns={len(warns)}")
    finally:
        bars_store.logger.warning = orig

    # 7. 동시 기록
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 22, 30), 200)
    errors = []

    def worker(k):
        try:
            for j in range(5):
                bars_store.append_closed_bars("AAPL", "1m", full.iloc[: 100 + (k * 5 + j) * 2], "22:30", "toss")
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    times = [t for name in bar_files() for t in file_time_lines(name)]
    check("7. 8스레드 동시 기록 → 예외 없음, 중복 0, 순서 유지",
          not errors and len(times) == len(set(times)) and times == sorted(times), f"rows={len(times)} errors={errors}")
    last_idx = 100 + (7 * 5 + 4) * 2  # 가장 큰 창 = 178행 → 177봉 저장
    check("7. 가장 큰 창까지의 봉이 모두 저장됨", len(times) == last_idx - 1, f"rows={len(times)} expected={last_idx - 1}")

    # 8. hook
    fresh_bars_env()
    full = bars_df(D(2026, 10, 6, 23, 0), 10)
    ctx = {"df": full, "ticker": "AAPL", "interval": "1m", "reset_time": "22:30",
           "candles_source": "yahoo", "config": {"bars_store_enabled": True}}
    n = bars_store.hook(ctx)
    with open(os.path.join(bars_store.bars_dir(), "AAPL_1m_2026-10-06.csv"), encoding="utf-8") as f:
        sources = {ln.strip().split(",")[-1] for ln in f.read().splitlines()[1:]}
    check("8. hook: 적재 + candles_source 가 source 로 기록", n == 9 and sources == {"yahoo"}, f"n={n} sources={sources}")
    fresh_bars_env()
    ctx_off = dict(ctx, config={"bars_store_enabled": False})
    check("8. bars_store_enabled=false → 아무것도 쓰지 않음", bars_store.hook(ctx_off) == 0 and bar_files() == [])
    check("8. df 가 비어 있으면 동작 안 함", bars_store.hook(dict(ctx, df=full.iloc[:0])) == 0 and bar_files() == [])
    check("8. config 키 없으면 기본 true 로 동작", bars_store.hook(dict(ctx, config={})) == 9)
    bad_ok = True
    for bad in (None, {}, {"df": "x"}, {"df": full, "ticker": None, "interval": None}, {"config": 5}):
        try:
            bars_store.hook(bad)
        except Exception:
            bad_ok = False
    check("8. 이상한 ctx(None/빈 dict/잘못된 타입)에도 예외 없음", bad_ok)

    # 9. read_range
    fresh_bars_env()
    d1 = bars_df(D(2026, 10, 6, 22, 30), 20)
    d2 = bars_df(D(2026, 10, 7, 22, 30), 20)
    bars_store.append_closed_bars("AAPL", "1m", d1, "22:30", "toss")
    bars_store.append_closed_bars("AAPL", "1m", d2, "22:30", "yahoo")
    g_all = bars_store.read_range("AAPL", "1m", "2026-10-06", "2026-10-07")
    g_one = bars_store.read_range("AAPL", "1m", "2026-10-07", "2026-10-07")
    g_none = bars_store.read_range("AAPL", "1m", "2026-01-01", "2026-01-02")
    g_other = bars_store.read_range("TSLA", "1m", "2026-10-06", "2026-10-07")
    check("9. 기간 필터 (전체 38봉 / 하루 19봉 / 범위 밖 0 / 다른 종목 0)",
          len(g_all) == 38 and len(g_one) == 19 and g_none.empty and g_other.empty,
          f"{len(g_all)} {len(g_one)} {len(g_none)} {len(g_other)}")
    check("9. 빈 결과도 컬럼 유지", list(g_none.columns) == bars_store.COLUMNS, str(list(g_none.columns)))
    check("9. 출처가 구분되어 읽힘", set(g_all["source"]) == {"toss", "yahoo"})
    # 같은 time 이 두 파일에 있는 경우 마지막 값 유지 (수동 중복 삽입)
    p = os.path.join(bars_store.bars_dir(), "AAPL_1m_2026-10-07.csv")
    first_t = g_one["time"].iloc[0].strftime("%Y-%m-%d %H:%M:%S")
    with open(p, "a", encoding="utf-8", newline="") as f:
        f.write(f"{first_t},9.0,9.0,9.0,9.0,9.0,dup\n")
    g_dup = bars_store.read_range("AAPL", "1m", "2026-10-07", "2026-10-07")
    row = g_dup[g_dup["time"] == pd.Timestamp(first_t)]
    check("9. time 중복은 마지막 값 유지", len(g_dup) == 19 and len(row) == 1 and float(row["close"].iloc[0]) == 9.0
          and row["source"].iloc[0] == "dup")
    check("9. date 객체/문자열 모두 허용", len(bars_store.read_range("AAPL", "1m", D(2026, 10, 6).date(), D(2026, 10, 7).date())) == 38)

    # tz-aware 입력은 KST 로 변환
    fresh_bars_env()
    utc = bars_df(D(2026, 10, 6, 14, 0), 6)  # UTC 14:00 = KST 23:00
    utc["time"] = pd.to_datetime(utc["time"]).dt.tz_localize("UTC")
    bars_store.append_closed_bars("AAPL", "1m", utc, "22:30", "yahoo")
    t0 = file_time_lines("AAPL_1m_2026-10-06.csv")[0] if bar_files() else None
    check("9. tz-aware(UTC) 입력은 KST naive 로 변환해 저장", t0 == "2026-10-06 23:00:00", str(t0))

    fresh_bars_env()


def main():
    print("=" * 70)
    print(" VWAP 3단계 검증 (네트워크 없음, 임시 DATA_DIR: %s)" % h.TMP_DIR)
    print("=" * 70)
    tests = [test_bars_store]  # 시니어: 여기에 T-P*/T-H* 함수를 추가
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
