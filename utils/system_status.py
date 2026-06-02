import os
import json
import discord
import re
import shutil
import threading
import time
from datetime import datetime

# 글로벌 캐시 저장소
_status_cache = {
    "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
    "memory": {"used": 0, "total": 0, "percentage": 0},
    "cpu": {"percentage": 0},
    "storage": {"used": "0", "total": "0", "percentage": 0},
    "status": "Initializing...",
    "last_updated": None
}

def get_system_status_embed():
    # 1. 배터리 정보
    raw_batt = os.popen('termux-battery-status').read()
    # 2. RAM 정보 (MB 단위)
    raw_mem = os.popen('free -m').read()
    
    embed = discord.Embed(title="📱 S9 서버 시스템 상태", color=discord.Color.blue(), timestamp=datetime.now())
    
    try:
        if raw_batt:
            batt_data = json.loads(raw_batt)
            perc = batt_data.get('percentage')
            temp = batt_data.get('temperature')
            status = batt_data.get('status')
            embed.add_field(name="🔋 배터리", value=f"{perc}% ({status})", inline=True)
            embed.add_field(name="🌡️ 온도", value=f"{temp}°C", inline=True)
            
        if raw_mem:
            lines = raw_mem.split('\n')
            if len(lines) > 1:
                mem_info = lines[1].split()
                total = mem_info[1]
                used = mem_info[2]
                embed.add_field(name="🧠 RAM 사용량", value=f"{used} / {total} MB", inline=True)
        
        return embed
    except Exception as e:
        print(f"상태 정보 파싱 오류: {e}")
        return None

def _collect_system_data():
    """실제 데이터를 수집하는 내부 함수"""
    new_data = {
        "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
        "memory": {"used": 0, "total": 0, "percentage": 0},
        "cpu": {"percentage": 0},
        "storage": {"used": "0", "total": "0", "percentage": 0},
        "status": "Healthy"
    }

    results = {}
    
    def run_cmd(key, cmd):
        try:
            results[key] = os.popen(cmd).read()
        except:
            results[key] = ""

    # 병렬 실행을 위한 쓰레드 정의 (top 명령어를 더 가볍게 수정)
    commands = {
        "batt": "termux-battery-status",
        "mem": "free -m",
        "cpu": "top -n 1 -b -d 0.1 | head -n 10",
        "disk": "df -h /storage/emulated/0"
    }

    threads = []
    for k, v in commands.items():
        t = threading.Thread(target=run_cmd, args=(k, v))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=3)

    try:
        # 1. Battery 파싱
        raw_batt = results.get("batt")
        if raw_batt:
            bj = json.loads(raw_batt)
            new_data["battery"] = {
                "percentage": bj.get('percentage', 0),
                "temperature": bj.get('temperature', 0),
                "status": bj.get('status', 'Unknown')
            }
        
        # 2. RAM 파싱
        raw_mem = results.get("mem")
        if raw_mem:
            for line in raw_mem.split('\n'):
                if 'Mem:' in line:
                    p = line.split()
                    if len(p) >= 3:
                        total_mb = int(p[1])
                        used_mb = int(p[2])
                        new_data["memory"] = {
                            "total": total_mb,
                            "used": used_mb,
                            "percentage": round((used_mb / total_mb) * 100, 1)
                        }
                    break

        # 3. CPU 파싱
        raw_top = results.get("cpu")
        if raw_top:
            cpu_sum = 0
            # User/System 퍼센트 추출
            m1 = re.search(r'User\s+(\d+)%,\s+System\s+(\d+)%', raw_top, re.I)
            m2 = re.search(r'(\d+)%\s+user,\s+(\d+)%\s+sys', raw_top, re.I)
            if m1:
                cpu_sum = int(m1.group(1)) + int(m1.group(2))
            elif m2:
                cpu_sum = int(m2.group(1)) + int(m2.group(2))
            new_data["cpu"]["percentage"] = cpu_sum if cpu_sum > 0 else 5

        # 4. Storage 파싱
        raw_disk = results.get("disk")
        if raw_disk:
            disk_lines = raw_disk.strip().split('\n')
            if len(disk_lines) > 1:
                parts = disk_lines[1].split()
                try:
                    s_val = float(parts[1].replace('G', ''))
                    u_val = float(parts[2].replace('G', ''))
                    # S9 실제 용량에 맞게 보정 (더 정확한 계산 필요시 수정)
                    new_data["storage"] = {
                        "total": f"{int(s_val)}G",
                        "used": f"{int(u_val)}G",
                        "percentage": int((u_val / s_val) * 100)
                    }
                except:
                    pass

    except Exception as e:
        new_data["status"] = f"Error: {str(e)}"
        print(f"Status parsing error: {e}")
        
    return new_data

def _update_cache_loop():
    """백그라운드에서 주기적으로 캐시를 업데이트"""
    global _status_cache
    while True:
        try:
            new_stats = _collect_system_data()
            new_stats["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _status_cache = new_stats
        except Exception as e:
            print(f"Cache update loop error: {e}")
        time.sleep(15) # 15초 간격으로 업데이트

# 모듈 로드 시 백그라운드 쓰레드 시작
threading.Thread(target=_update_cache_loop, daemon=True).start()

def get_system_status_data():
    """캐시된 데이터를 즉시 반환 (웹 대시보드 속도 향상)"""
    return _status_cache

def get_battery_short_report():
    # 캐시된 데이터를 활용하여 속도 개선
    data = _status_cache.get("battery", {})
    if data.get("percentage") != 0:
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C"
    
    # 캐시가 없으면 직접 조회
    raw_res = os.popen('termux-battery-status').read()
    try:
        bj = json.loads(raw_res)
        return f"📊 **S9 배터리**: {bj.get('percentage')}% | {bj.get('temperature')}°C"
    except:
        return "❌ 분석 실패"

