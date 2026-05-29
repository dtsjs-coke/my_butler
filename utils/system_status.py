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

def get_system_status_data():
    """S9(Termux) 환경에 최적화된 리소스 수집"""
    data = {
        "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
        "memory": {"used": 0, "total": 0, "percentage": 0},
        "cpu": {"percentage": 0},
        "storage": {"used": "0", "total": "0", "percentage": 0},
        "status": "Healthy"
    }
    
    try:
        # 1. Battery (Termux API)
        raw_batt = os.popen('termux-battery-status').read()
        if raw_batt:
            bj = json.loads(raw_batt)
            data["battery"] = {
                "percentage": bj.get('percentage', 0),
                "temperature": bj.get('temperature', 0),
                "status": bj.get('status', 'Unknown')
            }
        
        # 2. RAM (free -m)
        raw_mem = os.popen('free -m').read()
        for line in raw_mem.split('\n'):
            if 'Mem:' in line:
                p = line.split()
                data["memory"] = {
                    "total": int(p[1]), "used": int(p[2]),
                    "percentage": round((int(p[2])/int(p[1]))*100, 1)
                # 3. CPU (AP)
                # S9 등 안드로이드 상위 버전은 'top -n 1' 결과의 헤더가 다를 수 있음
                raw_top = os.popen('top -n 1 -b | head -n 20').read()

                cpu_sum = 0
                # 패턴: "User 10%, System 5%..." 또는 "10% user, 5% sys"
                m1 = re.search(r'User\s+(\d+)%,\s+System\s+(\d+)%', raw_top, re.I)
                m2 = re.search(r'(\d+)%\s+user,\s+(\d+)%\s+sys', raw_top, re.I)

                if m1:
                    cpu_sum = int(m1.group(1)) + int(m1.group(2))
                elif m2:
                    cpu_sum = int(m2.group(1)) + int(m2.group(2))

                data["cpu"]["percentage"] = cpu_sum if cpu_sum > 0 else 5 # 기본 부하 5% 보정

                # 4. Storage (S9 Custom logic: +25G Correction)
                raw_disk = os.popen('df -h /storage/emulated/0').read()
                disk_lines = raw_disk.strip().split('\n')
                if len(disk_lines) > 1:
                    parts = disk_lines[1].split()
                    # Size(parts[1]), Used(parts[2]) - ex: 231G, 13G
                    try:
                        s_val = float(parts[1].replace('G', ''))
                        u_val = float(parts[2].replace('G', ''))

                        total_final = s_val + 25 # 231 + 25 = 256
                        used_final = u_val + 25  # 13 + 25 = 38

                        data["storage"] = {
                            "total": f"{int(total_final)}G",
                            "used": f"{int(used_final)}G",
                            "percentage": int((used_final / total_final) * 100)
                        }
                    except:
                        pass

        data["status"] = f"Error: {str(e)}"
        
    return data

def get_battery_short_report():
    raw_res = os.popen('termux-battery-status').read()
    try:
        data = json.loads(raw_res)
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C"
    except:
        return "❌ 분석 실패"
