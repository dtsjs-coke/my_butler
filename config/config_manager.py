import json
import os
from SRT.passenger import Adult, Child, Senior, Disability1To3, Disability4To6
from SRT import SeatType

KEYWORDS_FILE = "keywords.json"
STATIONS_FILE = "stations.json"
QUEUE_FILE = "reservations.json"
MODEL_FILE = "model_config.json"

DEFAULT_KEYWORDS = ["AI Agent", "하네스 엔지니어링", "대우 건설"]
DEFAULT_STATIONS = ["수서", "동탄","광주송정", "평택지제", "천안아산", "오송", "대전", "김천(구미)", "서대구", "대구", "울산(통도사)", "부산", "공주", "익산", "정읍", "나주", "목포", "남원","순천","여천","여수EXPO"]
DEFAULT_MODEL = "gemini-3-flash-preview"

def load_json(file_path, default_data):
    if not os.path.exists(file_path):
        return default_data
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default_data

def save_json(file_path, data):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# --- Keywords ---
def load_keywords():
    return load_json(KEYWORDS_FILE, DEFAULT_KEYWORDS)

def save_keywords(keywords):
    save_json(KEYWORDS_FILE, keywords)

# --- Stations ---
def load_stations():
    return load_json(STATIONS_FILE, DEFAULT_STATIONS)

def save_stations(stations):
    save_json(STATIONS_FILE, stations)

# --- Model Name ---
def load_model_name():
    data = load_json(MODEL_FILE, {"model_name": DEFAULT_MODEL})
    return data.get("model_name", DEFAULT_MODEL)

def save_model_name(model_name):
    save_json(MODEL_FILE, {"model_name": model_name})

# --- Queue Persistence ---
def serialize_queue(queue):
    serialized = {}
    for user_id, task_list in queue.items():
        user_tasks = []
        for data in task_list:
            # passengers 객체를 클래스 이름 리스트로 변환
            passengers_serialized = [p.__class__.__name__ for p in data.get('passengers', [])]
            
            # SeatType Enum을 문자열로 변환
            seat_type_serialized = data.get('seat_type').name if isinstance(data.get('seat_type'), SeatType) else str(data.get('seat_type'))
            
            item = data.copy()
            item['passengers'] = passengers_serialized
            item['seat_type'] = seat_type_serialized
            user_tasks.append(item)
        serialized[str(user_id)] = user_tasks
    return serialized

def deserialize_queue(serialized_queue):
    queue = {}
    passenger_map = {
        'Adult': Adult,
        'Child': Child,
        'Senior': Senior,
        'Disability1To3': Disability1To3,
        'Disability4To6': Disability4To6
    }
    
    for user_id_str, task_list in serialized_queue.items():
        try:
            user_id = int(user_id_str)
            # 이전 버전 호환성: task_list가 리스트가 아닌 단일 딕셔너리인 경우 처리
            if isinstance(task_list, dict):
                task_list = [task_list]
                
            processed_tasks = []
            for data in task_list:
                # passengers 리스트를 다시 객체 리스트로 변환
                passengers = []
                for p_name in data.get('passengers', []):
                    if p_name in passenger_map:
                        passengers.append(passenger_map[p_name]())
                
                # seat_type 문자열을 다시 Enum으로 변환
                seat_type_str = data.get('seat_type', 'GENERAL_FIRST')
                try:
                    seat_type = SeatType[seat_type_str]
                except:
                    seat_type = SeatType.GENERAL_FIRST
                    
                data['passengers'] = passengers
                data['seat_type'] = seat_type
                processed_tasks.append(data)
            
            queue[user_id] = processed_tasks
        except Exception as e:
            print(f"Failed to deserialize queue item for {user_id_str}: {e}")
            continue
    return queue

def load_queue():
    data = load_json(QUEUE_FILE, {})
    return deserialize_queue(data)

def save_queue(queue):
    serialized = serialize_queue(queue)
    save_json(QUEUE_FILE, serialized)
