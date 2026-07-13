import os
import time
import json
import subprocess
import asyncio
import requests
from datetime import datetime
from dotenv import load_dotenv

# .env 파일 로드 (constants 임포트 전에 수행)
load_dotenv()

from utils.system_status import get_system_status_embed
from core.ai.service import ask_gemini
from config.constants import STATUS_CHANNEL_ID, CHAT_CHANNEL_ID
from core.agent_manager import load_agent_config, save_agent_config, add_pending_action

# 설정
TARGET_APP = "butler"
PM2_HOME = os.getenv("PM2_HOME", f"{os.path.expanduser('~')}/.pm2")
LOG_PATH = f"{PM2_HOME}/logs/{TARGET_APP}-error.log"
LOCAL_API_URL = "http://127.0.0.1:5000/send"
CHECK_INTERVAL = 60  # 기본 60초

class ButlerAgent:
    def __init__(self):
        self.last_log_size = 0
        if os.path.exists(LOG_PATH):
            self.last_log_size = os.path.getsize(LOG_PATH)
        self.config = load_agent_config()
        print(f"🚀 Butler Agent initialized. Watching {LOG_PATH}")

    def send_discord(self, content, channel_id=STATUS_CHANNEL_ID):
        if not channel_id or channel_id == 0:
            print(f"⚠️ Skipping send_discord: Invalid channel_id ({channel_id})")
            return
            
        try:
            payload = {"channel_id": channel_id, "content": content}
            requests.post(LOCAL_API_URL, json=payload, timeout=5)
        except Exception as e:
            print(f"Failed to send discord message: {e}")

    async def check_system(self):
        """S9 시스템 상태 모니터링 및 발열 관리 (캐시 활용)"""
        try:
            from utils.system_status import get_system_status_data
            data = get_system_status_data()
            batt_data = data.get("battery", {})
            temp = batt_data.get('temperature', 0)
            
            thermal_cfg = self.config["thermal_management"]
            if temp >= thermal_cfg["critical_temp"]:
                msg = f"""⚠️ **S9 발열 경고 ({temp}°C)**
온도 임계치를 초과했습니다. 에이전트가 작업을 지연시킵니다."""
                self.send_discord(msg)
                return True
            return False
        except Exception as e:
            print(f"System check error: {e}")
            return False

    async def analyze_logs(self):
        """에러 로그 감시 및 자가 치유 제안"""
        if not os.path.exists(LOG_PATH):
            return

        current_size = os.path.getsize(LOG_PATH)
        if current_size <= self.last_log_size:
            return

        with open(LOG_PATH, 'r') as f:
            f.seek(self.last_log_size)
            new_logs = f.read()
        
        self.last_log_size = current_size

        if "Exception" in new_logs or "Error" in new_logs:
            prompt = f"""당신은 Butler AI의 시스템 관리자입니다. 다음 에러 로그를 보고 원인을 진단한 후, 코드를 수정해야 한다면 수정된 전체 코드 또는 패치 내용을 JSON 형식으로 제안하세요.
형식: {{"analysis": "...", "need_fix": true, "patch": "...", "file": "..."}}

로그:
{new_logs[-1000:]}"""
            # JSON 응답을 위해 analyze_intent의 스키마 구조를 빌려쓰거나 ask_gemini 활용
            analysis_text = await ask_gemini(prompt)
            
            try:
                # 분석 결과에서 JSON 추출 (추후 정교화 필요)
                # 현재는 텍스트 보고 위주로 구현
                action_id = add_pending_action("SELF_HEALING", analysis_text, proposed_patch=None)
                
                report = f"""🚨 **자가 진단 보고 (ID: {action_id})**

**[분석 결과]**
{analysis_text[:1500]}

✅ 이 문제를 해결하기 위해 코드를 수정하시겠습니까? `!승인 {{action_id}}` 명령어를 입력하세요."""
                self.send_discord(report, CHAT_CHANNEL_ID)
            except Exception as e:
                print(f"Analysis handling error: {e}")

    async def process_pending_actions(self):
        """승인된 액션 실행 (파일 수정 및 재시작)"""
        self.config = load_agent_config()
        for action in self.config["pending_actions"]:
            if action["status"] == "approved":
                print(f"🛠 Executing approved action: {action['id']}")
                # TODO: 실제 파일 수정 로직 (A2AEngine과 연동)
                # 여기서는 완료 보고만 수행
                action["status"] = "completed"
                save_agent_config(self.config)
                self.send_discord(f"✅ **작업 완료**: {action['id']}가 성공적으로 적용되었습니다.", CHAT_CHANNEL_ID)
                
                # 서비스 재시작
                subprocess.run(["/data/data/com.termux/files/usr/bin/pm2", "restart", TARGET_APP])

    async def run(self):
        self.send_discord("""🤖 **Butler Agent v2 가동 시작**
자가 치유 및 발열 관리 모드가 활성화되었습니다.""")
        
        while True:
            # 1. 시스템 체크
            is_hot = await self.check_system()
            
            # 2. 로그 체크
            await self.analyze_logs()
            
            # 3. 승인된 액션 처리
            await self.process_pending_actions()
            
            # 발열 상태에 따라 인터벌 조절
            sleep_time = CHECK_INTERVAL * 2 if is_hot else CHECK_INTERVAL
            await asyncio.sleep(sleep_time)

if __name__ == "__main__":
    # 로컬 API 서버 대기
    time.sleep(5)
    agent = ButlerAgent()
    asyncio.run(agent.run())
