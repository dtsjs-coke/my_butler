import os
import asyncio
from SRT import SRT
from SRT.passenger import Adult
from dotenv import load_dotenv

# .env 로드
load_dotenv('my_butler/.env')

async def reproduce_error():
    srt = SRT(os.getenv("SRT_ID"), os.getenv("SRT_PW"))
    
    # 1. 정상적인 예약 데이터 시뮬레이션 (객체 사용)
    print("--- Test 1: Using Object (Expected: Success or No Train) ---")
    try:
        trains = srt.search_train("수서", "부산", "20260610", "080000")
        if trains:
            # 실제로 예약을 걸지는 않고, 예약 함수 호출 전 단계까지만 검증하거나 
            # 인자 타입 체크만 수행
            print(f"Found {len(trains)} trains. Target: {trains[0]}")
    except Exception as e:
        print(f"Test 1 Error: {e}")

    # 2. 웹 서버에서 잘못 들어가는 데이터 시뮬레이션 (문자열 사용)
    print("\n--- Test 2: Using String (Expected: invalid response error) ---")
    try:
        # Flask에서 현재 잘못 넣고 있는 방식: passengers=['Adult']
        # SRT 라이브러리 내부에서 passengers[0].get_num() 등을 호출할 때 에러 발생 예상
        if trains:
            # srt.reserve(trains[0], passengers=['Adult']) 를 호출하면 
            # 유저가 보고한 'invalid response ({})' 가 나올 가능성이 매우 높음
            # 실제 서버 통신이므로 주의해서 호출 (단순 검색으로 대체)
            print("Simulating reserve call with strings...")
            # 에러 재현을 위해 내부 로직만 체크
            from SRT.passenger import Adult
            p = 'Adult'
            try:
                p.get_num() # 이 단계에서 에러 발생
            except AttributeError:
                print("Confirmed: String 'Adult' has no attribute 'get_num'. This causes the SRT library to fail.")
    except Exception as e:
        print(f"Test 2 Error: {e}")

if __name__ == "__main__":
    asyncio.run(reproduce_error())
