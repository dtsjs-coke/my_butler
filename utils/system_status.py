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

_last_cpu_info = {"total": 0, "idle": 0}
_cache_lock = threading.Lock()

def get_system_status_data():
    """캐시된 데이터를 즉시 반환 (Lock 최소화)"""
    with _cache_lock:
        return _status_cache.copy()

def get_system_status_embed():
    data = get_system_status_data()
    embed = discord.Embed(title="📱 S9 서버 시스템 상태", color=discord.Color.blue(), timestamp=datetime.now())
    try:
        batt = data.get("battery", {})
        embed.add_field(name="🔋 배터리", value=f"{batt.get('percentage', 0)}% ({batt.get('status', 'Unknown')})", inline=True)
        embed.add_field(name="🌡️ 온도", value=f"{batt.get('temperature', 0)}°C", inline=True)
        mem = data.get("memory", {})
        embed.add_field(name="🧠 RAM 사용량", value=f"{mem.get('used', 0)} / {mem.get('total', 0)} MB", inline=True)
        embed.add_field(name="⚡ CPU", value=f"{data.get('cpu', {}).get('percentage', 0)}%", inline=True)
        storage = data.get("storage", {})
        embed.add_field(name="💾 저장", value=f"{storage.get('percentage', 0)}%", inline=True)
        if data.get("last_updated"):
            embed.set_footer(text=f"최근 갱신: {data['last_updated']}")
        return embed
    except: return None

def _safe_run(cmd, timeout=2):
    """명령어를 안전하고 빠르게 실행"""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.stdout if res.returncode == 0 else ""
    except: return ""

def _update_battery():
    # 1. Sysfs (Fastest)
    try:
        path = "/sys/class/power_supply/battery"
        if os.path.exists(path):
            with open(f"{path}/capacity", "r") as f: p = int(f.read().strip())
            with open(f"{path}/temp", "r") as f: t = int(f.read().strip()) / 10.0
            with open(f"{path}/status", "r") as f: s = f.read().strip()
            return {"percentage": p, "temperature": t, "status": s}
    except: pass
    # 2. Termux API
    raw = _safe_run(['termux-battery-status'], timeout=1.5)
    if raw:
        try:
            bj = json.loads(raw)
            return {"percentage": bj.get('percentage', 0), "temperature": bj.get('temperature', 0), "status": bj.get('status', 'Unknown')}
        except: pass
    return _status_cache["battery"]

def _update_memory():
    # 1. Proc (Fastest)
    try:
        with open('/proc/meminfo', 'r') as f:
            m = {l.split(':')[0]: int(l.split(':')[1].split()[0]) for l in f.readlines()[:10]}
        total = m['MemTotal'] // 1024
        free = m.get('MemAvailable', m.get('MemFree', 0)) // 1024
        used = total - free
        return {"total": total, "used": used, "percentage": round((used/total)*100, 1)}
    except: pass
    # 2. free -m
    raw = _safe_run(['free', '-m'])
    if raw:
        for line in raw.split('\n'):
            if 'Mem:' in line:
                p = line.split()
                t, u = int(p[1]), int(p[2])
                return {"total": t, "used": u, "percentage": round((u/t)*100, 1)}
    return _status_cache["memory"]

def _update_cpu():
    global _last_cpu_info
    try:
        with open('/proc/stat', 'r') as f: line = f.readline()
        if line.startswith('cpu '):
            p = list(map(int, line.split()[1:]))
            idle, total = p[3] + p[4], sum(p)
            du, dt = idle - _last_cpu_info["idle"], total - _last_cpu_info["total"]
            _last_cpu_info = {"total": total, "idle": idle}
            if dt > 0: return {"percentage": max(0, min(100, round(100 * (1 - (du / dt)), 1)))}
    except: pass
    # Fallback to a very lightweight top
    raw = _safe_run(['top', '-n', '1', '-b', '-d', '0.1'], timeout=1)
    m = re.search(r'(\d+)%\s+user,\s+(\d+)%\s+sys', raw, re.I)
    if m: return {"percentage": int(m.group(1)) + int(m.group(2))}
    return {"percentage": 5}

def _update_storage():
    try:
        u = shutil.disk_usage("/data/data/com.termux/files/home")
        return {"total": f"{u.total//(1024**3)}G", "used": f"{u.used//(1024**3)}G", "percentage": int((u.used/u.total)*100)}
    except: pass
    return _status_cache["storage"]

def _worker_loop():
    global _status_cache
    print("📡 Status Worker started.")
    
    # 지연 없는 초기화
    initial_data = {
        "battery": _update_battery(),
        "memory": _update_memory(),
        "cpu": _update_cpu(),
        "storage": _update_storage(),
        "status": "Healthy",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    with _cache_lock: _status_cache = initial_data

    while True:
        try:
            # 15초마다 수집
            time.sleep(15)
            
            # 각 태스크를 개별 쓰레드로 실행하여 지연 방지
            new_data = {}
            def task(key, func): new_data[key] = func()
            
            threads = [
                threading.Thread(target=task, args=("battery", _update_battery)),
                threading.Thread(target=task, args=("memory", _update_memory)),
                threading.Thread(target=task, args=("cpu", _update_cpu)),
                threading.Thread(target=task, args=("storage", _update_storage))
            ]
            for t in threads: t.start()
            # 최대 5초 대기
            for t in threads: t.join(timeout=5)
            
            new_data["status"] = "Healthy"
            new_data["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            with _cache_lock:
                _status_cache.update(new_data)
        except Exception as e:
            print(f"Worker Error: {e}")

# 백그라운드 쓰레드 실행
threading.Thread(target=_worker_loop, daemon=True).start()

def get_battery_short_report():
    d = get_system_status_data().get("battery", {})
    return f"📊 **S9 배터리**: {d.get('percentage')}% | {d.get('temperature')}°C"
