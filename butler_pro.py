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
        if content == "!help":
            help_text = """🚆 **SRT 채널 도움말**
- `!srt`: 메인 예약 메뉴 열기
- `!역 리스트`: 설정된 SRT 역 목록 보기
- `!역 추가 [역명]`: 새로운 역 추가
- `!역 삭제 [역명]`: 기존 역 삭제"""
            await message.channel.send(help_text)
        elif content == "!srt":
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
        if content == "!help":
            help_text = """📰 **뉴스 채널 도움말**
- `!뉴스 리스트`: 현재 추적 중인 키워드 목록
- `!뉴스 추가 [키워드]`: 새로운 뉴스 키워드 추가
- `!뉴스 삭제 [키워드]`: 기존 키워드 삭제
* 30분 간격으로 새 뉴스를 자동 검색합니다."""
            await message.channel.send(help_text)
        elif content == "!뉴스 리스트":
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
        if content == "!help":
            help_text = """📱 **상태 채널 도움말**
- `배터리`, `온도`, `상태` 등 키워드 포함 메시지: 기기 상태 보고
- 일반 대화: 버틀러와 자유 대화 (파일 목록 등 의도 파악 자동 대응)
- AI 응답 내 `[BATTERY]` 포함 시 요약 보고 실행"""
            await message.channel.send(help_text)
        elif not content.startswith('!'):
            status_keywords = ["배터리", "battery", "온도", "상태", "status"]
            if any(kw in content.lower() for kw in status_keywords):
                embed = get_system_status_embed()
                if embed:
                    await message.channel.send(embed=embed)
                else:
                    await message.channel.send("❌ 시스템 정보를 분석할 수 없습니다.")
                return

            async with message.channel.typing():
                # 실시간 파일 목록 가져오기
                workspace_dir = os.path.join(PROJECT_ROOT, "workspace")
                ws_files = os.listdir(workspace_dir) if os.path.exists(workspace_dir) else []
                
                ai_reply = await ask_gemini(content, workspace_files=ws_files)
                if "[VIBRATE]" in ai_reply: os.system('termux-vibrate')
                elif "[BATTERY]" in ai_reply:
                    report = get_battery_short_report()
                    await message.channel.send(report)
                    return
                await message.channel.send(ai_reply)
        return

    # 4. CHAT / A2A 채널
    elif channel_id == CHAT_CHANNEL_ID:
        if content == "!help":
            help_text = f"""🤖 **CHAT/A2A 채널 도움말**
- `!모델 리스트`: 사용 가능한 AI 모델 추천 및 현재 설정 확인
- `!모델 설정 [ModelID]`: 일반 대화용 AI 모델 변경
- `!a2a [요청] [파일명.py]`: 전용 티어 모델(Manager: Pro / Coder: Flash)을 활용한 자동 코딩 협업
- 일반 대화: 버틀러와 자유 대화 (파일 목록 등 의도 파악 자동 대응)"""
            await message.channel.send(help_text)
        elif content == "!모델 리스트":
            current_model = load_model_name()
            model_list = []
            for m in AVAILABLE_MODELS:
                recommendation = ""
                if "lite" in m: recommendation = "(추천: ⚡ 빠른 응답, 일상 대화)"
                elif "pro" in m: recommendation = "(추천: 🧠 복잡한 논리, 심층 분석)"
                elif "flash" in m and "lite" not in m: recommendation = "(추천: 🛠️ 일반 질문, 코딩 보조)"
                model_list.append(f"- `{m}` {recommendation}")
            
            a2a_info = f"\n**A2A 전용 티어:**\n- Manager: `{A2A_TIERS['MANAGER']}`\n- Coder: `{A2A_TIERS['CODER']}`"
            await message.channel.send(f"🤖 **사용 가능한 모델 리스트:**\n" + "\n".join(model_list) + f"\n\n**현재 일반 대화 설정:** `{current_model}`" + a2a_info)
        elif content.startswith("!모델 설정 "):
            new_model = content.replace("!모델 설정 ", "").strip()
            # 입력값에서 백틱 등 제거
            new_model = new_model.replace("`", "")
            
            # AVAILABLE_MODELS의 요소들과 비교 (추석문 제거 후 비교)
            clean_models = [m.split("#")[0].strip() for m in AVAILABLE_MODELS]
            
            if new_model in clean_models:
                save_model_name(new_model)
                await message.channel.send(f"✅ 일반 대화 모델이 `{new_model}` 로 변경되었습니다.\n*(A2A 모델은 시스템 최적화 설정을 유지합니다)*")
            else:
                await message.channel.send(f"⚠️ 사용 가능한 모델이 아닙니다. `!모델 리스트`를 확인하세요.")
        elif content.startswith("!a2a "):
            request = content.replace("!a2a ", "").strip()
            status_msg = await message.channel.send("🚀 **A2A 협업 시작**")

            # 정규표현식으로 파일명 추출 (예: status.py를 -> status.py)
            match = re.search(r'([a-zA-Z0-9_-]+\.py)', request)
            target_file = match.group(1) if match else "generated_task.py"

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
                    saved_path = result.get("saved_at", "N/A")
                    
                    # 1. 설계 및 코드 생성 결과 표시
                    design = result["design"]
                    embed = discord.Embed(title="📋 A2A 설계 및 코드 생성 완료", color=discord.Color.green())
                    embed.add_field(name="파일명", value=f"`{target_file}`", inline=True)
                    embed.add_field(name="저장 경로", value="`workspace/`", inline=True)
                    embed.add_field(name="아키텍처", value=design.get("architecture", "N/A"), inline=False)
                    embed.add_field(name="로직 흐름", value="\n".join([f"- {s}" for s in design.get("logic_flow", [])]), inline=False)
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

                    # 2. 실행 단계 진행 상태 업데이트
                    await status_msg.edit(content=f"⚙️ **작업 진행:**\n> 🏃 생성된 코드 실행 중... (`{target_file}`)")
                    
                    try:
                        # workspace 디렉토리에서 실행하도록 cwd 설정
                        workspace_dir = os.path.join(PROJECT_ROOT, "workspace")
                        process = await asyncio.create_subprocess_exec(
                            'python3', target_file,
                            cwd=workspace_dir,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        
                        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                        
                        if process.returncode == 0:
                            await status_msg.edit(content=f"✅ **A2A 작업 및 실행 완료!**\n💾 파일은 `workspace/{target_file}` 에 저장되었습니다.")
                            await message.channel.send(f"🚀 **실행 성공!** 결과는 Discord로 전송되었을 것입니다.")
                        else:
                            error_msg = stderr.decode().strip()
                            await status_msg.edit(content=f"⚠️ **A2A 작업 완료 (실행 중 오류 발생)**")
                            await message.channel.send(f"⚠️ **실행 중 오류 발생:**\n```\n{error_msg}\n```")
                            
                    except asyncio.TimeoutError:
                        if process: process.kill()
                        await status_msg.edit(content=f"⏱️ **A2A 작업 완료 (실행 시간 초과)**")
                        await message.channel.send("⏱️ **실행 시간 초과:** 30초 내에 완료되지 않아 강제 종료되었습니다.")
                    except Exception as e:
                        await status_msg.edit(content=f"❌ **A2A 작업 완료 (실행 실패)**")
                        await message.channel.send(f"❌ **실행 실패:** {str(e)}")
                else:
                    await status_msg.edit(content=f"❌ **A2A 실패:** {result['message']}")

            except Exception as e:
                await status_msg.edit(content=f"💥 **치명적 오류:** {str(e)}")

        elif not content.startswith('!'):
            async with message.channel.typing():
                # 실시간 파일 목록 가져오기
                workspace_dir = os.path.join(PROJECT_ROOT, "workspace")
                ws_files = os.listdir(workspace_dir) if os.path.exists(workspace_dir) else []
                
                ai_reply = await ask_gemini(content, workspace_files=ws_files)
                clean_reply = ai_reply.split(']')[-1].strip() if ']' in ai_reply else ai_reply
                await message.channel.send(clean_reply)

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
