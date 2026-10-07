"""리플레이 자식 프로세스 진입점:  python -m core.vwap.replay_job <job_id>   (설계 문서 §4.2)

부모(replay.start_job)가 상태 파일(data/replay/<job_id>.json)을 만든 뒤 이 프로세스를 띄웁니다.
단계: loading_data 10% → preparing 40% → simulating 50~90% → summarizing 95% → done 100%.
실패하면 상태 파일에 failed + error 코드(no_data / exception)를 남기고 종료 코드 1 로 끝납니다.
부모와 같은 data 폴더를 쓰도록 환경변수 VWAP_DATA_DIR 을 따릅니다(없으면 기본 DATA_DIR).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime

import core.vwap.config_manager as _cm
from core.vwap import replay


def _progress(job_id: str):
    def emit(stage: str, pct: int, message: str):
        replay.update_job(job_id, stage=stage, progress=int(pct), message=message)
    return emit


_hard_exit = os._exit   # 테스트에서 바꿔치기 가능


def _start_watchdog(job_id: str, timeout_sec: float):
    """자체 시간 제한: timeout_sec 후에도 끝나지 않으면 failed('timeout') 기록 후 스스로 종료. 취소용 Event 반환."""
    done = threading.Event()

    def _watch():
        if done.wait(timeout_sec):
            return
        try:
            replay.update_job(job_id, state="failed", error="timeout",
                              message=f"제한 시간({int(timeout_sec)}초)을 넘겨 중단했습니다. 기간을 줄이거나 봉 간격을 늘려보세요.",
                              finished_at=datetime.now().strftime(replay.DATE_FMT), finished_ts=time.time())
            print(f"[replay_job] {job_id} timeout {timeout_sec}s - 종료", file=sys.stderr)
        finally:
            _hard_exit(1)

    threading.Thread(target=_watch, name="replay-watchdog", daemon=True).start()
    return done


def run(job_id: str) -> int:
    """작업 1건 실행. 종료 코드(0 성공 / 1 실패)를 반환. (테스트에서 프로세스 없이 직접 호출 가능)"""
    job = replay.read_job(job_id)
    if job is None:
        print(f"[replay_job] 작업 파일이 없습니다: {job_id}", file=sys.stderr)
        return 1
    emit = _progress(job_id)
    started = time.time()
    try:
        timeout = float(job.get("timeout_sec") or replay.DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        timeout = float(replay.DEFAULT_TIMEOUT_SEC)
    watchdog = _start_watchdog(job_id, timeout)
    try:
        return _run_job(job_id, job, emit, started)
    finally:
        watchdog.set()


def _run_job(job_id: str, job: dict, emit, started: float) -> int:
    try:
        replay.update_job(job_id, state="running", stage="loading_data", progress=10, message="봉 데이터 불러오는 중",
                          started_at=datetime.now().strftime(replay.DATE_FMT), started_ts=started, pid=os.getpid())
        from core.vwap import bar_source
        params = job["params"]
        snap = params["config_snapshot"]
        as_of = datetime.strptime(params["as_of"], replay.DATE_FMT) if params.get("as_of") else None
        t_load = time.perf_counter()
        bars, meta = bar_source.load_bars(params["ticker"], params["interval"], params["days"], snap["reset_time"],
                                          market=snap.get("market"), now=as_of)
        load_sec = time.perf_counter() - t_load
        result = replay.compute_result(bars, meta, params, progress=emit)
        result["perf"]["load_sec"] = round(load_sec, 2)
        result["perf"]["total_sec"] = round(time.time() - started, 2)
        print(f"[replay_job] {job_id} 완료 perf={result['perf']}")
        done = replay.update_job(job_id, state="done", stage="done", progress=100, message="완료", error=None,
                                 result=result, finished_at=datetime.now().strftime(replay.DATE_FMT),
                                 finished_ts=time.time())
        return 0 if done is not None else 1   # None: 그 사이 부모가 timeout 등으로 종료 처리함
    except replay.NoDataError as e:
        print(f"[replay_job] no_data: {e}", file=sys.stderr)
        replay.update_job(job_id, state="failed", error="no_data", message=str(e),
                          finished_at=datetime.now().strftime(replay.DATE_FMT), finished_ts=time.time())
        return 1
    except Exception as e:
        traceback.print_exc()
        replay.update_job(job_id, state="failed", error="exception",
                          message=f"리플레이 계산 중 오류가 발생했습니다: {type(e).__name__}",
                          finished_at=datetime.now().strftime(replay.DATE_FMT), finished_ts=time.time())
        return 1


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("사용법: python -m core.vwap.replay_job <job_id>", file=sys.stderr)
        return 2
    data_dir = os.environ.get(replay.DATA_DIR_ENV)
    if data_dir:
        _cm.DATA_DIR = data_dir
    try:
        os.nice(10)   # 매매 봇보다 낮은 우선순위(POSIX 만, 실패 무시)
    except (AttributeError, OSError):
        pass
    return run(argv[0])


if __name__ == "__main__":
    sys.exit(main())
