import subprocess
import re
import os
import time
import requests
import asyncio
import threading
from datetime import datetime
from dotenv import load_dotenv

# 설정
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

BUTLER_API_PORT = 5000
SUB_MGR_PATH = os.path.join(os.path.dirname(PROJECT_ROOT), "subscription-manager")
URL_CACHE_FILE = os.path.join(PROJECT_ROOT, "data", "tunnel_url.txt")
DISCORD_WEBHOOK_URL = None # 필요시 추가 가능하지만, 여기선 Butler API를 사용합니다.

def get_current_stored_url():
    if os.path.exists(URL_CACHE_FILE):
        with open(URL_CACHE_FILE, "r") as f:
            return f.read().strip()
    return ""

def update_subscription_manager_code(new_url):
    """subscription-manager의 소스 코드를 새 URL로 수정 및 빌드 트리거 생성"""
    config_path = os.path.join(SUB_MGR_PATH, "src", "config.py")
    trigger_path = os.path.join(SUB_MGR_PATH, "reboot_trigger.txt")
    api_token = os.getenv("BUTLER_API_TOKEN", "butler_v3_secret_2026")
    
    try:
        # 1. src/config.py 업데이트
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(f'BUTLER_API_URL = "{new_url}"\n')
            f.write(f'BUTLER_API_TOKEN = "{api_token}"\n')
        print(f"✅ Updated API URL and Token in: {config_path}")
            
        # 2. reboot_trigger.txt 업데이트 (Streamlit Cloud 강제 갱신 유도)
        with open(trigger_path, "w", encoding="utf-8") as f:
            f.write(f"Force Reboot Trigger\nLast URL Change: {new_url}\nTimestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        print(f"🚀 Created rebuild trigger: {trigger_path}")
        
        return True
    except Exception as e:
        print(f"❌ Failed to update subscription-manager code: {e}")
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
    """Butler를 통해 디스코드에 알림 전송 (Flask API 호출)"""
    try:
        # 상태 확인 채널 ID 가져오기 (기본값 0)
        status_channel_id = int(os.getenv("STATUS_CHANNEL_ID", 0))
        api_token = os.getenv("BUTLER_API_TOKEN", "butler_v3_secret_2026")
        
        # S9의 실제 로컬 IP를 사용하여 통신 안정성 확보
        url = "http://172.30.1.5:5000/send"
        payload = {
            "channel_id": status_channel_id,
            "content": message
        }
        headers = {
            "X-Butler-Token": api_token
        }
        # timeout을 짧게 설정하여 메인 루프 지연 방지
        response = requests.post(url, json=payload, headers=headers, timeout=5)
        if response.status_code == 200:
            print(f"✅ Discord notification sent to {status_channel_id}: {message[:30]}...")
            return True
        else:
            print(f"⚠️ Discord notification failed (HTTP {response.status_code}): {response.text}")
    except Exception as e:
        print(f"❌ Discord notification error: {e}")
    return False

def run_tunnel():
    print("📡 Starting Cloudflare Tunnel...")
    # cloudflared 실행 (0.0.0.0으로 바인딩하여 모든 인터페이스에서 접근 가능하도록 함)
    process = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://172.30.1.5:{BUTLER_API_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    tunnel_url = ""
    
    def monitor_stdout():
        nonlocal tunnel_url
        for line in iter(process.stdout.readline, ""):
            line_str = line.strip()
            if line_str:
                print(f"[cloudflared] {line_str}")
            
            if not tunnel_url:
                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if match:
                    tunnel_url = match.group(0)
    
    # 별도 쓰레드에서 로그 상시 모니터링 (파이프 버퍼 누적 방지)
    threading.Thread(target=monitor_stdout, daemon=True).start()

    # 1. URL 감지 대기 (최대 30초)
    start_time = time.time()
    while not tunnel_url:
        if time.time() - start_time > 30:
            print("❌ Wait timeout for URL detection. Restarting...")
            process.terminate()
            return False
        time.sleep(1)

    print(f"\n✨ Current Tunnel URL Detected: {tunnel_url}")
    
    # 2. URL 변경 시 업데이트 및 알림
    old_url = get_current_stored_url()
    if tunnel_url != old_url:
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
        update_subscription_manager_code(tunnel_url)
        notify_via_butler(f"✅ **터널 연결 확인**\n현재 주소: {tunnel_url}\n상태: 정상 가동 중 (변동 없음)")

    # 3. Health Check 루프 (터널 생존 확인)
    fail_count = 0
    while process.poll() is None:
        try:
            # 60초마다 URL 접속 확인
            time.sleep(60)
            response = requests.get(tunnel_url, timeout=10)
            if response.status_code < 500: # 500 이상은 서버 에러지만 연결은 된 것임. 404 등도 연결은 된 상태.
                if fail_count > 0:
                    print(f"💚 Health check recovered: {tunnel_url}")
                fail_count = 0
            else:
                fail_count += 1
                print(f"⚠️ Health check failed ({fail_count}/3): HTTP {response.status_code}")
        except Exception as e:
            fail_count += 1
            print(f"⚠️ Health check error ({fail_count}/3): {e}")

        if fail_count >= 3:
            print(f"🚨 Tunnel seems broken. Forcing restart...")
            notify_via_butler(f"🚨 **터널 접속 불량 감지**\n주소: {tunnel_url}\n3회 연속 응답 없음. 터널을 재시작합니다.")
            process.terminate()
            break

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
