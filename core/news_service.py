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
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_FILE = os.path.join(BASE_DIR, "data", "news.json")
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))

def extract_publisher(url):
    """URL에서 언론사 이름을 정밀하게 추출합니다."""
    if not url: return "언론사"
    try:
        if 'n.news.naver.com' in url or 'news.naver.com' in url:
            return "NAVER"
            
        parsed_uri = urlparse(url)
        domain = parsed_uri.netloc.lower()
        if not domain: return "언론사"
        
        # 불필요한 서브도메인 제거
        domain = re.sub(r'^(www\.|news\.|mnews\.|m\.|app\.|blog\.|v\.|n\.)', '', domain)
        
        parts = domain.split('.')
        if len(parts) >= 2:
            # co.kr, or.kr, kyonggi.co.kr 등 복합 도메인 처리
            if parts[-2] in ['co', 'or', 'go', 'ne', 're', 'ac'] and len(parts) >= 3:
                name = parts[-3]
            else:
                name = parts[-2]
            return name.upper()
        return domain.upper()
    except:
        return "언론사"

def load_news():
    if not os.path.exists(NEWS_FILE): return []
    try:
        with open(NEWS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            news_list = data if isinstance(data, list) else []
            
            # 자동 마이그레이션: 누락된 언론사 정보 채우기
            updated = False
            for item in news_list:
                if 'publisher' not in item or item['publisher'] in ["언론사", "NEWS"]:
                    link = item.get('original_link') or item.get('link') or item.get('naver_link')
                    item['publisher'] = extract_publisher(link)
                    updated = True
                if 'link' not in item:
                    item['link'] = item.get('original_link') or item.get('naver_link')
                    updated = True
            
            if updated:
                # Flask 등에서 읽을 때는 저장을 지양하고, news_loop에서만 저장하도록 함 (성능)
                pass 
            return news_list
    except Exception as e:
        print(f"Error loading news: {e}")
        return []

def save_news(news_list):
    # 최근 3일 기사만 보유 (성능 최적화를 위해 기간 단축)
    limit = datetime.now() - timedelta(days=3)
    filtered = [n for n in news_list if (n.get('fetch_date') or n.get('date', '')[:10]) and datetime.strptime(n.get('fetch_date') or n.get('date', '')[:10], '%Y-%m-%d') > limit]
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

def clean_html(text):
    if not text: return ""
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def normalize_url(url):
    if not url: return ""
    return url.replace("https://", "").replace("http://", "").rstrip("/")

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

    # 루프가 끝날 때 혹은 새 뉴스가 있을 때 저장
    if new_found:
        save_news(stored)
    else:
        # 기존 데이터 마이그레이션 결과 반영을 위해 새 뉴스가 없더라도 
        # 로직상 publisher가 업데이트된 경우를 위해 강제 저장 (초기 1회용)
        save_news(stored)
