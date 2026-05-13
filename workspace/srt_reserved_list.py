import os
import sys
import requests
from dotenv import load_dotenv

# Set up path to include parent directory for modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load credentials
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(ENV_PATH)

# Assuming the module exists as 'srt_module' in parent directory
from srt_module import SRTClient

def main():
    chat_channel_id = os.getenv("CHAT_CHANNEL_ID")
    srt_user = os.getenv("SRT_USER")
    srt_pw = os.getenv("SRT_PW")
    api_url = "http://localhost:5000/send"

    try:
        # Initialize and authenticate
        client = SRTClient(srt_user, srt_pw)
        reservations = client.get_reservations()
        
        if not reservations:
            message = "현재 예약된 SRT 티켓이 없습니다."
        else:
            message = "[SRT 예약 목록]\n"
            for r in reservations:
                message += f"- {r['train_date']} {r['train_no']}호: {r['dep_station']} -> {r['arr_station']} ({r['seat_no']})\n"
        
        # Send to Local API
        payload = {
            "channel_id": int(chat_channel_id),
            "content": message
        }
        response = requests.post(api_url, json=payload)
        response.raise_for_status()
        print("Successfully sent reservation list to Discord.")

    except Exception as e:
        print(f"Error: {str(e)}")

if __name__ == '__main__':
    main()