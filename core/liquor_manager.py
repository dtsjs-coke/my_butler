"""
주류 구매 이력 관리 - 순수 로직 모듈.

설계 문서: docs/liquor_purchases_design.md
관련 ADR: docs/adr/0004-liquor-purchase-tracker.md

라우트(api/flask_app.py)는 이 모듈의 함수만 호출하고, 파싱/정규화/저장 로직을
직접 갖지 않는다(설계 문서 1장에 명시된 계층 분리).
"""
import os
import re
import csv
import io
import json
import uuid
import threading
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIQUOR_FILE = os.path.join(PROJECT_ROOT, "data", "liquor_purchases.json")
BACKUP_FILE = os.path.join(PROJECT_ROOT, "data", "liquor_purchases.bak.json")

_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 로드 / 저장 (원자적 쓰기 + 락 — 설계 문서 2-4)
# ---------------------------------------------------------------------------

def load_purchases() -> list:
    """파일이 없거나 비었거나 깨졌으면 빈 리스트를 반환한다(예외를 올리지 않음)."""
    if not os.path.exists(LIQUOR_FILE):
        return []
    try:
        with open(LIQUOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("liquor_purchases", []) or []
    except Exception:
        return []


def load_dismissed_pairs() -> list:
    """'병합 안 함' 처리된 product_key 쌍 목록. 없으면 빈 리스트(하위호환 - 기존 파일에는 이 필드가 없다)."""
    if not os.path.exists(LIQUOR_FILE):
        return []
    try:
        with open(LIQUOR_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("dismissed_merge_pairs", []) or []
    except Exception:
        return []


def save_purchases(records: list, dismissed_merge_pairs=None) -> None:
    """임시 파일에 다 쓴 뒤 os.replace()로 교체한다(원자적 쓰기). Flask threaded=True 대비 락 사용.

    dismissed_merge_pairs를 넘기지 않으면(대부분의 호출부가 그렇다) 파일에 이미 저장된
    값을 그대로 보존한다 - create/update/delete/merge_key 등 record만 다루는 기존 호출부가
    이 필드를 몰라도 실수로 지우지 않게 하기 위함이다.
    """
    if dismissed_merge_pairs is None:
        dismissed_merge_pairs = load_dismissed_pairs()
    payload = {
        "version": 1,
        "liquor_purchases": records,
        "dismissed_merge_pairs": dismissed_merge_pairs,
    }
    tmp_path = LIQUOR_FILE + ".tmp"
    with _LOCK:
        os.makedirs(os.path.dirname(LIQUOR_FILE), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, LIQUOR_FILE)


def dismiss_merge_suggestion(key_a: str, key_b: str) -> None:
    """product_key 쌍을 '병합 제안 안 보이게' 목록에 영구 추가한다. 순서 무관하게 정렬 저장.
    이미 등록된 쌍이면 아무 것도 하지 않는다(중복 추가 방지)."""
    if key_a == key_b:
        return
    pair = sorted([key_a, key_b])
    dismissed = load_dismissed_pairs()
    if pair in dismissed:
        return
    dismissed.append(pair)
    save_purchases(load_purchases(), dismissed)


def backup_purchases() -> None:
    """replace 모드 CSV 가져오기 직전 1회 백업(설계 문서 2-4, 4-7)."""
    if not os.path.exists(LIQUOR_FILE):
        return
    try:
        with open(LIQUOR_FILE, "r", encoding="utf-8") as src:
            content = src.read()
        with open(BACKUP_FILE, "w", encoding="utf-8") as dst:
            dst.write(content)
    except Exception as e:
        print(f"[Liquor Backup Error] {e}")


def list_purchases_sorted() -> list:
    """date 내림차순, 같은 날짜면 created_at 내림차순 (설계 문서 5-2)."""
    records = load_purchases()
    return sorted(
        records,
        key=lambda r: (r.get("date", ""), r.get("created_at", "")),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# ID / 타임스탬프
# ---------------------------------------------------------------------------

def new_id() -> str:
    """시간 기반이 아니라 난수 기반이라 CSV 일괄 저장 루프에서도 절대 겹치지 않는다(설계 문서 2-3)."""
    return uuid.uuid4().hex[:12]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# product_key 정규화 (설계 문서 3-3)
# ---------------------------------------------------------------------------

# 상품 식별과 무관한 잡음 토큰만 제거한다. 여기에 '년', 'CS', 숫자를 절대 추가하지 말 것.
_NOISE = ["면세", "免稅", "1병", "증정", "행사", "할인", "(정품)", "정품"]


def normalize_product_key(name_raw: str) -> str:
    s = (name_raw or "").strip()
    for token in _NOISE:
        s = s.replace(token, " ")
    s = s.upper()  # 영문만 영향받음. 한글은 그대로
    s = re.sub(r"\s+", " ", s).strip()
    return s or (name_raw or "").strip()


# ---------------------------------------------------------------------------
# 주종(category) 추정 (설계 문서 4-6)
# ---------------------------------------------------------------------------

_CATEGORY_KEYWORDS = {
    "위스키": ["위스키", "싱글몰트", "발렌타인", "글렌", "맥캘란", "카발란", "야마자키",
              "버번", "스카치", "라프로익", "아드벡", "WHISKY", "BOURBON"],
    "와인": ["와인", "샴페인", "까베르네", "피노", "메를로", "WINE"],
    "사케": ["사케", "니혼슈", "청주", "준마이", "다이긴조"],
    "브랜디": ["꼬냑", "코냑", "브랜디", "헤네시", "레미마르탱"],
}


def guess_category(name_raw: str) -> str:
    s = (name_raw or "").upper()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.upper() in s:
                return category
    return "미분류"


# ---------------------------------------------------------------------------
# 검증 / 숫자 파싱 헬퍼
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_date(s: str) -> bool:
    if not _DATE_RE.match(s or ""):
        return False
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _to_int_or_none(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _to_number_or_none(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CRUD (설계 문서 5-2 ~ 5-6)
# ---------------------------------------------------------------------------

def create_purchase(payload: dict):
    """1건 추가. 반환: (record, error_reason). 성공 시 error_reason은 None."""
    date = (payload.get("date") or "").strip()
    name_raw = (payload.get("name_raw") or "").strip()

    if not _is_valid_date(date):
        return None, "invalid_date"
    if not name_raw:
        return None, "empty_name"

    price_krw = _to_int_or_none(payload.get("price_krw"))
    # 가격이 기억나지 않아 0으로 기록해온 사용자 패턴이 있어 0은 유효값으로 허용한다(음수만 오류).
    if price_krw is None or price_krw < 0:
        return None, "invalid_price"

    product_key = (payload.get("product_key") or "").strip() or normalize_product_key(name_raw)
    category = (payload.get("category") or "").strip() or guess_category(name_raw)

    approx_fields = payload.get("approx_fields")

    ts = now_str()
    record = {
        "id": new_id(),
        "date": date,
        "store": payload.get("store") or "",
        "name_raw": name_raw,
        "product_key": product_key,
        "category": category,
        "volume_ml": _to_int_or_none(payload.get("volume_ml")),
        "promo": payload.get("promo") or "",
        "price_krw": price_krw,
        "fx_amount": _to_number_or_none(payload.get("fx_amount")),
        "fx_currency": (payload.get("fx_currency") or None),
        "fx_rate": _to_number_or_none(payload.get("fx_rate")),
        "note": payload.get("note") or "",
        "opened_date": payload.get("opened_date") or None,
        "emptied_date": payload.get("emptied_date") or None,
        "moved_date": payload.get("moved_date") or None,
        "cask": payload.get("cask") or None,
        "region": payload.get("region") or None,
        "classification1": payload.get("classification1") or None,
        "classification2": payload.get("classification2") or None,
        "approx_fields": approx_fields if isinstance(approx_fields, list) else [],
        "created_at": ts,
        "updated_at": ts,
    }

    records = load_purchases()
    records.append(record)
    save_purchases(records)
    return record, None


# PUT으로 부분 수정이 허용되는 필드들. id/created_at은 절대 덮어쓰지 않는다(설계 문서 5-4).
_EDITABLE_FIELDS_STR = (
    "date", "store", "name_raw", "product_key", "category", "promo", "note",
    "cask", "region", "classification1", "classification2",
)
_EDITABLE_FIELDS_INT = ("volume_ml",)
_EDITABLE_FIELDS_NUM = ("fx_amount", "fx_rate")


def update_purchase(purchase_id: str, payload: dict):
    """부분 수정. 반환: (record, error_reason)."""
    records = load_purchases()
    for rec in records:
        if rec.get("id") == purchase_id:
            for field in _EDITABLE_FIELDS_STR:
                if field in payload:
                    rec[field] = payload[field] if payload[field] is not None else ""
            for field in _EDITABLE_FIELDS_INT:
                if field in payload:
                    rec[field] = _to_int_or_none(payload[field])
            for field in _EDITABLE_FIELDS_NUM:
                if field in payload:
                    rec[field] = _to_number_or_none(payload[field])
            if "fx_currency" in payload:
                rec["fx_currency"] = payload["fx_currency"] or None
            if "price_krw" in payload:
                price_krw = _to_int_or_none(payload["price_krw"])
                # 0원은 유효값(가격 미상 기록 패턴)이라 허용하고, 음수만 무시한다.
                if price_krw is not None and price_krw >= 0:
                    rec["price_krw"] = price_krw
            if "opened_date" in payload:
                rec["opened_date"] = payload["opened_date"] or None
            if "emptied_date" in payload:
                rec["emptied_date"] = payload["emptied_date"] or None
            if "moved_date" in payload:
                rec["moved_date"] = payload["moved_date"] or None
            if "approx_fields" in payload:
                approx_fields = payload["approx_fields"]
                rec["approx_fields"] = approx_fields if isinstance(approx_fields, list) else []
            rec["updated_at"] = now_str()
            save_purchases(records)
            return rec, None
    return None, "not_found"


def delete_purchase(purchase_id: str) -> bool:
    records = load_purchases()
    new_records = [r for r in records if r.get("id") != purchase_id]
    if len(new_records) == len(records):
        return False
    save_purchases(new_records)
    return True


def merge_product_keys(from_keys: list, to_key: str) -> int:
    """3-4의 병합 기능. name_raw는 건드리지 않는다. 반환: 변경된 레코드 수."""
    records = load_purchases()
    from_set = set(from_keys)
    ts = now_str()
    updated = 0
    for rec in records:
        if rec.get("product_key") in from_set:
            rec["product_key"] = to_key
            rec["updated_at"] = ts
            updated += 1
    if updated:
        save_purchases(records)
    return updated


# ---------------------------------------------------------------------------
# CSV 가져오기 (설계 문서 4장)
# ---------------------------------------------------------------------------

_HEADER_ALIASES = {
    "date": ["일자", "날짜", "구매일", "구매일자", "date"],
    "store": ["구매장소", "장소", "구매처", "store"],
    "name_raw": ["술이름", "이름", "상품명", "제품명", "name"],
    "volume_ml": ["용량", "사이즈", "volume"],
    "promo": ["상품/행사", "상품행사", "행사", "프로모션", "promo"],
    "price_krw": ["가격", "금액", "원화", "price"],
    "fx_amount": ["외화", "외화금액", "fx"],
    "fx_rate": ["당시환율", "환율", "rate"],
    "note": ["비고", "메모", "note", "부가설명"],
    # 현재 시트에는 없는 컬럼이지만, 나중에 사용자가 시트에 추가할 수도 있어 미리 매핑해둔다.
    "opened_date": ["개봉일", "오픈일"],
    "emptied_date": ["비운날", "비운날짜", "완음일"],
    # 2026-09-18 시트 컬럼 확장(18개 컬럼)에 맞춘 매핑. "오픈기간"은 opened_date/emptied_date의
    # 파생값(차이일수)이라 의도적으로 매핑하지 않는다 - 화면에서 매번 계산한다(ADR-0004 3번 결정과 동일 원칙).
    "category": ["주종", "category"],
    "cask": ["캐스크", "cask"],
    "region": ["나라/지역", "지역", "나라", "region"],
    "classification1": ["분류1", "classification1"],
    "classification2": ["분류2", "classification2"],
    "moved_date": ["병옮긴날", "moved_date"],
}

_REQUIRED_FIELDS = ["date", "name_raw", "price_krw"]
_REQUIRED_DISPLAY = {"date": "일자", "name_raw": "술이름", "price_krw": "가격"}

_NUM_RE = re.compile(r"[0-9][0-9,.]*")


def _normalize_header(h: str) -> str:
    return (h or "").strip().lower().replace(" ", "")


def _build_header_map(header_row: list) -> dict:
    """헤더 행(list[str]) -> {internal_field: col_index}. 매칭 안 되는 컬럼은 조용히 무시한다."""
    alias_to_field = {}
    for field, aliases in _HEADER_ALIASES.items():
        for a in aliases:
            alias_to_field[_normalize_header(a)] = field

    col_map = {}
    for idx, h in enumerate(header_row):
        field = alias_to_field.get(_normalize_header(h))
        if field and field not in col_map:  # 먼저 매칭된 컬럼 우선
            col_map[field] = idx
    return col_map


def _cell(row: list, idx) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def parse_date(raw: str):
    """'24/4/6', '2024-04-06', '2024. 4. 6' 등을 YYYY-MM-DD로 변환. 실패 시 None."""
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"[./]", "-", s)
    parts = [p.strip() for p in s.split("-") if p.strip() != ""]
    if len(parts) != 3:
        return None
    y, m, d = parts
    try:
        y_i, m_i, d_i = int(y), int(m), int(d)
    except ValueError:
        return None
    if y_i < 100:
        y_i += 2000
    try:
        dt = datetime(y_i, m_i, d_i)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


def parse_price(raw: str):
    """'₩101,810' -> 101810. 실패/빈값이면 None."""
    s = re.sub(r"[^\d.-]", "", raw or "")
    if not s:
        return None
    try:
        return int(round(float(s)))
    except ValueError:
        return None


def parse_number(raw: str):
    s = re.sub(r"[^\d.-]", "", raw or "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_volume(raw: str):
    """실패/빈칸이면 None(오류 아님). 'ml'이 없는데 'l'/'L'/'리터'가 있으면 x1000."""
    s = (raw or "").strip()
    if not s:
        return None
    m = re.search(r"[\d.]+", s)
    if not m:
        return None
    try:
        num = float(m.group())
    except ValueError:
        return None
    lower = s.lower()
    if "ml" not in lower and ("l" in lower or "리터" in s):
        num *= 1000
    return int(round(num))


def parse_fx(raw: str):
    """'$262' / '262 USD' / 'NT$262' -> (금액, 기호원문). 실패 시 (None, None)."""
    s = (raw or "").strip()
    if not s:
        return None, None
    m = _NUM_RE.search(s)
    if not m:
        return None, None
    num_str = m.group().replace(",", "")
    try:
        amount = float(num_str)
    except ValueError:
        return None, None
    symbol = (s[:m.start()] + s[m.end():]).strip()
    return amount, (symbol or None)


def resolve_fx_currency(symbol, fx_rate):
    """환율 크기를 힌트로 통화를 추정한다(설계 문서 4-5). 확신 없으면 기호 원문을 그대로 쓴다."""
    if fx_rate is not None:
        if fx_rate >= 800:
            return "USD/EUR"
        if 30 <= fx_rate < 100:
            return "TWD/HKD"
    return symbol or None


def _decode_csv_bytes(file_bytes: bytes):
    """utf-8-sig 우선, 실패 시 cp949. 둘 다 실패하면 None."""
    for enc in ("utf-8-sig", "cp949"):
        try:
            return file_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def import_csv(file_bytes: bytes, dry_run: bool, mode: str):
    """CSV 가져오기. 반환: (http_status, response_dict).

    dry_run=True면 저장하지 않고 미리보기만 계산한다(설계 문서 4-7).
    """
    mode = mode if mode in ("append", "replace") else "append"

    text = _decode_csv_bytes(file_bytes)
    if text is None:
        return 400, {"status": "failed", "reason": "encoding_error"}

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return 400, {"status": "failed", "reason": "empty_file"}

    header_row = rows[0]
    col_map = _build_header_map(header_row)

    missing = [
        _REQUIRED_DISPLAY[f] for f in _REQUIRED_FIELDS if f not in col_map
    ]
    if missing:
        return 400, {"status": "failed", "reason": "missing_columns", "missing": missing}

    existing_records = load_purchases()

    errors = []
    warnings = []
    to_import = []
    # 자연키((date, name_raw, price_krw)) 기반 중복 스킵은 제거했다: 같은 날 같은 술을
    # 여러 병 사서 병마다 행을 따로 적는 것이 사용자의 정상적인 기록 패턴이라, dedupe가
    # 실제 구매 기록을 유실시켰다(사용자 피드백, 2026-09-18). 파싱에 성공한 행은 전부 저장한다.
    # skipped_duplicates 키는 프론트가 참조할 수 있어 응답에는 남기되 항상 0을 반환한다.
    skipped_duplicates = 0
    ts = now_str()

    for i, row in enumerate(rows[1:], start=1):
        row_number = i + 1  # 헤더가 1행이므로 데이터 첫 행은 2행

        date_raw = _cell(row, col_map.get("date"))
        name_raw = _cell(row, col_map.get("name_raw"))
        price_raw = _cell(row, col_map.get("price_krw"))

        # 일자·술이름·가격이 전부 비어 있는 행은 조용히 건너뛴다(시트 끝의 빈 줄).
        if not date_raw and not name_raw and not price_raw:
            continue

        date = parse_date(date_raw)
        if date is None:
            errors.append({
                "row": row_number, "column": "일자", "value": date_raw,
                "reason": "날짜 형식을 해석할 수 없음",
            })
            continue

        if not name_raw:
            errors.append({
                "row": row_number, "column": "술이름", "value": name_raw,
                "reason": "이름이 비어 있음",
            })
            continue

        price_krw = parse_price(price_raw)
        # 가격이 기억나지 않아 0으로 적어온 행이 있어 0은 허용하고, 음수/파싱 불가만 오류로 본다.
        if price_krw is None or price_krw < 0:
            errors.append({
                "row": row_number, "column": "가격", "value": price_raw,
                "reason": "가격이 비어 있거나 숫자가 아님",
            })
            continue

        store = _cell(row, col_map.get("store"))
        promo = _cell(row, col_map.get("promo"))
        note = _cell(row, col_map.get("note"))
        volume_ml = parse_volume(_cell(row, col_map.get("volume_ml")))
        opened_date = parse_date(_cell(row, col_map.get("opened_date")))
        emptied_date = parse_date(_cell(row, col_map.get("emptied_date")))
        moved_date = parse_date(_cell(row, col_map.get("moved_date")))

        # 주종은 CSV에 값이 있으면 그대로 쓰고, 비어 있을 때만 name_raw로 추정한다(2026-09-18 변경).
        category_raw = _cell(row, col_map.get("category"))
        category = category_raw or guess_category(name_raw)

        cask = _cell(row, col_map.get("cask")) or None
        region = _cell(row, col_map.get("region")) or None
        classification1 = _cell(row, col_map.get("classification1")) or None
        classification2 = _cell(row, col_map.get("classification2")) or None

        fx_amount, fx_symbol = parse_fx(_cell(row, col_map.get("fx_amount")))
        fx_rate = parse_number(_cell(row, col_map.get("fx_rate")))
        fx_currency = resolve_fx_currency(fx_symbol, fx_rate)

        if fx_amount is not None and fx_rate is not None and price_krw:
            estimated = fx_amount * fx_rate
            diff_ratio = abs(price_krw - estimated) / price_krw
            if diff_ratio > 0.15:
                warnings.append({
                    "row": row_number,
                    "reason": f"외화×환율({estimated:,.0f}원)과 가격({price_krw:,}원) 차이가 큼 — 통화 확인 필요",
                })

        record = {
            "id": new_id(),
            "date": date,
            "store": store,
            "name_raw": name_raw,
            "product_key": normalize_product_key(name_raw),
            "category": category,
            "volume_ml": volume_ml,
            "promo": promo,
            "price_krw": price_krw,
            "fx_amount": fx_amount,
            "fx_currency": fx_currency,
            "fx_rate": fx_rate,
            "note": note,
            "opened_date": opened_date,
            "emptied_date": emptied_date,
            "moved_date": moved_date,
            "cask": cask,
            "region": region,
            "classification1": classification1,
            "classification2": classification2,
            # CSV에서는 대략값 여부를 추정하지 않는다 - 사용자가 앱에서 직접 표시한다.
            "approx_fields": [],
            "created_at": ts,
            "updated_at": ts,
        }
        to_import.append(record)

    parsed = len(to_import)

    if dry_run:
        base_count = len(existing_records) if mode == "append" else 0
        imported = 0
        total_after = base_count + parsed
    else:
        if mode == "replace":
            backup_purchases()
            base_records = []
        else:
            base_records = existing_records
        final_records = base_records + to_import
        save_purchases(final_records)
        imported = parsed
        total_after = len(final_records)

    return 200, {
        "status": "success",
        "dry_run": dry_run,
        "mode": mode,
        "parsed": parsed,
        "imported": imported,
        "skipped_duplicates": skipped_duplicates,
        "total_after": total_after,
        "errors": errors,
        "warnings": warnings,
        "preview": to_import[:5],
    }
