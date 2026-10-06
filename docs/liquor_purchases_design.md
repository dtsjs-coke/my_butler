# 주류 구매 이력 대시보드 — 설계 문서

- **작성일**: 2026-09-18
- **작성**: senior-dev (멀티에이전트 오케스트레이터)
- **상태**: 설계 확정 (구현 대기)
- **대상 프로젝트**: `C:\Users\user\butler_pjt\dev_pjt\my_butler`
- **관련 ADR**: [ADR-0004](adr/0004-liquor-purchase-tracker.md)

이 문서는 junior-dev(백엔드)와 ui-dev(프론트엔드)가 **서로를 기다리지 않고 병렬로** 작업할 수 있도록 API 계약과 데이터 스키마를 확정한 것입니다. 이 문서에 적힌 필드명·엔드포인트·응답 형태는 양쪽이 지켜야 하는 약속이며, 임의로 바꾸면 반대쪽이 깨집니다. 바꿔야 할 이유가 생기면 먼저 오케스트레이터에게 알리십시오.

---

## 변경 이력

### 2026-09-18 — 사용자 피드백에 따른 스키마/로직 변경

- **CSV 가져오기 자연키 중복 스킵 제거**: `(date, name_raw, price_krw)`가 같으면 건너뛰던 로직을 삭제했다. 같은 날 같은 술을 여러 병 사서 병마다(개봉일·비운 날짜가 달라서) 행을 따로 기록하는 것이 사용자의 정상적인 사용 패턴이었고, 자연키 dedupe가 이 정상 구매 기록을 조용히 유실시켰다. `skipped_duplicates` 응답 필드는 프론트 호환을 위해 남겨두되 항상 `0`을 반환한다.
- **`price_krw` 0 허용**: 가격이 기억나지 않을 때 사용자가 `0`으로 기록해온 패턴이 있어, 검증 기준을 "0 이하면 오류"에서 "음수만 오류"로 완화했다.
- **`opened_date`/`emptied_date`/`approx_fields` 필드 추가**: 개봉일·완음일을 관리하고, 여러 필드가 "기억이 안 나서 대략 적은 값"임을 표시할 수 있도록 추가했다. `approx_fields`는 화이트리스트 검증 없는 자유 문자열 배열이다(추후 필드가 늘어나도 유연하게 대응하기 위함).

### 2026-09-18 (2) — 병합 제안 영구 제외 기능 추가

"술 이름 정리" 패널에서 병합 안 해도 되는 후보 쌍이 방문할 때마다 반복해서 뜬다는 피드백에 따라, "병합 안 함"으로 처리한 `product_key` 쌍을 영구히 기억하는 `dismissed_merge_pairs`를 추가했다(2-1, 3-4, 5-2, 5-6-1 참고). 새 파일을 만들지 않고 기존 `data/liquor_purchases.json`의 최상위 구조에 얹었다 — `sync_s9.py` exclude_list에 파일을 또 등록해야 하는 실수 위험을 없애기 위함이다. 작은 UX 개선이고 되돌리기 쉬워 ADR은 작성하지 않았다.

### 2026-09-18 (3) — 쓰기 작업에 VWAP admin 인증 재사용

조회는 그대로 열어두되, 추가/수정/삭제/병합/CSV 가져오기/병합 제안 제외 같은 쓰기 작업에는 `api/vwap_api.py`의 기존 `admin_required` 데코레이터(`vwap_session` 쿠키, `POST /vwap/login`)를 그대로 재사용해 보호를 추가했다(5-0 참고). 새 비밀번호 저장소나 로그인 엔드포인트를 만들지 않았다. 기존 체계를 그대로 가져다 쓰는 것이라 ADR은 작성하지 않았다.

### 2026-09-18 (4) — 구글 시트 컬럼 확장(18개 컬럼) 반영

사용자가 시트에 `주종`, `캐스크`, `나라/지역`, `분류1`, `분류2`, `병옮긴날`, `오픈기간` 컬럼을 추가했다. 이에 맞춰:
- `category`: CSV에 `주종` 값이 있으면 그대로 쓰고, 비어 있을 때만 `guess_category()`로 추정하도록 우선순위를 바꿨다(4-6 참고). `create_purchase`는 원래부터 이 우선순위였다.
- `cask`/`region`/`classification1`/`classification2` 4개 필드를 신규 추가했다. 전부 화이트리스트 검증 없는 자유 문자열이다(2-2 참고) — 값 종류를 실제로 확인한 결과 짧은 단어 나열 수준이라, `category`처럼 정해진 값 집합으로 강제할 근거가 없었다.
- `moved_date`(병 옮긴 날짜) 필드를 `opened_date`/`emptied_date`와 동일한 방식(자유 날짜, 기존 `parse_date()` 재사용)으로 추가했다.
- `오픈기간`은 의도적으로 매핑하지 않는다(4-2 참고) — `opened_date`/`emptied_date`의 차이를 매번 계산한 파생값이라 저장 대상이 아니다(ADR-0004 3번 결정과 동일 원칙).
- `부가설명`(새 시트) / `비고`(옛 시트) 둘 다 `note`로 매핑되도록 별칭을 추가했다(4-2 참고).

---

## 0. 먼저 읽어야 할 경고 (구현 전 필독)

### 0-1. 치명적: `sync_s9.py` 제외 목록에 넣지 않으면 데이터가 삭제된다

`dev_pjt/sync_manager/sync_s9.py`의 `cleanup_remote_orphans()`는 **"로컬에 없고 제외 목록에도 없는 원격 파일"을 S9에서 삭제**합니다. 그리고 `my_butler/.gitignore` 첫 줄은 `*.json`이라 `data/liquor_purchases.json`은 **git에 절대 올라가지 않습니다.**

따라서 아무 조치 없이 이 기능을 배포하면 다음이 벌어집니다.

```
S9 대시보드에서 CSV 122행 업로드 -> S9의 data/liquor_purchases.json 생성됨
    ...나중에 아무 이유로든 sync_s9.py 실행...
로컬 Windows에는 그 파일이 없음 + 제외 목록에도 없음
    -> cleanup_remote_orphans()가 원격 파일을 orphan으로 판정
    -> 122행 전부 삭제. 백업 없음. 구글 시트 다시 받아 재업로드해야 함.
```

**조치(필수, 구현 1순위)**: `dev_pjt/sync_manager/sync_s9.py`의 `exclude_list`에 `data/settlements.json` 바로 옆에 아래 한 줄을 추가합니다.

```python
"data/liquor_purchases.json",
```

`settlements.json`, `battery_guard.json` 등이 이미 같은 이유로 그 목록에 들어 있습니다. 즉 이건 예외 처리가 아니라 **이 프로젝트에서 "런타임에 쌓이는 데이터 파일"이 따르는 표준 절차**입니다.

### 0-2. CSV 122행 가져오기는 반드시 S9(운영)에서 해야 한다

0-1의 결과로 이 데이터 파일은 git으로도 sync로도 전달되지 않습니다. 즉 **로컬 Windows에서 CSV를 업로드하면 그 데이터는 S9에 영원히 도달하지 않습니다.**

- 로컬에서의 업로드는 **기능 테스트 용도로만** 사용합니다.
- 실제 122행 이관은 코드 배포(`sync_s9.py`) 후, **S9에서 돌고 있는 대시보드(Cloudflare 터널 URL)에 접속해서** 수행해야 합니다.
- 이 내용은 사용자에게 보고할 때 "사용자가 직접 해야 할 일"로 분리해서 안내해야 합니다.

### 0-3. 브리핑에 있던 사실 오류 두 가지

| 브리핑 내용 | 실제 확인 결과 |
|---|---|
| "`vwap_dashboard.html`에서 이미 Chart.js를 쓰고 있으니 재사용" | **Chart.js는 이 프로젝트 어디에도 없습니다.** `vwap_dashboard.html`이 쓰는 건 TradingView 위젯(`https://s3.tradingview.com/tv.js`)이고, 이건 증권 차트 임베드 위젯이라 임의의 JSON으로 라인/도넛 차트를 그리는 용도로 쓸 수 없습니다. 또 이 템플릿은 `base.html`을 상속하지 않는 독립 페이지라 참고할 공통 패턴도 없습니다. |
| "`data/*.json` flat 파일 패턴" | 맞습니다. 다만 위 0-1의 함의(gitignore + sync 제외)가 브리핑에 빠져 있었습니다. |

Chart.js는 **새로 도입하는 외부 라이브러리**입니다. 도입 판단 근거는 ADR-0004에 기록했습니다. CDN은 기존 Tailwind/lucide와 동일한 방식으로 `<script src>` 한 줄입니다.

---

## 1. 전체 구조

이 프로젝트의 기존 계층 규칙(`core/`에 로직, `api/`에 라우트)을 그대로 따릅니다. `core/news_service.py`의 `load_news()`, `config/config_manager.py`의 `load_keywords()/save_keywords()`가 같은 패턴입니다.

| 파일 | 역할 | 담당 |
|---|---|---|
| `core/liquor_manager.py` (신규) | 데이터 로드/저장, ID 생성, CSV 파싱, 정규화 — **순수 로직** | junior-dev |
| `api/flask_app.py` (수정) | 라우트만. 로직은 전부 `liquor_manager`에 위임 | junior-dev |
| `api/templates/liquor.html` (신규) | 화면 + 차트 + CSV 업로드 UI | ui-dev |
| `api/templates/base.html` (수정) | 사이드바에 메뉴 1개 추가 | ui-dev |
| `dev_pjt/sync_manager/sync_s9.py` (수정) | 제외 목록 1줄 추가 (0-1 참고) | junior-dev |
| `data/liquor_purchases.json` | 데이터 (코드가 없으면 자동 생성) | — |

**CSV 파싱을 라우트 함수 안에 넣지 마십시오.** 파싱 규칙(4장)은 이 기능에서 가장 틀리기 쉬운 부분이라, 라우트와 분리해서 함수 단위로 따로 확인할 수 있어야 합니다.

---

## 2. 데이터 스키마 — `data/liquor_purchases.json`

### 2-1. 파일 형태

기존 `settlements.json`의 `{"settlements": [...]}` 래핑 방식을 그대로 따릅니다.

```json
{
  "version": 1,
  "liquor_purchases": [
    {
      "id": "a3f19c04b7d2",
      "date": "2024-04-06",
      "store": "동탄 홈플러스",
      "name_raw": "발렌타인 17년",
      "product_key": "발렌타인 17년",
      "category": "위스키",
      "volume_ml": 700,
      "promo": "",
      "price_krw": 101810,
      "fx_amount": null,
      "fx_currency": null,
      "fx_rate": null,
      "note": "",
      "created_at": "2026-09-18 01:20:33",
      "updated_at": "2026-09-18 01:20:33"
    },
    {
      "id": "7b21e8d40a6f",
      "date": "2025-02-14",
      "store": "대만 가퉁완주",
      "name_raw": "카발란 술리스트 비노바리끄",
      "product_key": "카발란 솔리스트 비노바리끄",
      "category": "위스키",
      "volume_ml": 750,
      "promo": "",
      "price_krw": 11135,
      "fx_amount": 262,
      "fx_currency": "TWD",
      "fx_rate": 42.5,
      "note": "",
      "created_at": "2026-09-18 01:20:33",
      "updated_at": "2026-09-18 01:20:33"
    }
  ],
  "dismissed_merge_pairs": [
    ["카발란 술리스트 비노바리끄", "카발란 솔리스트 비노바리끄"]
  ]
}
```

`version`은 나중에 필드가 바뀌었을 때 옛 파일을 구분하기 위한 것입니다. 지금은 항상 `1`을 쓰고, 읽을 때 없으면 `1`로 간주합니다.

`dismissed_merge_pairs`(2026-09-18 추가)는 "병합 제안"에서 사용자가 "병합 안 함"을 누른 `product_key` 쌍을 영구히 기억해두는 배열입니다. 새 파일을 따로 만들지 않고 이 최상위 구조에 얹었습니다(5-6-1 참고). 옛 파일에는 이 키가 없을 수 있으므로 읽을 때 없으면 빈 배열로 간주합니다.

### 2-2. 필드 정의

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `id` | string | O | 레코드 고유 ID. 생성 방식은 2-3 참고. 한번 정해지면 절대 안 바뀜 |
| `date` | string | O | 구매일. **항상 `YYYY-MM-DD`**. 시트의 `24/4/6` 같은 형식은 저장 전에 변환 |
| `store` | string | O | 구매장소 원문 그대로 (`""` 허용) |
| `name_raw` | string | O | **시트에 적힌 술이름 원문. 절대 가공하지 않음** |
| `product_key` | string | O | 그룹핑용 표준 술이름. 편집 가능. 3장 참고 |
| `category` | string | O | 주종. `위스키` / `와인` / `사케` / `브랜디` / `기타` / `미분류`. CSV에 `주종` 컬럼 값이 있으면 그대로 쓰고, 비어 있을 때만 `guess_category()`로 추정(2026-09-18 변경) |
| `volume_ml` | number \| null | X | 용량(ml, 정수). 파싱 실패/빈칸이면 `null` |
| `promo` | string | O | 상품/행사 메모 (`""` 허용) |
| `price_krw` | number | O | 원화 결제액(정수). **모든 지출 차트의 유일한 기준값**. `0` 허용(가격 미상 기록), 음수만 오류 (2026-09-18 변경, 하단 변경 이력 참고) |
| `fx_amount` | number \| null | X | 외화 금액 |
| `fx_currency` | string \| null | X | 통화 표시용 라벨. 자유 문자열, 편집 가능 (4-5 참고) |
| `fx_rate` | number \| null | X | 당시 환율 |
| `note` | string | O | 비고 (`""` 허용) |
| `opened_date` | string \| null | X | 개봉일. `YYYY-MM-DD` 또는 `null` (2026-09-18 추가) |
| `emptied_date` | string \| null | X | 다 비운(완음) 날짜. `YYYY-MM-DD` 또는 `null` (2026-09-18 추가) |
| `moved_date` | string \| null | X | 병을 옮긴 날짜. `YYYY-MM-DD` 또는 `null` (2026-09-18 추가, CSV `병옮긴날`) |
| `cask` | string \| null | X | 캐스크 표기(예: "라이", "버번", "쉐리"). 자유 문자열, 화이트리스트 검증 없음 (2026-09-18 추가, CSV `캐스크`) |
| `region` | string \| null | X | 나라/지역(예: "대만", "영국 스코틀랜드"). 자유 문자열 (2026-09-18 추가, CSV `나라/지역`) |
| `classification1` | string \| null | X | 분류1(예: "싱글몰트", "포트와인"). 자유 문자열 (2026-09-18 추가, CSV `분류1`) |
| `classification2` | string \| null | X | 분류2. 현재 시트에는 값이 없음. 자유 문자열 (2026-09-18 추가, CSV `분류2`) |
| `approx_fields` | string[] | O | "대략적으로 기록한" 필드명 목록. 기본값 `[]`. 화이트리스트 검증 없이 자유 문자열 배열로 저장(2026-09-18 추가) |
| `created_at` | string | O | `YYYY-MM-DD HH:MM:SS` (기존 `settlements.json`과 동일 포맷) |
| `updated_at` | string | O | 동일 포맷. 수정 시 갱신 |

**규칙**:
- "필수"인 문자열 필드는 값이 없어도 키를 빼지 말고 `""`를 넣습니다. 프론트에서 `undefined` 방어 코드를 매번 쓰지 않게 하기 위함입니다.
- 숫자 필드는 "값 없음"을 `0`이 아니라 `null`로 표현합니다. `volume_ml: 0`과 `volume_ml: null`은 의미가 다릅니다(0이면 100ml당 단가 계산 시 0으로 나누게 됨).
- **저장하지 않는 값**: `price_per_100ml`(= `price_krw / volume_ml * 100`), 연/월 같은 파생값은 저장하지 않고 화면에서 계산합니다. 저장하면 원본이 수정됐을 때 어긋납니다.

### 2-3. ID 생성 — `uuid4` 사용 (기존 방식과 다름, 의도된 것)

기존 `manage_settlements()`는 `str(int(time.time() * 1000))`을 씁니다. **이 방식을 CSV 일괄 가져오기에 쓰면 안 됩니다.** Windows의 `time.time()` 해상도는 약 15.6ms라, 122행을 루프로 돌면 **같은 ID가 수십 개 생깁니다.** ID가 겹치면 "1개 수정했는데 3개가 같이 바뀌는" 버그가 되고, 사용자가 원인을 알기 어렵습니다.

```python
import uuid

def new_id() -> str:
    """레코드 ID 생성. 시간 기반이 아니라 난수 기반이라 루프에서도 절대 겹치지 않는다."""
    return uuid.uuid4().hex[:12]
```

기존 패턴과의 일관성보다 정확성을 택했습니다(ADR-0004). ID는 화면에 노출되지 않는 내부 식별자라 형식이 달라도 사용자 경험에는 영향이 없습니다.

### 2-4. 저장 규칙 — 원자적 쓰기 + 락 (필수)

122행은 손으로 수년간 쌓은 데이터이고 **git 백업이 없습니다**(0-1). `settlements.json`처럼 `open(w)`로 바로 덮어쓰면 쓰는 도중 프로세스가 죽었을 때 파일이 깨지고 복구 수단이 없습니다. 또 Flask가 `threaded=True`로 돌기 때문에 동시 요청이 서로의 쓰기를 덮어쓸 수 있습니다.

```python
import json, os, threading

_LOCK = threading.Lock()

def save_purchases(records: list) -> None:
    """임시 파일에 다 쓴 뒤 교체한다. 쓰다가 죽어도 기존 파일은 멀쩡하다."""
    payload = {"version": 1, "liquor_purchases": records}
    tmp_path = LIQUOR_FILE + ".tmp"
    with _LOCK:
        os.makedirs(os.path.dirname(LIQUOR_FILE), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, LIQUOR_FILE)   # 원자적 교체
```

`load_purchases()`는 파일이 없거나 비었거나 깨졌으면 **빈 리스트를 반환**하고 예외를 올리지 않습니다(`cleanup_old_settlements()`와 동일한 방어 방식). 즉 최초 배포 시 파일이 없어도 페이지가 정상적으로 뜹니다.

추가로 `replace` 모드 CSV 가져오기(4-7) 직전에는 기존 파일을 `data/liquor_purchases.bak.json`으로 1회 복사합니다.

---

## 3. "같은 술" 그룹핑 — `product_key` 방식

### 3-1. 문제

시트의 술이름은 표기가 흔들립니다(`카발란 술리스트` vs `카발란 솔리스트`, 띄어쓰기 차이 등). 그런데 **비슷해 보이지만 반드시 구분해야 하는 것도 섞여 있습니다**: `발렌타인 17년`과 `발렌타인 30년 RESTAGE`는 문자열 유사도가 높지만 가격대가 10배 넘게 차이 나는 완전히 다른 상품입니다.

### 3-2. 결정

**각 레코드에 편집 가능한 `product_key` 필드를 두고, 가져오기 시점에 자동으로 초기값을 채운 뒤, 사용자가 화면에서 고칠 수 있게 합니다. 자동 유사도 병합은 하지 않고, "제안"까지만 합니다.**

핵심은 세 가지입니다.

1. **한 번 계산해서 저장한다** — 읽을 때마다 정규화 함수를 다시 돌리지 않습니다. 그러면 나중에 정규화 로직을 건드리는 순간 과거 그룹핑이 조용히 바뀌고, 사용자가 손으로 고쳐둔 내용이 날아갑니다.
2. **원문은 절대 잃지 않는다** — `name_raw`를 항상 그대로 보존합니다. 그룹핑을 통째로 다시 하고 싶어져도 언제든 재계산할 수 있습니다.
3. **자동 정규화는 보수적으로, 합치기보다 나누는 쪽으로** — 잘못 나뉜 건 사용자가 병합 버튼 한 번으로 고칠 수 있지만, 잘못 합쳐진 건 **어떤 행들이 합쳐졌는지 사용자가 기억해내야** 풀 수 있습니다. 두 실패의 비용이 다르므로 안전한 쪽으로 기웁니다.

### 3-3. 자동 초기값 규칙 — `normalize_product_key()`

**하는 것**: 앞뒤 공백 제거, 연속 공백 1칸으로, 전각→반각, 영문 대문자화, 흔한 잡음 토큰 제거.
**하지 않는 것**: 숙성연수(`17년`, `12년`), 도수/제품 코드(`105`, `CS`, `NAS`), 캐스크 표기(`쉐리`, `비노바리끄`) 제거. **이건 상품을 구분하는 핵심 정보입니다.**

```python
import re

# 상품 식별과 무관한 잡음 토큰만 제거한다. 여기에 '년', 'CS', 숫자를 절대 추가하지 말 것.
_NOISE = ["면세", "免稅", "1병", "증정", "행사", "할인", "(정품)", "정품"]

def normalize_product_key(name_raw: str) -> str:
    s = (name_raw or "").strip()
    for token in _NOISE:
        s = s.replace(token, " ")
    s = s.upper()                       # 영문만 영향받음. 한글은 그대로
    s = re.sub(r"\s+", " ", s).strip()
    return s or (name_raw or "").strip()
```

정규화 결과가 빈 문자열이 되면 원문을 그대로 씁니다(전부 잡음 토큰인 경우 방어).

### 3-4. 사용자가 고치는 방법 — "술 이름 정리" 패널

화면에 별도 패널을 둡니다.

- 현재 존재하는 `product_key`들을 **구매 횟수와 함께** 목록으로 보여줍니다 (`발렌타인 17년 (4건)`).
- 두 개 이상을 체크해서 "병합"을 누르면, 대표 이름을 고르는 입력칸이 뜨고, 확인 시 `POST /api/liquor_purchases/merge_key` 한 번으로 해당 레코드들의 `product_key`가 일괄 변경됩니다.
- **병합 제안**: 화면에서 이름 쌍의 유사도(bigram Dice 계수 등 간단한 방식)를 계산해 0.7 이상인 쌍만 "혹시 같은 술인가요?"로 상단에 띄웁니다. **제안일 뿐이고 자동 적용은 절대 하지 않습니다.** 사용자가 누르지 않으면 아무 일도 일어나지 않습니다.
- **병합 제안 영구 제외 (2026-09-18 추가)**: 유사도는 높지만 실제로는 다른 술이라 "병합 안 함"을 누른 쌍은 다음 방문에도 같은 제안이 반복해서 뜨지 않아야 합니다. "병합 안 함" 버튼을 누르면 `POST /api/liquor_purchases/merge_key`가 아니라 `POST /api/liquor_purchases/dismiss_suggestion`을 호출해 해당 `product_key` 쌍을 영구 제외 목록에 추가합니다. 프론트는 `GET /api/liquor_purchases` 응답의 `dismissed_merge_pairs`를 읽어, 유사도 계산 결과 중 이 목록에 있는 쌍은 애초에 제안 목록에서 걸러냅니다.

122행 / 고유 이름 수십 개 규모에서, 이 패널로 정리하는 데 몇 분이면 충분합니다. 이 정도 수작업이 자동 유사도 병합의 오판 위험보다 훨씬 쌉니다.

### 3-5. 검토했지만 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| 완전 자동 정규화만으로 그룹핑 | `발렌타인 17년` / `발렌타인 30년`을 구분하면서 오타는 흡수하는 규칙은 존재하지 않음. 사용자가 고칠 수단도 없음 |
| 조회 시점 퍼지 매칭(Levenshtein 등)으로 동적 그룹핑 | 숙성연수만 다른 제품들이 유사도 0.9를 넘어 자동으로 합쳐짐. 결과가 데이터에 남지 않아 사용자가 교정할 수도 없음. **가장 위험한 선택** |
| 상품 마스터 테이블 분리(`products.json` + `product_id` 참조) | 정규화 모델로는 가장 옳지만, 마스터 CRUD 화면 / 고아 참조 정리 / 무결성 검사가 전부 따라옴. 1인 사용자 122행에는 과설계 |
| 술이름 원문을 그대로 그룹 키로 사용 | 요구사항 2("표기가 달라도 그룹핑")를 아예 만족하지 못함 |

---

## 4. CSV 가져오기 규칙

### 4-1. 인코딩

`utf-8-sig`로 먼저 시도하고, `UnicodeDecodeError`가 나면 `cp949`로 재시도합니다.
구글 시트 CSV 내보내기는 UTF-8(BOM 포함)이지만, 사용자가 중간에 Excel로 열어 저장하면 cp949가 되는 경우가 흔합니다. 둘 다 실패하면 400으로 "인코딩을 읽을 수 없습니다"를 반환합니다.

### 4-2. 헤더 매핑

**첫 행을 헤더로 간주**합니다. 헤더 이름은 `공백 제거 + 소문자화` 후 아래 별칭표로 매칭합니다. 표에 없는 컬럼은 조용히 무시합니다(시트에 계산용 임시 컬럼이 있어도 무해).

| 내부 필드 | 허용 헤더(별칭) |
|---|---|
| `date` | `일자`, `날짜`, `구매일`, `구매일자`, `date` |
| `store` | `구매장소`, `장소`, `구매처`, `store` |
| `name_raw` | `술이름`, `이름`, `상품명`, `제품명`, `name` |
| `volume_ml` | `용량`, `사이즈`, `volume` |
| `promo` | `상품/행사`, `상품행사`, `행사`, `프로모션`, `promo` |
| `price_krw` | `가격`, `금액`, `원화`, `price` |
| `fx_amount` | `외화`, `외화금액`, `fx` |
| `fx_rate` | `당시환율`, `환율`, `rate` |
| `note` | `비고`, `메모`, `note`, `부가설명`(2026-09-18 추가 - 확장된 시트의 표기) |
| `opened_date` | `개봉일`, `오픈일` |
| `emptied_date` | `비운날`, `비운날짜`, `완음일` |
| `category` | `주종`, `category` (2026-09-18 추가. 값이 있으면 그대로 쓰고 없을 때만 `guess_category()` 추정 - 4-6 참고) |
| `cask` | `캐스크`, `cask` (2026-09-18 추가) |
| `region` | `나라/지역`, `지역`, `나라`, `region` (2026-09-18 추가) |
| `classification1` | `분류1`, `classification1` (2026-09-18 추가) |
| `classification2` | `분류2`, `classification2` (2026-09-18 추가) |
| `moved_date` | `병옮긴날`, `moved_date` (2026-09-18 추가) |

**필수 컬럼은 `date`, `name_raw`, `price_krw` 셋뿐**입니다. 셋 중 하나라도 헤더에 없으면 파일 전체를 거부하고, **어떤 컬럼이 없는지 이름을 찍어서** 400을 반환합니다("헤더를 확인하세요" 같은 막연한 메시지 금지).

**의도적으로 매핑하지 않는 컬럼**: 2026-09-18 확장 시트의 `오픈기간`은 매핑 표에 등록하지 않았습니다. 값이 `"-"` 또는 `"N일"` 형태인 파생값(= `opened_date`와 `emptied_date`(또는 오늘 날짜)의 차이)이라, ADR-0004의 "파생값은 저장하지 않는다" 원칙과 동일하게 저장하지 않고 화면에서 매번 계산합니다. 표에 없는 컬럼은 조용히 무시되므로 별도 처리가 필요 없습니다.

### 4-3. 일자 파싱

허용 형식: `24/4/6`, `2024/4/6`, `2024-04-06`, `24-4-6`, `2024. 4. 6`.
구분자를 `/`, `-`, `.`로 통일한 뒤 세 조각으로 나눕니다. 연도가 2자리면 `2000 + YY`로 해석합니다(데이터 범위 2022~2026이므로 안전).
결과는 항상 `YYYY-MM-DD`로 0 채움(`2024-04-06`). 파싱 실패 시 행 단위 오류로 처리합니다.

### 4-4. 가격 / 용량 파싱

```python
def parse_price(raw: str):
    """'₩101,810' -> 101810"""
    s = re.sub(r"[^\d.-]", "", raw or "")   # ₩, 쉼표, 공백, '원' 전부 제거
    if not s:
        return None
    return int(round(float(s)))
```

- `price_krw`가 `None`이거나 음수면 행 오류(`0`은 허용 — 가격 미상 기록 패턴, 2026-09-18 변경. 상단 변경 이력 참고).
- 용량: 문자열에서 첫 숫자를 뽑고, 단위에 `ml`이 없는 `l`/`L`/`리터`가 있으면 ×1000 (`0.7L` → 700). `700 ml`, `1000ml`, `750 ml` 모두 정상 처리.
- 용량이 비었거나 파싱 실패면 **오류가 아니라 `volume_ml: null`**. 용량은 필수가 아니며, null인 행은 100ml당 단가 차트에서만 제외됩니다.

### 4-5. 외화 / 환율 파싱 — 통화는 "추정 라벨"로만 취급

`외화` 값은 `$262`, `262 USD`, `NT$262` 같은 형태입니다. 정규식으로 기호/코드와 숫자를 분리합니다.

**여기서 주의할 함정**: 시트에는 `외화 = $262`, `당시환율 = 42.5`인 행이 있습니다. USD 환율은 1,300원대이므로 **이 `$`는 달러가 아니라 대만 달러(NT$)입니다**(구매장소가 "대만 가퉁완주"인 것과 일치). 기호만 보고 통화를 확정하면 틀립니다.

따라서:
- `fx_currency`는 **검증 대상이 아닌 자유 문자열 표시 라벨**로 정의합니다. enum 검증을 넣지 마십시오.
- 가져오기 시 환율 크기를 힌트로 추정합니다: `fx_rate >= 800` → `USD`/`EUR` 후보, `30 <= fx_rate < 100` → `TWD`/`HKD` 후보, 그 외는 기호 원문(`$`)을 그대로 넣습니다. 확신이 없으면 **추측하지 말고 기호 원문을 넣는 쪽**을 택합니다.
- 사용자가 화면에서 언제든 고칠 수 있습니다.
- **정합성 경고(오류 아님)**: `fx_amount`, `fx_rate`가 둘 다 있는데 `|price_krw - fx_amount * fx_rate| / price_krw > 0.15`이면 응답의 `warnings[]`에 담아 알려주되 **가져오기는 그대로 진행**합니다. 원화 `price_krw`가 모든 차트의 기준값이고 외화는 참고 정보이므로, 여기서 막을 이유가 없습니다.

### 4-6. 주종(`category`) 추정

**2026-09-18 변경**: 확장된 시트에 `주종` 컬럼이 생겼습니다. CSV에 `주종` 값이 있으면 **그 값을 그대로 사용**하고, 비어 있을 때만 아래처럼 `name_raw` 키워드로 추정합니다(못 맞히면 `"미분류"`). 사용자가 화면에서 언제든 고칠 수 있는 건 이전과 동일합니다.

- `위스키`: 위스키, 싱글몰트, 발렌타인, 글렌, 맥캘란, 카발란, 야마자키, 버번, 스카치, 라프로익, 아드벡, WHISKY, BOURBON …
- `와인`: 와인, 샴페인, 까베르네, 피노, 메를로, WINE …
- `사케`: 사케, 니혼슈, 청주, 준마이, 다이긴조 …
- `브랜디`: 꼬냑, 코냑, 브랜디, 헤네시, 레미마르탱 …
- 그 외 → `미분류`

키워드 목록은 `core/liquor_manager.py` 상단에 dict 상수로 두어 나중에 추가하기 쉽게 합니다.

### 4-7. 행 단위 오류 처리와 두 단계 가져오기

**절대 하지 말 것**: 한 행이 깨졌다고 122행 전체를 거부하는 것. 그러면 사용자가 시트를 고치고 다시 올리기를 반복해야 합니다.

방식:
1. **1단계 `dry_run=true` (미리보기)** — 아무것도 저장하지 않고, 몇 행이 정상 파싱됐는지 / 어떤 행이 왜 실패했는지 / 중복으로 건너뛸 행은 몇 개인지 / 앞 5행이 어떻게 해석됐는지를 돌려줍니다. UI는 **가져오기 전에 반드시 이 단계를 먼저 호출**합니다.
2. **2단계 `dry_run=false` (실행)** — 사용자가 미리보기를 확인한 뒤 실행. 정상 행만 저장하고, 실패 행은 응답의 `errors[]`로 다시 알려줍니다.

- `일자`·`술이름`·`가격`이 **전부 비어 있는 행**은 오류가 아니라 조용히 건너뜁니다(시트 끝의 빈 줄).
- **자연키 중복 스킵 없음 (2026-09-18 변경)**: 원래 `(date, name_raw, price_krw)`가 기존 레코드와 같으면 건너뛰는 규칙이 있었으나 **제거했습니다.** 같은 날 같은 술을 여러 병 사서 병마다(개봉일·비운 날짜가 달라) 행을 따로 기록하는 것이 정상 패턴이라, 이 dedupe가 실제 구매 기록을 유실시켰습니다. 파싱에 성공한 행은 전부 저장하며, `skipped_duplicates`는 프론트 호환을 위해 응답에 남아 있지만 항상 `0`입니다. 상단 변경 이력 참고.
- `mode`는 `append`(기본) / `replace`(전체 교체, 실행 전 `.bak.json` 백업). `replace`는 UI에서 한 번 더 확인을 받습니다.

### 4-8. 업로드 안전장치

이 대시보드는 Cloudflare 터널로 외부에 노출됩니다. 업로드 엔드포인트에는 최소한의 방어를 둡니다.

- 파일 크기 **2MB 상한** 초과 시 413. (122행 CSV는 20KB 남짓)
- 확장자가 `.csv`/`.txt`가 아니면 400.
- 기존 API와 동일하게 `@token_required` 적용.

---

## 5. API 계약

모든 엔드포인트는 `@token_required`이며, 요청 헤더에 `X-Butler-Token`이 필요합니다. 프론트에서는 `base.html`이 이미 정의해 둔 전역 상수 `BUTLER_API_TOKEN`을 씁니다.

응답 형태는 기존 `manage_settlements()` 규약을 그대로 따릅니다: 성공은 `{"status": "success", ...}`, 실패는 `{"status": "failed", "reason": "..."}` + 적절한 HTTP 코드.

### 5-0. 쓰기 작업은 기존 VWAP admin 세션으로 보호 (2026-09-18 추가)

조회(`GET /liquor`, `GET /api/liquor_purchases`)는 `@token_required`만으로 누구나 가능하지만, **쓰기 작업**(추가/수정/삭제/병합/CSV 가져오기/병합 제안 제외)에는 `@token_required` 위에 `api/vwap_api.py`의 `admin_required`를 추가로 씌웠습니다. 새 비밀번호 체계를 만들지 않고 VWAP 트레이딩 시스템의 기존 admin 인증을 그대로 재사용한 것입니다.

- 로그인은 기존 `POST /vwap/login`을 그대로 씁니다(이 문서에 새 로그인 엔드포인트를 추가하지 않습니다). 성공하면 `vwap_session` 쿠키가 `path='/'`로 발급되어 사이트 전체(이 술 페이지 포함)에서 유효합니다 — 즉 VWAP에 이미 로그인돼 있으면 술 페이지에서도 별도 로그인 없이 쓰기 작업이 됩니다.
- `admin_required`는 `Authorization: Bearer <token>` 헤더 또는 `vwap_session` 쿠키의 토큰을 `VwapCrypto.verify_session_token()`으로 검증하고, 실패 시 `401 {"status":"failed","reason":"unauthorized"}`을 반환합니다.
- `@admin_required`가 붙는 엔드포인트: `POST/PUT/DELETE /api/liquor_purchases`, `POST /api/liquor_purchases/merge_key`, `POST /api/liquor_purchases/dismiss_suggestion`, `POST /api/liquor_purchases/import`.
- `GET /api/liquor_purchases`, `GET /liquor`에는 `admin_required`를 걸지 않습니다.

### 5-1. 페이지 라우트

```
GET /liquor  ->  render_template('liquor.html', api_token=BUTLER_API_TOKEN)
```

`/settlement`와 동일하게 페이지 자체에는 토큰 검사를 걸지 않습니다(토큰은 템플릿을 통해 JS로 주입됨).

### 5-2. `GET /api/liquor_purchases` — 전체 조회

쿼리 파라미터 없음. **필터링·집계는 전부 화면(JS)에서 합니다.**

> 서버 집계 엔드포인트를 따로 두지 않는 이유: 전체 데이터가 122행(약 30KB)이고 연 30행 정도씩만 늘어납니다. 한 번에 다 내려주면 차트 필터를 바꿀 때마다 서버를 다시 부를 필요가 없어 화면이 즉각 반응하고, 엔드포인트 수와 프론트/백 간 계약도 줄어듭니다.

```json
{
  "status": "success",
  "count": 122,
  "liquor_purchases": [ { ...레코드... }, ... ],
  "dismissed_merge_pairs": [ ["카발란 술리스트 비노바리끄", "카발란 솔리스트 비노바리끄"] ]
}
```

정렬: `date` **내림차순**(최신 먼저), 같은 날짜면 `created_at` 내림차순.

`dismissed_merge_pairs`는 "병합 안 함"으로 처리된 `product_key` 쌍 목록입니다(2026-09-18 추가, 5-6-1 참고). 프론트는 병합 제안을 계산할 때 이 목록에 있는 쌍을 걸러냅니다.

### 5-3. `POST /api/liquor_purchases` — 1건 추가

요청 (`Content-Type: application/json`). `id`, `created_at`, `updated_at`은 서버가 만드므로 **보내지 않습니다**.

```json
{
  "date": "2026-09-17",
  "store": "신라인터넷면세점",
  "name_raw": "글렌파클라스105 CS",
  "product_key": "글렌파클라스105 CS",
  "category": "위스키",
  "volume_ml": 700,
  "promo": "",
  "price_krw": 98000,
  "fx_amount": 68,
  "fx_currency": "USD",
  "fx_rate": 1390.5,
  "note": "",
  "opened_date": null,
  "emptied_date": null,
  "approx_fields": []
}
```

서버 처리:
- 필수 검증: `date`(형식 `YYYY-MM-DD`), `name_raw`(비어있지 않음), `price_krw`(0 이상 정수 — `0` 허용, 음수만 오류. 2026-09-18 변경). 위반 시 `400 {"status":"failed","reason":"invalid_date"|"empty_name"|"invalid_price"}`.
- `product_key`가 비어 있으면 `normalize_product_key(name_raw)`로 채웁니다.
- `category`가 비어 있으면 `guess_category(name_raw)`로 채웁니다.
- `opened_date`/`emptied_date`/`approx_fields`는 전부 선택 필드이며 기본값은 각각 `null`, `null`, `[]`입니다(2026-09-18 추가).
- 생략된 선택 필드는 2-2의 기본값(`""` 또는 `null`)으로 채웁니다.

응답 `200`:
```json
{ "status": "success", "id": "a3f19c04b7d2", "record": { ...저장된 전체 레코드... } }
```

`record`를 돌려주므로 프론트는 추가 후 목록을 다시 불러올 필요가 없습니다.

### 5-4. `PUT /api/liquor_purchases` — 1건 수정

요청 본문에 **`id` 필수** + 바꿀 필드들. 보내지 않은 필드는 기존 값을 유지합니다(부분 수정).

```json
{ "id": "a3f19c04b7d2", "product_key": "발렌타인 17년", "category": "위스키" }
```

- `id`가 없으면 `400 {"reason": "missing_id"}`, 해당 레코드가 없으면 `404 {"reason": "not_found"}`.
- `id`, `created_at`은 요청에 들어와도 **무시**합니다(덮어쓰기 금지).
- 성공 시 `updated_at`을 현재 시각으로 갱신.

응답 `200`:
```json
{ "status": "success", "record": { ...수정된 전체 레코드... } }
```

### 5-5. `DELETE /api/liquor_purchases` — 1건 삭제

기존 `manage_settlements()`의 DELETE와 동일하게 **JSON 본문**으로 id를 받습니다.

```json
{ "id": "a3f19c04b7d2" }
```

응답 `200` `{"status":"success"}` / 없으면 `404 {"status":"failed","reason":"not_found"}`.

> UI는 삭제 전 반드시 확인 모달을 띄웁니다(`base.html`의 `openModal()` 재사용).

### 5-6. `POST /api/liquor_purchases/merge_key` — 표준 이름 일괄 변경

3-4의 병합 기능용입니다.

```json
{ "from_keys": ["카발란 술리스트 비노바리끄", "카발란솔리스트 비노바리끄"], "to_key": "카발란 솔리스트 비노바리끄" }
```

- `from_keys`가 비었거나 `to_key`가 빈 문자열이면 `400 {"reason":"invalid_params"}`.
- `product_key`가 `from_keys` 중 하나와 정확히 일치하는 모든 레코드의 `product_key`를 `to_key`로 바꾸고 `updated_at`을 갱신합니다.
- `name_raw`는 **건드리지 않습니다.**

응답 `200`:
```json
{ "status": "success", "updated": 5, "to_key": "카발란 솔리스트 비노바리끄" }
```

### 5-6-1. `POST /api/liquor_purchases/dismiss_suggestion` — 병합 제안 영구 제외 (2026-09-18 추가)

3-4의 "병합 안 함" 버튼용입니다. 새 파일을 만들지 않고, 기존 `data/liquor_purchases.json`의 최상위에 `dismissed_merge_pairs` 배열을 추가해서 저장합니다(그러면 `sync_s9.py` exclude_list에 파일을 또 추가해야 하는 실수 위험이 없습니다).

```json
{ "a": "카발란 술리스트 비노바리끄", "b": "카발란 솔리스트 비노바리끄" }
```

- `a`, `b` 중 하나라도 빈 문자열이면 `400 {"status":"failed","reason":"invalid_params"}`.
- 서버는 `[a, b]`를 **알파벳/가나다 순으로 정렬**해서 저장합니다(`[min(a,b), max(a,b)]`). 순서를 바꿔서 다시 호출해도 같은 쌍으로 인식되어 중복 추가되지 않습니다.
- `a == b`인 경우 아무 것도 하지 않습니다(무의미한 자기 자신과의 쌍).

응답 `200`:
```json
{ "status": "success" }
```

`GET /api/liquor_purchases` 응답의 `dismissed_merge_pairs`(5-2 참고)로 현재까지 제외된 쌍 전체를 내려주므로, 프론트는 페이지 로드 시 이 목록으로 병합 제안 후보를 필터링하면 됩니다.

### 5-7. `POST /api/liquor_purchases/import` — CSV 가져오기

`Content-Type: multipart/form-data`

| 폼 필드 | 값 | 설명 |
|---|---|---|
| `file` | CSV 파일 | 필수 |
| `dry_run` | `"true"` / `"false"` | 기본 `"true"`. 문자열로 오므로 `== "true"` 비교 |
| `mode` | `"append"` / `"replace"` | 기본 `"append"` |

응답 `200` (미리보기·실행 공통 형태):

```json
{
  "status": "success",
  "dry_run": true,
  "mode": "append",
  "parsed": 122,
  "imported": 0,
  "skipped_duplicates": 0,
  "total_after": 124,
  "errors": [
    { "row": 57, "column": "가격", "value": "", "reason": "가격이 비어 있거나 숫자가 아님" },
    { "row": 88, "column": "일자", "value": "24/13/1", "reason": "날짜 형식을 해석할 수 없음" }
  ],
  "warnings": [
    { "row": 33, "reason": "외화×환율(11,135원)과 가격(11,000원) 차이가 큼 — 통화 확인 필요" }
  ],
  "preview": [ { ...파싱된 레코드 5건... } ]
}
```

- `row`는 **CSV 파일 기준 실제 행 번호(헤더=1행)** 입니다. 사용자가 시트에서 바로 찾아갈 수 있어야 합니다.
- `dry_run=true`면 `imported`는 항상 `0`이고 파일은 전혀 수정되지 않습니다.
- `dry_run=false`면 `imported`가 실제 저장 건수, `total_after`가 저장 후 총 레코드 수입니다.
- 헤더 자체가 잘못된 경우(4-2)는 `400 {"status":"failed","reason":"missing_columns","missing":["가격"]}`.
- `skipped_duplicates`는 자연키 중복 스킵 로직 제거(2026-09-18, 4-7 참고)로 인해 **항상 `0`**입니다. 필드 자체는 프론트 호환을 위해 남겨뒀습니다.

---

## 6. 화면 설계 (ui-dev 대상)

### 6-1. 사이드바 메뉴 추가

`api/templates/base.html`의 `/settlement` 링크(현재 108~111행) **바로 아래, `/vwap` 링크 위**에 삽입합니다. 기존 `<a>` 블록의 클래스를 그대로 복사해 쓰십시오.

- `href="/liquor"`, 아이콘 `data-lucide="wine"`, 라벨 **주류 구매 이력**
- lucide에 `wine`이 없으면 `glass-water`로 대체

### 6-2. 페이지 구성

`liquor.html`은 `{% extends 'base.html' %}` + `{% block page_title %}주류 구매 이력{% endblock %}` + `{% block content %}`로 작성합니다 (`settlement.html`과 동일 구조). 카드 스타일도 `settlement.html`의 것을 그대로 재사용하십시오.

```
1) 요약 카드 4개  — 총 지출 / 총 병 수 / 평균 병당 가격 / 최근 구매일
2) 필터 바        — 기간(연도 선택) · 주종 · 구매처 · 술이름 검색
3) 차트 영역      — 7장 참고 (탭 또는 2열 그리드)
4) 구매 이력 테이블 — 정렬/검색, 행 클릭 시 인라인 편집, 행 추가, 삭제
5) 술 이름 정리 패널 — 3-4 참고 (접어둘 수 있게)
6) CSV 가져오기 패널 — 6-3 참고
```

### 6-3. CSV 가져오기 UI 흐름

**반드시 2단계로** 만듭니다. 파일 선택 즉시 저장하는 UI는 만들지 마십시오.

```
파일 선택 -> [미리보기] 버튼 -> dry_run=true 호출
   -> "정상 120건 / 중복 2건 / 오류 2건" 요약 + 오류 행 목록(행번호·컬럼·사유) + 앞 5건 표
   -> 사용자가 확인 -> [가져오기] 버튼 활성화 -> dry_run=false 호출
   -> 결과 요약 표시 후 목록·차트 새로고침
```

`replace` 모드는 체크박스로 제공하되, 체크 시 "기존 N건이 모두 지워집니다"를 확인 모달로 한 번 더 묻습니다.

### 6-4. Chart.js 로드

`settlement.html`이 XLSX를 불러오는 방식과 동일하게 `{% block content %}` 최상단에 CDN 한 줄을 넣습니다.

```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
```

버전은 **고정(`@4.4.1`)** 합니다. `@latest`를 쓰면 어느 날 갑자기 차트가 깨질 수 있습니다.

다크모드 대응: `base.html`에 다크모드 토글(`toggleDarkMode()`)이 있으므로, 차트 색상은 하드코딩하지 말고 `document.documentElement.classList.contains('dark')`를 보고 축/그리드 색을 정하십시오.

---

## 7. 차트 설계

전부 `GET /api/liquor_purchases` 응답 하나로 JS에서 계산합니다. **7-1과 7-2가 필수**이고, 7-3~7-5는 여력이 되면 추가합니다. 한 번에 다 만들지 말고 필수부터 동작을 확인한 뒤 진행하십시오(프로젝트 규칙: 단계적으로).

### 7-1. [필수] 같은 술의 구매가격 변동 — 라인 차트

- 상단 셀렉트 박스에서 `product_key`를 고릅니다. 기본값은 **구매 횟수가 가장 많은 술**.
- x축 = `date`(시간순 오름차순), y축 = 가격. 점에 마우스를 올리면 구매장소·용량·행사 메모를 툴팁으로 보여줍니다.
- **y축 토글 2종을 반드시 제공**:
  - `병당 가격` = `price_krw`
  - `100ml당 단가` = `price_krw / volume_ml * 100`
- 이 토글이 필요한 이유: 같은 술이라도 700ml과 1000ml을 섞어 샀으면 병당 가격만으로는 "비싸게 샀는지"를 알 수 없습니다. 실제 시트에 700/750/1000ml이 섞여 있습니다.
- `volume_ml`이 `null`인 행은 100ml 모드에서 제외하고, "용량 미입력 N건 제외"를 차트 아래에 작게 표기합니다.
- 구매가 1건뿐인 술은 선이 그려지지 않으므로 점(point)만 표시하고 "구매 1회"를 안내합니다.

### 7-2. [필수] 월별·연도별 지출 추이 — 막대 + 누적선 복합 차트

- 연/월 전환 토글. 막대 = 해당 기간 지출 합계, 보조 y축의 선 = 누적 지출.
- 2022-11 ~ 2026-09를 한눈에 보는 메인 차트입니다. **데이터가 없는 달도 0으로 채워 넣어** 공백 구간이 시각적으로 드러나게 하십시오(월을 건너뛰면 추이가 왜곡됩니다).

### 7-3. 구매처별 지출 비중 — 도넛 차트

- `store` 기준 합계, 상위 7개 + 나머지는 `기타`로 묶습니다. 항목이 20개가 넘으면 범례가 차트를 덮습니다.
- 클릭 시 해당 구매처로 아래 테이블을 필터링하면 쓸모가 큽니다.

### 7-4. 주종별 지출 비중 — 도넛 또는 가로 막대

- `category` 기준. `미분류`는 회색으로 구분해 "분류 정리가 필요하다"는 신호를 주십시오.

### 7-5. 평균 구매 단가 추이 — 라인 차트

- 연도별 `100ml당 단가`의 평균. "해가 갈수록 더 비싼 술을 사고 있는가"를 보여줍니다.
- `volume_ml`이 있는 행만 대상으로 하고, 표본 수를 툴팁에 함께 표시합니다(3건 평균과 40건 평균을 같은 무게로 읽으면 오해가 생깁니다).

---

## 8. 구현 순서 (권장)

의존성이 있는 순서입니다. 1~2가 끝나기 전에는 UI에서 실제 데이터를 볼 수 없으므로, ui-dev는 그 전까지 **이 문서 5장의 응답 예시를 하드코딩한 더미 데이터**로 작업하면 병렬 진행이 가능합니다.

1. `sync_s9.py` 제외 목록 추가 (0-1) — **가장 먼저**
2. `core/liquor_manager.py`: load/save/new_id/정규화/카테고리 추정
3. `api/flask_app.py`: `/liquor` 페이지 + CRUD 3종 (5-2~5-5)
4. `api/templates/base.html` 메뉴 + `liquor.html` 골격 + 테이블 CRUD
5. `core/liquor_manager.py`: CSV 파서 (4장) + `/import` 엔드포인트
6. CSV 가져오기 UI (6-3)
7. 차트 7-1, 7-2
8. `merge_key` + 술 이름 정리 패널 (3-4)
9. 차트 7-3~7-5 (선택)

**배포/이관 절차** (코드 완성 후, 사용자가 수행):
1. `python dev_pjt/sync_manager/sync_s9.py`로 S9 전송
2. S9에서 `pm2 restart` (대상 프로세스명은 `docs/OPERATING_GUIDE.md` 참고)
3. **터널 URL로 접속해 S9 대시보드에서** 구글 시트 CSV 업로드 (0-2)
4. 미리보기로 122건이 맞는지 확인 후 실행
5. "술 이름 정리" 패널에서 표기 흔들린 항목 병합

---

## 9. 명시적으로 하지 않기로 한 것

범위를 지키기 위해 아래는 이번에 만들지 않습니다.

- **SQLite 도입** — 122행, 1인 사용, 단순 조회. flat JSON으로 충분하고 기존 패턴과도 일치합니다.
- **서버 사이드 집계/페이지네이션 API** — 5-2 참고.
- **상품 마스터 테이블** — 3-5 참고.
- **재고/시음 기록/평점** — 요구사항에 없습니다. 나중에 필요하면 필드를 추가하면 됩니다(`version` 필드를 둔 이유).
- **이미지 업로드(라벨 사진)** — S9 저장공간과 동기화 정책을 새로 정해야 해서 별건입니다.
