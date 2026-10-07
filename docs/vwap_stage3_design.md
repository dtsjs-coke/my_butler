# VWAP 3단계 구현 명세 — 섀도우 / 리플레이 / 봉 적재 / 전략 플러그인

- 작성: 2026-10-05 (시니어 개발자), 상태: **설계 확정 대기 (ADR-0007 Proposed)**
- 결정 근거: [`docs/adr/0007-vwap-shadow-replay-strategy-plugin.md`](adr/0007-vwap-shadow-replay-strategy-plugin.md), 선행 [ADR-0006](adr/0006-vwap-signal-redesign.md)
- 대상: `C:\Users\user\butler_pjt\dev_pjt\my_butler` (커밋·sync_s9는 사용자 요청 시에만)

---

## 0. 불변 원칙 (모든 작업자 공통)

1. **REAL 매매 결정·주문 코드는 바꾸지 않는다.** REAL `bot.py`/`broker.py`에 허용하는 변경은 아래 4가지뿐이다.
   - (a) post-cycle 훅 호출 1곳
   - (b) `VwapConfigManager.load_config()` 호출을 `self._load_config()` 위임으로 바꾸기
   - (c) `_cycle`/`_order_meta()`에 `cycle_id`·`df`·컨텍스트 저장
   - (d) `TossBroker.last_candles_source` 읽기 전용 속성
   - 그 밖의 REAL 경로 변경은 시니어 리뷰 없이 금지.
   - > **정정 2026-10-07**: 이 원칙의 예외로, 사용자 결정에 따라 [ADR-0009](adr/0009-vwap-start-wait-stop-loss.md)가 `bot.py` 7-1(거래 시작 대기 중 신호 덮어쓰기 범위)과 9-2-0(대기 중 보유 SELL 주기의 매수 미체결 취소)을 바꿨다. 3단계 기능 변경이 아니라 별도 매매 동작 수정이다.
   - > **정정 2026-10-07 (SR-3)**: (b)와 같은 종류의 비매매 위임 1건을 추가했다. `_loop_step_body`의 판단 시각을 `self._now()`로 읽고, 기본 구현은 `datetime.now()`다(REAL 동작은 바뀌지 않는다). `ShadowBot`은 이 메서드에서 REAL의 `candles_asof`를 돌려준다. 근거와 동치 테스트: [ADR-0007 SR-3 메모](adr/0007-vwap-shadow-replay-strategy-plugin.md).
2. 새 기능의 모든 예외는 내부에서 삼키고 이벤트·로그만 남긴다. REAL 루프가 죽거나 지연되면 안 된다. 훅 하나의 소요시간 상한은 기본 2초이고, 넘으면 경고 이벤트를 남긴다.
3. 회귀 테스트 `scripts/test_vwap_reliability.py`(69/69)와 `scripts/test_vwap_transparency.py`(69/69)는 **모든 작업 단위가 끝날 때마다** 통과해야 한다. 운영 `data/` 해시는 전후가 같아야 한다.
4. S9 제약: 순수 pandas/numpy만 쓴다(Numba·pyarrow·yfinance 금지). CPU를 많이 쓰는 작업은 **별도 프로세스**에서 한다. 런타임 파일은 반드시 `data/` 아래, sync 제외 목록에 있는 경로에만 쓴다.
5. 스크래치 venv 실행: `C:\Users\user\AppData\Local\Temp\claude\C--Users-user-orchestrator-pjt\2a53fb36-e1da-4f1d-afa2-203af4a50346\scratchpad\venv`, 환경변수 `PYTHONUTF8=1`.

---

## 1. `backtest/` 패키지 현황과 운영 통합 시 문제점 (실제 코드 확인 결과)

| 파일 | 내용 | 운영에서 재사용 | 문제점 |
|---|---|---|---|
| `engine.py` (390줄) | 봉 단위 엔진(`run_backtest`), `CostModel`, `Decision`/`Position`/`Trade`, `summarize`, `exit_reason_breakdown` | **그대로 import** | ① `summarize()`가 한글 키("승률%" 등)를 반환한다. API에는 영문 키로 매핑해서 내보낸다. ② 전략에 `on_start/on_session_start/on_entry/decide(j, df, pos)`를 요구하고 df에 `session_date`, `vwap`이 꼭 있어야 한다. ③ 고정 명목금액(초기자본×k%)에 정수 주식 수를 쓴다. 운영 예산 로직과 같다(현금 부족 시 축소 로직은 없음). ④ 일 손실한도·부분체결·큐 순서는 모델링하지 않는다(`limit_fill_buffer_pct`로 민감도만 조절). |
| `strategies.py` (330줄) | S0_Current(운영 **재구현본**), S1~S3 | **운영에서 import 금지** | ① S0가 운영 코드와 **별도 구현**이다. 그래서 동치 검증 대상이지 운영 경로가 아니다. ② S1~S3가 `bars_to_session_end`(**미래 정보**, F5)를 써서 실시간·섀도우에 그대로 옮길 수 없다. ③ ADX/RSI 필터와 Dev Band가 없다. |
| `indicators.py` (82줄) | 세션별 VWAP/표준편차, ATR, ADX, RSI, `bar_in_session`, `bars_to_session_end` | **운영에서 import 금지** | ① 세션을 ET 날짜(`session_date`)로 정의한다. 운영은 KST + `reset_time`이다. ② ADX/RSI를 세션 경계에서 끊는다. 운영 `calculate_adx/rsi`는 끊지 않는다. 그래서 같은 봉이라도 값이 다르다. ③ `bars_to_session_end`는 미래 정보다. |
| `data.py` (209줄) | yfinance 다운로드, 7일 분할, 정규장 필터, `backtest/data_cache/` CSV 캐시, IS/OOS 분리 | **운영에서 import 금지** | ① S9에 **yfinance가 없다**(F3, `requirements.txt` 확인). ② 캐시 경로 `backtest/data_cache/`가 sync 제외 대상이 아니다. S9에서 쓰면 다음 sync 때 orphan cleanup으로 지워지고, 로컬 9.4MB 캐시는 S9로 전송된다(F4). ③ 시각을 ET tz-naive로 둔다. 운영은 KST. ④ 정규장만 남긴다. Toss 캔들은 시간외가 섞여 있을 수 있다(S9에서 확인 필요). |
| `validate.py` (172줄) | 18개 자가검증 | 연구용 CLI 유지 | ① 실행할 때 **네트워크(yfinance)가 필수**다(`fetch_intraday("XXXX","1m")`). 오프라인 CI 용도로는 못 쓴다. ② VWAP 일치 검사가 ET 하루치만 비교해서 KST 자정 리셋 버그(F1)를 놓쳤다. |
| `diagnose.py`, `run_compare.py` | 진단·비교 CLI | 연구용 유지 | 운영 무관 |

**결론**: 운영은 `backtest.engine`만 import한다. 데이터 로더, 지표, S0 판단은 운영 코드(`VwapStrategy`)와 새 운영 전용 로더로 공급한다. `backtest/`의 다른 모듈은 연구용으로 그대로 둔다.

---

## 2. 신규·변경 파일 목록

> **정정 2026-10-07** (SR-1 구현 결과 반영): ① `rules.py` 함수 시그니처는 `is_waiting_for_start(now, start_time, reset_time, session: SessionSpec)`이고, 대기 구간 계산 `start_wait_window(...)`와 대기 중 덮어쓰기 범위 `start_wait_blocks(signal, position_qty)`(ADR-0009)가 함께 있다. ② 플러그인 테스트(T-P0~T-P5)는 `scripts/test_vwap_stage3_plugin.py`로 분리했다. `scripts/test_vwap_stage3.py`는 나머지(T-H/T-B/T-S/T-R/T-A) 담당이다.

| 구분 | 파일 | 담당 | 비고 |
|---|---|---|---|
| 신규 | `core/vwap/strategies/__init__.py`, `base.py` | 시니어 | `TradingStrategy`, `StrategySignal`, `StrategyContext`, `PositionView` |
| 신규 | `core/vwap/strategies/s0_current.py` | 시니어 | `S0CurrentStrategy` — `VwapStrategy` 래퍼 |
| 신규 | `core/vwap/strategies/rules.py` | 시니어 | 순수 함수 `is_waiting_for_start(now, start_time, reset_time, session)`, `start_wait_window(...)`, `start_wait_blocks(signal, position_qty)` (bot 7-1 로직과 동치. bot은 아직 이 함수를 쓰지 않음) — *정정 2026-10-07* |
| 신규 | `core/vwap/replay_engine_adapter.py` | 시니어 | `PluginEngineAdapter` (backtest.engine 전략 호환) |
| 신규 | `core/vwap/bars_store.py` | 주니어 | 봉 적재·조회 |
| 신규 | `core/vwap/bar_source.py` | 주니어 | 리플레이용 데이터 로더(bars_store + Yahoo requests 분할 수집 + `data/replay_cache/`) |
| 신규 | `core/vwap/replay.py` | 주니어 | 작업 관리(Popen, 상태 파일, 타임아웃, 보관 5개), 결과 스키마 생성 |
| 신규 | `core/vwap/replay_job.py` | 주니어 | `python -m core.vwap.replay_job <job_id>` 진입점(자식 프로세스) |
| 신규 | `core/vwap/shadow.py` | 주니어(시니어 리뷰 필수) | `StaticCandleBroker`, `ShadowBot(VWAPBot)`, `ShadowRunner`(재동기화·상태 파일), `compare()` |
| 변경 | `core/vwap/bot.py` | **시니어** | 원칙 0-1의 (a)(b)(c)만 |
| 변경 | `core/vwap/broker.py` | **시니어** | `TossBroker.last_candles_source` ("toss"/"yahoo"/"mock") |
| 변경 | `core/vwap/events.py` | 주니어 | `VALID_MODES`에 `VIRTUAL_SHADOW` 추가, `EVENT_TYPES`에 `SHADOW_SYNC` 추가 |
| 변경 | `core/vwap/config_manager.py` | 주니어 | 신규 설정 키 기본값(§7) |
| 변경 | `api/vwap_api.py` | 주니어 | 라우트 5개(§4.4, §5.6), 허용 설정키 추가 |
| 변경 | `api/templates/vwap_dashboard.html` | ui-dev | §10 |
| 변경 | `../sync_manager/sync_s9.py` | 주니어 | 제외 목록 추가(§7). **사용자 승인 후** (다른 프로젝트 파일) |
| 유지 | `core/vwap/backtester.py`, `/vwap/api/backtest` | — | 하위 호환으로 남김. 리플레이가 안정화되면 삭제(별도 작업) |
| 신규 | `scripts/test_vwap_stage3.py` | 작업별 분담 | §11 (T-H/T-B/T-S/T-R/T-A) |
| 신규 | `scripts/test_vwap_stage3_plugin.py` | 시니어 | §11 (T-P0~T-P5) — *정정 2026-10-07: 플러그인 테스트 분리* |
| 문서 | `docs/vwap_user_guide.md` 8장, Obsidian `vwap_system_flow_guide.md` | 오케스트레이터 | 구현 완료 후 |

---

## 3. 전략 플러그인 인터페이스 (시니어)

> **정정 2026-10-07** (SR-1 구현 결과 반영 — 실제 코드가 기준, 아래 본문은 정정된 내용):
> 1. `StrategyContext`에 **`market`** 필드가 있고, 세션 규칙은 **`ctx.session_spec()`**(= `SessionSpec.for_market(market, reset_time, ticker)`, ADR-0008)로 얻는다. `prepare`·세션 날짜·대기 규칙이 모두 이것을 쓴다. 엔진 입력 `session_date`도 `get_session_date(reset_time)`이 아니라 `SessionSpec.session_key` 기준이다.
> 2. `rules` 시그니처는 `is_waiting_for_start(now, start_time, reset_time, session)`이다(§2 정정 참고).
> 3. `evaluate(df, j, pos, ctx, now=None)` — `now`를 생략하면 **j 봉이 마감되는 시각(`time[j] + 봉 간격`)**을 쓴다. 리플레이 엔진은 j 봉 판단을 j+1 봉에 체결시키므로 이 시각이 실시간 봇의 `now`에 해당한다. 실시간 호출자는 현재 시각을 넘긴다. (이전 본문의 `df.time[j]`는 틀림)
> 4. 플러그인 테스트는 `scripts/test_vwap_stage3_plugin.py`로 분리했다(§11).
> 5. (ADR-0009) 대기 중 덮어쓰기는 `rules.start_wait_blocks(signal, pos.qty)`가 True(무보유 또는 BUY)일 때만이다. 보유 중 STOP_LOSS/SELL/HOLD 는 그대로 둔다.

```python
# core/vwap/strategies/base.py
@dataclass(frozen=True)
class StrategyContext:
    ticker: str; market: str; interval: str; reset_time: str; start_time: str   # market: 정정 2026-10-07
    params: dict          # n/m/x/k_percent, use_adx_filter, adx_threshold, use_rsi_filter, rsi_threshold,
                          # use_vwap_band, vwap_band_sigma (설정 dict 에서 접두어 제거한 값)

@dataclass(frozen=True)
class PositionView:
    qty: float = 0.0
    entry_price: float = 0.0   # 비용 미반영 평균 체결가 (운영 holdings.entry_price 와 같은 의미)
    bars_held: int = 0

@dataclass(frozen=True)
class StrategySignal:
    signal: str                 # BUY / SELL / STOP_LOSS / HOLD / WAIT
    reason_code: str
    reason_text: str
    target_buy_price: float
    target_sell_price: float
    stop_loss_price: float
    filters: dict
    indicators: dict            # vwap, adx, rsi, vwap_stdev, current_price
    exit_market: bool = False   # 미래 전략(시간/마감 청산)용. S0 는 항상 False

class TradingStrategy(ABC):
    name: str; version: str
    @classmethod
    def from_config(cls, config: dict, prefix: str) -> "TradingStrategy": ...
    @abstractmethod
    def prepare(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        """인과적 지표를 한 번에 계산해 반환 (미래 행을 참조하는 컬럼 금지)."""
    @abstractmethod
    def evaluate(self, df: pd.DataFrame, j: int, pos: PositionView, ctx: StrategyContext,
                 now: Optional[datetime] = None) -> StrategySignal:
        """prepare 결과의 j행까지만 보고 판단. now 생략 시 j 봉 마감 시각(time[j] + 봉 간격)."""
```

- `S0CurrentStrategy.prepare` = `VwapStrategy.calculate_vwap(df, ctx.reset_time, session=ctx.session_spec())`.
- `S0CurrentStrategy.evaluate` = `VwapStrategy.get_signals(df.iloc[[j]], n, m, x, pos.qty, pos.entry_price, …filters…)`를 `StrategySignal`로 변환. 여기에 `rules.start_wait_blocks(signal, pos.qty)`가 True이고 `rules.is_waiting_for_start(now, ctx.start_time, ctx.reset_time, ctx.session_spec())`가 True면 `WAIT`/`WAIT_START_TIME`으로 덮어쓴다(bot 7-1과 같은 규칙, ADR-0009). `now` 기본값은 j 봉 마감 시각.
- **인과성 계약**: `prepare` 결과의 j행 값은 j 이후 행이 바뀌어도 변하지 않아야 한다. S0는 누적합과 `ewm(adjust=False)`만 쓰므로 만족한다. 테스트로 검증한다(§11 T-P2).
- 알려진 차이: 실시간은 150봉 창으로 RSI/ADX를 매번 새로 계산하고(ewm 초기값이 창마다 다름), 리플레이는 전체 구간으로 한 번 계산한다. VWAP과 표준편차는 세션 누적이라 같다. 리플레이 결과 `assumptions.not_modeled`에 명시한다.
- `PluginEngineAdapter(strategy, ctx)`는 `backtest.engine`이 기대하는 `name/on_start/on_session_start/on_entry/decide`를 구현한다.
  - `decide(j)`가 `evaluate(j, PositionView(pos.qty, pos.entry_raw, pos.bars_held))`를 호출해 매핑한다.
  - 매핑: BUY → `Decision(buy_limit=target_buy)`, SELL → `Decision(sell_limit=target_sell)`, STOP_LOSS → `Decision(exit_market=True, note="STOP_LOSS_MKT")`, HOLD/WAIT → `Decision()`. `exit_market=True`면 → `Decision(exit_market=True, note=reason_code)`.
  - 엔진 입력 df에는 `session_date = ctx.session_spec().session_key(time)`(ADR-0008 `SessionSpec`, 봇과 같은 규칙)를 붙인다. *(정정 2026-10-07: 이전 본문의 `get_session_date(time, reset_time)`는 ADR-0008로 대체)*

---

## 4. 원클릭 리플레이 (주니어 + 시니어 어댑터)

### 4.1 데이터 로더 `bar_source.load_bars(ticker, interval, days, reset_time)`
1. `bars_store.read_range(ticker, interval, start, end)`로 적재분을 먼저 쓴다(Toss 원본).
2. 빠진 날짜는 Yahoo chart API를 `requests`로 직접 받는다. `period1/period2`는 1m 7일 분할(최대 30일), 5m/15m는 59일 분할(최대 60일)이다. `TossBroker._fetch_yahoo_candles`와 같은 시각 변환(UTC→KST, `+9h`)과 같은 User-Agent를 쓴다. 6자리 숫자 티커는 `.KS`, 실패 시 `.KQ`.
3. Yahoo 원본은 `data/replay_cache/{ticker}_{interval}_{fetch_date}.csv`로 캐시한다(같은 날 재사용).
4. 합친 뒤 `time` 기준 중복 제거(적재분 우선), 정렬, NaN과 high<low 제거. 각 행에 `source` 컬럼.
5. 반환 메타: `{"bars", "sessions", "from", "to", "source_breakdown": {"bars_store": n, "yahoo": n}, "gaps": [누락 세션 날짜], "limits": "…"}`.
6. interval별 최대 일수: 1m=30, 5m=60, 15m=60. 넘으면 잘라내고 `warnings`에 남긴다. Toss candles API는 과거 페이징이 확인되지 않아 쓰지 않는다(F6).

### 4.2 작업 실행 (별도 프로세스)
- `replay.start_job(params)`: 실행 중인 작업이 있으면 `JobRunning`을 던진다. 아니면 `job_id = "rp-YYYYmmdd-HHMMSS-xxxx"`를 만들고 `data/replay/<job_id>.json`에 `{state:"queued", params…}`를 쓴 뒤 다음을 실행한다.
  `subprocess.Popen([sys.executable, "-m", "core.vwap.replay_job", job_id], cwd=PROJECT_ROOT, stdout/stderr → data/replay/<job_id>.log)`
- 자식 프로세스는 시작하자마자 `os.nice(10)`(POSIX만, 실패 무시)으로 우선순위를 낮춘다. 진행 단계마다 상태 파일을 원자적으로 저장한다(tmp→replace). 단계: `loading_data` 10%, `preparing` 40%, `simulating` 50~90%, `summarizing` 95%, `done` 100%.
- 부모 쪽(`get_job`): 상태 파일을 읽는다. `running`인데 프로세스가 죽었으면 `failed("process_exited")`, 경과가 `replay_timeout_sec`(기본 300)를 넘으면 kill 후 `failed("timeout")`.
- 보관은 최근 5개다(json+log). 서버 재시작 시 `running` 상태로 남은 작업은 `failed("server_restarted")`로 정리한다.
- 설정은 **요청 시점의 REAL 설정 스냅샷**(`real_*`)을 params에 박아 넣는다. 작업 중에 설정이 바뀌어도 결과가 재현된다.

### 4.3 계산
- `strategy = S0CurrentStrategy.from_config(cfg, "real")`, `df = strategy.prepare(bars, ctx)`, `session_date` 추가
- `run_backtest(df, PluginEngineAdapter(strategy, ctx), CostModel(fee, slip, buffer), initial_equity=real_initial_balance, k_percent=real_k_percent, warmup_bars=30)`
- 기본 비용: 왕복 수수료 0.20%, 슬리피지 0.05%, 버퍼 0(요청으로 바꿀 수 있음)
- Buy&Hold: 같은 봉 구간의 첫 봉(warmup 이후) 시가 매수 → 마지막 종가 매도. 같은 명목금액과 같은 CostModel 적용
- 기대값 판정은 ADR-0006 기준을 따른다. 거래 30건 미만이면 `warnings: "표본 부족 — 판단 불가"`, `t_stat`은 그대로 표시

### 4.4 API
`POST /vwap/api/replay` (admin_required)
```json
요청: {"days": 10, "interval": "1m"(생략 시 real_interval), "fee_roundtrip_pct": 0.2, "slippage_roundtrip_pct": 0.05, "limit_fill_buffer_pct": 0.0}
202: {"status": "accepted", "job_id": "rp-20261005-231500-a1b2"}
409: {"status": "failed", "reason": "job_running", "job_id": "<실행 중 작업>"}
400: {"status": "failed", "reason": "invalid_params", "message": "한글 사유"}
```
`GET /vwap/api/replay/<job_id>` → `{"status":"success","job":{...}}` (없으면 404)
`GET /vwap/api/replay/latest` → 가장 최근 `done` 작업(없으면 `job: null`)

`job` 객체:
```json
{"job_id","state":"queued|running|done|failed","progress":0-100,"stage":"loading_data|preparing|simulating|summarizing|done",
 "message":"한글","error":null|"timeout|process_exited|server_restarted|no_data|exception",
 "created_at","started_at","finished_at","elapsed_sec",
 "params":{"ticker","interval","days","config_snapshot":{...},"fee_roundtrip_pct","slippage_roundtrip_pct","limit_fill_buffer_pct"},
 "result": null | {
   "summary":{"trades","win_rate_pct","avg_win_pct","avg_loss_pct","payoff_ratio","expectancy_pct","expectancy_amount",
              "net_pnl","total_return_pct","mdd_pct","profit_factor","avg_bars_held","t_stat","verdict":"insufficient|negative|inconclusive|positive"},
   "benchmark":{"buy_hold_return_pct","buy_hold_net_pnl"},
   "equity":[{"t":"YYYY-MM-DD HH:MM","strategy":float,"buy_hold":float}],   // 최대 500점으로 다운샘플
   "trades":[{"entry_time","exit_time","entry_price","exit_price","qty","net_pnl","ret_pct","bars_held","exit_reason","fees"}],  // 최근 500건
   "exit_reasons":[{"reason","count","avg_ret_pct","sum_ret_pct"}],
   "data":{"bars","sessions","from","to","interval","source_breakdown":{"bars_store","yahoo"},"gaps":[],"limits":"한글 설명"},
   "assumptions":{"fee_roundtrip_pct","slippage_roundtrip_pct","limit_fill_buffer_pct","fill_model":"보수적(다음 봉 체결, 갭은 시가, 손절 우선)","notional",
                  "not_modeled":["일 손실한도 패닉","부분체결/호가 대기열","취소·정정 지연","시간외 거래 여부 차이(Yahoo vs Toss)","지표 계산 창 차이(150봉 vs 전체)"]},
   "warnings":["한글"]}}
```
`verdict` 규칙: trades<30 → `insufficient`, t_stat≤−2 → `negative`, t_stat≥2 그리고 기대값>0 → `positive`, 그 외 `inconclusive`.

### 4.5 성능 예산 (구현자가 실측해 보고)
- 1m 30일은 약 8천 봉(정규장)~2만 봉(시간외 포함)이다. `calculate_vwap`은 iterrows라서, 1행 `get_signals` 호출과 함께 PC 측정 후 S9 추정치를 보고한다.
- 목표: S9 기준 300초 이내, 자식 프로세스 RSS 200MB 이하. 넘으면 시니어에게 보고한다. 대응책은 S0 `evaluate`의 배열 기반 경로이며, 동치 테스트 필수.

### 4.6 모델링 범위
- 모델링함: 지정가(VWAP±N/M% 또는 Dev Band), ADX/RSI 필터, 시장가 손절, 시작시각 대기, k% 예산, 비용
- 모델링 안 함: `assumptions.not_modeled` 목록. 화면에 반드시 표시한다.

---

## 5. 섀도우 모드 (주니어 구현, 시니어 리뷰 필수)

### 5.1 REAL 봇 훅 (시니어가 직접)

> **정정 2026-10-07** (SR-2 구현 결과 — 실제 코드가 기준):
> 1. 등록 API는 `bot.add_post_cycle_hook(name, fn)` / `remove_post_cycle_hook(name)`이다(같은 이름은 교체). `_post_cycle_hooks`는 `[(name, fn)]`. `bars_store`는 `VWAPBot.__init__`에서 모든 모드에 기본 등록되고, 하위 클래스가 `ATTACH_BARS_STORE = False`로 끌 수 있다(ShadowBot은 반드시 False — 같은 봉을 두 번 적재하지 않게).
> 2. ctx에 `market`(봇 설정값), `candles_asof`(캔들 요청 직전 시각), `running`이 추가됐다. `config`는 그 주기 설정 dict(평탄 키, `bars_store_enabled` 포함)의 복사본이며 비밀값(`toss_client_id/secret/account_seq`, `admin_password_hash`)은 빈 문자열이다. `df`·`config`·`position`은 훅마다 복사본이다.
> 3. 지연·예외 이벤트는 `ERROR`(warn, 알림 없음), `reason_code`가 `HOOK_SLOW`/`HOOK_ERROR`, 같은 훅·같은 예외 종류는 10분에 1회. 마지막 주기 훅별 소요시간은 `bot.last_hook_durations_ms`.
> 4. `cycle_id`는 `calculate_vwap` 후 마지막 행 시각(KST naive)이다. 추적 주문·거래 레코드·`ORDER_PLACED/REPLACED`·`FILL` 이벤트, 그리고 그 주기의 모든 이벤트 data(`_reason_data`)에 들어간다. 지정가 체결 레코드의 `cycle_id`는 **주문을 낸 주기**다(체결을 판정한 주기가 아님).
- `_loop_step` 끝(정상·예외 경로 모두, `_finish_cycle` 다음)에 `self._run_post_cycle_hooks()`를 호출한다.
- 훅 목록은 `self._post_cycle_hooks: list[callable(ctx)]`다. 모든 모드에 `bars_store.hook`이 붙고, REAL만 `shadow_runner.hook`이 붙는다(`api/vwap_api.py`에서 등록, `shadow_enabled` 설정 존중).
- 훅 컨텍스트 `ctx`(읽기 전용 dict): `{"mode", "generation", "cycle_id", "df"(캔들 원본, 훅마다 .copy()), "ticker", "interval", "reset_time", "candles_source", "config"(그 주기 설정 dict), "position":{"qty","entry_price"}|None, "cash"|None, "reason_code"}`
- 각 훅은 try/except로 감싸고 경과시간을 잰다. 2초를 넘으면 `ERROR`(warn, 알림 없음, 10분 억제) 이벤트를 남긴다.
- `cycle_id` = 이번 주기 df 마지막 행의 `time`(문자열 `YYYY-MM-DD HH:MM:SS`). `_order_meta()`에 포함되어 tracked 주문과 거래 레코드(`cycle_id` 필드)에 저장된다.

### 5.2 `ShadowBot(VWAPBot)` — mode `VIRTUAL_SHADOW`
> **정정 2026-10-07 (SR-3, 실제 코드가 기준)**:
> 1. 시계: `ShadowBot._now()`가 REAL의 `candles_asof`를 돌려준다. 그래서 세션 경계 주기도 건너뛰지 않는다(JR-3의 `CLOCK_BOUNDARY`는 제거). `candles_asof`가 없으면 `NO_ASOF`로 건너뛴다.
> 2. 건너뜀: `DATA_UNTRUSTED`·`LOOP_ERROR`·REAL 정지(`running=false`)·빈 캔들·비신뢰 출처·기준자본 없음·`NO_ASOF`·섀도우 자체 패닉 후 같은 세션·`POSITION_UNKNOWN`(재동기화가 필요한데 REAL 보유를 모를 때). REAL `DATA_UNAVAILABLE` 주기는 **실행한다**.
> 3. 실주문 방어: 실행 전과 후에 시세 소스(`real_broker`)와 `VirtualBroker.source_broker`가 같은 `StaticCandleBroker`인지 확인한다. 아니면 섀도우 봇을 폐기하고(`BROKER_SWAPPED`) 다음 주기에 `FIRST_SYNC`로 다시 맞춘다.
> 4. 재동기화 사유: `FIRST_SYNC`/`BOT_START`(generation 또는 프로세스 `boot_id` 변경)/`SESSION_START`/`CONFIG_CHANGE`/`REENABLED`. REAL 보유 원가가 기준자본보다 크면 현금이 음수가 된다(자르지 않음). 이때 `SHADOW_SYNC`는 `warn`, `over_allocated=true`.
- `_load_config()` 오버라이드: 훅에서 받은 REAL 설정을 복사한 뒤 `real_X` → `virtual_shadow_X`로 키를 매핑해 반환한다. 비밀값은 빈 문자열로 둔다.
- 시세: `self.real_broker = StaticCandleBroker(df)`로 미리 넣어둔다. `client_id/secret/account_seq` 속성을 매핑된 설정값(빈 문자열)과 맞춰서 bot 본체가 TossBroker를 새로 만들지 않게 한다. `get_candles`는 주입된 df를 돌려주고, 네트워크 메서드는 호출되면 예외를 낸다(테스트로 0회 보장).
- 알림: `_refresh_notify_flag` 오버라이드 → 항상 False.
- 실행: `ShadowRunner.hook(ctx)`가 `shadow_bot.running=True` 상태에서 `shadow_bot._loop_step()`를 **동기 1회** 호출한다(자체 스레드 없음). 가상 경로에는 sleep이 없어 수 ms~수백 ms 정도다.
- 로거: `trading_bot_virtual_shadow.log`(`*.log`는 sync 제외).

### 5.3 원장 동기화 `ShadowRunner`
- 상태 파일 `data/vwap_shadow_state.json`(원자 저장): `{"real_generation", "session_date", "cash", "holdings", "open_orders", "order_meta", "synced_at", "sync_reason"}`
- 다음 경우에 재동기화한다.
  - ① REAL `generation`이 바뀜(BOT_START)
  - ② `get_session_date(now, reset_time)`이 바뀜
  - ③ ticker 또는 initial_balance가 바뀜
- 재동기화 내용:
  - `cash = real_initial_balance − qty×entry_price`
  - `holdings = {ticker: {qty, entry_price}}`(REAL 보유. 조회 실패 주기면 다음 주기로 미룸)
  - `open_orders=[]`
  - `SHADOW_SYNC` 이벤트(data: 사유, real 보유, 이전 섀도우 보유)를 남긴다.
- 매 섀도우 주기 후 VirtualBroker 상태(cash/holdings/open_orders/order_meta)를 상태 파일에 저장한다. 프로세스 재시작 시 VirtualBroker를 만든 직후 상태 파일로 덮어쓴다(trades 재구성 결과 대신). 이유: 재동기화분이 trades에 없기 때문.

### 5.4 비교 알고리즘 `shadow.compare(days)`
- 입력: REAL·섀도우 각각의 이벤트(`ORDER_PLACED`, `ORDER_REPLACED`, `FILL`)와 거래 레코드(최근 days일)
- 주문 키: `(cycle_id, side)`. 지정가는 ORDER_PLACED/REPLACED 이벤트로, 시장가 청산은 FILL(side=STOP_LOSS)의 `cycle_id`로 잡는다.
- 체결 여부: 같은 order_id(=trade_id)의 FILL이 있는지
- 판정(verdict):

| 조건 | verdict |
|---|---|
| 양쪽 주문, 양쪽 체결, \|체결가 차이\|/가격 ≤ `shadow_price_tolerance_pct`(기본 0.05%) | `MATCH` |
| 양쪽 주문, 양쪽 체결, 차이 초과 | `PRICE_GAP` |
| 양쪽 주문, 섀도우만 체결 | `REAL_UNFILLED` |
| 양쪽 주문, REAL만 체결 | `SHADOW_UNFILLED` |
| 양쪽 주문, 둘 다 미체결 | `BOTH_UNFILLED` (요약에서만 집계, 목록 기본 숨김) |
| REAL만 주문 | `REAL_ONLY_ORDER` (상태 불일치·버그 신호) |
| 섀도우만 주문 | `SHADOW_ONLY_ORDER` |

- 요약: 모드별 `trades, fills, net_pnl`(REAL은 pnl−commission, 섀도우는 pnl−추정수수료), `win_rate_pct, avg_slippage_pct`, verdict별 건수, `match_rate_pct`(MATCH / 양쪽 체결 쌍)

### 5.5 데이터 파일
`data/vwap_trades_virtual_shadow.json`, `data/vwap_events_virtual_shadow.jsonl(.1)`, `data/vwap_shadow_state.json(.tmp)`. 앞의 둘은 기존 sync 제외 패턴에 포함된다. state 파일은 §7에서 추가한다.

### 5.6 API
> **정정 2026-10-07 (SR-3)**: 최종 계약은 아래 예시에 다음 필드를 더한 것이다. `status`에 `session_date`, `halted`, `skip`(`{reason,text,at,cycle_id}|null`)이 있고, `cash`는 음수일 수 있다. `compare`에는 최상위 `ticker`(현재 REAL 설정, UI 통화 판정용)와 `tickers`, `summary.real.unkeyed_fills`, `summary.excluded`(`{"REAL_PANIC": n}`), `summary.definitions`가 있다. pair에는 선택 필드 `excluded_reason`(`"REAL_PANIC"`)이 붙는다. excluded 건은 verdict 집계와 일치율에서는 빠지고 pairs에는 보인다. 섀도우 초기화에 실패하면 두 API 모두 `503 {"status":"failed","reason":"shadow_unavailable"}`를 돌려준다.
`GET /vwap/api/shadow/status`
```json
{"status":"success","shadow":{"enabled":bool,"following":"REAL","real_running":bool,"last_cycle_id","last_run_at","last_duration_ms",
  "synced_at","sync_reason","ticker","position":{"qty","entry_price"},"cash","reason_code","reason_text","signal"}}
```
`GET /vwap/api/shadow/compare?days=7` (days 1~30, 기본 7)
```json
{"status":"success","period":{"from","to","days"},
 "summary":{"real":{"trades","fills","net_pnl","win_rate_pct","avg_slippage_pct"},
            "shadow":{"trades","fills","net_pnl","win_rate_pct","avg_slippage_pct","fee_roundtrip_pct"},
            "verdicts":{"MATCH":n,"PRICE_GAP":n,"REAL_UNFILLED":n,"SHADOW_UNFILLED":n,"BOTH_UNFILLED":n,"REAL_ONLY_ORDER":n,"SHADOW_ONLY_ORDER":n},
            "match_rate_pct","net_pnl_gap"},
 "pairs":[{"cycle_id","side","verdict",
           "real":{"order_id","order_price","fill_price","filled_at","qty"}|null,
           "shadow":{"order_id","order_price","fill_price","filled_at","qty"}|null,
           "price_gap_pct","time_gap_sec"}]}   // 최신순, 최대 300, BOTH_UNFILLED 제외
```
섀도우 이벤트·거래는 기존 `/vwap/api/events?mode=VIRTUAL_SHADOW`로도 조회된다(`VALID_MODES` 추가).

---

## 6. 봉 데이터 적재 `bars_store` (주니어)

> **정정 2026-10-07** (SR-2 연결): ① 마지막 행은 `asof`(ctx `candles_asof`)가 있고 `봉 시각 + 봉 간격 + 60초 ≤ asof`면 마감으로 보고 저장한다(세션 마지막 봉 누락 방지). 그 외에는 이전처럼 제외. ② `hook`은 ctx `market`을 쓰고(없을 때만 티커로 추론), 출처가 `toss`/`yahoo`가 아니면(`mock` 난수 봉, 출처 불명) 적재하지 않는다. ③ `TossBroker.last_candles_source`는 mock_mode에서 Yahoo가 성공하면 `yahoo`, 난수 봉일 때만 `mock`이다.

- `append_closed_bars(ticker, interval, df, reset_time, source)`
  - df의 **마지막 행은 제외**한다(진행 중 봉일 수 있음).
  - `(ticker, interval)`별 메모리 `last_saved_time`보다 큰 행만 기록한다. 처음 호출 시에는 해당 세션 파일 마지막 줄에서 복원한다.
  - 행마다 `get_session_date(time, reset_time)`으로 파일을 나눈다 *(정정 2026-10-07: ADR-0008 이후 `SessionSpec.label_bars` 기준(bars_store 구현과 동일))*: `data/bars/{TICKER}_{interval}_{YYYY-MM-DD}.csv`(헤더 `time,open,high,low,close,volume,source`)
  - 모듈 Lock을 쓰고, 예외는 삼키되 경고 로그는 10분에 1회.
- `read_range(ticker, interval, start_date, end_date) -> DataFrame`: 파일을 합치고, 손상된 줄은 건너뛰고, `time` 기준 중복 제거(마지막 값 유지)와 정렬
- `hook(ctx)`: `ctx["df"]`가 비어 있지 않을 때만 동작하고, `candles_source`를 source로 쓴다. `bars_store_enabled=false`면 아무것도 하지 않는다.
- `TossBroker.last_candles_source`: `get_candles`가 반환 직전에 `"toss" | "yahoo" | "mock"`으로 설정한다(시니어 작업, 반환값·동작 무변경).

---

## 7. 설정 키 / 파일 / sync 제외

> **정정 2026-10-07**: 서버 설정 키 `ui_show_legacy_virtual`은 **삭제 확정**(QA 권고, `config_manager.py`에서 제거, 코드 참조 0건). 레거시 가상봇(V1~V3) 표시 토글은 브라우저 `localStorage`의 `vwap_ui_show_legacy_virtual`만 사용하며 서버 키는 없다. 아래 표와 bool 접미어 목록에서 해당 항목을 뺐다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `shadow_enabled` | `true` | REAL 훅에서 섀도우 실행 |
| `shadow_fee_roundtrip_pct` | `0.2` | 섀도우 순손익 추정 수수료 |
| `shadow_price_tolerance_pct` | `0.05` | MATCH 판정 허용 가격차 |
| `bars_store_enabled` | `true` | 봉 적재 |
| `replay_timeout_sec` | `300` | 리플레이 자식 프로세스 제한시간 |

- bool 키는 vwap_api 허용키 변환에서 `discord_notify`처럼 bool로 처리하도록 접미어 목록에 `_enabled`를 추가한다.
- **sync 제외 추가**(`sync_manager/sync_s9.py`): `data/replay`, `data/replay/*`, `data/replay_cache`, `data/replay_cache/*`, `data/vwap_shadow_state.json`, `data/vwap_shadow_state.json.tmp`. 함께 권장: `backtest/data_cache`, `backtest/data_cache/*`(F4. 9.4MB 전송 방지와 S9 런타임 캐시 삭제 방지). `data/bars`는 이미 추가됨. **다른 프로젝트 파일이므로 사용자 승인 후 진행.**

---

## 8. 이번 조사에서 발견한 기존 문제 (3단계 범위 밖 — 별도 결정 필요)

1. **[중요·매매 영향] VWAP KST 자정 리셋 (F1)**
   - 현상: `calculate_vwap`이 날짜 변경 시 세션을 끊는다. 봉 시각이 KST이면 미국장 도중(00:00 KST)에 VWAP 누적이 초기화된다.
   - 결과: 자정 이후 VWAP, 매수·매도 타겟, Dev Band가 그 시점부터 다시 계산된다.
   - 스크래치 재현: 00:00 봉 VWAP 기대 103.0 → 실제 106.0.
   - 영향 범위: REAL·가상·리플레이 전부. ADR-0006 실거래 표본 해석에도 영향 가능.
   - 확인 필요: S9에서 Toss 캔들 timestamp 시간대(오프셋)를 확인해야 한다. 3단계 봉 적재(`data/bars`)로도 확인할 수 있다.
   - 수정은 **매매 동작 변경**이므로 사용자 승인과 별도 ADR이 필요하다.
2. **서머타임 종료(2026-11-01) 후 `reset_time` 22:30 고정 (F2)**: 정규장 시작이 23:30 KST로 바뀐다. 설정값을 바꾸거나 시간대 기반 자동 계산이 필요하다. 매매 동작 변경이라 사용자 결정 사항이다.
3. **S9 `requirements.txt`에 pandas/numpy 미기재 (F3)**: 봇이 돌고 있으니 설치는 돼 있을 것이다. freeze 갱신을 권장한다(운영 재설치 시 위험).
4. **`backtest/data_cache` sync 문제 (F4)**: §7 제외 추가로 해결한다.

---

## 9. 사용자에게 확인할 결정 사항

| # | 질문 | 선택지 | 추천 |
|---|---|---|---|
| Q1 | REAL이 **정지 중**일 때도 섀도우를 돌릴까? | (a) REAL과 함께만 동작 (b) REAL 정지 중에도 독립 루프로 동작 | **(a)**. 섀도우는 REAL과의 비교가 목적이다. REAL 없이 전략을 보려면 리플레이가 더 정확하다(보수적 체결·비용). (b)는 A1의 단점(별도 시세 호출, 시각 불일치)을 다시 가져온다. |
| Q2 | REAL `bot.py`에 훅·위임·`cycle_id` 같은 **비매매 변경**을 허용할까? | (a) 허용(ADR-0007 D3) (b) REAL 파일 0줄 변경. 섀도우는 독립 인스턴스(A1) | **(a)**. 같은 봉 비교가 핵심이다. 변경은 매매 결정 밖의 3곳이고 회귀 69+69 + 격리 테스트로 보증한다. |
| Q3 | VWAP 자정 리셋(§8-1)을 언제 다룰까? | (a) 3단계와 별개로 즉시 조사·수정 ADR (b) 봉 적재로 증거를 모은 뒤 4단계에서 (c) 그대로 둠 | **(a) 조사는 즉시, 수정은 승인 후**. S9에서 timestamp 시간대만 확인하면 영향 여부가 확정된다. 영향이 있으면 지금 실거래 VWAP 타겟이 자정 이후 잘못 계산되고 있는 것이다. |
| Q4 | 서머타임 종료(11/1) 대응 | (a) 11/1 전에 `real_reset_time`을 23:30으로 수동 변경 (b) 자동 계산 기능 추가(4단계) | **(a) + 리마인더**. 코드 변경 없이 설정만으로 가능하다. |
| Q5 | sync 제외 목록 추가(§7) — sync_manager 수정 | 승인 / 보류 | **승인**. 안 하면 리플레이 결과·섀도우 상태가 sync 때 지워진다. |

---

## 10. UI 요구사항 (ui-dev)

> **정정 2026-10-07**: 1번 "V1~V3 숨김"의 토글 상태는 서버 설정 키가 아니라 브라우저 `localStorage`(`vwap_ui_show_legacy_virtual`)에만 저장한다. 서버 키 `ui_show_legacy_virtual`은 없다(§7 정정).

공통: 기존 디자인 토큰과 탭 구조를 유지한다. 외부 차트 라이브러리는 새로 넣지 않는다(현재 대시보드는 Tailwind+lucide만 쓴다). 그래프는 **인라인 SVG polyline**으로 그린다. 모든 숫자에 통화 표기 규칙(6자리 숫자 티커 ₩, 그 외 $)을 적용한다.

1. **V1~V3 숨김**: 브라우저 `localStorage`의 `vwap_ui_show_legacy_virtual`이 켜져 있지 않으면 가상 1~3 탭과 종합 가상 잔고 카드를 숨긴다(기본 숨김). 설정 화면에 "구버전 가상봇 표시" 토글을 둔다. 서버 설정 키는 없고 API·데이터는 그대로.
2. **섀도우 패널** (실거래 탭 안 또는 새 "실거래 vs 섀도우" 탭)
   - 상단 상태: "실거래 설정을 자동으로 따라가는 중" 문구, 마지막 실행 시각, 마지막 동기화 시각과 사유, 섀도우 판단 사유(`reason_text`)
   - 좌우 비교 카드(실거래 | 섀도우): 거래수, 체결수, 순손익, 승률, 평균 슬리피지%. 차이(net_pnl_gap)는 강조색
   - 판정 요약 배지: MATCH / PRICE_GAP / REAL_UNFILLED / SHADOW_UNFILLED / REAL_ONLY_ORDER / SHADOW_ONLY_ORDER 건수와 일치율
   - 괴리 목록 표: 시각(cycle_id), 매수/매도, 실거래(주문가→체결가), 섀도우(주문가→체결가), 가격차%, 시간차, 판정 배지. 기간 선택 1/7/30일
   - verdict 한글 라벨: MATCH "일치", PRICE_GAP "체결가 차이", REAL_UNFILLED "실거래만 미체결", SHADOW_UNFILLED "섀도우만 미체결", REAL_ONLY_ORDER "실거래만 주문", SHADOW_ONLY_ORDER "섀도우만 주문"
   - 빈 상태: "실거래 봇이 가동되면 같은 시각에 섀도우가 함께 돌며 기록이 쌓입니다."
3. **리플레이 탭** (기존 "과거 백테스트" 탭 교체)
   - 입력은 기간 선택(1/3/5/10/20/30일. 5m·15m면 최대 60일)과 **버튼 하나** "현재 실거래 설정으로 리플레이"가 전부다. 고급 설정(접힘)에는 수수료·슬리피지·체결 버퍼.
   - 실행 후 1.5초 간격으로 폴링해 진행률 바와 단계 문구를 표시한다. 409면 "이미 실행 중인 리플레이가 있습니다"와 함께 그 작업을 이어서 폴링한다.
   - 결과:
     - ① 판정 배너(`verdict`: 판단 불가/음수/불확실/양수 + 한 줄 설명)
     - ② KPI 카드(순손익, 수익률, 기대값/거래, 승률, MDD, 거래수, t-stat)와 **Buy&Hold 대비**
     - ③ 손익곡선 SVG(전략 vs Buy&Hold 2선)
     - ④ 청산 사유 표
     - ⑤ 거래 목록 표(최근 500)
     - ⑥ "데이터·가정" 접이식 패널(source_breakdown, gaps, limits, assumptions.not_modeled, warnings)을 **항상 노출**
   - 페이지를 열면 `GET /replay/latest`로 마지막 결과를 보여준다.
4. API 연동 전에는 §4.4·§5.6 예시 JSON으로 목업 개발이 가능하다(필드명 고정).

---

## 11. 테스트 계획 — `scripts/test_vwap_stage3.py` (네트워크 없음, 1단계 하네스 재사용)

> **정정 2026-10-07**: 플러그인 항목(T-P0~T-P5)은 `scripts/test_vwap_stage3_plugin.py`로 분리했다. T-P0(설정 해석), T-P5(ADR-0009 대기 중 포지션 보호)가 추가됐고, T-P1c(실제 봇 `_loop_step` 대조)와 T-P3의 거래 시작 대기 항목이 있다. 나머지 항목은 `scripts/test_vwap_stage3.py`에 있다.

| ID | 항목 | 담당 |
|---|---|---|
| T-P1 | `S0CurrentStrategy.evaluate`의 signal/target/stop/reason이 `VwapStrategy.get_signals`(같은 창)와 일치. 무작위 경로 200개 × 필터·밴드 on/off 조합 | 시니어 |
| T-P2 | 인과성: j 이후 행을 훼손해도 `prepare` 결과 j행과 `evaluate(j)`가 불변 | 시니어 |
| T-P3 | 엔진 동치: 합성 데이터(세션 경계 일치, 필터·밴드 off)에서 `PluginEngineAdapter(S0)` 거래 목록 == `backtest.strategies.S0_Current` 거래 목록 | 시니어 |
| T-P4 | `rules.is_waiting_for_start`가 bot 7-1 로직과 같음(자정 경계·잘못된 형식 포함) | 시니어 |
| T-P5 | (ADR-0009) 대기 중 실제 봇 == 플러그인(보유/무보유), 가상·REAL mock 에서 보유 손절 시장가 청산 / 무보유 BUY 차단 / 미체결 정리 — *추가 2026-10-07* | 시니어 |
| T-H1 | 훅 격리: 예외를 던지는 훅, 3초 지연 훅이 있어도 REAL `placed/canceled` 주문 시퀀스가 훅 없을 때와 **완전히 같음**. 경고 이벤트 1회 | 시니어 |
| T-H2 | `cycle_id`가 tracked 주문·REAL 거래 레코드·ORDER_PLACED 이벤트에 기록됨 | 시니어 |
| T-B1 | bars_store: 마감봉만, 150봉 겹치는 입력 10회 → 중복 0, 세션 날짜 분할, 재시작 후 복원, 손상 줄 무시, 쓰기 실패 무해, 동시 기록 | 주니어 |
| T-S1 | ShadowBot: 주입 df로 REAL과 같은 cycle_id 주문, StaticCandleBroker 네트워크 호출 0회, 알림 0회 | 주니어 |
| T-S2 | 재동기화: generation 변경 / 세션 변경 / ticker 변경 → SHADOW_SYNC 이벤트, 원장 = REAL 보유. 재시작 후 상태 파일 복원 | 주니어 |
| T-S3 | compare: 7가지 verdict 각각을 만드는 합성 시나리오, 요약 수치, days 필터 | 주니어 |
| T-R1 | bar_source: 적재분 우선 병합, Yahoo 응답 mock(requests 패치)으로 분할 요청 횟수·기간, 캐시 재사용, gaps 산출 | 주니어 |
| T-R2 | replay 작업: 합성 봉으로 자식 프로세스 실행 → done, 진행률 단조 증가, 409 동시성, 타임아웃 kill, 프로세스 사망 처리, 보관 5개 | 주니어 |
| T-R3 | 결과 스키마 필드 전부 존재, equity ≤500점, verdict 규칙, B&H 계산 | 주니어 |
| T-A1 | API 5개: 401(세션 없음), 정상, 파라미터 검증(400), 404 | 주니어 |
| T-G | `test_vwap_reliability.py` 69/69, `test_vwap_transparency.py` 69/69, 운영 `data/` 해시 동일, `trading_bot_*.log` 신규 생성 없음 | 전원 |
| T-PERF | 1m 30일 상당 합성 2만 봉 리플레이 소요시간·메모리 측정 보고 (PC 실측 + S9 첫 실행 로그) | 주니어 |

`backtest/validate.py`(네트워크 필요)는 엔진 파일을 바꾸지 않으므로 이번 단계 필수 항목이 아니다. 엔진을 바꾸게 되면 다시 돌린다.

---

## 12. 작업 분할과 순서

```
[Phase 0] 사용자 결정 Q1~Q5 확인 + S9 배포 종료 확인 (오케스트레이터)
   │
[Phase A] ─ 병렬 가능 ───────────────────────────────────────────────
   ├─ SR-1 (시니어)  core/vwap/strategies/* + rules.py + replay_engine_adapter.py + T-P1~P4
   ├─ JR-1 (주니어)  bars_store.py(훅 함수 포함, 아직 미연결) + events/config 키 추가 + T-B1
   └─ UI-1 (ui-dev)  V1~V3 숨김 토글 + 섀도우 패널/리플레이 탭 목업(§4.4·§5.6 예시 JSON)
   │
[Phase B] (SR-1 완료 후)
   ├─ SR-2 (시니어)  bot.py 훅·_load_config 위임·cycle_id + broker.last_candles_source + T-H1/H2 + 회귀 2종
   │                 → 이 시점부터 bars_store 훅 연결(REAL/가상 공통)
   └─ JR-2 (주니어)  bar_source.py + replay.py + replay_job.py + 리플레이 API + T-R1~R3, T-A1(리플레이), T-PERF
   │                 (JR-2 는 SR-1 에만 의존, SR-2 와 병렬 가능)
[Phase C] (SR-2 완료 후)
   └─ JR-3 (주니어)  shadow.py(ShadowBot/Runner/compare) + 섀도우 API + 훅 등록 + T-S1~S3, T-A1(섀도우)
   │                 → SR-3 (시니어) JR-3 코드리뷰 (REAL 경로 영향 집중 검토)
[Phase D]
   ├─ UI-2 (ui-dev)  실제 API 연동 (리플레이는 JR-2 후, 섀도우는 JR-3 후)
   ├─ JR-4 (주니어)  sync_s9 제외 목록 (Q5 승인 시)
   └─ QA  (qa-reviewer) 전체 검증 → 문서(user_guide 8장, Obsidian) → 배포는 사용자 요청 시
```

- 시니어가 직접 하는 일(SR-1/2/3): 인터페이스 설계, REAL 경로를 건드리는 변경, 동치·격리 테스트, 섀도우 리뷰. 이유: 되돌리기 어렵거나 실거래에 영향이 있는 부분이다.
- 주니어 작업(JR-1~4)은 이 문서의 명세만으로 구현할 수 있다. 막히면 추측하지 말고 오케스트레이터를 거쳐 질문한다.
- 각 단위가 끝날 때마다 T-G(회귀 2종 + data 해시)를 실행하고 결과를 보고에 원문으로 붙인다.
