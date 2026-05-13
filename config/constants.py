import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))
SRT_CHANNEL_ID = int(os.getenv("SRT_CHANNEL_ID", 0))
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", 0))
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))
CLI_CHANNEL_ID = int(os.getenv("CLI_CHANNEL_ID", 1504087135827918898))
DISCORD_ADMIN_USER_ID = int(os.getenv("DISCORD_ADMIN_USER_ID", 1451625941427159124))
NOTI_CHANNEL_ID = CHAT_CHANNEL_ID

AVAILABLE_MODELS = [
    "gemini-3.1-flash-lite-preview", # 추천: 일상 대화, 빠른 응답, 단순 요약
    "gemini-3-flash-preview",       # 추천: 일반적인 질문, 코딩 보조, 복잡한 지침 이행
    "gemini-3.1-pro-preview"        # 추천: 심층 분석, 복잡한 설계, 고도의 논리적 추론
]

# A2A 역할별 최적화 모델 티어 (무료 티어 호환성 고려)
A2A_TIERS = {
    "MANAGER": "gemini-3-flash-preview",       # 설계/아키텍처 (Flash급 지능)
    "CODER": "gemini-3-flash-preview",         # 구현/코딩 (속도와 지능 균형)
    "REVIEWER": "gemini-3.1-flash-lite-preview" # 단순 검증 (가장 빠름)
}

WORKSPACE_CONTEXT = f"""
ENVIRONMENT_INFO:
- Device: Samsung Galaxy S9 (Android)
- Terminal: Termux (Linux)
- Bot Framework: discord.py
- Local API (For standalone scripts): http://localhost:5000/send (POST, JSON: {{"channel_id": int, "content": "str"}})
- Key Channels (Access via os.getenv):
  - CHAT_CHANNEL_ID: {CHAT_CHANNEL_ID}
  - STATUS_CHANNEL_ID: {STATUS_CHANNEL_ID}
- Available Tools: termux-api, crontab, python3
- AI Capability: Gemini 3 Reasoning (Thinking Level support)
- A2A System: Role-based model routing (Pro for Manager, Flash for Coder)
- CRITICAL: Use 'os.getenv("VARIABLE_NAME")' to access all sensitive values and IDs.
- CRITICAL: DO NOT hardcode tokens, IDs, or secrets in the code.
- CRITICAL: For standalone scripts, include 'from dotenv import load_dotenv; load_dotenv()' if the .env file is in the same directory, or provide the relative path.
- CRITICAL: DO NOT use 'discord_webhook' or other external libs. Use 'requests' to call Local API.
"""
