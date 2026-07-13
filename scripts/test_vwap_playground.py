import sys
import os
import pandas as pd
import time

# Import paths setup
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from core.vwap.crypto import VwapCrypto
from core.vwap.config_manager import VwapConfigManager
from core.vwap.strategy import VwapStrategy
from core.vwap.broker import TossBroker, VirtualBroker

def print_menu():
    print("\n" + "=" * 50)
    print("      LIMIT VWAP Bot Interactive Playground")
    print("=" * 50)
    print("1. 토스 API 토큰 발급 및 접속 상태 점검")
    print("2. 매매 조건별 실시간 타겟 매수/매도가 조회")
    print("3. [가상 거래] 주문 집행 및 체결 시뮬레이션 테스트")
    print("4. [실제 거래] 1주 지정가 주문 전송 및 취소 테스트 (안전 장치 포함)")
    print("0. 종료")
    print("=" * 50)

def test_token_auth():
    print("\n--- [1] 토스 API 토큰 발급 및 접속 상태 점검 ---")
    config = VwapConfigManager.load_config()
    client_id = config.get("toss_client_id")
    client_secret = config.get("toss_client_secret")
    account_seq = config.get("toss_account_seq")
    
    if not client_id or not client_secret:
        print("[오류] TOSS API 설정이 누락되었습니다. .env 또는 대시보드 API 설정을 완료하세요.")
        return

    print("설정된 Client ID:", client_id[:6] + "..." if client_id else "None")
    
    broker = TossBroker(client_id, client_secret, account_seq)
    try:
        broker._ensure_token()
        if broker.mock_mode and not broker.is_mock_only:
            print("❌ 토큰 발급 실패: 브로커가 MOCK 모드로 폴백되었습니다.")
        else:
            print("✅ OAuth 토큰 발급 성공!")
            print(f"   Access Token: {broker.access_token[:15]}...")
            
            print("\n계좌 예수금 조회 시도...")
            balance = broker.get_balance()
            print("   예수금 (Cash):", balance.get("cash"))
            print("   보유 주식 (Holdings):", balance.get("holdings"))
    except Exception as e:
        print("❌ API 연동 실패!")
        print("   상세 오류:", e)

def test_strategy_targets():
    print("\n--- [2] 매매 조건별 실시간 타겟 매수/매도가 조회 ---")
    ticker = input("조회할 종목코드 (예: AAPL 또는 6자리 한국종목코드): ").strip().upper()
    if not ticker:
        ticker = "AAPL"
        
    n_percent = float(input("매수 이격률 (N%, 기본값 1.0): ") or 1.0)
    m_percent = float(input("매도 이격률 (M%, 기본값 1.0): ") or 1.0)
    use_dev_band = input("변동성 표준편차 밴드(Dev Band) 적용 여부? (y/n, 기본값 n): ").strip().lower() == 'y'
    
    print("\n실시간 캔들 수집 중...")
    config = VwapConfigManager.load_config()
    broker = TossBroker(
        client_id=config.get("toss_client_id"),
        client_secret=config.get("toss_client_secret"),
        account_seq=config.get("toss_account_seq")
    )
    
    # 캔들 가져오기
    interval = "1m"
    limit = 100
    df = broker.get_candles(ticker, interval, limit)
    
    if df.empty:
        print("❌ 캔들 데이터를 가져올 수 없습니다. 종목코드를 다시 확인하십시오.")
        return
        
    # VWAP 계산
    reset_time = "09:00" if (ticker.isdigit() and len(ticker) == 6) else "22:30"
    df_vwap = VwapStrategy.calculate_vwap(df, reset_time_str=reset_time)
    
    current_price = broker.get_current_price(ticker)
    if current_price <= 0:
        current_price = float(df_vwap.iloc[-1]['close'])
        
    latest_vwap = float(df_vwap.iloc[-1]['vwap'])
    
    print("\n" + "-" * 40)
    print(f" 종목: {ticker} | 현재가: {current_price:.2f}")
    print(f" 당일 누적 실시간 VWAP: {latest_vwap:.2f}")
    print("-" * 40)
    
    if use_dev_band:
        # Dev Band 연산
        df_band = VwapStrategy.calculate_vwap_dev_band(df_vwap, sigma=2.0)
        target_buy = float(df_band.iloc[-1]['vwap_lower'])
        target_sell = float(df_band.iloc[-1]['vwap_upper'])
        print(f" Dev Band (2.0 Sigma) 적용")
        print(f" 🟢 매수 타겟가 (Dev Lower): {target_buy:.2f} (현재가 대비 {((target_buy-current_price)/current_price*100):+.2f}%)")
        print(f" 🔴 매도 타겟가 (Dev Upper): {target_sell:.2f} (현재가 대비 {((target_sell-current_price)/current_price*100):+.2f}%)")
    else:
        # 고정 이격률 연산
        target_buy = latest_vwap * (1 - n_percent / 100.0)
        target_sell = latest_vwap * (1 + m_percent / 100.0)
        print(f" 고정 이격률 (N={n_percent}%, M={m_percent}%) 적용")
        print(f" 🟢 매수 타겟가 (VWAP - N%): {target_buy:.2f} (현재가 대비 {((target_buy-current_price)/current_price*100):+.2f}%)")
        print(f" 🔴 매도 타겟가 (VWAP + M%): {target_sell:.2f} (현재가 대비 {((target_sell-current_price)/current_price*100):+.2f}%)")
    print("-" * 40)

def test_virtual_trading():
    print("\n--- [3] [가상 거래] 주문 집행 및 체결 시뮬레이션 ---")
    config = VwapConfigManager.load_config()
    real_broker = TossBroker(
        client_id=config.get("toss_client_id"),
        client_secret=config.get("toss_client_secret"),
        account_seq=config.get("toss_account_seq")
    )
    
    # 임시 가상 브로커 생성 (시작자산: 10,000,000원)
    v_broker = VirtualBroker(initial_balance=10000000.0, ticker_source_broker=real_broker, mode="VIRTUAL_1")
    
    ticker = input("테스트할 가상 매매 종목 (기본 AAPL): ").strip().upper() or "AAPL"
    curr_price = v_broker.get_current_price(ticker)
    print(f"현재 실시간 시세: {curr_price:.2f}")
    
    side = input("주문 방향 (BUY / SELL): ").strip().upper()
    if side not in ["BUY", "SELL"]:
        print("❌ 잘못된 주문 방향입니다.")
        return
        
    price = float(input(f"주문 단가 (기본 현재가 {curr_price:.2f}): ") or curr_price)
    qty = float(input("주문 수량 (기본 1주): ") or 1.0)
    
    # 1. 가상 주문 제출
    order_id = v_broker.place_order(ticker, side, price, qty)
    if not order_id:
        print("❌ 가상 주문 제출 실패 (잔고 부족 등)")
        return
        
    print(f"가상 주문이 생성되었습니다. ID: {order_id}")
    print("현재 가상 미체결 주문 목록:", v_broker.get_open_orders(ticker))
    
    # 2. 체결 시뮬레이션 작동
    sim_action = input("\n주문을 체결 시뮬레이션하시겠습니까? (y/n, n선택 시 주문 취소 테스트): ").strip().lower()
    if sim_action == 'y':
        if side == "BUY":
            # 매수 체결 조건: 저가가 지정가 이하여야 함
            # 테스트를 위해 가상 고가/저가를 주어 강제 체결
            v_broker.update_simulation(ticker, current_price=price, high=price+1, low=price-1)
        else:
            # 매도 체결 조건: 고가가 지정가 이상이어야 함
            v_broker.update_simulation(ticker, current_price=price, high=price+1, low=price-1)
            
        print("\n시뮬레이션 갱신 후 가상 잔고 상태:")
        print("   현금 (Cash):", v_broker.get_balance()["cash"])
        print("   보유 주식 (Holdings):", v_broker.get_balance()["holdings"])
    else:
        # 주문 취소 테스트
        success = v_broker.cancel_order(order_id)
        print(f"주문 취소 결과: {'성공' if success else '실패'}")
        print("가상 잔고 상태:", v_broker.get_balance())

def test_real_trading():
    print("\n--- [4] [실제 거래] 1주 지정가 주문 전송 및 취소 테스트 ---")
    config = VwapConfigManager.load_config()
    client_id = config.get("toss_client_id")
    client_secret = config.get("toss_client_secret")
    account_seq = config.get("toss_account_seq")
    
    broker = TossBroker(client_id, client_secret, account_seq)
    if broker.is_mock_only:
        print("[오류] 실제 API 키가 연동되어 있지 않습니다. MOCK 모드에서는 실제 주문을 넣을 수 없습니다.")
        return
        
    ticker = input("실제 주문 종목코드 (기본 AAPL): ").strip().upper() or "AAPL"
    curr_price = broker.get_current_price(ticker)
    
    print(f"현재 실시간 시세: {curr_price:.2f}")
    print("⚠️ 테스트를 위해 아주 낮은 가격(매수) 또는 아주 높은 가격(매도)으로 미체결 지정가 주문을 넣는 것을 권장합니다.")
    
    side = input("실제 주문 방향 (BUY / SELL): ").strip().upper()
    if side not in ["BUY", "SELL"]:
        print("❌ 잘못된 주문 방향입니다.")
        return
        
    price = float(input("실제 주문 단가: "))
    qty = float(input("실제 주문 수량 (기본 1): ") or 1)
    
    print("\n" + "!" * 50)
    print(f"  [경고] {ticker} 종목을 지정가 {price:.2f}에 {qty}주 {side} 주문을 전송합니다.")
    print("  이 주문은 실제 주식 시장으로 전송되는 실제 거래 주문입니다!")
    print("!" * 50)
    
    confirm = input("정말로 전송하시겠습니까? (yes/no): ").strip().lower()
    if confirm != "yes":
        print("주문이 전송되지 않고 취소되었습니다.")
        return
        
    print("\n주문 전송 중...")
    order_id = broker.place_order(ticker, side, price, qty, order_type="LIMIT")
    
    if not order_id:
        print("❌ 실제 주문 제출에 실패하였습니다. (에러 로그 또는 시장 상태 확인)")
        return
        
    print(f"✅ 실제 주문이 정상 제출되었습니다! Order ID: {order_id}")
    
    # 취소 검증
    time.sleep(2)
    cancel_confirm = input("\n방금 접수한 실제 주문을 지금 바로 취소하시겠습니까? (y/n): ").strip().lower()
    if cancel_confirm == 'y':
        success = broker.cancel_order(order_id)
        if success:
            print("✅ 실제 주문이 성공적으로 취소되었습니다.")
        else:
            print("❌ 실제 주문 취소에 실패했습니다. (이미 체결되었거나 오류 발생)")
    else:
        print("주문을 취소하지 않았습니다. HTS나 토스 앱에서 미체결 내역을 수동으로 관리하십시오.")

def main():
    while True:
        print_menu()
        choice = input("원하는 메뉴 번호를 입력하세요: ").strip()
        if choice == '1':
            test_token_auth()
        elif choice == '2':
            test_strategy_targets()
        elif choice == '3':
            test_virtual_trading()
        elif choice == '4':
            test_real_trading()
        elif choice == '0':
            print("플레이그라운드를 종료합니다.")
            break
        else:
            print("잘못된 입력입니다. 다시 선택해주세요.")
        
        input("\n계속하려면 Enter를 누르세요...")

if __name__ == "__main__":
    main()
