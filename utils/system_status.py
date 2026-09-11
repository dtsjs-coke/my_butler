import os
import json
import discord
import re
import shutil
import threading
import time
import subprocess
import collections
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

# 최근 10분(10초 주기 x 60개) 추이 — 스파크라인용 슬림 스냅샷만 보관
_history = collections.deque(maxlen=60)

_cache_lock = threading.Lock()

def get_system_status_data():
    """캐시된 데이터를 즉각 반환"""
    with _cache_lock:
        return _status_cache.copy()

def get_system_status_history():
    """최근 추이(배터리/RAM/CPU/저장공간 %) 스냅샷 목록을 오래된 순으로 반환"""
    with _cache_lock:
        return list(_history)

# 수집 실패 사유를 기록해두는 저장소 (같은 실패가 반복될 때 로그 폭주 방지용)
_last_fail = {}

def _log_once(key, msg):
    """실패 상태가 '바뀔 때만' 로그를 남긴다.
    - msg가 비어있지 않으면: 실패 로그 (직전과 사유가 같으면 조용히 무시)
    - msg가 비어있으면: 직전에 실패 중이었을 때만 '정상화' 로그
    """
    prev = _last_fail.get(key)
    if prev == msg:
        return
    _last_fail[key] = msg
    if msg:
        print(f"[SystemStatus] {key} 수집 실패: {msg}")
    elif prev:
        print(f"[SystemStatus] {key} 수집 정상화")

def _safe_run(cmd, timeout=1.5):
    key = cmd[0]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            _log_once(key, f"exit={res.returncode} stderr={(res.stderr or '').strip()[:200]}")
            return ""
        _log_once(key, "")
        return res.stdout
    except subprocess.TimeoutExpired:
        _log_once(key, f"{timeout}초 타임아웃 (Termux:API 앱 응답 지연 가능성)")
        return ""
    except FileNotFoundError:
        _log_once(key, "명령어를 찾을 수 없음 (termux-api 패키지 미설치 또는 PATH 누락)")
        return ""
    except Exception as e:
        _log_once(key, f"{type(e).__name__}: {e}")
        return ""

def _update_battery():
    # S9에서 /sys는 막혀있으므로 바로 API 호출.
    # termux-battery-status는 Termux:API 앱에 브로드캐스트를 보내고 응답을 기다리는 구조라
    # 구형 기기에서는 2초를 넘기는 경우가 잦다. (기존 2.0초 -> 4.0초)
    # 워커의 join(timeout=8.0)보다 작아야 하므로 4.0초로 제한.
    raw = _safe_run(['termux-battery-status'], timeout=4.0)
    if raw:
        try:
            bj = json.loads(raw)
            _log_once("battery-parse", "")
            return {"percentage": bj.get('percentage', 0), "temperature": bj.get('temperature', 0), "status": bj.get('status', 'Unknown')}
        except Exception as e:
            _log_once("battery-parse", f"JSON 파싱 실패: {type(e).__name__} raw={raw.strip()[:200]}")
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

# /proc/stat 직전 스냅샷 (idle_ticks, total_ticks)
_prev_cpu_ticks = None

def _read_cpu_ticks():
    """/proc/stat 첫 줄(전체 CPU 합계)에서 (idle, total) 누적 tick을 읽는다."""
    with open('/proc/stat', 'r') as f:
        line = f.readline()
    parts = line.split()
    if not parts or parts[0] != 'cpu':
        return None
    vals = [int(v) for v in parts[1:] if v.isdigit()]
    if len(vals) < 4:
        return None
    # 0:user 1:nice 2:system 3:idle 4:iowait ...
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)

def _update_cpu():
    """CPU(AP) 사용률.

    기존에는 top 출력을 파싱했으나, `raw.replace(' ', '')`로 공백을 모두 제거한 뒤
    `\\s+`를 요구하는 정규식이라 절대 매칭될 수 없었고(=항상 실패),
    결국 loadavg 또는 하드코딩 5%가 표시되고 있었다.
    Android/Termux에서 안정적으로 읽히는 /proc/stat 델타 방식으로 교체한다.
    """
    global _prev_cpu_ticks
    try:
        cur = _read_cpu_ticks()
        if cur:
            prev = _prev_cpu_ticks
            _prev_cpu_ticks = cur
            if prev:
                d_idle = cur[0] - prev[0]
                d_total = cur[1] - prev[1]
                if d_total > 0:
                    _log_once("cpu", "")
                    pct = (1.0 - (d_idle / d_total)) * 100.0
                    return {"percentage": int(round(max(0.0, min(100.0, pct))))}
            # 첫 수집은 비교 대상이 없으므로 직전 값을 유지 (다음 주기부터 정상)
            return _status_cache["cpu"]
    except Exception as e:
        _log_once("cpu", f"/proc/stat 읽기 실패: {type(e).__name__}: {e}")

    # 대안: loadavg를 코어 수로 정규화
    try:
        with open('/proc/loadavg', 'r') as f:
            load = float(f.readline().split()[0])
        cores = os.cpu_count() or 1
        return {"percentage": int(round(min(100.0, (load / cores) * 100.0)))}
    except Exception:
        pass
    return _status_cache["cpu"]

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
            # 배터리 수집(최대 4초)보다 넉넉하게 대기
            for t in threads: t.join(timeout=8.0)

            new_data["status"] = "Healthy"
            new_data["last_updated"] = datetime.now().strftime("%H:%M:%S")

            with _cache_lock:
                _status_cache.update(new_data)
                # 주의: 수집 쓰레드가 시간 초과되면 new_data에 해당 키가 아예 없어서
                # 예전 코드는 추이 그래프에 0이 찍혔다. 병합된 캐시 기준으로 기록한다.
                _history.append({
                    "t": _status_cache["last_updated"],
                    "battery": _status_cache.get("battery", {}).get("percentage", 0),
                    "memory": _status_cache.get("memory", {}).get("percentage", 0),
                    "cpu": _status_cache.get("cpu", {}).get("percentage", 0),
                    "storage": _status_cache.get("storage", {}).get("percentage", 0),
                })
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
