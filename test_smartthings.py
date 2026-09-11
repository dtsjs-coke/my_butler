"""SmartThings 클라이언트 단독 테스트 스크립트

이 프로젝트에는 pytest 같은 테스트 프레임워크가 없고,
test_vwap_bot.py / test_toss_auth.py 처럼 '직접 실행하는 확인용 스크립트'를 씁니다.
이 파일도 같은 방식입니다.

사용법
------
1) 읽기만 (안전 — 플러그를 건드리지 않음)

       python test_smartthings.py

   기기 목록, 대상 플러그의 현재 상태/온오프/전력/온라인 여부만 확인합니다.

2) 실제 제어 테스트 (플러그가 켜졌다 꺼집니다)

       python test_smartthings.py --control

   순서:
     1. 현재 상태 확인 (GET)
     2. 켜기 명령 (POST on)
     3. 5초 대기 후 실제로 "on"이 됐는지 확인 (GET)
     4. 원래 상태로 되돌리는 명령 (POST)
     5. 5초 대기 후 원래대로 돌아왔는지 확인 (GET)

   * 3번 단계에서 플러그의 딸깍 소리가 나거나 SmartThings 앱의 상태가 바뀌는지
     눈으로 확인하시면 됩니다.
   * 5번 단계에서 '처음 상태'로 되돌리므로, 테스트 전후로 플러그 상태는 같아집니다.

주의
----
--control 옵션 없이는 절대 POST(제어) 명령을 보내지 않습니다.
실수로 실행해서 플러그가 꺼지는 일을 막기 위한 안전장치입니다.
"""

import os
import sys
import asyncio
from datetime import datetime

from dotenv import load_dotenv

# 이 스크립트는 my_butler 폴더에서 실행되므로, 같은 폴더의 .env를 읽습니다.
load_dotenv()

# core 패키지를 import 할 수 있도록 프로젝트 루트를 경로에 추가합니다.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

from core.smartthings.client import (
    SmartThingsClient,
    SmartThingsError,
    SmartThingsAuthError,
    SmartThingsNotFoundError,
)


# 테스트 대상 플러그의 deviceId.
# deviceId는 '우리 집의 어떤 기기'를 가리키는 개인 식별자이므로 소스코드에 적지 않고
# .env의 SMARTTHINGS_DEVICE_ID에서 읽어옵니다.
# (명령줄에서 직접 넘길 수도 있습니다: python test_smartthings.py <deviceId>)
DEFAULT_DEVICE_ID = os.getenv("SMARTTHINGS_DEVICE_ID", "")

# 명령을 보낸 뒤 기기가 실제로 반응할 때까지 기다리는 시간(초).
# Zigbee 기기는 클라우드를 거쳐 오므로 즉시 반영되지 않습니다.
WAIT_AFTER_COMMAND_SEC = 5

# 상태가 바뀌지 않았을 때 다시 확인해보는 횟수
VERIFY_RETRY = 3


def now():
    """로그 앞에 붙일 현재 시각 문자열 (초 단위까지)."""
    return datetime.now().strftime("%H:%M:%S")


def log(message):
    print(f"[{now()}] {message}")


async def show_current_state(client, device_id, title):
    """대상 플러그의 현재 상태를 읽어서 출력하고, 스위치 상태를 반환합니다."""
    log(f"--- {title} ---")

    state = await client.get_switch_state(device_id)
    log(f"    스위치 상태 : {state}")

    health = await client.get_health(device_id)
    log(f"    연결 상태   : {health}")

    power = await client.get_power_watt(device_id)
    if power is None:
        log("    소비 전력   : (측정 미지원)")
    else:
        log(f"    소비 전력   : {power} W")

    return state


async def wait_until_state(client, device_id, expected):
    """명령을 보낸 뒤, 실제로 원하는 상태가 됐는지 확인합니다.

    반환값: (성공 여부, 마지막으로 읽은 상태)
    """
    for attempt in range(1, VERIFY_RETRY + 1):
        log(f"    {WAIT_AFTER_COMMAND_SEC}초 대기 중... (확인 {attempt}/{VERIFY_RETRY})")
        await asyncio.sleep(WAIT_AFTER_COMMAND_SEC)

        actual = await client.get_switch_state(device_id)
        log(f"    확인 결과   : {actual} (기대값: {expected})")

        if actual == expected:
            return True, actual

    return False, actual


async def run_readonly(client, device_id):
    """플러그를 건드리지 않고 읽기만 하는 테스트."""
    log("SmartThings 기기 목록을 조회합니다 (GET /devices)")
    devices = await client.list_devices()
    log(f"    전체 기기 수      : {len(devices)}개")

    plugs = [d for d in devices if "SmartPlug" in d["categories"]]
    log(f"    스마트플러그 개수 : {len(plugs)}개")
    for plug in plugs:
        mark = " <-- 테스트 대상" if plug["deviceId"] == device_id else ""
        log(f"      - {plug['label']} ({plug['deviceId']}){mark}")

    print()
    await show_current_state(client, device_id, "대상 플러그 현재 상태")


async def run_control_test(client, device_id):
    """실제로 플러그를 켜고, 확인하고, 원래대로 되돌리는 테스트."""
    print()
    log("=" * 62)
    log("STEP 1) 테스트 전 현재 상태 확인 (GET — 아직 아무것도 바꾸지 않음)")
    log("=" * 62)
    original_state = await show_current_state(client, device_id, "테스트 시작 전 상태")

    print()
    log("=" * 62)
    log("STEP 2) 켜기 명령 전송 (POST switch/on)")
    log("=" * 62)
    log(">>> 지금 플러그에서 딸깍 소리가 나는지 확인해 주세요 <<<")
    result = await client.turn_on(device_id)
    log(f"    SmartThings 응답 : {result}")
    log("    (ACCEPTED = 클라우드가 명령을 접수했다는 뜻. 실제 동작은 다음 단계에서 확인)")

    print()
    log("=" * 62)
    log("STEP 3) 실제로 켜졌는지 확인 (GET)")
    log("=" * 62)
    turned_on, state_after_on = await wait_until_state(client, device_id, "on")
    if turned_on:
        log("    ✅ 켜기 성공: 플러그가 실제로 'on' 상태가 되었습니다.")
        power = await client.get_power_watt(device_id)
        if power is not None:
            log(f"    현재 소비 전력 : {power} W")
            if power > 0:
                log("    ✅ 전력이 흐르고 있습니다 (충전기가 연결되어 있다는 신호)")
            else:
                log("    ⚠️  0 W입니다. 이 플러그에 충전기가 꽂혀 있는지 확인이 필요합니다.")
    else:
        log(f"    ❌ 켜기 실패: 여전히 '{state_after_on}' 입니다.")

    print()
    log("=" * 62)
    log(f"STEP 4) 원래 상태('{original_state}')로 되돌리는 명령 전송 (POST)")
    log("=" * 62)
    result = await client.send_switch_command(device_id, original_state)
    log(f"    SmartThings 응답 : {result}")

    print()
    log("=" * 62)
    log("STEP 5) 원래 상태로 돌아왔는지 확인 (GET)")
    log("=" * 62)
    restored, final_state = await wait_until_state(client, device_id, original_state)
    if restored:
        log(f"    ✅ 복원 성공: 테스트 시작 전과 동일한 '{final_state}' 상태입니다.")
    else:
        log(f"    ❌ 복원 실패: 현재 '{final_state}' 입니다. 앱에서 직접 확인해 주세요.")

    print()
    log("=" * 62)
    log("테스트 요약")
    log("=" * 62)
    log(f"    시작 상태        : {original_state}")
    log(f"    켜기 명령 결과   : {'성공' if turned_on else '실패'}")
    log(f"    원상복구 결과    : {'성공' if restored else '실패'}")
    log(f"    최종 상태        : {final_state}")


async def main():
    do_control = "--control" in sys.argv

    # deviceId를 직접 넘기고 싶으면: python test_smartthings.py --control <deviceId>
    device_id = DEFAULT_DEVICE_ID
    for arg in sys.argv[1:]:
        if not arg.startswith("--"):
            device_id = arg

    client = SmartThingsClient()

    if not client.has_token():
        print("❌ .env 파일에 SMARTTHINGS_TOKEN이 없습니다. 먼저 설정해 주세요.")
        return

    if not device_id:
        print("❌ 테스트할 deviceId가 없습니다.")
        print("   .env에 SMARTTHINGS_DEVICE_ID를 추가하거나,")
        print("   실행할 때 직접 넘겨주세요: python test_smartthings.py <deviceId>")
        return

    # 토큰 값은 절대 출력하지 않고, '있다'는 사실만 알립니다.
    log("SMARTTHINGS_TOKEN 확인됨 (값은 출력하지 않습니다)")
    log(f"테스트 대상 deviceId : {device_id}")
    print()

    try:
        await run_readonly(client, device_id)

        if do_control:
            await run_control_test(client, device_id)
        else:
            print()
            log("읽기 전용 테스트를 마쳤습니다. 플러그는 건드리지 않았습니다.")
            log("실제 제어까지 테스트하려면: python test_smartthings.py --control")

    except SmartThingsAuthError as e:
        print(f"\n❌ 인증 실패: {e}")
        print("   토큰이 만료되었거나 권한이 부족합니다. SmartThings에서 토큰을 다시 확인하세요.")
    except SmartThingsNotFoundError as e:
        print(f"\n❌ 기기를 찾을 수 없습니다: {e}")
        print(f"   deviceId가 올바른지 확인하세요: {device_id}")
    except SmartThingsError as e:
        print(f"\n❌ SmartThings 오류: {e}")
    except Exception as e:
        print(f"\n💥 예상하지 못한 오류: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
