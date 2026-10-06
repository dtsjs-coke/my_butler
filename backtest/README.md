# backtest/ — VWAP 전략 연구용 백테스트 하네스

> **이 폴더는 운영에 영향을 주지 않습니다.** `core/vwap/*`(실제로 돌아가는 봇)와 완전히 분리된
> 오프라인 검증 전용 코드입니다. 폴더째 지워도 봇은 그대로 동작합니다.
> 기존 `core/vwap/backtester.py`(운영 API `/api/vwap/backtest`가 쓰는 것)는 건드리지 않았습니다.

## 왜 새로 만들었나

기존 `core/vwap/backtester.py`는 결과를 실제보다 좋게 만드는 가정이 있어 판단 근거로 쓸 수 없었습니다.

| 기존 백테스터 | 이 하네스 |
|---|---|
| 수수료/슬리피지 0 | 왕복 0.25%(수수료 0.2% + 슬리피지 0.05%) 기본, 파라미터화 |
| 매도 체결가 = `max(target_sell, vwap)` (유리한 쪽 선택) | 지정가 그대로. 갭이면 시가 |
| 손절은 항상 손절가에 체결 (갭 무시) | 시가가 손절가 아래면 **시가**에 체결 |
| 시간/장마감 청산 없음 | 지원 |
| 한 봉에서 손절·익절 동시 가능 시 익절 우선 | **항상 손절 우선** |

## 실행 방법

전부 `my_butler` 폴더에서, `venv312` 파이썬으로 실행합니다.

```bash
cd C:\Users\user\butler_pjt\dev_pjt\my_butler
set PY=C:\Users\user\butler_pjt\venv312\Scripts\python.exe

# 0) 하네스 자체를 먼저 검증 (18개 항목, 전부 통과해야 결과를 믿을 수 있음)
%PY% -m backtest.validate

# 1) 시간틀별 '먹이 크기 vs 비용' 진단 — 전략을 고치기 전에 먼저 볼 것
%PY% -m backtest.diagnose

# 2) 전략 후보 비교 (in-sample 튜닝 + out-of-sample 검증 + 통합 검정)
%PY% -m backtest.run_compare --ticker <TICKER> --interval 1m --tune --pooled \
     --cross "SPY,QQQ,TSLA,SOXL,NVDA,AMD"

# 3) 비용 민감도만 보고 싶을 때
%PY% -m backtest.run_compare --ticker <TICKER> --interval 5m --cost-sweep
```

`yfinance`가 필요합니다 (venv312에 설치 완료). 받은 데이터는 `backtest/data_cache/`에 CSV로
캐시되며 날짜가 바뀌면 자동으로 다시 받습니다.

## 파일 구성

| 파일 | 역할 |
|---|---|
| `data.py` | yfinance 분봉 수집(7일씩 잘라 이어붙임) + 정규장 필터 + 캐시 + 거래일 단위 IS/OOS 분리 |
| `indicators.py` | 세션별 VWAP·표준편차, ATR, ADX, RSI (전부 과거만 참조) |
| `engine.py` | 룩어헤드 없는 봉 단위 체결 엔진, 비용 모델, 성과 지표 |
| `strategies.py` | 후보 S0(현행 재현) / S1(결함 제거) / S2(추세추종) / S3(횡보 밴드) |
| `validate.py` | 하네스 자가검증 18개 항목 |
| `diagnose.py` | 시간틀별 변동폭 대비 비용 진단 |
| `run_compare.py` | 비교 실행 + 표 출력 |

## 데이터 한계 (반드시 인지)

- yfinance는 **1분봉을 최근 30일까지만**, 5~30분봉은 60일까지만 제공합니다.
  분봉 전략을 수년치로 검증하는 건 yfinance만으로는 불가능합니다.
- 표본이 작으면 통계적 유의성이 낮습니다. `--pooled` 결과의 `t-stat`과 `95%신뢰구간`을
  반드시 함께 보세요. 거래수 30건 미만은 "판단 불가"로 취급합니다.
- 반드시 `BH_그냥보유`(buy & hold) 행과 비교하세요. 상승장에서는 엣지가 없어도
  롱온리 전략이 수익 난 것처럼 보입니다.

## 결과 요약

2026-09-19 시점 검증 결론과 제안은 [`docs/adr/0006-vwap-signal-redesign.md`](../docs/adr/0006-vwap-signal-redesign.md) 참고.
