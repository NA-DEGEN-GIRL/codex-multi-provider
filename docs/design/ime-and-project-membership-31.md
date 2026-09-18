# 수정 31 — 오른쪽 Alt 한영 전환과 본앱의 프로젝트 이동

2026-09-17. 대상 작업: `예제 오디오 작업`,
`00000000-0000-7000-8000-000000000001`.

## 한영 입력 원인과 변경

수정 30은 키보드 포커스를 `Chrome_WidgetWin_1` 외곽 창에 전달했다.
영문 입력과 DOM caret은 정상이어도 프로필 전환 후 한글 IME는 작동하지 않았다.
실제 설치된 Codex 세 프로세스에서 오른쪽 Alt 뒤 `gksrmf`가 그대로 영문으로 입력되는 현상을 재현했다.

Chromium의 IME 입력 대상인 `Chrome_RenderWidgetHostHWND`를 같은 프로세스의
하위 창에서 찾아 포커스를 전달한다. 이 창은 화면을 D3D로 합성하면서 숨김 상태로
유지될 수 있으므로 입력 HWND 자체의 IsWindowVisible은 요구하지 않는다.
부모 창의 실제 표시 상태, 프로세스 식별, 활성화/응답 상태는 계속 검사한다.
후보가 여러 개면 임의 선택하지 않고 외곽 창으로 돌아간다.
기존 하위 컨트롤 포커스와 DOM 선택은 유지한다. 주기 조회에서 포커스를 바꾸지 않는다.

## 프로젝트 이동 원인과 변경

본앱의 상태에는 이 작업을 example-audio로 옮긴 명시적 배치가 있었지만,
native SQLite의 `threads.project_id`는 NULL이고 해당 작업의 배치 이전은 pending이었다.
관리용 설정에는 이전 카탈로그의 가상 작업 ID로만 배치가 복사됐고,
`threadAssignmentsMigrated`는 실제 완료 여부와 관계없이 true로 기록됐다.
그 결과 실제 작업 ID의 배치를 찾지 못해 이전 cwd인 example-assets로 표시됐다.

- 공통 저장소 모드에서는 실제 작업 ID로 로컬 배치와 프로젝트 밖 배치를 가져온다.
- 본앱의 pendingThreadAssignmentIds와 배치 이전 완료 상태를 보존한다.
- 명시적으로 이전 중인 작업의 본앱 배치는 오래된 native 배치보다 우선한다.
  이전이 완료된 작업은 계속 native 저장소의 현재 배치를 따른다.
- `thread/project/updated` 알림도 다른 창의 목록 갱신 신호로 전달한다.
  알림에는 작업 ID와 변경 순서만 전달하고 배치 값을 재생하지 않는다.

원본 대화를 이동시키거나 JSONL 기록을 다시 쓰지 않았다.

## 검증

- `artifacts/results/desktop-input-79d5c1f3/input-report.json`: 설치된 Codex를 빈 HOME 세 개로
  실행한 실제 WPF 내장 입력 시험, 15개 확인 통과. 프로필 왕복 6회 영문 입력,
  크기 변경, 선택/편집, 세 창 각각 오른쪽 Alt 스캔코드로 한글 전환 후 `한글 ` 입력,
  다시 오른쪽 Alt를 눌러 `한글 abc` 입력을 확인했다. 로그인과 모델 호출 없음.
  DOM composition 이벤트 배열은 비어 있으므로 조합 이벤트 전달까지 통과했다고 해석하지 않는다.
- `artifacts/results/project-membership-e249b1e8/report.json`: 실제 Rust 런타임 네 개가 같은
  임시 저장소를 사용해 프로젝트 왕복 이동과 프로젝트 밖 이동을 조회했다.
  본문 유지와 세 프로필의 pending 배치를 포함한 16개 확인 통과. 모델 응답은 로컬 fixture.
- NativeHost 경계 검사 53/53, 관련 Python/JS 검사, 배포 WPF 자체 검사 통과.
- 최종 시험 도구에서 실험용 포커스 보정 분기를 제거한 뒤 재빌드 성공.
  위 입력 시험은 해당 분기를 사용하지 않고 production NativeWindowHost만 사용했다.

실제 사용자의 로그인된 세 창에서 표시를 시각 확인한 결과는 아니다.
원본 동기화 앱의 새 프로젝트 알림 어댑터는 본앱을 다음에 정상 실행할 때 적용된다.
현재 배치 가져오기와 관리창의 IME 수정에는 본앱 종료가 필요하지 않다.

## 배포 및 적용

- 배포: `artifacts/manager/releases/20260917-074135-293`.
- 시작 문구: `한영 전환·프로젝트 이동 수정 31 · IPC 27`.
- 서비스 지문: `b9439b1dc6f6050ef9fe6e8c71ebe703ade91c1754aa861a32d00af32bd2b525`.
- 데스크톱 어댑터 revision 8. Rust 대화 런타임과 저장소 구조는 변경하지 않았다.
- 02/04의 이전 관리용 PID 75564/156788은 실행 세대, 전용 경로, 신선한 완전한 관찰 기록의
  활성 작업 수 0을 확인하고 종료했다. 03은 이미 종료돼 있었다. 본앱은 종료/이동하지 않았다.
- 세 프로필 모두 해당 실제 작업 ID가 example-audio에 배치되도록 본앱 설정 가져오기 완료.
  기록: `work/manager-31-profile-preparation.json`. 이전 관리용 설정은
  `work/manager-31-preferences-backup/`에 보관했다.
- 기존 `Open-Feedback-Test.cmd`와 `Open-Control-Center.cmd`가 새 배포를 실행한다.
  열려 있는 관리창만 닫고 다시 실행하면 된다. 프로필마다 강제 재시작할 필요는 없다.
