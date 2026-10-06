"""
validate.py — 백테스트 하네스 자체를 검증하는 스크립트

"백테스트 결과를 믿을 수 있는가?"를 확인하기 위한 자가 점검입니다.
백테스트는 조용히 틀리기 쉬워서(특히 룩어헤드), 결과를 해석하기 전에 반드시 이걸 먼저 돌립니다.

실행:
    cd C:\\Users\\user\\butler_pjt\\dev_pjt\\my_butler
    C:\\Users\\user\\butler_pjt\\venv312\\Scripts\\python.exe -m backtest.validate

점검 항목
  [1] VWAP 일치      : 하네스의 VWAP이 운영코드 core/vwap/strategy.py 의 VWAP과 같은가
  [2] 룩어헤드 없음  : 미래 봉을 바꿔치기해도 과거 거래 기록이 1건도 안 바뀌는가
  [3] 체결가 보수성  : 모든 체결가가 그 봉의 고가/저가 범위 안이고, 유리한 쪽으로 새지 않는가
  [4] 비용 단조성    : 비용을 올리면 순손익이 반드시 나빠지는가
  [5] 오버나잇 금지  : 장마감 청산을 켠 전략(S1/S2/S3)이 정말 세션을 넘겨 보유하지 않는가
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest.data import fetch_intraday
from backtest.indicators import add_indicators
from backtest.engine import CostModel, run_backtest
from backtest.strategies import S0_Current, S1_Fixed, S2_TrendPullback, S3_RangeBand

PASS, FAIL = "[통과]", "[실패]"
_results = []


def check(name: str, ok: bool, detail: str = ""):
    _results.append((name, ok))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def main():
    print("=" * 100)
    print("■ 백테스트 하네스 자가검증")
    print("=" * 100)
    raw = fetch_intraday("SPY", "1m")
    df = add_indicators(raw)
    cost = CostModel(0.20, 0.05)

    # ---------------- [1] VWAP 일치 ----------------
    print("\n[1] 운영코드(core/vwap/strategy.py)의 VWAP과 일치하는지")
    try:
        from core.vwap.strategy import VwapStrategy

        one_day = df[df["session_date"] == sorted(df["session_date"].unique())[3]].copy()
        ref = VwapStrategy.calculate_vwap(
            one_day[["time", "open", "high", "low", "close", "volume"]].copy(),
            reset_time_str="09:30",
        )
        diff = np.abs(ref["vwap"].to_numpy(float) - one_day["vwap"].to_numpy(float))
        sd_diff = np.abs(ref["vwap_stdev"].to_numpy(float) - one_day["vwap_stdev"].to_numpy(float))
        check("VWAP 최대오차 < 1e-8", float(diff.max()) < 1e-8, f"max={diff.max():.3e}")
        check("VWAP 표준편차 최대오차 < 1e-8", float(sd_diff.max()) < 1e-8, f"max={sd_diff.max():.3e}")
    except Exception as exc:
        check("운영코드 VWAP 비교", False, f"{type(exc).__name__}: {exc}")

    # ---------------- [2] 룩어헤드 없음 ----------------
    print("\n[2] 룩어헤드(미래참조) 없음 — 미래 봉을 훼손해도 과거 거래가 동일해야 함")
    cut_day = sorted(df["session_date"].unique())[int(df["session_date"].nunique() * 0.6)]
    rng = np.random.default_rng(42)
    tampered_raw = raw.copy()
    mask = tampered_raw["session_date"] >= cut_day
    shock = rng.normal(1.0, 0.03, size=int(mask.sum()))
    for col in ["open", "high", "low", "close"]:
        tampered_raw.loc[mask, col] = tampered_raw.loc[mask, col].to_numpy(float) * shock
    tampered_raw.loc[mask, "high"] = tampered_raw.loc[mask, ["open", "high", "low", "close"]].max(axis=1)
    tampered_raw.loc[mask, "low"] = tampered_raw.loc[mask, ["open", "high", "low", "close"]].min(axis=1)
    tampered = add_indicators(tampered_raw)

    for strat_factory in [S0_Current, S1_Fixed, S2_TrendPullback, S3_RangeBand]:
        a = run_backtest(df, strat_factory(), cost)
        b = run_backtest(tampered, strat_factory(), cost)

        def past_only(res):
            return [
                (t.entry_time, round(t.entry_price, 6), round(t.exit_price, 6), t.exit_reason)
                for t in res.trades
                if t.exit_time.date() < cut_day
            ]

        pa, pb = past_only(a), past_only(b)
        check(f"{strat_factory().name}: 훼손 시점 이전 거래 {len(pa)}건 동일", pa == pb,
              f"원본 {len(pa)}건 / 훼손본 {len(pb)}건")

    # ---------------- [3] 체결가 보수성 ----------------
    print("\n[3] 체결가가 '그 봉에서 실제로 거래된 가격 범위' 안에 있는지")
    t_index = {pd.Timestamp(t): k for k, t in enumerate(df["time"])}
    lo = df["low"].to_numpy(float)
    hi = df["high"].to_numpy(float)
    bad_range, bad_entry_better, bad_exit_better = 0, 0, 0
    slip = cost.slip_side
    for factory in [S0_Current, S1_Fixed, S2_TrendPullback, S3_RangeBand]:
        res = run_backtest(df, factory(), cost)
        for t in res.trades:
            ei, xi = t_index[t.entry_time], t_index[t.exit_time]
            raw_entry = t.entry_price / (1 + slip)
            raw_exit = t.exit_price / (1 - slip)
            if not (lo[ei] - 1e-6 <= raw_entry <= hi[ei] + 1e-6):
                bad_range += 1
            if not (lo[xi] - 1e-6 <= raw_exit <= hi[xi] + 1e-6):
                bad_range += 1
            # 매수를 그 봉 최저가보다 싸게, 매도를 최고가보다 비싸게 체결시키면 '공짜 이득'을 준 것
            if raw_entry < lo[ei] - 1e-6:
                bad_entry_better += 1
            if raw_exit > hi[xi] + 1e-6:
                bad_exit_better += 1
    check("모든 체결가가 해당 봉의 [저가, 고가] 범위 안", bad_range == 0, f"위반 {bad_range}건")
    check("그 봉 최저가보다 싸게 매수한 건 없음", bad_entry_better == 0, f"위반 {bad_entry_better}건")
    check("그 봉 최고가보다 비싸게 매도한 건 없음", bad_exit_better == 0, f"위반 {bad_exit_better}건")

    # ---------------- [4] 비용 단조성 ----------------
    print("\n[4] 비용을 올리면 순손익이 반드시 나빠지는지 (같은 거래 집합일 때)")
    for factory in [S0_Current, S1_Fixed, S2_TrendPullback, S3_RangeBand]:
        runs = []
        for f, s in [(0.0, 0.0), (0.2, 0.05), (0.6, 0.15)]:
            r = run_backtest(df, factory(), CostModel(f, s))
            runs.append((len(r.trades), sum(t.net_pnl for t in r.trades)))
        # 비용 게이트(edge_gate) 때문에 거래수 자체가 달라지면 단순 비교가 성립하지 않으므로,
        # 거래수가 같은 구간끼리만 단조성을 검사한다.
        ok = True
        for a, b in zip(runs, runs[1:]):
            if a[0] == b[0] and a[1] < b[1] - 1e-6:
                ok = False
        detail = " | ".join(f"{n}건/{p:,.0f}$" for n, p in runs)
        check(f"{factory().name}: 비용↑ → 순손익↓", ok, detail)

    # 거래수가 완전히 고정된 상태에서의 단조성도 한 번 더 확인 (게이트 없는 S0/S2로)
    print("\n[4-b] 거래수가 동일한 전략(S0, S2)에서 비용 단조성 재확인")
    for factory in [S0_Current, S2_TrendPullback]:
        pnls, cnts = [], []
        for f, s in [(0.0, 0.0), (0.1, 0.02), (0.2, 0.05), (0.3, 0.08)]:
            r = run_backtest(df, factory(), CostModel(f, s))
            pnls.append(sum(t.net_pnl for t in r.trades))
            cnts.append(len(r.trades))
        mono = all(a >= b - 1e-6 for a, b in zip(pnls, pnls[1:]))
        check(f"{factory().name}: 단조 감소", mono,
              f"거래수={cnts} / 손익={[round(p) for p in pnls]}")

    # ---------------- [5] 오버나잇 금지 확인 ----------------
    print("\n[5] 장마감 청산을 켠 전략이 세션을 넘겨 보유하지 않는지")
    for factory in [S1_Fixed, S2_TrendPullback, S3_RangeBand]:
        r = run_backtest(df, factory(), cost)
        overnight = [t for t in r.trades if t.entry_time.date() != t.exit_time.date()]
        check(f"{factory().name}: 오버나잇 보유 0건", len(overnight) == 0, f"{len(overnight)}건")
    r0 = run_backtest(df, S0_Current(), cost)
    on0 = [t for t in r0.trades if t.entry_time.date() != t.exit_time.date()]
    print(f"  [참고] S0(현행)는 장마감 청산이 없어 오버나잇 보유가 {len(on0)}/{len(r0.trades)}건 발생 "
          f"— 운영 봇의 실제 동작을 그대로 재현한 것입니다.")

    # ---------------- 종합 ----------------
    n_fail = sum(1 for _, ok in _results if not ok)
    print("\n" + "=" * 100)
    print(f"■ 검증 결과: 총 {len(_results)}개 중 통과 {len(_results)-n_fail}, 실패 {n_fail}")
    print("=" * 100)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
