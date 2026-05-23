import os
import json
import aiohttp
from datetime import datetime, timedelta
from discord.ext import tasks
from config.config_manager import load_keywords

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
# 프로젝트 루트 경로를 기준으로 파일 경로 설정
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_FILE = os.path.join(BASE_DIR, "news.json")
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))

def load_news():
    if not os.path.exists(NEWS_FILE): return []
    try:
        with open(NEWS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except: return []

def save_news(news_list):
    limit = datetime.now() - timedelta(days=7)
    filtered = [n for n in news_list if datetime.strptime(n['date'], '%Y-%m-%d') > limit]
    with open(NEWS_FILE, 'w', encoding='utf-8') as f:
        json.dump(filtered, f, ensure_ascii=False, indent=4)

async def fetch_news(query):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": 5, "sort": "date"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get('items', [])
            return []

@tasks.loop(minutes=30)
async def news_loop(client):
    await client.wait_until_ready()
    channel = client.get_channel(NEWS_CHANNEL_ID)
    if not channel: return

    keywords = load_keywords()
    keywords = load_keywords()
    # 루프 시작 시 항상 최신 정보를 파일에서 로드
    stored = load_news()
    new_found = False

    for kw in keywords:
        items = await fetch_news(kw)
        for item in items:
            # HTML 엔티티 및 태그 제거
            title = item['title'].replace('<b>','').replace('</b>','').replace('&quot;','"').replace('&apos;',"'").replace('&amp;','&').replace('&lt;','<').replace('&gt;','>')
            link = item['link']
            # 중복 체크: 링크(link)를 기준으로 비교
            if not any(n['link'] == link for n in stored):
                new_item = {"date": datetime.now().strftime('%Y-%m-%d'), "keyword": kw, "title": title, "link": link}
                stored.append(new_item)
                new_found = True
                await channel.send(f"📰 **새 뉴스 ({kw})**\n{title}\n<{link}>")
                # 새 뉴스 발송 즉시 저장하여 재시작 시 중복 방지
                save_news(stored)

    # 루프 종료 시에도 필터링된 상태로 한 번 더 저장 (유지보수용)
    if new_found:
        limit = datetime.now() - timedelta(days=7)
        stored = [n for n in stored if datetime.strptime(n['date'], '%Y-%m-%d') > limit]
        save_news(stored)
