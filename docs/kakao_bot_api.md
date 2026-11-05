# 카카오톡 봇 API 요약

출처: 카카오 디벨로퍼스 문서 (2026-10-26 확인, 로그인 필요)
- https://developers.kakao.com/docs/ko/bot/common (이해하기)
- https://developers.kakao.com/docs/ko/bot/webhook (웹훅)
- https://developers.kakao.com/docs/ko/bot/gateway (게이트웨이)
- https://developers.kakao.com/docs/ko/bot/rest-api (REST API)

## 전체 흐름
1. 사용자가 봇에게 메시지를 보낸다.
2. 카카오톡 서버가 봇으로 `DISPATCH` 이벤트를 보낸다. 페이로드에 `callbackToken`이 들어 있다.
3. 봇은 답장을 SkillResponse(오픈빌더 스킬 응답 v2.0 형식)로 만든다.
4. 봇이 **메시지 콜백 API**를 호출해 답장을 보낸다. 이때 봇 인증 토큰과 콜백 토큰이 필요하다.

웹훅 응답 본문으로는 답장할 수 없다. 답장은 항상 콜백 API로 보낸다.

## 토큰
- **봇 인증 토큰**(`BOT_TOKEN`): 앱 관리 페이지 [제품 설정] > [카카오톡 봇] > [봇]에서 확인한다. REST API를 호출하거나 게이트웨이에 연결할 때 `Authorization: KakaoAK ${BOT_TOKEN}`으로 쓴다.
- **콜백 토큰**(`d.callbackToken`): `MESSAGE_CREATE`, `ENTRANCE`, `CHAT_JOIN`, `AD_REWARD` 이벤트에 들어 있다. 일회용이고, **받은 뒤 5분이 지나면 만료**된다.

## 이벤트 수신 방식 (둘 중 하나만 사용)
| | 웹훅(HTTP) | 게이트웨이(웹소켓) |
|---|---|---|
| 연결 | 카카오 서버가 봇 URL로 POST 요청 | 봇이 `wss://bot-gateway.kakao.com/gateway`에 접속 |
| 권장 환경 | 서버리스(FaaS) 등 공개 URL이 있는 환경 | 상시 실행되는 워커나 데몬 |
| 연결 유지 | 필요 없음 | 봇이 HEARTBEAT를 보내고 재연결을 직접 처리 |

웹훅 URL을 등록해 두면 게이트웨이 방식은 쓸 수 없다.

## 사전 설정 (출처: https://developers.kakao.com/docs/ko/bot/prerequisite)
- 봇 생성 조건: 카카오비즈니스 통합 계정이면서 카카오톡 계정과 연결된 디벨로퍼스 계정, 앱 오너, 앱당 봇 1개
- 봇 이름(10자 이내)과 검색용 ID(2~15자)는 **생성 후 수정할 수 없다.**
- **호출 허용 IP 등록.** 문서에는 "필수"로 적혀 있다. 봇 API(콜백 등)를 호출할 서버 IP를 [어드민 키] > 호출 허용 IP에 등록한다. 등록하면 목록에 있는 IP만 허용되므로, EC2라면 탄력적 IP를 붙여 고정한다. 실제로는 비어 있어도 호출이 됐다(아래 "실측 결과").
- 웹훅 URL 제약: **HTTPS만 지원**하고, 카카오톡 게이트웨이가 접근할 수 있는 공개 URL만 등록할 수 있다. 커스텀 헤더는 최대 3개까지 등록할 수 있다.

## 웹훅
- `POST` 요청이 [카카오톡 봇] > [봇] > [웹훅]에 등록한 URL로 온다.
- **1초 안에 2xx로 응답**해야 한다. 이벤트 처리를 시작하기 전에 응답한다. 본문은 넣지 않으며, 문서 예시는 `204 No Content`다.
- 콜백 API 호출은 웹훅에 응답한 뒤 비동기로 처리한다.
- 헤더에는 웹훅 설정에서 등록한 **커스텀 헤더**가 함께 온다. 요청이 카카오 서버에서 왔는지 검증하는 데 쓴다.
- 본문 형식: `{"op": 0, "t": "<이벤트 종류>", "d": {...}}`

## 이벤트 종류 (`t`)
| t | 언제 | d의 주요 필드 |
|---|---|---|
| `MESSAGE_CREATE` | 사용자가 메시지를 보냄 | `id`, `content`, `timestamp`, `isChannelChatroom`, `botGroupKey`(채팅방), `botUserKey`(발화자), `callbackToken` |
| `ENTRANCE` | 봇이 채팅방에 들어감 | `botGroupKey`, `inviter.botUserKey`, `params`, `callbackToken` |
| `CHAT_JOIN` | 사용자가 채팅방에 들어옴 | `botGroupKey`, `user.botUserKey`, `callbackToken` |
| `LEAVE` | 봇이 채팅방에서 나감 | `botGroupKey` (콜백 토큰 없음, 수신 확인만 하면 됨) |
| `AD_REWARD` | 사용자가 광고 리워드를 받음 | `botGroupKey`, `viewer.botUserKey`, `params`, `callbackToken` |

`callbackToken`은 필수 필드가 아니다. 없으면 답장할 수 없으므로 건너뛴다.

## 메시지 콜백 API
```
POST https://kapi.kakao.com/v1/bot/callback
Authorization: KakaoAK ${BOT_TOKEN}
X-Bot-Callback-Token: ${callbackToken}
Content-Type: application/json

{"skillResponse": {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": "..."}}]}}}
```
- 성공하면 `200`과 함께 `{"totalCount": 1, "sendCount": 1, "errorCount": 0}`이 돌아온다.
- 실패 예시: `400`과 함께 `{"code": -870, "msg": "TalkBotCommonException", "extras": {"code": 10001, "message": "..."}}`

## 메시지 전송 API (봇이 먼저 보내는 메시지)
- `POST https://kapi.kakao.com/v1/bot/send_message`, 인증 토큰만 필요하다.
- 본문에 `botUserKey`(채널채팅) 또는 `botGroupKey`(일반·오픈·팀채팅) 중 하나와 `skillResponse`를 넣는다.
- 광고 메시지는 금지다.

## SkillResponse
- `version`: `"2.0"`만 지원
- `template.outputs`: 말풍선 1~3개
- `template.quickReplies`: 최대 10개. 문서는 채널채팅에서 쓸 수 있다고 하지만 **실제로는 보이지 않았다**("실측 결과"). 추천 질문은 카드의 `message` 버튼으로 보낸다.
- `extra.mentions`: 사용자 멘션용. SimpleText에서만 되고 채널채팅은 지원하지 않는다.
- 말풍선 종류: `simpleText`(최대 1,000자, 500자를 넘으면 접히고 "전체 보기"로 표시), `simpleImage`, `textCard`, `basicCard`, `listCard`, `itemCard`, `commerceCard`, `carousel`(채널채팅만)
- Button `label`은 최대 14자다. 가로로 2개를 놓으면 8자까지 쓸 수 있다.
- QuickReply 필드(REST API 문서 원문 그대로): `label`(String, 필수, 사용자에게 노출되는 바로가기 응답 표시), `action`(Enum, 필수, `message`만 있음: 선택한 발화가 그대로 전달), `messageText`(String, 선택, 사용자에게 노출되는 발화), `extra`(JSON, 선택). 
- 퀵리플라이에 대해 문서에 있는 것은 이것이 전부다(10/28 확인: 이해하기, 튜토리얼, REST API, 게이트웨이, 웹훅 페이지):
  - 이해하기: "채널채팅에서 봇이 응답할 때 다음 액션이나 발화를 제안", "채널채팅은 (명령어 대신) 퀵리플라이를 이용". 그림 "퀵리플라이 (채널채팅)"에는 마지막 말풍선 바로 아래 둥근 버튼 한 줄이 있다.
  - 튜토리얼 "다음 단계"의 "추천 발화 만들기"는 이해하기 페이지로만 연결된다.
  - REST API: 위 SkillTemplate·QuickReply 필드 표. 요청 예제 탭(SimpleText~Carousel, 콜백·메시지 전송 모두)에 quickReplies를 넣은 예제는 없다.
  - 게이트웨이, 웹훅 페이지에는 퀵리플라이 언급이 없다.
  - 문서에 없는 것: `label` 글자 수 제한, 보이기 위한 조건(채팅방 설정, 앱 버전, 함께 보내는 말풍선 종류 등), quickReplies를 넣은 완성 예제. 이런 내용은 모른다.
- Button `action`: `message`(`messageText` 필수), `webLink`(`webLinkUrl` 필수), `phone`, `share`, `operator` 등. 카드당 버튼은 세로 3개, 가로 2개까지.
- 주요 필드 제한:
  - `simpleImage`: `imageUrl`(카카오가 가져갈 수 있는 공개 주소), `altText` 50자
  - `textCard`: `title` 50자, `description` 제목 포함 400자(캐러셀 안에서는 128자), `buttons` 필수
  - `basicCard`: `title` 50자, `description` 230자, `thumbnail`과 `buttons` 필수
  - `listCard`: `header`(ListItem), `items` 5개(캐러셀 안에서는 4개), `buttons` 필수
  - `carousel`: `type`(basicCard, textCard 등), `items` 최대 10개(listCard는 5개). 채널채팅만 지원
- `listCard`의 ListItem: `title`(필수), `description`, `imageUrl`, `link`, `action`(`message`), `messageText`, `extra`. 항목별 글자 수 제한은 문서에 없다.
- 코드: `kakao.py`의 `simple_text`, `simple_image`(실측 안 함), `text_card`, `suggestion_card`, `list_card`, `list_item`, `message_button`, `web_link_button`, `skill_response`. 다른 카드는 위 필드대로 dict를 직접 만들어 `skill_response`에 넣는다.

## 쿼터
| API | 쿼터 |
|---|---|
| 메시지 전송 | 초당 200건, 하루 1,000건 |
| 메시지 콜백 | 초당 200건, 하루 18,000,000건 |
| 명령어·도움말 조회/수정 | 초당 20건 |

## 기타
- 지원하는 채팅방: 채널채팅, 일반채팅, 오픈채팅, 팀채팅. 나와의채팅과 비밀채팅은 지원하지 않는다.
- 명령어는 최대 30개까지 등록할 수 있다(`POST /v1/bot/commands/update`). 도움말 API도 있다.
- 예제 코드: 문서의 웹훅과 게이트웨이 에코봇이 모두 Python(FastAPI/httpx, websockets)으로 되어 있다.

## 실측 결과 (2026-10-27 로컬 테스트, 문서와 다른 점 포함)
문서에 없거나 문서와 다르게 동작한 내용이다. 봇 설정이나 카카오 쪽 변경에 따라 달라질 수 있다.

- **사진**: 별도 이벤트가 아니라 `MESSAGE_CREATE`의 `content`에 이미지 주소가 문자열로 온다. 예: `https://talk.kakaocdn.net/dna/.../i_xxx.jpg?credential=...&expires=...&signature=...`. 주소에 만료 시각(`expires`)과 서명이 붙어 있으므로 받은 즉시 내려받는다. 텍스트와 구분하려면 `content`가 `https://talk.kakaocdn.net/`으로 시작하는지 본다.
- **이모티콘**: 채널 채팅방에서는 이모티콘을 보낼 수 없었다.
- **같은 봇의 중복 접속**: 문서는 나중 연결이 먼저 연결을 끊는다(4004)고 하지만, 실제로는 **두 연결이 모두 유지되고 같은 이벤트를 둘 다 받았다.** 먼저 콜백을 보낸 쪽만 성공하고, 늦은 쪽은 `400 {"msg":"이미 사용된 콜백 토큰입니다.","code":-881}`을 받았다. 사용자에게는 답장이 하나만 간다.
- **네트워크 끊김 뒤 RESUME**: 약 40초 끊겼다가 다시 연결해 RESUME을 보내자 `4009 Session unavailable`로 거절됐다. **끊긴 동안 보낸 메시지는 새 세션에서 다시 오지 않았다(유실).** RESUME으로 이어받을 수 있는 조건(끊긴 시간, 끊긴 방식)은 아직 확인하지 못했다.
- **호출 허용 IP를 비워 둔 경우**: 문서는 등록이 "필수"라고 하지만, 목록에 아무 IP도 등록하지 않은 상태에서 로컬 PC의 콜백이 모두 성공했다(200, `sendCount` 정상). 비어 있으면 IP 검사를 하지 않는 것으로 보인다. 하나라도 등록한 뒤의 동작(목록 밖 IP 거부)은 앱 설정 문서 기준이며 아직 실측하지 않았다.
- **말풍선 개수**: 문서대로 `outputs`는 3개까지만 전달된다. 4개, 5개, 10개를 보내도 오류 없이 `200`이 오고, 응답은 `{"totalCount": 3, "sendCount": 3, "errorCount": 0}`이었다. 휴대폰에는 앞의 3개(1/N, 2/N, 3/N)만 보였다. **3개를 넘는 말풍선은 오류 없이 버려지므로** 로그만 봐서는 알 수 없다. 1개, 2개는 그대로 전달됐다(`totalCount`가 보낸 개수와 같음).
- **퀵리플라이, 카드, 버튼 (10/28)**:
  - `quickReplies`를 넣은 콜백은 `200`, `{"totalCount": 1, "sendCount": 1, "errorCount": 0}`로 정상 응답했지만 **휴대폰 채널 채팅방에는 글자 말풍선만 보이고 퀵리플라이는 보이지 않았다.** 카드와 함께 보내도, 글자 말풍선 하나와 보내도, `extra: {}`를 넣어도 같았다. 오류가 없으므로 로그만 봐서는 알 수 없다.
  - 재실측(10/29)도 같았다. 시험한 계정은 채널을 추가한(채널 친구) 상태였다. 아래 4가지 모두 `200`, `sendCount: 1`이었지만 퀵리플라이 버튼은 하나도 보이지 않았다.
    - 인터넷의 오픈빌더 예제 JSON 그대로(글자 말풍선 + 퀵리플라이 4개)
    - KakaoTalk-Bot-Wrapper 라이브러리 방식(`messageText` = `label`, 1개)
    - `textCard` + 퀵리플라이 2개
    - 콜백 대신 메시지 전송 API(`/v1/bot/send_message`, `botUserKey`)로 보낸 경우
  - 인터넷 예제와 라이브러리는 형식만 보여 줄 뿐, 카카오톡 봇 API에서 실제로 보였다는 근거는 없었다(예제는 2019년 오픈빌더 스킬 서버용).
  - `textCard`와 버튼(`message`, `webLink`)은 정상으로 보였다. `message` 버튼을 누르면 버튼의 `messageText`가 일반 `MESSAGE_CREATE`로 들어왔다. 그래서 추천 질문은 카드 버튼으로 보낸다(`kakao.suggestion_card`).
  - `kakao.suggestion_card`(세로 버튼 3개)를 글자 말풍선과 함께 보내면 둘 다 보였다. 14자를 넘는 질문은 버튼 글자가 "…"로 잘려 보였지만, 누르면 질문 전체(29자)가 메시지로 들어왔다.
  - 에이전트가 `quickReplies`를 섞어 돌려주면 `limit_response`가 빼고 경고를 남긴 뒤 나머지 말풍선만 정상으로 보냈다.
  - `thumbnail`이 빠진 `basicCard`(문서상 필수)도 거절 없이 `200`, `sendCount: 1`로 전달되고 카드로 보였다. 카카오가 실제로 거절하는 경우와 거절된 콜백 토큰을 다시 쓸 수 있는지는 아직 확인하지 못했다(`bot.py`의 글자 답장 재전송은 대비책으로 둔다).
- **입장 이벤트 (10/28)**: 문서상 `CHAT_JOIN`(사용자가 채팅방에 입장했을 때)과 `ENTRANCE`(봇이 채팅방에 입장했을 때)는 콜백 토큰을 담아 온다. 그런데 **이미 이 채널과 대화한 계정으로** 채널 채팅방을 나갔다 다시 들어가도, 채팅방을 삭제하고 채널 프로필에서 새로 시작해도 **게이트웨이로 아무 이벤트도 오지 않았다**. 확인 방법: 실측용 봇이 `bot.handle_event` 앞에서 모든 DISPATCH 이벤트의 종류를 로그에 남겼는데, 그동안 기록된 것은 `MESSAGE_CREATE`뿐이었다(`gateway.py`는 op 0 프레임을 종류와 상관없이 모두 넘기고, IDENTIFY에는 이벤트를 고르는 항목이 없다). 이어서 같은 계정으로 **채널 친구 추가, 채팅방 다시 들어오기, 채널 차단 후 해제**를 했을 때도 실제 `bot.py`(답장하지 않는 이벤트도 기록하는 버전) 로그에 `join`·`event ignored` 줄이 한 건도 없었다. **처음 들어오는 계정**에게 `CHAT_JOIN`이 오는지는 확인하지 않았다. 지금은 `bot.py`가 답장하지 않는 이벤트도 `event ignored type=...`로 남기므로 운영 로그로 확인할 수 있다. 실측에서 오지 않은 이벤트에 답하는 코드는 두지 않고, 첫 인사는 사용자의 첫 메시지로 판단한다(`agent.with_intro`). 휴대폰에서 첫 메시지에는 소개 글, 답변, 추천 질문 카드 3개가 순서대로 오고, 두 번째 메시지와 추천 질문 버튼을 누른 메시지에는 답변만 오는 것을 확인했다.
- **listCard (10/28)**: `kakao.list_card`로 항목 6개를 보내면 우리 코드가 5개로 자르고, 휴대폰에 5개가 보였다. 설명이 붙은 항목과 긴 설명도 정상으로 보였다. `action: "message"`인 항목을 누르면 그 `messageText`가, 아래 버튼을 누르면 버튼의 `messageText`가 일반 메시지로 들어왔다.
- **채널채팅 MESSAGE_CREATE의 `d` 항목 (10/28)**: `botGroupKey`, `botUserKey`, `callbackToken`, `content`, `id`, `isChannelChatroom`, `timestamp`, `type`. 채널채팅에서도 `botGroupKey`가 들어 있다.
- 콜백 토큰이 담긴 원본 이벤트에는 `d.type`(`MESSAGE_CREATE`)과 `isChannelChatroom`(채널 채팅이면 `true`)도 들어 있다.
