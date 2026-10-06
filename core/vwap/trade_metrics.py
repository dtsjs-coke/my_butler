"""거래 레코드 보강용 계산 헬퍼 (관측성 전용 — 매매 판단에는 쓰이지 않습니다).

실거래(bot._record_real_fill)와 가상(VirtualBroker) 레코드가 같은 정의를 쓰도록 한곳에 둡니다.
"""
from datetime import datetime

TS_FMT = "%Y-%m-%d %H:%M:%S"


def compute_slippage(side: str, fill_price, intended_price):
    """슬리피지 = 실제 체결가와 '의도한 가격'의 차이. **불리하면 양수**로 정의합니다.

      - 매수(BUY):              slippage = 체결가 - 의도가   (의도보다 비싸게 사면 +)
      - 매도(SELL / STOP_LOSS): slippage = 의도가 - 체결가   (의도보다 싸게 팔면 +)
      - slippage_pct = slippage / 의도가 * 100

    의도가: 지정가 주문은 주문 단가, 시장가 청산(손절/패닉)은 주문 결정 시점의 현재가(봉 종가).
    의도가가 없거나 0 이하이면 (None, None).
    """
    try:
        fp = float(fill_price)
        ip = float(intended_price or 0.0)
    except (TypeError, ValueError):
        return None, None
    if ip <= 0 or fp <= 0:
        return None, None
    slip = (fp - ip) if str(side).upper() == "BUY" else (ip - fp)
    return round(slip, 4), round(slip / ip * 100.0, 4)


def _parse_ts(ts):
    try:
        return datetime.strptime(str(ts), TS_FMT)
    except Exception:
        return None


def holding_minutes_for_exit(prior_trades: list, ticker: str, exit_ts: str):
    """청산(SELL/STOP_LOSS) 레코드의 보유 시간(분). 기존 거래기록을 시간순으로 따라가며
    '이 종목 보유가 0 → 양수로 바뀐 첫 매수 체결 시각'을 포지션 시작으로 봅니다. 판단 불가 시 None."""
    running = 0.0
    open_ts = None
    for t in prior_trades or []:
        if t.get("ticker") != ticker:
            continue
        try:
            q = float(t.get("qty") or 0.0)
        except (TypeError, ValueError):
            continue
        side = str(t.get("side", "")).upper()
        if side == "BUY":
            if running <= 1e-9:
                open_ts = t.get("timestamp")
            running += q
        elif side in ("SELL", "STOP_LOSS"):
            running -= q
            if running <= 1e-9:
                running = 0.0
                open_ts = None
    start = _parse_ts(open_ts) if open_ts else None
    end = _parse_ts(exit_ts)
    if not start or not end:
        return None
    return round(max((end - start).total_seconds(), 0.0) / 60.0, 1)


def enrich_record(record: dict, prior_trades: list, intended_price, meta: dict = None) -> dict:
    """거래 레코드에 관측성 필드를 추가합니다 (기존 필드는 건드리지 않음).

    추가: intended_price, slippage, slippage_pct, holding_minutes, reason_code, filters, config_snapshot
    meta: 주문 당시의 {"reason_code", "filters", "config_snapshot"} (없으면 빈 값)
    """
    meta = meta or {}
    side = str(record.get("side", "")).upper()
    slip, slip_pct = compute_slippage(side, record.get("price"), intended_price)
    record.setdefault("intended_price", round(float(intended_price), 4) if intended_price else None)
    record["slippage"] = slip
    record["slippage_pct"] = slip_pct
    record["holding_minutes"] = (holding_minutes_for_exit(prior_trades, record.get("ticker"), record.get("timestamp"))
                                 if side in ("SELL", "STOP_LOSS") else None)
    record["reason_code"] = meta.get("reason_code") or record.get("reason_code") or ""
    record["filters"] = meta.get("filters") or record.get("filters") or {}
    record["config_snapshot"] = meta.get("config_snapshot") or record.get("config_snapshot") or {}
    return record
