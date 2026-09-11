"""SmartThings REST API 클라이언트 (얇은 래퍼)

이 모듈이 하는 일은 딱 하나입니다:
"SmartThings 서버에 말을 거는 방법"만 알고 있습니다.

배터리가 몇 %일 때 플러그를 켜고 끌지 같은 '정책/판단'은 이 파일이 아니라
core/smartthings/battery_guard.py (다음 단계에서 작성)가 담당합니다.
이렇게 나눠두면 나중에 Discord 명령어(!플러그 켜)나 웹 대시보드 토글을 붙일 때
이 파일을 그대로 재사용할 수 있습니다.

[보안 규칙 — 중요]
- 토큰은 오직 .env 파일의 SMARTTHINGS_TOKEN 값을 os.getenv()로만 읽습니다.
- 토큰 값은 절대 print / 로그 / Discord 메시지 / 예외 메시지에 넣지 않습니다.
  (HTTP 요청의 Authorization 헤더에만 들어갑니다)
- 이 모듈은 응답 본문(body)을 에러 메시지에 넣을 때 앞부분 200자만 잘라 씁니다.

[이벤트 루프 규칙 — 중요]
- my_butler는 discord.py 기반이라 메인 이벤트 루프를 막으면 봇 전체가 멈춥니다.
- 그래서 동기 라이브러리인 requests가 아니라 비동기 라이브러리 aiohttp를 씁니다.
  (core/news_service.py 가 네이버 API를 호출할 때 쓰는 방식과 동일합니다)
"""

import os
import asyncio

import aiohttp


# SmartThings API의 기본 주소
BASE_URL = "https://api.smartthings.com/v1"

# 네트워크 응답을 기다릴 최대 시간(초).
# S9는 구형 기기이고 가정용 인터넷을 쓰므로 조금 넉넉하게 잡습니다.
DEFAULT_TIMEOUT_SEC = 10.0


# ---------------------------------------------------------------------------
# 예외(Exception) 정의
#
# 호출하는 쪽(battery_guard)이 "무슨 종류의 실패인지"에 따라 다르게 대응할 수
# 있도록, 실패 상황을 종류별로 나눠둡니다.
#
#   - SmartThingsAuthError     : 토큰이 잘못됐거나 만료됨 -> 재시도해도 소용없음.
#                                사용자에게 알려야 하는 상황.
#   - SmartThingsNotFoundError : deviceId가 틀림 -> 설정을 고쳐야 하는 상황.
#   - SmartThingsRateLimited   : 너무 자주 호출함 -> 이번 주기만 건너뛰면 됨.
#   - SmartThingsServerError   : SmartThings 서버 쪽 문제 -> 다음 주기에 재시도.
#   - SmartThingsNetworkError  : 인터넷이 끊겼거나 응답이 너무 느림 -> 다음 주기 재시도.
# ---------------------------------------------------------------------------

class SmartThingsError(Exception):
    """SmartThings 관련 모든 오류의 부모 클래스.

    status: HTTP 상태 코드 (네트워크 오류처럼 상태 코드가 없으면 None)
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class SmartThingsAuthError(SmartThingsError):
    """401/403. 토큰이 없거나, 만료됐거나, 권한이 부족합니다."""
    pass


class SmartThingsNotFoundError(SmartThingsError):
    """404. 그런 deviceId를 가진 기기가 없습니다."""
    pass


class SmartThingsRateLimited(SmartThingsError):
    """429. 짧은 시간에 너무 많이 호출했습니다."""
    pass


class SmartThingsServerError(SmartThingsError):
    """500번대. SmartThings 클라우드 쪽 일시적 문제입니다."""
    pass


class SmartThingsNetworkError(SmartThingsError):
    """인터넷 끊김, DNS 실패, 타임아웃 등. 응답 자체를 받지 못한 경우입니다."""
    pass


class SmartThingsClient:
    """SmartThings API를 호출하는 객체.

    사용 예시:

        client = SmartThingsClient()
        state = await client.get_switch_state(device_id)   # "on" 또는 "off"
        await client.turn_off(device_id)

    주의: 이 클래스는 '기기 상태를 읽고 명령을 보내는 것'까지만 합니다.
          명령을 보냈다고 해서 기기가 실제로 동작했다는 보장은 없으므로
          (아래 send_switch_command 설명 참고), 확인이 필요하면 호출하는 쪽에서
          잠시 후 get_switch_state()로 다시 읽어 확인해야 합니다.
    """

    def __init__(self, token=None, timeout_sec=DEFAULT_TIMEOUT_SEC):
        """
        token: 직접 넘기지 않으면 환경변수 SMARTTHINGS_TOKEN에서 읽습니다.
               (테스트용으로 다른 토큰을 넣을 수 있게 인자로도 받아둡니다)
        timeout_sec: 응답을 기다릴 최대 시간(초).
        """
        # os.getenv는 .env가 load_dotenv()로 이미 읽혀 있을 때 값을 돌려줍니다.
        # butler_pro.py가 시작할 때 load_dotenv()를 호출하므로 봇 안에서는 자동입니다.
        # 단독 스크립트에서 쓸 때는 스크립트 맨 위에서 load_dotenv()를 먼저 호출해야 합니다.
        self._token = token if token else os.getenv("SMARTTHINGS_TOKEN")
        self._timeout_sec = timeout_sec

    def has_token(self):
        """토큰이 설정돼 있는지만 알려줍니다. (토큰 값 자체는 반환하지 않습니다)"""
        return bool(self._token)

    # -----------------------------------------------------------------------
    # 내부 공통 함수
    # -----------------------------------------------------------------------

    async def _request(self, method, path, json_body=None):
        """SmartThings API에 요청을 보내고, 성공하면 응답을 dict로 돌려줍니다.

        method    : "GET" 또는 "POST"
        path      : BASE_URL 뒤에 붙일 경로 (예: "/devices/xxxx/status")
        json_body : POST일 때 보낼 내용 (dict). GET이면 None.

        실패하면 위에서 정의한 예외 중 하나를 던집니다.
        """
        if not self._token:
            # 토큰이 없는 것은 '인증 실패'와 같은 대응(사용자에게 알림)이 필요하므로
            # 같은 예외 종류로 묶습니다.
            raise SmartThingsAuthError(
                "환경변수 SMARTTHINGS_TOKEN이 설정되어 있지 않습니다. "
                ".env 파일을 확인하세요."
            )

        url = BASE_URL + path
        headers = {
            # 토큰은 오직 여기에만 들어갑니다.
            "Authorization": "Bearer " + self._token,
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        timeout = aiohttp.ClientTimeout(total=self._timeout_sec)

        try:
            # 세션을 요청마다 새로 만듭니다.
            # 5분에 한 번 호출하는 용도라 성능 손해는 사실상 없고,
            # 반대로 세션을 오래 들고 있으면 봇이 재연결될 때
            # "이미 닫힌 이벤트 루프에 묶인 세션" 문제가 생길 수 있어 이 방식이 안전합니다.
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=json_body) as resp:
                    status = resp.status
                    text = await resp.text()

        except asyncio.TimeoutError:
            raise SmartThingsNetworkError(
                f"SmartThings 응답이 {self._timeout_sec}초 안에 오지 않았습니다 (타임아웃)."
            )
        except aiohttp.ClientError as e:
            # DNS 실패, 연결 거부, 인터넷 끊김 등
            raise SmartThingsNetworkError(
                f"SmartThings에 연결하지 못했습니다: {type(e).__name__}"
            )

        # --- 여기서부터는 응답을 받긴 받은 경우 ---

        if status == 200:
            # 정상 응답. 본문이 비어있을 수도 있으므로 방어적으로 처리합니다.
            if not text:
                return {}
            try:
                # aiohttp의 resp.json()은 Content-Type이 다르면 에러를 내므로
                # 직접 파싱합니다.
                import json as _json
                return _json.loads(text)
            except Exception:
                raise SmartThingsServerError(
                    "SmartThings 응답을 JSON으로 해석하지 못했습니다: " + text[:200],
                    status=status,
                )

        # 에러 본문은 토큰을 포함하지 않지만, 길 수 있으므로 앞 200자만 씁니다.
        snippet = text[:200] if text else ""

        if status in (401, 403):
            raise SmartThingsAuthError(
                f"SmartThings 인증 실패(HTTP {status}). "
                f"토큰이 만료되었거나 권한이 부족합니다. 응답: {snippet}",
                status=status,
            )
        if status == 404:
            raise SmartThingsNotFoundError(
                f"SmartThings에서 대상을 찾을 수 없습니다(HTTP 404). "
                f"deviceId가 올바른지 확인하세요. 응답: {snippet}",
                status=status,
            )
        if status == 429:
            raise SmartThingsRateLimited(
                f"SmartThings 호출 한도를 초과했습니다(HTTP 429). 응답: {snippet}",
                status=status,
            )
        if 500 <= status < 600:
            raise SmartThingsServerError(
                f"SmartThings 서버 오류(HTTP {status}). 응답: {snippet}",
                status=status,
            )

        raise SmartThingsError(
            f"예상하지 못한 응답(HTTP {status}). 응답: {snippet}",
            status=status,
        )

    # -----------------------------------------------------------------------
    # 읽기(GET) 기능
    # -----------------------------------------------------------------------

    async def list_devices(self):
        """계정에 연결된 모든 기기 목록을 간단한 형태로 돌려줍니다.

        반환값 예시:
            [
                {
                    "label": "거실 스마트 플러그",
                    "deviceId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                    "categories": ["SmartPlug"],
                    "has_switch": True,
                },
                ...
            ]
        """
        data = await self._request("GET", "/devices")

        result = []
        for device in data.get("items", []) or []:
            capabilities = set()
            categories = set()

            for component in device.get("components", []) or []:
                for capability in component.get("capabilities", []) or []:
                    capability_id = capability.get("id")
                    if capability_id:
                        capabilities.add(capability_id)
                for category in component.get("categories", []) or []:
                    category_name = category.get("name")
                    if category_name:
                        categories.add(category_name)

            result.append({
                "label": device.get("label") or device.get("name"),
                "deviceId": device.get("deviceId"),
                "categories": sorted(categories),
                "has_switch": "switch" in capabilities,
            })

        return result

    async def get_switch_state(self, device_id):
        """플러그(스위치)가 지금 켜져 있는지 꺼져 있는지 읽어옵니다.

        반환값: "on" 또는 "off" (문자열)

        이 프로젝트의 설계에서 '플러그의 진짜 상태'는 항상 이 함수로 확인합니다.
        로컬 파일에 "지금 켜져 있음" 같은 플래그를 저장해두지 않는 이유는,
        봇이 재시작되거나 사용자가 SmartThings 앱에서 직접 플러그를 조작하면
        그 플래그가 실제와 어긋나기 때문입니다.
        """
        path = f"/devices/{device_id}/components/main/capabilities/switch/status"
        data = await self._request("GET", path)

        # 응답 예시: {"switch": {"value": "off", "timestamp": "..."}}
        value = (data.get("switch") or {}).get("value")

        if value not in ("on", "off"):
            raise SmartThingsError(
                f"스위치 상태를 해석하지 못했습니다. 받은 값: {value!r}"
            )
        return value

    async def get_health(self, device_id):
        """기기가 지금 온라인인지 확인합니다.

        반환값: "ONLINE", "OFFLINE", "UNHEALTHY" 등의 문자열.

        플러그가 OFFLINE이면 명령을 보내도 실제로는 동작하지 않으므로,
        호출하는 쪽에서 명령 전에 확인하는 용도로 쓸 수 있습니다.
        """
        data = await self._request("GET", f"/devices/{device_id}/health")
        return data.get("state") or "UNKNOWN"

    async def get_power_watt(self, device_id):
        """플러그가 지금 몇 W를 쓰고 있는지 읽어옵니다. (전력 측정 지원 기기만)

        반환값: 실수(float) 또는 None(해당 기능이 없는 기기).

        "플러그는 켜져 있다고 나오는데 실제로 충전이 안 되는" 상황을
        눈으로 확인할 때 유용합니다. (0W면 아무것도 안 꽂혀 있거나 꺼진 상태)
        """
        data = await self._request("GET", f"/devices/{device_id}/status")

        main = (data.get("components") or {}).get("main") or {}
        power_meter = main.get("powerMeter") or {}
        power = power_meter.get("power") or {}
        value = power.get("value")

        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -----------------------------------------------------------------------
    # 쓰기(POST) 기능 — 실제로 플러그를 켜고 끕니다
    # -----------------------------------------------------------------------

    async def send_switch_command(self, device_id, command):
        """플러그에 켜기/끄기 명령을 보냅니다.

        command: "on" 또는 "off"

        반환값: SmartThings가 돌려준 결과 dict
                (보통 {"results": [{"id": "...", "status": "ACCEPTED"}]})

        [중요] "ACCEPTED"는 'SmartThings 클라우드가 명령을 접수했다'는 뜻이지
        '플러그가 실제로 켜졌다'는 뜻이 아닙니다. Zigbee 통신이 실패하면
        접수는 됐는데 기기는 그대로일 수 있습니다.
        그래서 확실히 확인하려면 몇 초 뒤에 get_switch_state()로 다시 읽어야 합니다.
        """
        if command not in ("on", "off"):
            # 오타로 엉뚱한 명령이 나가는 것을 막습니다.
            raise ValueError(f"command는 'on' 또는 'off'여야 합니다. 받은 값: {command!r}")

        body = {
            "commands": [
                {
                    "component": "main",
                    "capability": "switch",
                    "command": command,
                }
            ]
        }
        return await self._request("POST", f"/devices/{device_id}/commands", json_body=body)

    async def turn_on(self, device_id):
        """플러그를 켭니다 (= S9 충전 시작)."""
        return await self.send_switch_command(device_id, "on")

    async def turn_off(self, device_id):
        """플러그를 끕니다 (= S9 충전 중단)."""
        return await self.send_switch_command(device_id, "off")
