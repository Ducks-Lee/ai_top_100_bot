# 문서 안내

상황에 맞는 문서를 골라 읽는다.

| 이럴 때 | 볼 문서 |
|---|---|
| 전체 구조를 한눈에 보고 싶다 | [아키텍처](architecture.md): 흐름도, 메시지 처리 순서, 재연결, 설계 이유 |
| `gateway.py`, `bot.py` 코드를 한 줄씩 이해하고 싶다 | [코드 해설](code_walkthrough.md): 초보자용, Python 비동기 개념부터 |
| 모르는 용어가 나왔다 | [용어집](glossary.md) |
| 카카오 API 형식이나 제한을 확인하고 싶다 | [카카오톡 봇 API 요약](kakao_bot_api.md) |
| 서버에 올리거나 운영·장애 대응을 해야 한다 | [EC2 배포 가이드](deploy_ec2.md) |
| 대회 전 준비, 당일 배포, 심사 기간에 할 일 | [대회 체크리스트](contest_checklist.md) |
| 당일 관계자 인터뷰, 받은 데이터 검증 | [인터뷰 질문지와 데이터 검증](interview_and_data.md) |
| 대회 일정과 규칙을 확인하고 싶다 | 프로젝트 루트의 `CLAUDE.md` "대회 정보" |

## 처음 공부한다면 이 순서로
1. [아키텍처](architecture.md)의 "한눈에 보기" 흐름도
2. 흐름도에 나온 용어를 [용어집](glossary.md)에서 찾아보기(통신 방식 → 게이트웨이 프로토콜 → 봇 API 순)
3. [코드 해설](code_walkthrough.md)을 옆에 두고 `gateway.py` → `bot.py` → `kakao.py` 코드 따라가기
4. `tests/test_gateway.py`에서 재연결이 어떻게 검증되는지 보기
5. [EC2 배포 가이드](deploy_ec2.md)로 실제 배포
