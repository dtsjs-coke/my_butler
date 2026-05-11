import os
import sys
import discord
import asyncio
import threading
import re
from dotenv import load_dotenv

# 프로젝트 루트 경로를 sys.path에 추가 (절대 경로 import 보장)
PROJECT_ROOT = "/data/data/com.termux/files/home/dev_pjt/my_butler"
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

load_dotenv()

# 모듈별 임포트
from config.constants import *
from config.config_manager import load_keywords, save_keywords, load_stations, save_stations, load_model_name, save_model_name
from core.ai_service import ask_gemini, a2a_engine
from core.news_service import news_loop
from core.srt_manager import srt_reservation_loop, SRTMainMenuView
from api.flask_app import run_flask
from utils.system_status import get_system_status_embed, get_battery_short_report
from core import srt_service

# 봇 초기화
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'✅ {client.user} 가동 시작! (Clean Architecture)')
    
    # 주기적 작업 시작
    if not news_loop.is_running():
        news_loop.start(client)
    
    if not srt_reservation_loop.is_running():
        srt_reservation_loop.start(client)
        
    # Flask 서버 별도 쓰레드 실행
    threading.Thread(target=run_flask, args=(client,), daemon=True).start()

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    content = message.content
    channel_id = message.channel.id

    # 1. SRT 예약 채널
    if channel_id == SRT_CHANNEL_ID:
        if content == "!srt":
            view = SRTMainMenuView(message.author.id)
            await message.channel.send("🚆 **SRT 예약 메뉴**", view=view)
        elif content == "!역 리스트":
            stations = load_stations()
            await message.channel.send(f"📍 **현재 설정된 SRT 역 리스트:**\n{', '.join(stations)}")
        elif content.startswith("!역 추가 "):
            station = content.replace("!역 추가 ", "").strip()
            stations = load_stations()
            if station not in stations:
                stations.append(station)
                save_stations(stations)
                srt_service.STATIONS = stations
                await message.channel.send(f"✅ **{station}** 역이 추가되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역은 이미 리스트에 있습니다.")
        elif content.startswith("!역 삭제 "):
            station = content.replace("!역 삭제 ", "").strip()
            stations = load_stations()
            if station in stations:
                stations.remove(station)
                save_stations(stations)
                srt_service.STATIONS = stations
                await message.channel.send(f"✅ **{station}** 역이 삭제되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역을 찾을 수 없습니다.")
        return

    # 2. 뉴스 채널
    if channel_id == NEWS_CHANNEL_ID:
        if content == "!뉴스 리스트":
            keywords = load_keywords()
            await message.channel.send(f"📰 **현재 추적 중인 뉴스 키워드:**\n{', '.join(keywords)}")
        elif content.startswith("!뉴스 추가 "):
            kw = content.replace("!뉴스 추가 ", "").strip()
            keywords = load_keywords()
            if kw not in keywords:
                keywords.append(kw)
                save_keywords(keywords)
                await message.channel.send(f"✅ 키워드 **{kw}** 가 추가되었습니다.")
            else:
                await message.channel.send(f"⚠️ 키워드 **{kw}** 는 이미 리스트에 있습니다.")
        elif content.startswith("!뉴스 삭제 "):
            kw = content.replace("!뉴스 삭제 ", "").strip()
            keywords = load_keywords()
            if kw in keywords:
                keywords.remove(kw)
                save_keywords(keywords)
                await message.channel.send(f"✅ 키워드 **{kw}** 가 삭제되었습니다.")
            else:
                await message.channel.send(f"⚠️ 키워드 **{kw}** 를 찾을 수 없습니다.")
        return

    # 3. 기기 상태 채널
    if channel_id == STATUS_CHANNEL_ID:
        if not content.startswith('!'):
            status_keywords = ["배터리", "battery", "온도", "상태", "status"]
            if any(kw in content.lower() for kw in status_keywords):
                embed = get_system_status_embed()
                if embed:
                    await message.channel.send(embed=embed)
                else:
                    await message.channel.send("❌ 시스템 정보를 분석할 수 없습니다.")
                return

            async with message.channel.typing():
                ai_reply = await ask_gemini(content)
                if "[VIBRATE]" in ai_reply: os.system('termux-vibrate')
                elif "[BATTERY]" in ai_reply:
                    report = get_battery_short_report()
                    await message.channel.send(report)
                    return
                await message.channel.send(ai_reply)
        return

    # 4. CHAT / A2A 채널
    elif channel_id == CHAT_CHANNEL_ID:
        if content == "!모델 리스트":
            current_model = load_model_name()
            await message.channel.send(f"🤖 **사용 가능한 모델:**\n" + "\n".join([f"- {m}" for m in AVAILABLE_MODELS]) + f"\n\n**현재 설정:** `{current_model}`")
        elif content.startswith("!모델 설정 "):
            new_model = content.replace("!모델 설정 ", "").strip()
            if new_model in AVAILABLE_MODELS:
                save_model_name(new_model)
                # 엔진의 모델명도 업데이트 (재시작 없이 반영하기 위해 엔진 내부 변수 수정 고려)
                a2a_engine.model_name = new_model
                await message.channel.send(f"✅ AI 모델이 `{new_model}` 로 변경되었습니다.")
            else:
                await message.channel.send(f"⚠️ 사용 가능한 모델이 아닙니다.")
        elif content.startswith("!a2a "):
            request = content.replace("!a2a ", "").strip()
            status_msg = await message.channel.send("🚀 **A2A 협업 시작**")

            match = re.search(r'([a-zA-Z0-9_-]+\.py)', request)
            target_file = match.group(1) if match else None

            async def progress_callback(text):
                await status_msg.edit(content=f"⚙️ **진행 중:**\n> {text}")

            try:
                result = await a2a_engine.run_a2a(
                    request, 
                    progress_callback, 
                    save_path=target_file,
                    context=WORKSPACE_CONTEXT
                )
                
                if result["status"] == "success":
                    success_text = "✅ **A2A 작업 완료!**"
                    if result.get("saved_at"):
                        success_text += f"\n💾 저장 위치: `{result['saved_at']}`"
                    await status_msg.edit(content=success_text)
                    
                    design = result["design"]
                    embed = discord.Embed(title="📋 설계 아키텍처", color=discord.Color.green())
                    embed.add_field(name="구조", value=design.get("architecture", "N/A"), inline=False)
                    embed.add_field(name="흐름", value="\n".join([f"- {s}" for s in design.get("logic_flow", [])]), inline=False)
                    await message.channel.send(embed=embed)

                    code = result["code"]
                    if len(code) > 1900:
                        tmp_path = os.path.join(PROJECT_ROOT, "generated_code.py")
                        with open(tmp_path, "w", encoding="utf-8") as f:
                            f.write(code)
                        await message.channel.send("📄 코드가 길어 파일로 첨부합니다.", file=discord.File(tmp_path))
                        os.remove(tmp_path)
                    else:
                        await message.channel.send(f"💻 **생성된 코드:**\n```python\n{code}\n```")
                else:
                    await status_msg.edit(content=f"❌ **A2A 실패:** {result['message']}")

            except Exception as e:
                await status_msg.edit(content=f"💥 **치명적 오류:** {str(e)}")

        elif not content.startswith('!'):
            async with message.channel.typing():
                ai_reply = await ask_gemini(content)
                clean_reply = ai_reply.split(']')[-1].strip() if ']' in ai_reply else ai_reply
                await message.channel.send(clean_reply)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
