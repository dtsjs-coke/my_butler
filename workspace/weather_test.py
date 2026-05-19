import os
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def get_weather_forecast():
    # Dongtan, Hwaseong-si coordinates
    lat, lon = 37.2037, 127.1087
    
    # Open-Meteo API URL for daily forecast
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Asia%2FSeoul"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        # Tomorrow's data (Index 1 in the daily array)
        tomorrow_idx = 1
        date = data['daily']['time'][tomorrow_idx]
        max_temp = data['daily']['temperature_2m_max'][tomorrow_idx]
        min_temp = data['daily']['temperature_2m_min'][tomorrow_idx]
        precip = data['daily']['precipitation_sum'][tomorrow_idx]
        w_code = data['daily']['weathercode'][tomorrow_idx]
        
        # Simple weather code mapping
        weather_desc = {0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 61: "약한 비", 63: "비", 71: "약한 눈"}.get(w_code, "정보 없음")
        
        message = f"📅 **내일 동탄 날씨 ({date})**\n🌤 상태: {weather_desc}\n🌡 기온: {min_temp}°C ~ {max_temp}°C\n☔ 강수량: {precip}mm"
        return message
    except Exception as e:
        return f"❌ 날씨 정보를 가져오는데 실패했습니다: {str(e)}"

def send_to_discord(content):
    status_channel_id = os.getenv("STATUS_CHANNEL_ID")
    local_api_url = "http://localhost:5000/send"
    
    if not status_channel_id:
        print("Error: STATUS_CHANNEL_ID not found in environment.")
        return

    payload = {
        "channel_id": int(status_channel_id),
        "content": content
    }
    
    try:
        res = requests.post(local_api_url, json=payload)
        if res.status_code == 200:
            print("Successfully sent weather update to Discord.")
        else:
            print(f"Local API error: {res.status_code}")
    except Exception as e:
        print(f"Connection error: {e}")

if __name__ == "__main__":
    forecast_msg = get_weather_forecast()
    send_to_discord(forecast_msg)