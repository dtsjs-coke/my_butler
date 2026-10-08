import os
import re
import json
import tempfile
import logging
import threading
from datetime import datetime
from dotenv import load_dotenv
from core.vwap.crypto import VwapCrypto

_auth_logger = logging.getLogger("butler_auth")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "vwap_config.json")
TRADES_PATH = os.path.join(DATA_DIR, "vwap_trades.json")

# 암호화하여 저장할 민감한 필드 목록
SENSITIVE_KEYS = ["toss_client_secret", "toss_account_seq"]

# 설정 파일 쓰기 직렬화(Flask 요청 스레드 + 봇 스레드). RLock: update_admin_password_hash 가 잡은 채로 다시 쓴다.
_config_write_lock = threading.RLock()

# 거래기록 파일명(vwap_trades_<mode>.json)에 들어가는 mode 는 영문·숫자·밑줄만 허용(경로 조작 방지, 심층 방어).
_SAFE_TRADES_MODE_RE = re.compile(r"[A-Za-z0-9_]{1,32}")


def _trades_path(mode) -> str:
    m = mode if isinstance(mode, str) else ""
    if not _SAFE_TRADES_MODE_RE.fullmatch(m):
        raise ValueError(f"허용되지 않는 거래기록 mode: {m[:40]!r}")
    return os.path.join(DATA_DIR, f"vwap_trades_{m.lower()}.json")


def _write_json_atomic(path: str, data) -> None:
    """같은 폴더의 임시 파일에 쓴 뒤 os.replace 로 교체합니다(쓰는 도중 꺼져도 기존 파일 보존). 실패 시 예외."""
    with _config_write_lock:
        fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                                        dir=os.path.dirname(path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

class VwapConfigManager:
    # add_trade 가 손상 파일을 백업했을 때의 알림 (mode -> [메시지]). 봇이 꺼내서 자기 로거에 ERROR 로 남깁니다.
    _trade_file_warnings = {}
    # (ADR-0011 정정 2026-10-07) admin 비밀번호 미설정 경고를 이미 남겼는지. load_config 는 봇 주기마다 불리므로
    # '설정됨 → 미설정'으로 바뀔 때 한 번만 경고한다(프로세스 메모리, 재시작하면 다시 한 번).
    _admin_pw_unset_warned = False

    @classmethod
    def pop_trade_file_warnings(cls, mode: str) -> list:
        """해당 모드의 거래기록 파일 경고 메시지를 꺼내고 비웁니다."""
        return cls._trade_file_warnings.pop(str(mode).upper(), [])

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

            # Discord 알림 (관측성) — 실거래는 기본 ON, 가상봇(1~3 공통)은 기본 OFF
            "real_discord_notify": True,
            "virtual_discord_notify": False,

            # 3단계(섀도우/리플레이/봉 적재) — docs/vwap_stage3_design.md §7
            "shadow_enabled": True,                # REAL 훅에서 섀도우 실행
            "shadow_fee_roundtrip_pct": 0.2,       # 섀도우 순손익 추정 수수료(왕복 %)
            "shadow_price_tolerance_pct": 0.05,    # MATCH 판정 허용 가격차(%)
            "bars_store_enabled": True,            # 마감 봉 적재
            "replay_timeout_sec": 300,             # 리플레이 자식 프로세스 제한시간(초)
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
        # (ADR-0011 D10) 여기는 설정 파일에 해시가 없을 때의 대체값이다. load_config 는 봇 주기마다 불리므로
        # 느린 KDF 대신 옛 형식(SHA-256)을 쓰고, 이 값으로 로그인에 성공하면 새 형식으로 바꿔 파일에 저장한다.
        # (ADR-0011 정정 2026-10-07) 코드에 기본 비밀번호를 두지 않는다(공개 저장소). .env 에도 없으면 해시는 ""
        # 로 남고, verify_password 는 빈 해시를 어떤 입력과도 불일치로 처리하므로 로그인이 항상 실패한다(fail-closed).
        if env_admin_pw and env_admin_pw.strip():
            config["admin_password_hash"] = VwapCrypto.legacy_hash_password(env_admin_pw)

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

        cls._check_admin_password_set(config)
        return config

    @classmethod
    def _check_admin_password_set(cls, config: dict) -> None:
        """(ADR-0011 정정 2026-10-07) 최종 해시가 비어 있으면(없음/None/공백/문자열 아님) "" 로 맞추고 경고를 한 번 남긴다.
        로그인 응답은 그대로 401(invalid_password)이다 — '미설정' 여부를 외부에 알리지 않는다."""
        stored = config.get("admin_password_hash")
        if isinstance(stored, str) and stored.strip():
            cls._admin_pw_unset_warned = False
            return
        config["admin_password_hash"] = ""
        if not cls._admin_pw_unset_warned:
            cls._admin_pw_unset_warned = True
            _auth_logger.warning("[로그인] 관리자 비밀번호가 설정되지 않아 로그인이 비활성화됨 — "
                                 ".env VWAP_ADMIN_PASSWORD 설정 후 재시작")

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
            _write_json_atomic(CONFIG_PATH, save_data)
        except Exception as e:
            print(f"[ConfigManager] Failed to save config: {e}")

    @classmethod
    def update_admin_password_hash(cls, new_hash: str, expected_old: str) -> bool:
        """(ADR-0011 D10) 로그인 성공 시 옛 형식 해시를 새 형식으로 바꿔 저장합니다(자동 마이그레이션).

        save_config 와 같은 파일·같은 원자적 쓰기를 쓰되, 파일의 원본 JSON 에서 admin_password_hash 한 키만 바꿉니다.
        load_config 결과를 통째로 저장하지 않는 이유: load_config 는 .env 값을 섞고, 파일이 손상돼 읽지 못하면
        기본값을 돌려주므로 그대로 저장하면 설정 파일을 덮어쓸 수 있습니다.
          - 파일이 없거나 JSON 객체가 아니면 저장하지 않습니다(False). 로그인 자체는 이미 성공한 상태입니다.
          - 그 사이 파일의 해시가 바뀌었으면(다른 요청이 비밀번호를 변경) 덮어쓰지 않습니다(False).
        Returns: 저장 여부
        """
        if not new_hash:
            return False
        with _config_write_lock:
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    file_config = json.load(f)
            except Exception as e:
                print(f"[ConfigManager] 비밀번호 해시 갱신 생략(설정 파일 읽기 실패): {type(e).__name__}")
                return False
            if not isinstance(file_config, dict):
                return False
            current = file_config.get("admin_password_hash") or ""
            # 파일에 해시가 비어 있으면 load_config 는 .env 대체 해시를 썼다 → 그 값과 일치한 경우 파일에 새로 기록
            if current and current != expected_old:
                return False
            file_config["admin_password_hash"] = new_hash
            try:
                _write_json_atomic(CONFIG_PATH, file_config)
                return True
            except Exception as e:
                print(f"[ConfigManager] 비밀번호 해시 갱신 저장 실패: {type(e).__name__}")
                return False

    @staticmethod
    def load_trades(mode: str = "VIRTUAL") -> list:
        """가상/실제 거래 이력을 로드합니다."""
        try:
            trades_path = _trades_path(mode)
        except ValueError as e:
            print(f"[ConfigManager] {e}")
            return []
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
    def save_trades(cls, trades: list, mode: str = "VIRTUAL") -> bool:
        """거래 이력을 원자적으로 저장합니다 (임시파일 작성 후 교체 — 쓰는 도중 꺼져도 기존 파일 보존).
        Returns: 저장 성공 여부"""
        try:
            trades_path = _trades_path(mode)
        except ValueError as e:
            print(f"[ConfigManager] {e}")
            return False
        tmp_path = trades_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(trades, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, trades_path)
            return True
        except Exception as e:
            print(f"[ConfigManager] Failed to save trades for {mode}: {e}")
            return False

    @classmethod
    def add_trade(cls, trade_item: dict, mode: str = "VIRTUAL") -> bool:
        """새로운 거래 이력을 추가합니다.

        기존 파일을 읽지 못했을 때 빈 목록으로 간주해 덮어쓰면 이력 전체가 1건으로 사라지므로:
          - 파일은 있는데 JSON 파싱 실패(손상): 원본을 `<파일>.corrupt-YYYYmmddHHMMSS` 로 옮겨 보존한 뒤
            새 파일에 이번 거래부터 기록하고 경고를 출력합니다 (복구는 수동).
          - 그 외 읽기 오류(권한/IO 등): 아무것도 쓰지 않고 False 반환.
        Returns: 기록 성공 여부
        """
        try:
            trades_path = _trades_path(mode)
        except ValueError as e:
            print(f"[ConfigManager] {e}")
            return False
        if os.path.exists(trades_path):
            try:
                with open(trades_path, "r", encoding="utf-8") as f:
                    trades = json.load(f)
                if not isinstance(trades, list):
                    raise ValueError(f"거래기록 최상위가 list가 아님: {type(trades).__name__}")
            except ValueError as e:  # json.JSONDecodeError 포함
                backup_path = f"{trades_path}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}"
                try:
                    os.replace(trades_path, backup_path)
                except Exception as be:
                    msg = f"손상된 거래기록({trades_path}) 백업 실패, 기록을 중단합니다: {be}"
                    print(f"[ConfigManager] ❌ {msg}")
                    cls._trade_file_warnings.setdefault(str(mode).upper(), []).append(msg)
                    return False
                msg = f"거래기록 파일이 손상되어({e}) {backup_path} 로 보존하고 새 파일에 기록합니다. 수동 복구가 필요합니다."
                print(f"[ConfigManager] ⚠️ {msg}")
                cls._trade_file_warnings.setdefault(str(mode).upper(), []).append(msg)
                trades = []
            except Exception as e:
                msg = f"거래기록({trades_path}) 읽기 실패 — 덮어쓰지 않고 기록을 중단합니다: {e}"
                print(f"[ConfigManager] ❌ {msg}")
                cls._trade_file_warnings.setdefault(str(mode).upper(), []).append(msg)
                return False
        else:
            trades = cls.load_trades(mode)  # 레거시 vwap_trades.json 마이그레이션 경로 포함
        trades.append(trade_item)
        return cls.save_trades(trades, mode)

    @staticmethod
    def load_tracked_orders(mode: str = "REAL") -> dict:
        """실거래 봇이 추적 중인 주문 상태(vwap_tracked_orders_<mode>.json)를 로드합니다.

        Returns:
            {"orders": {order_id: info}, "last_qty": {ticker: qty}} — 파일이 없거나 손상되면 빈 구조.
        """
        path = os.path.join(DATA_DIR, f"vwap_tracked_orders_{mode.lower()}.json")
        empty = {"orders": {}, "last_qty": {}}
        if not os.path.exists(path):
            return empty
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return empty
            orders = data.get("orders") if isinstance(data.get("orders"), dict) else {}
            last_qty = data.get("last_qty") if isinstance(data.get("last_qty"), dict) else {}
            return {"orders": orders, "last_qty": last_qty}
        except Exception as e:
            print(f"[ConfigManager] Failed to load tracked orders for {mode}: {e}")
            return empty

    @staticmethod
    def save_tracked_orders(data: dict, mode: str = "REAL"):
        """추적 주문 상태를 원자적으로 저장합니다 (임시파일 작성 후 교체 — 쓰는 도중 꺼져도 기존 파일 보존)."""
        path = os.path.join(DATA_DIR, f"vwap_tracked_orders_{mode.lower()}.json")
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, path)
        except Exception as e:
            print(f"[ConfigManager] Failed to save tracked orders for {mode}: {e}")
