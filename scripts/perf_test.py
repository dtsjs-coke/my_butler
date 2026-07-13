import os
import time
import json
import subprocess
import shutil
import re

def measure(name, func):
    start = time.perf_counter()
    try:
        result = func()
        end = time.perf_counter()
        elapsed = (end - start) * 1000
        print(f"[{name}]")
        print(f"  - 시간: {elapsed:.2f}ms")
        print(f"  - 값: {result}")
    except Exception as e:
        print(f"[{name}] 실패: {e}")
    print("-" * 30)

# --- CPU ---
def cpu_proc():
    with open('/proc/stat', 'r') as f:
        line = f.readline()
    return line[:50] + "..."

def cpu_top():
    res = subprocess.run(['top', '-n', '1', '-b'], capture_output=True, text=True, timeout=5)
    m = re.search(r'(\d+)%\s+user,\s+(\d+)%\s+sys', res.stdout, re.I)
    return f"{m.group(0)}" if m else "N/A"

# --- Memory ---
def mem_proc():
    with open('/proc/meminfo', 'r') as f:
        lines = f.readlines()[:3]
    return [l.strip() for l in lines]

def mem_free():
    res = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=5)
    return res.stdout.strip().split('\n')[1]

# --- Battery ---
def batt_sys():
    path = "/sys/class/power_supply/battery"
    perc = open(f"{path}/capacity").read().strip()
    temp = int(open(f"{path}/temp").read().strip()) / 10.0
    return f"{perc}%, {temp}C"

def batt_api():
    res = subprocess.run(['termux-battery-status'], capture_output=True, text=True, timeout=5)
    data = json.loads(res.stdout)
    return f"{data['percentage']}%, {data['temperature']}C"

# --- Storage ---
def storage_shutil():
    u = shutil.disk_usage("/data/data/com.termux/files/home")
    return f"{u.used//(1024**3)}G / {u.total//(1024**3)}G"

def storage_df():
    res = subprocess.run(['df', '-h', '/data/data/com.termux/files/home'], capture_output=True, text=True, timeout=5)
    return res.stdout.strip().split('\n')[-1]

if __name__ == "__main__":
    print("🚀 S9 하드웨어 정보 수집 성능 테스트\n")
    
    print("--- CPU ---")
    measure("Proc Stat (최적화)", cpu_proc)
    measure("Top Command (기존)", cpu_top)
    
    print("\n--- Memory ---")
    measure("Proc Meminfo (최적화)", mem_proc)
    measure("Free Command (기본)", mem_free)
    
    print("\n--- Battery ---")
    measure("Sysfs Battery (최적화)", batt_sys)
    measure("Termux API (기본)", batt_api)
    
    print("\n--- Storage ---")
    measure("Shutil Usage (최적화)", storage_shutil)
    measure("DF Command (기본)", storage_df)
