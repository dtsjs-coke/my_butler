# ADR-0007: VWAP 3단계 — 섀도우 모드 / 원클릭 리플레이 / 봉 데이터 적재 / 전략 플러그인 인터페이스

- **날짜**: 2026-10-05
- **상태**: **Accepted** (상태 갱신 2026-10-07: Phase A 커밋 `0c05162`, Phase B 커밋 `4fa93a0`, Phase C 커밋 `9ead037` 모두 QA 통과 후 S9 배포 완료. 설계와 달라진 점은 하단 정정 메모·SR-3 메모 참고)
- **대상**: `my_butler` VWAP 자동매매 서브시스템
- **선행 ADR**: [ADR-0006](0006-vwap-signal-redesign.md) — 신호 재설계 보류, 연구 하네스 `backtest/` 도입, "봉 적재" 권고
- **구현 명세**: [`docs/vwap_stage3_design.md`](../vwap_stage3_design.md) (파일/API/UI/테스트/작업 분할)

---

## 배경

1·2단계에서 실거래 체결 판정의 신뢰성(ADR 없음, 1단계)과 관측성(2단계, 사유·이벤트·알림)을 갖췄다. 하지만 사용자에게는 아직 다음 질문의 답이 없다.

1. **"실거래가 전략대로 체결되고 있나?"** — 미체결, 슬리피지, 버그로 인한 실행 괴리를 측정할 기준선이 없다. 가상봇 V1~V3는 이 질문에 답할 수 없다. 사용자가 설정을 따로 맞춰야 하고, 실거래와 다른 시각·다른 봉으로 돌기 때문이다. 사용자는 이 화면을 "사용자 친화적이지 않다"고 했고 거의 쓰지 않는다.
2. **"지금 설정으로 최근 며칠 돌렸으면 어땠나?"** — 운영 `core/vwap/backtester.py`는 150봉, 수수료 0, 유리한 체결을 가정한다. ADR-0006에서 이 결과로는 판단할 수 없다고 결론 냈다. 검증된 하네스 `backtest/`는 연구 전용 CLI라 화면에서 쓸 수 없다.
3. **분봉 데이터가 30~60일(yfinance)을 넘지 못한다.** ADR-0006은 "봇이 받은 봉을 지금부터 적재하라"고 권고했다.
4. **4단계 전략 재연구 결과를 운영에 넣을 통로가 없다.** 같은 전략이 운영 `VwapStrategy.get_signals`와 연구 `backtest/strategies.S0_Current` 두 곳에 **따로 구현**돼 있어 언제든 서로 어긋날 수 있다.

### 이번 조사에서 새로 확인한 사실 (설계 제약)

- **(F1) 운영 VWAP이 KST 자정에 세션 중간에 리셋된다.** `VwapStrategy.calculate_vwap`은 "날짜가 바뀌면 새 세션"(`t.date() != prev_t.date()`)으로 처리한다. 봉 시각이 KST이면 미국장(22:30~05:00 KST) 도중인 00:00에 VWAP 누적이 끊긴다. 스크래치 재현 결과: 22:30~23:30 종가 100/102/104 뒤 00:00 종가 106에서, 기대 VWAP 103.0 대신 106.0이 나왔다. `backtest/validate.py`의 VWAP 일치 검사는 ET 기준 하루치만 비교해서 이 문제를 잡지 못했다. Toss 캔들 timestamp의 실제 시간대는 S9에서 확인해야 한다. **이것은 매매 동작에 영향을 주는 기존 버그다.** 이 ADR의 범위가 아니고 별도 결정이 필요하다(설계 문서 §8).
- **(F2) `reset_time`은 고정값 22:30이다.** 미국 서머타임은 2026-11-01에 끝나고, 그 뒤 정규장 시작은 23:30 KST가 된다.

> **메모 (2026-10-06 추가)**: F1·F2는 [ADR-0008](0008-vwap-session-boundary.md)에서 처리한다. 그래서 4단계(전략 재연구) ADR 번호는 0009가 된다. 이 ADR의 `session_date`(봉 적재·리플레이) 계산은 ADR-0008의 `SessionSpec.session_key`를 따라야 한다.
- **(F3) S9 `requirements.txt`에 `yfinance`가 없다.** pandas/numpy도 freeze 목록에 없다(설치는 돼 있을 것으로 추정). `backtest/data.py`는 yfinance에 의존한다. 최신 yfinance는 `curl_cffi` 같은 네이티브 의존성이 있어 Termux 설치가 불확실하다.
- **(F4) `sync_s9`는 로컬에 없는 원격 파일을 지운다**(`cleanup_remote_orphans`). `backtest/data_cache/`는 제외 목록에 없다. 따라서 S9 런타임이 이 폴더에 쓰면 다음 동기화 때 지워진다. 반대로 로컬의 9.4MB 캐시는 S9로 전송된다.
- **(F5) 연구 하네스의 `indicators.bars_to_session_end`는 세션 전체 길이를 미리 안다(미래 정보).** 백테스트(정규장 고정 길이)에서는 문제가 없지만, 실시간·섀도우 경로에서는 쓸 수 없다. S1~S3의 장마감 청산이 이 값을 쓴다.
- **(F6) Toss 캔들 API 사용 코드는 `count≤200`만 쓰고 과거 구간 페이징이 없다.** 공식 API의 페이징 지원 여부는 확인하지 못했다. 리플레이의 장기 데이터 소스로는 쓸 수 없다고 본다.

---

## 결정

### D1. 전략 플러그인 인터페이스 — 운영 코드를 "감싸서" 단일 구현으로 만든다

`core/vwap/strategies/` 패키지에 공통 인터페이스 `TradingStrategy`를 두고, 이번 단계에서는 **S0(현행)만** 구현한다.

- `prepare(df, ctx) -> df`: 인과적(과거만 보는) 지표를 **한 번에 벡터 계산**한다. S0는 `VwapStrategy.calculate_vwap`을 그대로 호출한다.
- `evaluate(df, j, position, ctx) -> StrategySignal`: 봉 j까지의 정보로 판단한다. S0는 `VwapStrategy.get_signals(df.iloc[[j]], …)`을 그대로 호출한다. `get_signals`는 넘겨받은 df의 **마지막 행만** 읽기 때문에, 미리 계산한 df의 한 행을 넘기면 실시간과 같은 코드가 O(1)로 실행된다.
- `StrategySignal`은 `signal, reason_code, reason_text, target_buy_price, target_sell_price, stop_loss_price, filters, indicators`와 `exit_market`(미래 전략의 시간·마감 청산용, S0는 항상 False)을 담는다.
- **`core/vwap/strategy.py`, `bot.py`의 실거래 신호 경로는 이번 단계에서 바꾸지 않는다.** REAL 봇은 계속 `VwapStrategy`를 직접 호출한다. 이번 단계에서 플러그인을 쓰는 곳은 리플레이뿐이다. 섀도우는 REAL과 같은 `_loop_step` 코드를 재사용하므로(D3) 같은 `VwapStrategy`를 쓴다. 플러그인을 REAL에 연결하는 것은 4단계에서 설정 키 `real_strategy` 도입과 함께 별도로 결정한다.
- **연구 엔진 연결**: `PluginEngineAdapter(backtest.engine BaseStrategy 호환)`가 `StrategySignal`을 엔진 `Decision`으로 바꾼다. 실시간 봇의 주문 매핑을 그대로 따른다(BUY→buy_limit, SELL→sell_limit, STOP_LOSS→exit_market, HOLD/WAIT→무주문). 이렇게 하면 `backtest/strategies.S0_Current`(재구현본)와 운영 S0(원본)의 거래 결과를 **동치 검증**할 수 있다.

### D2. 리플레이 — 검증된 `backtest/engine.py`를 제자리에서 import하고, 데이터 로더는 운영용으로 새로 만든다. 계산은 별도 프로세스에서 한다

- **엔진**: `backtest.engine.run_backtest / CostModel / summarize`를 운영에서 **그대로 import**한다. 파일은 옮기지 않는다. 의존성이 pandas와 numpy뿐이라 S9에서도 돌고, 18개 자가검증이 걸린 코드를 옮기면 "검증된 그 코드"라는 근거가 약해진다.
- **`backtest/data.py`와 `indicators.py`는 운영에서 import하지 않는다.** 이유는 F3(yfinance), F4(캐시 경로), F5(미래 정보), 그리고 시간대·세션 정의가 ET 정규장으로 운영(KST, reset_time)과 다르다는 점이다.
- **운영 데이터 로더 `core/vwap/bar_source.py`**:
  - ① `data/bars/` 적재분(Toss 원본, 최우선)
  - ② 부족한 구간은 Yahoo chart API를 **`requests`로 직접** 호출한다. 이미 `TossBroker._fetch_yahoo_candles`가 쓰는 방식이고 yfinance가 필요 없다. 기간은 `period1/period2`로 7일씩 나눠 받는다(1m 30일, 5m/15m 60일).
  - 시각은 운영과 같은 KST로 맞추고, 세션은 운영과 같은 `reset_time` 규칙을 따른다.
  - 캐시는 `data/replay_cache/`(동기화 제외)에 둔다.
- **세션 정의는 "실거래 봇이 실제로 했을 일"을 재현하는 쪽을 택한다.** S0는 `calculate_vwap`을 그대로 쓰므로 F1의 자정 리셋도 그대로 재현된다. F1을 고치면 리플레이에도 자동으로 반영된다.
- **실행 방식**: `POST /vwap/api/replay`는 작업 ID만 바로 돌려준다(202). 계산은 **별도 파이썬 프로세스**(`python -m core.vwap.replay_job <job_id>`)가 맡고, 결과는 `data/replay/<job_id>.json`에 쓴다. 화면은 폴링한다.
  - 한 번에 1개 작업만 허용한다(나머지 409). 타임아웃은 기본 300초, 결과는 최근 5개만 보관한다.
  - 별도 프로세스를 택한 이유: CPU를 쓰는 계산이 같은 프로세스 안에서 돌면 GIL 경합으로 REAL 봇 스레드와 Discord 루프가 느려진다. 리플레이가 메모리 부족 등으로 죽어도 Butler 본체는 영향을 받지 않는다.
- 기존 `/vwap/api/backtest`와 `core/vwap/backtester.py`는 **당분간 유지**한다(하위 호환). UI 탭은 리플레이로 교체한다. 삭제는 리플레이가 1주 이상 안정적으로 돈 뒤 별도 작업으로 한다.

### D3. 섀도우 모드 — REAL 봇의 같은 주기·같은 봉으로 도는 "그림자 원장". REAL 루프 끝의 격리된 훅에서 실행한다

- 구성 요소: `core/vwap/shadow.py`의 `ShadowBot(VWAPBot)`. 모드명은 **`VIRTUAL_SHADOW`**다. `VIRTUAL` 접두어 덕분에 기존 가상 경로를 그대로 탄다. 데이터 파일은 `data/vwap_trades_virtual_shadow.json`, `data/vwap_events_virtual_shadow.jsonl`, `data/vwap_shadow_state.json`이다.
- **주문 관리 로직을 다시 짜지 않는다.** `ShadowBot`은 REAL과 **같은 `_loop_step` 코드**(예산 k%, 시작시각 대기, 정정·취소, 손절, 일손실한도, 2단계 사유·이벤트)를 가상 브로커로 실행한다. 다른 점은 세 가지뿐이다.
  - ① 시세 소스가 "REAL이 이번 주기에 받은 df를 돌려주는 정적 브로커"(`StaticCandleBroker`)다.
  - ② 설정 소스가 "REAL 설정의 `real_*` 키를 `virtual_shadow_*`로 바꾼 dict"다.
  - ③ Discord 알림은 항상 끈다.
  - 이를 위해 `bot.py`의 `VwapConfigManager.load_config()` 직접 호출 1곳을 `self._load_config()` 위임으로 바꾼다. 기본 구현이 같은 호출이라 동작은 동일하다.
- **실행 위치**: REAL 봇 `_loop_step`가 끝난 뒤 실행되는 **post-cycle 훅** 하나(`_run_post_cycle_hooks`)에서 실행한다.
  - 훅에는 이번 주기에 REAL이 받은 **같은 캔들 df(복사본)**, 같은 시각, 그 주기의 REAL 설정(`real_*`)을 넘긴다.
  - 섀도우는 **추가 API 호출을 하지 않는다**(시세는 REAL이 받은 df를 재사용).
  - 훅은 `try/except`로 완전히 격리한다. REAL의 주문·체결 판정이 모두 끝난 뒤에 실행되며, REAL 상태를 읽기만 하고 쓰지 않는다.
  - 소요시간이 기준(기본 2초)을 넘으면 경고 이벤트를 남긴다.
- **설정 동기화**: 섀도우는 자체 설정이 없다. 매 주기 REAL의 `real_*` 설정을 그대로 복사해 쓴다("설정 없이 자동으로 따라감"). 신호는 REAL과 같은 `VwapStrategy`(= S0)로 낸다. 4단계에서 REAL이 플러그인으로 전환되면 섀도우도 같은 코드라 자동으로 따라간다.
- **원장 동기화**: 독립 원장(가상 현금·보유)으로 운영하되, **REAL BOT_START 시점과 매 세션 시작(reset_time)에 REAL의 실제 보유수량·평단·기준자본으로 재동기화**한다. 한쪽만 체결된 뒤 상태가 영원히 갈라져 비교가 무의미해지는 것을 막기 위해서다.
- **비교 단위는 "주기 ID"(`cycle_id` = 판단에 쓴 봉 시각)다.**
  - 2단계의 `_order_meta()`에 `cycle_id`를 추가한다. 관측성 메타일 뿐이고 매매 동작은 바뀌지 않는다.
  - REAL 주문과 섀도우 주문을 `cycle_id + side`로 짝짓는다.
  - 결과를 `MATCH / REAL_UNFILLED / SHADOW_UNFILLED / PRICE_GAP / REAL_ONLY_ORDER / SHADOW_ONLY_ORDER`로 분류한다.
- **체결 모델**: 기존 VirtualBroker의 "스치면 체결(지정가)"을 그대로 쓴다. 섀도우는 "이상적 실행"의 기준선이다. 실거래가 미체결인데 섀도우가 체결됐으면 큐 순서나 유동성 같은 실행 괴리의 신호다. 수수료는 `shadow_fee_roundtrip_pct`(기본 0.2%)로 추정해 순손익에 반영한다.
- 기존 V1~V3는 **코드·데이터·API를 그대로 둔다.** UI에서만 기본으로 숨긴다(`ui_show_legacy_virtual=false`).
- REAL이 정지 중이면 섀도우도 돌지 않는다. 섀도우의 존재 이유가 REAL과의 비교이기 때문이다. REAL 없이 전략을 보려면 리플레이를 쓴다. → 사용자 확인 항목(설계 문서 §9 Q1).

### D4. 봉 데이터 적재 — 마감된 봉만, 세션 날짜별 CSV, 중복 없이 append

- 경로: `data/bars/{TICKER}_{interval}_{session_date}.csv`. `session_date`는 운영 `get_session_date(reset_time)` 기준이다. 컬럼은 `time,open,high,low,close,volume,source`이고, `source`는 `toss` 또는 `yahoo`다.
- `source`가 필요한 이유: `TossBroker.get_candles`는 실패하면 **조용히 Yahoo로 폴백**한다. 그래서 브로커에 읽기 전용 속성 `last_candles_source`를 추가한다. 매매 동작과는 무관하다.
- **마감된 봉만** 기록한다(df의 마지막 행은 진행 중일 수 있어 제외). 메모리에 `(ticker, interval)`별 마지막 기록 시각을 두고 그보다 새 행만 append한다. 재시작 시에는 해당 파일 끝부분을 읽어 복원한다. 읽는 쪽도 `time` 기준으로 중복을 제거한다(이중 안전장치).
- 기록 주체는 REAL·가상 봇 공통의 post-cycle 훅(D3와 같은 훅)이다. 모듈 수준 Lock을 쓰고 실패는 삼킨다. 같은 종목을 여러 봇이 돌려도 파일 하나에 중복 없이 쌓인다.
- 용량은 1m 기준 종목당 하루 약 90KB, 연 약 33MB로 추정한다. 정리 정책은 이번에 두지 않는다(연구 원재료이므로 보존).

---

## 기각한 대안

| # | 대안 | 기각 근거 |
|---|---|---|
| A1 | 섀도우를 **별도 VWAPBot 인스턴스(독립 스레드)**로 운영하고 `real_*` 설정만 복사 | REAL 경로를 전혀 안 건드린다는 장점은 크다. 그러나 ① 루프 시각이 최대 60초 어긋나 **다른 봉으로 판단**하게 되고, 그 차이가 "실행 괴리"와 섞여 측정 대상 자체를 오염시킨다. ② Toss 캔들 호출이 2배로 늘어 호출 한도와 토큰 실패 위험이 커진다. ③ 설정 복사 시점 차이로 동기화 버그가 생긴다. → **D3(같은 주기 훅)** 채택. 단, 사용자가 "REAL 코드는 한 줄도 바꾸지 말라"를 우선하면 이 대안으로 되돌릴 수 있다(설계 문서 §9 Q2). |
| A2 | 섀도우를 REAL `_loop_step_body` 안에 섞어서 구현 | 섀도우 버그가 REAL 주문 흐름 중간에서 예외나 지연을 일으킬 수 있다. → 루프 **끝**의 단일 격리 훅으로 한정. |
| A3 | `backtest/` 패키지 전체(`data.py`, `indicators.py` 포함)를 운영에서 import | F3(yfinance가 S9에 없음), F4(캐시가 sync에 지워짐), F5(미래 정보 `bars_to_session_end`), 시간대·세션 정의 불일치(ET 정규장 vs KST reset_time) 때문에 기각. |
| A4 | 엔진을 `core/vwap/research/`로 **이동**하고 `backtest/`가 거기서 import | 구조는 깔끔하지만, 18개 자가검증이 걸린 파일을 옮기는 순간 재검증과 경로 변경이 필요하다. `backtest/`가 이미 import 가능한 패키지라 이득이 작다. 나중에 연구 코드가 커지면 재검토. |
| A5 | 기존 `core/vwap/backtester.py`를 고쳐서 사용 | 150봉 제한, 비용 0, 유리한 체결이라는 구조적 문제가 있다(ADR-0006 결함 6). 고치는 것보다 검증된 엔진을 연결하는 편이 싸고 믿을 만하다. |
| A6 | 리플레이를 Flask 요청 안에서 **동기**로 실행 | S9 기준 1m 30일 ≈ 8천~2만 봉으로 수십 초~수 분이 걸릴 수 있다. HTTP 타임아웃과 Flask 스레드 점유가 생기고, GIL 경합으로 REAL 봇이 지연될 수 있다. |
| A7 | 리플레이를 Butler 프로세스 안의 **백그라운드 스레드**로 실행 | 응답 문제는 해결된다. 하지만 GIL 경합(REAL 봇·Discord 지연)과 메모리 폭주 시 본체 동반 사망 위험이 남는다. 별도 프로세스가 Termux에서도 표준 라이브러리 `subprocess`만으로 가능해 추가 비용이 작다. |
| A8 | 섀도우 원장을 **매 주기 REAL 보유로 덮어쓰기**(순수 "주문 단위 실행 비교") | 주문 단위 체결 비교는 정확해진다. 대신 "같은 전략을 이상적으로 실행했다면 손익이 얼마였나"를 볼 수 없다. → 세션 단위 재동기화로 절충(D3). |
| A9 | 플러그인 도입과 동시에 REAL 신호 경로도 플러그인으로 전환 | 실거래 경로 무변경 원칙에 어긋난다. 4단계에서 회귀 테스트·동치 테스트와 함께 별도로 결정한다. |
| A11 | 섀도우를 플러그인 + 별도 주문관리 코드로 새로 구현 | 예산·시작대기·정정·손절·손실한도 로직을 한 벌 더 만들게 된다. 그러면 "섀도우와 REAL의 차이"에 **코드 차이**가 섞여 실행 괴리 측정이 오염된다. → REAL과 같은 `_loop_step`을 재사용하는 `ShadowBot`을 채택. |
| A10 | 봉 적재를 SQLite/Parquet로 | Termux에서 pyarrow 설치가 불확실하다. SQLite는 sync 제외 패턴(`*.db`)과 겹친다. CSV는 사람이 열어보기 쉽고 연구 하네스와 바로 호환된다. 용량도 작다. |

---

## 결과

### 좋아지는 것
- 실거래 실행 괴리(미체결·슬리피지·버그)를 **주문 단위로 숫자로** 볼 수 있다.
- "현재 설정으로 최근 N일" 리플레이 결과가 보수적 체결, 비용, buy&hold 대비를 포함한 **믿을 수 있는 근거**가 된다.
- 운영·섀도우·리플레이가 **같은 S0 신호 코드**(`VwapStrategy`)를 쓴다. 리플레이 쪽은 플러그인 래퍼를 거치고, 연구용 재구현본(`S0_Current`)과의 동치를 테스트로 증명한다. 4단계에서 REAL이 플러그인으로 전환되면 신규 전략은 인터페이스 구현 하나로 세 곳에 동시에 들어간다.
- 오늘부터 Toss 원본 봉이 쌓인다. 몇 달 뒤 하락장을 포함한 표본을 확보할 수 있다(ADR-0006의 최대 약점 해소 경로).

### 감수하는 것
- REAL `bot.py`에 **post-cycle 훅 1곳**, `_load_config()` 위임 1곳, `_order_meta`의 `cycle_id` 필드가 추가되고, `broker.py`에 `last_candles_source` 속성이 추가된다. 매매 결정·주문 코드는 바뀌지 않지만 "REAL 파일 무변경"은 아니다. 회귀 테스트 2종과 신규 격리 테스트로 담보한다.
- 섀도우의 체결 모델(스치면 체결)은 낙관적이다. 섀도우가 이기는 것 자체는 정상이고, 해석은 "괴리의 크기"로 한다.
- 리플레이의 S0는 F1(자정 리셋)까지 재현한다. 버그를 고치기 전 결과와 고친 뒤 결과는 다를 수 있다.
- 리플레이에서 일 손실한도 패닉, 시작 시각 대기 등 봇 단계 규칙은 일부만 모델링한다(설계 문서 §4.6). 결과 화면에 "모델링하지 않은 것" 목록으로 명시한다.

### 나중에 뒤집는다면
- 섀도우를 독립 인스턴스(A1)로 바꾸려면 `ShadowBot`에 자체 루프 스레드를 주고(일반 `start()`) 시세 소스만 실제 브로커로 돌리면 된다. 훅은 제거한다. 데이터 파일 형식은 그대로 둘 수 있다.
- 엔진을 이동(A4)할 때는 `backtest/validate.py`의 import 경로만 바꾸고 18개 검증을 다시 돌린다.

---

## 정정 메모 (2026-10-07 추가 — 본문은 수정하지 않음)

- **4단계 ADR 번호**: 위 25행 메모의 "4단계(전략 재연구) ADR 번호는 0009"는 정정한다. 0009 는 [ADR-0009](0009-vwap-start-wait-stop-loss.md)(거래 시작 대기 중 손절 허용)가 사용했으므로 **4단계 ADR 은 0010** 이다.
- **`ui_show_legacy_virtual`**: 80행의 "UI에서만 기본으로 숨긴다(`ui_show_legacy_virtual=false`)"는 서버 설정 키를 뜻하지 않는다. 서버 설정 키 `ui_show_legacy_virtual`은 **삭제 확정**(2026-10-07, QA 권고 — 코드 참조 0건)이며, 레거시 가상봇 표시 토글은 브라우저 `localStorage`(`vwap_ui_show_legacy_virtual`)만 사용한다.
- **4단계 ADR 번호 재정정 (2026-10-07)**: 위 "4단계 ADR 은 0010" 도 다시 정정한다. 0010 은 [ADR-0010](0010-vwap-real-untrusted-candles.md)(REAL 신뢰 불가 캔들 보호)가 사용했으므로 **4단계(전략 재연구) ADR 은 0011** 이다.

## SR-3 메모 (2026-10-07 추가 — Phase C 섀도우 시니어 리뷰 결과, 본문은 수정하지 않음)

- **REAL `bot.py` 변경 1건 추가 — 판단 시각 위임 `VWAPBot._now()`**
  - 변경: `_loop_step_body`의 `now = datetime.now()`를 `now = self._now()`로 바꾸고, 기본 구현은 `return datetime.now()`로 둔다. `ShadowBot._now()`는 그 주기 REAL의 판단 시각(ctx `candles_asof`)을 돌려준다.
  - 원칙 0-1 해당 여부: (b) `_load_config()` 위임과 같은 종류인 **비매매 위임**으로 판단했다. 기본 구현이 기존 식과 같아서 REAL·가상 봇의 동작은 바뀌지 않는다. 모듈 전역 `datetime`을 호출 시점에 해석하므로 기존 테스트의 시계 고정 패치도 그대로 적용된다.
  - 동치 근거: 회귀 9종이 전부 통과했다. `test_vwap_stage3_shadow.py`의 "REAL 동치" 3건은 다음을 확인한다. ① `_now()`가 패치된 `datetime.now()`와 같다. ② `_loop_step_body`에서 판단 시각을 읽는 곳은 `self._now()` 1곳뿐이고, 남은 `datetime.now()`는 상태 캐시의 `last_updated` 1곳뿐이다. ③ REAL 주기의 세션 시작과 cycle_id가 고정 시계 기준과 같다.
  - 이유: 섀도우는 REAL보다 수 ms~수백 ms 늦게 실행된다. 그 사이에 세션 리셋이나 거래 시작 시각을 넘으면 세션 날짜, 시작 대기, 손실한도 기준일이 REAL과 달라진다. JR-3은 이런 주기를 건너뛰는 방식(`CLOCK_BOUNDARY`)을 썼는데, 그러면 매 세션 경계 주기의 비교가 빠지고 경계 판정 여유(250ms)라는 근거 약한 상수가 남는다. 위임으로 바꾸면 섀도우가 REAL과 **같은 봉, 같은 설정, 같은 시각**으로 판단하므로 건너뛸 이유가 없어진다. `CLOCK_BOUNDARY`는 제거하고, `candles_asof`가 없을 때만 `NO_ASOF`로 건너뛴다.
  - 되돌리기: `self._now()`를 `datetime.now()`로 되돌리고 섀도우에 `CLOCK_BOUNDARY` 건너뛰기를 복원하면 된다(약 1시간).
- **섀도우 건너뜀 규칙 정정**: REAL 주기가 `DATA_UNAVAILABLE`로 끝나도 섀도우는 **실행한다**. 빈 캔들은 `NO_CANDLES`로 따로 걸러진다. 잔고나 미체결 조회 실패는 REAL 쪽 사정이라, 섀도우가 그 주기를 건너뛰면 그 봉의 가상 체결 판정이 영구히 사라져 `SHADOW_UNFILLED` 오탐이 생긴다. 건너뛰는 것은 `DATA_UNTRUSTED`와 `LOOP_ERROR`뿐이다.
- **compare `summary.excluded`(`{"REAL_PANIC": n}`)와 pair의 `excluded_reason`을 계약에 포함한다.** REAL 패닉 청산 주기는 Q1(a)에 따라 섀도우가 일부러 돌지 않는 주기다. 그래서 `REAL_ONLY_ORDER`(버그 신호)로 세면 오탐이 된다. 이 건은 verdict 집계와 `match_rate_pct`에서 빼고 pairs에는 표시한다.
- **섀도우 현금 음수 허용**: REAL 보유 원가가 기준 자본금보다 크면 섀도우 현금은 `기준자본 − 보유원가` 그대로 음수가 된다. 0으로 자르면 청산 뒤 현금(기준자본 + 손익)이 REAL 장부와 어긋난다. 보유 중에는 전략이 BUY를 내지 않으므로 청산 전까지 판단 차이도 없다. 이 경우 `SHADOW_SYNC`를 `warn`, `over_allocated=true`로 남긴다.
- **상태 판단**: Phase C는 구현과 시니어 리뷰를 마쳤지만, QA, UI-2 섀도우 탭 연결, 커밋·배포가 남아 있다. 그래서 상태는 **Proposed를 유지**한다. Phase C/D가 QA를 통과하고 사용자 요청으로 커밋·배포되면 Accepted로 바꾼다. 사용자 결정(Q1(a)·Q2(a))은 이미 확정됐으므로 남은 조건은 배포뿐이다.

---

## 정정 메모 (2026-10-07, ADR-0011 작성 시 추가 — 본문은 수정하지 않음)

- **4단계 ADR 번호 재정정**: 이 문서에서 "4단계(전략 재연구) ADR 은 0011"이라고 적은 부분은 정정한다. 0011 은 [ADR-0011](0011-butler-auth-boundary.md)(Butler 대시보드·API 인증 경계)이 사용했으므로 **4단계 ADR 은 0012** 이다.
