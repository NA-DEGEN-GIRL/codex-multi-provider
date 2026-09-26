# 수정 96 — Codex Chrome 확장 연결(native host)과 번들 플러그인 등록

## 확인한 실패

- Chrome 확장 측면 패널: `Codex Chrome native host v2 manifest is missing`.
  관리 프로필의 Codex도 computer use와 브라우저 제어를 사용할 수 없다고 답했다.
- Chrome은 `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.openai.codexextension`이
  가리키는 v1 manifest의 `extension-host.exe`를 실행한다. 그 host는
  `%LOCALAPPDATA%\OpenAI\Codex\chrome-native-hosts-v2.json`만 읽고 CLI와 `CODEX_HOME`을 고른다.
  두 파일은 데스크톱이 번들 `chrome` 플러그인을 설치한 뒤 `g2`(26.917 이름)에서 함께 쓴다.
- 관리 프로필에서는 그 단계에 도달하지 못했다. 데스크톱은 번들 marketplace를
  `<CODEX_HOME>\.tmp\bundled-marketplaces\openai-bundled`에 만든 뒤 app-server에
  `marketplace/add`로 등록한다. 관리 루트 경로에 `#`이 있으면 런타임
  (`core-plugins/src/marketplace_add/source.rs`의 `split_source_ref`)이 마지막 `#` 뒤를
  git ref로 읽고 `--ref is only supported for git marketplace sources`로 거절한다.
  모든 프로필에서 `openai-bundled` 등록이 실패해 chrome·computer-use 플러그인이 설치되지 않았고,
  v2 파일은 어디에도 생기지 않았다. 실패는 경고 로그(`bundled_plugins_marketplace_add_failed`)에만 남았다.
- 남아 있던 v1 manifest는 과거에 플러그인의 `installManifest.mjs`가 이전 관리 복사본의
  host를 가리키도록 쓴 것이다. 데스크톱은 이 값을 다시 쓰지 못하고 있었다.

## 변경

관리용 데스크톱 복사본에만 세 가지 byte patch를 추가한다(`desktop_chrome_host.py`,
`desktop_bundle.patch_archive`에서 다른 patch 뒤에 적용).

1. **번들 marketplace 등록 재시도** — 첫 요청은 그대로 보낸다. 위 ref 오류이고 경로에 `#`이
   있을 때만 끝에 `#`을 붙여(빈 ref) 한 번 더 보낸다. 앱의 다른 marketplace 추가 경로가 이미
   쓰는 복구 방식과 같다. 경로를 그대로 받는 런타임에서는 재시도가 일어나지 않는다.
   이로써 chrome·computer-use 등 번들 플러그인이 각 프로필에 정상 설치된다.
2. **한 프로필만 Chrome에 등록** — `g2`는 `CODEX_MANAGER_CHROME_NATIVE_HOST=1`인 데스크톱에서만
   실행된다. 관리자는 목록 순서에서 첫 번째 "자체 ChatGPT 로그인" 프로필
   (`auth_mode=native`, 제거·로그인 대기·계정 누락·읽기 전용 제외)에만 이 값을 준다.
   API 키·빌린 로그인 프로필의 `CODEX_HOME`에는 확장용 app-server가 쓸 인증이 없다.
3. **등록 시 이전 관리 항목 정리** — 등록하는 데스크톱은 v2 파일(전역·`CODEX_HOME` 사본 모두)에서
   `<관리 루트>\artifacts\managed-desktop\` 아래 resources를 가리키는 다른 항목을 지운다.
   이전 관리 복사본과 이전 소유 프로필의 항목이 쌓이지 않는다. 공식 앱 등 다른 설치의 항목은 유지한다.

v1 manifest와 레지스트리 값, 확장용 app-server 실행 파일 복사는 앱의 기존 동작을 그대로 쓴다.
관리자는 레지스트리나 `%LOCALAPPDATA%\OpenAI`를 직접 쓰지 않는다.

## 동작과 제한

- 소유 프로필은 실행 시점에 정해진다. 목록 순서를 바꾸면 새 소유 프로필이 다음에 시작할 때
  등록하고, 이전 항목은 그 등록에서 지워진다. 이전 소유 프로필이 계속 실행 중이면 그 프로필의
  플러그인 캐시가 바뀔 때 다시 등록할 수 있으므로, 전환 후에는 두 프로필을 한 번 다시 시작한다.
- 측면 패널 대화는 데스크톱과 같은 방식으로 앱에 포함된 CLI 사본
  (`<CODEX_HOME>\plugins\.plugin-appserver\codex.exe`)으로 실행된다. 관리 실행기(인증 연결,
  공통 기록 경로)를 거치지 않으므로 그 대화는 해당 프로필 `CODEX_HOME`에만 저장된다.
- 번들 CLI 사본(`codex.exe`와 보조 실행 파일)은 Chrome 플러그인을 가진 프로필마다 약 0.4GB이며
  소유 여부와 관계없이 첫 동기화 때 한 번 복사된다(앱의 기존 설치 단계).
- 완전 종료 후에도 v2 항목은 남는다. 스키마상 pid 확인이 없고 공식 앱도 종료 후 항목을 유지하므로
  종료 시 정리는 하지 않았다. 관리 루트 밖으로 옮기거나 프로필을 지우면 다음 등록에서 정리된다.
- 새 데스크톱 버전: 표시 문자열(`bundled_plugins_marketplace_add_failed`,
  `chrome-native-hosts-v2.json`)이 있는데 patch 위치를 찾지 못하면 관리 복사본 준비를 중단한다.
  버전 올림 점검 목록에 `_ADD_VARIANTS`, `_HOST_VARIANTS`를 포함한다.
  `_HOST_VARIANTS`의 마지막 이름(정리 코드가 호출하는 `node:path` 바인딩)은 anchor에 들어 있지
  않으므로, 그 모듈에 `let <이름>=require("node:path");`가 정확히 한 번 있어야 patch를 적용한다.
- 런타임 자체 수정(로컬 경로이면 `#` 분리 생략)은 이 변경과 충돌하지 않지만 이번에는 넣지 않았다.

## 검사

- `tests/test_manager_chrome_native_host.py`: patch 표(26.917 원문), 모호·부분 일치 시 중단,
  이미 적용된 archive, 다른 patch 뒤 적용과 integrity 재계산, Node로 실제 교체 코드 실행
  (재시도 조건 6종, 등록 게이트, 관리 항목 정리 규칙), 소유 프로필 선택과 실행 환경 표시.
- 설치된 26.917 `app.asar`를 임시 폴더로 patch: 세 patch 모두 한 번씩 적용, 바뀐 모듈 `node --check` 통과.
- 실제 Chrome 연결은 새 관리 빌드로 프로필을 다시 연 뒤 확인해야 한다.
