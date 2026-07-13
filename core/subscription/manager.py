import os
from discord.ext import tasks
from core.subscription.service import SubscriptionService

STREAMLIT_URL = os.getenv("STREAMLIT_URL")

class SubscriptionManager:
    def __init__(self, client):
        self.client = client
        self.service = SubscriptionService(discord_client=client)

    @tasks.loop(hours=6)
    async def notification_loop(self):
        """6시간마다 구독 만료 체크 및 알림"""
        await self.client.wait_until_ready()
        print("🔍 Checking subscription notifications...")
        await self.service.send_notifications_async()

def start_subscription_tasks(client):
    manager = SubscriptionManager(client)
    if not manager.notification_loop.is_running():
        manager.notification_loop.start()
    return manager
