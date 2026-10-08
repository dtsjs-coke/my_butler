"""로그 설정(utils/log_setup.py, core/vwap/bot.py setup_logger) 검증 — 네트워크 없음, 로그 파일은 임시 폴더에만.

검증 항목
  L1. 캔들 timestamp 확인(probe) INFO 가 "vwap_bot" 로거로 실제 기록됨 (assertLogs)
  L2. setup_common_loggers 후 probe INFO 가 trading_bot_common.log 에 실제로 남음
  L3. 멱등: 두 번 설정해도 로거마다 파일 핸들러 1개 + stderr 핸들러 1개, 두 로거가 파일 핸들러 공유
  L4. stderr 중복 없음: INFO 는 stderr 에 안 나감, WARNING 은 stderr 에 정확히 1번, root 로 전파 안 됨
  L5. 회전: 작은 maxBytes 로 회전 → <이름>.1.log, .2.log (백업 개수 상한), sync 제외 패턴 trading_bot_*.log 에 맞음
  L6. 이어 쓰기: 기존 파일 내용을 지우지 않음
  L7. setup_logger(봇별) 가 RotatingFileHandler(5MB x 5) 를 붙이고, 지정 폴더 밖(프로젝트 루트)에 파일을 만들지 않음
  L8. 디스크 쓰기/회전 실패가 logger 호출자에게 예외로 올라가지 않음, 공용 설정 실패도 예외 없이 False
  L9. 운영 data/ 해시 전후 동일

실행:  python scripts/test_vwap_logging.py
"""
import io
import os
import sys
import shutil
import fnmatch
import logging
import tempfile
import unittest
import traceback
from logging.handlers import RotatingFileHandler
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# 1단계 하네스 재사용: 운영 data/ 해시 스냅샷 + DATA_DIR 임시 폴더 패치 + 봇 로거 선등록(운영 로그 파일 보호)
import test_vwap_reliability as h  # noqa: E402

import utils.log_setup as log_setup  # noqa: E402
import core.vwap.bot as bot_module  # noqa: E402
from core.vwap.broker import TossBroker  # noqa: E402

check = h.check
LOG_TMP = tempfile.mkdtemp(prefix="vwap_logging_test_")
SYNC_PATTERN = "trading_bot_*.log"   # dev_pjt/sync_manager/sync_s9.py exclude_list 의 항목


class _TC(unittest.TestCase):
    def runTest(self):  # pragma: no cover
        pass


TC = _TC()


def _snapshot(names):
    return {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers), logging.getLogger(n).propagate)
            for n in names}


def _restore(snap):
    for n, (level, handlers, prop) in snap.items():
        lg = logging.getLogger(n)
        for hd in list(lg.handlers):
            if hd not in handlers:
                lg.removeHandler(hd)
                try:
                    hd.close()
                except Exception:
                    pass
        for hd in handlers:
            if hd not in lg.handlers:
                lg.addHandler(hd)
        lg.setLevel(level)
        lg.propagate = prop


def _probe(broker=None):
    TossBroker._candle_probe_logged = False
    b = broker or TossBroker("id", "secret", "1")
    raw = [{"timestamp": "2026-10-06T03:00:00+09:00"}, {"timestamp": "2026-10-06T02:59:00+09:00"}]
    b._log_candle_timestamp_probe(raw, {"nextBefore": "x"})


def _marked(lg, kind):
    return [hd for hd in lg.handlers if getattr(hd, log_setup._MARK, None) == kind]


def test_probe_assertlogs():
    print("\nL1. 캔들 probe INFO 가 vwap_bot 로거로 기록되는지 (assertLogs)")
    with TC.assertLogs("vwap_bot", level="INFO") as cm:
        _probe()
    hit = [r for r in cm.records if "캔들 timestamp 확인(1회)" in r.getMessage()]
    check("probe INFO 1건 기록 (logger=vwap_bot, level=INFO)",
          len(hit) == 1 and hit[0].name == "vwap_bot" and hit[0].levelno == logging.INFO,
          str([(r.name, r.levelname) for r in cm.records]))


def test_common_logger_file_and_idempotent():
    print("\nL2~L4. 공용 로거: 파일 기록 / 멱등 / stderr 중복 없음 / 전파 없음")
    names = list(log_setup.COMMON_LOGGER_NAMES)
    snap = _snapshot(names + [""])
    d = os.path.join(LOG_TMP, "common")
    os.makedirs(d)
    fake_err = io.StringIO()
    root_buf = io.StringIO()
    root_h = logging.StreamHandler(root_buf)
    try:
        with mock.patch.object(sys, "stderr", fake_err):
            ok1 = log_setup.setup_common_loggers(log_dir=d)
            ok2 = log_setup.setup_common_loggers(log_dir=d)
        vb, au = logging.getLogger("vwap_bot"), logging.getLogger("butler_auth")
        check("setup_common_loggers 2회 호출 모두 True", ok1 is True and ok2 is True)
        check("(멱등) 로거마다 파일 핸들러 1개 + stderr 핸들러 1개",
              all(len(_marked(lg, "file")) == 1 and len(_marked(lg, "stderr")) == 1 for lg in (vb, au)),
              str([[type(x).__name__ for x in lg.handlers] for lg in (vb, au)]))
        check("두 로거가 같은 파일 핸들러 객체 공유(같은 파일을 두 핸들러가 회전시키지 않음)",
              _marked(vb, "file")[0] is _marked(au, "file")[0])
        fh = _marked(vb, "file")[0]
        check("파일 핸들러 = RotatingFileHandler, 5MB x 5, trading_bot_common.log",
              isinstance(fh, RotatingFileHandler) and fh.maxBytes == 5 * 1024 * 1024 and fh.backupCount == 5
              and os.path.basename(fh.baseFilename) == "trading_bot_common.log",
              f"{type(fh).__name__} {fh.maxBytes} {fh.backupCount} {fh.baseFilename}")
        check("파일명이 sync 제외 패턴 trading_bot_*.log 에 맞음",
              fnmatch.fnmatch(os.path.basename(fh.baseFilename), SYNC_PATTERN))
        check("레벨 INFO, propagate=False", all(lg.level == logging.INFO and lg.propagate is False for lg in (vb, au)))

        logging.getLogger("").addHandler(root_h)   # 누가 root 에 핸들러를 붙였다고 가정 → 이중 출력 없어야 함
        # (L2) probe INFO → 파일
        _probe()
        vb.info("plain-info-line")
        vb.warning("warn-line-once")
        au.warning("[로그인] 실패 client=test 최근 60초 1/5회")
        fh.flush()
        with open(fh.baseFilename, encoding="utf-8") as f:
            content = f.read()
        check("(L2) probe INFO 가 trading_bot_common.log 에 기록됨",
              "캔들 timestamp 확인(1회)" in content and "INFO vwap_bot" in content, content[:200])
        check("butler_auth WARNING 도 같은 파일에 logger 이름과 함께 기록", "WARNING butler_auth" in content)
        err = fake_err.getvalue()
        check("(L4) INFO 는 stderr(pm2) 로 나가지 않음",
              "plain-info-line" not in err and "캔들 timestamp" not in err, err[:200])
        check("(L4) WARNING 은 stderr 에 정확히 1번", err.count("warn-line-once") == 1, err[:200])
        check("(L4) root 로 전파되지 않음(root 핸들러 출력 0)", root_buf.getvalue() == "", root_buf.getvalue()[:200])
        check("지정 폴더 밖(프로젝트 루트)에 trading_bot_common.log 를 만들지 않음",
              not os.path.exists(os.path.join(PROJECT_ROOT, "trading_bot_common.log")))
    finally:
        logging.getLogger("").removeHandler(root_h)
        _restore(snap)
    check("테스트 후 원래 로거 상태 복원(다른 테스트의 vwap_bot 레벨 패턴과 충돌 없음)",
          _snapshot(names) == {n: snap[n] for n in names})


def test_rotation():
    print("\nL5~L6. 회전 / 이어 쓰기")
    d = os.path.join(LOG_TMP, "rot")
    os.makedirs(d)
    path = os.path.join(d, "trading_bot_rotest.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("EXISTING-LINE\n")
    lg = logging.getLogger("vwap_logging_test_rot")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    hd = log_setup.make_rotating_file_handler(path, max_bytes=300, backup_count=2)
    lg.addHandler(hd)
    try:
        lg.info("first-after-existing")
        hd.flush()
        with open(path, encoding="utf-8") as f:
            first = f.read()
        check("(L6) 기존 내용 유지 + 이어 쓰기", first.startswith("EXISTING-LINE\n") and "first-after-existing" in first)
        for i in range(60):
            lg.info(f"line-{i:03d} " + "x" * 40)
        hd.flush()
        files = sorted(os.listdir(d))
        check("(L5) 회전 파일 = rotest.log / rotest.1.log / rotest.2.log (백업 2개 상한)",
              files == ["trading_bot_rotest.1.log", "trading_bot_rotest.2.log", "trading_bot_rotest.log"], str(files))
        check("(L5) 각 파일 크기 <= maxBytes 근처", all(os.path.getsize(os.path.join(d, x)) <= 300 + 100 for x in files),
              str([os.path.getsize(os.path.join(d, x)) for x in files]))
        check("(L5) 모든 회전 파일이 sync 제외 패턴 trading_bot_*.log 에 맞음",
              all(fnmatch.fnmatch(x, SYNC_PATTERN) for x in files))
        check("(L5) 기본 이름(.log.1)이었다면 패턴에 안 맞음 — namer 가 필요한 이유",
              not fnmatch.fnmatch("trading_bot_rotest.log.1", SYNC_PATTERN))
        with open(os.path.join(d, "trading_bot_rotest.log"), encoding="utf-8") as f:
            cur = f.read()
        check("(L5) 최신 줄은 현재 파일에", "line-059" in cur)
    finally:
        lg.removeHandler(hd)
        hd.close()


def test_setup_logger_rotating():
    print("\nL7. 봇별 setup_logger → RotatingFileHandler, 지정 폴더에만")
    d = os.path.join(LOG_TMP, "bot")
    os.makedirs(d)
    name = "vwap_bot_logtest"
    lg = logging.getLogger(name)
    try:
        with mock.patch.object(bot_module, "PROJECT_ROOT", d):
            lg1 = bot_module.setup_logger("LOGTEST")
            lg2 = bot_module.setup_logger("LOGTEST")
        fhs = [x for x in lg1.handlers if isinstance(x, logging.FileHandler)]
        check("파일 핸들러 1개 = RotatingFileHandler(5MB x 5), 두 번 불러도 핸들러 수 그대로(2)",
              lg1 is lg2 and len(lg1.handlers) == 2 and len(fhs) == 1 and isinstance(fhs[0], RotatingFileHandler)
              and fhs[0].maxBytes == 5 * 1024 * 1024 and fhs[0].backupCount == 5,
              str([type(x).__name__ for x in lg1.handlers]))
        check("파일 경로 = <지정폴더>/trading_bot_logtest.log (이름 규칙 유지)",
              fhs and fhs[0].baseFilename == os.path.join(d, "trading_bot_logtest.log"))
        for x in lg1.handlers:
            if not isinstance(x, logging.FileHandler):
                x.setLevel(logging.CRITICAL)   # 테스트 출력 정리
        lg1.info("bot-line")
        fhs[0].flush()
        with open(os.path.join(d, "trading_bot_logtest.log"), encoding="utf-8") as f:
            check("봇 로그가 지정 폴더 파일에 기록", "bot-line" in f.read())
        check("프로젝트 루트에 trading_bot_logtest.log 없음",
              not os.path.exists(os.path.join(PROJECT_ROOT, "trading_bot_logtest.log")))

        # 핸들러 생성이 실패해도 setup_logger 는 예외 없이 콘솔만 붙임
        name2 = "vwap_bot_logtest2"
        with mock.patch.object(bot_module, "make_rotating_file_handler", side_effect=OSError("disk")), \
                mock.patch.object(bot_module, "PROJECT_ROOT", d), mock.patch("builtins.print"):
            lg3 = bot_module.setup_logger("LOGTEST2")
        check("(L8) 파일 핸들러 생성 실패 → 예외 없이 콘솔 핸들러만",
              len(lg3.handlers) == 1 and not isinstance(lg3.handlers[0], logging.FileHandler))
    finally:
        for n in (name, "vwap_bot_logtest2"):
            g = logging.getLogger(n)
            for x in list(g.handlers):
                g.removeHandler(x)
                x.close()


def test_logging_failures_do_not_raise():
    print("\nL8. 디스크 쓰기/회전 실패가 호출자(봇 루프)로 예외를 올리지 않음")
    lg = logging.getLogger("vwap_logging_test_fail")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    old_raise = logging.raiseExceptions
    logging.raiseExceptions = False   # handleError 의 stderr 트레이스백 출력만 끔(동작은 동일하게 '삼킴')
    raised = []
    # (a) 없는 폴더(파일 열기 실패)
    bad = log_setup.make_rotating_file_handler(os.path.join(LOG_TMP, "no_such_dir", "trading_bot_x.log"))
    lg.addHandler(bad)
    try:
        lg.info("will-fail-open")
    except Exception as e:
        raised.append(repr(e))
    finally:
        lg.removeHandler(bad)
        bad.close()
    # (b) 회전(rename) 실패
    d = os.path.join(LOG_TMP, "failrot")
    os.makedirs(d)
    hd = log_setup.make_rotating_file_handler(os.path.join(d, "trading_bot_fr.log"), max_bytes=50, backup_count=2)
    hd.rotate = mock.Mock(side_effect=PermissionError("locked"))
    lg.addHandler(hd)
    try:
        for i in range(5):
            lg.info("rotate-fail " + "y" * 30)
    except Exception as e:
        raised.append(repr(e))
    finally:
        lg.removeHandler(hd)
        hd.close()
        logging.raiseExceptions = old_raise
    check("(L8) 파일 열기 실패·회전 실패 모두 logger 호출에서 예외 없음", not raised, str(raised))

    # (c) 공용 설정 자체가 실패해도 예외 없이 False, 로거 상태는 건드리지 않음
    names = list(log_setup.COMMON_LOGGER_NAMES)
    snap = _snapshot(names)
    try:
        with mock.patch.object(log_setup, "make_rotating_file_handler", side_effect=OSError("disk")), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            ok = log_setup.setup_common_loggers(log_dir=LOG_TMP)
        check("(L8) setup_common_loggers 실패 → False 반환(예외 없음), 로거 상태 불변",
              ok is False and _snapshot(names) == snap)
    finally:
        _restore(snap)


def main():
    print("=" * 70)
    print(" 로그 설정 검증 (네트워크 없음, 로그 임시 폴더: %s)" % LOG_TMP)
    print("=" * 70)
    root_logs_before = sorted(x for x in os.listdir(PROJECT_ROOT) if ".log" in x)
    tests = [test_probe_assertlogs, test_common_logger_file_and_idempotent, test_rotation,
             test_setup_logger_rotating, test_logging_failures_do_not_raise]
    for fn in tests:
        try:
            fn()
        except Exception:
            h.RESULTS.append((fn.__name__ + " (예외)", False))
            print(f"  [FAIL] {fn.__name__} 실행 중 예외")
            traceback.print_exc()

    root_logs_after = sorted(x for x in os.listdir(PROJECT_ROOT) if ".log" in x)
    check("프로젝트 루트 로그 파일 목록 변화 없음", root_logs_before == root_logs_after, str(root_logs_after))
    shutil.rmtree(LOG_TMP, ignore_errors=True)
    shutil.rmtree(h.TMP_DIR, ignore_errors=True)
    after = h._hash_dir(h.REAL_DATA_DIR)
    check("(L9) 운영 data/ 디렉터리 파일 변경 없음 (테스트 전후 해시 동일)", after == h.REAL_DATA_HASH_BEFORE)

    passed = sum(1 for _, ok in h.RESULTS if ok)
    print("\n" + "=" * 70)
    print(f" 결과: {passed}/{len(h.RESULTS)} 통과")
    print("=" * 70)
    sys.exit(0 if passed == len(h.RESULTS) else 1)


if __name__ == "__main__":
    main()
