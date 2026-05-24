import os
import yaml
import requests
import asyncio
import aiohttp
from datetime import datetime, date

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "subscriptions.yaml")
USERS_FILE = os.path.join(DATA_DIR, "users.yaml")

def load_yaml(file_path):
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def save_yaml(file_path, data):
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)

class SubscriptionService:
    def __init__(self, telegram_token=None, discord_client=None, chat_channel_id=None):
        self.telegram_token = telegram_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.discord_client = discord_client
        self.chat_channel_id = chat_channel_id or int(os.getenv("CHAT_CHANNEL_ID", 0))

    def check_notifications(self):
        """구독 만료 알림 체크 (Telegram & Discord)"""
        users_data = load_yaml(USERS_FILE).get("users", [])
        subscriptions_data = load_yaml(SUBSCRIPTIONS_FILE).get("subscriptions", {})
        today = date.today()
        
        updated = False
        notifications = []

        # 유저 ID별 텔레그램 ID 매핑
        user_telegram_map = {u["id"]: u.get("telegram_chat_id") for u in users_data}

        for user_id, items in subscriptions_data.items():
            telegram_chat_id = user_telegram_map.get(user_id)
            
            for item in items:
                end_date_str = item.get("end_date")
                if not end_date_str or end_date_str == '9999-12-31':
                    continue
                
                try:
                    end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                
                days_left = (end_date - today).days
                notify_sent = item.get("notify_sent", [])
                
                # 알림 임계값: 30, 7, 1, 0일 전
                thresholds = [30, 7, 1, 0]
                for t in thresholds:
                    # 데이터에 '0d' 처럼 문자열로 저장되어 있을 수도 있으니 유연하게 처리
                    t_str = f"{t}d"
                    if days_left == t and t not in notify_sent and t_str not in notify_sent:
                        msg = self._format_message(item, days_left)
                        notifications.append({
                            "telegram_chat_id": telegram_chat_id,
                            "message": msg
                        })
                        notify_sent.append(t)
                        item["notify_sent"] = notify_sent
                        updated = True

        if updated:
            save_yaml(SUBSCRIPTIONS_FILE, {"subscriptions": subscriptions_data})
            
        return notifications

    def _format_message(self, item, days_left):
        name = item.get("name", "알 수 없는 서비스")
        price = item.get("price", 0)
        end_date = item.get("end_date", "")
        
        if days_left == 0:
            return f"🚨 [구독 만료] 오늘은 '{name}' 구독이 만료되는 날입니다!\n📅 만료일: {end_date}\n💰 금액: {price}원"
        else:
            return f"⏰ [구독 알림] '{name}' 구독 만료까지 {days_left}일 남았습니다.\n📅 만료일: {end_date}\n💰 금액: {price}원"

    async def send_notifications_async(self):
        notifications = self.check_notifications()
        for note in notifications:
            # 텔레그램 전송
            if self.telegram_token and note["telegram_chat_id"]:
                self._send_telegram(note["telegram_chat_id"], note["message"])
            
            # 디스코드 전송
            if self.discord_client and self.chat_channel_id:
                channel = self.discord_client.get_channel(self.chat_channel_id)
                if channel:
                    await channel.send(note["message"])

    def _send_telegram(self, chat_id, message):
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        try:
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"Telegram send failed: {e}")

    async def ping_streamlit(self, url):
        """Streamlit 앱을 깨우기 위한 Ping (aiohttp 사용)"""
        if not url:
            return
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(url, timeout=15) as resp:
                    print(f"Ping sent to {url}, Status: {resp.status}")
        except Exception as e:
            print(f"Ping failed: {e}")
