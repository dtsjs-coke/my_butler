import os
import asyncio
import discord
from discord.ext import tasks
from korail2 import Korail
from core.ktx_service import ktx_reservation_queue, save_ktx_queue, KTXStationView

# KTX도 SRT와 동일한 채널을 사용하거나, 별도 설정이 없으므로 SRT_CHANNEL_ID 사용
SRT_CHANNEL_ID = int(os.getenv("SRT_CHANNEL_ID", 0))

@tasks.loop(seconds=10)
async def ktx_reservation_loop(client):
    await client.wait_until_ready()

    current_users = list(ktx_reservation_queue.keys())

    for user_id in current_users:
        if user_id not in ktx_reservation_queue: continue
        
        user_tasks = ktx_reservation_queue[user_id]
        tasks_to_remove = []

        for i, task in enumerate(user_tasks):
            if task.get('status') == "시도중":
                try:
                    # KORAIL_ID, KORAIL_PW 사용 (없으면 SRT_ID, SRT_PW를 대안으로 시도하거나 에러 처리)
                    korail_id = os.getenv("KORAIL_ID") or os.getenv("SRT_ID")
                    korail_pw = os.getenv("KORAIL_PW") or os.getenv("SRT_PW")
                    
                    korail = await asyncio.to_thread(Korail, korail_id, korail_pw)
                    
                    # KTX 열차 검색
                    trains = await asyncio.to_thread(
                        korail.search_train,
                        dep=task['dep'], 
                        arr=task['arr'], 
                        date=task['date'], 
                        time=task['time'], 
                        passengers=task['passengers'],
                        include_no_seats=False
                    )

                    if not trains:
                        continue

                    # 첫 번째 가능한 열차 시도
                    target_train = trains[0]
                    
                    # korail2의 reserve는 좌석이 없으면 에러를 내거나 실패할 수 있음
                    reservation = await asyncio.to_thread(
                        korail.reserve,
                        target_train, 
                        passengers=task['passengers'],
                        option=task['seat_type']
                    )

                    if reservation:
                        tasks_to_remove.append(i)
                        
                        channel = client.get_channel(SRT_CHANNEL_ID)
                        if channel:
                            await channel.send(f"🔔 <@{user_id}>님! **KTX 예약 성공**\n{reservation}")
                    
                except Exception as e:
                    # 매진 등의 일반적인 에러는 무시하고 다음 루프에서 재시도
                    if "매진" not in str(e) and "잔여석" not in str(e):
                        print(f"KTX 예약 오류 (User {user_id}): {e}")

        if tasks_to_remove:
            for index in sorted(tasks_to_remove, reverse=True):
                if user_id in ktx_reservation_queue and index < len(ktx_reservation_queue[user_id]):
                    ktx_reservation_queue[user_id].pop(index)
            
            if user_id in ktx_reservation_queue and not ktx_reservation_queue[user_id]:
                del ktx_reservation_queue[user_id]
                
            save_ktx_queue(ktx_reservation_queue)

class KTXMainMenuView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    @discord.ui.button(label="KTX 예약", style=discord.ButtonStyle.primary, emoji="🎫")
    async def reserve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🚆 KTX 예약 단계를 시작합니다.", view=KTXStationView(), ephemeral=True)

    @discord.ui.button(label="현재 Queue 확인", style=discord.ButtonStyle.secondary, emoji="⏳")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_tasks = ktx_reservation_queue.get(self.user_id, [])
        if not user_tasks:
            return await interaction.response.send_message("현재 진행 중인 예약 시도 작업이 없습니다.", ephemeral=True)
        
        embed = discord.Embed(
            title="⏳ KTX 실시간 예약 시도 현황", 
            description=f"버틀러가 현재 {len(user_tasks)}개의 조건으로 KTX 좌석을 찾고 있습니다.",
            color=discord.Color.blue()
        )
        
        view = discord.ui.View()
        
        for i, data in enumerate(user_tasks):
            embed.add_field(
                name=f"작업 #{i+1}", 
                value=f"🛣️ {data['dep']} ➡️ {data['arr']}\n📅 {data['date']} {data['time']}\n📡 {data.get('status', '대기 중')}", 
                inline=False
            )
            
            stop_btn = discord.ui.Button(label=f"#{i+1} 삭제", style=discord.ButtonStyle.danger)
            
            def make_callback(index):
                async def callback(it: discord.Interaction):
                    if self.user_id in ktx_reservation_queue and index < len(ktx_reservation_queue[self.user_id]):
                        removed = ktx_reservation_queue[self.user_id].pop(index)
                        if not ktx_reservation_queue[self.user_id]:
                            del ktx_reservation_queue[self.user_id]
                        save_ktx_queue(ktx_reservation_queue)
                        await it.response.send_message(f"🛑 KTX 작업 #{index+1} ({removed['dep']}➡️{removed['arr']}) 이 삭제되었습니다.", ephemeral=True)
                    else:
                        await it.response.send_message("이미 처리되었거나 삭제된 작업입니다.", ephemeral=True)
                return callback
            
            stop_btn.callback = make_callback(i)
            view.add_item(stop_btn)
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="예약 취소", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚠️ 안전을 위해 앱 또는 홈페이지에서 직접 취소해 주세요.", ephemeral=True)
