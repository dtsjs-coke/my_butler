import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from core.vwap_config_manager import VwapConfigManager
from core.vwap_broker import TossBroker

def test_credentials():
    print("==================================================")
    print("  Toss Securities API Credentials Verification")
    print("==================================================")
    print("Loading config...")
    config = VwapConfigManager.load_config()
    
    client_id = config.get("toss_client_id")
    client_secret = config.get("toss_client_secret")
    account_seq = config.get("toss_account_seq")
    
    mask = lambda s: s[:4] + "*" * (len(s)-4) if (s and len(s) > 4) else ("****" if s else "None")
    print(f"Client ID     : {mask(client_id)}")
    print(f"Client Secret : {mask(client_secret)}")
    print(f"Account Seq   : {mask(account_seq)}")
    
    if not client_id or not client_secret or not account_seq:
        print("\n[ERROR] Client ID, Client Secret, or Account Seq is missing in config/env.")
        return

    broker = TossBroker(client_id, client_secret, account_seq)
    
    print("\nAttempting OAuth Token fetch...")
    broker._ensure_token()
    
    if broker.mock_mode:
        print("\n[ERROR] Token authentication failed or broker fell back to MOCK mode.")
        print("Please check if your Client ID and Client Secret are correct.")
    else:
        print("[SUCCESS] OAuth Token retrieved successfully!")
        
        # 1. 먼저 사용자의 계좌 목록을 조회하여 각 계좌의 accountSeq를 알아냅니다.
        print("\nFetching Account List to find your correct accountSeq...")
        import requests
        headers = {
            "Authorization": f"Bearer {broker.access_token}",
            "Content-Type": "application/json"
        }
        try:
            acc_res = requests.get(f"{broker.base_url}/api/v1/accounts", headers=headers, timeout=5)
            if acc_res.status_code == 200:
                accounts = acc_res.json().get("result", [])
                print(f"[SUCCESS] Found {len(accounts)} account(s):")
                for acc in accounts:
                    acc_no = acc.get("accountNo", "")
                    acc_seq = acc.get("accountSeq")
                    acc_type = acc.get("accountType", "")
                    
                    # 마스킹 처리
                    masked_acc_no = acc_no[:4] + "*" * (len(acc_no)-4) if len(acc_no) > 4 else acc_no
                    print(f"  * Account Type : {acc_type}")
                    print(f"    Account No   : {masked_acc_no}")
                    print(f"    accountSeq   : {acc_seq}  <-- Copy this value to TOSS_ACCOUNT_SEQ in .env!")
            else:
                print(f"[ERROR] Failed to fetch account list (HTTP {acc_res.status_code})")
                print(f"API Error Response: {acc_res.text}")
        except Exception as e:
            print(f"[ERROR] Failed to query account list: {e}")

        # 2. 현재 입력된 account_seq가 유효한지 확인합니다.
        print("\nVerifying currently configured Account Sequence (account_seq)...")
        headers["X-Tossinvest-Account"] = str(broker.account_seq)
        try:
            res = requests.get(f"{broker.base_url}/api/v1/commissions", headers=headers, timeout=5)
            if res.status_code == 200:
                print("[SUCCESS] Currently configured Account Sequence is VALID! (HTTP 200)")
                print(f"Details: {res.json()}")
            else:
                print(f"[ERROR] Currently configured Account Sequence is INVALID! (HTTP {res.status_code})")
                print(f"API Error Response: {res.text}")
        except Exception as e:
            print(f"[ERROR] Failed to contact Toss API: {e}")
    print("==================================================")

if __name__ == "__main__":
    test_credentials()
