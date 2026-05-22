import os
import sys
import discord
import asyncio
import threading
import re
from dotenv import load_dotenv

# 프로젝트 루트 경로를 설정 (파일 위치 기준 동적 설정)
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

load_dotenv()

# 모듈별 임포트
from config.constants import *
from config.config_manager import (
    load_keywords, save_keywords, 
    load_stations, save_stations, 
    load_ktx_stations, save_ktx_stations,
    load_model_name, save_model_name
)
from core.ai_service import ask_gemini, a2a_engine, analyze_intent
from core.news_service import news_loop
from core.srt_manager import srt_reservation_loop, SRTMainMenuView
from core.ktx_manager import ktx_reservation_loop, KTXMainMenuView
from core.subscription_manager import start_subscription_tasks
from api.flask_app import run_flask
from utils.system_status import get_system_status_embed, get_battery_short_report
from core import srt_service, ktx_service

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
        
    if not ktx_reservation_loop.is_running():
        ktx_reservation_loop.start(client)
        
    # 구독 관리 작업 시작
    start_subscription_tasks(client)
        
    # Flask 서버 별도 쓰레드 실행
    threading.Thread(target=run_flask, args=(client,), daemon=True).start()

async def handle_a2a_task(message, request):
    """A2A 작업을 처리하는 공통 함수"""
    status_msg = await message.channel.send("🚀 **A2A 에이전트 가동**")
    
    # 정규표현식으로 파일명 추출
    match = re.search(r'([a-zA-Z0-9_-]+\.py)', request)
    target_file = match.group(1) if match else "auto_generated.py"

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
            embed = discord.Embed(title="📋 A2A 자동 작업 완료", color=discord.Color.blue())
            embed.add_field(name="파일명", value=f"`{target_file}`", inline=True)
            output = result.get('output', 'Success')
            embed.add_field(name="실행 결과", value=f"```\n{output[:500]}\n```", inline=False)
            await message.channel.send(embed=embed)
            await status_msg.edit(content=f"✅ **A2A 자율 작업 및 검증 완료!**")
        else:
            await status_msg.edit(content=f"❌ **A2A 실패:** {result['message']}")
    except Exception as e:
        await status_msg.edit(content=f"💥 **치명적 오류:** {str(e)}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    content = message.content
    channel_id = message.channel.id

    # 1. SRT & KTX 예약 채널
    if channel_id == SRT_CHANNEL_ID:
        if content == "!help":
            help_text = """🚆 **기차 예약 채널 도움말**
- `!srt`: SRT 메인 예약 메뉴
- `!ktx`: KTX 메인 예약 메뉴
- `!역 리스트`: 설정된 SRT 역 목록
- `!역 추가 [역명]`, `!역 삭제 [역명]`
- `!ktx역 리스트`: 설정된 KTX 역 목록
- `!ktx역 추가 [역명]`, `!ktx역 삭제 [역명]`"""
            await message.channel.send(help_text)
        elif content == "!srt":
            view = SRTMainMenuView(message.author.id)
            await message.channel.send("🚆 **SRT 예약 메뉴**", view=view)
        elif content == "!ktx":
            # view = KTXMainMenuView(message.author.id)
            # await message.channel.send("🚆 **KTX 예약 메뉴**", view=view)
            await message.channel.send("⚠️ **korail에서 미지원해서 사용 불가**")
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
                await message.channel.send(f"✅ **{station}** SRT 역이 추가되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역은 이미 SRT 리스트에 있습니다.")
        elif content.startswith("!역 삭제 "):
            station = content.replace("!역 삭제 ", "").strip()
            stations = load_stations()
            if station in stations:
                stations.remove(station)
                save_stations(stations)
                srt_service.STATIONS = stations
                await message.channel.send(f"✅ **{station}** SRT 역이 삭제되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역을 SRT 리스트에서 찾을 수 없습니다.")
        elif content == "!ktx역 리스트":
            stations = load_ktx_stations()
            await message.channel.send(f"📍 **현재 설정된 KTX 역 리스트:**\n{', '.join(stations)}")
        elif content.startswith("!ktx역 추가 "):
            station = content.replace("!ktx역 추가 ", "").strip()
            stations = load_ktx_stations()
            if station not in stations:
                stations.append(station)
                save_ktx_stations(stations)
                ktx_service.KTX_STATIONS = stations
                await message.channel.send(f"✅ **{station}** KTX 역이 추가되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역은 이미 KTX 리스트에 있습니다.")
        elif content.startswith("!ktx역 삭제 "):
            station = content.replace("!ktx역 삭제 ", "").strip()
            stations = load_ktx_stations()
            if station in stations:
                stations.remove(station)
                save_ktx_stations(stations)
                ktx_service.KTX_STATIONS = stations
                await message.channel.send(f"✅ **{station}** KTX 역이 삭제되었습니다.")
            else:
                await message.channel.send(f"⚠️ **{station}** 역을 KTX 리스트에서 찾을 수 없습니다.")
        return

    # 2. 뉴스 채널
    elif channel_id == NEWS_CHANNEL_ID:
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
    elif channel_id == STATUS_CHANNEL_ID:
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
- `!a2a [요청] [파일명.py]`: 수동 A2A 작업 시작
- 일반 대화: 버틀러가 의도를 분석하여 자동 대응 (CHAT, TOOL, A2A)"""
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
            new_model = new_model.replace("`", "")
            clean_models = [m.split("#")[0].strip() for m in AVAILABLE_MODELS]
            
            if new_model in clean_models:
                save_model_name(new_model)
                await message.channel.send(f"✅ 일반 대화 모델이 `{new_model}` 로 변경되었습니다.\n*(A2A 모델은 시스템 최적화 설정을 유지합니다)*")
            else:
                await message.channel.send(f"⚠️ 사용 가능한 모델이 아닙니다. `!모델 리스트`를 확인하세요.")
        elif content.startswith("!승인 "):
            action_id = content.replace("!승인 ", "").strip()
            from core.agent_manager import resolve_action
            action = resolve_action(action_id, approved=True)
            if action:
                await message.channel.send(f"🆗 **승인 완료**: ID `{action_id}` 작업을 곧 실행합니다.")
            else:
                await message.channel.send(f"❌ **오류**: 유효하지 않은 ID `{action_id}` 입니다.")
        elif content.startswith("!a2a "):
            request = content.replace("!a2a ", "").strip()
            await handle_a2a_task(message, request)
        elif not content.startswith('!'):
            async with message.channel.typing():
                workspace_dir = os.path.join(PROJECT_ROOT, "workspace")
                ws_files = os.listdir(workspace_dir) if os.path.exists(workspace_dir) else []
                
                analysis = await analyze_intent(content, workspace_files=ws_files)
                intent = analysis.get("intent", "CHAT")
                
                if intent == "CHAT":
                    await message.channel.send(analysis.get("chat_response") or await ask_gemini(content))
                elif intent == "TOOL":
                    tool = analysis.get("tool_name")
                    if tool == "STATUS":
                        embed = get_system_status_embed()
                        await message.channel.send("📱 시스템 상태를 확인합니다.", embed=embed)
                    elif tool == "SRT":
                        await message.channel.send("🚆 SRT 예약 관련 문의시군요. `!srt` 명령어를 사용하시거나 구체적인 역 정보를 말씀해주세요.")
                    elif tool == "KTX":
                        await message.channel.send("🚆 KTX 예약 관련 문의시군요. `!ktx` 명령어를 사용하시거나 구체적인 역 정보를 말씀해주세요.")
                    elif tool == "VIBRATE":
                        os.system('termux-vibrate')
                        await message.channel.send("📳 진동을 울렸습니다.")
                    else:
                        await message.channel.send(await ask_gemini(content))
                elif intent == "A2A":
                    a2a_req = analysis.get("a2a_request") or content
                    await message.channel.send(f"🛠 **자율 작업 시작:** {analysis.get('thought')}")
                    await handle_a2a_task(message, a2a_req)
        return

    # 5. CLI Agent 전용 채널 (관리자 전용)
    elif channel_id == CLI_CHANNEL_ID:
        if message.author.id != DISCORD_ADMIN_USER_ID:
            return 

        if content == "!help":
            await message.channel.send("🛠 **CLI 에이전트 모드**\n- 셸 명령어 실행, 파일 관리, 시스템 제어 가능\n- 자연어로 요청하면 적절한 명령어를 실행합니다.")
            return

        async with message.channel.typing():
            all_files = []
            for root, dirs, files in os.walk(PROJECT_ROOT):
                if any(x in root for x in [".git", "__pycache__", "venv"]): continue
                rel_path = os.path.relpath(root, PROJECT_ROOT)
                for f in files:
                    all_files.append(os.path.join(rel_path, f) if rel_path != "." else f)
            
            analysis = await analyze_intent(content, workspace_files=all_files[:50], is_cli_mode=True)
            intent = analysis.get("intent")

            if intent == "SHELL":
                cmd = analysis.get("command")
                thought = analysis.get("thought")
                await message.channel.send(f"💻 **실행 계획:** {thought}\n> `{cmd}`")
                
                try:
                    process = await asyncio.create_subprocess_shell(
                        cmd,
                        cwd=PROJECT_ROOT,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
                    
                    result = stdout.decode().strip()
                    error = stderr.decode().strip()
                    
                    output = f"✅ **성공:**\n```\n{result[:1900]}\n```" if result else ""
                    if error:
                        output += f"\n⚠️ **에러/경고:**\n```\n{error[:500]}\n```"
                    
                    if not output:
                        output = "✅ 명령어가 실행되었으나 출력이 없습니다."
                    
                    await message.channel.send(output)
                except Exception as e:
                    await message.channel.send(f"❌ **실행 실패:** {str(e)}")
            elif intent == "CHAT":
                await message.channel.send(analysis.get("chat_response") or await ask_gemini(content))
            elif intent == "A2A":
                a2a_req = analysis.get("a2a_request") or content
                await message.channel.send(f"🛠 **자율 작업 시작:** {analysis.get('thought')}")
                await handle_a2a_task(message, a2a_req)
            else:
                await message.channel.send(await ask_gemini(content))
        return

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
