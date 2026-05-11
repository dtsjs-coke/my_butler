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

def get_battery_short_report():
    raw_res = os.popen('termux-battery-status').read()
    try:
        data = json.loads(raw_res)
        return f"📊 **S9 배터리**: {data.get('percentage')}% | {data.get('temperature')}°C"
    except:
        return "❌ 분석 실패"
