import os
import json
import aiohttp
from datetime import datetime, timedelta
from discord.ext import tasks
from config.config_manager import load_keywords

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
NEWS_FILE = "/data/data/com.termux/files/home/dev_pjt/my_butler/news.json"
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
    stored = load_news()
    new_found = False
    
    for kw in keywords:
        items = await fetch_news(kw)
        for item in items:
            title = item['title'].replace('<b>','').replace('</b>','').replace('&quot;','"')
            link = item['link']
            if not any(n['title'] == title for n in stored):
                new_item = {"date": datetime.now().strftime('%Y-%m-%d'), "keyword": kw, "title": title, "link": link}
                stored.append(new_item)
                new_found = True
                await channel.send(f"📰 **새 뉴스 ({kw})**\n{title}\n<{link}>")
    
    if new_found: save_news(stored)
