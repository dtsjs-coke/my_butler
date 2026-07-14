import sys
import os
import time

# 임포트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from core.vwap.config_manager import VwapConfigManager
from core.vwap.strategy import VwapStrategy
from core.vwap.broker import TossBroker, VirtualBroker

# 실제 대시보드의 VIRTUAL_1/2/3, REAL 장부와는 완전히 분리된 전용 테스트 모드.
# vwap_trades_virtual_test.json 파일에만 기록되며, vwap_config.json은 절대 저장하지 않는다.
TEST_MODE = "VIRTUAL_TEST"


def load_default_params() -> dict:
    """대시보드의 VIRTUAL_1 설정값을 초기값으로 불러옵니다 (세션 동안만 수정 가능, 파일 저장 안 함)."""
    config = VwapConfigManager.load_config()
    return {
        "ticker": config.get("virtual_1_ticker", "AAPL"),
        "market": config.get("virtual_1_market", "US"),
        "interval": config.get("virtual_1_interval", "1m"),
        "reset_time": config.get("virtual_1_reset_time", "22:30"),
        "n_percent": float(config.get("virtual_1_n_percent", 1.0)),
        "m_percent": float(config.get("virtual_1_m_percent", 1.0)),
        "x_percent": float(config.get("virtual_1_x_percent", 2.0)),
        "k_percent": float(config.get("virtual_1_k_percent", 10.0)),
        "initial_balance": float(config.get("virtual_1_initial_balance", 10000000.0)),
        "use_adx_filter": bool(config.get("virtual_1_use_adx_filter", False)),
        "adx_threshold": float(config.get("virtual_1_adx_threshold", 25.0)),
        "use_rsi_filter": bool(config.get("virtual_1_use_rsi_filter", False)),
        "rsi_threshold": float(config.get("virtual_1_rsi_threshold", 30.0)),
        "use_vwap_band": bool(config.get("virtual_1_use_vwap_band", False)),
        "vwap_band_sigma": float(config.get("virtual_1_vwap_band_sigma", 2.0)),
    }


def build_real_broker() -> TossBroker:
    """시세 조회 전용 소스 브로커. API 키가 없으면 TossBroker가 자동으로 Yahoo/Mock 폴백합니다."""
    config = VwapConfigManager.load_config()
    return TossBroker(
        client_id=config.get("toss_client_id", ""),
        client_secret=config.get("toss_client_secret", ""),
        account_seq=config.get("toss_account_seq", "")
    )


def get_virtual_broker(state: dict) -> VirtualBroker:
    """초기 가상 자본금이 바뀌면 실봇(bot.py)과 동일하게 가상 브로커를 재생성합니다."""
    vb = state.get("_virtual_broker")
    if vb is None or vb.initial_balance != state["params"]["initial_balance"]:
        vb = VirtualBroker(
            initial_balance=state["params"]["initial_balance"],
            ticker_source_broker=state["_real_broker"],
            mode=TEST_MODE
        )
        state["_virtual_broker"] = vb
    return vb


def _edit_float(label: str, current: float) -> float:
    raw = input(f"{label} [{current}]: ").strip()
    if not raw:
        return current
    try:
        return float(raw)
    except ValueError:
        print("   ⚠️ 숫자가 아니어서 기존 값을 유지합니다.")
        return current


def _edit_bool(label: str, current: bool) -> bool:
    raw = input(f"{label} y/n [{'y' if current else 'n'}]: ").strip().lower()
    if raw == 'y':
        return True
    if raw == 'n':
        return False
    return current


def print_conditions(params: dict):
    print("\n" + "=" * 50)
    print(" 현재 설정된 가상매매 조건 (VIRTUAL_TEST 전용 샌드박스)")
    print("=" * 50)
    print(f" 종목(ticker)        : {params['ticker']} ({params['market']})")
    print(f" 캔들 주기(interval) : {params['interval']}")
    print(f" VWAP 리셋 시각      : {params['reset_time']}")
    print(f" 매수 이격률 N%      : {params['n_percent']}")
    print(f" 매도 이격률 M%      : {params['m_percent']}")
    print(f" 손절 비율 X%        : {params['x_percent']}")
    print(f" 투자 비중 K%        : {params['k_percent']}")
    print(f" 초기 가상 자본금    : {params['initial_balance']:,.0f}")
    print(f" ADX 필터            : {'사용' if params['use_adx_filter'] else '미사용'} (임계값 {params['adx_threshold']})")
    print(f" RSI 필터            : {'사용' if params['use_rsi_filter'] else '미사용'} (임계값 {params['rsi_threshold']})")
    print(f" VWAP 표준편차 밴드  : {'사용' if params['use_vwap_band'] else '미사용'} (시그마 {params['vwap_band_sigma']})")
    print("=" * 50)
    print(" ※ 위 조건은 이 세션에서만 유지되며 vwap_config.json에는 저장되지 않습니다.")


def edit_conditions(params: dict):
    print("\n--- 매매 조건 수정 (Enter만 누르면 현재 값 유지) ---")
    ticker_in = input(f"종목코드 [{params['ticker']}]: ").strip().upper()
    if ticker_in:
        params["ticker"] = ticker_in
    market_in = input(f"시장 US/KR [{params['market']}]: ").strip().upper()
    if market_in in ("US", "KR"):
        params["market"] = market_in
    interval_in = input(f"캔들 주기 1m/5m/15m [{params['interval']}]: ").strip()
    if interval_in:
        params["interval"] = interval_in
    reset_in = input(f"VWAP 리셋 시각 HH:MM [{params['reset_time']}]: ").strip()
    if reset_in:
        params["reset_time"] = reset_in

    params["n_percent"] = _edit_float("매수 이격률 N%", params["n_percent"])
    params["m_percent"] = _edit_float("매도 이격률 M%", params["m_percent"])
    params["x_percent"] = _edit_float("손절 비율 X%", params["x_percent"])
    params["k_percent"] = _edit_float("투자 비중 K%", params["k_percent"])
    params["initial_balance"] = _edit_float("초기 가상 자본금", params["initial_balance"])

    params["use_adx_filter"] = _edit_bool("ADX 필터 사용", params["use_adx_filter"])
    if params["use_adx_filter"]:
        params["adx_threshold"] = _edit_float("ADX 임계값", params["adx_threshold"])

    params["use_rsi_filter"] = _edit_bool("RSI 필터 사용", params["use_rsi_filter"])
    if params["use_rsi_filter"]:
        params["rsi_threshold"] = _edit_float("RSI 임계값", params["rsi_threshold"])

    params["use_vwap_band"] = _edit_bool("VWAP 표준편차 밴드 사용", params["use_vwap_band"])
    if params["use_vwap_band"]:
        params["vwap_band_sigma"] = _edit_float("VWAP 밴드 시그마", params["vwap_band_sigma"])

    print("✅ 조건이 이번 세션에 반영되었습니다. (initial_balance를 바꾸면 가상 잔고가 새로 초기화됩니다)")


def show_signal(state: dict):
    """봇이 실제로 보는 것과 동일한 화면(현재가/VWAP/타겟가/시그널)을 1회 조회합니다."""
    params = state["params"]
    v_broker = get_virtual_broker(state)

    try:
        df = v_broker.get_candles(params["ticker"], params["interval"], 150)
    except Exception as e:
        print(f"❌ 시세 조회 중 오류가 발생했습니다 (일시적인 API 장애일 수 있으니 잠시 후 다시 시도하세요): {e}")
        return
    if df.empty:
        print("❌ 캔들 데이터를 가져오지 못했습니다. 종목코드/시장 설정을 확인하세요.")
        return
    df = VwapStrategy.calculate_vwap(df, params["reset_time"])

    balance = v_broker.get_balance()
    holding = balance["holdings"].get(params["ticker"], {"qty": 0.0, "entry_price": 0.0})

    signals = VwapStrategy.get_signals(
        df, params["n_percent"], params["m_percent"], params["x_percent"],
        holding["qty"], holding["entry_price"],
        use_adx_filter=params["use_adx_filter"], adx_threshold=params["adx_threshold"],
        use_rsi_filter=params["use_rsi_filter"], rsi_threshold=params["rsi_threshold"],
        use_vwap_band=params["use_vwap_band"], vwap_band_sigma=params["vwap_band_sigma"]
    )

    print("\n" + "-" * 50)
    print(f" [{params['ticker']}] 현재가: {signals['current_price']:.2f} | VWAP: {signals['vwap']:.2f}")
    print(f" 매수 타겟가: {signals['target_buy_price']:.2f} | 매도 타겟가: {signals['target_sell_price']:.2f}")
    if holding["qty"] > 0:
        print(f" 보유량: {holding['qty']}주 @ {holding['entry_price']:.2f} | 손절가: {signals['stop_loss_price']:.2f}")
    else:
        print(" 보유량: 없음 (무포지션)")
    print(f" ADX: {signals['adx']} | RSI: {signals['rsi']} | VWAP 표준편차: {signals['vwap_stdev']}")
    print(f" >>> 매매 시그널: {signals['signal']} <<<")
    print(f" 가상 현금: {balance['cash']:,.2f}")
    print("-" * 50)


def run_virtual_cycle(state: dict, verbose: bool = True):
    """실봇(VWAPBot._loop_step)의 가상매매 로직 1주기와 동일한 순서로 시뮬레이션합니다."""
    params = state["params"]
    v_broker = get_virtual_broker(state)
    ticker = params["ticker"]

    try:
        df = v_broker.get_candles(ticker, params["interval"], 150)
    except Exception as e:
        print(f"❌ 시세 조회 중 오류가 발생했습니다 (일시적인 API 장애일 수 있으니 잠시 후 다시 시도하세요): {e}")
        return
    if df.empty:
        print("❌ 캔들 데이터를 가져오지 못했습니다. 다음 시도에서 재시도하세요.")
        return
    df = VwapStrategy.calculate_vwap(df, params["reset_time"])
    latest = df.iloc[-1]
    current_price = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])

    # 1. 기존 가상 미체결 주문 체결 여부 먼저 반영
    v_broker.update_simulation(ticker, current_price, high, low)

    # 2. 갱신된 잔고/포지션 조회
    balance = v_broker.get_balance()
    holding = balance["holdings"].get(ticker, {"qty": 0.0, "entry_price": 0.0})
    qty = holding["qty"]
    entry_price = holding["entry_price"]
    cash = balance["cash"]

    # 3. 시그널 계산
    signals = VwapStrategy.get_signals(
        df, params["n_percent"], params["m_percent"], params["x_percent"], qty, entry_price,
        use_adx_filter=params["use_adx_filter"], adx_threshold=params["adx_threshold"],
        use_rsi_filter=params["use_rsi_filter"], rsi_threshold=params["rsi_threshold"],
        use_vwap_band=params["use_vwap_band"], vwap_band_sigma=params["vwap_band_sigma"]
    )
    signal = signals["signal"]
    target_buy_price = signals["target_buy_price"]
    target_sell_price = signals["target_sell_price"]

    open_orders = v_broker.get_open_orders(ticker)

    # 4. 시그널에 따른 주문 집행 (실봇의 Cancel & Replace 로직과 동일)
    if signal == "STOP_LOSS":
        v_broker.force_market_stop_loss(ticker, current_price)
    elif signal == "SELL":
        sell_orders = [o for o in open_orders if o["side"] == "SELL"]
        if sell_orders:
            existing = sell_orders[0]
            if abs(existing["price"] - target_sell_price) > 0.01:
                if v_broker.cancel_order(existing["order_id"]):
                    v_broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
        else:
            v_broker.place_order(ticker, "SELL", target_sell_price, qty, "LIMIT")
    elif signal == "BUY":
        buy_orders = [o for o in open_orders if o["side"] == "BUY"]
        base_balance = params["initial_balance"] if params["initial_balance"] > 0.0 else cash
        invest_cash = base_balance * (params["k_percent"] / 100.0)
        if invest_cash > cash:
            invest_cash = cash
        buy_qty = int(invest_cash / target_buy_price)
        if buy_qty > 0:
            if buy_orders:
                existing = buy_orders[0]
                if abs(existing["price"] - target_buy_price) > 0.01 or int(existing["qty"]) != buy_qty:
                    if v_broker.cancel_order(existing["order_id"]):
                        v_broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
            else:
                v_broker.place_order(ticker, "BUY", target_buy_price, buy_qty, "LIMIT")
    else:
        for order in open_orders:
            v_broker.cancel_order(order["order_id"])

    if verbose:
        new_balance = v_broker.get_balance()
        new_holding = new_balance["holdings"].get(ticker, {"qty": 0.0})
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {ticker} 현재가 {current_price:.2f} | VWAP {signals['vwap']:.2f} | 시그널 {signal} "
              f"| 가상현금 {new_balance['cash']:,.0f} | 보유 {new_holding['qty']}주")


def auto_loop(state: dict):
    try:
        interval_sec = int(input("반복 주기(초, 기본 60): ").strip() or 60)
    except ValueError:
        interval_sec = 60
    print(f"▶ {interval_sec}초 간격으로 가상 매매 사이클을 반복 실행합니다. 중단하려면 Ctrl+C를 누르세요.")
    try:
        while True:
            run_virtual_cycle(state)
            time.sleep(interval_sec)
    except KeyboardInterrupt:
        print("\n⏹ 자동 반복을 중단했습니다.")


def show_status(state: dict):
    v_broker = get_virtual_broker(state)
    balance = v_broker.get_balance()
    trades = VwapConfigManager.load_trades(TEST_MODE)

    print("\n" + "=" * 50)
    print(" 가상 잔고 현황 (VIRTUAL_TEST)")
    print("=" * 50)
    print(f" 현금: {balance['cash']:,.2f}")
    print(f" 보유종목: {balance['holdings']}")
    print(f" 미체결 주문: {v_broker.get_open_orders(state['params']['ticker'])}")
    print(f" 누적 거래 수: {len(trades)}")
    for t in trades[-10:]:
        print(f"   {t['timestamp']} {t['side']} {t['ticker']} {t['qty']}주 @ {t['price']} (손익 {t.get('pnl', 0):+.2f})")
    print("=" * 50)


def reset_test_ledger(state: dict):
    confirm = input("VIRTUAL_TEST 가상 거래이력을 초기화합니다. 계속할까요? (y/n): ").strip().lower()
    if confirm != 'y':
        print("취소되었습니다.")
        return
    VwapConfigManager.save_trades([], TEST_MODE)
    v_broker = state.get("_virtual_broker")
    if v_broker:
        v_broker.open_orders = []
        v_broker._sync_balance_from_trades()
    print("✅ VIRTUAL_TEST 장부가 초기화되었습니다. (실제 대시보드의 VIRTUAL_1/2/3 데이터는 영향을 받지 않습니다)")


def print_menu():
    print("\n" + "=" * 50)
    print("   VWAP 가상매매 테스트 랩 (VIRTUAL_TEST 전용 샌드박스)")
    print("=" * 50)
    print("1. 현재 매매 조건 확인")
    print("2. 매매 조건 수정 (세션 한정, 파일 저장 안 함)")
    print("3. 현재 시세 + VWAP + 매매 시그널 1회 조회")
    print("4. 가상 매매 1사이클 실행")
    print("5. 자동 반복 실행 (N초 간격, Ctrl+C로 중단)")
    print("6. 가상 잔고 / 보유종목 / 거래이력 보기")
    print("7. 가상 거래이력 초기화")
    print("0. 종료")
    print("=" * 50)


def main():
    state = {
        "params": load_default_params(),
        "_real_broker": build_real_broker(),
        "_virtual_broker": None
    }
    print("💡 이 스크립트는 실제 계좌 및 대시보드의 VIRTUAL_1~3 장부와 완전히 분리된")
    print("   'VIRTUAL_TEST' 전용 가상 자금으로만 동작합니다. 실제 돈에는 어떤 영향도 없습니다.")

    while True:
        print_menu()
        choice = input("원하는 메뉴 번호를 입력하세요: ").strip()
        if choice == '1':
            print_conditions(state["params"])
        elif choice == '2':
            edit_conditions(state["params"])
        elif choice == '3':
            show_signal(state)
        elif choice == '4':
            run_virtual_cycle(state)
        elif choice == '5':
            auto_loop(state)
        elif choice == '6':
            show_status(state)
        elif choice == '7':
            reset_test_ledger(state)
        elif choice == '0':
            print("가상매매 테스트 랩을 종료합니다.")
            break
        else:
            print("잘못된 입력입니다. 다시 선택해주세요.")

        input("\n계속하려면 Enter를 누르세요...")


if __name__ == "__main__":
    main()
