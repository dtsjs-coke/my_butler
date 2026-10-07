import os
import asyncio
import time
import json
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

# .env 로드 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from core.news_service import load_news
from core import liquor_manager
from core.subscription.service import load_yaml, save_yaml, SUBSCRIPTIONS_FILE, USERS_FILE
from utils.system_status import get_system_status_data, get_system_status_history
from config.config_manager import (
    load_keywords, load_stations,
    save_queue, serialize_queue
)
from core.srt.service import reservation_queue
from SRT.passenger import Adult, Child, Senior, Disability1To3
from SRT import SeatType

app = Flask(__name__)
from api.vwap_api import vwap_bp, admin_required
app.register_blueprint(vwap_bp, url_prefix='/vwap')
discord_client = None
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))
# (ADR-0011) 인증 경계. API 토큰(BUTLER_API_TOKEN)은 요청 시점에 .env 에서 읽고 기본값이 없다(fail-closed).
# 토큰은 템플릿에 넘기지 않는다 — 브라우저는 admin 세션 쿠키(vwap_session)로 인증한다.
# 로그인 없이 열리는 페이지: /settlement, /liquor, /news (사용자 결정 2026-10-07). 그 외 페이지는 admin 세션 필요.
from api.auth import session_or_token, token_only, local_send_only, page_login_required

@app.route('/')
@page_login_required
def home():
    return render_template('index.html')

@app.route('/trains')
@page_login_required
def trains_page():
    stations = load_stations()
    return render_template('trains.html', stations=stations)

@app.route('/api/srt/queue', methods=['GET', 'DELETE'])
@session_or_token
def manage_srt_queue():
    if request.method == 'GET':
        # [수정] 파일 대신 봇 메모리(reservation_queue)를 직접 직렬화하여 반환
        return jsonify({"status": "success", "queue": serialize_queue(reservation_queue)})
    
    data = request.get_json()
    user_id = str(data.get('user_id'))
    # 키가 숫자인 경우를 위해 변환 시도
    try:
        user_id_key = int(user_id)
    except ValueError:
        user_id_key = user_id

    idx = data.get('index')
    
    # [수정] 봇 메모리에서 즉시 삭제
    if user_id_key in reservation_queue and 0 <= idx < len(reservation_queue[user_id_key]):
        del reservation_queue[user_id_key][idx]
        if not reservation_queue[user_id_key]:
            del reservation_queue[user_id_key]
        save_queue(reservation_queue)
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "failed", "reason": "not_found"}), 404

@app.route('/api/srt/reserve', methods=['POST'])
@session_or_token
def api_srt_reserve():
    from datetime import datetime
    data = request.get_json()
    # 기본 검증
    if not data.get('dep') or not data.get('arr') or not data.get('date'):
        return jsonify({"status": "failed", "reason": "missing_data"}), 400

    # 데이터 변환 (Discord와 동일한 포맷)
    user_id = "WEB_USER" # 웹 예약은 공통 ID 사용
    
    # [수정] 봇 메모리 직접 사용
    if user_id not in reservation_queue:
        reservation_queue[user_id] = []
    
    if len(reservation_queue[user_id]) >= 3:
        return jsonify({"status": "failed", "reason": "queue_full"}), 400

    # [수정] 승객 리스트를 단순 글자가 아닌 실제 SRT 객체로 생성 (중요: 에러 해결책)
    passengers = []
    for _ in range(int(data.get('adult', 1))): passengers.append(Adult())
    for _ in range(int(data.get('child', 0))): passengers.append(Child())
    for _ in range(int(data.get('senior', 0))): passengers.append(Senior())
    for _ in range(int(data.get('disability', 0))): passengers.append(Disability1To3())

    # [수정] SeatType을 Enum 객체로 변환
    seat_type_str = data.get('seat_type', 'GENERAL_FIRST')
    try:
        seat_type = SeatType[seat_type_str]
    except:
        seat_type = SeatType.GENERAL_FIRST

    task = {
        "dep": data['dep'],
        "arr": data['arr'],
        "date": data['date'],
        "time": data['time'],
        "time_limit": data.get('time_limit'),
        "passengers": passengers,  # 실제 객체 리스트 저장
        "seat_type": seat_type,    # Enum 객체 저장
        "window_seat": data.get('window_seat', False),
        "status": "시도중",
        "user_name": "Web Dashboard",
        "created_at": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }
    
    reservation_queue[user_id].append(task)
    # 영속성 파일 저장
    save_queue(reservation_queue)
    return jsonify({"status": "success"}), 200

from datetime import datetime, timedelta

@app.route('/news')
def news_page():
    # 3일치 뉴스 로드
    news = load_news()
    
    # 키워드 그룹 설정 로드
    groups = {}
    group_file = os.path.join(PROJECT_ROOT, "data", "keyword_groups.json")
    if os.path.exists(group_file) and os.path.getsize(group_file) > 0:
        try:
            with open(group_file, 'r', encoding='utf-8') as f:
                raw_groups = json.load(f)
                # 역방향 매핑 (아이온큐 -> ionq)
                for group_name, members in raw_groups.items():
                    for m in members:
                        groups[m.lower()] = group_name
        except: pass

    # 최신순 정렬
    news.sort(key=lambda x: x.get('pub_date', x.get('date', '')), reverse=True)
    news = news[:200]

    # 키워드별 그룹화 (그룹핑 적용)
    categorized_news = {}
    for n in news:
        raw_kw = n.get('keyword', '기타')
        # 그룹 매핑이 있으면 그룹명 사용, 없으면 원본 키워드 사용
        kw_lower = raw_kw.lower()
        kw = groups.get(kw_lower, raw_kw)
        
        # UI 표시를 위해 그룹 이름은 대문자로 통일하거나 첫 글자 대문자 처리
        if kw_lower in groups:
            kw = groups[kw_lower].upper()
        
        if kw not in categorized_news:
            categorized_news[kw] = []
        if len(categorized_news[kw]) < 50:
            categorized_news[kw].append(n)

    return render_template('news.html', categorized_news=categorized_news, now=datetime.now())

def _load_keyword_groups(group_file):
    if not os.path.exists(group_file) or os.path.getsize(group_file) == 0:
        return {}
    try:
        with open(group_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

@app.route('/api/keyword_groups', methods=['GET'])
def get_keyword_groups():
    """공개: 뉴스 페이지(공개)의 그룹 필터가 쓴다. 수정(POST/DELETE)은 세션 또는 토큰 필요(ADR-0011)."""
    group_file = os.path.join(PROJECT_ROOT, "data", "keyword_groups.json")
    return jsonify({"status": "success", "groups": _load_keyword_groups(group_file)})

@app.route('/api/keyword_groups', methods=['POST', 'DELETE'])
@session_or_token
def manage_keyword_groups():
    group_file = os.path.join(PROJECT_ROOT, "data", "keyword_groups.json")

    def load_groups():
        return _load_keyword_groups(group_file)

    data = request.get_json()
    groups = load_groups()

    if request.method == 'POST':
        group_name = data.get('group_name')
        members = data.get('members', [])
        if not group_name:
            return jsonify({"status": "failed", "reason": "empty_group_name"}), 400
        
        groups[group_name] = members
        try:
            with open(group_file, 'w', encoding='utf-8') as f:
                json.dump(groups, f, ensure_ascii=False, indent=4)
            return jsonify({"status": "success"}), 200
        except:
            return jsonify({"status": "failed", "reason": "save_error"}), 500

    elif request.method == 'DELETE':
        group_name = data.get('group_name')
        if group_name in groups:
            del groups[group_name]
            try:
                with open(group_file, 'w', encoding='utf-8') as f:
                    json.dump(groups, f, ensure_ascii=False, indent=4)
                return jsonify({"status": "success"}), 200
            except:
                return jsonify({"status": "failed", "reason": "save_error"}), 500
        return jsonify({"status": "failed", "reason": "not_found"}), 404

@app.route('/api/system_status')
@session_or_token
def api_status():
    start_time = time.time()
    data = get_system_status_data()
    data["history"] = get_system_status_history()
    elapsed = (time.time() - start_time) * 1000
    print(f"[API] system_status request took {elapsed:.2f}ms")
    return jsonify(data)

from config.config_manager import save_keywords

@app.route('/api/keywords', methods=['GET', 'POST', 'DELETE'])
@session_or_token
def manage_keywords():
    if request.method == 'GET':
        return jsonify({"status": "success", "keywords": load_keywords()})
    
    data = request.get_json()
    if request.method == 'POST':
        keyword = data.get('keyword')
        if not keyword:
            return jsonify({"status": "failed", "reason": "empty_keyword"}), 400
        
        keywords = load_keywords()
        if keyword in keywords:
            return jsonify({"status": "failed", "reason": "already_exists"}), 400
        
        keywords.append(keyword)
        save_keywords(keywords)
        return jsonify({"status": "success"}), 200

    elif request.method == 'DELETE':
        keyword = data.get('keyword')
        if not keyword:
            return jsonify({"status": "failed", "reason": "empty_keyword"}), 400
        
        keywords = load_keywords()
        if keyword not in keywords:
            return jsonify({"status": "failed", "reason": "not_found"}), 404
        
        keywords.remove(keyword)
        save_keywords(keywords)
        return jsonify({"status": "success"}), 200

@app.route('/settlement')
def settlement_page():
    return render_template('settlement.html')

# ---------------------------------------------------------------------------
# 정산 (ADR-0011): 공개 페이지이며, 친구들이 로그인 없이 저장/불러오기/삭제까지 하는 공유 계산기다.
# 그래서 /api/settlements 는 조회·쓰기 모두 인증 없이 열어 둔다. 대신 공개 쓰기의 위험을 줄인다.
#   - 저장형 XSS 차단: 이 페이지는 제목·이름·메모를 innerHTML/속성/onclick 문자열에 그대로 넣는다. 같은 출처에
#     VWAP admin 화면이 있으므로, 여기서 스크립트가 실행되면 admin 쿠키로 실거래 API 를 호출할 수 있다.
#     → 모든 문자열(딕셔너리 키 포함)에서 < > " ' ` & \ 와 제어문자를 거부한다(400 invalid_chars).
#   - 크기·개수 상한: 본문 64KB, 제목 100자, 문자열 200자, 참석자 50명, 항목 200개, 보관 정산 50건.
#   - 원자적 쓰기 + 잠금: 임시 파일에 쓴 뒤 os.replace (threaded=True 동시 요청에서 파일이 깨지지 않게).
# 쓰기를 admin 전용으로 돌리려면 SETTLEMENT_PUBLIC_WRITE 를 False 로 바꾼다(세션 또는 토큰 필요).
# ---------------------------------------------------------------------------
import threading
import math
SETTLEMENT_PUBLIC_WRITE = True
SETTLEMENT_MAX_BODY = 64 * 1024
SETTLEMENT_MAX_TITLE = 100
SETTLEMENT_MAX_STR = 200
SETTLEMENT_MAX_PARTICIPANTS = 50
SETTLEMENT_MAX_ITEMS = 200
SETTLEMENT_MAX_SAVED = 50
_SETTLEMENT_FORBIDDEN = frozenset('<>"\'`&\\')
_settlement_lock = threading.RLock()

def _settlement_file():
    return os.path.join(PROJECT_ROOT, "data", "settlements.json")

def _write_settlements(settlements):
    settlement_file = _settlement_file()
    os.makedirs(os.path.dirname(settlement_file), exist_ok=True)
    tmp_path = settlement_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"settlements": settlements}, f, ensure_ascii=False, indent=4, allow_nan=False)
    os.replace(tmp_path, settlement_file)

def _settlement_str_ok(value, max_len=SETTLEMENT_MAX_STR):
    if len(value) > max_len:
        return False
    return not any(c in _SETTLEMENT_FORBIDDEN or ord(c) < 0x20 or ord(c) == 0x7F for c in value)

def _settlement_value_ok(value, depth=0):
    """정산 항목 값 검사: 허용 타입(문자열/숫자/불리언/None/리스트/딕셔너리), 깊이 4, 문자열 규칙."""
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)  # NaN/Infinity 는 JSON 표준이 아니라 저장 목록·응답을 깨뜨린다
    if isinstance(value, str):
        return _settlement_str_ok(value)
    if isinstance(value, list):
        return len(value) <= SETTLEMENT_MAX_ITEMS and all(_settlement_value_ok(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= SETTLEMENT_MAX_ITEMS and all(
            isinstance(k, str) and _settlement_str_ok(k) and _settlement_value_ok(v, depth + 1)
            for k, v in value.items())
    return False

def _validate_settlement_payload(data):
    """(정상 여부, 실패 사유) 반환."""
    if not isinstance(data, dict):
        return False, "invalid_body"
    title = data.get('title', '새 정산')
    participants = data.get('participants', [])
    items = data.get('items', [])
    s_id = data.get('id')
    if not isinstance(title, str) or not isinstance(participants, list) or not isinstance(items, list):
        return False, "invalid_body"
    if s_id is not None and not (isinstance(s_id, str) and len(s_id) <= 40 and s_id.isascii() and s_id.isdigit()):
        return False, "invalid_id"
    if len(participants) > SETTLEMENT_MAX_PARTICIPANTS or len(items) > SETTLEMENT_MAX_ITEMS:
        return False, "too_large"
    if not _settlement_str_ok(title, SETTLEMENT_MAX_TITLE):
        return False, "invalid_chars"
    if not all(isinstance(p, str) for p in participants) or not all(isinstance(i, dict) for i in items):
        return False, "invalid_body"
    if not _settlement_value_ok(participants) or not _settlement_value_ok(items):
        return False, "invalid_chars"
    return True, None

def _settlement_write_guard(f):
    """SETTLEMENT_PUBLIC_WRITE=False 이면 쓰기(POST/DELETE)에 세션 또는 토큰을 요구한다."""
    from functools import wraps
    from api.auth import has_admin_session, has_valid_token

    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method != 'GET' and not SETTLEMENT_PUBLIC_WRITE and not (has_admin_session() or has_valid_token()):
            return jsonify({"status": "failed", "reason": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

def _reject_json_constant(name):
    raise ValueError("non-finite JSON constant: " + name)

def _parse_finite_float(text):
    value = float(text)
    if not math.isfinite(value):  # 1e999 -> inf
        raise ValueError("non-finite number")
    return value

def _read_settlement_json():
    """정산 쓰기 본문을 읽어 (data, 오류 사유) 를 돌려준다. 오류 사유: None / 'too_large' / 'too_deep'.
    Content-Length 가 없는 chunked 전송도 상한(+1바이트)까지만 읽어 64KB 상한이 우회되지 않게 한다."""
    limit = SETTLEMENT_MAX_BODY
    if request.content_length is not None and request.content_length > limit:
        return None, "too_large"
    if not request.is_json:
        return None, None
    stream = request.stream
    buf = bytearray()
    while len(buf) <= limit:
        chunk = stream.read(limit + 1 - len(buf))
        if not chunk:
            break
        buf += chunk
    if len(buf) > limit:
        return None, "too_large"
    try:
        return json.loads(bytes(buf), parse_constant=_reject_json_constant, parse_float=_parse_finite_float), None
    except RecursionError:
        return None, "too_deep"
    except ValueError:  # JSONDecodeError, UnicodeDecodeError, NaN/Infinity/1e999
        return None, None

def _settlement_record_ok(record):
    """저장된 레코드가 dict 이고 엄격한 JSON(NaN/Infinity 없음)으로 직렬화되는지 확인한다."""
    if not isinstance(record, dict):
        return False
    try:
        json.dumps(record, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return False
    return True

def _settlement_response(payload, status=200):
    """allow_nan=False 로 직렬화한 JSON 응답(비정상 값이 클라이언트에서 JSON.parse 를 깨뜨리지 않게)."""
    return app.response_class(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n",
                              status=status, mimetype="application/json")

def cleanup_old_settlements():
    settlement_file = _settlement_file()
    if not os.path.exists(settlement_file):
        return []
    with _settlement_lock:
        try:
            with open(settlement_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                settlements = data.get("settlements", [])
        except:
            return []

        now = datetime.now()
        valid_settlements = []
        changed = False
        if not isinstance(settlements, list):
            return []
        for s in settlements:
            # 이미 저장된 비정상 레코드(NaN 등)는 목록에서 건너뛴다. 값을 임의로 고치면 정산 금액이 바뀌므로
            # 정리하지 않고 제외한다. 파일은 다음 쓰기(POST/DELETE) 때 정상 레코드만으로 다시 쓰인다.
            if not _settlement_record_ok(s):
                continue
            try:
                created_at = datetime.strptime(s.get("created_at"), "%Y-%m-%d %H:%M:%S")
                if now - created_at < timedelta(days=7):
                    valid_settlements.append(s)
                else:
                    changed = True
            except Exception as e:
                valid_settlements.append(s)

        if changed:
            try:
                _write_settlements(valid_settlements)
            except Exception as e:
                print(f"[Settlement Cleanup Error] {e}")

        return valid_settlements

@app.route('/api/settlements', methods=['GET', 'POST', 'DELETE'])
@_settlement_write_guard
def manage_settlements():
    if request.method == 'GET':
        valid_list = cleanup_old_settlements()
        return _settlement_response({"status": "success", "settlements": valid_list})

    data, body_error = _read_settlement_json()
    if body_error == "too_large":
        return jsonify({"status": "failed", "reason": "too_large"}), 413
    if body_error == "too_deep":
        return jsonify({"status": "failed", "reason": "too_deep"}), 400

    if request.method == 'POST':
        ok, reason = _validate_settlement_payload(data)
        if not ok:
            return jsonify({"status": "failed", "reason": reason}), 400
        s_id = data.get('id')
        title = data.get('title', '새 정산')
        participants = data.get('participants', [])
        items = data.get('items', [])

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with _settlement_lock:
            valid_list = cleanup_old_settlements()

            found = False
            if s_id:
                for s in valid_list:
                    if s.get('id') == s_id:
                        s['title'] = title
                        s['participants'] = participants
                        s['items'] = items
                        s['created_at'] = now_str  # Update timestamp to refresh retention duration
                        found = True
                        break
            if not found:
                if len(valid_list) >= SETTLEMENT_MAX_SAVED:
                    return jsonify({"status": "failed", "reason": "too_many_saved"}), 409
                s_id = str(int(time.time() * 1000))
                valid_list.append({
                    "id": s_id,
                    "title": title,
                    "participants": participants,
                    "items": items,
                    "created_at": now_str
                })

            try:
                _write_settlements(valid_list)
                return jsonify({"status": "success", "id": s_id}), 200
            except Exception as e:
                return jsonify({"status": "failed", "reason": str(e)}), 500

    elif request.method == 'DELETE':
        s_id = data.get('id') if isinstance(data, dict) else None
        if not s_id:
            return jsonify({"status": "failed", "reason": "missing_id"}), 400

        with _settlement_lock:
            valid_list = cleanup_old_settlements()
            new_list = [s for s in valid_list if s.get('id') != s_id]

            try:
                _write_settlements(new_list)
                return jsonify({"status": "success"}), 200
            except Exception as e:
                return jsonify({"status": "failed", "reason": str(e)}), 500

@app.route('/liquor')
def liquor_page():
    return render_template('liquor.html')

@app.route('/api/liquor_purchases', methods=['GET'])
def get_liquor_purchases():
    """조회는 누구나 가능 - 공개 페이지(ADR-0011)라 토큰도 요구하지 않는다(사용자 요청, 2026-09-18/10-07)."""
    records = liquor_manager.list_purchases_sorted()
    dismissed_merge_pairs = liquor_manager.load_dismissed_pairs()
    return jsonify({
        "status": "success",
        "count": len(records),
        "liquor_purchases": records,
        "dismissed_merge_pairs": dismissed_merge_pairs,
    }), 200

@app.route('/api/liquor_purchases', methods=['POST', 'PUT', 'DELETE'])
@admin_required
def write_liquor_purchases():
    """추가/수정/삭제는 VWAP과 동일한 admin 세션(vwap_session 쿠키)이 필요하다(2026-09-18)."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        record, reason = liquor_manager.create_purchase(data)
        if reason:
            return jsonify({"status": "failed", "reason": reason}), 400
        return jsonify({"status": "success", "id": record["id"], "record": record}), 200

    elif request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        purchase_id = data.get('id')
        if not purchase_id:
            return jsonify({"status": "failed", "reason": "missing_id"}), 400
        record, reason = liquor_manager.update_purchase(purchase_id, data)
        if reason == "not_found":
            return jsonify({"status": "failed", "reason": "not_found"}), 404
        return jsonify({"status": "success", "record": record}), 200

    elif request.method == 'DELETE':
        data = request.get_json(silent=True) or {}
        purchase_id = data.get('id')
        if not purchase_id:
            return jsonify({"status": "failed", "reason": "missing_id"}), 400
        ok = liquor_manager.delete_purchase(purchase_id)
        if not ok:
            return jsonify({"status": "failed", "reason": "not_found"}), 404
        return jsonify({"status": "success"}), 200

@app.route('/api/liquor_purchases/merge_key', methods=['POST'])
@admin_required
def merge_liquor_product_key():
    data = request.get_json(silent=True) or {}
    from_keys = data.get('from_keys') or []
    to_key = (data.get('to_key') or '').strip()
    if not from_keys or not to_key:
        return jsonify({"status": "failed", "reason": "invalid_params"}), 400
    updated = liquor_manager.merge_product_keys(from_keys, to_key)
    return jsonify({"status": "success", "updated": updated, "to_key": to_key}), 200

@app.route('/api/liquor_purchases/dismiss_suggestion', methods=['POST'])
@admin_required
def dismiss_liquor_merge_suggestion():
    data = request.get_json(silent=True) or {}
    key_a = (data.get('a') or '').strip()
    key_b = (data.get('b') or '').strip()
    if not key_a or not key_b:
        return jsonify({"status": "failed", "reason": "invalid_params"}), 400
    liquor_manager.dismiss_merge_suggestion(key_a, key_b)
    return jsonify({"status": "success"}), 200

LIQUOR_IMPORT_MAX_BYTES = 2 * 1024 * 1024  # 2MB 상한 (설계 문서 4-8)

@app.route('/api/liquor_purchases/import', methods=['POST'])
@admin_required
def import_liquor_purchases():
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({"status": "failed", "reason": "missing_file"}), 400

    filename = file.filename.lower()
    if not (filename.endswith('.csv') or filename.endswith('.txt')):
        return jsonify({"status": "failed", "reason": "invalid_file_type"}), 400

    file_bytes = file.read()
    if len(file_bytes) > LIQUOR_IMPORT_MAX_BYTES:
        return jsonify({"status": "failed", "reason": "file_too_large"}), 413

    dry_run = request.form.get('dry_run', 'true') == 'true'
    mode = request.form.get('mode', 'append')

    status_code, body = liquor_manager.import_csv(file_bytes, dry_run, mode)
    return jsonify(body), status_code

@app.route('/subscriptions/all', methods=['GET'])
@token_only
def get_all_subscriptions():
    data = load_yaml(SUBSCRIPTIONS_FILE).get("subscriptions", {})
    return jsonify(data)

@app.route('/subscriptions/<user_id>', methods=['GET', 'POST'])
@token_only
def handle_subscriptions(user_id):
    if request.method == 'GET':
        subscriptions = load_yaml(SUBSCRIPTIONS_FILE).get("subscriptions", {})
        return jsonify(subscriptions.get(user_id, []))
    else:
        data = request.get_json()
        all_data = load_yaml(SUBSCRIPTIONS_FILE)
        if "subscriptions" not in all_data:
            all_data["subscriptions"] = {}
        all_data["subscriptions"][user_id] = data
        save_yaml(SUBSCRIPTIONS_FILE, all_data)
        return jsonify({"status": "success"})

@app.route('/users/all', methods=['GET'])
@token_only
def get_all_users():
    users_list = load_yaml(USERS_FILE).get("users", [])
    return jsonify(users_list)

@app.route('/users/<user_id>', methods=['GET', 'POST'])
@token_only
def handle_users(user_id):
    if request.method == 'GET':
        users_list = load_yaml(USERS_FILE).get("users", [])
        user_info = next((u for u in users_list if u["id"] == user_id), {})
        return jsonify(user_info)
    else:
        data = request.get_json()
        all_data = load_yaml(USERS_FILE)
        users_list = all_data.get("users", [])
        found = False
        for i, u in enumerate(users_list):
            if u["id"] == user_id:
                users_list[i] = data
                found = True
                break
        if not found:
            users_list.append(data)
        all_data["users"] = users_list
        save_yaml(USERS_FILE, all_data)
        return jsonify({"status": "success"})

from utils.security import SecurityChecker

async def safe_send(channel, content):
    """실제 메시지 전송을 수행하는 비동기 래퍼 (예외 처리 포함)"""
    try:
        await channel.send(content)
    except Exception as e:
        print(f"[API] Failed to send message in background: {e}")

@app.route('/send', methods=['POST'])
@local_send_only
def send_message_api():
    """외부 스크립트에서 메시지 전송을 요청하는 API (보안 필터링 및 안정성 강화)"""
    global discord_client
    try:
        data = request.get_json()
        channel_id = data.get('channel_id', CHAT_CHANNEL_ID)
        raw_content = data.get('content', '')
        
        # 보안 필터링: 민감 정보 마스킹
        content = SecurityChecker.filter_sensitive_data(raw_content)
        
        print(f"[API] Received send request for channel {channel_id}")
        
        if not discord_client:
            print("[API] Error: discord_client is None")
            return jsonify({"status": "failed", "reason": "client_not_ready"}), 400
            
        if discord_client.is_closed() or not discord_client.is_ready():
            print("[API] Error: discord_client is closed or not ready")
            return jsonify({"status": "failed", "reason": "connection_not_active"}), 503

        if not content:
            print("[API] Error: content is empty")
            return jsonify({"status": "failed", "reason": "empty_content"}), 400
            
        channel = discord_client.get_channel(int(channel_id))
        if channel:
            # 외부 쓰레드(Flask)에서 디스코드 메인 루프로 작업 안전하게 전달
            asyncio.run_coroutine_threadsafe(safe_send(channel, content), discord_client.loop)
            return jsonify({"status": "success"}), 200
        else:
            print(f"[API] Error: channel {channel_id} not found in cache")
            return jsonify({"status": "failed", "reason": "channel_not_found"}), 400
            
    except Exception as e:
        print(f"[API] Critical Error: {e}")
        return jsonify({"status": "failed", "reason": str(e)}), 500

def run_flask(client):
    global discord_client
    discord_client = client
    # threaded=True를 명시하여 동시 요청 처리 능력 향상
    # (ADR-0011) 127.0.0.1 에만 바인딩한다. 외부 접근은 cloudflared 터널(127.0.0.1:5000 으로 접속)로만 한다.
    app.run(host='127.0.0.1', port=5000, threaded=True)

if __name__ == '__main__':
    # Standalone execution for testing purposes
    app.run(host='127.0.0.1', port=5000, debug=True)
