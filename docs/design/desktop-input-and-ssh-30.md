# 수정 30 — 내장 창 키보드 입력과 공통 SSH 켜짐 상태

2026-09-17. 사용자 첨부 로그: `example-log/pasted-text.txt`.

## 확인한 원인

- 수정 29의 화면 표시 검사는 픽셀/크기/부모 창만 확인했다. 실제 입력 시험은 없었다.
- 실제 설치된 Codex를 빈 HOME 세 개로 실행한 재현에서 DOM의 입력칸은 focus/클릭을 받았지만 Windows GUI thread의 keyboard focus는 WPF 관리창에 남았다. 여섯 번의 입력 모두 keydown 0개, 문자열 빈 값이었다.
- `EmbeddedMouseActivation` 등록 조건은 관리창이 전경이 아닐 때만 활성화됐다. 관리창 사이드바를 클릭한 뒤에는 내부 DOM 클릭만으로 OS 키보드 대상을 바꾸지 못했다.
- 이미 시작된 Owl 창은 내부 `isVisible()` 값과 실제 native surface 상태가 달랐다. 새로운 SetParent 연결에서도 `showInactive()`를 생략하면 표시/입력 상태가 맞지 않을 수 있었다.
- SSH 가져오기는 `prepared` 원격 바인딩이 없으면 원본 ON 값을 강제로 OFF로 저장했다. 02/04는 ON인 세 서버가 모두 OFF, 03은 미리 준비했던 remote-dev만 ON이었다.
- hp/remote-linux는 새 Python이 이미 설치돼 있었지만, 준비 검사는 pyenv의 기본 Python 3.10만 보고 거절했다.

## 최종 변경

- 현재 보이는 관리용 창의 신선한 실제 클릭에서만 OS 포커스를 전달한다. 이미 그 Codex 또는 하위 컨트롤에 있으면 유지한다. 원래 클릭과 DOM 선택은 재생/변경하지 않는다.
- 전경/실제 HWND/프로세스 수명/부모/활성화/응답 상태를 검사하고, 포커스 재진입을 막는다. 주기 조회나 프로필 선택 자체에서 포커스를 가져오지 않는다.
- 입력 진단에 실제 OS 키보드 대상(내부 Codex/관리창/다른 창/없음)을 표시한다. 숨겨진 프로필의 포커스 진단은 반복 출력하지 않는다.
- 새 내장 연결마다 비활성 표시를 정확히 한 번 실행한다. 일반 앱의 프로그램식 focus/show 반복 호출 제한은 유지한다.
- 공통 SSH ON/OFF를 원본에서 그대로 가져온다. 이전 가져오기에서 강제로 저장했던 OFF도 다음 준비에서 갱신된다. 독립적으로 수정한 프로필 값의 기존 병합 규칙은 유지된다.
- 저장된 SSH 별칭의 처음 연결 시 별도의 SSH 실행기에서 누락된 프로필 바인딩을 자동 준비한다. UI 실행 경로에서는 원격 설치를 기다리지 않는다. 준비 중 실행 세대/모델 정책이 바뀌면 결과를 적용하지 않는다.
- 동일 프로필/호스트의 준비는 한 번으로 합치고, 서로 다른 호스트의 짧은 manifest 병합만 잠근다. SSH 등록 잠금의 짧은 경합을 연결 오류로 처리하지 않는다.
- 기존 Python 3.11 이상을 검색해 실제 실행 파일을 사용한다. 서버의 기본 Python이나 설치된 원본 Codex를 교체하지 않는다.

## 실제 검증

- 수정 전 재현: `artifacts/results/desktop-input-311d8073/input-report.json` — 클릭 6회, 키 입력 0회.
- 최종 실제 입력: `artifacts/results/desktop-input-330a731b/input-report.json` — 독립 Codex 프로세스 3개, 프로필 왕복 6회 모두 입력 성공. 창 크기 변경 뒤 Ctrl+A, Unicode 한글 입력, 방향키/Backspace 성공. 로그인·모델 요청 없음. 한글 IME 조합 전체를 검증한 것은 아님.
- 실제 표시/전환: `artifacts/results/desktop-render-4650f9c0/wpf-host.json` — 렌더러 준비 전 숨김, 다시 표시, 크기 변경, 부모 유지, 전경 탈취 없음.
- Native host 경계 로직 52/52, 배포 WPF 자체 검사 통과. 관련 Python 검사(자동 SSH 준비, 경합, 설정 가져오기, 원격 설치 도우미, 배포/실행) 및 JS 어댑터 검사 통과. Linux 전용 helper 시험 하나는 Windows에서 skip.
- `work/manager-30-ssh-verification.json` — 02/03/04 × remote-linux/hp/remote-dev, 총 9개 조합에서 배포된 어댑터의 native-probe/native-version 성공. 이 검사는 원격 모델 실행이나 전체 데스크톱 SSH 대화를 검증한 것은 아니다.

## 적용

- 배포: `artifacts/manager/releases/20260917-064422-709`.
- 시작 문구: `키보드 입력·SSH 자동 연결 수정 30 · IPC 27`.
- 서비스 지문: `d9ec2eddd0f56c1230063664592c3c5250db180fb4a671da3c5ed6a31dd7e99e`.
- 데스크톱 어댑터 revision 7. 네이티브 Rust 대화 런타임과 공통 저장소 구조는 수정하지 않았다.
- 사용자 보고의 이전 관리용 PID 67912/22636/106748은 실행 세대, 실행 파일, 전용 UI 경로, 최근 작업 수 0을 확인한 후 종료했다. 본앱은 종료/이동하지 않았다. 기록: `work/manager-30-old-profile-cleanup.json`.
- 세 프로필의 SSH ON/OFF가 본앱과 일치한다. ON인 세 서버에 대해 누락된 8개 바인딩을 준비했고 기존 03/remote-dev는 유지했다. 기록: `work/manager-30-ssh-preparation.json`.
- 기존 `Open-Feedback-Test.cmd`/`Open-Control-Center.cmd`가 위 배포를 실행한다. 사용자가 프로필마다 강제 재시작할 필요 없이 다음 실행에서 적용된다.
