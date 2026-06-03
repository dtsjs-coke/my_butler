import os
import asyncio
import discord
from discord.ext import tasks
from SRT import SRT
from core.srt_service import reservation_queue, save_queue, SRTStationView

SRT_CHANNEL_ID = int(os.getenv("SRT_CHANNEL_ID", 0))

@tasks.loop(seconds=10)
async def srt_reservation_loop(client):
    await client.wait_until_ready()

    current_users = list(reservation_queue.keys())

    for user_id in current_users:
        if user_id not in reservation_queue: continue
        
        user_tasks = reservation_queue[user_id]
        tasks_to_remove = []

        for i, task in enumerate(user_tasks):
            if task.get('status') == "시도중":
                try:
                    srt = await asyncio.to_thread(SRT, os.getenv("SRT_ID"), os.getenv("SRT_PW"))
                    
                    trains = await asyncio.to_thread(
                        srt.search_train,
                        dep=task['dep'], 
                        arr=task['arr'], 
                        date=task['date'], 
                        time=task['time'], 
                        time_limit=task.get('time_limit'),
                        available_only=False
                    )

                    if not trains:
                        continue

                    target_train = trains[0]
                    reservation = None

                    if "예약가능" in str(target_train):
                        reservation = await asyncio.to_thread(
                            srt.reserve,
                            target_train, 
                            passengers=task['passengers'],
                            special_seat=task['seat_type'],
                            window_seat=task['window_seat']
                        )
                    elif "예약대기" in str(target_train):
                        reservation = await asyncio.to_thread(
                            srt.reserve_standby,
                            target_train,
                            passengers=task['passengers'],
                            special_seat=task['seat_type']
                        )
                    if reservation:
                        tasks_to_remove.append(i)
                        
                        channel = client.get_channel(SRT_CHANNEL_ID)
                        if channel:
                            await channel.send(f"🔔 <@{user_id}>님! **SRT 예약/대기 성공**\n{reservation}")
                    
                except Exception as e:
                    print(f"SRT 예약 오류 (User {user_id}): {e}")

        if tasks_to_remove:
            for index in sorted(tasks_to_remove, reverse=True):
                if user_id in reservation_queue and index < len(reservation_queue[user_id]):
                    reservation_queue[user_id].pop(index)
            
            if user_id in reservation_queue and not reservation_queue[user_id]:
                del reservation_queue[user_id]
                
            save_queue(reservation_queue)

class SRTMainMenuView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    @discord.ui.button(label="기차 예약", style=discord.ButtonStyle.primary, emoji="🎫")
    async def reserve_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🚆 예약 단계를 시작합니다.", view=SRTStationView(), ephemeral=True)

    @discord.ui.button(label="현재 Queue 확인", style=discord.ButtonStyle.secondary, emoji="⏳")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 전체 큐를 보여주도록 수정하여 Web 예약건도 디스코드에서 관리 가능하게 함
        all_tasks = []
        for uid, tasks_list in reservation_queue.items():
            for i, t in enumerate(tasks_list):
                all_tasks.append({'user_id': uid, 'index': i, 'data': t})

        if not all_tasks:
            return await interaction.response.send_message("현재 진행 중인 예약 시도 작업이 없습니다.", ephemeral=True)
        
        embed = discord.Embed(
            title="⏳ 실시간 예약 시도 현황 (전체)", 
            description=f"버틀러가 현재 총 {len(all_tasks)}개의 조건으로 좌석을 찾고 있습니다.",
            color=discord.Color.orange()
        )
        
        view = discord.ui.View()
        
        for i, task_info in enumerate(all_tasks[:5]): # 디스코드 버튼 제한 고려 상위 5개만
            uid = task_info['user_id']
            idx = task_info['index']
            data = task_info['data']
            
            user_label = "Web" if uid == 'WEB_USER' else f"User {uid}"
            embed.add_field(
                name=f"작업 {i+1} ({user_label})", 
                value=f"🛣️ {data['dep']} ➡️ {data['arr']}\n📅 {data['date']} {data['time']}\n📡 {data.get('status', '대기 중')}", 
                inline=False
            )
            
            stop_btn = discord.ui.Button(label=f"#{i+1} 삭제", style=discord.ButtonStyle.danger)
            
            def make_callback(target_uid, target_idx):
                async def callback(it: discord.Interaction):
                    if target_uid in reservation_queue and target_idx < len(reservation_queue[target_uid]):
                        removed = reservation_queue[target_uid].pop(target_idx)
                        if not reservation_queue[target_uid]:
                            del reservation_queue[target_uid]
                        save_queue(reservation_queue)
                        await it.response.send_message(f"🛑 작업 ({removed['dep']}➡️{removed['arr']}) 이 삭제되었습니다.", ephemeral=True)
                    else:
                        await it.response.send_message("이미 처리되었거나 삭제된 작업입니다.", ephemeral=True)
                return callback
            
            stop_btn.callback = make_callback(uid, idx)
            view.add_item(stop_btn)
        
        if len(all_tasks) > 5:
            embed.set_footer(text=f"그 외 {len(all_tasks)-5}개의 작업이 더 있습니다. 웹 대시보드에서 확인하세요.")

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="예약 취소", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚠️ 안전을 위해 앱 또는 홈페이지에서 직접 취소해 주세요. (봇 취소 기능 비활성화)", ephemeral=True)
