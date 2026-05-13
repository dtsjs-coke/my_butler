import os
import json
import aiohttp
import asyncio
import py_compile
import tempfile
from datetime import datetime
from utils.security import SecurityChecker, FileManager
from config.constants import A2A_TIERS

class A2AEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.headers = {'Content-Type': 'application/json'}
        # 보안 파일 매니저 초기화 (workspace 경로 설정)
        self.file_manager = FileManager("/data/data/com.termux/files/home/dev_pjt/my_butler/workspace")

    async def _ask_gemini(self, contents, model_name, thinking_level="high", response_schema=None):
        """Gemini 3 API 호출 (snake_case 표준 규격 적용)"""
        url = f"{self.base_url}/{model_name}:generateContent?key={self.api_key}"
        
        # 최신 Gemini 3 서버와 가장 호환성이 높은 snake_case 구조
        generation_config = {
            "response_mime_type": "application/json",
            "thinking_config": {
                "thinking_level": thinking_level
            }
        }
        
        if response_schema:
            generation_config["response_schema"] = response_schema
            
        payload = {
            "contents": contents,
            "generation_config": generation_config
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload) as resp:
                if resp.status != 200:
                    data = await resp.json()
                    raise Exception(f"API Error ({resp.status}): {data.get('error', {}).get('message', 'Unknown')}")
                
                result = await resp.json()
                candidate = result['candidates'][0]
                content = candidate['content']
                
                # 응답 데이터 보안 필터링 및 thoughtSignature 유지
                for part in content.get('parts', []):
                    if 'text' in part:
                        part['text'] = SecurityChecker.filter_sensitive_data(part['text'])
                
                return content

    def _validate_code(self, code):
        """Python 문법 및 보안 검사"""
        # 1. 명령어 보안 검사
        is_safe, msg = SecurityChecker.is_safe_command(code)
        if not is_safe:
            return False, msg

        # 2. 문법 검사
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode='w', encoding='utf-8') as tf:
            tf.write(code)
            temp_name = tf.name
        
        try:
            py_compile.compile(temp_name, doraise=True)
            return True, None
        except py_compile.PyCompileError as e:
            return False, str(e)
        finally:
            if os.path.exists(temp_name):
                os.remove(temp_name)

    async def run_a2a(self, request, progress_callback=None, save_path=None, context=""):
        """
        A2A Workflow: Manager (Design) -> Coder (Code) -> Validator -> Self-Correction
        A2A_TIERS 기반 동적 모델 라우팅 적용
        """
        
        # 1. Manager Phase (Pro Model 사용)
        if progress_callback: await progress_callback(f"🔍 Manager: 설계 및 아키텍처 구성 중... (Model: {A2A_TIERS['MANAGER']})")
        
        manager_schema = {
            "type": "object",
            "properties": {
                "architecture": {"type": "string"},
                "logic_flow": {"type": "array", "items": {"type": "string"}},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"}
                        },
                        "required": ["name", "purpose"]
                    }
                }
            },
            "required": ["architecture", "logic_flow", "files"]
        }

        manager_instruction = f"""
        ROLE: Senior System Architect (Manager Agent)
        TASK: Analyze user request and provide a technical design.
        CONTEXT: {context}
        RULES:
        - Output MUST be valid JSON according to schema.
        - SECURITY: All files MUST be created within the './workspace/' directory.
        """
        
        manager_contents = [{
            "role": "user",
            "parts": [{"text": f"{manager_instruction}\n\nINPUT: {SecurityChecker.filter_sensitive_data(request)}"}]
        }]
        
        manager_response = await self._ask_gemini(
            manager_contents, 
            model_name=A2A_TIERS["MANAGER"], 
            thinking_level="high", 
            response_schema=manager_schema
        )
        design_json_raw = manager_response['parts'][0]['text']
        design = json.loads(design_json_raw)

        # 2. Coder Phase (Flash Model 사용)
        coder_schema = {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "explanation": {"type": "string"}
            },
            "required": ["code", "explanation"]
        }

        coder_instruction = f"""
        ROLE: Expert Python Developer (Coder Agent)
        TASK: Implement the following design into a single, executable Python script.
        CONTEXT: {context}
        DESIGN: {json.dumps(design)}
        RULES:
        - Output MUST be valid JSON according to schema.
        - SECURITY: Use 'os.getenv()' for all IDs, tokens, and secrets. NEVER hardcode them.
        - SECURITY: To load .env, use 'from dotenv import load_dotenv; load_dotenv("/data/data/com.termux/files/home/dev_pjt/my_butler/.env")'.
        - DISCORD API: Use the Local API to send results to Discord.
          URL: 'http://localhost:5000/send' (POST)
          Payload: {{"channel_id": int(os.getenv("CHAT_CHANNEL_ID")), "content": "Your result message"}}
          Note: ALWAYS include the final result or status in the Discord message.
        - SECURITY: DO NOT use dangerous commands (rm -rf, etc.).
        - SECURITY: All code MUST be designed to run from the './workspace/' directory.
        """

        # Coder phase history 초기화
        coder_history = [{
            "role": "user",
            "parts": [{"text": f"{coder_instruction}\n\nINPUT: {SecurityChecker.filter_sensitive_data(request)}"}]
        }]

        current_attempt = 1
        max_retries = 3
        last_error = None
        final_code = ""

        while current_attempt <= max_retries:
            if progress_callback: 
                msg = f"💻 Coder: 코드 작성 중... (Model: {A2A_TIERS['CODER']}, 시도 {current_attempt}/{max_retries})"
                if last_error: msg += f"\n⚠️ 에러 수정 중... (Thinking: High)"
                await progress_callback(msg)

            # 에러 발생 시 히스토리에 에러 메시지 추가
            if last_error:
                coder_history.append({
                    "role": "user",
                    "parts": [{"text": f"PREVIOUS_ERROR: {last_error}\nPlease fix the error above and provide the full corrected code."}]
                })

            # Gemini 3 호출 (에러 수정 시 thinking_level="high" 사용)
            t_level = "high" if last_error else "medium"
            coder_response = await self._ask_gemini(
                coder_history, 
                model_name=A2A_TIERS["CODER"],
                thinking_level=t_level, 
                response_schema=coder_schema
            )
            
            # 모델 응답을 히스토리에 추가 (thoughtSignature 포함)
            coder_history.append(coder_response)
            
            code_json_raw = coder_response['parts'][0]['text']
            code_data = json.loads(code_json_raw)
            final_code = code_data.get("code", "")

            # 3. Validation Phase
            if progress_callback: await progress_callback("🧪 Validator: 문법 및 보안 검사 중...")
            is_valid, error = self._validate_code(final_code)

            if is_valid:
                # 4. Safe File Saving
                if save_path:
                    success, result = self.file_manager.safe_write(save_path, final_code)
                    if success:
                        if progress_callback: await progress_callback(f"✅ 작업 완료! 파일 저장: {result}")
                        return {"status": "success", "design": design, "code": final_code, "saved_at": result}
                    else:
                        last_error = result
                else:
                    if progress_callback: await progress_callback("✅ 검증 완료!")
                    return {"status": "success", "design": design, "code": final_code}
            
            last_error = error
            current_attempt += 1

        return {"status": "error", "message": f"최대 재시도 횟수 초과. 마지막 에러: {last_error}", "code": final_code}
