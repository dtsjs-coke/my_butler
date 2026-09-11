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

# ---------------------------------------------------------------------------
# CPU(AP) 사용률
#
# [S9 실측 결과 — 2026-09-11]
#   /proc/stat      -> Permission denied (삼성 커널 SELinux 제한)
#   /proc/loadavg   -> Permission denied
#   os.getloadavg() -> bionic에 getloadavg(3)가 없어 AttributeError
#   top -n 2 -b     -> 요약 라인이 부하를 걸어도 항상 "800%cpu 0%user ... 800%idle"
#                      (toybox top도 내부적으로 /proc/stat에 의존하므로 무의미)
#   /proc/<pid>/stat-> 읽기 가능. busy loop 1초 측정 시 99.0%로 정확히 산출됨.
#
# 결론: 이 기기에서 "시스템 전체 CPU 사용률"은 권한상 취득할 방법이 없다.
#       따라서 이 지표는 취득 가능한 값인 "Butler 프로세스가 점유한 CPU"로 정의한다.
#       (기기 전체가 아니라 우리 서버 프로세스의 부하 = 모니터링 목적상 더 유용)
#       100% = 8코어 전부 점유. 1코어를 꽉 쓰면 12.5%.
# ---------------------------------------------------------------------------

# 직전 스냅샷: (프로세스 누적 cpu tick, 측정 시각)
_prev_proc_cpu = None
_CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
_CPU_CORES = os.cpu_count() or 1

def _read_proc_cpu_ticks():
    """자기 프로세스(모든 쓰레드 합)의 누적 CPU tick(utime+stime)을 읽는다."""
    with open('/proc/self/stat', 'r') as f:
        raw = f.read()
    # comm 필드에 공백/괄호가 들어갈 수 있으므로 마지막 ')' 뒤부터 자른다.
    fields = raw.rsplit(')', 1)[1].split()
    # rsplit 이후 인덱스: 0=state(3번째 필드) 이므로 utime(14번째)=11, stime(15번째)=12
    return int(fields[11]) + int(fields[12])

def _update_cpu():
    global _prev_proc_cpu
    try:
        cur_ticks = _read_proc_cpu_ticks()
        now = time.time()
        prev = _prev_proc_cpu
        _prev_proc_cpu = (cur_ticks, now)
        if prev:
            d_ticks = cur_ticks - prev[0]
            d_wall = now - prev[1]
            if d_wall > 0 and d_ticks >= 0:
                _log_once("cpu", "")
                pct = (d_ticks / _CLK_TCK) / (d_wall * _CPU_CORES) * 100.0
                return {"percentage": round(max(0.0, min(100.0, pct)), 1)}
        # 첫 수집은 비교 대상이 없으므로 직전 값 유지 (다음 주기부터 정상)
        return _status_cache["cpu"]
    except Exception as e:
        _log_once("cpu", f"/proc/self/stat 읽기 실패: {type(e).__name__}: {e}")
    return _status_cache["cpu"]

def _update_storage():
    """Termux 홈이 속한 /data 파티션 사용량.

    [S9 실측 — 2026-09-11] df -h 결과 231G / 13G used / 217G avail / Use% 6%
    파이썬 shutil 값과 동일하므로 수집 로직 자체에는 버그가 없었다.
    다만 표시가 `//(1024**3)` 정수 절삭이라 230G(실제 230.7G), 5%(실제 5.7%)로
    df보다 한 단위씩 작게 보였던 부분만 반올림/소수 1자리로 보정한다.
    """
    try:
        u = shutil.disk_usage("/data/data/com.termux/files/home")
        return {
            "total": f"{u.total/(1024**3):.1f}G",
            "used": f"{u.used/(1024**3):.1f}G",
            "percentage": round((u.used / u.total) * 100, 1),
        }
    except Exception as e:
        _log_once("storage", f"disk_usage 실패: {type(e).__name__}: {e}")
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
