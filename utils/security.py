import os
import re
import subprocess
from datetime import datetime

class SecurityChecker:
    """시스템 명령어 및 파일 경로 보안 검증 클래스"""
    
    # 시스템 파괴 위험이 있는 블랙리스트 명령어
    COMMAND_BLACKLIST = [
        r"\brm\b.*-rf", r"\bmkfs\b", r"\bdd\b", r"\bkill\b", r"\bpkill\b",
        r"\bchmod\s+777\b", r"\bchown\b", r"\bfind\b.*-delete",
        r":\(\)\{ :\|:& \};:",  # 포크 폭탄
        r"mv\s+.*\s+/dev/null"
    ]

    # 민감한 환경 변수 패턴 (메시지 필터링용)
    SENSITIVE_PATTERNS = [
        r"DISCORD_TOKEN", r"GEMINI_API_KEY", r"NAVER_CLIENT_SECRET",
        r"SRT_PW", r"SRT_ID"
    ]

    @staticmethod
    def is_safe_command(command):
        """명령어 보안 검증 (파일 접근은 허용하되 파괴적 명령만 차단)"""
        for pattern in SecurityChecker.COMMAND_BLACKLIST:
            if re.search(pattern, command):
                return False, f"⚠️ 보안 위험 감지: 금지된 명령어 패턴 ({pattern})"
        return True, None

    @staticmethod
    def filter_sensitive_data(text):
        """텍스트에서 민감한 정보를 마스킹 처리"""
        if not text: return text
        
        filtered_text = text
        # 1. 키워드 기반 패턴 마스킹 (KEY=VALUE)
        for key in SecurityChecker.SENSITIVE_PATTERNS:
            filtered_text = re.sub(fr"({key})[\s:=]+[^\s]+", r"\1: ********", filtered_text)
        
        # 2. 실제 토큰/키 형태 탐지 (Discord Token 등)
        # 예: MT... (디스코드 토큰 형태) 또는 AIza... (구글 API 키 형태)
        token_patterns = [
            r"[M-Q][a-zA-Z0-9_-]{23,25}\.[a-zA-Z0-9_-]{5,7}\.[a-zA-Z0-9_-]{27,35}", # Discord Token
            r"AIza[0-9A-Za-z-_]{35}" # Google API Key
        ]
        for pattern in token_patterns:
            filtered_text = re.sub(pattern, "********", filtered_text)
            
        return filtered_text

class FileManager:
    """디렉토리 권한 및 파일 조작 관리 클래스"""
    
    def __init__(self, workspace_path):
        self.workspace_path = os.path.abspath(workspace_path)
        # 환경에 따른 홈 디렉토리 자동 감지 (Termux/Linux vs Windows)
        self.root_path = os.path.expanduser("~")

    def is_in_workspace(self, path):
        """경로가 workspace 내부에 있는지 확인"""
        abs_path = os.path.abspath(path)
        return abs_path.startswith(self.workspace_path)

    def safe_write(self, filename, content):
        """workspace 내부에서만 파일 쓰기 허용"""
        full_path = os.path.join(self.workspace_path, filename)
        if not self.is_in_workspace(full_path):
            return False, f"❌ 권한 거부: workspace 외부 수정 금지 ({filename})"
        
        try:
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return True, full_path
        except Exception as e:
            return False, str(e)

    def safe_read(self, path):
        """모든 경로는 읽기 허용하되, 민감 정보 필터링 적용은 상위에서 처리"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), None
        except Exception as e:
            return None, str(e)

class GitManager:
    """안전한 Git 운영을 위한 클래스 (현재 비활성화 상태)"""
    
    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.is_enabled = False # 사용자 요청으로 비활성화

    def run_git_safe(self, command_args):
        """파괴적인 명령어를 제외한 안전한 Git 실행"""
        if not self.is_enabled:
            return "🚫 Git 기능이 현재 비활성화되어 있습니다."

        # 금지 구문 체크
        forbidden = ["--force", "-f", "rebase", "reset --hard"]
        if any(arg in forbidden for arg in command_args):
            return f"❌ 파괴적인 Git 명령어 사용 금지: {command_args}"

        # 실행 전 자동 커밋 권장 로직 (Add -> Commit)
        # subprocess.run(["git", "add", "."], cwd=self.repo_path)
        # subprocess.run(["git", "commit", "-m", f"Auto-snapshot: {datetime.now()}"], cwd=self.repo_path)
        
        try:
            result = subprocess.run(["git"] + command_args, cwd=self.repo_path, capture_output=True, text=True)
            return result.stdout if result.returncode == 0 else result.stderr
        except Exception as e:
            return str(e)

# 테스트 및 유틸리티 함수
def security_check_all(command, path=None):
    """종합 보안 체크 함수"""
    is_safe, msg = SecurityChecker.is_safe_command(command)
    if not is_safe:
        return False, msg
    
    # 추가적인 로직 (예: 특정 경로 실행 제한 등)을 여기에 구현 가능
    return True, "Safe"
