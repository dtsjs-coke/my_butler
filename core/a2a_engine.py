import os
import json
import aiohttp
import asyncio
import py_compile
import tempfile
from datetime import datetime

class A2AEngine:
    def __init__(self, api_key, model_name):
        self.api_key = api_key
        self.model_name = model_name
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        self.headers = {'Content-Type': 'application/json'}

    async def _ask_gemini(self, system_instruction, user_input):
        payload = {
            "contents": [{
                "parts": [{"text": f"{system_instruction}\n\nINPUT: {user_input}"}]
            }],
            "generationConfig": {
                "response_mime_type": "application/json"
            }
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.url, headers=self.headers, json=payload) as resp:
                if resp.status != 200:
                    data = await resp.json()
                    raise Exception(f"API Error ({resp.status}): {data.get('error', {}).get('message', 'Unknown')}")
                
                result = await resp.json()
                return result['candidates'][0]['content']['parts'][0]['text']

    def _validate_code(self, code):
        """Python 문법 검사 (py_compile 활용)"""
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
        """
        # 1. Manager Phase
        if progress_callback: await progress_callback("🔍 Manager: 설계 및 아키텍처 구성 중...")
        
        manager_instruction = f"""
        ROLE: Senior System Architect (Manager Agent)
        TASK: Analyze user request and provide a technical design.
        CONTEXT: {context}
        RULES:
        - Output MUST be valid JSON.
        - No greetings or fillers.
        - JSON Structure: {{"architecture": "string", "logic_flow": ["step1", "step2"], "files": [{{"name": "string", "purpose": "string"}}]}}
        """
        
        design_json_raw = await self._ask_gemini(manager_instruction, request)
        design = json.loads(design_json_raw)

        # 2. Coder Phase (with Correction Loop)
        coder_instruction = f"""
        ROLE: Expert Python Developer (Coder Agent)
        TASK: Implement the following design into a single, executable Python script.
        CONTEXT: {context}
        DESIGN: {json.dumps(design)}
        RULES:
        - Output MUST be valid JSON.
        - Provide complete, clean, and commented Python code.
        - JSON Structure: {{"code": "string", "explanation": "string"}}
        - CRITICAL: Use the provided CONTEXT to make code immediately executable in the user's environment.
        """

        current_attempt = 1
        max_retries = 3
        last_error = None
        final_code = ""

        while current_attempt <= max_retries:
            if progress_callback: 
                msg = f"💻 Coder: 코드 작성 중... (시도 {current_attempt}/{max_retries})"
                if last_error: msg += f"\n⚠️ 이전 에러 수정 중..."
                await progress_callback(msg)

            coder_input = request
            if last_error:
                coder_input += f"\n\nPREVIOUS_SYNTAX_ERROR: {last_error}\nPlease fix the syntax error above and provide the full corrected code."

            code_json_raw = await self._ask_gemini(coder_instruction, coder_input)
            code_data = json.loads(code_json_raw)
            final_code = code_data.get("code", "")

            # 3. Validation Phase
            if progress_callback: await progress_callback("🧪 Validator: 문법 검사 중...")
            is_valid, error = self._validate_code(final_code)

            if is_valid:
                # 파일 저장 로직 추가
                if save_path:
                    with open(save_path, 'w', encoding='utf-8') as f:
                        f.write(final_code)
                    if progress_callback: await progress_callback(f"💾 파일 저장 완료: {save_path}")

                if progress_callback: await progress_callback("✅ 검증 완료!")
                return {"status": "success", "design": design, "code": final_code, "saved_at": save_path}
            
            last_error = error
            current_attempt += 1

        return {"status": "error", "message": f"최대 재시도 횟수 초과. 마지막 에러: {last_error}", "code": final_code}
