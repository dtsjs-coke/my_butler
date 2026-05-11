import os
import asyncio
from flask import Flask, render_template_string, request
from core.news_service import load_news

app = Flask(__name__)
discord_client = None
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))

@app.route('/')
def home():
    news = load_news()
    news.reverse()
    return render_template_string("""
        <h1>📱 버틀러 Pro 뉴스룸</h1>
        <p>S9 서버 운영 중 (7일 보관)</p><hr>
        {% for n in news %}<p>[{{n.date}}] <b>{{n.keyword}}</b>: <a href="{{n.link}}">{{n.title}}</a></p>{% endfor %}
    """, news=news)

@app.route('/send', methods=['POST'])
async def send_message_api():
    """외부 스크립트에서 메시지 전송을 요청하는 API"""
    global discord_client
    data = await asyncio.to_thread(request.get_json)
    channel_id = data.get('channel_id', CHAT_CHANNEL_ID)
    content = data.get('content', '')
    
    if discord_client:
        channel = discord_client.get_channel(int(channel_id))
        if channel and content:
            await channel.send(content)
            return {"status": "success"}, 200
    return {"status": "failed"}, 400

def run_flask(client):
    global discord_client
    discord_client = client
    app.run(host='0.0.0.0', port=5000)
