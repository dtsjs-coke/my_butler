import os
import json
import discord
import re
import shutil
from datetime import datetime

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

import threading

def get_system_status_data():
    """S9(Termux) 환경에 최적화된 리소스 수집 (병렬 처리로 성능 개선)"""
    data = {
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

    # 병렬 실행을 위한 쓰레드 정의
    commands = {
        "batt": "termux-battery-status",
        "mem": "free -m",
        "cpu": "top -n 1 -b | head -n 20",
        "disk": "df -h /storage/emulated/0"
    }

    threads = []
    for k, v in commands.items():
        t = threading.Thread(target=run_cmd, args=(k, v))
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=2) # 최대 2초 대기

    try:
        # 1. Battery 파싱
        raw_batt = results.get("batt")
        if raw_batt:
            bj = json.loads(raw_batt)
            data["battery"] = {
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
                        data["memory"] = {
                            "total": total_mb,
                            "used": used_mb,
                            "percentage": round((used_mb / total_mb) * 100, 1)
                        }
                    break

        # 3. CPU 파싱
        raw_top = results.get("cpu")
        if raw_top:
            cpu_sum = 0
            m1 = re.search(r'User\s+(\d+)%,\s+System\s+(\d+)%', raw_top, re.I)
            m2 = re.search(r'(\d+)%\s+user,\s+(\d+)%\s+sys', raw_top, re.I)
            if m1:
                cpu_sum = int(m1.group(1)) + int(m1.group(2))
            elif m2:
                cpu_sum = int(m2.group(1)) + int(m2.group(2))
            data["cpu"]["percentage"] = cpu_sum if cpu_sum > 0 else 5

        # 4. Storage 파싱
        raw_disk = results.get("disk")
        if raw_disk:
            disk_lines = raw_disk.strip().split('\n')
            if len(disk_lines) > 1:
                parts = disk_lines[1].split()
                try:
                    s_val = float(parts[1].replace('G', ''))
                    u_val = float(parts[2].replace('G', ''))
                    total_final = s_val + 25
                    used_final = u_val + 25
                    data["storage"] = {
                        "total": f"{int(total_final)}G",
                        "used": f"{int(used_final)}G",
                        "percentage": int((used_final / total_final) * 100)
                    }
                except:
                    pass

    except Exception as e:
        data["status"] = f"Error: {str(e)}"
        print(f"Status parsing error: {e}")
        
    return data

def get_battery_short_report():
    raw_res = os.popen('termux-battery-status').read()
    try:
        data = json.loads(raw_res)
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C"
    except:
        return "❌ 분석 실패"
