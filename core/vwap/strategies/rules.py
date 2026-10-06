"""봇 단계 규칙을 순수 함수로 옮긴 것 (설계 문서 §2·§3).

is_waiting_for_start 는 core/vwap/bot.py _loop_step_body 의 '7-1. 거래 시작 시간 대기' 로직과 동치입니다
(ADR-0008 반영: 세션 시작은 SessionSpec 기준, US_AUTO 에서 시작 시각이 개장보다 최대 1시간 앞이면 대기 없음).
start_wait_blocks 는 7-1 에서 '대기 중 어떤 신호를 WAIT 로 덮는가'(ADR-0009: 신규 진입만 막음)와 동치입니다.
동치는 scripts/test_vwap_stage3_plugin.py T-P4·T-P5 가 '실제 봇 _loop_step 실행 결과'와 대조해 검증합니다.

주의: bot.py 는 아직 이 함수를 쓰지 않습니다(REAL 경로 무변경 원칙). bot 7-1 을 고치면 이 파일도 함께 고쳐야 하며,
T-P4 가 둘의 불일치를 잡아냅니다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

from core.vwap.session import SessionSpec


def start_wait_window(now: datetime, start_time, reset_time, session: SessionSpec
                      ) -> Optional[Tuple[datetime, datetime]]:
    """거래 시작 대기 구간 [T_reset, T_start) 을 반환. 규칙이 적용되지 않으면 None.

    규칙이 적용되지 않는 경우 (bot 7-1 과 동일):
      - start_time 이 비어 있음
      - start_time 이 설정 reset_time 문자열과 같음
      - start_time 이 지금 세션의 실제 시작 시각(HH:MM)과 같음 (US_AUTO 에서 23:30 등)
      - start_time 형식 오류 (bot 은 에러 로그만 남기고 대기하지 않음)
    """
    sess_start = session.session_start(now)
    if not (start_time and start_time != reset_time and start_time != sess_start.strftime("%H:%M")):
        return None
    try:
        start_h, start_m = map(int, start_time.split(':'))
        t_reset = sess_start
        t_start_temp = t_reset.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        if t_start_temp >= t_reset:
            t_start = t_start_temp
        elif session.is_us_auto and t_reset - t_start_temp <= timedelta(hours=1):
            # (ADR-0008 D5) 서머타임 종료로 개장이 늦어져 시작 시각이 개장보다 최대 1시간 앞 → 대기 없음
            t_start = t_reset
        else:
            t_start = t_start_temp + timedelta(days=1)
        return t_reset, t_start
    except Exception:
        return None


def is_waiting_for_start(now: datetime, start_time, reset_time, session: SessionSpec) -> bool:
    """now 가 거래 시작 대기 구간 [T_reset, T_start) 안이면 True.
    이때 무엇을 막는지는 start_wait_blocks 가 정합니다 (ADR-0009: 신규 진입만 막고 보유 포지션 관리는 그대로)."""
    window = start_wait_window(now, start_time, reset_time, session)
    if window is None:
        return False
    t_reset, t_start = window
    try:
        return t_reset <= now < t_start
    except Exception:  # bot 7-1 은 비교까지 try 안에서 하므로 예외 시 대기 없음
        return False


def start_wait_blocks(signal: str, position_qty: float) -> bool:
    """거래 시작 대기 중일 때 이번 전략 신호를 WAIT(WAIT_START_TIME)로 덮어야 하면 True (ADR-0009).

    - 무보유(position_qty <= 0): 덮음 → 신규 진입(BUY) 차단. 9-4 에서 남은 미체결 주문도 정리됨
    - 보유 중: 덮지 않음 → STOP_LOSS(시장가 손절)·SELL(지정가 매도)·HOLD 는 대기와 무관하게 평소대로 처리
      (get_signals 는 보유 중 BUY 를 내지 않지만, 혹시 BUY 가 오면 신규 진입이므로 덮음)
    bot.py 7-1 의 `if is_waiting_for_start and (qty <= 0 or signal == "BUY")` 와 같은 조건입니다.
    """
    return position_qty <= 0 or signal == "BUY"
