import subprocess
import re
import os
import time
import requests
import asyncio
from datetime import datetime

# 설정
BUTLER_API_PORT = 5000
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUB_MGR_PATH = os.path.join(os.path.dirname(PROJECT_ROOT), "subscription-manager")
URL_CACHE_FILE = os.path.join(PROJECT_ROOT, "data", "tunnel_url.txt")
DISCORD_WEBHOOK_URL = None # 필요시 추가 가능하지만, 여기선 Butler API를 사용합니다.

def get_current_stored_url():
    if os.path.exists(URL_CACHE_FILE):
        with open(URL_CACHE_FILE, "r") as f:
            return f.read().strip()
    return ""

def update_subscription_manager_code(new_url):
    """subscription-manager의 소스 코드를 새 URL로 수정"""
    files_to_fix = [
        os.path.join(SUB_MGR_PATH, "src", "data_manager.py"),
        os.path.join(SUB_MGR_PATH, "src", "auth_manager.py")
    ]
    
    pattern = r'BUTLER_API_URL = "https://.*\.trycloudflare\.com"'
    replacement = f'BUTLER_API_URL = "{new_url}"'
    
    updated = False
    for file_path in files_to_fix:
        if not os.path.exists(file_path): continue
        
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        if re.search(pattern, content):
            new_content = re.sub(pattern, replacement, content)
            if content != new_content:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"✅ Updated: {file_path}")
                updated = True
    return updated

def git_push_changes():
    """수정된 코드를 GitHub에 Push"""
    try:
        subprocess.run(["git", "add", "."], cwd=SUB_MGR_PATH, check=True)
        subprocess.run(["git", "commit", "-m", f"fix: auto-update tunnel url ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"], cwd=SUB_MGR_PATH, check=True)
        subprocess.run(["git", "push"], cwd=SUB_MGR_PATH, check=True)
        print("🚀 Git Push Success!")
        return True
    except Exception as e:
        print(f"❌ Git Push Failed: {e}")
        return False

def notify_via_butler(message):
    """Butler를 통해 디스코드에 알림 전송"""
    try:
        # Butler의 Flask API를 사용하여 메시지 전송
        requests.post("http://localhost:5000/send", json={"content": message}, timeout=5)
    except:
        print("Discord notification failed (Butler might be offline)")

def run_tunnel():
    print("📡 Starting Cloudflare Tunnel...")
    # cloudflared 실행 (stderr에 로그가 찍히므로 stderr를 캡처)
    process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{BUTLER_API_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    tunnel_url = ""
    start_time = time.time()
    
    # 출력 내용을 한 줄씩 읽으며 URL 탐색
    for line in iter(process.stdout.readline, ""):
        print(line.strip())
        match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
        if match:
            tunnel_url = match.group(0)
            print(f"\n✨ New Tunnel URL Detected: {tunnel_url}")
            
            old_url = get_current_stored_url()
            if tunnel_url != old_url:
                # 1. URL 저장
                with open(URL_CACHE_FILE, "w") as f:
                    f.write(tunnel_url)
                
                # 2. 코드 수정 및 Push
                if update_subscription_manager_code(tunnel_url):
                    git_push_changes()
                    notify_via_butler(f"🔗 **터널 주소 변경 감지**\n새 주소: {tunnel_url}\n웹 코드 수정 및 GitHub Push 완료! (약 1분 후 반영)")
            break
        
        # 30초 동안 못 찾으면 재시도
        if time.time() - start_time > 30:
            print("Wait timeout. Restarting...")
            process.terminate()
            return False

    # 프로세스가 종료될 때까지 대기
    process.wait()
    return True

if __name__ == "__main__":
    while True:
        try:
            run_tunnel()
        except Exception as e:
            print(f"Error: {e}")
        print("Restarting tunnel in 10 seconds...")
        time.sleep(10)
