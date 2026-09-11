"""배터리 가드 — S9 배터리 잔량에 따라 스마트플러그를 자동으로 켜고 끄는 기능

목적
----
갤럭시 S9에는 '충전 상한 제한' 기능이 없어서, 24시간 꽂아두면 계속 100%로
충전된 채 유지되어 배터리 수명이 빨리 줄어듭니다.
그래서 S9 충전기가 꽂혀 있는 SmartThings 스마트플러그를 이렇게 제어합니다.

    배터리 90% 이상  ->  플러그 OFF (충전 중단)
    배터리 30% 이하  ->  플러그 ON  (충전 재개)
    그 사이(31~89%)  ->  아무것도 하지 않음

이 파일이 담당하는 것 / 담당하지 않는 것
-------------------------------------
- 담당함   : "언제 켜고 끌지" 판단, 폴링 루프, 에러 처리, Discord 알림
- 담당 안함: SmartThings와 실제로 통신하는 방법 -> core/smartthings/client.py

핵심 설계 3가지 (왜 이렇게 만들었는지)
-----------------------------------
1) 배터리 값은 새로 측정하지 않고 '이미 있는 캐시'를 씁니다.
   utils/system_status.py의 백그라운드 워커가 10초마다 termux-battery-status를
   호출해 결과를 메모리에 담아두고 있습니다. 구형 기기에서 이 명령은 몇 초씩
   걸리므로, 중복 호출하지 않고 get_system_status_data()로 꺼내 씁니다.

2) 플러그 상태를 로컬 파일에 기억해두지 않습니다 (query-before-command).
   "지금 꺼져 있음" 같은 플래그를 저장하는 방식은
     - 봇이 재시작되거나
     - 사용자가 SmartThings 앱에서 직접 플러그를 조작하거나
     - 명령은 접수됐는데 실제 기기는 반응하지 않은 경우
   실제 상태와 어긋나 버립니다.
   그래서 명령을 보내기 직전에 항상 SmartThings에 실제 상태를 물어보고,
   이미 원하는 상태면 명령을 보내지 않습니다. 이것이 히스테리시스 역할을 합니다.
   (설정 파일의 last_action은 '기록/표시용'일 뿐 판단에 쓰지 않습니다)

3) 31~89% 구간(데드밴드)에서는 네트워크 호출이 단 한 건도 발생하지 않습니다.
   대부분의 시간이 이 구간이므로 평소 API 사용량은 사실상 0입니다.

주의
----
discord.ext.tasks의 루프는 안에서 예외가 밖으로 새어나가면 '조용히 멈춥니다'.
그러면 어느 날 인터넷이 한 번 끊긴 뒤로 기능이 죽어 있는데 아무도 모르는
상태가 됩니다. 그래서 이 파일의 루프 본문은 반드시 전부 try/except로 감쌉니다.
"""

import asyncio
from datetime import datetime

from discord.ext import tasks

from config.config_manager import (
    load_battery_guard_config,
    save_battery_guard_config,
)
from config.constants import STATUS_CHANNEL_ID
from core.smartthings.client import (
    SmartThingsClient,
    SmartThingsError,
    SmartThingsAuthError,
    SmartThingsNotFoundError,
    SmartThingsRateLimited,
    SmartThingsServerError,
    SmartThingsNetworkError,
)
from utils.system_status import get_system_status_data


# 루프의 기본 주기(분). 실제 주기는 설정 파일의 poll_minutes를 따르며,
# 이 값은 설정을 읽기 전 초기값으로만 쓰입니다.
DEFAULT_POLL_MINUTES = 5

# poll_minutes에 말도 안 되는 값(0, 9999 등)이 들어와도 안전하도록 제한합니다.
MIN_POLL_MINUTES = 1
MAX_POLL_MINUTES = 60

# 켜기/끄기 명령을 보낸 뒤, 실제로 반영됐는지 확인하기까지 기다리는 시간(초).
# SmartThings 클라우드를 거쳐 Zigbee로 전달되므로 즉시 반영되지 않습니다.
VERIFY_DELAY_SEC = 5

# SmartThings 클라이언트는 상태를 갖지 않으므로 모듈에서 하나만 만들어 재사용합니다.
# (토큰은 이 객체 안에서 os.getenv로만 읽히며 밖으로 노출되지 않습니다)
_st_client = SmartThingsClient()

# 문제 상황을 '중복 없이' 알리기 위한 저장소.
# utils/system_status.py의 _log_once와 같은 아이디어입니다:
# 같은 문제가 5분마다 반복돼도 로그/알림은 상태가 '바뀔 때'만 남깁니다.
_issue_state = {}


def _now_text():
    """로그에 붙일 현재 시각 문자열."""
    return datetime.now().strftime("%H:%M:%S")


def _log(message):
    """표준 출력 로그. PM2가 수집하므로 `pm2 logs`에서 볼 수 있습니다."""
    print(f"[BatteryGuard {_now_text()}] {message}")


async def _send_discord(client, message):
    """상태 채널로 메시지를 보냅니다. 실패해도 절대 예외를 밖으로 내보내지 않습니다.

    (알림 전송 실패 때문에 배터리 가드 본체가 멈추면 주객전도입니다)
    """
    try:
        if not STATUS_CHANNEL_ID:
            return
        channel = client.get_channel(STATUS_CHANNEL_ID)
        if channel is None:
            return
        await channel.send(message)
    except Exception as e:
        _log(f"Discord 알림 전송 실패: {type(e).__name__}: {e}")


async def _report_issue(client, key, message, notify_discord):
    """문제 발생을 알립니다. 단, '같은 문제가 계속될 때'는 한 번만 알립니다.

    key            : 문제의 종류를 구분하는 이름 (예: "auth", "network")
    message        : 사람이 읽을 설명
    notify_discord : 설정에서 Discord 알림을 켜뒀는지 여부
    """
    if _issue_state.get(key) == message:
        # 직전과 완전히 같은 상태 -> 조용히 넘어갑니다 (로그/알림 폭주 방지)
        return

    _issue_state[key] = message
    _log(f"⚠️ {message}")

    if notify_discord:
        await _send_discord(client, f"⚠️ **배터리 가드 문제**\n{message}")


async def _clear_issue(client, key, recovered_message, notify_discord):
    """문제가 해소됐을 때 '정상화됐다'고 한 번만 알립니다."""
    if key not in _issue_state:
        return

    del _issue_state[key]
    _log(f"✅ {recovered_message}")

    if notify_discord:
        await _send_discord(client, f"✅ **배터리 가드 복구**\n{recovered_message}")


def _read_battery_percentage():
    """캐시에서 배터리 퍼센트를 읽습니다.

    반환값: 정수/실수 퍼센트, 또는 아직 믿을 수 없는 값이면 None

    None을 돌려주는 경우:
      - 워커가 아직 한 번도 수집하지 못함 (봇이 막 켜진 직후)
      - termux-battery-status가 실패해서 초기값 0이 그대로 남아 있음

    이 방어가 중요한 이유: 초기값 0%를 그대로 믿으면 "30% 이하"로 판단해
    엉뚱하게 플러그를 켜버립니다. 5분 뒤 다음 주기에 정상값으로 판단하면 됩니다.
    """
    data = get_system_status_data()

    if not data.get("last_updated"):
        return None

    battery = data.get("battery") or {}
    percentage = battery.get("percentage")

    if not isinstance(percentage, (int, float)):
        return None
    if percentage <= 0 or percentage > 100:
        return None

    return percentage


async def _apply_desired_state(client, config, device_id, desired, percentage):
    """실제 상태를 확인하고, 필요할 때만 명령을 보냅니다.

    desired : "on" 또는 "off"

    여기가 히스테리시스의 핵심입니다.
    이미 원하는 상태라면 명령을 보내지 않으므로,
    배터리가 100%로 며칠 있어도 '끄기' 명령이 반복 전송되지 않습니다.
    """
    notify_discord = bool(config.get("notify_discord", True))

    # 1) 지금 실제로 어떤 상태인지 물어봅니다. (API 호출 1건)
    actual = await _st_client.get_switch_state(device_id)

    if actual == desired:
        # 이미 원하는 상태입니다. 아무것도 하지 않습니다. -> 중복 명령 없음
        _log(f"배터리 {percentage}% / 플러그 이미 '{actual}' -> 명령 생략")
        return

    # 2) 상태가 다르므로 명령을 보냅니다. (API 호출 1건)
    label = config.get("device_label") or device_id
    action_text = "충전 중단" if desired == "off" else "충전 재개"
    _log(f"배터리 {percentage}% / 플러그 '{actual}' -> '{desired}' 명령 전송 ({action_text})")

    await _st_client.send_switch_command(device_id, desired)

    # 3) SmartThings의 "ACCEPTED"는 '접수했다'는 뜻이지 '기기가 실제로 바뀌었다'는
    #    뜻이 아닙니다. 잠시 뒤 실제 상태를 다시 읽어 확인합니다. (API 호출 1건)
    #    이 검증은 상태가 바뀌는 순간(하루 2~4회)에만 일어나므로 비용이 거의 없습니다.
    await asyncio.sleep(VERIFY_DELAY_SEC)
    verified = await _st_client.get_switch_state(device_id)

    if verified == desired:
        await _clear_issue(
            client,
            "command_failed",
            f"플러그 제어가 다시 정상 동작합니다 ({label}).",
            notify_discord,
        )

        # 기록용으로 마지막 동작을 남겨둡니다. (판단에는 쓰지 않습니다)
        config["last_action"] = {
            "action": desired,
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "battery": percentage,
        }
        save_battery_guard_config(config)

        if notify_discord:
            if desired == "off":
                await _send_discord(
                    client,
                    f"🔌 **충전 중단** — 배터리 {percentage}%\n"
                    f"`{label}` 플러그를 껐습니다. (상한 {config.get('high_threshold')}%)",
                )
            else:
                await _send_discord(
                    client,
                    f"⚡ **충전 재개** — 배터리 {percentage}%\n"
                    f"`{label}` 플러그를 켰습니다. (하한 {config.get('low_threshold')}%)",
                )
        return

    # 4) 명령은 접수됐는데 기기가 반응하지 않은 경우입니다.
    #    원인을 좁히기 위해 이때만 추가로 연결 상태를 확인합니다.
    try:
        health = await _st_client.get_health(device_id)
    except SmartThingsError:
        health = "확인 실패"

    await _report_issue(
        client,
        "command_failed",
        f"`{label}` 플러그에 '{desired}' 명령을 보냈지만 실제 상태가 바뀌지 않았습니다 "
        f"(현재 '{verified}', 기기 연결 상태 '{health}').\n"
        f"플러그 전원이나 Zigbee 연결을 확인해 주세요.",
        notify_discord,
    )


async def _run_once(client):
    """폴링 한 주기 동안 할 일 전부. 실패하면 예외를 위로 던집니다."""
    config = load_battery_guard_config()
    notify_discord = bool(config.get("notify_discord", True))

    # --- 설정 확인 ---
    if not config.get("enabled", True):
        _log("기능이 꺼져 있습니다 (enabled=false). 이번 주기를 건너뜁니다.")
        return

    # device_id가 비어 있으면 SmartThings를 호출하지 않고 안전하게 건너뜁니다.
    # (빈 값으로 호출하면 엉뚱한 URL을 두드리게 되고, 매 주기 404 오류만 쌓입니다)
    # _report_issue가 중복을 걸러주므로 경고는 상태가 바뀔 때 한 번만 나갑니다.
    device_id = config.get("device_id")
    if not device_id or not str(device_id).strip():
        await _report_issue(
            client,
            "no_device",
            "플러그 device_id가 설정되어 있지 않아 동작을 건너뜁니다.\n"
            "`.env`에 `SMARTTHINGS_DEVICE_ID`를 추가하고 봇을 재시작하거나, "
            "`data/battery_guard.json`의 device_id를 직접 채워 주세요.",
            notify_discord,
        )
        return
    await _clear_issue(client, "no_device", "device_id 설정이 확인되었습니다.", notify_discord)

    high = config.get("high_threshold", 90)
    low = config.get("low_threshold", 30)

    # 상한/하한이 뒤집혀 있으면 플러그를 껐다 켰다 반복하게 되므로 아예 동작을 멈춥니다.
    if not isinstance(high, (int, float)) or not isinstance(low, (int, float)) or low >= high:
        await _report_issue(
            client,
            "bad_threshold",
            f"임계값 설정이 잘못되었습니다 (하한 {low}, 상한 {high}). "
            f"하한은 상한보다 작아야 합니다. 안전을 위해 동작을 멈춥니다.",
            notify_discord,
        )
        return
    await _clear_issue(client, "bad_threshold", "임계값 설정이 정상으로 돌아왔습니다.", notify_discord)

    # --- 배터리 읽기 (캐시 사용, 네트워크/명령 호출 없음) ---
    percentage = _read_battery_percentage()
    if percentage is None:
        _log("배터리 값을 아직 신뢰할 수 없습니다 (수집 전 또는 수집 실패). 이번 주기를 건너뜁니다.")
        return

    # --- 데드밴드: 여기서 끝나면 SmartThings 호출이 0건입니다 ---
    if low < percentage < high:
        _log(f"배터리 {percentage}% — 조치 불필요 구간({low}% ~ {high}%). API 호출 없음.")
        return

    desired = "off" if percentage >= high else "on"
    await _apply_desired_state(client, config, device_id, desired, percentage)


async def _guarded_cycle(client):
    """_run_once를 실행하되, 모든 실패를 종류별로 구분해 처리합니다.

    여기서 예외를 전부 붙잡기 때문에 루프가 중간에 죽지 않습니다.
    """
    # 알림 설정을 읽는 것조차 실패할 수 있으므로 기본값을 True로 두고 시작합니다.
    notify_discord = True
    try:
        notify_discord = bool(load_battery_guard_config().get("notify_discord", True))
    except Exception:
        pass

    try:
        await _run_once(client)

        # 여기까지 왔다면 이번 주기는 성공했다는 뜻이므로,
        # 통신 관련 문제 표시를 해제합니다.
        await _clear_issue(client, "auth", "SmartThings 인증이 정상으로 복구되었습니다.", notify_discord)
        await _clear_issue(client, "not_found", "SmartThings 기기를 다시 찾았습니다.", notify_discord)
        await _clear_issue(client, "network", "SmartThings 통신이 정상으로 복구되었습니다.", notify_discord)

    except SmartThingsAuthError as e:
        # 토큰 만료/무효. 재시도해도 소용없으므로 사용자에게 알려야 합니다.
        # (자동으로 기능을 꺼버리지는 않습니다. 토큰을 고치면 다음 주기에 저절로 복구됩니다)
        await _report_issue(
            client,
            "auth",
            f"SmartThings 인증에 실패했습니다. 토큰이 만료되었거나 권한이 부족합니다.\n"
            f"`.env`의 `SMARTTHINGS_TOKEN`을 새로 발급해 주세요.\n"
            f"(상세: {e})",
            notify_discord,
        )

    except SmartThingsNotFoundError as e:
        await _report_issue(
            client,
            "not_found",
            f"SmartThings에서 플러그를 찾지 못했습니다. "
            f"`data/battery_guard.json`의 device_id를 확인해 주세요.\n(상세: {e})",
            notify_discord,
        )

    except SmartThingsRateLimited as e:
        # 5분 주기에서는 사실상 발생하지 않습니다. 발생해도 다음 주기에 자연히 해소됩니다.
        _log(f"호출 한도 초과로 이번 주기를 건너뜁니다: {e}")

    except (SmartThingsNetworkError, SmartThingsServerError) as e:
        # 인터넷 끊김 / SmartThings 서버 문제. 다음 주기에 자동 재시도합니다.
        # 한 주기 안에서 재시도하지 않는 이유: 5분 뒤가 자연스러운 재시도 시점이고,
        # 별도 백오프 로직을 넣으면 복잡도만 늘어납니다.
        await _report_issue(
            client,
            "network",
            f"SmartThings와 통신하지 못했습니다. 다음 주기에 다시 시도합니다.\n(상세: {e})",
            notify_discord,
        )

    except SmartThingsError as e:
        await _report_issue(
            client,
            "network",
            f"SmartThings 처리 중 오류가 발생했습니다.\n(상세: {e})",
            notify_discord,
        )

    except Exception as e:
        # 예상하지 못한 버그까지 여기서 막습니다. 루프는 계속 살아 있어야 합니다.
        _log(f"💥 예상하지 못한 오류: {type(e).__name__}: {e}")


@tasks.loop(minutes=DEFAULT_POLL_MINUTES)
async def battery_guard_loop(client):
    """주기적으로 배터리를 확인하는 백그라운드 루프.

    news_loop / srt_reservation_loop와 같은 방식으로 동작합니다.
    """
    await client.wait_until_ready()

    # 설정에서 주기를 바꿨으면 반영합니다.
    # (다음 단계에서 Discord 명령으로 주기를 바꿔도 재시작 없이 적용되게 하기 위함)
    try:
        poll_minutes = load_battery_guard_config().get("poll_minutes", DEFAULT_POLL_MINUTES)
        poll_minutes = int(poll_minutes)
        poll_minutes = max(MIN_POLL_MINUTES, min(MAX_POLL_MINUTES, poll_minutes))

        if battery_guard_loop.minutes != poll_minutes:
            _log(f"폴링 주기를 {battery_guard_loop.minutes}분 -> {poll_minutes}분으로 변경합니다.")
            battery_guard_loop.change_interval(minutes=poll_minutes)
    except Exception as e:
        _log(f"폴링 주기 설정을 읽지 못했습니다(기본값 유지): {type(e).__name__}: {e}")

    await _guarded_cycle(client)


def start_battery_guard(client):
    """butler_pro.py의 on_ready()에서 호출해 루프를 시작합니다."""
    if battery_guard_loop.is_running():
        return

    # 첫 실행 전에도 설정된 주기를 반영해둡니다.
    try:
        poll_minutes = int(load_battery_guard_config().get("poll_minutes", DEFAULT_POLL_MINUTES))
        poll_minutes = max(MIN_POLL_MINUTES, min(MAX_POLL_MINUTES, poll_minutes))
        battery_guard_loop.change_interval(minutes=poll_minutes)
    except Exception as e:
        poll_minutes = DEFAULT_POLL_MINUTES
        _log(f"설정을 읽지 못해 기본 주기 {DEFAULT_POLL_MINUTES}분으로 시작합니다: {type(e).__name__}: {e}")

    _log(f"🔋 배터리 가드 시작 (폴링 주기 {poll_minutes}분)")
    battery_guard_loop.start(client)
