import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.append(PROJECT_ROOT)

from core.vwap_config_manager import VwapConfigManager
from core.vwap_broker import TossBroker

config = VwapConfigManager.load_config()
print("====================================")
print("Toss API Connection Verification")
print("====================================")
print("Client ID:", config.get("toss_client_id"))
secret = config.get("toss_client_secret", "")
print("Client Secret Decrypted:", secret[:4] + "***" if secret else "EMPTY")
seq = config.get("toss_account_seq", "")
print("Account Seq Decrypted:", seq[:4] + "***" if seq else "EMPTY")

broker = TossBroker(
    client_id=config["toss_client_id"],
    client_secret=config["toss_client_secret"],
    account_seq=config["toss_account_seq"]
)

print("Mock Mode Status:", broker.mock_mode)
try:
    balance = broker.get_balance()
    print("API Access Result: SUCCESS")
    print("Balance Cash:", balance.get("cash"))
    holdings_keys = list(balance.get("holdings", {}).keys())
    print("Balance Holdings:", holdings_keys)
    if holdings_keys:
        print("Batch Fetching Prices for Holdings...")
        prices = broker.get_current_prices(holdings_keys)
        print("Batch Fetch Result:", prices)
except Exception as e:
    print("API Access Result: FAILED")
    print("Error:", e)
print("====================================")
