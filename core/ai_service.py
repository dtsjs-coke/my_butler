import os
import aiohttp
import asyncio
import json
from core.a2a_engine import A2AEngine
from config.config_manager import load_model_name

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = load_model_name()

# A2A 엔진 초기화
a2a_engine = A2AEngine(GEMINI_API_KEY)

async def analyze_intent(text, workspace_files=None, is_cli_mode=False):
    """사용자의 의도를 분석하여 행동을 결정하는 Router Agent"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    
    file_info = f"\n[현재 workspace 파일 목록]: {', '.join(workspace_files)}" if workspace_files else ""
    
    intent_list = ["CHAT", "TOOL", "A2A"]
    if is_cli_mode:
        intent_list.append("SHELL")

    router_schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": intent_list},
            "tool_name": {"type": "string", "enum": ["SRT", "NEWS", "STATUS", "VIBRATE", "NONE"]},
            "a2a_request": {"type": "string", "description": "A2A로 수행할 상세 요청"},
            "command": {"type": "string", "description": "실행할 셸 명령어 (SHELL 인텐트일 때)"},
            "chat_response": {"type": "string", "description": "즉시 응답할 메시지"},
            "thought": {"type": "string", "description": "의도 분석 근거"}
        },
        "required": ["intent", "thought"]
    }

    system_instruction = f"""
    당신은 스마트 비서 '버틀러'의 핵심 두뇌입니다.
    사용자의 요청을 분석하여 최적의 행동을 선택하세요.
    
    1. CHAT: 일상적인 대화, 단순 질문 답변.
    2. TOOL: 이미 구축된 도구(SRT 예약, 뉴스 관리, 배터리/시스템 상태) 사용.
    3. A2A: 코딩이 필요한 고수준 작업 (파일 생성, 데이터 분석 등).
    """

    if is_cli_mode:
        system_instruction += """
    4. SHELL: 시스템 관리자로서 셸 명령어를 실행하거나 파일을 조작해야 할 때.
       - 예: 파일 목록 보기(ls), 파일 내용 읽기(cat), 프로세스 확인(ps), 패키지 설치 등.
       - 보안 주의: 위험한 명령어는 신중히 판단하세요.
        """
    
    system_instruction += f"\n{file_info}"
    
    payload = {
        "contents": [{"parts": [{"text": f"{system_instruction}\n\n사용자 요청: {text}"}]}],
        "generation_config": {
            "response_mime_type": "application/json",
            "response_schema": router_schema
        }
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            if response.status == 200:
                result = await response.json()
                return json.loads(result['candidates'][0]['content']['parts'][0]['text'])
            return {"intent": "CHAT", "chat_response": "⚠️ 서버와 연결이 원활하지 않습니다.", "thought": "Error response"}

async def ask_gemini(text, workspace_files=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": f"당신은 버틀러입니다. 다음 요청에 대해 짧고 친절하게 대화하세요: {text}"}]}]}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as response:
            result = await response.json()
            if 'candidates' in result and result['candidates']:
                return result['candidates'][0]['content']['parts'][0]['text']
            return "응답을 생성할 수 없습니다."
