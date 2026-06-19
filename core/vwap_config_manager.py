import os
import json
from dotenv import load_dotenv
from utils.vwap_crypto import VwapCrypto

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "vwap_config.json")
TRADES_PATH = os.path.join(PROJECT_ROOT, "vwap_trades.json")

# 암호화하여 저장할 민감한 필드 목록
SENSITIVE_KEYS = ["toss_client_secret", "toss_account_seq"]

class VwapConfigManager:
    @staticmethod
    def get_default_config() -> dict:
        """기본 설정 딕셔너리를 반환합니다."""
        return {
            "mode": "VIRTUAL",              # REAL (실거래) / VIRTUAL (가상 거래)
            "ticker": "AAPL",               # 기본 거래 대상 (미국 주식 AAPL)
            "market": "US",                 # US (미국) / KR (한국)
            "interval": "1m",               # 봉 주기 (1m, 5m, 15m 등)
            "n_percent": 1.0,               # 매수 진입 하방 이격도 %
            "m_percent": 1.0,               # 매도 청산 상방 이격도 %
            "x_percent": 2.0,               # 손절 비율 %
            "k_percent": 10.0,              # 투자 비중 % (잔고 대비)
            "initial_balance": 10000000.0,  # 가상 투자 초기 자본 (원화 또는 USD 기준)
            "max_investment_limit": 5000000.0, # 1회 최대 투자 가용 한도액 (미수/신용 차단용)
            "reset_time": "22:30",          # VWAP 누적 리셋 시각 (HH:MM)
            "toss_client_id": "",           # 토스 Client ID
            "toss_client_secret": "",       # 토스 Client Secret (암호화 대상)
            "toss_account_seq": "",         # 토스 계좌식별자 (암호화 대상)
            "admin_password_hash": ""       # 어드민 비밀번호 해시
        }

    @classmethod
    def load_config(cls) -> dict:
        """설정을 로드합니다. env -> json 파일 순으로 우선순위 결합 및 복호화."""
        # .env 강제 로드
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

        config = cls.get_default_config()

        # 1. 환경 변수 우선 로드
        env_client_id = os.getenv("TOSS_CLIENT_ID")
        env_client_secret = os.getenv("TOSS_CLIENT_SECRET")
        env_account_seq = os.getenv("TOSS_ACCOUNT_SEQ")
        env_admin_pw = os.getenv("VWAP_ADMIN_PASSWORD")

        if env_client_id:
            config["toss_client_id"] = env_client_id
        if env_client_secret:
            config["toss_client_secret"] = env_client_secret
        if env_account_seq:
            config["toss_account_seq"] = env_account_seq
        if env_admin_pw:
            config["admin_password_hash"] = VwapCrypto.hash_password(env_admin_pw)
        else:
            # 기본 비밀번호는 'admin1234'
            config["admin_password_hash"] = VwapCrypto.hash_password("admin1234")

        # 2. vwap_config.json 파일이 있으면 병합
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
                
                # 파일에 기록된 값으로 덮어씀
                for k, v in file_config.items():
                    if k in config:
                        config[k] = v

                # 민감한 정보는 복호화하여 인메모리에 보관
                for key in SENSITIVE_KEYS:
                    if config.get(key):
                        decrypted = VwapCrypto.decrypt(config[key])
                        if decrypted:  # 복호화 성공 시에만 대입
                            config[key] = decrypted
            except Exception as e:
                print(f"[ConfigManager] Failed to load json config: {e}")

        return config

    @classmethod
    def save_config(cls, config_data: dict):
        """설정을 암호화하여 vwap_config.json 파일에 저장합니다."""
        save_data = config_data.copy()

        # 민감 데이터 암호화
        for key in SENSITIVE_KEYS:
            if save_data.get(key):
                # 이미 암호화된 값인지 확인하여 이중 암호화 방지
                # 복호화를 시도했을 때 성공하면 아직 평문이라는 의미
                decrypted = VwapCrypto.decrypt(save_data[key])
                if decrypted:
                    # 복호화된 평문이 있으면, 원본 평문을 암호화
                    save_data[key] = VwapCrypto.encrypt(decrypted)
                else:
                    # 복호화가 안 되면 현재 값이 평문이므로 그대로 암호화
                    save_data[key] = VwapCrypto.encrypt(save_data[key])

        # 패스워드 해시는 파일에 굳이 안 써도 되나, 대시보드 저장 시 유지
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[ConfigManager] Failed to save config: {e}")

    @staticmethod
    def load_trades() -> list:
        """가상/실제 거래 이력을 로드합니다."""
        if not os.path.exists(TRADES_PATH):
            return []
        try:
            with open(TRADES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[ConfigManager] Failed to load trades: {e}")
            return []

    @classmethod
    def save_trades(cls, trades: list):
        """거래 이력을 저장합니다."""
        try:
            with open(TRADES_PATH, "w", encoding="utf-8") as f:
                json.dump(trades, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[ConfigManager] Failed to save trades: {e}")

    @classmethod
    def add_trade(cls, trade_item: dict):
        """새로운 거래 이력을 추가합니다."""
        trades = cls.load_trades()
        trades.append(trade_item)
        cls.save_trades(trades)
