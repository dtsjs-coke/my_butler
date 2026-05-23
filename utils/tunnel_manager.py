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
    file_path = os.path.join(SUB_MGR_PATH, "src", "config.py")
    
    if not os.path.exists(file_path):
        print(f"⚠️ Config file not found: {file_path}")
        return False
        
    pattern = r'BUTLER_API_URL = "https://.*\.trycloudflare\.com"'
    replacement = f'BUTLER_API_URL = "{new_url}"'
    
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    if re.search(pattern, content):
        new_content = re.sub(pattern, replacement, content)
        if content != new_content:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print(f"✅ Updated: {file_path}")
            return True
    else:
        # 패턴이 일치하지 않을 경우 (예: 초기 주소가 다를 때) 직접 쓰기 시도
        print("⚠️ Pattern match failed in config.py. Attempting direct overwrite.")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f'BUTLER_API_URL = "{new_url}"\n')
        return True
    return False

def git_push_changes(new_url):
    """수정된 코드를 GitHub에 Push (S9 자율 관리 모드)"""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    
    github_token = os.getenv("GITHUB_TOKEN")
    repo_url = "https://github.com/dtsjs-coke/subscription-manager.git"
    authenticated_url = f"https://{github_token}@github.com/dtsjs-coke/subscription-manager.git" if github_token else repo_url

    try:
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", SUB_MGR_PATH], check=False)
        
        # 1. Git 초기화 및 리모트 설정
        if not os.path.exists(os.path.join(SUB_MGR_PATH, ".git")):
            print("📦 Initializing Git repository on S9...")
            subprocess.run(["git", "init"], cwd=SUB_MGR_PATH, check=True)
            subprocess.run(["git", "remote", "add", "origin", authenticated_url], cwd=SUB_MGR_PATH, check=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=SUB_MGR_PATH, check=False)
        else:
            subprocess.run(["git", "remote", "set-url", "origin", authenticated_url], cwd=SUB_MGR_PATH, check=True)

        # 2. 동기화 (S9의 URL 정보가 우선되어야 하므로 fetch 후 reset)
        print("🔄 Fetching latest from GitHub and resetting to sync...")
        subprocess.run(["git", "fetch", "origin"], cwd=SUB_MGR_PATH, check=False)
        subprocess.run(["git", "reset", "--hard", "origin/main"], cwd=SUB_MGR_PATH, check=False)

        # 3. 소스 코드 업데이트 (Git 동기화 이후에 수행해야 덮어씌워지지 않음)
        code_updated = update_subscription_manager_code(new_url)
        if not code_updated:
            print("ℹ️ No code changes detected in config.py.")

        # 4. 변경사항 커밋
        subprocess.run(["git", "add", "."], cwd=SUB_MGR_PATH, check=True)
        commit_msg = f"fix: auto-update tunnel url ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
        res_commit = subprocess.run(["git", "commit", "-m", commit_msg], cwd=SUB_MGR_PATH, capture_output=True, text=True)
        
        # 5. Push
        print(f"🚀 Pushing updated URL ({new_url}) to GitHub...")
        res_push = subprocess.run(["git", "push", "origin", "main"], cwd=SUB_MGR_PATH, capture_output=True, text=True)
        
        if res_push.returncode != 0:
            print(f"⚠️ Regular push failed, attempting force push... Error: {res_push.stderr}")
            res_push = subprocess.run(["git", "push", "origin", "main", "--force"], cwd=SUB_MGR_PATH, capture_output=True, text=True)

        if res_push.returncode == 0:
            print("🚀 Git Push Success from S9!")
            return True
        else:
            print(f"❌ Git Push Failed: {res_push.stderr}")
            return False
    except Exception as e:
        print(f"❌ Git Operation Error: {e}")
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
            print(f"\n✨ Current Tunnel URL Detected: {tunnel_url}")
            
            old_url = get_current_stored_url()
            
            if tunnel_url != old_url:
                # URL이 바뀌었을 때만 Git 동기화 및 업데이트 진행
                git_success = git_push_changes(tunnel_url)
                
                if git_success:
                    with open(URL_CACHE_FILE, "w") as f:
                        f.write(tunnel_url)
                    status_msg = "변경 및 GitHub 업데이트 완료"
                else:
                    status_msg = "GitHub 업데이트 실패 (로그 확인 필요)"
                
                notify_via_butler(
                    f"🔗 **터널 주소 정보 업데이트**\n"
                    f"현재 주소: {tunnel_url}\n"
                    f"이전 주소: {old_url}\n"
                    f"상태: {status_msg}"
                )
            else:
                # URL은 같지만 혹시 코드가 안 맞을 수 있으므로 체크 (Push는 안 함)
                update_subscription_manager_code(tunnel_url)
                notify_via_butler(f"✅ **터널 연결 확인**\n현재 주소: {tunnel_url}\n상태: 정상 가동 중 (변동 없음)")
            
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
