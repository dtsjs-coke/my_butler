import os
import time
import json
import hashlib
from cryptography.fernet import Fernet

# Key 파일 위치 설정 (프로젝트 루트 디렉토리 기준)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(PROJECT_ROOT, "vwap_secret.key")

class VwapCrypto:
    _fernet = None

    @classmethod
    def _initialize(cls):
        """Fernet 인스턴스를 지연 초기화(Lazy Initialization)합니다."""
        if cls._fernet is not None:
            return

        if not os.path.exists(KEY_PATH):
            # 키 파일이 없으면 새로 생성
            key = Fernet.generate_key()
            with open(KEY_PATH, "wb") as f:
                f.write(key)
        else:
            # 기존 키 로드
            with open(KEY_PATH, "rb") as f:
                key = f.read()

        cls._fernet = Fernet(key)

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        """평문 문자열을 암호화하여 base64 인코딩된 암호문 문자열을 반환합니다."""
        if not plaintext:
            return ""
        cls._initialize()
        encrypted_bytes = cls._fernet.encrypt(plaintext.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")

    @classmethod
    def decrypt(cls, ciphertext: str) -> str:
        """base64 암호문을 복호화하여 평문 문자열을 반환합니다."""
        if not ciphertext:
            return ""
        cls._initialize()
        try:
            decrypted_bytes = cls._fernet.decrypt(ciphertext.encode("utf-8"))
            return decrypted_bytes.decode("utf-8")
        except Exception as e:
            print(f"[Crypto] Decryption error: {e}")
            return ""

    @classmethod
    def hash_password(cls, password: str) -> str:
        """Admin 비밀번호를 단방향 SHA-256 해시값으로 변환합니다."""
        if not password:
            return ""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    @classmethod
    def generate_session_token(cls, username: str = "admin") -> str:
        """토큰 위조를 막기 위해 유저 이름과 현재 시간을 포함한 암호화 세션 토큰을 생성합니다."""
        token_data = {
            "username": username,
            "created_at": time.time()
        }
        # JSON 문자열로 직렬화 후 Fernet으로 암호화
        serialized = json.dumps(token_data)
        return cls.encrypt(serialized)

    @classmethod
    def verify_session_token(cls, token: str, max_age_seconds: int = 86400) -> bool:
        """세션 토큰을 복호화하여 유효성 및 만료 여부(기본 24시간)를 검증합니다."""
        if not token:
            return False
        
        decrypted = cls.decrypt(token)
        if not decrypted:
            return False

        try:
            token_data = json.loads(decrypted)
            username = token_data.get("username")
            created_at = token_data.get("created_at", 0)

            if username != "admin":
                return False

            # 만료 시간 검증
            if time.time() - created_at > max_age_seconds:
                return False

            return True
        except Exception:
            return False
