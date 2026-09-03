import os
import json
import aiohttp
import re
import html
import hashlib
import difflib
import discord
from urllib.parse import urlparse
from datetime import datetime, timedelta
from discord.ext import tasks
from config.config_manager import load_keywords
from core.activity_feed import log_event

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWS_FILE = os.path.join(BASE_DIR, "data", "news.json")
KEYWORD_GROUPS_FILE = os.path.join(BASE_DIR, "data", "keyword_groups.json")
NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))

# 임베드 색상이 매번 랜덤하게 튀지 않도록, 팔레트 중 하나를 그룹/키워드명 해시로 고정 선택한다
EMBED_COLOR_PALETTE = [
    0x3B82F6,  # blue
    0x10B981,  # green
    0xF59E0B,  # amber
    0xEF4444,  # red
    0x8B5CF6,  # violet
    0xEC4899,  # pink
    0x06B6D4,  # cyan
    0xF97316,  # orange
]

def get_keyword_color(kw):
    """키워드가 속한 그룹(없으면 키워드 자체)을 해시하여 안정적인 임베드 색상을 반환합니다."""
    group_name = kw
    try:
        if os.path.exists(KEYWORD_GROUPS_FILE):
            with open(KEYWORD_GROUPS_FILE, 'r', encoding='utf-8') as f:
                groups = json.load(f)
            for gname, members in groups.items():
                if kw in members:
                    group_name = gname
                    break
    except Exception:
        pass
    idx = int(hashlib.md5(group_name.encode('utf-8')).hexdigest(), 16) % len(EMBED_COLOR_PALETTE)
    return EMBED_COLOR_PALETTE[idx]

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

def normalize_title(title):
    """유사도 비교용으로 제목에서 특수문자/공백 편차를 제거합니다."""
    t = title.lower()
    t = re.sub(r'[^\w\s가-힣]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def cluster_titles(candidates, threshold=0.72):
    """제목이 비슷한(같은 이슈를 다룬) 항목들을 하나의 클러스터로 묶는다.
    candidates: 'title' 키를 가진 dict 리스트. 반환값: dict 리스트의 리스트."""
    clusters = []
    for cand in candidates:
        norm = normalize_title(cand['title'])
        for cluster in clusters:
            rep_norm = normalize_title(cluster[0]['title'])
            if difflib.SequenceMatcher(None, norm, rep_norm).ratio() >= threshold:
                cluster.append(cand)
                break
        else:
            clusters.append([cand])
    return clusters

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
        new_candidates = []
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
                parsed_pub = datetime.now()
                pub_date = parsed_pub.strftime("%Y-%m-%d %H:%M:%S")

            # 2. 언론사 추출
            publisher = extract_publisher(link)

            norm_url = normalize_url(link)
            if title not in seen_titles and norm_url not in seen_urls:
                new_candidates.append({
                    "fetch_date": datetime.now().strftime('%Y-%m-%d'),
                    "date": pub_date,
                    "pub_date": pub_date,
                    "title": title,
                    "link": link,
                    "naver_link": naver_link,
                    "original_link": original_link,
                    "publisher": publisher,
                    "keyword": kw,
                    "_parsed_pub": parsed_pub,
                })
                seen_titles.add(title)
                seen_urls.add(norm_url)

        if new_candidates:
            new_found = True
            # 같은 이슈를 다룬 기사는 클러스터로 묶어 임베드 1개로 전송 (매체 스팸 방지)
            for cluster in cluster_titles(new_candidates):
                lead = cluster[0]
                embed = discord.Embed(
                    title=lead["title"], url=lead["link"],
                    color=get_keyword_color(kw), timestamp=lead["_parsed_pub"]
                )
                embed.set_author(name=f"📰 새 뉴스 · {kw}")
                embed.set_footer(text=lead["publisher"])
                if len(cluster) > 1:
                    publishers = list(dict.fromkeys(c["publisher"] for c in cluster))
                    embed.add_field(name=f"{len(cluster)}개 매체 보도", value=", ".join(publishers), inline=False)
                await channel.send(embed=embed)
                log_event("news", f"[{kw}] {lead['title']}")

            for cand in new_candidates:
                cand.pop("_parsed_pub", None)
            stored.extend(new_candidates)

    # 루프가 끝날 때 혹은 새 뉴스가 있을 때 저장
    if new_found:
        save_news(stored)
    else:
        # 기존 데이터 마이그레이션 결과 반영을 위해 새 뉴스가 없더라도 
        # 로직상 publisher가 업데이트된 경우를 위해 강제 저장 (초기 1회용)
        save_news(stored)
