import os
import json
import aiohttp
import asyncio
import py_compile
import tempfile
from datetime import datetime
from utils.security import SecurityChecker, FileManager
from config.constants import A2A_TIERS

# 프로젝트 루트 경로를 기준으로 workspace 경로 설정
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

class A2AEngine:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.headers = {'Content-Type': 'application/json'}
        # 보안 파일 매니저 초기화 (workspace 경로 설정)
        self.file_manager = FileManager(WORKSPACE_DIR)

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

    async def _execute_code(self, file_name):
        """작성된 코드를 실제로 실행하고 결과를 캡처"""
        try:
            # 윈도우와 리눅스(Termux) 호환성 고려
            python_cmd = 'python' if os.name == 'nt' else 'python3'
            
            process = await asyncio.create_subprocess_exec(
                python_cmd, file_name,
                cwd=WORKSPACE_DIR,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            
            if process.returncode == 0:
                return True, stdout.decode().strip()
            else:
                return False, stderr.decode().strip()
        except asyncio.TimeoutError:
            return False, "Execution Timeout (30s)"
        except Exception as e:
            return False, str(e)

    async def run_a2a(self, request, progress_callback=None, save_path="generated_task.py", context=""):
        """
        A2A Workflow: Manager -> Coder -> Validator (Syntax + Runtime) -> Self-Correction
        """
        
        # 1. Manager Phase
        if progress_callback: await progress_callback(f"🔍 Manager: 작업 분석 및 설계 중... ({A2A_TIERS['MANAGER']})")
        
        manager_schema = {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "설계 의도 및 해결 전략"},
                "architecture": {"type": "string"},
                "logic_flow": {"type": "array", "items": {"type": "string"}},
                "needed_modules": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["thought", "architecture", "logic_flow"]
        }

        manager_instruction = f"""
        ROLE: Senior System Architect
        TASK: Analyze user request and provide a technical design for a Python script.
        CONTEXT: {context}
        RULES:
        - Design for Android/Termux environment.
        - Output MUST be valid JSON.
        """
        
        manager_contents = [{"role": "user", "parts": [{"text": f"{manager_instruction}\n\nINPUT: {request}"}]}]
        manager_response = await self._ask_gemini(manager_contents, model_name=A2A_TIERS["MANAGER"], response_schema=manager_schema)
        design = json.loads(manager_response['parts'][0]['text'])

        # 2. Coder Phase with Runtime Feedback Loop
        coder_schema = {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "explanation": {"type": "string"}
            },
            "required": ["code", "explanation"]
        }

        coder_instruction = f"""
        ROLE: Expert Python Developer
        TASK: Implement the design into a single executable script.
        CONTEXT: {context}
        DESIGN: {json.dumps(design)}
        RULES:
        - Use Local API (http://localhost:5000/send) to report results to Discord.
        - Use os.getenv() for all secrets/IDs.
        - Only use standard libraries or already installed ones (requests, dotenv).
        """

        coder_history = [{"role": "user", "parts": [{"text": f"{coder_instruction}\n\nINPUT: {request}"}]}]
        current_attempt = 1
        max_retries = 3
        last_error = None

        while current_attempt <= max_retries:
            if progress_callback: 
                msg = f"💻 Coder: 코드 구현 중... (시도 {current_attempt}/{max_retries})"
                if last_error: msg += f"\n⚠️ 에러 감지! 자동 수정 시도 중..."
                await progress_callback(msg)

            if last_error:
                coder_history.append({"role": "user", "parts": [{"text": f"RUNTIME_ERROR:\n{last_error}\n\nPlease fix this error and provide the full corrected code."}]})

            coder_response = await self._ask_gemini(coder_history, model_name=A2A_TIERS["CODER"], thinking_level="high", response_schema=coder_schema)
            coder_history.append(coder_response)
            
            code_data = json.loads(coder_response['parts'][0]['text'])
            final_code = code_data.get("code", "")

            # 3. Validation Phase (Syntax)
            is_valid, error = self._validate_code(final_code)
            if not is_valid:
                last_error = f"Syntax/Security Error: {error}"
                current_attempt += 1
                continue

            # 4. Save & Runtime Validation
            self.file_manager.safe_write(save_path, final_code)
            
            if progress_callback: await progress_callback("🧪 Runner: 코드 실행 및 결과 검증 중...")
            success, result = await self._execute_code(save_path)

            if success:
                if progress_callback: await progress_callback(f"✅ 작업 완료! 실행 결과: {result[:100]}...")
                return {"status": "success", "design": design, "code": final_code, "output": result, "saved_at": save_path}
            else:
                last_error = f"Runtime Error: {result}"
                current_attempt += 1

        return {"status": "error", "message": f"최대 재시도 초과. 마지막 에러: {last_error}", "code": final_code}

