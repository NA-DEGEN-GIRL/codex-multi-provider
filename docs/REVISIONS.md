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

### 수정 126 — 프로필별 Codex 닫기와 실행 중 계정의 "닫고 제거"

- 증상: 계정을 목록에서 제거하면 `profile.remove`가 "이 계정의 Codex 창을 닫은 뒤 제거하세요"로 거부했다. 프로필 창은 관리창 안에
  들어 있어서 한 프로필만 닫을 방법이 없었다(완전 종료만 모든 프로필을 닫는다).
- **Codex 닫기**(프로필 카드 오른쪽 클릭 메뉴와 "프로필 관리" 메뉴): 완전 종료가 앱마다 하는 일을 한 프로필에 한다.
  - 확인 창에 작업 바로가기와 같은 작업 상태(`thread_activity`: 작업 중·승인 대기·입력 대기, 로컬과 SSH)를 세어 진행 중인 작업을 경고한다.
  - 창을 관리창에서 분리한 뒤 같은 종료 요청(`NativeWindowShutdown.RequestAsync`, 기본 60초 + 종료 정리 20초)을 보낸다.
    끝까지 창이 남아 있으면 종료가 시작되지 않은 것으로 보고 닫기를 실패로 끝낸다(앱과 작업은 유지, 창은 다시 연결).
  - 남은 프로세스 정리(`profile.cleanup` → 서비스 `process.stop`, 같은 세대만)를 한다. 창을 찾지 못한 실행은 이 정리로 끝낸다.
  - 준비된 SSH 원격 실행은 완전 종료와 같은 `profile.remote_stop`으로 현재 답변이 끝난 뒤 종료한다. 확인되지 않으면
    "원격 실행을 서버에 남겨 두고 닫기"를 묻는다. 완전 종료의 원격 종료 대기는 `StopRemoteRuntimesAsync`로 분리해 둘이 같이 쓴다.
  - 닫은 프로필은 목록에 남고 "닫힘"으로 보이며, 다시 선택하면 보통처럼 새로 연다.
- **닫고 제거**: 실행 중인 계정을 제거하면 거부 대신 같은 확인 창(제거 안내 포함)을 띄우고, 닫기가 끝난 뒤 제거한다.
  상태가 늦어 서비스가 `profile_running` 코드로 거부해도 같은 흐름으로 이어진다. 연결된 작업 바로가기처럼 제거할 수 없는 이유는
  아무것도 닫기 전에 거부한다(`ProfileLifecycle.check_removable`). 닫기가 실패하면 이유와 함께 제거하지 않는다.
- **다시 열림 방지**: 서비스 명령 `profile.close_begin`/`profile.close_end`(Rust 허용 목록 추가, 상태 `capabilities.profile_close`).
  `close_begin`은 그 프로필의 launch admission 안에서 `Instances.hold_launches`를 걸어, 이미 들어온 실행이 끝난 뒤의 실행 정보를 돌려주고
  이후의 실행을 막는다. 미리 열기(warmup은 `closing`으로 건너뜀), 작업 열기의 새 실행, 로그인, 설정 적용 다시 열기,
  SSH 재연결의 로컬 열기, 이미 실행 중인 창의 `profile.show`가 모두 `profile_closing`으로 거부된다. 기다리던 작업 열기는 `ENDS_WAITING_OPENS`로 버린다.
  `close_end(remove)`는 같은 보류 안에서 제거하고, 성공·실패와 관계없이 보류를 푼다. 관리창이 도중에 사라져도 보류는 15분 뒤 풀린다.
  로컬 설정 적용이 진행 중이거나 예약된 프로필은 닫지 않는다(SSH 전용 백그라운드 작업은 원격 종료가 넘겨받는다).
- 관리창은 닫는 동안 그 프로필 창을 다시 연결하지 않고(`_closingProfiles`), 같은 프로필의 다른 동작은 프로필 동작 잠금으로 막는다.
  닫기 중에는 완전 종료를 잠시 미룬다. 이전 버전 서비스에 연결된 경우에는 아무것도 닫지 않고 완전 종료 후 새 버전으로 열라고 안내한다.
- 시험: `tests/test_manager_profile_close.py`(닫고 제거, 닫기 실패 시 제거 거부, 보류 중 실행·`profile.show`·warmup 거부, 닫기만 한 뒤 다시 열기,
  제거 불가 사전 거부, 설정 적용 중 거부, 보류 만료), Rust `per_profile_close_commands_reach_the_backend`, Shell 자체 시험
  `WindowControlsSelfTest.CheckProfileClose`(닫힌 프로필과 서비스 미지원 시 종료 요청 없음, 확인 문구).

### 수정 125 — 런타임 핫픽스: 새 GPT 모델 버전 식별, Claude 뒤 GPT 문맥 재구성, Claude 압축 거부 표시, 공유 경로 음성 캐시

0.153.4 런타임 패치만 바꾼다(결과 트리 `c1ac1ce5…`, 패치 SHA256 `0767d660…`, 423개 파일). 관리 앱 코드 변경은 없다.

- **새 GPT 모델 버전 식별:** 이 빌드의 번들 카탈로그에 없는 모델(0.160 카탈로그가 더한 sol 계열 등)은 추론 요청에서
  카탈로그 클라이언트 버전을 `version` 헤더와 user agent로 보낸다(HTTP, websocket 연결, `/responses/compact`).
  서비스는 최소 버전보다 낮은 클라이언트에 "model is not supported when using Codex with a ChatGPT account"로 거절했다.
  식별 버전이 바뀌면 websocket을 다시 연결한다. 번들 카탈로그가 아는 모델은 이전처럼 이 빌드의 버전을 쓴다.
- **Claude→GPT 예산 재구성:** 마지막 체크포인트 뒤의 Claude(로컬 에이전트) 평문 turn 때문에 이어 받을 기록이 모델 문맥 창보다
  커지면, 다시 열 때 예산 안으로 문맥을 재구성한다. 이전에는 첫 GPT turn이 전체 기록을 원격 압축하려다 압축 요청 자체가 창을 넘었다.
  재구성한 문맥의 토큰 사용량도 다시 계산한다.
- **Claude 압축 거부 표시:** Claude 공급자에서 수동 압축은 오류 전에 turn 시작을 알린다. 이제 압축이 계속 실행 중으로 보이지 않고 실패로 끝난다.
- **공유 경로 음성 캐시:** 소유 프로필이 새 공유 작업을 알린 직후, 첫 상태 행이 생기기 전에 다른 프로필이 읽으면
  `SharedStore::route`가 그 실패를 자기 저장소 경로로 영구 캐시했다. 그 런타임이 다시 시작될 때까지 `thread/read`가 "thread not loaded"로 실패했다.
  이제 자기 저장소에 기록(상태 행 또는 실행 중 recorder)이 있을 때만 경로를 고정하고, 없으면 다음 호출에서 카탈로그로 다시 찾는다.
- 시험(빌드 서버): codex-core lib 2,579 통과·3 실패(`multi_agents` 2, `turn_tests` 1 — 수정 121 배포 트리에서도 실패하는 기존 실패),
  core 스위트(`inference_client_version`, `client_websockets`, `claude_code`) 51 통과, app-server `manager_record_catalog`(공유 실행 포함) 32 통과,
  `tasks::claude_code`·`portable_context`·`client_version`·`rollout_reconstruction`·`tasks::compact` 93 통과.
- 배포 후보(활성화하지 않음): Linux 묶음 `0.153.4-managed-0767d660038aa500`(`codex` `1c769954…`, 격리 SIGHUP 종료 감사 통과 →
  `DRAIN_AUDITED_SHA256`에 추가), Windows 서버 교차 빌드(CRLF, `codex.exe` `9a27df01…`)를 스테이징했다. 공유 편집(paginated)·공통 저장소 무인 검증,
  Claude 네이티브 e2e 12개가 PASS이고 마이그레이션 지문은 수정 121과 같다. 활성화 게이트는 포인터를 쓰지 않는 시험 실행으로 통과했다.
  적용은 완전 종료 뒤 일회성 helper(`work/swap-rev125-on-exit.py`, 한 번만 활성화·더 새 런타임이면 건너뜀)로 한다.

### 수정 124 — Claude 사용 한도 안내·알림·재설정 후 자동 이어하기

- 증상: 03:13 KST에 한 Claude 프로필의 SSH 작업 3개가 함께 멈췄다. CLI 기록은 합성 assistant 메시지(`error: "rate_limit"`, `isApiErrorMessage`,
  "You've hit your session limit · resets 4:30am (Asia/Seoul)")로 끝났지만 앱에는 일반 오류 "Claude did not complete this turn…"만 보였다.
  수정 121의 R1은 `authentication_failed`만 구분했다. 계정이 5시간 한도에 닿아도 알림이 없었다.
- 러너(`claude_runner.py`):
  - 감지: assistant `error == "rate_limit"`, 또는 is_error 결과의 CLI 한도 문장(`You've hit/reached your …`, `You're out of usage credits/extra usage`).
    CLI 2.1.282 번들의 한도 이름은 session, weekly, Opus, Sonnet, Fable, usage credit, monthly spend limit, team's shared budget이다.
  - 코드 `claude_usage_limit`. 영어 문장 끝에 고정 꼬리표 `[usage-limit: <kind>, resets <UTC ISO>]`를 붙이고, `done.error`에 `limit_kind`, `resets_at`(epoch), `reset_text`를 넣는다.
  - 재설정 시각: 거부된 `rate_limit_event`의 `resetsAt`을 먼저 쓴다. 없으면 CLI 문장("4:30am (Asia/Seoul)", "Oct 12, 4am (…)")을 그 시간대로 계산한다.
    시간대 DB가 없으면(Windows) 러너 기계의 시간대를 쓰고, 31일보다 먼 값은 버린다.
  - CLI 문장은 commentary나 알림으로 남기지 않고 "Claude reached its usage limit." 알림만 보낸다.
  - 이어 쓰기: R4의 `auth_stop` 표시를 그대로 쓰고 `reason: "usage_limit"`를 더한다. 다음 턴은 같은 세션을 이어 쓰며 한도 안내 문장(`USAGE_STOP_NOTE`)을 앞에 붙인다.
    런타임 `context::resumable`은 표시의 모양(turn_id·turn_fingerprints)과 멈춘 turn의 마지막 항목이 Claude 출력인지만 보므로 Rust 변경이 없다.
    도구 결과도 Claude 출력 메시지로 기록되므로 도구 뒤에 멈춘 turn도 이어진다. 출력 없이 첫 요청에서 멈추면 새 세션으로 시작한다.
- 관리 측 감지: 로컬 런타임 프록시와 SSH 펌프의 `RuntimeObserver`가 `turn/completed`(failed)와 `error`(willRetry false)의 오류 문장에서 꼬리표만 읽는다.
  thread·turn·종류·재설정 시각을 `work/control-center/usage-continuations/inbox/`에 파일 하나로 남기고, 문장 자체는 남기지 않는다.
- 자동 이어하기(`usage_continuation.py`, 백엔드 스레드 15초 주기):
  - 멈춘 turn마다 한 번, 재설정 + 60~120초에 예약한다. `state.json`에 저장해 관리자를 다시 시작해도 유지한다.
    예약·전송·건너뜀은 모두 `log.jsonl`과 관리창 로그에 남긴다(메시지 내용은 남기지 않음).
  - 보내기 직전 확인:
    - 프로필이 있고 설정이 켜져 있는가.
    - 작업이 로드되어 쉬는 중인가.
    - 가장 최근 turn이 멈춘 turn이고 failed인가(아니면 새 메시지가 있는 것).
    - 사용량 창이 아직 100%로 보이면 새 재설정으로 다시 예약한다(최대 3회).
    - 프로필이 닫혀 있으면 15분 기다린 뒤 건너뛰고, 12시간 넘게 늦었으면 건너뛴다. 재설정 시각을 모르면(크레딧 등) 예약하지 않는다.
  - 전송: 프로필의 인증된 런타임 관리 통로(admin)에 `turn/start`를 더했다. 고정 이어하기 문장 하나만 받는다.
    최신 turn 확인용 `thread/turns/list`는 1개, id·상태만 돌려준다. 결과가 불확실한 전송은 다시 보내지 않는다.
    보낸 이어하기가 10분 안에 다시 한도로 멈추면 같은 멈춤의 재시도로 센다.
  - 설정: Claude 프로필 창의 "한도 재설정 후 자동으로 이어하기"(기본 켜짐). `claude_settings` 밖의 `claude_auto_continue`라서
    런타임 설정이나 SSH 바인딩을 바꾸지 않고, 프로필을 다시 열 필요도 없다.
- 알림(`usage_alerts.py`):
  - 대상: 모든 계정(Codex·Claude)의 5시간·주간 창. 90%를 넘을 때, 100%이거나 Claude 작업이 한도로 멈췄을 때, 재설정 시각이 지났을 때("다시 사용 가능").
  - Windows 알림과 카드 경고 칩("한도 93%", "한도 도달 · 자동 이어하기 04:31")으로 보인다. 로컬 작업의 멈춤 알림을 누르면 그 작업으로 이동한다.
  - 창·재설정 주기마다 한 번만 알린다(`usage-alerts.json`). 첫 실행은 현재 상태를 기준으로만 기록하고, 새 네트워크 호출은 없다.
- 적용:
  - 러너·관리 측 모두 Python이라 런타임(Rust) 빌드가 필요 없다. 관리 서비스 명령 목록도 그대로다.
  - 관리 앱을 빌드한 뒤 프로필을 다시 열어야 프록시와 관리 통로가 바뀐다. 이전 프록시에서는 자동 이어하기가 `runtime_outdated`로 건너뛴다.
  - SSH 호스트의 러너는 준비할 때 올리는 helper 파일이다. SSH 연결을 다시 준비하기 전까지는(또는 다시 준비를 포함하는 SSH 업데이트 전까지는) 이전 러너가 돈다.
    그동안 SSH 한도 멈춤은 일반 오류로 보이고 자동 이어하기도 동작하지 않는다. 90%·100% 알림은 그대로 동작한다.
- 미확인: 관리 통로로 시작한 turn을 데스크톱 화면이 사용자 메시지로 그리는지는 실제 앱에서 확인하지 않았다(진행 알림은 같은 연결로 전달된다).
  시험: 가짜 CLI의 한도 시나리오 5종, 관찰자·관리 통로 검증, 가짜 시계 예약기(건너뜀·재예약·재시작·연쇄), 알림 중복 방지, Shell 컴파일.

### 수정 123 — 작업 바로가기 반응 개선, 진행 표시, 마지막 클릭 우선

- 근거: 10월 7–9일 작업 바로가기 클릭 67건을 측정했다. 앱이 이미 떠 있고 호스트가 연결된 경우는 약 1초(p90 4초)에 열렸지만,
  실패·지연 38건 중 33건은 120초 안에 다시 눌렸다. 원인은 (1) 화면 선택 확인이 SSH 상태와 무관하게 16초에 오류를 냈고,
  (2) 카드에 진행 표시가 없었고(작업 열기는 진행 표시에서도 빠짐), (3) 꺼진 프로필의 시작이 클릭 안에서 준비 전체와 8초 창 대기를 거치며
  미리 열기 묶음(최대 8개 동시)보다 우선하지 못했고(묶음 안 p50 약 40초, 단독 8–26초), (4) 새 클릭이 이전 요청을 최대 25초 기다리고,
  열기 중 프로필 카드 클릭은 거부됐으며, 백엔드가 모든 작업 열기를 전역 잠금 하나로 처리했고, (5) 고정 750ms 폴링, 열 때마다 실행 환경
  재구성과 두 번째 ChatGPT.exe 실행, 200ms 선택 확인 폴링이 있었고, (6) shell 로그가 300줄만 남고 클릭별 기록·앱 종료 원인이 없었으며,
  (7) 대기 중 앱이 종료되면 열기가 취소되어 다시 눌러야 했다.
- 기록(A): shell 로그는 한 줄씩 그날 파일에 덧붙인다(날짜가 바뀌거나 16MB를 넘으면 새 파일, 화면·복사는 최근 300줄). 텍스트 로그는 7일·50MB,
  성능 기록은 7일·100MB까지 두고 오래된 것부터 지운다(현재 프로세스 파일과 다른 로그는 지우지 않음). 성능 기록은 4MB마다 넘기며 3개를 남긴다.
  클릭마다 `shortcut_open` 기록 하나(click, request_sent, launch_start/launch_ready(서비스가 잰 시간), reader_wait/ready, ssh_wait/ready,
  navigation_sent, confirmed, 결과)와 로그 요약 한 줄을 남긴다. 백엔드는 열기 결과를 고정 코드로 `profile-launch.performance.jsonl`에 남긴다.
  관리 앱 창이 닫히면 프로세스 종료를 기다려 종료 코드, 흔한 의미, 관리자가 닫거나 다시 여는 중이었는지를 기록한다.
- 진행 표시(B): 누른 카드와 상태 줄에 "앱 시작 중 · 12초", "SSH 연결 중 · <host> · 20초", "대화 여는 중"을 초 단위로 보여 준다. 상태 줄은 높이가
  고정이라 Codex 화면 크기가 바뀌지 않고, 머리글 진행 표시는 다른 요청 옆에 열기 개수를 함께 센다. 열기가 진행되는 동안 창이 없는 것은 오류로
  보이지 않는다(열기 자체가 제한된 시간 안에 결과를 낸다). 작업 열기 요청 제한은 90초, SSH 작업은 화면 선택 확인을 5초/20초 기다리고 미확인도
  오류가 아닌 안내로 보인다.
- 우선 처리(C): 꺼진 프로필의 작업 클릭은 미리 열기 순서와 시작 대기열에서 앞으로 가고, 그 프로필이 시작될 때까지(최대 60초) 새 미리 열기를 멈춘다.
  여유 커밋 메모리(GlobalMemoryStatusEx `ullAvailPageFile`)가 12GB 미만이면 미리 열기를 동시에 3개, 6GB 미만이면 2개로 줄인다(대기 중인 우선 프로필 제외).
  공유 실행에서는 앱 프로세스가 생기면 바로 돌아오고(창 대기는 백그라운드), 기존 준비 대기가 이어서 링크 시점을 정한다.
- 마지막 클릭 우선(D): 새 클릭은 진행 중인 모든 열기(요청, 준비 대기, 선택 확인)를 즉시 멈추고 바로 자기 요청을 보낸다. 같은 작업을 다시 누르면 합류한다.
  작업 열기와 프로필 열기는 서로 대체하고, 설정 적용·로그인·복구는 여전히 거부한다. 백엔드는 작업 열기를 대상 프로필별로 직렬화하고(전역 잠금 제거),
  같은 프로필의 늦게 도착한 열기가 있으면 앞선 열기는 시작한 실행만 마치고 링크를 보내지 않는다(`superseded`). 같은 프로필의 새 대기는 이전 대기를 지운다.
- 이벤트 방식(E): 실행 중인 앱의 로컬 작업은 그 앱의 app-tools 파이프(`navigate_to_codex_page`, 설명자의 프로필·실행·프로세스와 파이프 서버 PID를
  확인)로 보내 두 번째 ChatGPT.exe를 띄우지 않는다. SSH 작업(도구에 호스트 ID가 없음)과 파이프 실패는 기존 링크로 보낸다. 링크 허가는 기록 저장소 키만
  계산하고(`Instances.navigation_environment`), 전체 실행 환경은 두 번째 실행이 필요할 때만 만든다. 준비 확인은 요청·응답 IPC라 서버 대기 대신
  150ms에서 600ms까지 늘려 가며 묻고, 화면 선택 확인은 `active-task.json` 변경 이벤트(최대 1초)를 기다린다.
- 앱 종료(F): 준비 대기 중 그 대기가 따르는 앱이 끝나면 한 번 다시 시작하고 같은 작업을 이어 연다("앱 다시 시작 중"). 다른 실행(재시작·업데이트)이 생기면
  취소, 두 번째 종료나 시작 거부(완전 종료 중 등)는 링크 없이 끝낸다. 프로필 중지·재시작·로그인은 그 프로필의 대기를 지운다. 링크를 보낸 뒤 선택 전에
  앱이 끝나면 shell이 같은 작업을 한 번 다시 연다.
- 미룬 일: SSH 연결 미리 시작(별도 작업). 시험: Python 단위 시험, Shell 컴파일과 자체 시험 시나리오 갱신(시험 바이너리는 실행하지 않음).

### 수정 122 — SSH 이전 런타임 재실행 방지, SSH 자동 업데이트 기본 켜기, 완전 종료 대기 자동화

- 원인: SSH 서버들이 수정 118의 sqlx 풀 정리 버그 수정(launchbadge/sqlx#3645) 이전 런타임 묶음(`98ad6b88` 등)을 계속 실행했다.
  자동 업데이트가 꺼져 있어서, 관리자를 열 때마다 각 연결이 기억한 이전 묶음이 다시 시작됐다. 그 프로세스는 약 10분 뒤 CPU 100%로 멈추지 않고,
  관리 종료(SIGHUP)에도 끝나지 않아 완전 종료와 업데이트가 반복해서 멈췄다(4코어인 호스트에서 특히 드러남).
  이전 런타임은 작업 상태 확인 지표가 없어서(`remote_idle_diagnostics_unavailable`) 업데이트 작업도 기다리기만 했다.
- 프로필을 열 때 원격 실행이 멈춰 있는 호스트가 현재 제공 묶음과 다른 묶음에 연결돼 있으면, 이전 묶음을 시작하지 않고 현재 묶음으로 준비해 시작한다.
  실행 중인 원격 작업은 그대로 둔다. 새 호스트 연결은 다른 프로필이 남긴 이전 묶음을 재사용하지 않는다.
- SSH 자동 업데이트(작업 종료 후 적용)를 기본으로 켜고, 기존 연결도 한 번 켠다(사용자가 끈 값은 이후 유지).
  멈춘 호스트의 이전 묶음도 자동 업데이트 대상이다. 대기 중인 작업의 SSH 확인 간격은 15초에서 최대 2분(오류 시 10분)까지 늘린다.
- 업데이트 상태와 `health_report.py`에 이전 묶음 연결·실행을 표시한다(`stale_bundle`, "이전 버전 SSH 런타임: N건").
- 시작 중 SSH 작업 열기로 앱이 끝나던 문제: 꺼진 프로필의 SSH 작업 바로가기는 로컬 런타임 준비 직후 `codex://threads/<id>?hostId=remote-ssh-discovered:<host>`를 보냈다.
  시작 중인 앱은 이 링크를 시작 마지막(`flushPendingDeepLinks`)에 처리하며 아직 등록되지 않은 SSH 연결을 찾다가 "Connection for host ID … not found"로 시작 자체를 실패했다.
  이제 관리자는 이번 실행의 SSH 연결이 돌기 시작한 뒤(`ssh-activity-<host>-*.json`)에만 링크를 보내고(최대 90초, 넘으면 보내지 않음), 관리용 데스크톱 사본은
  시작 중 링크 실패를 치명 오류로 만들지 않는다(`desktop_bundle.deep_link_guard_patches`, 26.930부터 필수 · 다음 관리 앱 시작 때 새 사본을 만든다).
- 운영 조치(코드 외): 2026-10-09 모든 연결(33개)을 `e224215e`로 옮기고, 작업 없이 멈춰 돌던 이전 프로세스를 종료했다.
  세 호스트에서 쓰이지 않는 이전 런타임 폴더도 지웠다.
- 완전 종료 대기 자동화: 앱 11개가 메모리 부족 속에 함께 끝나면 60초 정상 종료 대기를 넘겨 매번 두 번 눌러야 했다. 대기를 앱 수에 맞춰 늘리고
  (60초 + 3개를 넘는 앱마다 10초, 최대 180초 · 11개 140초) "N/M 종료됨 · 경과 초"를 보여 준다. 끝까지 남은 앱 중 창을 닫고 종료 정리 중인 앱은
  20초 더 기다린 뒤 두 번째 누름과 같은 남은 프로세스 정리로 바로 이어 간다. 창이 그대로인 앱과 정리 실패만 관리창을 유지한다(정리 확인 6초→15초).

### 수정 121 — SSH Claude 로그인 자동 갱신·계정별 장기 토큰, llm-usage 연동 정리

**Claude 로그인 갱신과 장기 토큰** ([원인·변경·호환·적용 순서](design/claude-login-renewal-121.md))

- 증상: SSH Claude 작업 6개가 함께 일반 오류 "Claude did not complete this turn"로 끝났다. 원인은 턴 시작 때 빌린 8시간짜리 Windows 로그인 토큰이 턴 중에 만료된 것(401)이다.
  CLI는 이 토큰을 갱신할 수 없었고, 실행 중인 턴에 새 토큰을 넘길 경로도 없었다.
- 러너·런타임 변경:
  - 인증 실패를 `claude_auth_expired`/`claude_auth_rejected`/`claude_login_required`로 구분한다.
  - 만료되면 한 번 갱신해 같은 세션(`--resume`)으로 이어 한다.
  - 갱신하지 못한 멈춤은 다음 턴이 같은 세션을 이어 쓴다(`auth_stop`).
  - 실행 중인 CLI의 토큰을 미리·거부 뒤에 교체한다(`update_environment_variables`, 401 대기).
- Windows 중개자는 270초 이상 남은 토큰만 빌려 주고, 같은 계정의 동시 요청을 한 번의 읽기로 합친다(작업자 8개).
- SSH 호스트(Linux)의 토큰 전달:
  - 러너에는 stdin 채널로, CLI에는 `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`로 넘기고, 모두 소켓 쌍이다. 파일·환경 변수·로그에는 남지 않는다.
  - 러너와 프록시는 non-dumpable이다. Claude 계정이 있는 프로필의 런타임은 core hard 제한 0으로 시작하므로 그 프로필에서는 `ulimit -c`를 올릴 수 없다.
- 계정별 장기 토큰(`claude setup-token`, 1년):
  - Claude 로그인·설정 창에서 발급 창을 열고 붙여넣어 DPAPI로 저장한다. `claude.token.save/remove/retry/issue` 명령으로 다룬다.
  - 런타임이 `executionPresetCredentialSources: 2`를 알리고 수정 121로 준비한 바인딩일 때만 SSH 작업에 빌려 준다.
  - 거부되면 턴마다 한 번만 기록하고 같은 턴은 PC 로그인으로 이어 한다. 같은 Linux 사용자의 프로세스가 작업 중 토큰을 읽을 수 있다는 점은 패널이 경고한다.
- 적용: Windows·Linux 런타임 빌드(트리 `0511f3be…`), 관리 앱 빌드, 완전 종료 뒤 다시 열기, SSH 업데이트·다시 준비가 필요하다. 각 단계는 이전 판과 섞여도 안전하게 동작한다.

**llm-usage 연동 정리** ([없앤 것·남긴 호환 경로·정리 순서](design/llm-usage-removal-121.md))

- 쓰지 않는 llm-usage 연동이 계속 돌았다. 상태 조회가 30초마다 계정 동기화를 했고, SSH 목록 읽기가 호스트마다 계정 레지스트리를 확인했다.
  둘 다 없앴다. `profile.bind`·`accounts.list` 명령을 지웠고, ↻(`accounts.refresh`)는 Windows 로그인 프로필만 확인한다.
- 기존 상태는 다시 쓰지 않는다. 가져온 프로필의 home·기록·이름·`usage:` source와 예전 필드는 그대로 남는다.
  SSH 호스트의 `catalog-mixed-sources.json`에 이미 등록된 home은 폴더가 있는 동안 유지된다.
- 수정 121 이전에 준비한 SSH helper는 다시 준비할 때까지 시작마다 레지스트리를 읽는다. 레지스트리를 읽지 못하면 시작이 실패한다.
  호스트에서는 `~/.config/llm-usage`를 통째로 지우거나 먼저 다시 준비한다.
- 새 릴리스를 활성화하기 전에는 llm-usage 체크아웃과 `%LOCALAPPDATA%\llm-usage`를 지우지 않는다.
  사용자의 Claude Code `statusLine`도 아직 llm-usage를 실행한다.
- 런타임 README의 SSH 기존 기록 검색 문장도 "기본 `~/.codex`와 이미 등록된 home"으로 고쳤다.

### 수정 120 — 런타임 시작 실패 자동 재시도, 시작 실패 뒤 오류창 정리

- 프로필 여러 개가 한꺼번에 시작할 때 커밋 메모리가 한도에 가까우면 런타임이 DLL 초기화 단계에서 `0xC0000142`로 끝났다.
  이어서 런타임 프록시가 종료 중 `sys.stdin` 잠금 때문에 강제 중단되어, 앱에는 "failed to start (code=3221225477) … finalizing" 창이 떴다.
- 프록시가 앱 입력을 `os.read`로 읽고 항상 `os._exit(런타임 종료 코드)`로 끝난다. `0xC0000142`는 런타임이 아무것도 출력하지 않았고 initialize도 끝나지 않았을 때만
  보관한 입력을 그대로 넘겨 최대 2회 다시 시작한다(10초·4 MiB 한도, 접근 위반은 재시도하지 않음). 런타임은 프록시의 콘솔을 같이 써서 콘솔 호스트가 하나 줄었다.
- `runtime-state.json`의 `start_retries`와 `health_report.py`의 복구 줄로 재시도를 확인한다.
- [원인·변경·검증](design/runtime-start-retry-120.md).

### 수정 119 — Claude·SSH 버그 수정, 관리창 속도 개선, 재발 방지 장치

- Claude 전환이 이어 쓰는 세션에서 뒤쪽 요약을 버려 `/compact` 뒤에도 거절되던 문제(런타임 `ccc7fae5`), SSH Claude 러너가 사용량 이벤트에서 죽던 문제,
  SSH 포크 대화가 원래 메모를 공유하지 못하던 문제, 4958 데스크톱에서 SSH worktree 대화가 목록에서 사라지던 문제를 고쳤다.
- 측정으로 찾은 느림: 같은 내용의 플러그인 표시 파일 재작성에 따른 재복사, 대화 열기 안의 Browser 복구, 무거운 상태 조회를 줄였다.
- 재발 방지: 활성화 관문(마이그레이션·PE 검사·릴리스 빌드), 정상 확인 런타임으로만 되돌리기, 런타임 종료 원인 기록, `scripts/health_report.py`, SSH 업데이트 알림.
- [원인·변경·남은 과제](design/stability-perf-119.md).

### 수정 118 — 원격 Codex 종료 멈춤 수정, 완전 종료 대기 선택지, Windows 교차 빌드

- 완전 종료가 한 서버의 원격 Codex 종료 확인을 기다리며 멈췄다. 원인은 sqlx-core 0.9.0 풀 정리 작업의 무한 루프였다
  (유휴 연결 수가 `usize::MAX`로 넘어가 양보 없이 돎, launchbadge/sqlx#3645). 이 루프가 작업 스레드를 점유해 종료가 끝나지 않고, 평소에도 CPU 한 코어를 쓴다.
  `codex-state` 풀에서 유휴·수명 제한을 꺼 정리 작업을 만들지 않는다. Windows·Linux 런타임을 다시 빌드한다.
- 원격 종료 확인이 제한 시간을 넘기면 오류 대신 "원격 실행을 서버에 남겨 두고 완전 종료" 선택 창을 띄운다.
  대기 중에는 대상 프로필과 경과 시간을 보여 준다.
- 빌드 서버 빌드를 64스레드(CPU 0-31·64-95, nice 10)로 제한하고, Windows 런타임을 서버에서 교차 빌드한다
  (`package_windows_runtime.py`, clang-cl·lld-link·xwin MSVC 14.44/SDK 10.0.26100, 약 8분). 받은 패키지는 `stage_manager_runtime.py --source`로
  해시·패치 트리를 다시 확인해 후보로 만든다. LLVM 매니페스트 병합이 Windows가 거부하는 매니페스트를 만드는 문제를 `/MANIFESTUAC:NO`로 막았다.
  처음 적용한 교차 빌드본은 LF 소스라 sqlx 마이그레이션 체크섬이 기존 기록 DB(CRLF로 마이그레이션됨)와 달라 시작하지 못했다(로컬 빌드로 되돌림).
  교차 빌드를 CRLF 체크아웃으로 바꾸고, 스테이징·활성화가 후보의 마이그레이션 체크섬을 실제 기록 DB와 비교하게 했다(`runtime_migrations.py`).
- [원인·변경·검증](design/remote-exit-hang-118.md).

### 수정 117 — Claude 토큰 자동 갱신, GPT 새 모델 목록, 작업 바로가기 순서 바꾸기

- 원격·위임 Claude 작업은 Windows 공식 Claude 로그인의 접근 토큰을 빌려 쓰는데, 로컬 CLI가 실행되지 않아 토큰이 만료된 채로 남았다
  ("the selected Claude account is unavailable", 사용량 인증 실패). 만료·임박 시 공식 CLI의 내장 `/usage`를 숨겨 실행해 CLI가 스스로 갱신하게 한다.
- 관리 런타임(0.153.4)이 모델 목록을 0.160.0 기준으로 요청해 공식 앱과 같은 모델(gpt-6.1-sol 등)을 받는다(목록 요청에만 적용).
- 작업 바로가기를 끌어서 순서를 바꾼다(`shortcut.reorder`).
- [원인·변경·검증](design/token-models-shortcut-order-117.md).

### 수정 116 — 작업 중 공식 앱 업데이트 적용, 26.930.4958 지원, 관리창 정보 정리

- 실행 중인 Codex가 모두 관리용 사본이면(패키지 신원 없음) 패키지 서비스만 멈추고 바로 등록한다. 원래 공식 앱 창이 열려 있거나
  경로를 읽지 못한 프로세스가 있으면 이전처럼 미룬다. 26.930.4958의 바뀐 바인딩(React `Z5`, 추론 검사기 `kve`)을 추가했다.
- 완전 종료의 정상 종료 대기를 15초에서 60초로 늘렸다(26.930의 종료 정리가 더 오래 걸린다).
- 관리창: 선택 고리를 아바타와 같은 중심으로, 카드·상단은 이름·상태·사용량(주간·5시간 이중 링)·경고 하나만 보이고 나머지는 도움말로 옮겼다.
  SSH 호스트는 카드·상단에서 뺐다(도움말·연결 상세·검색에는 남음).
- [원인·변경·검증](design/update-flow-ui-polish-116.md).

### 수정 115 — 프로필이 공식 Codex 26.930 사용, 공식 앱 업데이트 적용, 관리창 화면 개편

- 공식 앱 업데이트 보류의 원인은 패키지의 자동 시작 서비스 `CodexSandboxService.OpenAI.Codex`였다. Codex 창이 하나도 없을 때만
  `-ForceApplicationShutdown`으로 등록을 마친다(창 확인은 설치 직전에 다시 한다).
- 26.930의 ASAR 무결성 검사를 켠 채로 관리용 사본을 만든다: 사본 `ChatGPT.exe`의 무결성 값만 패치한 아카이브 헤더 해시로 다시 봉인한다.
  26.930의 새 패치 위치(나뉜 주 프로세스 동기화, app-shared 청크, 알림·맥락·선택기 변형)를 추가했다.
- 관리창 왼쪽을 프로필 레일(펼치면 두 줄 프로필 카드) + 전체 높이 작업 바로가기(검색, 두 줄 카드)로 바꾸고, 선택한 프로필 요약을 상단에 둔다.
- [원인·변경·검증](design/desktop-930-update-ui-115.md).

### 수정 114 — Claude Ultracode 선택 메뉴 표시와 백그라운드 워크플로 실행

- 관리용 26.917 데스크톱의 모델·추론 강도 메뉴가 별도 행 생성 함수에서 `ultracode`를 걸러 Ultracode를 고를 수 없던 문제를 고쳤다
  (`desktop_reasoning_ui._power_choices`, 아이콘 표 보강, 업그레이드 v3 사본 `26.917.9434.0-215877556fad4160`).
- Claude 실행기가 승인 창 없는 Ultracode에서 Workflow를 허용하고, 백그라운드 워크플로가 끝나 CLI가 이어 가는 턴의 결과까지 기다린다.
  CLI가 Ultracode를 실제로 적용하지 않으면 알린다. 계정 전환 후 ultracode 복원도 고쳤다.
- [원인·변경·검증](design/claude-ultracode-workflows-114.md).

### 수정 113 — GPT 긴 작업의 Claude 전환, Claude 이미지, 공식 앱 업데이트 보류

- 긴 GPT 작업을 Claude로 이어 갈 때 "이식용 요약 + 최근 기록"까지 거부하던 Claude 전달 조건을 고쳤다. 크기 확인 기준을 재구성과
  맞추고, Claude 검증 요약이 GPT 요약 체크포인트를 지워 요약이 없어지던 경로와, 오래전 중단된 턴 하나가 그 뒤의 모든 요약을
  버리던 경로를 막았다. 첫 전달 상한은 240,000토큰.
- 관리용 Windows 런타임을 디버그 빌드 대신 release 빌드로 배포한다. 1.6 GB 기록에서 Claude 턴 시작 전 대기가 60초에서 9초 수준으로 줄었다.
- Claude 모델 목록이 텍스트 전용으로 알려 이미지 첨부가 막히던 문제를 고쳤다(런타임은 PNG/JPEG를 파일로 전달).
- 위치를 읽지 못한 다른 계정의 프로세스 때문에 공식 앱만 갱신하는 업데이트가 보류되던 문제를 고쳤다.
- [원인·변경·검증](design/claude-long-handoff-images-update-113.md).

### 수정 112 — Claude 사용량·긴 대화 전환, 업데이트 재실행 대기

- 로그인되어도 CLI 첫 실행 화면 때문에 비어 있던 Claude 사용량을 별도로 조회한다.
- 종료 후 패키지 설치 중 다시 실행하면 설치 결과를 확인한 뒤 프로필을 연다.
- 검증된 Linux 런타임의 안전 종료 허용 목록과 SSH 대기 조회 간격을 보완한다.
- 긴 대화의 중복 순회·해시·첨부 준비를 줄인다. [원인·변경·검증 경계](design/claude-usage-startup-update-112.md).

### 수정 111 — Claude SSH·프로젝트 신뢰와 앱 내 업데이트 적용

- 준비된 Claude SSH 런타임을 기본 배포에 반영하고, 프리셋 동기화가 SSH 중계기를 재호출하던 경로를 수정합니다.
- 같은 컴퓨터에서 이미 승인한 프로젝트의 신뢰 설정을 프로필 전환 시 연결합니다. 기존 거부 설정은 유지합니다.
- 보류된 공식 앱 업데이트를 작업 공간에서 적용하며, 파일 사용 중이면 완전 종료 직후 한 번 적용합니다.
- [원인·구현·실제 검증과 제한](design/claude-ssh-trust-update-111.md).

### 수정 110 — 설정 예약과 완전 종료 충돌 수정

- 로컬 자동 재시작 예약이 SSH 종료 요청과 관리 서비스 종료를 막던 경로를 수정합니다.
- 새 서비스는 대기 예약을 정리하고, 이전 서비스는 로컬 창 종료 확인 후 어댑터를 정상 종료·재연결해 SSH 종료를 이어갑니다.
- 실제 이전 서비스 바이너리로 오류 재현과 격리된 서비스 종료를 확인했습니다. 실제 서버 종료와 공식 패키지 등록 완료를 뜻하지는 않습니다.
- [원인·종료 순서·검증 범위](design/full-exit-local-restart-110.md).

### 수정 109 — SSH 종료 대기 복구·완전 종료·공식 앱 업데이트 분리

- 원격 종료 예약만 남아도 로컬 완전 종료를 막던 서비스와 이전 서비스 연결 경로를 수정합니다.
- 검증된 독립 관리용 앱을 유지하면서 공식 Windows 패키지만 업데이트하는 경로를 추가합니다.
- 멈춘 기존 원격 실행 6개는 백업과 프로세스 신원 확인 후 정리했고 잠금 해제를 확인했습니다.
- 최종 관리창 `20261004-053808-002` 적용, 고정 Python 검사 89개·파일 해시 218개 검증.
  공식 앱 `26.930.3930.0`은 Windows 지연 등록 준비 완료이며 조사 시 등록 버전은
  `26.930.2377.0`입니다. 준비와 실제 적용을 구분해 표시합니다.
- [원인·설계·검증 및 남은 경계](design/shutdown-ssh-package-update-109.md).

### 수정 108 — 새 플랜 화면 전환·완전 종료·업데이트 안내 수정

- 새 개인 플랜을 인식하지 못해 ChatGPT에서 Codex로 돌아가지 못하는 관리용 화면을 보정했습니다. 일반 SSH 준비 중 완전 종료 요청을 거절하던 경로와, 이전 서비스의 거절 뒤 관리창만 남는 경로도 수정했습니다.
- 최종 빌드는 `20261004-045528-854`입니다. 고정 빌드 Python 검사 179개, WPF 레이아웃, 실제 ASAR의 플랜 판단 함수 및 고정 파일 217개 해시 검사를 통과했습니다. 다음 실행용으로 게시하며 실행 중인 서비스·프로필은 바꾸지 않습니다. [원인·검증·적용 순서](design/plan-switch-and-full-exit-108.md).
- 첫 SSH 서버가 종료 대기 중이어도 나머지 서버의 종료를 진행합니다. 모든 종료가 확인되기 전에는 재시작하거나 연결 잠금을 해제하지 않습니다.
- 관리창에는 대기 서버를 표시하고, 알려진 원격 종료 대기는 일반 연결 시간 초과와 구분합니다.
- 관련 Python 검사 71개 통과. 실행 중 서비스와 원격 프로세스는 변경하지 않았습니다.
- 마지막 경쟁 조건 수정 후 drain 검사 18개, 고정 후보 핵심 검사 3개 통과. 후보 `20261004-042100-114` 빌드·패키징 완료, 활성화하지 않았습니다.
- [확인한 원인·검증 범위·Claude 및 플랜 표시의 남은 문제](design/ssh-drain-progress-108.md).
- 업데이트 사전 검사의 작업 상태 조회 실패가 관리 중인 Codex를 미관리 실행으로 오인하던 문제를 수정합니다. 프로필별 이유를 펼쳐 볼 수 있게 하며, 작업 진행·호환성 미검증에 따른 차단은 유지합니다. [추가 수정과 검증](design/update-preflight-diagnostics-108.md).
- 중간 후보 `20261004-044251-802`는 업데이트 Python 검사 86개, WPF 레이아웃, 고정 후보 회귀 4개와 파일 해시 검증을 통과했고, 위 최종 빌드로 대체했습니다.

### 수정 106 — 작업 화면 이동 확인·재시작 없는 자동 복구

- 열기 요청 이후 실제 화면 선택을 확인하고 막힌 경우 기존 창에 직접 이동을 한 번 전달합니다.
- 선택 변경 시 취소하며, 요청 수신만으로 성공 처리하거나 프로필을 자동 재시작하지 않습니다.
- 관리 앱 시험 10종과 최종 후보의 작업 전환 검사 29개 통과. 기존 관리 서비스·프로필은 유지합니다.
- [원인 관찰·검증·관리창만 적용하는 방법](design/shortcut-navigation-recovery-106.md).

### 수정 105 — Claude SSH 연결·원격 실행 준비

- 원격 Claude 지원 추가 후에도 남아 있던 로컬 전용 등록·열기 조건을 정리합니다.
- 현재 실행 세대의 SSH 추적 여부를 확인하며 원격 유지보수와 기존 연결 확인을 처리합니다.
- Python 회귀 71개와 관리 앱 자체 검사 10종 통과. 서버 3곳 준비 완료, 실행 중 서비스 교체와 실제 화면 연결은 대기 상태입니다.
- [원인·검증·적용 순서](design/claude-ssh-enrollment-105.md).

### 수정 104 — 계정 이메일 표시·기본 숨김

- 프로필 제목 옆 버튼으로 Codex·Claude 계정 이메일을 한꺼번에 표시하거나 숨깁니다. 이름 아래 한 줄로 표시하고 긴 주소는 툴팁으로 확인합니다.
- 숨김이 기본값이며 다시 실행해도 숨김으로 시작합니다. 이메일은 일반 상태·설정·진단 로그에 저장하지 않고 버튼을 누를 때만 읽습니다.
- 조회 중 숨김과 늦게 도착한 결과, 계정 연결 변경을 처리하며 이메일 조회가 프로필 열기를 막지 않습니다.
- [구현·검증 및 적용 상태](design/profile-email-visibility-104.md).

### 수정 103 — SSH 실행 프리셋·원격 교차 하위 에이전트

- 최종 Linux 릴리스의 기능 감지와 Windows 긴 경로 메모 저장을 보완했습니다. 관리 앱 10종 자체 시험과 실행 프리셋 검사, 양방향 원격 위임 검사를 통과했습니다. 관리 서비스 단위 시험은 Windows 빌드 정책 차단으로 미실행이며 후보는 아직 활성화하지 않았습니다.
- SSH 호스트별 불변 역할 정의와 변경 가능한 작업 선택을 분리하고, 준비된 조합을 다음 응답부터 적용합니다.
- 선택된 Codex·Claude 계정의 접근 토큰을 원격 연결에 한정해 빌리며 인증 파일과 refresh token은 복사하지 않습니다.
- 재연결 전 선택 게시와 leaf 하위 에이전트의 부모 결과 전달을 보강합니다.
- [설계·설치·검증 및 배포 상태](design/ssh-execution-presets-103.md).

### 수정 102 — 작업별 실행 프리셋·Codex↔Claude 하위 에이전트
- 계정별로 여러 조합을 저장하고 작업마다 설정 버전을 고정해 선택합니다. 준비된 조합 변경은 다음 응답부터 적용합니다.
- Codex·Claude 계정을 역할별로 지정하고, Claude의 위임도 네이티브 에이전트 관리자를 통하도록 연결합니다.
- 진행 중 응답·하위 에이전트의 설정과 기존 작업 기록은 유지합니다. SSH 원격 적용은 아직 지원하지 않습니다.
- [구현·검증 기록](design/execution-presets-cross-agents-102.md).

### 수정 101 — Claude 작업 중 모델 전환·대화 유지
- 프로필 기본 모델을 모든 요청에 강제하던 경로를 수정하고, 같은 작업에서 Opus·Sonnet·Fable·Opus 5.5를 선택합니다.
- 같은 계정의 모델·추론 강도 변경은 기존 Claude 세션을 이어 사용하며, UltraCode 선택과 전달을 검증합니다.
- 워크트리별 영속 담당 대화는 기존 기능과 새로 필요한 실행·통합 제어를 구분해 적용안을 작성했습니다. 아직 자동 조율 기능을 구현한 것은 아닙니다.
- [구현·검증 기록](design/claude-model-switching-101.md), [워크트리 적용 검토](design/persistent-worktree-domains.md).

### 수정 100 — 로컬 모델 프로필·최대 컨텍스트·호환 안내
- 로컬 Responses 서버를 주 프로필과 하위 에이전트에 등록하며 키 없는 인증과 실행 위치를 구분합니다.
- 세 Flash 모델의 공식 최대 컨텍스트와 최대 추론 대응을 제공하고 임의 축소를 거절합니다.
- 이전 관리용 앱을 사용하는 안내를 설정 화면에 한 번 표시하며 실제 오류 경고는 유지합니다.
- 실제 서버 접속·최대 길이 처리는 서버 정보를 받은 뒤 검증해야 합니다. [상세 기록](design/local-model-profiles-100.md).

### 수정 99 — Claude 기본 압축·UltraCode·구독 사용량
- Claude 기본 컨텍스트와 자동 압축을 선택할 수 있고, 숫자 직접 지정도 유지합니다.
- UltraCode를 별도 effort로 전달하고 프로필·작업 바로가기에 5시간·주간 사용량을 표시합니다.
  값의 출처·오래됨·미확인을 구분하며 계정이 바뀌면 이전 수치를 제거합니다.
- 사용자 지정 SSH 작업 소속을 다음 안전한 프로필 실행 때 적용하는 일회성 복구를 추가합니다.
- 바로가기의 전체 상태 조회 대기와 화면 크기 변화를 없애고, 공통 저장소에서 버리던 기록 목록 생성을 건너뜁니다.
- 구현과 검증 범위: [상세 기록](design/claude-defaults-usage-99.md).

### 수정 98 — Claude 로그인 프로필, 공통 작업과 전환 캐시
- 공식 Claude Code CLI를 사용하는 프로필을 추가합니다. OAuth 인증은 CLI가 소유하고 기존 GPT/API
  인증과 분리합니다. Claude의 모델·추론·컨텍스트·압축 비율 설정과 공통 스킬 진입점을 제공합니다.
- 공통 작업의 완료 턴을 평문으로 전달하며, 계정별 Claude 세션으로 돌아올 때는 보지 않은 턴만 전달합니다.
  재개 장부는 공통 기록 저장이 확인된 뒤 확정하며, 수정·중단·인증 주체 변경 시 재검증합니다.
- 자동 압축의 검증된 요약을 원본 기록과 함께 보존합니다. 현재 Windows 로컬 실행 범위이며,
  SSH 실행·Codex 전용 도구 변환·실계정 모델 검증을 지원 완료로 간주하지 않습니다.
- 구현 및 검증 범위: `docs/design/claude-code-agent-profiles-98.md`.

### 수정 97 — SSH 런타임 경량화, 서버별 실패 분리, 작업 바로가기 상태
- 수정 96 배포 직후 한 프로필의 SSH가 모든 서버에서 막혔습니다. 원인은 디버그 정보가 든 1.45GB 원격
  런타임, 여러 프로필의 같은 서버 동시 업로드(디스크 부족), 한 서버 실패가 프로필 전체를 막는 구조였습니다.
- 원격 런타임에서 디버그 정보를 빼 328MB로 줄였습니다(gzip 전송 약 140MB). 서버별로 업로드를 한 번만
  하고, 제한 시간은 크기에 비례합니다. 설정 적용 중 한 서버 준비가 실패하면 그 서버만 이전 설정으로 연결하고
  나머지는 새 설정으로 엽니다. 1GiB 넘는 런타임의 '작업 유지 종료'가 항상 거부되던 잠재 버그도 고쳤습니다.
- 작업 바로가기 카드: 계정 사용량 링(가운데 계정 이름), 작업 중·승인 대기·입력 대기 상태, 하위 에이전트
  칩, `⋯` 메뉴. 작업 상태는 로컬 런타임과 SSH 프록시의 알림에서 내용 없이 모읍니다.
- 앱 아이콘 추가, '지금 열린 작업 추가'의 OLE 클립보드 형식 오류 수정.
- 적용 중 발견해 함께 고침: 완전 종료가 닫히는 중인 프로세스와 거부된 원격 종료에 막히던 문제, 꺼져 있는
  SSH 서버가 시작 때 다른 서버 연결과 완전 종료를 막던 문제(접속 불가 서버만 따로 보류).
- 문서: `docs/design/ssh-upload-and-task-shortcuts-97.md`.

### 수정 96 — 잦은 계정·공급자 전환의 캐시 최적화
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
- 설계와 결과: `docs/design/switch-cache-optimization-96.md`(예상 절약, 적용 뒤 확인, 남은 과제),
  `docs/design/cross-provider-context-94.md`(수정 96 절).
- 함께 고친 실사용 문제:
  - 공유 기록 안정성(`docs/design/shared-history-stability-96.md`): 기록 순번 중복으로 작업이 열리지 않던
    문제의 복구 도구와 재발 방지, 작업 삭제 수십 초 지연, 하위 에이전트가 있는 작업의 전송 실패, 대기 중인
    SSH 설정 적용이 완전 종료를 막던 문제, 작업 메모 창 상태의 프로필별 저장.
  - SSH worktree 프로젝트의 작업이 다른 프로젝트로 옮겨 보이던 문제(`ssh-worktree-project-grouping-96.md`).
  - 새로 만든 프로젝트에서 폴더의 예전 작업이 만든 창에만 안 보이던 문제(`new-project-thread-visibility-96.md`).
  - 저장소 경로의 `#` 때문에 내장 플러그인(Chrome 확장 연결, computer use)이 설치되지 않던 문제
    (`chrome-native-host-96.md`).

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
  확인), 큰 문맥 전달 한도, 공급자별 작업 설정의 런타임 저장, 자동 압축 기준을 넘는 복귀의 처리(사용자
  결정).
- Claude 연동: 구독 OAuth 토큰을 꺼내 쓰는 서드파티 플러그인 방식은 약관상 하지 않습니다. 공식 Claude Code
  CLI를 외부 에이전트로 실행하는 설계 초안이 있으며, 공개 여부와 약관 해석을 사용자가 정할 때까지
  커밋하지 않습니다.
- 사용 설정 선택지(사용자 결정): Fast 등급 범위, 자동 압축 기준, 쓰지 않는 플러그인·MCP 정리.
- 수정 97의 나머지: 원격 서버의 쓰지 않는 런타임 자동 정리(현재 바인딩과 실행 중 프로세스 기준).
- 수정 121의 나머지: 문서화되지 않은 Claude CLI 동작(토큰 교체·401 대기·디스크립터 토큰)은 CLI 2.1.282에서만 확인했다.
  CLI가 바뀌면 다시 확인한다. SSH 런타임 데몬의 non-dumpable 처리와 `ptrace_scope` 준비 경고는 하지 않았다.

## 전체 목록

| 수정 | 문서 | 주제 |
|---|---|---|
| 112 | [claude-usage-startup-update-112](design/claude-usage-startup-update-112.md) | Claude 사용량·긴 대화 전환, 업데이트 재실행 대기와 SSH 복구 |
| 106 | [shortcut-navigation-recovery-106](design/shortcut-navigation-recovery-106.md) | 실제 화면 이동 확인, 직접 전달 자동 복구와 취소 |
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
| 96 | [switch-cache-optimization-96](design/switch-cache-optimization-96.md), [shared-history-stability-96](design/shared-history-stability-96.md), [ssh-worktree-project-grouping-96](design/ssh-worktree-project-grouping-96.md), [new-project-thread-visibility-96](design/new-project-thread-visibility-96.md), [chrome-native-host-96](design/chrome-native-host-96.md) | 전환 캐시 절약, 공유 기록 안정성, SSH 프로젝트 분류, 새 프로젝트 표시, Chrome·computer use 플러그인 |
| 97 | [ssh-upload-and-task-shortcuts-97](design/ssh-upload-and-task-shortcuts-97.md) | SSH 런타임 경량화·서버별 실패 분리, 작업 바로가기 상태, 앱 아이콘 |
| 98 | [claude-code-agent-profiles-98](design/claude-code-agent-profiles-98.md) | Claude 로그인 프로필, 공통 대화·스킬, 계정 전환과 압축 |
| 99 | [claude-defaults-usage-99](design/claude-defaults-usage-99.md) | Claude 기본 압축·UltraCode·사용량, 작업 바로가기 전환 |
| 100 | [local-model-profiles-100](design/local-model-profiles-100.md) | 로컬 모델 등록·최대 컨텍스트·호환 안내 |
| 101 | [claude-model-switching-101](design/claude-model-switching-101.md), [persistent-worktree-domains](design/persistent-worktree-domains.md) | Claude 작업 중 모델 전환·대화 유지, 영속 워크트리 담당 대화 검토 |
| 108 | [ssh-drain-progress-108](design/ssh-drain-progress-108.md), [update-preflight-diagnostics-108](design/update-preflight-diagnostics-108.md) | SSH 서버별 종료 대기·업데이트 미관리 실행 오탐과 안내 수정 |
| 109 | [shutdown-ssh-package-update-109](design/shutdown-ssh-package-update-109.md) | 원격 종료 예약과 로컬 종료 분리, 멈춘 SSH 복구, 공식 패키지 지연 등록 |
| 110 | [full-exit-local-restart-110](design/full-exit-local-restart-110.md) | 로컬 설정 예약의 종료 충돌, 이전 어댑터 정리와 SSH 종료 연속 처리 |
| 113 | [claude-long-handoff-images-update-113](design/claude-long-handoff-images-update-113.md) | GPT 긴 작업의 Claude 전환, Claude 이미지, 공식 앱 업데이트 보류 |
| 114 | [claude-ultracode-workflows-114](design/claude-ultracode-workflows-114.md) | Claude Ultracode 선택 메뉴와 백그라운드 워크플로 |
| 115 | [desktop-930-update-ui-115](design/desktop-930-update-ui-115.md) | 프로필의 공식 Codex 26.930 사용, 공식 앱 업데이트 적용, 관리창 화면 개편 |
| 116 | [update-flow-ui-polish-116](design/update-flow-ui-polish-116.md) | 작업 중 공식 앱 업데이트 적용, 26.930.4958 지원, 관리창 정보 정리 |
| 117 | [token-models-shortcut-order-117](design/token-models-shortcut-order-117.md) | Claude 토큰 자동 갱신, GPT 새 모델 목록, 작업 바로가기 순서 |
| 118 | [remote-exit-hang-118](design/remote-exit-hang-118.md) | 원격 Codex 종료 멈춤(sqlx 정리 작업), 완전 종료 대기 선택지, 서버 64스레드 제한·Windows 교차 빌드 |
| 119 | [stability-perf-119](design/stability-perf-119.md), [ssh-worktree-project-grouping-96](design/ssh-worktree-project-grouping-96.md) | Claude·SSH 버그, 관리창 속도, 재발 방지 장치 |
| 120 | [runtime-start-retry-120](design/runtime-start-retry-120.md) | 런타임 시작 실패 재시도, 시작 실패 뒤 오류창, 콘솔 공유 |
| 121 | [claude-login-renewal-121](design/claude-login-renewal-121.md), [llm-usage-removal-121](design/llm-usage-removal-121.md) | SSH Claude 로그인 자동 갱신·이어 쓰기·실행 중 토큰 교체, 계정별 장기 토큰과 토큰 전달 보호, llm-usage 연동 제거와 기존 프로필·SSH home 호환 |
| 122 | (REVISIONS 본문) | SSH 이전 런타임 재실행 방지, SSH 자동 업데이트 기본 켜기와 대기 확인 간격 늘림, 완전 종료 대기를 앱 수에 맞추고 남은 정리 자동 진행 |
| 123 | (REVISIONS 본문) | 작업 바로가기 클릭별 기록과 하루치 로그, 카드·상태 줄 진행 표시, 클릭한 프로필 우선 시작·메모리 부족 시 미리 열기 축소, 마지막 클릭 우선과 프로필별 처리, 앱 파이프 직접 이동·대기 단축, 대기 중 앱 종료 시 한 번 다시 시작 |
| 124 | (REVISIONS 본문) | Claude 사용 한도 멈춤의 구체 오류(`claude_usage_limit`)와 같은 세션 이어 쓰기, 재설정 후 자동 이어하기(예약·확인·재예약·기록, admin 고정 `turn/start`), 모든 계정의 90%·한도·재설정 알림과 카드 강조 |
| 125 | (REVISIONS 본문) | 런타임 핫픽스: 번들 카탈로그 밖 GPT 모델(sol 계열)의 카탈로그 클라이언트 버전 식별, Claude 평문 구간 뒤 GPT 복귀 시 예산 안 문맥 재구성·토큰 재계산, Claude 압축 거부의 실패 표시, 공유 작업 경로 음성 캐시 제거 |
| 126 | (REVISIONS 본문) | 프로필별 Codex 닫기(진행 중 작업 경고, 완전 종료와 같은 종료 요청·남은 프로세스 정리·SSH 원격 종료), 실행 중 계정의 "닫고 제거", 닫기~제거 사이 프로필 실행 보류(`profile.close_begin`/`close_end`) |
