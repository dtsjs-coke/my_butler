import os
import json
import aiohttp
import re
import html
from urllib.parse import urlparse
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
    # 최근 3일 기사만 보유
    limit = datetime.now() - timedelta(days=3)
    filtered = []
    
    for n in news_list:
        try:
            # 신규 스키마(fetch_date) 또는 구형 스키마(date) 호환 지원
            date_str = n.get('fetch_date') or n.get('date', '')[:10]
            if not date_str and n.get('pub_date'):
                date_str = n.get('pub_date')[:10]
                
            if date_str and datetime.strptime(date_str, '%Y-%m-%d') > limit:
                filtered.append(n)
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
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def normalize_url(url):
    """URL 비교를 위해 프로토콜과 트레일링 슬래시 제거"""
    if not url: return ""
    return url.replace("https://", "").replace("http://", "").rstrip("/")

def extract_publisher(url):
    """URL에서 언론사 이름을 추출합니다."""
    if not url: return "언론사"
    try:
        if 'naver.com' in url:
            return "NAVER"
            
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc.lower()
        parts = domain.split('.')
        
        if len(parts) >= 2:
            # co.kr, or.kr 등 처리
            if parts[-2] in ['co', 'or', 'go', 'ne', 're', 'ac'] and len(parts) >= 3:
                name = parts[-3]
            else:
                name = parts[-2]
            
            # 서브도메인이 이름인 경우 처리
            if name in ['www', 'news', 'm', 'mnews', 'sports'] and len(parts) >= 3:
                name = parts[-2]
                
            return name.upper()
        return "언론사"
    except:
        return "언론사"

async def fetch_news(query):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": 15, "sort": "date"}
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
    
    seen_titles = set(n.get('title', '') for n in stored)
    seen_urls = set(normalize_url(n.get('link', '')) for n in stored)

    new_found = False

    for kw in keywords:
        items = await fetch_news(kw)
        for item in items:
            title = clean_html(item.get('title', ''))
            naver_link = item.get('link', '')
            original_link = item.get('originallink', '')
            link = original_link if original_link else naver_link
            
            # 1. 발행일자 처리
            raw_pub = item.get('pubDate', '')
            try:
                parsed_pub = datetime.strptime(raw_pub, "%a, %d %b %Y %H:%M:%S %z")
                pub_date = parsed_pub.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pub_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
            # 2. 언론사 추출
            publisher = extract_publisher(link)
            
            norm_url = normalize_url(link)
            if title not in seen_titles and norm_url not in seen_urls:
                new_item = {
                    "fetch_date": datetime.now().strftime('%Y-%m-%d'),
                    "date": pub_date,
                    "pub_date": pub_date,
                    "title": title,
                    "link": link,
                    "naver_link": naver_link,
                    "original_link": original_link,
                    "publisher": publisher,
                    "keyword": kw
                }
                
                await channel.send(f"📰 **새 뉴스 ({kw})**\n{title}\n<{link}>")
                stored.append(new_item)
                seen_titles.add(title)
                seen_urls.add(norm_url)
                new_found = True

    if new_found:
        save_news(stored)
