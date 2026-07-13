import os
import json
from dotenv import load_dotenv
from core.vwap.crypto import VwapCrypto

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "vwap_config.json")
TRADES_PATH = os.path.join(DATA_DIR, "vwap_trades.json")

# 암호화하여 저장할 민감한 필드 목록
SENSITIVE_KEYS = ["toss_client_secret", "toss_account_seq"]

class VwapConfigManager:
    @staticmethod
    def get_default_config() -> dict:
        """기본 설정 딕셔너리를 반환합니다."""
        return {
            "mode": "VIRTUAL",              # 하위 호환성 유지용
            "ticker": "AAPL",               # 하위 호환성 유지용
            "market": "US",                 # 하위 호환성 유지용
            "interval": "1m",               # 하위 호환성 유지용
            "n_percent": 1.0,               # 하위 호환성 유지용
            "m_percent": 1.0,               # 하위 호환성 유지용
            "x_percent": 2.0,               # 하위 호환성 유지용
            "k_percent": 10.0,              # 하위 호환성 유지용
            "initial_balance": 10000000.0,  # 하위 호환성 유지용
            "max_daily_loss_limit": 5.0,    # 하위 호환성 유지용
            "reset_time": "22:30",          # 하위 호환성 유지용
            "use_adx_filter": False,        # 하위 호환성 유지용
            "adx_period": 14,               # 하위 호환성 유지용
            "adx_threshold": 25.0,          # 하위 호환성 유지용
            "use_rsi_filter": False,        # 하위 호환성 유지용
            "rsi_period": 14,               # 하위 호환성 유지용
            "rsi_threshold": 30.0,          # 하위 호환성 유지용
            "use_vwap_band": False,         # 하위 호환성 유지용
            "vwap_band_sigma": 2.0,         # 하위 호환성 유지용

            "admin_password_hash": "",       # 어드민 비밀번호 해시
            "toss_client_id": "",           # 토스 Client ID
            "toss_client_secret": "",       # 토스 Client Secret (암호화 대상)
            "toss_account_seq": "",         # 토스 계좌식별자 (암호화 대상)
            
            # 가상 거래(VIRTUAL) 파라미터 - 하위 호환용
            "virtual_ticker": "AAPL",
            "virtual_market": "US",
            "virtual_interval": "1m",
            "virtual_n_percent": 1.0,
            "virtual_m_percent": 1.0,
            "virtual_x_percent": 2.0,
            "virtual_k_percent": 10.0,
            "virtual_initial_balance": 10000000.0,
            "virtual_max_daily_loss_limit": 5.0,
            "virtual_reset_time": "22:30",
            "virtual_start_time": "",
            "virtual_use_adx_filter": False,
            "virtual_adx_period": 14,
            "virtual_adx_threshold": 25.0,
            "virtual_use_rsi_filter": False,
            "virtual_rsi_period": 14,
            "virtual_rsi_threshold": 30.0,
            "virtual_use_vwap_band": False,
            "virtual_vwap_band_sigma": 2.0,
            "virtual_is_running": False,

            # 가상 거래 1(VIRTUAL_1) 파라미터
            "virtual_1_ticker": "AAPL",
            "virtual_1_market": "US",
            "virtual_1_interval": "1m",
            "virtual_1_n_percent": 1.0,
            "virtual_1_m_percent": 1.0,
            "virtual_1_x_percent": 2.0,
            "virtual_1_k_percent": 10.0,
            "virtual_1_initial_balance": 10000000.0,
            "virtual_1_max_daily_loss_limit": 5.0,
            "virtual_1_reset_time": "22:30",
            "virtual_1_start_time": "",
            "virtual_1_use_adx_filter": False,
            "virtual_1_adx_period": 14,
            "virtual_1_adx_threshold": 25.0,
            "virtual_1_use_rsi_filter": False,
            "virtual_1_rsi_period": 14,
            "virtual_1_rsi_threshold": 30.0,
            "virtual_1_use_vwap_band": False,
            "virtual_1_vwap_band_sigma": 2.0,
            "virtual_1_is_running": False,

            # 가상 거래 2(VIRTUAL_2) 파라미터
            "virtual_2_ticker": "TSLA",
            "virtual_2_market": "US",
            "virtual_2_interval": "1m",
            "virtual_2_n_percent": 1.0,
            "virtual_2_m_percent": 1.0,
            "virtual_2_x_percent": 2.0,
            "virtual_2_k_percent": 10.0,
            "virtual_2_initial_balance": 10000000.0,
            "virtual_2_max_daily_loss_limit": 5.0,
            "virtual_2_reset_time": "22:30",
            "virtual_2_start_time": "",
            "virtual_2_use_adx_filter": False,
            "virtual_2_adx_period": 14,
            "virtual_2_adx_threshold": 25.0,
            "virtual_2_use_rsi_filter": False,
            "virtual_2_rsi_period": 14,
            "virtual_2_rsi_threshold": 30.0,
            "virtual_2_use_vwap_band": False,
            "virtual_2_vwap_band_sigma": 2.0,
            "virtual_2_is_running": False,

            # 가상 거래 3(VIRTUAL_3) 파라미터
            "virtual_3_ticker": "NVDA",
            "virtual_3_market": "US",
            "virtual_3_interval": "1m",
            "virtual_3_n_percent": 1.0,
            "virtual_3_m_percent": 1.0,
            "virtual_3_x_percent": 2.0,
            "virtual_3_k_percent": 10.0,
            "virtual_3_initial_balance": 10000000.0,
            "virtual_3_max_daily_loss_limit": 5.0,
            "virtual_3_reset_time": "22:30",
            "virtual_3_start_time": "",
            "virtual_3_use_adx_filter": False,
            "virtual_3_adx_period": 14,
            "virtual_3_adx_threshold": 25.0,
            "virtual_3_use_rsi_filter": False,
            "virtual_3_rsi_period": 14,
            "virtual_3_rsi_threshold": 30.0,
            "virtual_3_use_vwap_band": False,
            "virtual_3_vwap_band_sigma": 2.0,
            "virtual_3_is_running": False,

            # 실제 거래(REAL) 파라미터
            "real_ticker": "AAPL",
            "real_market": "US",
            "real_interval": "1m",
            "real_n_percent": 1.0,
            "real_m_percent": 1.0,
            "real_x_percent": 2.0,
            "real_k_percent": 10.0,
            "real_initial_balance": 10000000.0,
            "real_max_daily_loss_limit": 5.0,
            "real_reset_time": "22:30",
            "real_start_time": "",
            "real_use_adx_filter": False,
            "real_adx_period": 14,
            "real_adx_threshold": 25.0,
            "real_use_rsi_filter": False,
            "real_rsi_period": 14,
            "real_rsi_threshold": 30.0,
            "real_use_vwap_band": False,
            "real_vwap_band_sigma": 2.0,
            "real_is_running": False,
        }

    @classmethod
    def load_config(cls) -> dict:
        """설정을 로드합니다. env -> json 파일 순으로 우선순위 결합 및 복호화."""
        # .env 강제 로드 (기존 시스템 환경 변수 덮어쓰기 허용)
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

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
                        # 이미 환경변수로 채워진 값이 있고 파일의 값이 비어있으면 덮어쓰지 않음
                        if config[k] and (v == "" or v is None):
                            continue
                        config[k] = v

                # [마이그레이션] 만약 기존 단일 필드 구조라면 새 구조로 복사
                is_migrated = False
                legacy_keys = [
                    "ticker", "market", "interval", "n_percent", "m_percent", 
                    "x_percent", "k_percent", "initial_balance", "max_daily_loss_limit", 
                    "reset_time", "use_adx_filter", "adx_period", "adx_threshold", 
                    "use_rsi_filter", "rsi_period", "rsi_threshold", "use_vwap_band", 
                    "vwap_band_sigma"
                ]
                if "virtual_ticker" not in file_config:
                    for lk in legacy_keys:
                        if lk in file_config:
                            config[f"virtual_{lk}"] = file_config[lk]
                            config[f"real_{lk}"] = file_config[lk]
                    is_migrated = True
                
                if is_migrated:
                    cls.save_config(config)

                # [마이그레이션 2] 단일 가상 설정을 가상_1 설정으로 복사
                is_migrated_v1 = False
                if "virtual_1_ticker" not in file_config:
                    v_keys = [
                        "ticker", "market", "interval", "n_percent", "m_percent", 
                        "x_percent", "k_percent", "initial_balance", "max_daily_loss_limit", 
                        "reset_time", "start_time", "use_adx_filter", "adx_period", "adx_threshold", 
                        "use_rsi_filter", "rsi_period", "rsi_threshold", "use_vwap_band", 
                        "vwap_band_sigma", "is_running"
                    ]
                    for vk in v_keys:
                        old_key = f"virtual_{vk}"
                        if old_key in file_config:
                            config[f"virtual_1_{vk}"] = file_config[old_key]
                        elif old_key in config:
                            config[f"virtual_1_{vk}"] = config[old_key]
                    is_migrated_v1 = True
                
                if is_migrated_v1:
                    cls.save_config(config)

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
            val = save_data.get(key)
            if val:
                # 이미 암호화된 Fernet 토큰 형식인지 체크하여 이중 암호화 및 복호화 실패 시 데이터 오염 방지
                is_already_encrypted = False
                if isinstance(val, str) and val.startswith("gAAAAA") and len(val) >= 50:
                    try:
                        # 복호화가 성공하면 이미 올바르게 암호화된 값임
                        dec = VwapCrypto.decrypt(val)
                        if dec:
                            is_already_encrypted = True
                    except Exception:
                        pass
                
                if not is_already_encrypted:
                    # 평문일 때만 새로 암호화하여 저장
                    save_data[key] = VwapCrypto.encrypt(val)

        # 패스워드 해시는 파일에 굳이 안 써도 되나, 대시보드 저장 시 유지
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[ConfigManager] Failed to save config: {e}")

    @staticmethod
    def load_trades(mode: str = "VIRTUAL") -> list:
        """가상/실제 거래 이력을 로드합니다."""
        trades_path = os.path.join(DATA_DIR, f"vwap_trades_{mode.lower()}.json")
        if not os.path.exists(trades_path):
            # 하위 호환: 기존 vwap_trades.json이 있고 mode가 VIRTUAL이면 마이그레이션
            legacy_path = os.path.join(DATA_DIR, "vwap_trades.json")
            if mode == "VIRTUAL" and os.path.exists(legacy_path):
                try:
                    os.rename(legacy_path, trades_path)
                except Exception:
                    pass
            else:
                return []
        try:
            with open(trades_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[ConfigManager] Failed to load trades for {mode}: {e}")
            return []

    @classmethod
    def save_trades(cls, trades: list, mode: str = "VIRTUAL"):
        """거래 이력을 저장합니다."""
        trades_path = os.path.join(DATA_DIR, f"vwap_trades_{mode.lower()}.json")
        try:
            with open(trades_path, "w", encoding="utf-8") as f:
                json.dump(trades, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[ConfigManager] Failed to save trades for {mode}: {e}")

    @classmethod
    def add_trade(cls, trade_item: dict, mode: str = "VIRTUAL"):
        """새로운 거래 이력을 추가합니다."""
        trades = cls.load_trades(mode)
        trades.append(trade_item)
        cls.save_trades(trades, mode)
