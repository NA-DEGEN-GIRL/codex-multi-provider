# 수정 기록 (다른 에이전트·LLM용 요약)

이 문서는 이 저장소가 어떤 순서로, 왜 바뀌어 왔는지 한곳에서 보게 하는 색인입니다. 자세한 설계와
검증은 각 수정의 `docs/design/*-<번호>.md`에 있고, 이 문서는 요약과 링크만 둡니다. 새 수정을 넣을 때마다
맨 위 "최근 수정"에 항목을 추가하고 목록 표를 갱신합니다.

## 한눈에 보는 구조

- **관리창(WPF, `manager/Shell`)** ↔ **관리 서비스(Rust, `manager/service`, named pipe)** ↔ **Python 백엔드**
  (`scripts/control_center.py`, `scripts/manager_core/`).
- 계정마다 Codex 데스크톱 프로필 하나(ChatGPT 계정 여러 개 + 외부 API 프로필). 모든 프로필이 **하나의 공통
  기록 저장소**를 공유해 같은 작업을 다른 계정·공급자로 이어 갑니다.
- 패치한 Codex 런타임: `runtime/`(upstream 기반 worktree)을 `patches/codex-0.153.4-cross-provider.patch`로
  복원합니다(`patches/README.md`). Windows 런타임은 `artifacts/manager-runtime`, SSH용 Linux 묶음은
  `artifacts/remote/linux-*`에 둡니다(둘 다 Git 밖).
- 관리 앱 배포는 `artifacts/manager/releases/<빌드>`에 고정 사본으로 만들고 `current.json`이 다음 실행을
  가리킵니다. 실행 중인 앱은 저장소 파일을 직접 읽지 않습니다.

## 작업 규칙 (에이전트가 지킬 것)

- 사용자는 앱을 쓰는 중일 수 있습니다. 프로세스를 강제로 끝내지 말고, 빌드·적용이 필요하면 사용자가
  **완전 종료**한 뒤에 합니다. `-SelfTest` 빌드는 시험 창을 띄웁니다.
- 공개 저장소입니다. 커밋 전 추가된 줄에서 프로필 UUID, 계정 정보, 사용자·서버 이름, IP, 개인 경로·프로젝트
  이름, 토큰을 확인하고 `<ssh-host>`, `<profile>`, `remote-dev` 같은 자리표시자를 씁니다.
  `docs/MIGRATION.md`는 개인 정보 때문에 커밋하지 않습니다.
- 시험: `tests/*.py`(unittest, `tests/test_launchers.py` 제외 — 실제 GUI 실행), `manager/service`에서
  `cargo test`, Shell은 임시 출력 폴더로 `dotnet build`. 런타임은 `runtime/codex-rs`에서 crate 시험과
  app-server `portable_context` 시험. 알려진 기준선: Python `test_manager_ssh_pump` setUpClass 오류 1건
  (SshProxy Release 빌드 필요), app-server 전체 시험의 기존 실패 21건.
- 런타임 패치를 바꾸면 패치와 `patches/runtime-source.json`을 다시 만들고 `scripts/restore_runtime.py`로
  트리 ID가 재현되는지 확인합니다. 활성화에는 공유 편집·공통 저장소 무인 검증 보고서가 필요합니다
  (`scripts/activate_manager_runtime.py`).
- 큰 임시 파일(소스 복제본, 빌드 폴더)을 사용자 `%TEMP%`에 쌓지 않습니다. Windows 샌드박스 첫 설정이
  `%TEMP%` 전체에 권한을 적용하므로 설정이 느려집니다(수정 96).

## 최근 수정

### 수정 96 (진행 중) — 잦은 계정·공급자 전환의 캐시 최적화
- 전제: 사용자가 앞으로 같은 작업에서 ChatGPT 계정과 공급자(GPT↔DeepSeek 등)를 자주 바꿉니다.
- 조사 결과(최근 2주 실제 기록, 내용 없이 토큰 수치만):
  - 프롬프트 캐시는 서버에서 계정(조직)별로 분리됩니다. 계정을 바꾸면 첫 요청 1개가 캐시 없이
    전체 문맥을 읽고, 그다음 요청부터는 약 98% 적중합니다.
  - 전환이 드물던 과거 패턴에서는 전환 손실이 사용량의 약 0.4%였습니다.
  - 큰 비용은 요청마다 큰 문맥을 캐시로 다시 읽는 비용(약 63%)과 Fast 등급(2.5배)입니다.
- 계획과 구현:
  - 런타임: 다른 공급자 턴 뒤 돌아온 모델이 자기 이전 요청 앞부분을 그대로 이어 붙이기(검증 실패 시 기존
    재구성). 외부 공급자 압축 요청도 평소 턴과 같은 앞부분 사용. 요청 앞부분 변화 기록. 사용량 기록에
    공급자 표시.
  - 관리 앱: 사용량 장부, 공급자별 추론 강도·등급 복원, 프로필 카드의 캐시 상태·첫 요청 비용 표시.
  - 프로필별 런타임 임시 폴더(`%TEMP%\codex-manager\<profile>`)로 샌드박스 첫 설정 지연을 없앱니다.
- 설계와 결과: `docs/design/switch-cache-optimization-96.md`(작성 예정).

### 수정 95 — 선택한 프로필 먼저 시작, 요약 요청 캐시
- 시작 때 마지막으로 선택한(또는 알림의) 프로필을 먼저 열고, 그 창이 뜨거나 시간 한도가 지나면 나머지를
  함께 엽니다. 시작 중에는 창 연결을 최대 90초 기다립니다.
- 런타임: 압축 때의 평문 요약 요청을 압축 요청과 같은 앞부분으로 보내 캐시가 맞게 했습니다(실측 97%).
  요약 사용량과 걸린 시간을 체크포인트에 남깁니다.
- 문서: `docs/design/parallel-profile-launch-94.md`(수정 95 절), `docs/design/cross-provider-context-94.md`.

### 수정 94 — 병렬 시작, 완전 종료 누락, 공급자 간 문맥
- 서로 다른 프로필을 동시에 준비·시작(공유/배타 fence). 첫 실측에서 8개 동시 부팅이 첫 창을 늦춰
  수정 95의 우선 시작을 추가했습니다.
- 시작 직후 실패한 실행이 남긴 프로세스를 거두고, 완전 종료가 모든 프로필을 확인합니다.
- 런타임: GPT 압축 뒤 다른 공급자가 평문 요약으로 이어 받습니다. 실제 계정 확인: GPT→DeepSeek→GPT,
  요약만으로 이어 받기, 다른 ChatGPT 계정이 압축 이어 받기 모두 통과
  (`scripts/test_cross_provider_context_live.py`, 사용자 승인 후 실행).
- 문서: `parallel-profile-launch-94.md`, `full-exit-unsaved-launch-94.md`, `cross-provider-context-94.md`.

### 수정 93 — 기록 동기화 허브
- 계정 간 작업 목록 동기화를 관리 서비스의 허브로 모으고, 시작 때 개인 스킬 동기화의 잠금 경합을
  줄였습니다. `docs/design/record-hub-93.md`.

### 수정 92 — 응답성, SSH 내장 브라우저, 최신 데스크톱 지원
- 상태 갱신 지연(약 0.65초→0.15초), 창 연결 대기, 첫 알림 멈춤, SSH 원격 `127.0.0.1:<포트>` 열기,
  데스크톱 26.917 지원. `docs/design/responsiveness-and-ssh-browser-92.md`.

## 보류·다음 과제

- 수정 96의 나머지: 도구 목록 머리말 고정(측정 결과에 따라), 작업별 서비스 등급, 전환 전 압축(사용자
  확인), 큰 문맥 전달 한도.
- Claude 연동: 구독 OAuth 토큰을 꺼내 쓰는 서드파티 플러그인 방식은 약관상 하지 않습니다. 공식 Claude Code
  CLI를 외부 에이전트로 실행하는 설계 초안이 있으며, 공개 여부와 약관 해석을 사용자가 정할 때까지
  커밋하지 않습니다.
- 사용 설정 선택지(사용자 결정): Fast 등급 범위, 자동 압축 기준, 쓰지 않는 플러그인·MCP 정리.

## 전체 목록

| 수정 | 문서 | 주제 |
|---|---|---|
| 30 | [desktop-input-and-ssh-30](design/desktop-input-and-ssh-30.md) | 내장 창 키보드 입력과 공통 SSH 켜짐 상태 |
| 31 | [ime-and-project-membership-31](design/ime-and-project-membership-31.md) | 한영 전환과 본앱 프로젝트 이동 |
| 32 | [record-refresh-32](design/record-refresh-32.md) | 열린 대화 갱신과 재부팅 후 실행 연결 |
| 33 | [inline-ime-33](design/inline-ime-33.md) | 한글 인라인 조합과 독립 입력 창 |
| 34 | [viewport-sync-api-34](design/viewport-sync-api-34.md) | 화면 영역, 기록 갱신, API 프로필 |
| 35 | [renderer-refresh-and-restore-35](design/renderer-refresh-and-restore-35.md) | 보이는 기록 갱신과 창 수명 |
| 36 | [capture-layout-and-source-schema-36](design/capture-layout-and-source-schema-36.md) | 캡처, 배치, 이전 기록 목록 |
| 37 | [shutdown-and-switch-load-37](design/shutdown-and-switch-load-37.md) | 관리 종료와 프로필 전환 부하 |
| 38 | [switch-responsiveness-38](design/switch-responsiveness-38.md) | 프로필 전환 응답 진단 |
| 39 | [profile-presentation-39](design/profile-presentation-39.md) | 프로필 표시 복구 |
| 40 | [workspace-notifications-40](design/workspace-notifications-40.md) | 작업 공간 알림 |
| 41 | [profile-login-recovery-41](design/profile-login-recovery-41.md) | 프로필 로그인 복구 |
| 42 | [shared-login-storage-42](design/shared-login-storage-42.md) | 첫 로그인부터 공통 기록 |
| 43 | [new-profile-ssh-43](design/new-profile-ssh-43.md) | 새 프로필 SSH 준비 |
| 44 | [ssh-projects-and-delete-44](design/ssh-projects-and-delete-44.md) | SSH 프로젝트 선언과 기록 삭제 |
| 46 | [browser-policy-runtime-46](design/browser-policy-runtime-46.md) | 브라우저 정책 도우미 |
| 47 | [profile-order-and-notes-47](design/profile-order-and-notes-47.md) | 프로필 순서와 메모 배치 |
| 48 | [common-personal-skills-48](design/common-personal-skills-48.md) | 공통 개인 스킬 |
| 49 | [shared-projects-and-provider-resume-49](design/shared-projects-and-provider-resume-49.md) | 공유 프로젝트와 프로필별 모델 선택 |
| 50 | [external-model-settings-and-startup-recovery-50](design/external-model-settings-and-startup-recovery-50.md) | 외부 모델 설정과 시작 복구 |
| 51 | [project-membership-sync-51](design/project-membership-sync-51.md) | 프로젝트 끌어놓기 |
| 52 | [settings-backdrop-52](design/settings-backdrop-52.md) | 설정 대화상자와 프로필 기본값 |
| 53 | [profile-agent-badges-53](design/profile-agent-badges-53.md) | 하위 에이전트 표시 |
| 54 | [project-membership-mode-refresh-54](design/project-membership-mode-refresh-54.md), [shared-plugin-installations-54](design/shared-plugin-installations-54.md) | 모드 갱신 중 프로젝트, 공유 플러그인 |
| 55 | [profile-recovery-55](design/profile-recovery-55.md), [ssh-selfheal-note-fork-usage-plugins-55](design/ssh-selfheal-note-fork-usage-plugins-55.md) | SSH 수정·추론·역할, 자가 복구·메모·한도 |
| 56 | [local-first-profile-startup-56](design/local-first-profile-startup-56.md) | 로컬 창 먼저, SSH는 백그라운드 |
| 57 | [shared-notes-and-profile-startup-57](design/shared-notes-and-profile-startup-57.md) | 공유 메모와 시작 복구 |
| 58 | [viewport-move-resize-58](design/viewport-move-resize-58.md) | 화면 영역 이동과 복구 |
| 64 | [profile-startup-performance-64](design/profile-startup-performance-64.md) | 프로필 시작 성능 |
| 65 | [electron-background-performance-65](design/electron-background-performance-65.md) | Electron 백그라운드 작업 |
| 66 | [profile-launch-latency-66](design/profile-launch-latency-66.md) | 프로필 실행 지연 |
| 67 | [input-latency-67](design/input-latency-67.md) | 입력 지연 |
| 68 | [input-and-runtime-performance-68](design/input-and-runtime-performance-68.md) | 입력과 런타임 성능 |
| 69 | [resume-model-settings-69](design/resume-model-settings-69.md) | 재개 시 모델 설정 |
| 70 | [windows-skills-over-ssh-70](design/windows-skills-over-ssh-70.md) | SSH 프로젝트의 Windows 스킬 |
| 71 | [profile-open-contention-71](design/profile-open-contention-71.md) | 프로필 준비 경합 |
| 72 | [sidebar-split-72](design/sidebar-split-72.md) | 사이드바 구역 크기 |
| 73 | [workspace-lifetime-73](design/workspace-lifetime-73.md) | 업데이트 중 작업 유지 |
| 74 | [workspace-update-viewport-74](design/workspace-update-viewport-74.md) | 업데이트 호환 안내 |
| 75 | [mirror-source-and-provider-probe-75](design/mirror-source-and-provider-probe-75.md) | 화면 합성 배율과 DeepSeek 검사 |
| 76 | [ssh-shared-records-and-note-images-76](design/ssh-shared-records-and-note-images-76.md) | SSH 기록 편집과 메모 이미지 |
| 77 | [ssh-updates-77](design/ssh-updates-77.md) | SSH 버전 확인과 업데이트 예약 |
| 78 | [live-service-update-status-78](design/live-service-update-status-78.md) | 실행 중 서비스 기준 업데이트 |
| 79 | [ssh-reconnect-recovery-79](design/ssh-reconnect-recovery-79.md) | SSH 연결 유지 |
| 80 | [terminal-update-result-80](design/terminal-update-result-80.md) | 터미널 Codex 업데이트 결과 |
| 81 | [full-shutdown-81](design/full-shutdown-81.md) | SSH 업데이트 기록이 남은 완전 종료 |
| 82 | [ssh-reconnect-82](design/ssh-reconnect-82.md) | SSH 재연결과 오래된 예약 |
| 83 | [ssh-large-records-83](design/ssh-large-records-83.md) | 큰 SSH 응답 |
| 84 | [ssh-sidebar-cache-84](design/ssh-sidebar-cache-84.md) | SSH 사이드바 캐시 |
| 85 | [windows-execution-mode-85](design/windows-execution-mode-85.md) | Windows 실행 권한 선택 |
| 86 | [administrator-transition-86](design/administrator-transition-86.md) | 관리자 실행 전환 |
| 87 | [ssh-settings-deferred-87](design/ssh-settings-deferred-87.md) | SSH 설정 변경 후 대기 복구 |
| 88 | [browser-bundle-recovery-88](design/browser-bundle-recovery-88.md) | 내장 브라우저 구성요소 복구 |
| 89 | [permission-selection-persistence-89](design/permission-selection-persistence-89.md), [profile-remote-drain-89](design/profile-remote-drain-89.md) | 권한 선택 유지, 프로필별 원격 종료 |
| 90 | [sandbox-setup-window-activation-90](design/sandbox-setup-window-activation-90.md) | 샌드박스 설치와 창 활성화 |
| 91 | [shortcut-login-recovery-91](design/shortcut-login-recovery-91.md) | 작업 바로가기와 만료 로그인 |
| 92 | [responsiveness-and-ssh-browser-92](design/responsiveness-and-ssh-browser-92.md) | 응답성과 SSH 브라우저 |
| 93 | [record-hub-93](design/record-hub-93.md) | 기록 동기화 허브 |
| 94–95 | [parallel-profile-launch-94](design/parallel-profile-launch-94.md), [full-exit-unsaved-launch-94](design/full-exit-unsaved-launch-94.md), [cross-provider-context-94](design/cross-provider-context-94.md) | 병렬·우선 시작, 완전 종료, 공급자 간 문맥 |
