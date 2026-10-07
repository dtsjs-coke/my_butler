import os
import re
import hmac
import time
import json
import base64
import hashlib
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger("butler_auth")

# Key 파일 위치 설정 (프로젝트 루트 디렉토리 기준)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY_PATH = os.path.join(PROJECT_ROOT, "data", "vwap_secret.key")

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

    # ------------------------------------------------------------------
    # Admin 비밀번호 해시 (ADR-0011 D10)
    #
    # 저장 형식(문자열 하나에 알고리즘·파라미터·salt·결과를 모두 담는다):
    #   scrypt$n=16384,r=8,p=2$<salt>$<hash>        (우선)
    #   pbkdf2_sha256$i=400000$<salt>$<hash>        (scrypt 를 못 쓰는 환경의 대체 경로)
    #   <64자리 소문자 hex>                          (옛 형식: salt 없는 SHA-256. 검증만 하고, 로그인 성공 시 새 형식으로 바꾼다)
    # salt/hash 는 base64url(패딩 없음). 구분자 '$' 는 base64url 에 나오지 않는다.
    #
    # 파라미터 근거: 로컬 PC(Intel Core Ultra)에서 scrypt(n=2^14,r=8,p=2) 약 0.07초, pbkdf2 40만 회 약 0.08초.
    # S9(Exynos 9810)는 단일 코어 기준 대략 6~8배 느리다고 보고 둘 다 0.4~0.6초 안팎을 목표로 잡았다.
    # scrypt 메모리 = 128*r*n = 16MiB. 동시 로그인이 몰려도 S9 메모리를 크게 잡아먹지 않도록 n 은 2^14 로 두고 p 로 시간을 늘린다.
    # ------------------------------------------------------------------
    PW_SCRYPT_N = 2 ** 14
    PW_SCRYPT_R = 8
    PW_SCRYPT_P = 2
    PW_SCRYPT_MAXMEM = 64 * 1024 * 1024
    PW_PBKDF2_ITER = 400_000
    PW_SALT_BYTES = 16
    PW_DKLEN = 32
    PW_MAX_LEN = 1024  # 이보다 긴 입력은 해시하지 않고 실패 처리

    # 저장된 문자열을 파싱할 때 허용하는 범위(손상·조작된 값이 CPU/메모리를 폭주시키지 않게)
    _SCRYPT_N_MAX = 2 ** 20
    _SCRYPT_R_MAX = 32
    _SCRYPT_P_MAX = 16
    _PBKDF2_ITER_MIN = 10_000
    _PBKDF2_ITER_MAX = 5_000_000
    _LEGACY_RE = re.compile(r"^[0-9a-f]{64}$")
    _B64_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

    _scrypt_ok = None  # None = 아직 확인 안 함. True/False 는 프로세스 수명 동안 캐시

    @classmethod
    def _reset_scrypt_probe(cls):
        """(테스트용) scrypt 사용 가능 여부 캐시를 비운다."""
        cls._scrypt_ok = None

    @classmethod
    def scrypt_available(cls) -> bool:
        """이 런타임에서 hashlib.scrypt 를 쓸 수 있는지. OpenSSL 빌드에 따라 함수가 없거나 호출이 실패할 수 있다."""
        if cls._scrypt_ok is None:
            fn = getattr(hashlib, "scrypt", None)
            ok = False
            if callable(fn):
                try:
                    fn(b"probe", salt=b"probe-salt-16byt", n=16, r=1, p=1, dklen=16)
                    ok = True
                except Exception as e:
                    logger.warning(f"[비밀번호 해시] scrypt 사용 불가 -> pbkdf2 로 대체: {type(e).__name__}")
            else:
                logger.warning("[비밀번호 해시] hashlib.scrypt 없음 -> pbkdf2 로 대체")
            cls._scrypt_ok = ok
        return cls._scrypt_ok

    @staticmethod
    def _b64e(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def _b64d(cls, text: str) -> bytes:
        if not cls._B64_RE.match(text):
            raise ValueError("bad base64")
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    @classmethod
    def _scrypt(cls, pw: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
        return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, maxmem=cls.PW_SCRYPT_MAXMEM, dklen=dklen)

    @staticmethod
    def _pbkdf2(pw: bytes, salt: bytes, iterations: int, dklen: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", pw, salt, iterations, dklen)

    @staticmethod
    def legacy_hash_password(password: str) -> str:
        """옛 형식(salt 없는 SHA-256 hex). 새로 저장하는 데는 쓰지 않는다.
        설정 로드 시 .env(VWAP_ADMIN_PASSWORD) 비밀번호의 대체 해시로만 쓴다(load_config 는 자주 불리므로 매번 느린 KDF 를 돌리지 않는다).
        이 형식으로 로그인에 성공하면 그 자리에서 새 형식으로 바뀌어 설정 파일에 저장된다."""
        if not password:
            return ""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()

    @classmethod
    def hash_password(cls, password: str) -> str:
        """Admin 비밀번호를 salt 포함 KDF 해시 문자열로 만든다(scrypt 우선, 안 되면 pbkdf2_sha256).
        빈 값/문자열이 아닌 값/너무 긴 값은 "" 를 돌려준다(호출자는 "" 를 저장하지 않는다)."""
        if not password or not isinstance(password, str) or len(password) > cls.PW_MAX_LEN:
            return ""
        pw = password.encode("utf-8")
        salt = os.urandom(cls.PW_SALT_BYTES)
        if cls.scrypt_available():
            n, r, p = cls.PW_SCRYPT_N, cls.PW_SCRYPT_R, cls.PW_SCRYPT_P
            try:
                dk = cls._scrypt(pw, salt, n, r, p, cls.PW_DKLEN)
                return f"scrypt$n={n},r={r},p={p}${cls._b64e(salt)}${cls._b64e(dk)}"
            except Exception as e:
                # 작은 값으로는 되지만 실제 파라미터(메모리 한도 등)에서 실패하는 빌드 → 이후로는 pbkdf2 만 쓴다
                logger.warning(f"[비밀번호 해시] scrypt 실행 실패 -> pbkdf2 로 대체: {type(e).__name__}")
                cls._scrypt_ok = False
        it = cls.PW_PBKDF2_ITER
        dk = cls._pbkdf2(pw, salt, it, cls.PW_DKLEN)
        return f"pbkdf2_sha256$i={it}${cls._b64e(salt)}${cls._b64e(dk)}"

    @staticmethod
    def _parse_params(text: str) -> dict:
        out = {}
        for part in text.split(","):
            k, sep, v = part.partition("=")
            if not sep or not k or not v.isdigit() or len(v) > 9 or k in out:
                raise ValueError("bad params")
            out[k] = int(v)
        return out

    @classmethod
    def verify_password(cls, password, stored) -> tuple:
        """비밀번호를 저장된 해시와 상수 시간(hmac.compare_digest)으로 비교한다.

        Returns: (일치 여부, 재해시 필요 여부)
          - 재해시 필요: 일치했고, 저장 형식이 옛 SHA-256 이거나 지금 쓰는 알고리즘·파라미터보다 약할 때만 True.
          - 형식이 잘못됐거나 계산할 수 없으면 예외 없이 (False, False).
        """
        try:
            if not password or not isinstance(password, str) or len(password) > cls.PW_MAX_LEN:
                return False, False
            if not stored or not isinstance(stored, str):
                return False, False
            pw = password.encode("utf-8")

            if cls._LEGACY_RE.match(stored):
                calc = hashlib.sha256(pw).hexdigest()
                ok = hmac.compare_digest(calc.encode("ascii"), stored.encode("ascii"))
                return ok, ok

            parts = stored.split("$")
            if len(parts) != 4:
                return False, False
            alg, params_text, salt_text, dk_text = parts
            params = cls._parse_params(params_text)
            salt = cls._b64d(salt_text)
            expected = cls._b64d(dk_text)
            if len(salt) < 8 or not (16 <= len(expected) <= 64):
                return False, False

            if alg == "scrypt":
                if set(params) != {"n", "r", "p"}:
                    return False, False
                n, r, p = params["n"], params["r"], params["p"]
                if n < 2 or n > cls._SCRYPT_N_MAX or (n & (n - 1)) or not (1 <= r <= cls._SCRYPT_R_MAX) \
                        or not (1 <= p <= cls._SCRYPT_P_MAX):
                    return False, False
                if not cls.scrypt_available():
                    logger.error("[비밀번호 해시] 저장된 해시가 scrypt 인데 이 런타임에서 scrypt 를 쓸 수 없어 검증 불가")
                    return False, False
                calc = cls._scrypt(pw, salt, n, r, p, len(expected))
                ok = hmac.compare_digest(calc, expected)
                weaker = (n, r, p) != (cls.PW_SCRYPT_N, cls.PW_SCRYPT_R, cls.PW_SCRYPT_P)
                return ok, bool(ok and weaker)

            if alg == "pbkdf2_sha256":
                if set(params) != {"i"}:
                    return False, False
                it = params["i"]
                if not (cls._PBKDF2_ITER_MIN <= it <= cls._PBKDF2_ITER_MAX):
                    return False, False
                calc = cls._pbkdf2(pw, salt, it, len(expected))
                ok = hmac.compare_digest(calc, expected)
                # scrypt 를 쓸 수 있게 됐거나 반복 횟수가 기준보다 적으면 다음 로그인 때 갱신
                weaker = cls.scrypt_available() or it < cls.PW_PBKDF2_ITER
                return ok, bool(ok and weaker)

            return False, False
        except Exception as e:
            logger.error(f"[비밀번호 해시] 검증 중 오류(형식 손상 의심): {type(e).__name__}")
            return False, False

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
