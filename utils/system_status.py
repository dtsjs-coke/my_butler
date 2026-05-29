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
    # 1. 원시 데이터 수집
    raw_batt = os.popen('termux-battery-status').read()
    raw_mem = os.popen('free -m').read()
    # top 명령어는 배치 모드(-b)가 더 안정적일 수 있음
    raw_cpu = os.popen('top -b -n 1 | head -n 10').read()
    # df 명령어는 현재 디렉토리 기준으로 수행
    raw_disk = os.popen('df -h .').read()
    
    data = {
        "battery": {"percentage": 0, "temperature": 0, "status": "Unknown"},
        "memory": {"used": 0, "total": 0, "percentage": 0},
        "cpu": {"percentage": 0},
        "storage": {"used": "0", "total": "0", "percentage": 0},
        "status": "Healthy"
    }
    
    try:
        # --- [1] Battery & Temp ---
        if raw_batt:
            batt_json = json.loads(raw_batt)
            data["battery"] = {
                "percentage": batt_json.get('percentage', 0),
                "temperature": batt_json.get('temperature', 0),
                "status": batt_json.get('status', 'Unknown')
            }
        
        # --- [2] Memory ---
        mem_lines = raw_mem.strip().split('\n')
        for line in mem_lines:
            if line.startswith('Mem:'):
                parts = line.split()
                total = int(parts[1])
                used = int(parts[2])
                data["memory"] = {
                    "used": used, "total": total,
                    "percentage": round((used / total) * 100, 1)
                }
                break

        # --- [3] CPU (AP) ---
        # Android/Termux top 출력 예시: "User 5%, System 4%, IOW 0%, IRQ 0%"
        # 또는 "[  12%] user" 등 다양함. 숫자가 포함된 퍼센트 합산을 시도
        cpu_usage = 0
        cpu_match = re.search(r'([\d\.]+)%\s*(?:user|usr|sys|system)', raw_cpu, re.IGNORECASE)
        if cpu_match:
            cpu_usage = float(cpu_match.group(1))
        else:
            # 다른 형태: "CPU: 15%"
            cpu_match_alt = re.search(r'CPU:\s*([\d\.]+)%', raw_cpu, re.IGNORECASE)
            if cpu_match_alt:
                cpu_usage = float(cpu_match_alt.group(1))
        
        data["cpu"]["percentage"] = int(cpu_usage)

        # --- [4] Storage (HDD) ---
        # df -h 출력의 두 번째 라인부터 실제 데이터가 있는지 확인
        disk_lines = raw_disk.strip().split('\n')
        for line in disk_lines[1:]:
            parts = line.split()
            # 보통 Size(1), Used(2), Avail(3), Use%(4) 순서
            if len(parts) >= 5 and '%' in parts[4]:
                data["storage"] = {
                    "total": parts[1],
                    "used": parts[2],
                    "percentage": int(parts[4].replace('%', ''))
                }
                break
            elif len(parts) >= 4 and '%' in parts[3]: # 가끔 컬럼이 밀리는 경우
                data["storage"] = {
                    "total": parts[0], # 장치명이 너무 길어 줄바꿈 된 경우
                    "used": parts[1],
                    "percentage": int(parts[3].replace('%', ''))
                }
            
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
