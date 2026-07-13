
# SRT 예약 서비스
## 작동방식 
'''
### 예약
1. 디스코드 SRT 방에 !srt를 호출하면 예약할지,현재 예약작업이 있는지, 예약정보확인할지, 취소할지 선택박스혹은 4개의 선택지로 보여주기.
2. 예약 선택
3. 출발지 및 목적지 드롭박스 형태로 순서대로 선택하는 창 띄우기, 다음/이전 버튼 제공
4. 출발 일자(yyyyMMdd), 출발시간(hhmmss), 출발시간조회한도(hhmmss) 입력창 띄우기, 다음/이전 버튼 제공
5. 예약자 수(int) 입력(>>>어른,어린이,경로,장애 칸 숫자 입력)창 띄우기,  다음/이전 버튼 제공
6. 좌석 타입 및 창가 유무 체크박스 (필수입력아님, 체크 안하면 기본타입으로 제공), 예약/취소 버튼  제공
7. 예약 안될경우 예약대기로 진행.
*입력하기위한 별도의 창 구현
*예약작업 중단 가능해야함.
*현재 예약중인 동작 있는지 확인 필요함
*예약된 좌석정보 불러오기 기능 추가
*예약 취소기능 만들어는 놓고 오픈하지말기

'''

*** 각 기능들이 유기적인 동작

# SRT Package 설명


from SRT import SRT

## 0. SRT 클라이언트 클래스
class SRT.SRT(srt_id: str, srt_pw: str, auto_login: bool = True, verbose: bool = False)
'''
Parameters:
srt_id (str) – SRT 계정 아이디 (멤버십 번호, 이메일, 전화번호)
srt_pw (str) – SRT 계정 패스워드
auto_login (bool) – login() 함수 호출 여부
verbose (bool) – 디버깅용 로그 출력 여부
'''

login(srt_id: str | None = None, srt_pw: str | None = None)
SRT 서버에 로그인합니다.
일반적인 경우에는 인스턴스가 생성될 때에 자동으로 로그인 되므로, 이 함수를 직접 호출할 필요가 없습니다.

Parameters:
srt_id (str, optional) – SRT 계정 아이디
srt_pwd (str, optional) – SRT 계정 패스워드
Returns:로그인 성공 여부
Return type:bool

logout()→ bool
SRT 서버에서 로그아웃합니다.


## 1. 로그인
>>>srt = SRT(SRT_ID,SRT_PW)

## 2. SRT 열차 검색 
search_train(dep: str, arr: str, date: str | None = None, time: str | None = None, time_limit: str | None = None, available_only: bool = True)→ list[SRT.train.SRTTrain]
주어진 출발지에서 도착지로 향하는 SRT 열차를 검색합니다.
'''
Parameters:
dep (str) – 출발역
arr (str) – 도착역
date (str, optional) – 출발 날짜 (yyyyMMdd) (default: 당일)
time (str, optional) – 출발 시각 (hhmmss) (default: 0시 0분 0초)
time_limit (str, optional) – 출발 시각 조회 한도 (hhmmss)
available_only (bool, optional) – 매진되지 않은 열차만 검색합니다 (default: True)

Returns:열차 리스트
Return type:list[SRTTrain]

'''

### 결과 예시
- time_limit 미기입
>>> trains = srt.search_train("동탄", "광주송정", "20260608", "050000", available_only=True)
접속자가 많아 대기열에 들어갑니다.
대기인원: 4명
>>> trains
[[SRT 601] 06월 08일, 동탄~광주송정(05:56~07:28) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 9601] 06월 08일, 동탄~광주송정(05:56~07:28) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 653] 06월 08일, 동탄~광주송정(06:56~08:37) 특실 매진, 일반실 예약가능, 예약대기 불가능, [SRT 605] 06월 08일, 동탄~광주송정(08:48~10:29) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 655] 06월 08일, 동탄~광주송정(09:56~11:31) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 607] 06월 08일, 동탄~광주송정(10:37~12:19) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 611] 06월 08일, 동탄~광주송정(14:26~15:57) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 661] 06월 08일, 동탄~광주송정(16:29~17:56) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 663] 06월 08일, 동탄~광주송정(17:27~18:58) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 615] 06월 08일, 동탄~광주송정(18:22~19:54) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 665] 06월 08일, 동탄~광주송정(19:26~21:09) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 617] 06월 08일, 동탄~광주송정(19:58~21:28) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 667] 06월 08일, 동탄~광주송정(21:26~22:58) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 621] 06월 08일, 동탄~광주송정(23:13~00:42) 특실 예약가능, 일반실 예약가능, 예약대기 불가능]

- time_limit 기입
>>> trains = srt.search_train("동탄", "광주송정", "20260608", "050000","060000", available_only=True)
>>> trains

[[SRT 601] 06월 08일, 동탄~광주송정(05:56~07:28) 특실 예약가능, 일반실 예약가능, 예약대기 불가능, [SRT 9601] 06월 08일, 동탄~광주송정(05:56~07:28) 특실 예약가능, 일반실 예약가능, 예약대기 불가능]


## 3. 열차 예약
reserve(train: SRTTrain, passengers: list[SRT.passenger.Passenger] | None = None, special_seat: SeatType = SeatType.GENERAL_FIRST, window_seat: bool | None = None)→ SRTReservation
열차를 예약합니다.

### 사용 예시
>>>srt.reserve(trains[0])
'''
Parameters:
train (SRTrain) – 예약할 열차
passengers (list[Passenger], optional) – 예약 인원 (default: 어른 1명)
special_seat (SeatType) – 일반실/특실 선택 유형 (default: 일반실 우선)
window_seat (bool, optional) – 창가 자리 우선 예약 여부

Returns:예약 내역
Return type:SRTReservation
'''

** SeatType 사용은 from SRT import SeatType 필요.
SeatType.GENERAL_FIRST : 일반실 우선
SeatType.GENERAL_ONLY : 일반실만
SeatType.SPECIAL_FIRST : 특실 우선
SeatType.SPECIAL_ONLY : 특실만
* SeatType  예시:일반실 우선 예약
>>>from SRT import SeatType
>>>srt.reserve(self, trains[0], special_seat=SeatType.GENERAL_FIRST)

** 여러명 예약
예시) 어른 2명, 어린이 1명 예약

>>> from SRT.passenger import Adult, Child
>>> srt.reserve(trains[0], passengers=[Adult(), Adult(), Child()])
Adult: 어른/청소년
Child: 어린이
Senior: 경로
Disability1To3: 장애 1~3급
Disability4To6: 장애 4~6급


## 4. 예약대기 신청
reserve_standby(train: SRTTrain, passengers: list[SRT.passenger.Passenger] | None = None, special_seat: SeatType = SeatType.GENERAL_FIRST, mblPhone: str | None = None)→ SRTReservation
열차대기 신청합니다.

>>>srt.reserve_standby(trains[0])

'''
Parameters:
train (SRTrain) – 예약할 열차
passengers (list[Passenger], optional) – 예약 인원 (default: 어른 1명)
special_seat (SeatType) – 일반실/특실 선택 유형 (default: 일반실 우선)
mblPhone (str, optional) – 휴대폰 번호

Returns:예약 내역
Return type:SRTReservation
'''

## 5. 예약대기 옵션 적용
reserve_standby_option_settings(reservation: SRTReservation | int, isAgreeSMS: bool, isAgreeClassChange: bool, telNo: str | None = None)→ bool

>>>trains = srt.search_train("수서", "부산", "210101", "000000")
>>>srt.reserve_standby(trains[0])
>>>srt.reserve_standby_option_settings("1234567890", True, True, "010-1234-xxxx")

Parameters:
reservation (SRTReservation or int) – 예약 번호
isAgreeSMS (bool) – SMS 수신 동의 여부
isAgreeClassChange (bool) – 좌석등급 변경 동의 여부
telNo (str, optional) – 휴대폰 번호

Returns:예약대기 옵션 적용 성공 여부
Return type:bool


## 6. 예약 정보 조회
get_reservations(paid_only: bool = False)→ list[SRT.reservation.SRTReservation]
전체 예약 정보를 얻습니다.

Parameters:
paid_only (bool) – 결제된 예약 내역만 가져올지 여부

Returns:예약 리스트
Return type:list[SRTReservation]


## 7. 예약에 포함된 티켓 정보
ticket_info(reservation: SRTReservation | int)→ list[SRT.reservation.SRTTicket]
예약에 포함된 티켓 정보를 반환합니다.

>>>reservations = srt.get_reservations()
>>>reservations
# [[SRT] 09월 30일, 수서~부산(15:30~18:06) 130700원(3석), 구입기한 09월 19일 19:11]
>>>reservations[0].tickets
# [18호차 9C (일반실) 어른/청소년 [52300원(600원 할인)],
# 18호차 10C (일반실) 어른/청소년 [52300원(600원 할인)],
# 18호차 10D (일반실) 장애 4~6급 [26100원(26800원 할인)]]


Parameters:
reservation (SRTReservation or int) – 예약 번호

Returns:list[SRTTicket]


## 8. 예약 취소
cancel(reservation: SRTReservation | int)→ bool
예약을 취소합니다.

>>>reservation = srt.reserve(train)
>>>srt.cancel(reservation)
>>>reservations = srt.get_reservations()
>>>srt.cancel(reservations[0])

Parameters:
reservation (SRTReservation or int) – 예약 번호

Returns:예약 취소 성공 여부
Return type:bool

