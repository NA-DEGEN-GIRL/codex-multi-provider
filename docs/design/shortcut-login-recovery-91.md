# 수정 91 — 현재 작업 바로가기와 만료 프로필 로그인

## 확인한 실패

- `지금 열린 작업 추가`는 `NativeConversationCapture`에서 Codex HWND의 root가
  현재 foreground인지 검사했다. 독립 top-level viewport 방식에서는 메뉴 클릭 시
  관리창이 foreground가 되므로 정상 선택도 거절했다. `연결 확인`도 같은 코드를 사용했다.
- 프로필 창의 X만으로 Electron의 프로필 실행이 끝나지 않을 수 있다.
  `NativeLogin.prepare()`는 일반 실행의 계정 변경을 차단하기 때문에 창을 닫고
  `로그인 · API 키 관리`를 눌러도 기존 실행을 닫으라는 오류가 반복됐다.
  로그에서 반복 요청 실패와 동일 프로필의 남은 실행을 확인했다.

## 변경

`NativeConversationCapture`는 명시적인 사용자 동작에서만 다음을 허용한다.

1. 관리창 HWND가 현재 프로세스의 보이는 활성화 가능한 root인지 확인한다.
2. 캡처 대상의 HWND/PID/실행 경로와 현재 연결·프로필 선택을 검증한다.
3. 클립보드 snapshot을 읽은 뒤에도 같은 창이 foreground이고 선택이 같을 때만
   해당 viewport로 한 번 활성화 요청을 보낸다.
4. 실제 foreground와 키보드 포커스 확인 후 링크 복사 단축키를 한 번 보낸다.

다른 앱으로 이동, 프로필 변경, 사라진 창, 활성화 거절은 입력 전 취소한다.
캡처 중 선택 변경도 취소하며, 외부 클립보드 변경을 덮어쓰지 않는 기존 규칙을 유지한다.
종료 뒤 이전 창을 강제로 재활성화하지 않는다. 창 제목이나 최근 대화로 작업을 추측하지 않는다.

로그인 메뉴는 `profile.login_status`로 현재 저장된 로그인 상태를 확인한다.
일반 관리 실행이 남아 있고 유효한 로그인이 없다면, **해당 프로필의 실행 종료와
미전송 입력 손실·SSH 연결 중단 가능성을 안내하고 확인을 받는다.**
확인 후 기존 `profile.recover`에 선택한 profile ID와 generation을 전달한다.
종료가 확인된 경우에만 `profile.login`으로 전용 로그인 화면을 연다.
취소하거나 종료 확인이 실패하면 로그인 실행을 추가하지 않는다.
이미 유효한 로그인이나 전용 로그인 진행 상태는 다시 종료하지 않는다.

서버의 계정 고정 검사, 인증 파일, 대화 기록, SSH 프로필을 직접 수정하지 않는다.
새 로그인 성공 여부와 등록 계정 일치는 기존 로그인 흐름에서 확인한다.
재로그인 버튼은 실행 중에도 사용할 수 있으며 필요한 종료 확인은 위 흐름에서 처리한다.

## 검사와 적용 범위

- Release 빌드: 경고·오류 없음.
- 실제 WPF `LoginProfileAsync`를 격리 RPC fixture로 실행:
  취소, 동의 후 정확한 generation 종료 → 로그인 순서, 다른 프로필 보호,
  종료 실패 시 중단, 유효한 로그인 유지 검사 통과.
- 캡처 활성화 정책 fixture:
  관리창 → 선택 viewport 한 번 전환, 이미 활성 상태, Alt-Tab,
  선택 변경, 잘못된 관리창, 검증 중 foreground 변경, Windows 활성화 거절 검사 통과.
- 기존 Windows 창·메뉴 fixture 포함 13개 검사 그룹 통과.
- fixture는 실제 사용자 창의 포커스·클립보드를 조작하거나 키를 보내지 않는다.
  실제 Electron의 링크 복사 및 사용자의 OAuth 로그인 완료는 업데이트 후 확인해야 한다.

관련 파일: `MainWindow.cs`, `NativeConversationCapture.cs`,
`ConversationCaptureActivation.cs`, `ProfileLoginPresentation.cs`, `ShortcutLoginSelfTest.cs`.
변경은 관리창 코드에 있으므로 관리창을 다시 열어 수정 91을 적용한다.
관리창 X는 실행 중인 다른 프로필을 유지한다. 재로그인 시 선택 프로필 종료는 별도 확인한다.
