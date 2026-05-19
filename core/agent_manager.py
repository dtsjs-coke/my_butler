import os
import json
import asyncio
from datetime import datetime
from config.config_manager import load_keywords

# 설정 파일 경로
AGENT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "agent_config.json")

DEFAULT_CONFIG = {
    "thermal_management": {
        "enabled": True,
        "critical_temp": 45,
        "throttle_interval": 1800, # 발열 시 30분
        "normal_interval": 600    # 평상시 10분 (체크 빈도)
    },
    "self_healing": {
        "enabled": True,
        "auto_apply": False,      # 자동 적용 금지 (기본값)
        "require_approval": True   # 승인 필수
    },
    "pending_actions": []         # 승인 대기 중인 액션들
}

def load_agent_config():
    if not os.path.exists(AGENT_CONFIG_PATH):
        save_agent_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    with open(AGENT_CONFIG_PATH, 'r') as f:
        return json.load(f)

def save_agent_config(config):
    with open(AGENT_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=4)

def add_pending_action(action_type, details, proposed_patch=None):
    config = load_agent_config()
    action_id = datetime.now().strftime("%Y%m%d%H%M%S")
    action = {
        "id": action_id,
        "type": action_type,
        "details": details,
        "patch": proposed_patch,
        "status": "pending",
        "timestamp": datetime.now().isoformat()
    }
    config["pending_actions"].append(action)
    save_agent_config(config)
    return action_id

def resolve_action(action_id, approved=True):
    config = load_agent_config()
    for action in config["pending_actions"]:
        if action["id"] == action_id:
            action["status"] = "approved" if approved else "rejected"
            save_agent_config(config)
            return action
    return None
