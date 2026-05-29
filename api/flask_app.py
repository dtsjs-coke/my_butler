import os
import asyncio
from flask import Flask, render_template, request, jsonify
from core.news_service import load_news
from core.subscription_service import load_yaml, save_yaml, SUBSCRIPTIONS_FILE, USERS_FILE
from utils.system_status import get_system_status_data
from config.config_manager import load_keywords, load_queue, load_ktx_queue

app = Flask(__name__)
discord_client = None
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))

@app.route('/')
def home():
    return render_template('index.html')

from datetime import datetime, timedelta

@app.route('/news')
def news_page():
    news = load_news()
    news.reverse()
    
    categorized_news = {}
    for n in news:
        kw = n.get('keyword', '기타')
        if kw not in categorized_news:
            categorized_news[kw] = []
        categorized_news[kw].append(n)
        
    return render_template('news.html', categorized_news=categorized_news, now=datetime.now())

@app.route('/api/system_status')
def api_status():
    data = get_system_status_data()
    return jsonify(data)

@app.route('/api/graph_data')
def api_graph_data():
    keywords = load_keywords()
    srt_queue = load_queue()
    ktx_queue = load_ktx_queue()
    
    nodes = [
        {"id": "root", "label": "Butler Pro", "color": "#3b82f6", "size": 25},
        {"id": "news_root", "label": "News", "color": "#10b981"},
        {"id": "train_root", "label": "Trains", "color": "#f59e0b"}
    ]
    edges = [
        {"from": "root", "to": "news_root"},
        {"from": "root", "to": "train_root"}
    ]
    
    # Add Keywords
    for i, kw in enumerate(keywords):
        node_id = f"kw_{i}"
        nodes.append({"id": node_id, "label": kw, "color": "#6ee7b7", "size": 12})
        edges.append({"from": "news_root", "to": node_id})
        
    # Add SRT/KTX Tasks
    task_count = 0
    for user_id, tasks in srt_queue.items():
        for task in tasks:
            node_id = f"task_{task_count}"
            label = f"SRT: {task.get('dep')}→{task.get('arr')}"
            nodes.append({"id": node_id, "label": label, "color": "#fcd34d", "size": 12})
            edges.append({"from": "train_root", "to": node_id})
            task_count += 1
            
    for user_id, tasks in ktx_queue.items():
        for task in tasks:
            node_id = f"task_{task_count}"
            label = f"KTX: {task.get('dep')}→{task.get('arr')}"
            nodes.append({"id": node_id, "label": label, "color": "#fbbf24", "size": 12})
            edges.append({"from": "train_root", "to": node_id})
            task_count += 1
            
    return jsonify({"nodes": nodes, "edges": edges})

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
