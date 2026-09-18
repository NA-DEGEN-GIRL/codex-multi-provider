# 수정 33 — 한글 인라인 조합과 독립 입력 창

## 원인

기존 호스트는 다른 프로세스의 Codex 창을 `SetParent`로 WPF에 붙이고,
숨겨진 `Chrome_RenderWidgetHostHWND`로 `SetFocus`를 전달했다. 확정된 한글은
들어와도 Chromium TSF가 기대하는 최상위 창의 활성화가 성립하지 않아
조합 중인 글자가 입력칸 밖의 기본 IME 창에 표시될 수 있었다.

Microsoft는 서로 다른 스레드 사이의 부모/자식 **및 소유 창 관계**가 입력 큐를
자동 연결한다고 설명한다. 소유 창만 바꾸는 방식도 해결책이 되지 않는다.

- [Microsoft: Parent/child and owner/owned windows share input state](https://devblogs.microsoft.com/oldnewthing/20130607-00/?p=4143)
- [Chromium: InputMethodWinBase의 활성 창/포커스 검사](https://chromium.googlesource.com/chromium/src/+/main/ui/base/ime/win/input_method_win_base.cc)

추가로 Win32 좌표만 바꾸면 Codex의 Owl 창 구현이 사용하는 내부 좌표와
렌더링 상태가 뒤처졌다. 프로필 전환 중 창 표시와 입력 경로도 함께 수정했다.

## 변경

- Codex는 부모와 소유자가 없는 최상위 창을 유지한다. `SetParent`, 교차
  스레드 `SetFocus`, 마우스 감시를 이용한 포커스 강제 전달을 사용하지 않는다.
- 관리창의 표시 영역에 Codex의 **클라이언트 영역**을 맞춘다. 프레임 스타일을
  반복 변경하는 대신 창 영역을 잘라 동일한 위치에 표시한다.
- 화면의 픽셀 좌표와 Codex 자체 `setBounds`를 함께 갱신한다. 창이 나타날 때
  `showInactive`로 렌더링 상태를 맞추며, 키보드 포커스와 조합은 Codex가 처리한다.
- 선택된 창은 관리창이 활성인 동안 위에 표시한다. 다른 앱·관리창의 대화상자·
  최소화 상태에서는 숨기고 최상위 표시를 해제한다. 활성 창 전환 중 일시적인
  NULL 핸들은 300ms 동안 유지해 스스로 입력을 취소하지 않는다.
- WPF가 실제로 표시 영역 HWND를 숨기거나 보여준 뒤 즉시 반영한다.
  Codex의 늦은 표시 명령은 실제 HWND의 SHOW/HIDE 이벤트로 감지해 선택 상태와
  대화상자 상태에 다시 맞춘다. 1초 주기 검사까지 기다리지 않는다.
- 연결 해제 시 원래 프레임, 영역, 위치와 표시 상태를 복구한다. 관리창이
  비정상 종료된 경우에는 정확한 PID·실행 파일·창 수명 표식을 검증하는
  별도 복구 경로가 클리핑과 최상위 상태를 해제한다. Codex 작업은 종료하지 않는다.
- 창별 mutex와 살아 있는 소유자의 연결 기록으로 중복 연결/복구를 막는다.

## 검증

Windows, 144 DPI, 설치된 Codex **26.911.7940.0**의 실제 창 3개와 빈 별도 홈을
사용했다. 모델에 요청을 보내지 않았고 원본 앱의 작업·입력·로그인을 사용하지 않았다.
입력은 시험 창이 활성인지 확인한 후에만 전송했다.

- `artifacts/results/desktop-input-b0c87ef8/input-report.json`: **최종 배포 DLL로 전체 통과**.
  `20260917-110131-450` 릴리스의 Shell/Shared DLL과 시험 대상 해시 일치 확인.
- `artifacts/results/desktop-input-b0c87ef8/2/inline-preedit.png`: 최종 배포판의 조합 중 화면.
- `artifacts/results/desktop-input-d88d7393/input-report.json`: 전체 통과.
- `artifacts/results/desktop-input-59741666/input-report.json`: 추가 조합 편집 시험 포함 전체 통과.
- `artifacts/results/desktop-input-59741666/2/inline-preedit.png`: 실제 조합 중인 `한`의 렌더링 캡처.
- input, textarea, contenteditable를 대상으로 프로필을 `0 → 1 → 2 → 0 → 2 → 1`로 전환.
- 오른쪽 Alt, 확정 전 `한` 및 새 `compositionupdate`, 받침 Backspace/재입력,
  `한글 abc` 입력, 조합 중 크기 변경을 검증했다.
- 창 이동·크기 변경, 관리창 비활성/대화상자, 최소화·복원 후 입력을 검증했다.
- 네이티브 자체 검사에서 잘못된 프로세스 거절, 원래 창 복구, 닫힌 HWND 해제,
  살아 있는 관리창의 복구 거절, 고아 창 복구, 모달 중 늦은 표시 명령 정리를 검증했다.

시작 문구: `한글 인라인 조합·독립 입력 창 수정 33 · IPC 27`.
기존 실행 파일 경로는 그대로 사용한다. 이미 실행 중인 관리창은 정상 종료한 뒤
다시 열어 새 호스트와 전용 데스크톱 어댑터를 함께 적용한다.

최종 반복 검사에서는 모든 편집기의 두 번의 클릭과 `abcabc`, 6회의 새로운 한글
조합 이벤트, 조합 중 수정 및 창 크기 변경, `한글 abc`, 창 수명 관련 검사가 모두
통과했다. 테스트 입력은 실제 마우스처럼 down/up 사이를 두었고, 스냅샷 파일의
원자적 교체와 충돌하지 않도록 읽기 핸들에 삭제 공유를 허용했다.

검증 범위는 위 Windows/144 DPI 환경이다. 실제 계정의 메시지를 전송하거나
진행 중인 작업을 종료해서 시험하지 않았다.
