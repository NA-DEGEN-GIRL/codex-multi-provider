# 수정 110 — 설정 적용 예약이 완전 종료를 막는 문제

## 확인한 원인

수정 109 관리창에서 완전 종료를 실행한 로그를 확인했다. 로컬 Codex 프로세스들은
정상 종료됐지만, 일부 프로필에 남은 **로컬 자동 재시작 예약** 때문에
`profile.remote_stop`이 `profile_prepare_busy`를 반환했다. 이 예약은 SSH 전용 예약과
달리 `remote_background`가 없고, `phase=waiting`, 유지보수 transaction은 없는 상태였다.
새 관리창을 열 때 실행 파일 변경을 감지한 `StartupUpdates`가 만든 예약이었다.

사용자가 원격 실행을 남기는 선택을 해도 `WorkspaceShutdown.CanDrainLegacy`는 이
로컬 예약을 진행 중인 작업으로 판단해 관리 서비스 종료를 막았다. 수정 109는 SSH 전용
예약만 처리했고, 실제로 실행 중인 이전 Python 백엔드의 로컬 예약을 놓쳤다.
관리창 교체와 관리 서비스 교체는 별개이므로, 관리창 버전만으로 적용 여부를 판단하면 안 된다.

## 변경

- `ProfileRestarts.pause_local`과 `shutdown_status`: 완전 종료의 시작 단계에서 새 로컬
  재시작을 받지 않고, 이미 들어간 단계가 끝날 때까지 실제 실행/worker 수를 보고한다.
  아직 transaction을 얻지 않은 대기 예약은 종료 사유를 기록하고 끝낸다.
  저장된 모델·하위 에이전트 설정은 유지하고 다음 실행 때 적용한다.
- `ControlCenter`: 기존 `manager.stop_warmup`/`manager.resume_launches`에 예약 진입 제어를
  연결하고 `shutdown_restart_barrier`와 `local_restarts`를 상태에 제공한다.
- `MainWindow`: 로컬 재시작 worker와 실행 중인 단계를 모두 비운 후 창 종료를 진행한다.
- `WorkspaceShutdown.SettleLegacyRestartsAsync`: 이전 서비스에는 위 기능이 없으므로,
  로컬 창 종료, 새 실행 금지, 다른 관리 요청 없음, transaction 없는 대기 예약임을 확인한다.
  기존 `supervisor.reconnect`로 Python 어댑터에 EOF를 보내 정상 종료시키고, 새 어댑터에
  실행 금지를 다시 설정한 뒤 SSH 종료를 요청한다. `manager.startup`이나 프로필 열기를
  실행하지 않는다. 어댑터 종료가 미확정이면 실행 금지를 유지한 채 같은 단계부터 재시도한다.
- 이미 transaction을 얻은 설정 적용·복구·연결 검증을 단순 대기 예약으로 취급하지 않는다.
  일반 제목줄 X, 자동 서비스 교체의 안전 조건도 완화하지 않는다.

## 검증과 범위

- 로컬 예약 대기 → 종료 진입 → worker 종료 → SSH 종료 요청 수락, 처리 중인 관찰과의
  경합, transaction 보존, 종료 취소 후 새 요청 수락에 대한 Python 회귀 검사.
- 관리창 검사는 이전 서비스 진입 조건, EOF 후 재연결 순서, 시간 초과 후 재시도를 검사한다.
- 별도 작업 폴더에서 실제 이전 서비스 바이너리와 이전 `ProfileRestarts`를 사용해
  `profile_prepare_busy`를 먼저 재현했다. 새 관리창의 종료 클래스와 실제 named pipe로
  예약 정리 → SSH 종료 요청 수락 → 어댑터 종료 → 서비스 프로세스 종료를 확인했다.
  이 시험의 SSH 작업 및 로컬 창 상태는 fixture이므로 실제 서버 종료를 검증한 것은 아니다.
- 배포 후보의 고정 Python 코드와 파일 해시를 따로 검사한다. 시험 자료는 Git 밖
  `work/validation/release-110`에 저장한다.

검증한 빌드는 `20261004-054830-557`이다. 고정 Python 검사 176개, 파일 해시 218개,
관리창 검사 18개를 통과했다. 이 빌드의 관리창만 교체한 뒤 수정 110 시작 로그와
기존 프로필 실행 세대 유지, 기존 서비스 연결을 확인했다. 앞서 설명한 이전 서비스
호환 경로가 다음 완전 종료에서 사용된다.

실제 사용 중인 모든 프로필의 완전 종료와 공식 앱 패키지 등록 완료는 별도 결과다.
관리창만 교체한 시점에 이 둘이 성공했다고 보고하지 않는다.
