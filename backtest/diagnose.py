"""
diagnose.py — "그 시간틀에서 애초에 먹을 게 있는가?"를 숫자로 보는 진단

전략을 고치기 전에 반드시 먼저 봐야 하는 값들입니다.
아무리 좋은 신호를 만들어도, '한 번 움직이는 폭'이 '왕복 비용'보다 작으면
그 시간틀에서는 구조적으로 돈을 벌 수 없습니다. 이건 전략 문제가 아니라 산수 문제입니다.

출력 항목
  - ATR%          : 1봉당 평균 변동폭 (ATR14 / 가격). '한 봉에 보통 이만큼 움직인다'
  - 왕복비용%     : 수수료 + 슬리피지
  - 비용/ATR      : 본전 치려면 평균 몇 봉치 움직임이 필요한가. 3 이상이면 매우 불리.
  - |종가-VWAP|%  : 가격이 VWAP에서 벌어지는 정도의 중앙값/상위값
                    = 'VWAP 평균회귀'로 노릴 수 있는 이론상 최대 먹이 크기
  - VWAP복귀익%   : 실제로 VWAP 아래에서 샀을 때 VWAP까지 되돌아오면 먹는 폭 (비용 차감 전/후)
  - 세션레인지%   : 하루 (고가-저가)/시가

실행:
    cd C:\\Users\\user\\butler_pjt\\dev_pjt\\my_butler
    C:\\Users\\user\\butler_pjt\\venv312\\Scripts\\python.exe -m backtest.diagnose
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from backtest.data import fetch_intraday
from backtest.indicators import add_indicators

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 40)


def diagnose_one(ticker: str, interval: str, roundtrip_pct: float, lookback=None) -> dict:
    df = add_indicators(fetch_intraday(ticker, interval, lookback))
    df = df.iloc[30:]  # 지표 워밍업 구간 제외

    atr_pct = (df["atr"] / df["close"] * 100.0)
    dev_pct = ((df["close"] - df["vwap"]).abs() / df["vwap"] * 100.0)

    # VWAP 아래에 있을 때, VWAP까지 복귀하면 먹는 폭
    below = df[df["close"] < df["vwap"]]
    revert_gain = ((below["vwap"] - below["close"]) / below["close"] * 100.0)

    sess = df.groupby("session_date").agg(hi=("high", "max"), lo=("low", "min"), op=("open", "first"))
    sess_range = ((sess["hi"] - sess["lo"]) / sess["op"] * 100.0)

    med_atr = float(atr_pct.median())
    return {
        "종목": ticker,
        "봉": interval,
        "봉수": len(df),
        "일수": int(df["session_date"].nunique()),
        "가격": round(float(df["close"].iloc[-1]), 2),
        "ATR%(중앙)": round(med_atr, 4),
        "왕복비용%": roundtrip_pct,
        "비용/ATR": round(roundtrip_pct / med_atr, 2) if med_atr > 0 else np.nan,
        "|C-VWAP|%중앙": round(float(dev_pct.median()), 4),
        "|C-VWAP|%상위90": round(float(dev_pct.quantile(0.90)), 4),
        "VWAP복귀익%중앙": round(float(revert_gain.median()), 4),
        "복귀익-비용%": round(float(revert_gain.median()) - roundtrip_pct, 4),
        "복귀익>비용 비율%": round(float((revert_gain > roundtrip_pct).mean() * 100), 1),
        "세션레인지%중앙": round(float(sess_range.median()), 2),
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="SPY,QQQ,TSLA,SOXL,NVDA,AMD")
    ap.add_argument("--intervals", default="1m,5m,15m")
    ap.add_argument("--roundtrip", type=float, default=0.25, help="왕복 총비용 %%")
    args = ap.parse_args(argv)

    rows = []
    for iv in [x.strip() for x in args.intervals.split(",") if x.strip()]:
        for tk in [x.strip() for x in args.tickers.split(",") if x.strip()]:
            try:
                rows.append(diagnose_one(tk, iv, args.roundtrip))
            except Exception as exc:
                print(f"  [경고] {tk} {iv} 실패: {type(exc).__name__}: {exc}")

    out = pd.DataFrame(rows)
    print("\n" + "=" * 150)
    print(f"■ 시간틀별 '먹이 크기 vs 비용' 진단 (왕복비용 {args.roundtrip}% 가정)")
    print("=" * 150)
    for iv in out["봉"].unique():
        print(f"\n-- {iv} --")
        print(out[out["봉"] == iv].to_string(index=False))

    print("\n" + "=" * 150)
    print("■ 해석 가이드")
    print("  · '비용/ATR' 이 1보다 크면, 한 봉 평균 변동폭보다 왕복 비용이 크다는 뜻입니다.")
    print("    이 경우 짧게 치고 빠지는 매매는 구조적으로 불가능하고, 여러 봉을 끌고 가야만 합니다.")
    print("  · 'VWAP복귀익%중앙'이 '왕복비용%'보다 작으면, VWAP 평균회귀의 '평균적인' 먹이가")
    print("    비용보다 작다는 뜻입니다. 이기는 거래가 있어도 평균적으로는 마이너스가 됩니다.")
    print("  · '복귀익>비용 비율%'는 '비용을 넘길 기회 자체가 전체의 몇 %인가'입니다.")
    print("=" * 150)
    return 0


if __name__ == "__main__":
    sys.exit(main())
