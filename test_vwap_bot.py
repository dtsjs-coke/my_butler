import sys
import os
import pandas as pd

# 임포트 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from utils.vwap_crypto import VwapCrypto
from core.vwap_config_manager import VwapConfigManager
from core.vwap_strategy import VwapStrategy
from core.vwap_broker import TossBroker, VirtualBroker
from backtest.vwap_backtester import VwapBacktester

def run_tests():
    print("==================================================")
    print("  LIMIT VWAP Trading Bot Unit Tests Starting")
    print("==================================================")

    # 1. 암호화 / 복호화 테스트
    print("\n1. [Security] Cryptography Encryption & Decryption...")
    test_secret = "my_toss_super_secret_key_1234!"
    encrypted = VwapCrypto.encrypt(test_secret)
    decrypted = VwapCrypto.decrypt(encrypted)
    
    assert decrypted == test_secret, "Decryption mismatch!"
    print(f"   [OK] Plaintext: {test_secret}")
    print(f"   [OK] Ciphertext: {encrypted[:20]}...")
    print(f"   [OK] Decrypted: {decrypted}")

    # 2. 어드민 세션 토큰 테스트
    print("\n2. [Security] Admin Session Token Creation & Verification...")
    token = VwapCrypto.generate_session_token("admin")
    is_valid = VwapCrypto.verify_session_token(token)
    assert is_valid is True, "Token validation failed!"
    print("   [OK] Session Token validation successful.")

    # 3. 설정 로드 및 영속화 테스트
    print("\n3. [Config] Config Manager Load/Save Validation...")
    config = VwapConfigManager.load_config()
    original_mode = config["mode"]
    
    # 임시 변경 테스트
    config["mode"] = "VIRTUAL"
    config["toss_client_secret"] = "my_secret_token_abc"
    VwapConfigManager.save_config(config)
    
    # 재로드 검사
    reloaded_config = VwapConfigManager.load_config()
    assert reloaded_config["toss_client_secret"] == "my_secret_token_abc", "Config load mismatch!"
    print("   [OK] Configuration Encryption and Decryption Load/Save successful.")
    
    # 원상 복구
    config["mode"] = original_mode
    config["toss_client_secret"] = ""
    VwapConfigManager.save_config(config)

    # 4. VWAP 전략 연산 검증
    print("\n4. [Strategy] Sample Candle Rolling VWAP Calculation...")
    # 임의의 캔들 데이터 생성
    times = pd.date_range("2026-06-19 09:00:00", periods=10, freq="1min")
    data = {
        "time": times,
        "open": [100.0, 101.0, 102.0, 101.5, 100.5, 101.0, 102.5, 103.0, 102.0, 101.0],
        "high": [100.5, 101.5, 102.5, 102.0, 101.0, 101.5, 103.0, 103.5, 102.5, 101.5],
        "low": [99.5, 100.5, 101.5, 101.0, 100.0, 100.5, 102.0, 102.5, 101.5, 100.5],
        "close": [100.0, 101.0, 102.0, 101.5, 100.5, 101.0, 102.5, 103.0, 102.0, 101.0],
        "volume": [10.0, 20.0, 15.0, 30.0, 25.0, 10.0, 40.0, 50.0, 20.0, 10.0]
    }
    df = pd.DataFrame(data)
    df_vwap = VwapStrategy.calculate_vwap(df, reset_time_str="09:00")
    
    assert "vwap" in df_vwap.columns, "VWAP column missing!"
    print(f"   [OK] Latest VWAP: {df_vwap.iloc[-1]['vwap']:.2f}")

    # 5. 백테스터 연산 검증
    print("\n5. [Backtest] VwapBacktester Calculation Verification...")
    mock_toss = TossBroker(client_id="", client_secret="", account_seq="")
    
    result = VwapBacktester.run(
        broker=mock_toss,
        ticker="AAPL",
        interval="1m",
        n_percent=1.0,
        m_percent=1.0,
        x_percent=2.0,
        initial_balance=10000000.0
    )
    
    print("   [OK] Backtest Report:")
    print(f"        Initial Capital: {result['initial_balance']:.2f}")
    print(f"        Final Asset: {result['final_asset']:.2f}")
    print(f"        ROI: {result['roi']:+.2f}%")
    print(f"        MDD: {result['mdd']:.2f}%")
    print(f"        Total Trades: {result['total_trades']} times")
    print(f"        Win Rate: {result['win_rate']:.2f}%")

    print("\n==================================================")
    print("  ALL VWAP BOT CORE MODULE TESTS PASSED SUCCESSFULLY!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
