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
    # 최근 3일 기사만 보유
    limit = datetime.now() - timedelta(days=3)
    filtered = []
    
    for n in news_list:
        try:
            # 신규 스키마(fetch_date) 또는 구형 스키마(date) 호환 지원
            date_str = n.get('fetch_date') or n.get('date')
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
    
    # 중복 체크를 위한 Set 생성 (제목, 네이버링크, 오리지널링크)
    seen_titles = set()
    seen_naver_urls = set()
    seen_origin_urls = set()
    
    for n in stored:
        if n.get('title'): seen_titles.add(n['title'])
        
        naver = n.get('naver_link') or n.get('link')
        if naver: seen_naver_urls.add(normalize_url(naver))
            
        origin = n.get('original_link') or n.get('originallink')
        if origin: seen_origin_urls.add(normalize_url(origin))

    new_found = False

    for kw in keywords:
        items = await fetch_news(kw)
        for item in items:
            title = clean_html(item.get('title', ''))
            naver_link = item.get('link', '')
            original_link = item.get('originallink', '')
            
            # 발행일자 처리 (Naver Format: Thu, 28 May 2026 09:00:00 +0900)
            raw_pub = item.get('pubDate', '')
            try:
                parsed_pub = datetime.strptime(raw_pub, "%a, %d %b %Y %H:%M:%S %z")
                pub_date = parsed_pub.strftime("%Y-%m-%d %H:%M:%S")
            except:
                pub_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            norm_naver = normalize_url(naver_link)
            norm_origin = normalize_url(original_link)
            
            # 중복 체크: 셋 중 하나라도 일치하면 이미 처리한 기사로 간주
            is_title_seen = title and title in seen_titles
            is_naver_seen = norm_naver and norm_naver in seen_naver_urls
            is_origin_seen = norm_origin and norm_origin in seen_origin_urls
            
            is_duplicate = is_title_seen or is_naver_seen or is_origin_seen
            
            # 세 가지 조건이 완전히 똑같으면 DB 용량을 위해 추가 기록 생략
            is_exact_match = is_title_seen and (is_naver_seen or not norm_naver) and (is_origin_seen or not norm_origin)
            
            if not is_exact_match:
                new_item = {
                    "fetch_date": datetime.now().strftime('%Y-%m-%d'), # 보관 기한 산정용
                    "pub_date": pub_date,
                    "naver_link": naver_link,
                    "original_link": original_link,
                    "title": title,
                    "is_sent": 1 if is_duplicate else 0, # 중복이면 메일발송여부(is_sent)=1 로 기록
                    "keyword": kw
                }
                
                # 완전 새로운 기사인 경우 발송
                if not is_duplicate:
                    await channel.send(f"📰 **새 뉴스 ({kw})**\n{title}\n<{naver_link}>")
                    new_item["is_sent"] = 1 # 발송 완료 기록
                
                stored.append(new_item)
                seen_titles.add(title)
                if norm_naver: seen_naver_urls.add(norm_naver)
                if norm_origin: seen_origin_urls.add(norm_origin)
                
                new_found = True
                save_news(stored) # 즉시 저장

    if new_found:
        print(f"[{datetime.now()}] New news items processed and saved.")
