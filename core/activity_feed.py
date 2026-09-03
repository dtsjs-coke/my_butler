import os
import json
import threading
from datetime import datetime

# butler(Flask+Discord)와 agent(자가치유)가 서로 다른 pm2 프로세스로 동작하므로
# 인메모리 캐시가 아니라, 다른 flat JSON 파일들과 동일한 방식으로 파일에 공유 저장한다.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEED_FILE = os.path.join(BASE_DIR, "data", "activity_feed.json")
MAX_EVENTS = 200

_file_lock = threading.Lock()


def _load():
    if not os.path.exists(FEED_FILE):
        return []
    try:
        with open(FEED_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(events):
    temp_file = FEED_FILE + ".tmp"
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(events[-MAX_EVENTS:], f, ensure_ascii=False, indent=2)
        if os.path.exists(FEED_FILE):
            os.remove(FEED_FILE)
        os.rename(temp_file, FEED_FILE)
    except Exception as e:
        print(f"Error saving activity feed: {e}")


def log_event(category, message, level="info", meta=None):
    """활동 피드에 이벤트를 하나 기록한다. category 예: news/system/agent/vwap."""
    event = {
        "ts": datetime.now().isoformat(),
        "category": category,
        "message": message,
        "level": level,
        "meta": meta or {},
    }
    with _file_lock:
        events = _load()
        events.append(event)
        _save(events)
    return event


def get_recent_events(limit=50, since_ts=None):
    """최근 이벤트를 최신순으로 반환. since_ts가 주어지면 그 이후 이벤트만 반환."""
    with _file_lock:
        events = _load()
    if since_ts:
        events = [e for e in events if e["ts"] > since_ts]
    return list(reversed(events))[:limit]
