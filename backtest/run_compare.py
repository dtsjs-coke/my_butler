"""
run_compare.py — 전략 후보 S0~S3 비교 실행 스크립트

실행 방법 (반드시 my_butler 폴더에서, venv312 파이썬으로):
    cd C:\\Users\\user\\butler_pjt\\dev_pjt\\my_butler
    C:\\Users\\user\\butler_pjt\\venv312\\Scripts\\python.exe -m backtest.run_compare

주요 옵션:
    --ticker SPY --interval 1m
    --fee 0.2 --slippage 0.05        (왕복 %)
    --is-ratio 0.6                   (앞 60% 거래일 = in-sample, 뒤 40% = out-of-sample)
    --tune                           (in-sample에서 소수의 파라미터만 탐색)
    --cross SPY,QQQ,TSLA,SOXL        (다른 종목으로 교차검증)
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from backtest.data import fetch_intraday, split_in_out_sample
from backtest.indicators import add_indicators
from backtest.engine import CostModel, run_backtest, summarize, exit_reason_breakdown
from backtest.strategies import S0_Current, S1_Fixed, S2_TrendPullback, S3_RangeBand

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)

# 윈도우 콘솔(cp949)에서 한글이 깨지지 않도록 표준출력을 UTF-8로 강제합니다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def build_strategies(params: dict | None = None):
    """비교할 전략 후보 목록을 만듭니다. params 로 튜닝 결과를 덮어씌울 수 있습니다."""
    p = params or {}
    return [
        S0_Current(n_percent=1.0, m_percent=1.0, x_percent=2.0),
        S1_Fixed(
            n_percent=p.get("s1_n", 1.0),
            sell_anchor=p.get("s1_anchor", "vwap_m"),
            min_profit_mult=1.5,
            atr_stop_mult=p.get("s1_atr", 1.5),
            max_hold_bars=30,
            eod_exit_bars=5,
            edge_gate_mult=3.0,
        ),
        S2_TrendPullback(
            adx_threshold=p.get("s2_adx", 20.0),
            atr_trail_mult=p.get("s2_atr", 2.0),
            max_hold_bars=60,
            eod_exit_bars=5,
        ),
        S3_RangeBand(
            sigma=p.get("s3_sigma", 1.5),
            adx_range_threshold=p.get("s3_adx", 20.0),
            min_profit_mult=1.5,
            atr_stop_mult=p.get("s3_atr", 1.5),
            max_hold_bars=30,
            eod_exit_bars=5,
            edge_gate_mult=3.0,
        ),
    ]


def _verdict(mean_ret: float, tstat: float, n: int) -> str:
    """통계적으로 뭐라고 말할 수 있는지를 보수적으로 판정합니다."""
    if n < 30:
        return "표본부족(판단불가)"
    if mean_ret <= 0:
        return "음의 기대값" + (" (유의)" if tstat < -2.0 else " (유의하지 않음)")
    if tstat > 2.0:
        return "양의 기대값 (통계적으로 유의)"
    return "양의 기대값이나 0과 구분 불가"


def benchmark_row(df: pd.DataFrame, cost: CostModel, equity=100_000.0, k=10.0) -> dict:
    """
    벤치마크: '그냥 사서 들고 있기(buy & hold)'를 전략과 똑같은 금액/비용으로 환산한 것.

    이게 왜 중요한가: 검증 기간에 주가가 크게 올랐다면, 롱온리 전략은 '엣지가 있어서'가 아니라
    '그냥 시장이 올라서' 수익이 난 것처럼 보입니다. 전략 수익이 이 값보다 못하면
    그 전략은 존재 이유가 없습니다.
    """
    first, last = df.iloc[0], df.iloc[-1]
    buy = cost.buy_price(float(first["open"]))
    sell = cost.sell_price(float(last["close"]))
    qty = float(int((equity * k / 100.0) / buy))
    notional = buy * qty
    net = (sell - buy) * qty - cost.fee(buy * qty) - cost.fee(sell * qty)
    # 보유 기간 최대낙폭
    eq = equity + (df["close"].to_numpy(float) - buy) * qty
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak * 100.0
    return {
        "전략": "BH_그냥보유",
        "거래수": 1,
        "승률%": 100.0 if net > 0 else 0.0,
        "평균이익%": np.nan,
        "평균손실%": np.nan,
        "손익비": np.nan,
        "기대값%/거래": round(net / notional * 100.0, 4),
        "기대값$/거래": round(net, 2),
        "총순손익$": round(net, 2),
        "총수익률%": round(net / equity * 100.0, 3),
        "MDD%": round(float(dd.max()), 3),
        "PF": np.nan,
        "평균보유봉": len(df),
        "t-stat": np.nan,
    }


def describe_period(df: pd.DataFrame, label: str) -> None:
    """구간 자체의 성격(추세/변동성)을 먼저 보여줍니다. 결과 해석에 반드시 필요합니다."""
    o, c = float(df.iloc[0]["open"]), float(df.iloc[-1]["close"])
    daily = df.groupby("session_date")["close"].last()
    day_ret = daily.pct_change().dropna()
    up_days = int((day_ret > 0).sum())
    print(
        f"  [{label}] 구간수익률 {(c/o-1)*100:+.2f}%  "
        f"(시가 {o:.2f} -> 종가 {c:.2f}) | 상승일 {up_days}/{len(day_ret)} | "
        f"일간 변동성(표준편차) {day_ret.std()*100:.2f}%"
    )


def run_set(df: pd.DataFrame, cost: CostModel, params=None, equity=100_000.0, k=10.0):
    """전략 목록 전체를 같은 데이터/비용으로 돌리고 (요약표, 결과객체목록) 반환."""
    rows, results = [], []
    for strat in build_strategies(params):
        res = run_backtest(df, strat, cost, initial_equity=equity, k_percent=k)
        rows.append(summarize(res))
        results.append(res)
    rows.append(benchmark_row(df, cost, equity, k))
    return pd.DataFrame(rows), results


def tune_in_sample(df_is: pd.DataFrame, cost: CostModel) -> dict:
    """
    in-sample에서만 '아주 적은 수'의 파라미터를 탐색합니다.
    과최적화를 막기 위해 후보를 의도적으로 3~4개씩으로 제한했습니다.
    (조합 수가 적을수록 in-sample 성과가 우연일 확률이 낮아집니다.)
    """
    best = {}

    # S1: 매도 앵커 2가지 x ATR 손절폭 3가지 = 6조합
    cand = []
    for anchor in ["vwap_m", "vwap"]:
        for atrm in [1.5, 2.5, 4.0]:
            s = S1_Fixed(sell_anchor=anchor, atr_stop_mult=atrm)
            r = run_backtest(df_is, s, cost)
            m = summarize(r)
            cand.append((m["기대값%/거래"] if m["거래수"] >= 5 else -999, anchor, atrm, m["거래수"]))
    cand.sort(reverse=True)
    best["s1_anchor"], best["s1_atr"] = cand[0][1], cand[0][2]
    print(f"  [S1 튜닝] 최상위 3개={cand[:3]} -> 선택: 앵커={best['s1_anchor']}, ATRx{best['s1_atr']}")

    # S2: ADX 임계 3가지 x ATR 배수 3가지 = 9조합
    cand = []
    for adx in [15.0, 20.0, 25.0]:
        for atrm in [1.5, 2.0, 3.0]:
            s = S2_TrendPullback(adx_threshold=adx, atr_trail_mult=atrm)
            r = run_backtest(df_is, s, cost)
            m = summarize(r)
            cand.append((m["기대값%/거래"] if m["거래수"] >= 5 else -999, adx, atrm, m["거래수"]))
    cand.sort(reverse=True)
    best["s2_adx"], best["s2_atr"] = cand[0][1], cand[0][2]
    print(f"  [S2 튜닝] 최상위 3개={cand[:3]} -> 선택: ADX>={best['s2_adx']}, ATRx{best['s2_atr']}")

    # S3: sigma 3가지 x ADX 임계 2가지 x ATR 손절폭 2가지 = 12조합
    cand = []
    for sg in [1.0, 1.5, 2.0]:
        for adx in [15.0, 20.0]:
            for atrm in [1.5, 3.0]:
                s = S3_RangeBand(sigma=sg, adx_range_threshold=adx, atr_stop_mult=atrm)
                r = run_backtest(df_is, s, cost)
                m = summarize(r)
                cand.append((m["기대값%/거래"] if m["거래수"] >= 5 else -999, sg, adx, atrm, m["거래수"]))
    cand.sort(reverse=True)
    best["s3_sigma"], best["s3_adx"], best["s3_atr"] = cand[0][1], cand[0][2], cand[0][3]
    print(f"  [S3 튜닝] 최상위 3개={cand[:3]} -> 선택: sigma={best['s3_sigma']}, "
          f"ADX<{best['s3_adx']}, ATRx{best['s3_atr']}")

    return best


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="SPY")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--lookback", type=int, default=None)
    ap.add_argument("--fee", type=float, default=0.20, help="왕복 수수료 %%")
    ap.add_argument("--slippage", type=float, default=0.05, help="왕복 슬리피지 %%")
    ap.add_argument("--fill-buffer", type=float, default=0.0,
                    help="지정가 체결 인정 버퍼 %% (줄서기 보수화, 0=스치면 체결)")
    ap.add_argument("--is-ratio", type=float, default=0.6)
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--k", type=float, default=10.0, help="거래당 투입 비중 %%(초기자본 기준 고정금액)")
    ap.add_argument("--tune", action="store_true")
    ap.add_argument("--cross", default="", help="교차검증 티커 (콤마구분)")
    ap.add_argument("--cost-sweep", action="store_true", help="비용 민감도 분석")
    ap.add_argument("--pooled", action="store_true", help="여러 종목 OOS 거래를 합쳐 통계 검정")
    args = ap.parse_args(argv)

    cost = CostModel(fee_roundtrip_pct=args.fee, slippage_roundtrip_pct=args.slippage,
                     limit_fill_buffer_pct=args.fill_buffer)
    print("=" * 110)
    print(f"■ 데이터 수집: {args.ticker} {args.interval}")
    raw = fetch_intraday(args.ticker, args.interval, args.lookback)
    df = add_indicators(raw)

    is_df, oos_df, is_days, oos_days = split_in_out_sample(df, args.is_ratio)
    print(f"■ 전체 {len(df)}봉 / {df['session_date'].nunique()}영업일 "
          f"({df['session_date'].min()} ~ {df['session_date'].max()})")
    print(f"  - in-sample     : {len(is_df):>6}봉 / {len(is_days)}일 ({is_days[0]} ~ {is_days[-1]})")
    print(f"  - out-of-sample : {len(oos_df):>6}봉 / {len(oos_days)}일 ({oos_days[0]} ~ {oos_days[-1]})")
    print(f"■ 비용 가정: 왕복 수수료 {args.fee}% + 왕복 슬리피지 {args.slippage}% "
          f"= 왕복 총 {cost.roundtrip_frac*100:.3f}%  (본전까지 필요한 최소 상승폭)")
    print(f"■ 사이징: 초기자본 ${args.equity:,.0f}, 거래당 고정 ${args.equity*args.k/100:,.0f} (복리 아님)")
    print("■ 구간 성격 (이걸 모르면 결과를 오독합니다)")
    describe_period(df, "전체")
    describe_period(is_df, "IS  ")
    describe_period(oos_df, "OOS ")
    print("=" * 110)

    params = None
    if args.tune:
        print("\n▶ [1] in-sample 파라미터 탐색 (여기서만 최적화, OOS는 절대 보지 않음)")
        params = tune_in_sample(is_df, cost)

    print("\n▶ [2] IN-SAMPLE 성과")
    is_tbl, is_res = run_set(is_df, cost, params, args.equity, args.k)
    print(is_tbl.to_string(index=False))

    print("\n▶ [3] OUT-OF-SAMPLE 성과  ★ 이 표가 실제 판단 근거 ★")
    oos_tbl, oos_res = run_set(oos_df, cost, params, args.equity, args.k)
    print(oos_tbl.to_string(index=False))

    print("\n▶ [4] 전체구간(참고용) 성과")
    all_tbl, all_res = run_set(df, cost, params, args.equity, args.k)
    print(all_tbl.to_string(index=False))

    print("\n▶ [5] 전체구간 청산사유 분해")
    for r in all_res:
        br = exit_reason_breakdown(r)
        if not br.empty:
            print(f"\n  -- {r.name} --")
            print(br.to_string())

    if args.cost_sweep:
        print("\n▶ [6] 비용 민감도 (전체구간, 거래당 기대값 %)")
        rows = []
        for fee, slip in [(0.0, 0.0), (0.10, 0.02), (0.20, 0.05), (0.40, 0.10)]:
            cm = CostModel(fee, slip, args.fill_buffer)
            t, _ = run_set(df, cm, params, args.equity, args.k)
            row = {"왕복비용%": round(cm.roundtrip_frac * 100, 3), "버퍼%": args.fill_buffer}
            for _, rr in t.iterrows():
                row[rr["전략"]] = rr["기대값%/거래"]
            rows.append(row)
        # 지정가 체결 낙관성(줄서기) 민감도
        for bufp in [0.02, 0.05]:
            cm = CostModel(args.fee, args.slippage, bufp)
            t, _ = run_set(df, cm, params, args.equity, args.k)
            row = {"왕복비용%": round(cm.roundtrip_frac * 100, 3), "버퍼%": bufp}
            for _, rr in t.iterrows():
                row[rr["전략"]] = rr["기대값%/거래"]
            rows.append(row)
        print(pd.DataFrame(rows).to_string(index=False))

    if args.cross:
        print("\n▶ [7] 교차검증 (다른 종목, 전체구간, 동일 파라미터)")
        rows = []
        for tk in [t.strip() for t in args.cross.split(",") if t.strip()]:
            try:
                d = add_indicators(fetch_intraday(tk, args.interval, args.lookback))
                t, _ = run_set(d, cost, params, args.equity, args.k)
                for _, rr in t.iterrows():
                    rows.append({"종목": tk, **rr.to_dict()})
            except Exception as exc:
                print(f"  [경고] {tk} 실패: {type(exc).__name__}: {exc}")
        if rows:
            cx = pd.DataFrame(rows)[["종목", "전략", "거래수", "승률%", "기대값%/거래",
                                     "총순손익$", "MDD%", "PF", "t-stat"]]
            print(cx.to_string(index=False))
            print("\n  -- 전략별 종목 평균 --")
            print(cx.groupby("전략")[["거래수", "승률%", "기대값%/거래", "총순손익$"]]
                  .mean().round(3).to_string())

    if args.pooled:
        print("\n▶ [8] ★통합 out-of-sample 검정★ — 여러 종목의 OOS 거래를 전부 합쳐서 판단")
        print("   (종목별로는 거래수가 적어 우연인지 구분이 안 되므로, 표본을 합쳐 검정력을 높입니다)")
        tickers = [args.ticker] + [t.strip() for t in args.cross.split(",") if t.strip()]
        pool = {}
        for tk in tickers:
            try:
                d = add_indicators(fetch_intraday(tk, args.interval, args.lookback))
                _, o_df, _, _ = split_in_out_sample(d, args.is_ratio)
                for strat in build_strategies(params):
                    r = run_backtest(o_df, strat, cost, args.equity, args.k)
                    pool.setdefault(strat.name, []).extend([t.ret_pct for t in r.trades])
            except Exception as exc:
                print(f"  [경고] {tk} 실패: {type(exc).__name__}: {exc}")

        rows = []
        for name, rets in pool.items():
            a = np.array(rets, dtype=float)
            n = len(a)
            if n == 0:
                rows.append({"전략": name, "OOS 총거래수": 0})
                continue
            sd = a.std(ddof=1) if n > 1 else 0.0
            se = sd / np.sqrt(n) if n > 1 else np.nan
            t = a.mean() / se if se and se > 0 else np.nan
            rows.append(
                {
                    "전략": name,
                    "OOS 총거래수": n,
                    "승률%": round((a > 0).mean() * 100, 1),
                    "기대값%/거래": round(a.mean(), 4),
                    "표준편차%": round(sd, 3),
                    "표준오차%": round(se, 4) if se == se else np.nan,
                    "t-stat": round(t, 2) if t == t else np.nan,
                    "95%신뢰구간": (
                        f"[{a.mean()-1.96*se:+.3f}, {a.mean()+1.96*se:+.3f}]" if se == se else "-"
                    ),
                    "판정": _verdict(a.mean(), t if t == t else 0.0, n),
                }
            )
        print(pd.DataFrame(rows).to_string(index=False))

    print("\n" + "=" * 110)
    print("주의: 위 수치는 특정 종목/짧은 기간의 과거 데이터에 대한 시뮬레이션 결과일 뿐이며,")
    print("      미래 수익을 보장하지 않습니다. 거래수가 적으면 통계적 의미도 약합니다.")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    sys.exit(main())
