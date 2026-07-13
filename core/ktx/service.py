from datetime import datetime
import discord
from discord import ui
from korail2 import Korail, ReserveOption, AdultPassenger, ChildPassenger, SeniorPassenger
import re

from config.config_manager import load_ktx_stations, load_ktx_queue, save_ktx_queue

# 예약 큐 관리 (영속성 로드)
ktx_reservation_queue = load_ktx_queue()
KTX_STATIONS = load_ktx_stations()

# --- [4단계: 좌석 및 최종 확인] ---
class KTXSeatOptionView(ui.View):
    def __init__(self, data):
        super().__init__(timeout=300)
        self.data = data
        self.seat_type = ReserveOption.GENERAL_FIRST

    @ui.select(placeholder="좌석 타입을 선택하세요 (기본: 일반실 우선)", options=[
        discord.SelectOption(label="일반실 우선", value="GENERAL_FIRST"),
        discord.SelectOption(label="일반실 전용", value="GENERAL_ONLY"),
        discord.SelectOption(label="특실 우선", value="SPECIAL_FIRST"),
        discord.SelectOption(label="특실 전용", value="SPECIAL_ONLY"),
    ])
    async def select_seat(self, interaction: discord.Interaction, select: ui.Select):
        self.seat_type = getattr(ReserveOption, select.values[0])
        await interaction.response.defer()

    @ui.button(label="예약 등록", style=discord.ButtonStyle.primary)
    async def confirm_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.data['seat_type'] = self.seat_type
        self.data['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
        user_id = interaction.user.id
        
        # 유저별 큐 가져오기
        if user_id not in ktx_reservation_queue:
            ktx_reservation_queue[user_id] = []
            
        # 최대 3개 제한
        if len(ktx_reservation_queue[user_id]) >= 3:
            return await interaction.response.send_message(
                "⚠️ 이미 3개의 KTX 예약 대기 작업이 등록되어 있습니다.", 
                ephemeral=True
            )
            
        # 새로운 작업 추가
        task_data = self.data.copy()
        task_data['status'] = "시도중"
        task_data['user_name'] = interaction.user.name
        ktx_reservation_queue[user_id].append(task_data)
        
        # 영속성 저장
        save_ktx_queue(ktx_reservation_queue)
    
        await interaction.response.send_message(
            f"✅ KTX 예약 대기열에 등록되었습니다. (현재 {len(ktx_reservation_queue[user_id])}/3 개)\n"
            f"**방법:** `!ktx` -> `현재 Queue 확인` 메뉴에서 중단 가능합니다.", 
            ephemeral=True
        )
        
    @ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(KTXPassengerModal(self.data))

# --- [3단계: 인원수 입력 모달] ---
class KTXPassengerModal(ui.Modal):
    def __init__(self, data):
        super().__init__(title="3단계: 예약자 수 입력")
        self.data = data
        self.adult = ui.TextInput(label="어른", default="1", max_length=1)
        self.child = ui.TextInput(label="어린이", default="0", max_length=1)
        self.senior = ui.TextInput(label="경로", default="0", max_length=1)
        
        for item in [self.adult, self.child, self.senior]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            counts = [int(self.adult.value), int(self.child.value), int(self.senior.value)]
            passengers = []
            if counts[0] > 0: passengers.append(AdultPassenger(counts[0]))
            if counts[1] > 0: passengers.append(ChildPassenger(counts[1]))
            if counts[2] > 0: passengers.append(SeniorPassenger(counts[2]))
            
            if not passengers:
                return await interaction.response.send_message("❌ 최소 1명 이상의 인원을 입력해야 합니다.", ephemeral=True)

            self.data['passengers'] = passengers
            await interaction.response.send_message("마지막 단계: 좌석 옵션을 선택하세요.", view=KTXSeatOptionView(self.data), ephemeral=True)
        except:
            await interaction.response.send_message("❌ 인원수는 숫자만 입력 가능합니다.", ephemeral=True)

# --- [2단계: 날짜 및 시간 입력 모달] ---
class KTXTimeModal(ui.Modal):
    def __init__(self, data):
        super().__init__(title="2단계: 일시 입력")
        self.data = data
        self.date = ui.TextInput(label="출발 일자 (yyyyMMdd)", min_length=8, max_length=8, placeholder="20260608")
        self.time = ui.TextInput(label="출발 시간 (hhmmss)", min_length=6, max_length=6, placeholder="080000")
        
        self.add_item(self.date)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction):
        if not (re.match(r"^\d{8}$", self.date.value) and re.match(r"^\d{6}$", self.time.value)):
            return await interaction.response.send_message("❌ 날짜/시간 형식이 틀렸습니다.", ephemeral=True)
        
        self.data.update({"date": self.date.value, "time": self.time.value})

        view = ui.View()
        next_btn = ui.Button(label="다음: 인원수 입력하기", style=discord.ButtonStyle.primary)

        async def next_callback(it: discord.Interaction):
            await it.response.send_modal(KTXPassengerModal(self.data))

        next_btn.callback = next_callback
        view.add_item(next_btn)

        await interaction.response.send_message(
            content=f"✅ 일시 입력 완료: {self.data['date']} {self.data['time']}\n아래 버튼을 눌러 인원수를 입력해주세요.",
            view=view,
            ephemeral=True
        )

# --- [1단계: 역 선택 View] ---
class KTXStationView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.data = {}
        self.add_dep_select()

    def add_dep_select(self):
        self.clear_items()
        # 25개 제한 때문에 STATIONS를 나눠야 할 수도 있지만 현재는 22개
        select = ui.Select(placeholder="출발역을 선택하세요", options=[discord.SelectOption(label=s) for s in KTX_STATIONS[:25]])
        select.callback = self.dep_callback
        self.add_item(select)

    async def dep_callback(self, interaction: discord.Interaction):
        self.data['dep'] = interaction.data['values'][0]
        self.clear_items()
        select = ui.Select(placeholder=f"출발: {self.data['dep']} | 도착역 선택", 
                           options=[discord.SelectOption(label=s) for s in KTX_STATIONS[:25] if s != self.data['dep']])
        select.callback = self.arr_callback
        self.add_item(select)
        await interaction.response.edit_message(content=f"📍 출발역 **{self.data['dep']}** 선택됨.", view=self)

    async def arr_callback(self, interaction: discord.Interaction):
        self.data['arr'] = interaction.data['values'][0]
        await interaction.response.send_modal(KTXTimeModal(self.data))
