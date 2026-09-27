# 수정 97: SSH 런타임 업로드, 서버별 실패 분리, 작업 바로가기 상태, 앱 아이콘

## 1. SSH 연결이 안 되던 문제 (수정 96 배포 직후)

### 증상
수정 96으로 다시 연 뒤 한 프로필의 SSH 연결이 모든 서버에서 되지 않았습니다. 설정을 바꾼 프로필 세 개가
동시에 새 원격 런타임을 준비하던 중이었습니다.

### 원인
- **런타임이 너무 컸습니다.** 수정 94부터 upstream 릴리스 프로필이 `debug = "line-tables-only"`,
  `strip = false`로 바뀌었습니다. 패키징이 디버그 정보를 그대로 실어 Linux 묶음이 1.45GB였습니다
  (수정 93 이전 453MB).
- **같은 서버에 동시에 올렸습니다.** 프로필마다 따로 업로드해서 세 프로필이 한 서버에 1.45GB씩 동시에
  스테이징했습니다. 디스크가 작은 서버(여유 2.2GB)에서 셋 다 `remote_disk_full`로 실패했습니다.
  공간이 충분했더라도 느린 업링크를 셋이 나눠 써서 고정 600초 제한에 걸렸을 것입니다.
- **한 서버 실패가 프로필 전체를 막았습니다.** 설정 적용은 서버 묶음 전체를 한 번에 준비·시작합니다.
  한 서버 준비가 실패하면 적용 게이트가 `attention`이 되고, 같은 프로필의 다른 서버 연결도 모두
  "SSH 설정을 준비하지 못했습니다"로 막혔습니다.
- **잠재 버그:** 원격 '작업 유지 종료'는 실행 파일을 최대 1GiB까지만 해시합니다. 1.29GB인 수정 94~96
  런타임은 해시가 항상 `None`이 되어 `remote_drain_unaudited`로 거부됐습니다.

### 수정
- `scripts/remote_helpers/package_runtime.py`: 복사본에서 `codex`와 `codex-code-mode-host`의 디버그
  정보를 제거합니다(`strip --strip-debug --strip-unneeded`). 심볼은 빌드 서버의
  `work/remote-build/<arch>/package/*.debug`에 남기고, cargo 출력은 건드리지 않습니다.
  결과는 1.45GB에서 328MB(gzip 약 140MB)입니다.
- `scripts/manager_core/remote.py`:
  - 업로드 tar를 `gzip -1`로 보냅니다. 원격 설치기는 원래 `r|*`로 읽습니다.
  - 제한 시간은 크기에 비례해 600~3600초입니다(최소 256KiB/s 가정).
  - 런타임을 올려야 하는 프로필은 서버 식별자별 잠금(`work/control-center/remote-uploads/`)을 잡습니다.
    기다린 프로필은 사전 점검을 다시 해서, 먼저 끝난 업로드를 재사용합니다.
- `scripts/manager_core/remote_maintenance.py`, `update_hooks.py`, `profile_restart.py`:
  - 설정 적용(`isolate_host_failures`)에서 한 서버 준비가 `RemoteError`로 실패하면 그 서버만
    `prepare_failed`로 기록하고 나머지 서버는 새 설정으로 시작합니다.
  - 실패한 서버는 이전 바인딩을 유지한 채 `pending_policy_hosts`/`deferred_policy_hosts`에 올라가,
    이전 설정으로 다시 연결됩니다(기존 '설정 보류' 방식과 같습니다).
  - 게이트는 `released`와 `settings_deferred`, `deferred_reason=host_prepare_failed`가 되고, 작업
    안내는 `attention` 단계에서 실패한 서버와 이유를 보여 줍니다. 로컬 창과 다른 서버는 계속 쓸 수 있습니다.
  - 명시적인 SSH 런타임 업데이트(`force_runtime_update`)와 전체 재시작 복원은 기존처럼 실패를 그대로
    보고합니다.
- `scripts/remote_helpers/native_controller.py`: 해시 한도를 2GiB로 올리고, 경량 런타임
  digest(`b8f98675…`)를 종료 검증 목록에 추가했습니다.

### 운영 메모
- 경량 런타임은 `work/remote-candidates/linux-x86_64-98ad6b88-stripped`에 있으며, 완전 종료 뒤
  `artifacts/remote/linux-x86_64`로 교체합니다. 버전 문자열은 같고 묶음 ID(매니페스트 해시)만 다릅니다.
- 원격 서버의 오래된 런타임은 자동으로 지우지 않습니다. 이번에는 사용자 요청으로 한 서버에서 쓰지 않는
  런타임 2개와 캐시를 정리했습니다(1.84GB). 자동 정리는 남은 과제입니다.

## 2. 작업 바로가기 카드

요청: 작업 중인지 한눈에 보이게, 계정을 분명하게, 사용량 잔량을 원형으로, 하위 에이전트도 간결하게.

- 링(40 DIP): 바깥 링은 계정의 주간 사용량 잔량, 안쪽 링은 더 짧은 창이 보고될 때만 표시합니다.
  30% 미만은 주황, 10% 미만은 빨강입니다. 가운데에는 계정 이름(좁은 글자 3자, 한글 2자)을 둡니다.
  외부 API 프로필은 회색 링입니다.
- 상태 알약: 작업 중(초록, 점이 깜박임), 승인 대기·입력 대기(주황), 대기, 계정 닫힘. 다른 계정에서 실행 중이면
  `작업 중 · 01`처럼 그 계정을 붙입니다.
- 하위 에이전트 칩: `↳ deepseek-flash`, 여러 개면 `+N`, 외부 전용이면 `전용`, API 프로필은
  `API · 모델명`입니다. 저장된 정책이며 실제 호출 중인 모델 표시는 아닙니다.
- 계정 이동·별칭 변경·링크 삭제는 카드의 `⋯` 메뉴로 옮겼습니다. 카드를 누르면 작업이 열립니다.
- 파일: `manager/Shell/ShortcutCardData.cs`, `ShortcutCards.cs`(XAML 템플릿), `JsonValues.cs`, `MainWindow.cs`.

### 작업 상태 수집
- `RuntimeObserver`(`app_transport.py`)가 `thread/status/changed`의 `activeFlags`와 `turn/started`를
  보고 작업별 상태(`working`, `waiting_approval`, `waiting_input`)를 만듭니다. 제목이나 내용은 다루지
  않습니다.
- 로컬 런타임은 기존 `runtime-state.json`에 `thread_activity`를 싣습니다.
- SSH 연결은 스냅샷 파일이 없으므로 네이티브 SSH 프록시가 `instances/<profile>/ssh-activity-<host>-<pid>.json`을
  씁니다(`thread_activity.py`). 변경 시 또는 10초마다 쓰고, 종료 때 지웁니다. 읽는 쪽은 30초보다 오래된
  파일과 다른 실행 세대의 파일을 무시합니다.
- `instances.observe`가 두 출처를 합쳐 프로필마다 `thread_activity`를 붙이고, 셸이 바로가기의
  `thread_id`로 찾습니다.

## 3. 기타

- 앱 아이콘: `manager/Shell/Assets/app.ico`(16~256px). 어두운 타일에 사용량 링 두 개와 `>_`를 넣었습니다.
  작은 크기는 링을 빼고 글리프를 키웠습니다. `ApplicationIcon`으로 넣어 모든 창이 이 아이콘을 씁니다.
- '지금 열린 작업 추가'가 `Clipboard format DataObject cannot be preserved safely`로 실패하던 문제:
  OLE 클립보드의 표식 형식(`DataObject`, `Ole Private Data`)은 데이터가 아니므로 저장·복원에서 건너뜁니다.

## 검증
- Python: SSH·원격 관련 모듈 167개, 작업 상태·전송·SSH 프록시 78개 통과. 새 시험:
  서버별 실패 분리(엔트리 수준·로컬 우선 흐름), 압축 업로드와 대기 후 재사용, 업로드 잠금 시간 초과,
  작업 상태 매핑, SSH 상태 파일의 병합·만료·갱신.
- Shell: 컴파일 경고·오류 0. 카드 템플릿은 관리 앱과 별개인 오프스크린 렌더러로 그려 확인했습니다.
- 경량 런타임: `codex --version` 확인, 종료 표식·외부 에이전트 표식 문자열 유지 확인.
- 실제 앱에서의 확인(작업 상태 표시, 새 런타임 설치 시간)은 적용 후 합니다.
