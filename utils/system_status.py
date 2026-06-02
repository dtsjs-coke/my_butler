import os
import json
import discord
import re
import shutil
import threading
import time
import subprocess
from datetime import datetime

# 글로벌 캐시 저장소 (초기값 설정)
_status_cache = {
    "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
    "memory": {"used": 0, "total": 0, "percentage": 0},
    "cpu": {"percentage": 0},
    "storage": {"used": "0", "total": "0", "percentage": 0},
    "status": "Initializing...",
    "last_updated": None
}

_cache_lock = threading.Lock()

def get_system_status_data():
    """캐시된 데이터를 즉각 반환"""
    with _cache_lock:
        return _status_cache.copy()

def _safe_run(cmd, timeout=1.5):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except: return ""

def _update_battery():
    # S9에서 /sys는 막혀있으므로 바로 API 호출 (타임아웃 강화)
    raw = _safe_run(['termux-battery-status'], timeout=2.0)
    if raw:
        try:
            bj = json.loads(raw)
            return {"percentage": bj.get('percentage', 0), "temperature": bj.get('temperature', 0), "status": bj.get('status', 'Unknown')}
        except: pass
    return _status_cache["battery"]

def _update_memory():
    # S9에서 /proc/meminfo는 작동함 (매우 빠름)
    try:
        m = {}
        with open('/proc/meminfo', 'r') as f:
            for line in f:
                parts = line.split(':')
                if len(parts) == 2: m[parts[0].strip()] = int(parts[1].split()[0])
        total = m['MemTotal'] // 1024
        avail = m.get('MemAvailable', m.get('MemFree', 0) + m.get('Cached', 0)) // 1024
        used = total - avail
        return {"total": total, "used": used, "percentage": round((used/total)*100, 1)}
    except: pass
    return _status_cache["memory"]

def _update_cpu():
    # S9의 top 명령어 포맷에 맞춘 파싱
    raw = _safe_run(['top', '-n', '1', '-b'], timeout=1.5)
    if raw:
        # "800%cpu   10%user   5%sys" 형태 파싱
        m = re.search(r'(\d+)%user\s+(\d+)%nice\s+(\d+)%sys', raw.replace(' ', ''))
        if m:
            return {"percentage": int(m.group(1)) + int(m.group(3))}
        # 다른 형태 시도 (Tasks: ... Mem: ...)
        m2 = re.search(r'(\d+)%user,\s+(\d+)%sys', raw, re.I)
        if m2:
            return {"percentage": int(m2.group(1)) + int(m2.group(2))}
    
    # 마지막 대안: loadavg
    try:
        with open('/proc/loadavg', 'r') as f:
            load = float(f.readline().split()[0])
            return {"percentage": min(100, int(load * 10))}
    except: pass
    return {"percentage": 5}

def _update_storage():
    try:
        u = shutil.disk_usage("/data/data/com.termux/files/home")
        return {"total": f"{u.total//(1024**3)}G", "used": f"{u.used//(1024**3)}G", "percentage": int((u.used/u.total)*100)}
    except: pass
    return _status_cache["storage"]

def _worker_loop():
    global _status_cache
    print("🔋 S9 Status Worker Active.")
    
    while True:
        try:
            # 병렬 수집 (각각 독립 쓰레드)
            new_data = {}
            def t_wrap(k, f): new_data[k] = f()
            
            threads = [
                threading.Thread(target=t_wrap, args=("battery", _update_battery)),
                threading.Thread(target=t_wrap, args=("memory", _update_memory)),
                threading.Thread(target=t_wrap, args=("cpu", _update_cpu)),
                threading.Thread(target=t_wrap, args=("storage", _update_storage))
            ]
            for t in threads: t.start()
            for t in threads: t.join(timeout=5.0)
            
            new_data["status"] = "Healthy"
            new_data["last_updated"] = datetime.now().strftime("%H:%M:%S")
            
            with _cache_lock:
                _status_cache.update(new_data)
        except Exception as e:
            print(f"Worker Error: {e}")
        
        time.sleep(10) # 10초마다 갱신

# 즉시 시작
threading.Thread(target=_worker_loop, daemon=True).start()

def get_system_status_embed():
    from utils.system_status import get_system_status_data
    data = get_system_status_data()
    embed = discord.Embed(title="📱 S9 서버 시스템 상태", color=discord.Color.blue(), timestamp=datetime.now())
    batt = data.get("battery", {})
    embed.add_field(name="🔋 배터리", value=f"{batt.get('percentage')}% ({batt.get('status')})", inline=True)
    embed.add_field(name="🌡️ 온도", value=f"{batt.get('temperature')}°C", inline=True)
    mem = data.get("memory", {})
    embed.add_field(name="🧠 RAM", value=f"{mem.get('percentage')}% ({mem.get('used')}/{mem.get('total')}MB)", inline=True)
    embed.add_field(name="⚡ CPU", value=f"{data.get('cpu', {}).get('percentage')}%", inline=True)
    embed.set_footer(text=f"Last updated: {data.get('last_updated')}")
    return embed

def get_battery_short_report():
    d = get_system_status_data().get("battery", {})
    return f"📊 **S9 배터리**: {d.get('percentage')}% | {d.get('temperature')}°C"
