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
from api.vwap_api import vwap_bp
app.register_blueprint(vwap_bp, url_prefix='/vwap')
discord_client = None
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))
BUTLER_API_TOKEN = os.getenv("BUTLER_API_TOKEN", "butler_v3_secret_2026")

from functools import wraps

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Butler-Token')
        if not token or token != BUTLER_API_TOKEN:
            # 브라우저 직접 접근 시 또는 토큰 누락 시
            return jsonify({"status": "failed", "reason": "unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def home():
    return render_template('index.html', api_token=BUTLER_API_TOKEN)

@app.route('/trains')
def trains_page():
    stations = load_stations()
    return render_template('trains.html', stations=stations, api_token=BUTLER_API_TOKEN)

@app.route('/api/srt/queue', methods=['GET', 'DELETE'])
@token_required
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
@token_required
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

    return render_template('news.html', categorized_news=categorized_news, now=datetime.now(), api_token=BUTLER_API_TOKEN)

@app.route('/api/keyword_groups', methods=['GET', 'POST', 'DELETE'])
@token_required
def manage_keyword_groups():
    group_file = os.path.join(PROJECT_ROOT, "data", "keyword_groups.json")
    
    # helper to load groups safely
    def load_groups():
        if not os.path.exists(group_file) or os.path.getsize(group_file) == 0:
            return {}
        try:
            with open(group_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}

    if request.method == 'GET':
        return jsonify({"status": "success", "groups": load_groups()})

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
@token_required
def api_status():
    start_time = time.time()
    data = get_system_status_data()
    data["history"] = get_system_status_history()
    elapsed = (time.time() - start_time) * 1000
    print(f"[API] system_status request took {elapsed:.2f}ms")
    return jsonify(data)

from config.config_manager import save_keywords

@app.route('/api/keywords', methods=['GET', 'POST', 'DELETE'])
@token_required
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
    return render_template('settlement.html', api_token=BUTLER_API_TOKEN)

def cleanup_old_settlements():
    settlement_file = os.path.join(PROJECT_ROOT, "data", "settlements.json")
    if not os.path.exists(settlement_file):
        return []
    try:
        with open(settlement_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            settlements = data.get("settlements", [])
    except:
        return []

    now = datetime.now()
    valid_settlements = []
    changed = False
    for s in settlements:
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
            os.makedirs(os.path.dirname(settlement_file), exist_ok=True)
            with open(settlement_file, "w", encoding="utf-8") as f:
                json.dump({"settlements": valid_settlements}, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[Settlement Cleanup Error] {e}")
            
    return valid_settlements

@app.route('/api/settlements', methods=['GET', 'POST', 'DELETE'])
@token_required
def manage_settlements():
    settlement_file = os.path.join(PROJECT_ROOT, "data", "settlements.json")
    
    if request.method == 'GET':
        valid_list = cleanup_old_settlements()
        return jsonify({"status": "success", "settlements": valid_list})
        
    elif request.method == 'POST':
        data = request.get_json()
        s_id = data.get('id')
        title = data.get('title', '새 정산')
        participants = data.get('participants', [])
        items = data.get('items', [])
        
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        valid_list = cleanup_old_settlements()
        
        if s_id:
            found = False
            for s in valid_list:
                if s['id'] == s_id:
                    s['title'] = title
                    s['participants'] = participants
                    s['items'] = items
                    s['created_at'] = now_str  # Update timestamp to refresh retention duration
                    found = True
                    break
            if not found:
                s_id = str(int(time.time() * 1000))
                valid_list.append({
                    "id": s_id,
                    "title": title,
                    "participants": participants,
                    "items": items,
                    "created_at": now_str
                })
        else:
            s_id = str(int(time.time() * 1000))
            valid_list.append({
                "id": s_id,
                "title": title,
                "participants": participants,
                "items": items,
                "created_at": now_str
            })
            
        try:
            os.makedirs(os.path.dirname(settlement_file), exist_ok=True)
            with open(settlement_file, "w", encoding="utf-8") as f:
                json.dump({"settlements": valid_list}, f, ensure_ascii=False, indent=4)
            return jsonify({"status": "success", "id": s_id}), 200
        except Exception as e:
            return jsonify({"status": "failed", "reason": str(e)}), 500
            
    elif request.method == 'DELETE':
        data = request.get_json()
        s_id = data.get('id')
        if not s_id:
            return jsonify({"status": "failed", "reason": "missing_id"}), 400
            
        valid_list = cleanup_old_settlements()
        new_list = [s for s in valid_list if s['id'] != s_id]
        
        try:
            os.makedirs(os.path.dirname(settlement_file), exist_ok=True)
            with open(settlement_file, "w", encoding="utf-8") as f:
                json.dump({"settlements": new_list}, f, ensure_ascii=False, indent=4)
            return jsonify({"status": "success"}), 200
        except Exception as e:
            return jsonify({"status": "failed", "reason": str(e)}), 500

@app.route('/subscriptions/all', methods=['GET'])
@token_required
def get_all_subscriptions():
    data = load_yaml(SUBSCRIPTIONS_FILE).get("subscriptions", {})
    return jsonify(data)

@app.route('/subscriptions/<user_id>', methods=['GET', 'POST'])
@token_required
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
@token_required
def get_all_users():
    users_list = load_yaml(USERS_FILE).get("users", [])
    return jsonify(users_list)

@app.route('/users/<user_id>', methods=['GET', 'POST'])
@token_required
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
@token_required
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
    app.run(host='0.0.0.0', port=5000, threaded=True)

if __name__ == '__main__':
    # Standalone execution for testing purposes
    app.run(host='127.0.0.1', port=5000, debug=True)
