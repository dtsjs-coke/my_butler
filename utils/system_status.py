import os
import json
import discord
from datetime import datetime

def get_system_status_embed():
    # 1. 배터리 정보
    raw_batt = os.popen('termux-battery-status').read()
    # 2. RAM 정보 (MB 단위)
    raw_mem = os.popen('free -m').read()
    # 3. CPU 정보 (Load Average)
    raw_cpu = os.popen('uptime').read()
    
    try:
        # 배터리 파싱
        batt_data = json.loads(raw_batt)
        percentage = batt_data.get('percentage')
        temp = batt_data.get('temperature')
        
        # RAM 파싱
        mem_lines = raw_mem.split('\n')
        mem_info = mem_lines[1].split()
        total_mem = mem_info[1]
        used_mem = mem_info[2]
        mem_pct = round((int(used_mem) / int(total_mem)) * 100, 1)
        
        # CPU 파싱 (uptime에서 load average 추출)
        load_avg = raw_cpu.split('load average:')[1].strip()
        
        # 깔끔한 Embed 포맷으로 응답
        embed = discord.Embed(
            title="📱 S9 시스템 리소스 보고", 
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        embed.add_field(name="🔋 배터리", value=f"**{percentage}%** ({temp}°C)", inline=True)
        embed.add_field(name="🧠 RAM 사용량", value=f"**{mem_pct}%** ({used_mem}/{total_mem} MB)", inline=True)
        embed.add_field(name="⚙️ CPU 부하 (1/5/15m)", value=f"`{load_avg}`", inline=False)
        embed.add_field(name="⚡ 충전 상태", value=f"`{batt_data.get('status')}`", inline=True)
        embed.add_field(name="🏥 배터리 건강", value=f"`{batt_data.get('health')}`", inline=True)
        
        return embed
    except Exception as e:
        print(f"상태 정보 파싱 오류: {e}")
        return None

def get_system_status_data():
    """웹 대시보드용 원시 시스템 데이터 반환 (Battery, Temp, RAM, CPU, Storage)"""
    raw_batt = os.popen('termux-battery-status').read()
    raw_mem = os.popen('free -m').read()
    # CPU 사용량 추출 (top -n 1 사용)
    raw_cpu = os.popen('top -n 1 | grep "CPU:"').read()
    # 저장공간 추출 (Termux 홈 디렉토리 기준)
    raw_disk = os.popen('df -h /data/data/com.termux/files/home').read()
    
    data = {
        "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
        "memory": {"used": 0, "total": 0, "percentage": 0},
        "cpu": {"percentage": 0},
        "storage": {"used": "0", "total": "0", "percentage": 0},
        "status": "Healthy"
    }
    
    try:
        # 1. Battery & Temp
        batt_json = json.loads(raw_batt)
        data["battery"] = {
            "percentage": batt_json.get('percentage'),
            "temperature": batt_json.get('temperature'),
            "status": batt_json.get('status')
        }
        
        # 2. Memory
        mem_lines = raw_mem.split('\n')
        if len(mem_lines) > 1:
            mem_info = mem_lines[1].split()
            total = int(mem_info[1])
            used = int(mem_info[2])
            data["memory"] = {
                "used": used, "total": total,
                "percentage": round((used / total) * 100, 1)
            }

        # 3. CPU (AP)
        # "CPU: 12% usr  5% sys ..." 형태 파싱
        cpu_match = re.search(r'CPU:\s+(\d+)%', raw_cpu)
        if cpu_match:
            data["cpu"]["percentage"] = int(cpu_match.group(1))

        # 4. Storage (HDD)
        disk_lines = raw_disk.split('\n')
        if len(disk_lines) > 1:
            disk_info = disk_lines[1].split()
            # Size, Used, Avail, Use% 순서 (df -h 결과에 따라 다를 수 있음)
            data["storage"] = {
                "total": disk_info[1],
                "used": disk_info[2],
                "percentage": int(disk_info[4].replace('%', ''))
            }
            
    except Exception as e:
        data["status"] = f"Error: {str(e)}"
        
    return data

def get_battery_short_report():
    raw_res = os.popen('termux-battery-status').read()
    try:
        data = json.loads(raw_res)
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C"
    except:
        return "❌ 분석 실패"
