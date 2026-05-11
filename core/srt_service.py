from datetime import datetime
import discord
from discord import ui
from SRT import SRT, SeatType
from SRT.passenger import Adult, Child, Senior, Disability1To3, Disability4To6
import re

from config.config_manager import load_stations, load_queue, save_queue

# 예약 큐 관리 (영속성 로드)
reservation_queue = load_queue()
STATIONS = load_stations()

# --- [4단계: 좌석 및 최종 확인] ---
class SRTSeatOptionView(ui.View):
    def __init__(self, data):
        super().__init__(timeout=300)
        self.data = data
        self.seat_type = SeatType.GENERAL_FIRST
        self.window_seat = False

    @ui.select(placeholder="좌석 타입을 선택하세요 (기본: 일반실 우선)", options=[
        discord.SelectOption(label="일반실 우선", value="GENERAL_FIRST"),
        discord.SelectOption(label="일반실 전용", value="GENERAL_ONLY"),
        discord.SelectOption(label="특실 우선", value="SPECIAL_FIRST"),
        discord.SelectOption(label="특실 전용", value="SPECIAL_ONLY"),
    ])
    async def select_seat(self, interaction: discord.Interaction, select: ui.Select):
        self.seat_type = getattr(SeatType, select.values[0])
        await interaction.response.defer()

    @ui.button(label="창가 자리 우선 (OFF)", style=discord.ButtonStyle.gray)
    async def window_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.window_seat = not self.window_seat
        button.label = f"창가 자리 우선 ({'ON' if self.window_seat else 'OFF'})"
        button.style = discord.ButtonStyle.green if self.window_seat else discord.ButtonStyle.gray
        await interaction.response.edit_message(view=self)

    @ui.button(label="예약 등록", style=discord.ButtonStyle.primary)
    async def confirm_btn(self, interaction: discord.Interaction, button: ui.Button):
        self.data['seat_type'] = self.seat_type
        self.data['window_seat'] = self.window_seat
        self.data['created_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
        user_id = interaction.user.id
        
        # 유저별 큐 가져오기 (없으면 리스트 생성)
        if user_id not in reservation_queue:
            reservation_queue[user_id] = []
            
        # 최대 3개 제한 확인
        if len(reservation_queue[user_id]) >= 3:
            return await interaction.response.send_message(
                "⚠️ 이미 3개의 예약 대기 작업이 등록되어 있습니다. 기존 작업을 완료하거나 삭제한 후 다시 시도해 주세요.", 
                ephemeral=True
            )
            
        # 새로운 작업 추가
        task_data = self.data.copy()
        task_data['status'] = "시도중"
        task_data['user_name'] = interaction.user.name
        reservation_queue[user_id].append(task_data)
        
        # 영속성 저장
        save_queue(reservation_queue)
    
        await interaction.response.send_message(
            f"✅ SRT 예약 대기열에 등록되었습니다. (현재 {len(reservation_queue[user_id])}/3 개)\n"
            f"**방법:** `!srt` -> `현재 Queue 확인` 메뉴에서 언제든지 중단할 수 있습니다.", 
            ephemeral=True
        )
        
    @ui.button(label="이전", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: ui.Button):
        # 3단계(인원수) 모달 다시 띄우기
        await interaction.response.send_modal(SRTPassengerModal(self.data))

# --- [3단계: 인원수 입력 모달] ---
class SRTPassengerModal(ui.Modal):
    def __init__(self, data):
        super().__init__(title="3단계: 예약자 수 입력")
        self.data = data
        self.adult = ui.TextInput(label="어른", default="1", max_length=1)
        self.child = ui.TextInput(label="어린이", default="0", max_length=1)
        self.senior = ui.TextInput(label="경로", default="0", max_length=1)
        self.disability = ui.TextInput(label="장애(1~6급)", default="0", max_length=1)
        
        for item in [self.adult, self.child, self.senior, self.disability]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # 입력 검증
            counts = [int(self.adult.value), int(self.child.value), int(self.senior.value), int(self.disability.value)]
            passengers = []
            for _ in range(counts[0]): passengers.append(Adult())
            for _ in range(counts[1]): passengers.append(Child())
            for _ in range(counts[2]): passengers.append(Senior())
            for _ in range(counts[3]): passengers.append(Disability1To3()) # 기본값 1~3급 처리
            
            self.data['passengers'] = passengers
            await interaction.response.send_message("마지막 단계: 좌석 옵션을 선택하세요.", view=SRTSeatOptionView(self.data), ephemeral=True)
        except:
            await interaction.response.send_message("❌ 인원수는 숫자만 입력 가능합니다.", ephemeral=True)


# --- [2단계: 날짜 및 시간 입력 모달] ---
class SRTTimeModal(ui.Modal):
    def __init__(self, data):
        super().__init__(title="2단계: 일시 및 시간한도 입력")
        self.data = data
        self.date = ui.TextInput(label="출발 일자 (yyyyMMdd)", min_length=8, max_length=8, placeholder="20260608")
        self.time = ui.TextInput(label="출발 시간 (hhmmss)", min_length=6, max_length=6, placeholder="080000")
        self.limit = ui.TextInput(label="조회 한도 시간 (hhmmss) - 선택", min_length=0, max_length=6, required=False)
        
        self.add_item(self.date)
        self.add_item(self.time)
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction):
        # 형식 검증
        if not (re.match(r"^\d{8}$", self.date.value) and re.match(r"^\d{6}$", self.time.value)):
            return await interaction.response.send_message("❌ 날짜/시간 형식이 틀렸습니다.", ephemeral=True)
        
        self.data.update({
            "date": self.date.value, 
            "time": self.time.value, 
            "time_limit": self.limit.value if self.limit.value else None
        })

        # [해결책] 모달에서 바로 모달을 띄우지 않고, 버튼이 포함된 메시지를 보냅니다.
        view = ui.View()
        next_btn = ui.Button(label="다음: 인원수 입력하기", style=discord.ButtonStyle.primary)

        async def next_callback(it: discord.Interaction):
            # 버튼 클릭 인터랙션은 새로운 모달을 띄울 수 있습니다.
            from srt_service import SRTPassengerModal
            await it.response.send_modal(SRTPassengerModal(self.data))

        next_btn.callback = next_callback
        view.add_item(next_btn)

        await interaction.response.send_message(
            content=f"✅ 일시 입력 완료: {self.data['date']} {self.data['time']}\n아래 버튼을 눌러 인원수를 입력해주세요.",
            view=view,
            ephemeral=True
        )

# --- [1단계: 역 선택 View] ---
class SRTStationView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.data = {}
        self.add_dep_select()

    def add_dep_select(self):
        self.clear_items()
        select = ui.Select(placeholder="출발역을 선택하세요", options=[discord.SelectOption(label=s) for s in STATIONS])
        select.callback = self.dep_callback
        self.add_item(select)

    async def dep_callback(self, interaction: discord.Interaction):
        self.data['dep'] = interaction.data['values'][0]
        self.clear_items()
        # 출발역 제외하고 도착역 목록 생성
        select = ui.Select(placeholder=f"출발: {self.data['dep']} | 도착역 선택", options=[discord.SelectOption(label=s) for s in STATIONS if s != self.data['dep']])
        select.callback = self.arr_callback
        self.add_item(select)
        await interaction.response.edit_message(content=f"📍 출발역 **{self.data['dep']}** 선택됨.", view=self)

    async def arr_callback(self, interaction: discord.Interaction):
        self.data['arr'] = interaction.data['values'][0]
        # View(인터랙션)에서 Modal을 띄우는 것은 허용됨
        await interaction.response.send_modal(SRTTimeModal(self.data))
