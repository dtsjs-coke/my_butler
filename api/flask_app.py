import os
import asyncio
from flask import Flask, render_template, request, jsonify
from core.news_service import load_news
from core.subscription_service import load_yaml, save_yaml, SUBSCRIPTIONS_FILE, USERS_FILE

app = Flask(__name__)
discord_client = None
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/news')
def news_page():
    news = load_news()
    news.reverse()
    return render_template('news.html', news=news)

@app.route('/subscriptions/<user_id>', methods=['GET', 'POST'])
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

@app.route('/users/<user_id>', methods=['GET', 'POST'])
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

@app.route('/send', methods=['POST'])
def send_message_api():
    """외부 스크립트에서 메시지 전송을 요청하는 API (보안 필터링 적용)"""
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
            
        if not content:
            print("[API] Error: content is empty")
            return jsonify({"status": "failed", "reason": "empty_content"}), 400
            
        channel = discord_client.get_channel(int(channel_id))
        if channel:
            # 외부 쓰레드(Flask)에서 디스코드 메인 루프로 작업 전달
            discord_client.loop.create_task(channel.send(content))
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
    app.run(host='0.0.0.0', port=5000)
