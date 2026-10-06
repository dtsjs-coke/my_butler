"""
core/liquor_manager.py 수동 확인용 독립 스크립트 (pytest 아님, 직접 실행).
실제 data/liquor_purchases.json은 건드리지 않도록 임시 경로로 바꿔서 테스트한다.

실행: python test_liquor_manager.py
"""
import sys

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import os
import io
import tempfile

from core import liquor_manager as lm

FAILS = []


def check(label, condition):
    status = "OK" if condition else "FAIL"
    if not condition:
        FAILS.append(label)
    print(f"[{status}] {label}")


# --- product_key 정규화 -----------------------------------------------------
check(
    "normalize_product_key: 숙성연수는 보존",
    lm.normalize_product_key("발렌타인 17년") == "발렌타인 17년",
)
check(
    "normalize_product_key: 제품코드(105, CS)는 보존",
    lm.normalize_product_key("글렌파클라스105 CS") == "글렌파클라스105 CS",
)
check(
    "normalize_product_key: 잡음 토큰 제거 + 공백 정리",
    lm.normalize_product_key("카발란 솔리스트 비노바리끄 (정품) 면세") == "카발란 솔리스트 비노바리끄",
)
check(
    "normalize_product_key: 30년과 17년은 서로 다른 키 유지",
    lm.normalize_product_key("발렌타인 17년") != lm.normalize_product_key("발렌타인 30년 RESTAGE"),
)

# --- 날짜 파싱 ---------------------------------------------------------------
check("parse_date: 24/4/6", lm.parse_date("24/4/6") == "2024-04-06")
check("parse_date: 2024-04-06", lm.parse_date("2024-04-06") == "2024-04-06")
check("parse_date: 2024. 4. 6", lm.parse_date("2024. 4. 6") == "2024-04-06")
check("parse_date: 잘못된 날짜(24/13/1)는 None", lm.parse_date("24/13/1") is None)
check("parse_date: 빈 문자열은 None", lm.parse_date("") is None)

# --- 가격 / 용량 파싱 ---------------------------------------------------------
check("parse_price: ₩101,810 -> 101810", lm.parse_price("₩101,810") == 101810)
check("parse_price: 빈 값은 None", lm.parse_price("") is None)
check("parse_volume: 700 ml -> 700", lm.parse_volume("700 ml") == 700)
check("parse_volume: 0.7L -> 700", lm.parse_volume("0.7L") == 700)
check("parse_volume: 빈 값은 None(오류 아님)", lm.parse_volume("") is None)

# --- 외화 / 통화 추정 (설계 문서 0-3, 4-5의 대만달러 사례) ---------------------
fx_amount, fx_symbol = lm.parse_fx("$262")
check("parse_fx: $262 -> amount 262", fx_amount == 262)
currency = lm.resolve_fx_currency(fx_symbol, 42.5)
check("resolve_fx_currency: fx_rate=42.5 -> TWD/HKD 후보", currency == "TWD/HKD")
currency_usd = lm.resolve_fx_currency("USD", 1390.5)
check("resolve_fx_currency: fx_rate=1390.5 -> USD/EUR 후보", currency_usd == "USD/EUR")
currency_literal = lm.resolve_fx_currency("JPY", 9.1)
check("resolve_fx_currency: 애매한 범위 밖이면 기호 원문 유지", currency_literal == "JPY")

# --- 주종 추정 ---------------------------------------------------------------
check("guess_category: 발렌타인 -> 위스키", lm.guess_category("발렌타인 17년") == "위스키")
check("guess_category: 모르는 이름 -> 미분류", lm.guess_category("이상한술") == "미분류")

# --- CRUD + CSV 가져오기 (임시 파일로 격리) ----------------------------------
with tempfile.TemporaryDirectory() as tmp_dir:
    lm.LIQUOR_FILE = os.path.join(tmp_dir, "liquor_purchases.json")
    lm.BACKUP_FILE = os.path.join(tmp_dir, "liquor_purchases.bak.json")

    check("load_purchases: 파일 없으면 빈 리스트", lm.load_purchases() == [])

    record, reason = lm.create_purchase({
        "date": "2026-09-17",
        "store": "신라인터넷면세점",
        "name_raw": "글렌파클라스105 CS",
        "price_krw": 98000,
    })
    check("create_purchase: 성공", reason is None and record is not None)
    check("create_purchase: product_key 자동 채움", record["product_key"] == "글렌파클라스105 CS")
    check("create_purchase: category 자동 추정", record["category"] == "위스키")

    bad_record, bad_reason = lm.create_purchase({"date": "이상함", "name_raw": "x", "price_krw": 1})
    check("create_purchase: 잘못된 날짜는 invalid_date", bad_reason == "invalid_date")

    # 가격이 기억나지 않아 0으로 기록하는 패턴이 있어 0은 허용, 음수만 오류 (2026-09-18 변경)
    zero_price_record, zero_price_reason = lm.create_purchase({
        "date": "2026-09-18", "name_raw": "가격모름술", "price_krw": 0,
    })
    check("create_purchase: price_krw=0은 허용", zero_price_reason is None and zero_price_record["price_krw"] == 0)
    _, neg_price_reason = lm.create_purchase({"date": "2026-09-18", "name_raw": "x", "price_krw": -100})
    check("create_purchase: price_krw 음수는 invalid_price", neg_price_reason == "invalid_price")
    lm.delete_purchase(zero_price_record["id"])

    updated, u_reason = lm.update_purchase(record["id"], {"product_key": "발렌타인 17년"})
    check("update_purchase: 부분 수정 성공", u_reason is None and updated["product_key"] == "발렌타인 17년")
    check("update_purchase: name_raw는 그대로 유지(보내지 않은 필드)", updated["name_raw"] == "글렌파클라스105 CS")

    _, nf_reason = lm.update_purchase("존재하지않는id", {"note": "x"})
    check("update_purchase: 없는 id는 not_found", nf_reason == "not_found")

    # --- 신규 필드: opened_date / emptied_date / approx_fields (2026-09-18 추가) ---
    check("create_purchase: opened_date/emptied_date 기본값 null", record["opened_date"] is None and record["emptied_date"] is None)
    check("create_purchase: approx_fields 기본값 []", record["approx_fields"] == [])

    opened_record, _ = lm.create_purchase({
        "date": "2026-01-01", "name_raw": "개봉일테스트술", "price_krw": 50000,
        "opened_date": "2026-01-05", "emptied_date": "2026-03-01",
        "approx_fields": ["price_krw", "opened_date"],
    })
    check("create_purchase: opened_date/emptied_date 저장", opened_record["opened_date"] == "2026-01-05" and opened_record["emptied_date"] == "2026-03-01")
    check("create_purchase: approx_fields 저장(화이트리스트 검증 없음)", opened_record["approx_fields"] == ["price_krw", "opened_date"])

    updated2, _ = lm.update_purchase(opened_record["id"], {"emptied_date": "2026-04-01", "approx_fields": []})
    check("update_purchase: opened_date/emptied_date 부분 수정", updated2["emptied_date"] == "2026-04-01" and updated2["opened_date"] == "2026-01-05")
    check("update_purchase: approx_fields 비우기", updated2["approx_fields"] == [])

    lm.delete_purchase(opened_record["id"])

    # --- 신규 필드: cask / region / classification1 / classification2 / moved_date (2026-09-18 (4) 추가) ---
    check(
        "create_purchase: cask/region/classification1/classification2/moved_date 기본값 null",
        record["cask"] is None and record["region"] is None
        and record["classification1"] is None and record["classification2"] is None
        and record["moved_date"] is None,
    )

    cask_record, _ = lm.create_purchase({
        "date": "2026-02-01", "name_raw": "확장필드테스트술", "price_krw": 80000,
        "cask": "쉐리", "region": "영국 스코틀랜드",
        "classification1": "싱글몰트", "classification2": "캐스크스트렝스",
        "moved_date": "2026-02-10",
    })
    check(
        "create_purchase: cask/region/classification1/classification2/moved_date 저장",
        cask_record["cask"] == "쉐리" and cask_record["region"] == "영국 스코틀랜드"
        and cask_record["classification1"] == "싱글몰트" and cask_record["classification2"] == "캐스크스트렝스"
        and cask_record["moved_date"] == "2026-02-10",
    )

    updated3, _ = lm.update_purchase(cask_record["id"], {"cask": "버번", "moved_date": "2026-02-15"})
    check("update_purchase: cask/moved_date 부분 수정", updated3["cask"] == "버번" and updated3["moved_date"] == "2026-02-15")
    check("update_purchase: 보내지 않은 region은 그대로 유지", updated3["region"] == "영국 스코틀랜드")

    lm.delete_purchase(cask_record["id"])

    csv_text = (
        "일자,구매장소,술이름,용량,상품/행사,가격,외화,당시환율,비고,개봉일,비운날\n"
        "24/4/6,동탄 홈플러스,발렌타인 17년,700ml,,101810,,,,2024-04-10,2024-05-01\n"
        "2025-02-14,대만 가퉁완주,카발란 술리스트 비노바리끄,750ml,,11135,$262,42.5,,,\n"
        "2026-02-01,어딘가,가격0술,700ml,,0,,,,,\n"
        ",,,,,,,,,,\n"
        "24/13/1,어딘가,이상한날짜술,700ml,,50000,,,,,\n"
        "2025-01-01,,,700ml,,,,,,,\n"
        "2026-01-01,어딘가,가격없음술,700ml,,,,,,,\n"
    ).encode("utf-8-sig")

    status, body = lm.import_csv(csv_text, dry_run=True, mode="append")
    check("import_csv(dry_run): HTTP 200", status == 200)
    check("import_csv(dry_run): 정상 3건 파싱(가격 0원 포함)", body["parsed"] == 3)
    check("import_csv(dry_run): 저장 안 함(imported=0)", body["imported"] == 0)
    check("import_csv(dry_run): 오류 3건(날짜/이름빈값/가격)", len(body["errors"]) == 3)
    check("import_csv(dry_run): dry_run 중 파일 미변경", len(lm.load_purchases()) == 1)
    check(
        "import_csv(dry_run): 대만달러 행 fx_currency=TWD/HKD",
        any(r.get("fx_currency") == "TWD/HKD" for r in body["preview"]),
    )
    check(
        "import_csv(dry_run): opened_date/emptied_date 파싱",
        any(r.get("opened_date") == "2024-04-10" and r.get("emptied_date") == "2024-05-01" for r in body["preview"]),
    )
    check(
        "import_csv(dry_run): approx_fields는 CSV에서 항상 []",
        all(r.get("approx_fields") == [] for r in body["preview"]),
    )

    status2, body2 = lm.import_csv(csv_text, dry_run=False, mode="append")
    check("import_csv(실행): imported=3", body2["imported"] == 3)
    check("import_csv(실행): total_after=4 (기존1+신규3)", body2["total_after"] == 4)
    check("import_csv(실행 후): 실제 저장 4건", len(lm.load_purchases()) == 4)

    # 자연키 dedupe를 제거했으므로(2026-09-18) 같은 CSV를 다시 올려도 전부 다시 저장된다.
    status3, body3 = lm.import_csv(csv_text, dry_run=False, mode="append")
    check(
        "import_csv(재실행): 중복 스킵 없이 다시 저장됨(skipped_duplicates는 항상 0)",
        body3["skipped_duplicates"] == 0 and body3["imported"] == 3,
    )
    check("import_csv(재실행 후): 실제 저장 7건(4+3)", len(lm.load_purchases()) == 7)

    bad_header_csv = "이상한헤더1,이상한헤더2\nA,B\n".encode("utf-8-sig")
    status4, body4 = lm.import_csv(bad_header_csv, dry_run=True, mode="append")
    check("import_csv: 필수 헤더 누락시 400", status4 == 400 and body4["reason"] == "missing_columns")

    # --- 2026-09-18 (4): 구글 시트 컬럼 확장(18개 컬럼) 반영 확인 ---
    expanded_header = (
        "일자,구매장소,술이름,용량,상품/행사,가격,외화,당시환율,부가설명,"
        "주종,캐스크,나라/지역,분류1,분류2,개봉일,병옮긴날,비운날,오픈기간"
    )
    row_explicit_category = ",".join([
        "2024. 4. 6", "동탄 홈플러스", "글렌알라키 15년", "700ml", "", "101810", "", "",
        "좋았음", "위스키(오크통구분)", "쉐리", "영국 스코틀랜드", "싱글몰트", "",
        "2024. 4. 10", "2024. 4. 12", "2024. 5. 1", "25일",
    ])
    row_fallback_category = ",".join([
        "2025-01-10", "대만 가퉁완주", "카발란 솔리스트 비노바리끄", "750ml", "", "60000", "", "",
        "", "", "", "", "", "", "", "", "", "-",
    ])
    expanded_csv = (expanded_header + "\n" + row_explicit_category + "\n" + row_fallback_category + "\n").encode("utf-8-sig")

    status5, body5 = lm.import_csv(expanded_csv, dry_run=True, mode="append")
    check("import_csv(확장 헤더): HTTP 200(18개 컬럼도 정상 인식)", status5 == 200)
    check("import_csv(확장 헤더): 2건 파싱", body5["parsed"] == 2)
    preview_by_name = {r["name_raw"]: r for r in body5["preview"]}

    explicit_rec = preview_by_name["글렌알라키 15년"]
    check("import_csv: 주종 컬럼 값이 있으면 그대로 사용(guess_category로 덮어쓰지 않음)", explicit_rec["category"] == "위스키(오크통구분)")
    check("import_csv: note는 '부가설명' 헤더에서도 채워짐", explicit_rec["note"] == "좋았음")
    check("import_csv: cask 파싱", explicit_rec["cask"] == "쉐리")
    check("import_csv: region 파싱", explicit_rec["region"] == "영국 스코틀랜드")
    check("import_csv: classification1 파싱", explicit_rec["classification1"] == "싱글몰트")
    check("import_csv: classification2는 빈 값이면 None", explicit_rec["classification2"] is None)
    check("import_csv: opened_date 파싱(확장 헤더)", explicit_rec["opened_date"] == "2024-04-10")
    check("import_csv: moved_date 파싱", explicit_rec["moved_date"] == "2024-04-12")
    check("import_csv: emptied_date 파싱(확장 헤더)", explicit_rec["emptied_date"] == "2024-05-01")
    check(
        "import_csv: '오픈기간' 컬럼은 매핑되지 않아 레코드에 남지 않음",
        "오픈기간" not in explicit_rec and "open_duration" not in explicit_rec,
    )

    fallback_rec = preview_by_name["카발란 솔리스트 비노바리끄"]
    check("import_csv: 주종이 비어 있으면 guess_category()로 폴백", fallback_rec["category"] == "위스키")
    check("import_csv: 신규 필드가 전부 빈 값이면 None", fallback_rec["cask"] is None and fallback_rec["region"] is None)

    # 수동 생성 레코드(product_key를 "발렌타인 17년"으로 수정해둠) + CSV를 두 번 올려서
    # 생긴 "발렌타인 17년" 행 2개(dedupe가 없으므로 둘 다 저장됨) = 총 3건이 병합 대상이다.
    merged = lm.merge_product_keys(["발렌타인 17년"], "발렌타인 17년(표준)")
    check("merge_product_keys: 3건 병합", merged == 3)

    # --- 병합 제안 영구 제외 (2026-09-18 추가) ---
    check("load_dismissed_pairs: 초기값은 빈 리스트", lm.load_dismissed_pairs() == [])

    lm.dismiss_merge_suggestion("나가", "가나")
    check(
        "dismiss_merge_suggestion: 정렬된 쌍으로 저장",
        lm.load_dismissed_pairs() == [["가나", "나가"]],
    )

    records_before_dismiss = lm.load_purchases()
    lm.dismiss_merge_suggestion("가나", "나가")  # 이미 등록된 쌍, 순서만 다름
    check(
        "dismiss_merge_suggestion: 순서 바꿔 다시 호출해도 중복 추가 안 됨",
        lm.load_dismissed_pairs() == [["가나", "나가"]],
    )
    check(
        "dismiss_merge_suggestion: 중복이라 실제로는 다시 저장하지 않음(레코드 그대로)",
        lm.load_purchases() == records_before_dismiss,
    )

    lm.dismiss_merge_suggestion("다라", "라다")
    check(
        "dismiss_merge_suggestion: 서로 다른 쌍은 둘 다 누적",
        lm.load_dismissed_pairs() == [["가나", "나가"], ["다라", "라다"]],
    )

    lm.dismiss_merge_suggestion("자기자신", "자기자신")
    check(
        "dismiss_merge_suggestion: key_a==key_b는 무시",
        lm.load_dismissed_pairs() == [["가나", "나가"], ["다라", "라다"]],
    )

    # save_purchases()가 dismissed_merge_pairs를 잃어버리지 않는지(create_purchase 등 다른
    # 저장 경로가 이 필드를 몰라도 보존돼야 한다) 회귀 확인.
    guard_record, _ = lm.create_purchase({"date": "2026-09-18", "name_raw": "보존확인술", "price_krw": 1000})
    check(
        "save_purchases: 다른 저장(create_purchase) 이후에도 dismissed_merge_pairs 보존",
        lm.load_dismissed_pairs() == [["가나", "나가"], ["다라", "라다"]],
    )
    lm.delete_purchase(guard_record["id"])

    ok = lm.delete_purchase(record["id"])
    check("delete_purchase: 성공", ok is True)
    ok2 = lm.delete_purchase(record["id"])
    check("delete_purchase: 이미 없으면 False", ok2 is False)

print()
if FAILS:
    print(f"{len(FAILS)}개 실패:")
    for f in FAILS:
        print(f" - {f}")
    raise SystemExit(1)
else:
    print("전부 통과.")
