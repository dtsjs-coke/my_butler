import os
import json
import discord
import re
import shutil
import threading
import time
import subprocess
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

# CPU 계산을 위한 이전 상태 저장
_last_cpu_info = {"total": 0, "idle": 0}

def get_system_status_embed():
    """캐시된 데이터를 사용하여 즉시 임베드 생성 (속도 대폭 개선)"""
    data = _status_cache
    embed = discord.Embed(title="📱 S9 서버 시스템 상태", color=discord.Color.blue(), timestamp=datetime.now())
    
    try:
        # 1. 배터리
        batt = data.get("battery", {})
        perc = batt.get('percentage', 0)
        temp = batt.get('temperature', 0)
        status = batt.get('status', 'Unknown')
        embed.add_field(name="🔋 배터리", value=f"{perc}% ({status})", inline=True)
        embed.add_field(name="🌡️ 온도", value=f"{temp}°C", inline=True)
            
        # 2. RAM
        mem = data.get("memory", {})
        used = mem.get('used', 0)
        total = mem.get('total', 0)
        embed.add_field(name="🧠 RAM 사용량", value=f"{used} / {total} MB ({mem.get('percentage', 0)}%)", inline=True)
        
        # 3. CPU & Storage (추가 정보)
        embed.add_field(name="⚡ CPU 사용량", value=f"{data.get('cpu', {}).get('percentage', 0)}%", inline=True)
        storage = data.get("storage", {})
        embed.add_field(name="💾 저장 공간", value=f"{storage.get('percentage', 0)}% ({storage.get('used', '0')} / {storage.get('total', '0')})", inline=True)
        
        if data.get("last_updated"):
            embed.set_footer(text=f"최근 갱신: {data['last_updated']}")
            
        return embed
    except Exception as e:
        print(f"임베드 생성 오류: {e}")
        return None

def _get_cpu_usage():
    """/proc/stat을 읽어 CPU 사용율 계산 (가장 빠름)"""
    global _last_cpu_info
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
        if not line.startswith('cpu '):
            return 0
        
        parts = list(map(int, line.split()[1:]))
        # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
        idle = parts[3] + parts[4]
        total = sum(parts)
        
        diff_idle = idle - _last_cpu_info["idle"]
        diff_total = total - _last_cpu_info["total"]
        
        _last_cpu_info = {"total": total, "idle": idle}
        
        if diff_total == 0: return 0
        usage = round(100 * (1 - (diff_idle / diff_total)), 1)
        return max(0, min(100, usage))
    except:
        return 5 # 실패 시 기본값

def _get_memory_info():
    """/proc/meminfo를 읽어 RAM 정보 획득 (무부하)"""
    try:
        mem_info = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem_info[parts[0].strip()] = int(parts[1].split()[0])
        
        total_kb = mem_info.get('MemTotal', 0)
        avail_kb = mem_info.get('MemAvailable', mem_info.get('MemFree', 0) + mem_info.get('Cached', 0))
        used_kb = total_kb - avail_kb
        
        total_mb = total_kb // 1024
        used_mb = used_kb // 1024
        
        return {
            "total": total_mb,
            "used": used_mb,
            "percentage": round((used_mb / total_mb) * 100, 1) if total_mb > 0 else 0
        }
    except:
        return {"used": 0, "total": 0, "percentage": 0}

def _get_battery_info():
    """시스템 파일 또는 termux-api를 통해 배터리 정보 획득"""
    # 1. 시스템 파일 시도 (Root 권한 없어도 읽기 가능한 경우 많음)
    try:
        base_path = "/sys/class/power_supply/battery"
        if os.path.exists(base_path):
            with open(f"{base_path}/capacity", "r") as f:
                perc = int(f.read().strip())
            with open(f"{base_path}/temp", "r") as f:
                temp = int(f.read().strip()) / 10.0 # 보통 0.1도 단위
            with open(f"{base_path}/status", "r") as f:
                status = f.read().strip()
            return {"percentage": perc, "temperature": temp, "status": status}
    except:
        pass

    # 2. Termux API 시도 (Timeout 설정하여 지연 방지)
    try:
        res = subprocess.run(['termux-battery-status'], capture_output=True, text=True, timeout=2)
        if res.returncode == 0:
            bj = json.loads(res.stdout)
            return {
                "percentage": bj.get('percentage', 0),
                "temperature": bj.get('temperature', 0),
                "status": bj.get('status', 'Unknown')
            }
    except:
        pass
        
    return {"percentage": 0, "temperature": 0, "status": "Unknown"}

def _get_storage_info():
    """shutil을 사용하여 저장 공간 확인 (가장 빠름)"""
    try:
        # S9의 주요 저장소 경로 시도
        paths = ["/storage/emulated/0", "/data/data/com.termux/files/home", "/"]
        path = "/"
        for p in paths:
            if os.path.exists(p):
                path = p
                break
                
        usage = shutil.disk_usage(path)
        total_gb = usage.total // (1024**3)
        used_gb = usage.used // (1024**3)
        perc = int((usage.used / usage.total) * 100)
        
        return {
            "total": f"{total_gb}G",
            "used": f"{used_gb}G",
            "percentage": perc
        }
    except:
        return {"used": "0", "total": "0", "percentage": 0}

def _collect_system_data():
    """모든 데이터를 최적화된 방식으로 수집"""
    new_data = {
        "battery": _get_battery_info(),
        "memory": _get_memory_info(),
        "cpu": {"percentage": _get_cpu_usage()},
        "storage": _get_storage_info(),
        "status": "Healthy"
    }
    return new_data

def _update_cache_loop():
    """백그라운드에서 주기적으로 캐시를 업데이트 (초기 지연 방지)"""
    global _status_cache
    # 첫 실행 시 CPU 기초 데이터 확보를 위해 한 번 미리 실행
    try:
        _collect_system_data()
        time.sleep(1) # CPU 델타 계산을 위한 간격
    except: pass

    while True:
        try:
            new_stats = _collect_system_data()
            new_stats["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _status_cache = new_stats
        except Exception as e:
            print(f"Cache update loop error: {e}")
        time.sleep(10) # 10초 간격으로 업데이트 (더 기민하게)

# 모듈 로드 시 백그라운드 쓰레드 시작
threading.Thread(target=_update_cache_loop, daemon=True).start()

def get_system_status_data():
    """캐시된 데이터를 즉시 반환 (웹 대시보드 속도 향상)"""
    return _status_cache

def get_battery_short_report():
    """캐시된 데이터를 활용하여 즉시 보고"""
    data = _status_cache.get("battery", {})
    if data.get("percentage") != 0:
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C | {data.get('status')}"
    return "⏳ 시스템 상태 수집 중..."
