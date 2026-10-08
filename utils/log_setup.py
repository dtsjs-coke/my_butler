"""로그 파일 설정 공용 모듈 (관측 전용 — 매매 판단/주문 경로와 무관).

- make_rotating_file_handler(): 크기 기준으로 회전하는 파일 핸들러를 만듭니다.
  봇별 로거(core/vwap/bot.py 의 setup_logger)와 공용 로거가 같은 규칙을 씁니다.
- setup_common_loggers(): 여러 모듈이 함께 쓰는 공용 로거("vwap_bot", "butler_auth")를
  앱 시작 시 한 번 설정합니다. 여러 번 불러도 핸들러가 늘어나지 않습니다(멱등).

회전 파일 이름: 기본 규칙(trading_bot_real.log.1)이 아니라 trading_bot_real.1.log 로 바꿔 둡니다.
sync_s9.py 의 제외 패턴 "trading_bot_*.log" 에 회전 파일까지 걸리게 하려는 것입니다
(걸리지 않으면 동기화 때 '로컬에 없는 원격 파일' 정리 단계가 S9 의 회전 로그를 지웁니다).
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler

# 파일 1개 최대 5MB, 백업 5개 → 로그 1종당 최대 약 30MB
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

COMMON_LOG_FILENAME = "trading_bot_common.log"
COMMON_LOGGER_NAMES = ("vwap_bot", "butler_auth")
LOG_FORMAT = "[%(asctime)s] %(levelname)s - %(message)s"
COMMON_LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 이 모듈이 붙인 핸들러 표시용 속성 (멱등 판단에 사용)
_MARK = "_butler_common_handler"

_DEFAULT_LOG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _rotation_namer(default_name: str) -> str:
    """'.../trading_bot_real.log.1' -> '.../trading_bot_real.1.log' (확장자 .log 유지)."""
    root, _, num = default_name.rpartition(".")
    if num.isdigit() and root.endswith(".log"):
        return f"{root[:-4]}.{num}.log"
    return default_name


def make_rotating_file_handler(path: str, max_bytes: int = LOG_MAX_BYTES,
                               backup_count: int = LOG_BACKUP_COUNT) -> RotatingFileHandler:
    """기존 파일에 이어 쓰는(mode='a') 크기 회전 핸들러. delay=True 라 첫 기록 때 파일을 엽니다.
    디스크 쓰기/회전 실패는 logging 이 내부에서 처리(stderr 에 안내)하며 호출한 쪽으로 예외가 올라가지 않습니다."""
    handler = RotatingFileHandler(path, mode="a", maxBytes=max_bytes, backupCount=backup_count,
                                  encoding="utf-8", delay=True)
    handler.namer = _rotation_namer
    return handler


def setup_common_loggers(log_dir: str = None, max_bytes: int = LOG_MAX_BYTES,
                         backup_count: int = LOG_BACKUP_COUNT):
    """공용 로거("vwap_bot", "butler_auth") 설정. 앱 시작 시 1회 호출(다시 불러도 핸들러 1개 유지).

    - 레벨 INFO. INFO 이상 → trading_bot_common.log (두 로거가 같은 핸들러 1개를 공유 — 같은 파일을
      두 핸들러가 따로 회전시키면 충돌하므로)
    - WARNING 이상 → stderr 1번 (설정 전 lastResort 와 같은 범위라 pm2 로그 양은 그대로, INFO 는 파일에만)
    - propagate=False: 나중에 누가 root 에 핸들러를 붙여도 이중 출력되지 않게.
    설정 중 예외가 나도 앱 기동을 막지 않습니다(이 경우 False 반환, 기존처럼 lastResort 로 동작)."""
    try:
        log_dir = log_dir or _DEFAULT_LOG_DIR
        loggers = [logging.getLogger(n) for n in COMMON_LOGGER_NAMES]

        def _find(kind):
            for lg in loggers:
                for hd in lg.handlers:
                    if getattr(hd, _MARK, None) == kind:
                        return hd
            return None

        file_handler = _find("file")
        if file_handler is None:
            file_handler = make_rotating_file_handler(os.path.join(log_dir, COMMON_LOG_FILENAME),
                                                      max_bytes, backup_count)
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(logging.Formatter(COMMON_LOG_FORMAT, datefmt=LOG_DATEFMT))
            setattr(file_handler, _MARK, "file")

        err_handler = _find("stderr")
        if err_handler is None:
            err_handler = logging.StreamHandler(sys.stderr)
            err_handler.setLevel(logging.WARNING)
            err_handler.setFormatter(logging.Formatter(COMMON_LOG_FORMAT, datefmt=LOG_DATEFMT))
            setattr(err_handler, _MARK, "stderr")

        for lg in loggers:
            lg.setLevel(logging.INFO)
            lg.propagate = False
            for hd in (file_handler, err_handler):
                if hd not in lg.handlers:
                    lg.addHandler(hd)
        return True
    except Exception as e:  # 로그 설정 실패가 봇/서버 기동을 막으면 안 됨
        try:
            sys.stderr.write(f"[log_setup] 공용 로거 설정 실패(기존 방식으로 계속): {e!r}\n")
        except Exception:
            pass
        return False
