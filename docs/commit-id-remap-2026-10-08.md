# 커밋 ID 대응표 (2026-10-08 이력 정리)

2026-10-08, 공개 이력에서 `data/users.yaml`·`data/subscriptions.yaml`(구독 앱 계정 해시)과 이미 무효화된 옛 토큰·기본 비밀번호 문자열을 제거하기 위해 이력을 다시 썼다(사용자 요청, force-with-lease). 코드 내용(HEAD 트리)은 바뀌지 않았고 커밋 ID만 바뀌었다. ADR·로그에 적힌 옛 커밋 번호는 아래 표로 찾는다.

| 옛 ID | 새 ID | 날짜 | 제목 |
|---|---|---|---|
| 74f045b | 27592f0 | 2026-05-23 | "cloudflare 유지 및 자동인지 추가" |
| 3987360 | cb08809 | 2026-05-23 | 에러 수정 |
| becff71 | 7f685af | 2026-05-23 | url 연동에러 수정 |
| f2e9bfa | 228082d | 2026-05-23 | git업로드 수정 |
| 197d3be | 3db3566 | 2026-05-23 | git 수정 |
| 812ee68 | 3a5e2fa | 2026-05-23 | "git status 변경" |
| c5cdc17 | ed010b7 | 2026-05-23 | git 설정 변경 |
| b6aef30 | 042bf47 | 2026-05-23 | git 설정 변경 |
| 9637fe5 | b2ddcfe | 2026-05-23 | tunnel url기능 수정 |
| bb19477 | de0e58e | 2026-05-23 | 터널 변경 시 파일 변화를 주어 재배포 자동화 |
| 0c5464f | 87523ce | 2026-05-23 | 뉴스 중복발행되지 않도록 수정, 채팅방 help기능 수정 |
| 3f7e2b3 | e7db45d | 2026-05-24 | streamlit ping 전달방식 수정 |
| 836c8ce | 8fbceef | 2026-05-28 | 주소 ping test 추가 및 error 시 주소 갱신 |
| ce83496 | a00a825 | 2026-05-28 | agent 오류 수정 |
| 8e8b903 | 87b2cde | 2026-05-28 | news 수정 |
| a832df6 | 07882f3 | 2026-05-28 | news 서비스 업데이트 |
| ef78d68 | cdb658f | 2026-05-30 | 260530 0032 기본web 변경 |
| bafa8ca | a4131d5 | 2026-05-30 | 260530 0049 web 배터리 잔량표기, 그래프뷰 연결 |
| 0841df6 | 4daf50a | 2026-05-30 | 260530 0058 web 뉴스 페이지 개편 |
| 487a6af | 385848f | 2026-05-30 | 260530 0108 news room error 수정 |
| 7e77cb7 | 1557967 | 2026-05-30 | 260530 0117 GEMINI.md 수정 |
| 965c667 | 435f49c | 2026-05-30 | 260530 0121 new room 뉴스 보이기 수정 |
| a09b593 | 889eebe | 2026-05-30 | 260530 0129 뉴스 수정 |
| e99bd19 | 8db0881 | 2026-05-30 | 260530 0138 뉴스룸 수정 |
| d029b3a | c2e71da | 2026-05-30 | 260530 0142 news.json 수정 |
| 6e4471d | deac929 | 2026-05-30 | 260530 0204 뉴스룸 우측 사이드바 수정 |
| 4130f1d | 668be26 | 2026-05-30 | 260530 0212 좌우사이드바 반응형 구축(모바일은 접히게) |
| 051285d | 022b9aa | 2026-05-30 | 260530 0224 온도 추가, 사이드바 수정 |
| fcf90e7 | f543714 | 2026-05-30 | 260530 0232 홈화면 수정 |
| 822c13a | 30d90c3 | 2026-05-30 | 260530 0240 AP 및 저장용량 표시 수정 |
| 585b1f7 | 1d62d94 | 2026-05-30 | 260530 0247 ap,저장용량 수정 |
| 5a0a4c7 | f01fb5f | 2026-05-30 | 260530 0259 저장용량 수정 |
| a838c3a | d1d7075 | 2026-05-30 | 260530 0307 에러 수정 |
| 8b433b3 | 82e3b49 | 2026-05-30 | 260530 0950 뉴스 키워드 수정 |
| 5ee1926 | db1791d | 2026-05-31 | 260531 1040 api 전송 안정화, 재연결 로직 등 수정 |
| 02069c6 | 3163467 | 2026-05-31 | 260531 2350 butler-tunnel 에러 수정 |
| a3ffd7d | e965efd | 2026-05-31 | 260531 2355 butler-tunnel 통신에러 수정 |
| 51271a7 | 9328e07 | 2026-06-01 | 260601 0027 주소창 공유방 변경 |
| 1fd5c1a | f3aa32e | 2026-06-02 | 260602 0126 gitignore 수정 |
| 0975810 | 1df1b2a | 2026-06-02 | 260602 0119 ignore test |
| 100b2d8 | 5332493 | 2026-06-02 | 260602 0122 .gitignore test2 |
| 17055ca | 4aef991 | 2026-06-02 | fix: remove sensitive data from git tarcking |
| 389dc9a | be61f10 | 2026-06-02 | 260602 0134 홈화면의 Add News Keyword 작동 구현 |
| 156e0ca | 6096193 | 2026-06-02 | 260602 0144 api 호출 보완 강화 |
| cd24d50 | 43aab30 | 2026-06-02 | 260602 0150 web 뉴스관리기능 수정 |
| c9a2a99 | 2d36acd | 2026-06-02 | 260602 0157 뉴스기능 수정 |
| 624e43e | a8f534b | 2026-06-02 | 260602 0200 뉴스 관리 버튼 수정 |
| 8484892 | 75dda95 | 2026-06-02 | 260602 0206 제목 수정 및 서버 상태 로딩 최적화 |
| 6d722a9 | febe7c9 | 2026-06-02 | 260602 2318 butler-tunnel오류 |
| 0d999d3 | 687cae9 | 2026-06-02 | 260602 2356 butler-tunnel 오류 수정 |
| fe07609 | e9f7894 | 2026-06-03 | 260603 0019 휴대폰 정보 개선 |
| c849989 | 4ac544b | 2026-06-03 | 260603 0055 정보 수집 느림 개선 |
| dc70e8c | 2a2d061 | 2026-06-03 | 260603 0112 web 로딩 개선 |
| 39b9ed6 | e840db2 | 2026-06-03 | 260603 0119 수정 |
| fc33fca | bbf742a | 2026-06-03 | 260603 0132 뉴스룸 최적화 |
| 3dc6dff | b5cef69 | 2026-06-03 | 260603 0140 뉴스룸 업데이트 |
| 5623ed2 | 5cfbcaa | 2026-06-03 | 260603 0147 뉴스룸 에러 수정 |
| 6ee01ab | c7b9d58 | 2026-06-03 | 260603 0154 뉴스룸 개선 |
| 4b5e1e9 | c9f3642 | 2026-06-03 | 260603 0205 뉴스룸 그루핑 |
| 37f249f | 06f37a3 | 2026-06-03 | 260603 2157 graph view 개선 |
| cc402c0 | 90d40cb | 2026-06-04 | 260604 0013 srt 예약 추가 at web |
| 61c46da | f5b4296 | 2026-06-04 | 260604 0028 srt 기능 개선 |
| 96d9ed6 | 570cbb7 | 2026-06-04 | 260604 0034 srt 기능개선 |
| d32d73e | 9ad2a5b | 2026-06-04 | 260604 0044 기차 수정 |
| 882e16b | ee4337f | 2026-06-04 | 260604 0058 srt 수정 |
| 7888437 | 2c5778e | 2026-06-04 | 260604 0110 srt 기능 수정 |
| 87c22b6 | e0b0603 | 2026-06-04 | 260604 0123 srt 기능 개선 |
| 35de733 | 650501c | 2026-06-04 | 260604 2020 butler url 인지 수정 |
| ee43f22 | d8c1267 | 2026-06-20 | 260620 0044 다시진행 |
| 5f0684f | 27596f0 | 2026-06-20 | 260620 0054 feat: add mock warning, balance reset, and dynamic currency symbols to dashboa |
| ede6d6d | c168328 | 2026-06-20 | 260620 0125 feat: remove max investment limit & add daily maximum loss limit (%) Panic Sto |
| da22dfb | 0adf639 | 2026-06-20 | 260620 0143 feat: 보조 지표(ADX, RSI, VWAP 표준편차 밴드) 설정 폼 및 봇 제 |
| e7a1e03 | 85b6da1 | 2026-06-25 | 260625 0145 secret key 동작 방식 변경 |
| b1c55a9 | c331d96 | 2026-06-25 | 260625 2120 회식계산 추가 |
| 5767819 | a70da6c | 2026-06-25 | 260625 2141 정산탭 개선 |
| f56ed6a | c8b67f4 | 2026-06-25 | 260625 2152 개선 |
| bca5e27 | 7555a50 | 2026-06-25 | 260625 2314 vwap수정 - 실제거래 탭 분리 |
| 8e4f912 | 88736e9 | 2026-06-25 | 260625 2343 vwap 개선 |
| 155c4fb | 0f280ba | 2026-06-26 | 260626 0005 vwap 개선 |
| 019385f | c858f0a | 2026-06-26 | 260626 0016 vwap 수정 |
| 39591b0 | 33d691a | 2026-06-26 | 260626 0105 vwap 개선 |
| 8342b5e | 7cc2665 | 2026-06-26 | 260626 1745 vwap 거래시작시간 추가 |
| c54c1d0 | 9f818c9 | 2026-06-26 | 260626 1805 vwap 가상거래봇 3개로 증설 |
| ea8c673 | 12071dc | 2026-06-26 | 260626 2303 vwap 개별거래봇 이슈해결 |
| a962ba6 | 36598f1 | 2026-06-27 | 260627 0003 vwap 유저가이드 |
| b660852 | 09138b2 | 2026-06-27 | 260627 0125 vwap 종합자산현황 개선 |
| f92dfd0 | c3a9d85 | 2026-06-27 | 260627 1227 Implement VWAP bot status persistence and auto-recovery on restart |
| 260a61e | b2fa027 | 2026-06-27 | 260627 1313 Fix TOSS account ROI calculation and implement manual refresh and masking for  |
| b44601b | 546656d | 2026-06-27 | 260627 1714 Unmask bot performance, open orders and history, keep only total assets and ho |
| c992d96 | 50bb70e | 2026-06-30 | 260630 0031 vwap 거래 변경 |
| f7ee4d6 | e6d5f84 | 2026-06-30 | 260630 0050 평가자산 살리기 |
| 9521386 | d3ced96 | 2026-06-30 | 260630 0055 자산살리기 |
| 0b4efe4 | e69311f | 2026-06-30 | 260630 0101 투자비중 로직 수정 |
| 68dea90 | ff8652b | 2026-06-30 | 260630 0106 손실한도 수정 |
| 00eb8d7 | ca1faa5 | 2026-06-30 | 260630 0113 api 통신 수정 |
| 34a5214 | 9288ac5 | 2026-06-30 | 260630 2321 vwap 실거래 자산 수정 |
| 9146264 | e185ef9 | 2026-07-08 | 260708 2157 web srt예매 출발시간 선택박스에서 입력으로 바꿈 |
| 4801205 | d3645ee | 2026-07-14 | 리팩토링: 루트 정리 + core/ 기능별 재구성, S9 배포 rsync 전환 |
| ffa0885 | 495caa0 | 2026-07-14 | fix: core/subscription/service.py DATA_DIR가 리팩토링 후 경로 깊이 보정이 빠 |
| 4ea33d4 | 05b0a3b | 2026-07-14 | fix: cloudflared 진단 로그의 환경변수 덤프를 새 터널 URL로 오인하는 � |
| 11753cd | c466286 | 2026-07-14 | 260714 2121 vwap 기능 수정 |
| e7d0dc4 | 289b8c0 | 2026-09-03 | 260903 2318 fix: tunnel_manager notify_via_butler exception handling + non-blocking |
| ead9745 | 526fb55 | 2026-09-04 | 260904 0017 feat: news_service Discord embed + near-duplicate clustering (1.1-1.2) |
| f3ac554 | dd8e5be | 2026-09-04 | 260904 0021 feat: news.html search/sort/group filter + bookmark/read state (1.3-1.4) |
| ca61f15 | 319daa7 | 2026-09-04 | 260904 0029 feat: activity feed backbone + pending-action approval UI + stats history spar |
| e35cd40 | c293af1 | 2026-09-04 | 260904 0030 fix: cap pending_actions API response to avoid rendering 1200+ item backlog |
| 175e16e | 1ae2e59 | 2026-09-04 | 260904 0037 feat: VWAP fill/start-stop events into activity feed + real-time system map no |
| d16f546 | b8e6f05 | 2026-09-04 | 260904 0058 refactor: remove right sidebar (Graph View, activity feed, pending-action appr |
| 689590a | 0e2d065 | 2026-09-11 | Fix S9 dashboard status metrics (CPU, sparklines, history) |
| a7e3ae3 | d23232d | 2026-09-11 | Redefine AP usage metric to Butler process CPU (S9 permission limits) |
| 1875287 | b857be1 | 2026-09-11 | Add ADR-0001: AP usage metric redefinition |
| 64a91a3 | ddc248e | 2026-09-11 | Add SmartThings battery guard: auto charge cutoff at 90%/30% |
| 0bf4667 | 744fb4c | 2026-09-11 | Add ADR-0002: battery guard polling architecture |
| 041c11b | 725b5e8 | 2026-09-12 | Move dashboard sparklines beside each card value, auto-scale Y axis |
| c551d2a | 09475ef | 2026-09-12 | Add temperature sparkline to dashboard |
| be6fd64 | d47ccfb | 2026-10-07 | Pause battery guard in favor of SmartThings routine (ADR-0003) |
| 3e61f45 | de54f50 | 2026-10-07 | Merge settlement amount/ratio tables into one, fix tab order |
| 6d8fa0d | 3e785f2 | 2026-10-07 | Add liquor purchase tracker page (/liquor) (ADR-0004, ADR-0005) |
| ed25b7d | 950c871 | 2026-10-07 | Add VWAP strategy research backtest harness (ADR-0006) |
| f1da81f | d40052d | 2026-10-07 | VWAP: fix fill detection and virtual bots, add decision transparency, fix session boundary |
| 24d7c24 | 997acf0 | 2026-10-07 | VWAP stage 3 Phase A: strategy plugin, bar storage, shadow/replay UI mockups; keep stop-lo |
| 60fa70f | a60fd3a | 2026-10-07 | VWAP stage 3 Phase B: post-cycle hooks, live bar storage, one-click replay backend |
| 9671032 | a61f207 | 2026-10-07 | VWAP: block REAL trading on untrusted candles, remove mock_mode race (ADR-0010) |
| 2cf03a7 | 0c276ac | 2026-10-07 | Update ADR-0010 status: deployed in 8ae6953 |
| 85d609a | 86886f7 | 2026-10-07 | VWAP stage 3 Phase C: shadow mode and live replay/shadow dashboard |
| 35fbbf8 | 36ea836 | 2026-10-07 | Mark ADR-0007 Accepted: stage 3 deployed (9ead037) |
| 5cb30a7 | 5ca2c34 | 2026-10-07 | VWAP dashboard: decision explainer tab, manual tab rendered from md |
| 94ce5fa | 0a96d58 | 2026-10-07 | Route VWAP Discord alerts to VWAP_CHANNEL_ID (fallback: status channel) |
| 59394be | 49142bb | 2026-10-08 | Butler auth boundary: remove public token exposure, session auth, login hardening (ADR-001 |
| cdee713 | fe62f0f | 2026-10-08 | Make main dashboard and SRT page public; keep SRT queue/reserve and keyword admin behind l |

## 1차 정리(2026-10-07) 이전 ID

아래 ID는 2026-10-07 1차 이력 정리(종목명·성과 수치 가림) 이전의 로컬 ID라 공개된 적이 없다. ADR-0007~0011, 사용자 가이드, 오케스트레이션 로그에 이 번호로 적혀 있다. 커밋 제목으로 찾은 현재 ID다.

| 문서상 ID | 현재 ID | 제목 |
|---|---|---|
| be6fd64 | d47ccfb | Pause battery guard |
| 3e61f45 | de54f50 | Merge settlement amount |
| 6d8fa0d | 3e785f2 | Add liquor purchase tracker |
| 8d4190a | 950c871 | Add VWAP strategy research backtest |
| a72d12d | d40052d | VWAP: fix fill detection |
| 0c05162 | 997acf0 | VWAP stage 3 Phase A |
| 4fa93a0 | a60fd3a | VWAP stage 3 Phase B |
| 8ae6953 | a61f207 | VWAP: block REAL trading on untrusted |
| 2665ddd | 0c276ac | Update ADR-0010 status |
| 9ead037 | 86886f7 | VWAP stage 3 Phase C |
| ce71492 | 36ea836 | Mark ADR-0007 Accepted |
