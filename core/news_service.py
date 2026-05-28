import os
import json
import aiohttp
import re
import html
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
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error loading news: {e}")
        return []

def save_news(news_list):
    # 최근 30일 이내의 뉴스만 유지 (중복 방지 기간 확대)
    limit = datetime.now() - timedelta(days=30)
    filtered = []
    seen_links = set()
    
    for n in news_list:
        try:
            # 중복 제거 (리스트 자체에 중복이 있을 경우 대비)
            link = n.get('link')
            if link and link in seen_links: continue
            
            if datetime.strptime(n['date'], '%Y-%m-%d') > limit:
                filtered.append(n)
                if link: seen_links.add(link)
        except:
            continue
    
    # 원자적 저장을 위해 임시 파일 사용 후 교체
    temp_file = NEWS_FILE + ".tmp"
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(filtered, f, ensure_ascii=False, indent=4)
        
        if os.path.exists(temp_file):
            if os.path.exists(NEWS_FILE):
                os.remove(NEWS_FILE)
            os.rename(temp_file, NEWS_FILE)
    except Exception as e:
        print(f"Error saving news: {e}")
        if os.path.exists(temp_file):
            try: os.remove(temp_file)
            except: pass

def clean_html(text):
    """HTML 태그 및 엔티티 제거, 줄바꿈 정리"""
    if not text: return ""
    # HTML 엔티티 복원 (&#34; -> ", &quot; -> " 등)
    text = html.unescape(text)
    # 태그 제거
    text = re.sub(r'<[^>]+>', '', text)
    # 줄바꿈 및 연속된 공백 정리
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def normalize_url(url):
    """URL 비교를 위해 프로토콜과 트레일링 슬래시 제거"""
    if not url: return ""
    return url.replace("https://", "").replace("http://", "").rstrip("/")

async def fetch_news(query):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": 10, "sort": "date"} # 검색 결과 수 약간 확대
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
    if not channel: 
        print(f"News channel {NEWS_CHANNEL_ID} not found.")
        return

    keywords = load_keywords()
    stored = load_news()
    
    # 빠른 중복 체크를 위한 URL Set 생성
    seen_urls = set()
    for n in stored:
        if n.get('link'): seen_urls.add(normalize_url(n['link']))
        if n.get('originallink'): seen_urls.add(normalize_url(n['originallink']))

    new_found = False

    for kw in keywords:
        items = await fetch_news(kw)
        for item in items:
            # 제목 정제 (태그/엔티티/줄바꿈 제거)
            title = clean_html(item.get('title', ''))
            link = item.get('link', '')
            originallink = item.get('originallink', '')
            
            # 중복 체크: link와 originallink 모두 확인
            norm_link = normalize_url(link)
            norm_origin = normalize_url(originallink)
            
            if norm_link not in seen_urls and (not norm_origin or norm_origin not in seen_urls):
                new_item = {
                    "date": datetime.now().strftime('%Y-%m-%d'), 
                    "keyword": kw, 
                    "title": title, 
                    "link": link,
                    "originallink": originallink
                }
                stored.append(new_item)
                if norm_link: seen_urls.add(norm_link)
                if norm_origin: seen_urls.add(norm_origin)
                
                new_found = True
                await channel.send(f"📰 **새 뉴스 ({kw})**\n{title}\n<{link}>")
                # 새 뉴스 발송 즉시 저장
                save_news(stored)

    if new_found:
        print(f"[{datetime.now()}] New news items sent and saved.")
