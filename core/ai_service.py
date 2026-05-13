import os
import aiohttp
import asyncio
from core.a2a_engine import A2AEngine
from config.config_manager import load_model_name

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = load_model_name()

# A2A 엔진 초기화
a2a_engine = A2AEngine(GEMINI_API_KEY)

async def ask_gemini(text, workspace_files=None):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    
    file_info = f"\n[현재 workspace 파일 목록]: {', '.join(workspace_files)}" if workspace_files else ""
    
    system_instruction = f"""
    당신은 S9 안드로이드의 관리자 '버틀러'입니다.
    {file_info}
    사용자의 질문 의도를 파악하여 적절히 응답하세요. 
    반드시 아래 키워드 중 하나로 응답을 시작하세요:
    [VIBRATE], [BATTERY], [CHAT]
    인사는 짧게 하고 본론만 말하세요.
    """
    payload = {"contents": [{"parts": [{"text": f"{system_instruction}\n\n사용자 질문: {text}"}]}]}

    # --- 타임아웃 설정 (10초) ---
    timeout = aiohttp.ClientTimeout(total=20)

    try:
        async with aiohttp.ClientSession(timeout = timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                result = await response.json()
                # 1. HTTP 상태 코드가 200이 아닐 경우 처리
                if response.status != 200:
                    error_msg = result.get('error', {}).get('message', 'Unknown Error')
                    return f"❌ API 에러 (상태 코드 {response.status}): {error_msg}"
                
                # 2. candidates 존재 여부 확인
                if 'candidates' not in result or not result['candidates']:
                    return "❌ AI 에러: 응답 후보(candidates)가 없습니다. (세이프티 필터 가능성)"

                return result['candidates'][0]['content']['parts'][0]['text']
    except asyncio.TimeoutError:
        return f"❌ AI 에러: 응답시간 초과(10초)"
    except Exception as e:
        return f"❌ AI 에러: {str(e)}"
