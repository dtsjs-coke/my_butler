import json
import os
from SRT.passenger import Adult, Child, Senior, Disability1To3, Disability4To6
from SRT import SeatType
from korail2 import AdultPassenger, ChildPassenger, SeniorPassenger, ReserveOption

# 프로젝트 루트 경로 설정 (config/ 폴더의 부모인 my_butler/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

KEYWORDS_FILE = os.path.join(BASE_DIR, "keywords.json")
STATIONS_FILE = os.path.join(BASE_DIR, "stations.json")
QUEUE_FILE = os.path.join(BASE_DIR, "reservations.json")
KTX_STATIONS_FILE = os.path.join(BASE_DIR, "ktx_stations.json")
KTX_QUEUE_FILE = os.path.join(BASE_DIR, "ktx_reservations.json")
MODEL_FILE = os.path.join(BASE_DIR, "model_config.json")

DEFAULT_KEYWORDS = ["AI Agent", "하네스 엔지니어링", "대우 건설"]
DEFAULT_STATIONS = ["수서", "동탄","광주송정", "평택지제", "천안아산", "오송", "대전", "김천(구미)", "서대구", "대구", "울산(통도사)", "부산", "공주", "익산", "정읍", "나주", "목포", "남원","순천","여천","여수EXPO"]
DEFAULT_KTX_STATIONS = ["서울", "용산", "영등포", "광명", "수원", "천안아산", "오송", "대전", "김천구미", "동대구", "경주", "울산", "부산", "포항", "마산", "진주", "익산", "전주", "순천", "여수EXPO", "광주송정", "목포"]
DEFAULT_MODEL = "gemini-3-flash-preview"

def load_json(file_path, default_data):
    # ktx_stations.json 이나 ktx_reservations.json 등은 my_butler 루트에 위치하도록 함
    # (다른 파일들과 동일한 경로)
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

def load_ktx_stations():
    return load_json(KTX_STATIONS_FILE, DEFAULT_KTX_STATIONS)

def save_ktx_stations(stations):
    save_json(KTX_STATIONS_FILE, stations)

# --- Model Name ---
def load_model_name():
    data = load_json(MODEL_FILE, {"model_name": DEFAULT_MODEL})
    return data.get("model_name", DEFAULT_MODEL)

def save_model_name(model_name):
    save_json(MODEL_FILE, {"model_name": model_name})

# --- SRT Queue Persistence ---
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
            try:
                user_id = int(user_id_str)
            except ValueError:
                user_id = user_id_str
                
            if isinstance(task_list, dict):
                task_list = [task_list]
                
            processed_tasks = []
            for data in task_list:
                passengers = []
                for p_name in data.get('passengers', []):
                    if p_name in passenger_map:
                        passengers.append(passenger_map[p_name]())
                
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

# --- KTX Queue Persistence ---
def serialize_ktx_queue(queue):
    serialized = {}
    for user_id, task_list in queue.items():
        user_tasks = []
        for data in task_list:
            # korail2 passengers: list of (class_name, count)
            passengers_serialized = []
            for p in data.get('passengers', []):
                passengers_serialized.append({'type': p.__class__.__name__, 'count': p.count})
            
            # ReserveOption Enum을 문자열로 변환
            seat_type_serialized = data.get('seat_type').name if isinstance(data.get('seat_type'), ReserveOption) else str(data.get('seat_type'))
            
            item = data.copy()
            item['passengers'] = passengers_serialized
            item['seat_type'] = seat_type_serialized
            user_tasks.append(item)
        serialized[str(user_id)] = user_tasks
    return serialized

def deserialize_ktx_queue(serialized_queue):
    queue = {}
    passenger_map = {
        'AdultPassenger': AdultPassenger,
        'ChildPassenger': ChildPassenger,
        'SeniorPassenger': SeniorPassenger
    }
    
    for user_id_str, task_list in serialized_queue.items():
        try:
            try:
                user_id = int(user_id_str)
            except ValueError:
                user_id = user_id_str
                
            if isinstance(task_list, dict):
                task_list = [task_list]
                
            processed_tasks = []
            for data in task_list:
                passengers = []
                for p_data in data.get('passengers', []):
                    p_type = p_data.get('type')
                    p_count = p_data.get('count', 1)
                    if p_type in passenger_map:
                        passengers.append(passenger_map[p_type](p_count))
                
                seat_type_str = data.get('seat_type', 'GENERAL_FIRST')
                try:
                    seat_type = ReserveOption[seat_type_str]
                except:
                    seat_type = ReserveOption.GENERAL_FIRST
                    
                data['passengers'] = passengers
                data['seat_type'] = seat_type
                processed_tasks.append(data)
            
            queue[user_id] = processed_tasks
        except Exception as e:
            print(f"Failed to deserialize KTX queue item for {user_id_str}: {e}")
            continue
    return queue

def load_ktx_queue():
    data = load_json(KTX_QUEUE_FILE, {})
    return deserialize_ktx_queue(data)

def save_ktx_queue(queue):
    serialized = serialize_ktx_queue(queue)
    save_json(KTX_QUEUE_FILE, serialized)
