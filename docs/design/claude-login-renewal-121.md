# 수정 121 — SSH Claude 작업의 로그인 자동 갱신과 계정별 장기 토큰

대상: Windows 로그인 토큰을 빌려 SSH 호스트에서 실행하는 Claude 작업(Claude 프로필의 SSH 작업, SSH 실행 프리셋의 Claude 위임).
같은 수정의 다른 절반은 [llm-usage 연동 정리](llm-usage-removal-121.md)다. 배경은 [수정 103](ssh-execution-presets-103.md)과 [수정 117](token-models-shortcut-order-117.md).

## 1. 사용자가 만난 증상

- 2026-10-08, 한 SSH 호스트에서 같은 Claude 프로필의 작업 6개(긴 오케스트레이터 2개와 그 하위 Codex 에이전트 4개)가 48초 안에 모두 끝났다.
  앱에는 일반 오류 `Claude agent: Claude did not complete this turn…`만 보였다.
- CLI 기록에서는 작업마다 `401 OAuth access token has expired`를 두 번 재시도한 뒤 `authentication_failed`로 끝났다. 401 문장은 commentary로 공통 기록에 남았다.
- 6개 작업이 모두 같은 빌린 토큰을 썼다. Windows 공식 CLI가 만든 접근 토큰은 수명이 정확히 8시간이다. 작업들은 시작할 때 남은 시간이
  약 21분~2시간 33분인 토큰을 받았고, 마지막 갱신 뒤 8시간이 되는 순간 함께 실패했다.
- 실패한 기록은 dirty로 남아, 다음 턴이 같은 Claude 세션을 이어 쓰지 못하고 전체 기록 전달로 새로 시작했다.

## 2. 원인

1. **턴 중 토큰 교체 불가.** 턴 시작 때 빌린 접근 토큰 하나를 `CLAUDE_CODE_OAUTH_TOKEN`으로 CLI에 넘겼다. 이 토큰에는 갱신 토큰이 없어 CLI가 스스로 갱신하지 못하고,
   실행 중인 턴에 새 토큰을 넘길 경로도 없었다.
2. **미리 갱신 불가.** Windows 빌림 경로는 만료 2분 이내일 때만 갱신했고(수정 117), 공식 CLI는 만료 5분 이내일 때만 갱신한다. 긴 턴 전에 8시간짜리 토큰을 새로 받을 방법이 없다.
3. **원인을 알 수 없는 오류.** 러너가 인증 실패를 구분하지 않고 일반 오류로 끝냈다.
4. **이어 쓰기 불가.** 인증 실패로 멈춘 기록은 dirty라 다음 턴에 재개하지 않았다.
5. **동시 만료.** Windows 중개자(`ExecutionPresetAuthProxy`)는 작업자 2개로 같은 계정의 요청을 따로 처리해, 여러 턴이 함께 만료되면 응답 마감을 넘길 수 있었다.

## 3. 변경

### R1 — 구체적인 인증 오류 (`claude_runner.py`)
- 감지: assistant 메시지의 `error == "authentication_failed"`, 또는 `is_error` 결과의 `api_error_status == 401`·"Failed to authenticate." 문장.
- 코드(사용자에게는 "Claude agent: " 뒤 영어 문장):
  - `claude_auth_expired` — 빌린 토큰이 만료됨. API 문장(`OAuth (access )?token has expired`, `api_error_code == token_expired`)을 먼저 보고,
    없을 때만 `expiresAt - 60초` 시계 비교로 판단한다(Windows와 SSH 호스트의 시계 차이 대비).
  - `claude_auth_rejected` — 만료 전 거부. `claude_login_required` — 로컬 프로필의 로그인 거부.
- 다른 API 오류는 "Claude API request failed: …" 알림으로 바꾸고, 그 응답의 0 사용량이 마지막 사용량을 덮지 않는다.
- 같은 수정: 체크포인트 생성 중 `rate_limit_event`가 정의되지 않은 이름(`usage_store`)으로 러너를 끝내던 오류.

### R2 — 한 번 갱신하고 같은 턴 이어 하기
- 런타임은 토큰을 빌려 준 턴에만 hello에 `auth_refresh: 1`을 넣는다.
- 러너는 `claude_auth_expired`이고 빌린 로그인이 있을 때:
  1. 멈춘 CLI에서 열려 있던 도구 호출을 오류 `tool_end`(결과 알 수 없음)로 닫는다.
  2. 알림을 보내고 `{"type":"auth_refresh"}`를 요청한다.
  3. 런타임은 턴 시작과 같은 역할로 자격 증명을 다시 읽어(한 번에 35초 한도) `auth_update`로 답한다.
  4. 러너는 같은 프로필·계정, 이전보다 늦은 만료, 300초 이상 남음을 확인한다. 아니면 첫 요청 35초 뒤(Windows 갱신 재시도 간격 30초 이후) 한 번 더 묻는다. 전체 대기는 90초.
  5. 메모리 안의 CLI 환경만 바꿔 `--resume <같은 세션>`과 고정 이어 하기 문장으로 다시 실행한다. MCP 연결, `run.lock`, 기록 레코드는 그대로다.
- 만료로 인한 재실행은 턴마다 한 번이다. 두 번째 실패는 R1 오류로 끝난다.
- 마지막 요청 뒤 체크포인트가 만료로 실패하면, 그 턴에서 아직 갱신하지 않았을 때 한 번 갱신하고 다시 만든다.
- 이미 끝난 러너에 보내는 승인 응답이 실패해도 턴을 깨지 않고, 러너의 마지막 메시지가 결과를 정한다.
- 권한 확인 창이나 위임 에이전트 응답을 기다리는 동안에도 로그인 요청을 읽고 답한다. 갱신은 읽는 즉시 별도 작업으로 시작하고, 그사이 들어온 다른 러너 메시지는 순서대로 64개까지 보관한다.

### R3 — Windows 빌림 경로 보강 (`claude_borrowed_auth.py`, `execution_preset_auth.py`)
- 빌려 줄 최소 남은 시간을 120초에서 270초로 올렸다. 공식 CLI는 5분 이내에서만 갱신하므로, 300초 이상을 요구하면 갱신 없이 거절만 늘어난다.
- 같은 프로필·계정의 동시 읽기는 진행 중인 한 번의 읽기를 공유한다(갱신 한 번, 실패도 공유). 작업자는 2개에서 8개로 늘렸다.
- 마감: 작업자가 시작한 뒤 25초 또는 도착 뒤 28초 중 먼저. 28초까지 작업자를 못 받은 요청은 읽기를 시작하지 않는다(런타임 요청 한도 30초).

### R4 — 로그인 때문에 멈춘 세션 이어 쓰기
- 런타임 hello는 항상 `auth_resume: 1`을 보낸다.
- 러너는 다음 조건을 모두 만족하면 레코드에 `auth_stop = {turn_id, turn_fingerprints}`를 남긴다(dirty 유지):
  - 코드가 `claude_auth_expired`/`claude_auth_rejected`이다.
  - CLI가 마지막 오류 결과를 쓰고 5초 안에 스스로 끝났다.
  - CLI가 요청을 받았다(사용자 메시지 재생 확인).
  - 중단·프로토콜 오류가 없었다.
- 다음 턴은 표시된 turn이 그대로이고 지문이 모두 맞으며 같은 계정일 때만 같은 세션을 이어 쓴다. 요청 앞에 "이전 요청이 로그인 문제로 멈췄고 백그라운드 작업은 끝났다"는 고정 문장을 붙인다.
- 런타임 `context::resumable`은 멈춘 turn의 마지막 항목이 Claude의 출력일 때만 받아들인다(그 뒤에 들어온 입력은 Claude가 보지 못했으므로 새 세션).

### S1 — 토큰 전달 경로 (SSH 호스트, Linux)
- **러너로:** 런타임은 `CODEX_MANAGER_CLAUDE_AUTH_CHANNEL=stdin`(비밀 아님)으로 러너를 시작한다. 러너가 `auth_channel`을 알리면 hello 전에 `auth_init`으로 자격 증명을 보낸다(70,000바이트 한도).
  환경 변수나 hello, 기록에는 남지 않는다. 런타임↔러너 입출력은 Unix 소켓 쌍이다.
- **CLI로:** 실행마다(첫 실행, 재실행, 체크포인트) `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`로 넘기고, `CLAUDE_CODE_OAUTH_SCOPES=user:inference`로 환경 변수 토큰과 같은 범위로 묶는다.
  CLI의 stdin·stdout·토큰 디스크립터는 모두 소켓 쌍이라 다른 프로세스가 `/proc/<pid>/fd`로 열 수 없다(ENXIO). Windows에서는 계속 `CLAUDE_CODE_OAUTH_TOKEN`을 쓴다.
- **프로세스 보호:**
  - 러너: `PR_SET_DUMPABLE=0`과 core 크기 hard 제한 0. CLI와 그 자식이 이 제한을 물려받는다.
  - SSH 채널을 중계하는 `app-server proxy`: helper가 `CODEX_MANAGER_PRIVATE_PROXY=1`을 주면 새 런타임이 스스로 non-dumpable이 된다.
    helper의 오래된 인스턴스 정리와 기록 복구의 열린 파일 검사는 이 프록시와 러너를 건너뛴다.
  - 런타임 데몬: 준비된 권한 정보에 Claude 계정(주 계정·프리셋 역할)이 있으면 core hard 제한 0으로 시작한다. 데몬 자체는 dumpable로 둔다(유지 보수가 `/proc/<pid>/exe`로 찾는다).
    대가: 그 프로필의 에이전트 명령은 `ulimit -c`를 올릴 수 없고 충돌한 프로그램이 core 파일을 남기지 않는다(`docs/operations/ssh-host-onboarding.md`).
- 로컬 실행: Claude의 Read 거부 규칙에 `work/control-center/credentials/**`를 더했다.

### R6 — 실행 중 토큰 교체
- 빌린 토큰이 있고 hello에 `auth_rotate: 1`이 있을 때만 쓴다. CLI는 `CLAUDE_CODE_OAUTH_401_WAIT_MS=60000`으로 시작해, 거부된 요청을 바로 실패시키지 않고 새 토큰을 기다린다.
- **미리:** CLI 토큰 만료 240초 전에 `auth_refresh`(reason `rotate`)를 보낸다. 턴 중과 CLI 턴 사이 모두 동작하고, 토큰마다 35초 간격으로 최대 4회다.
- **거부 뒤:** CLI가 401/403 `authentication_failed`로 `api_retry`를 알리면 다시 묻는다. 15초 이상 간격으로 CLI 프로세스마다 최대 4회(대기 한 번에 2회)다. 기다리기 시작할 때 알림을 보낸다.
- 새 토큰은 CLI stdin의 `update_environment_variables`로 넣는다(요청 ID `codex-auth-<uuid>`, 로그 없음). 30초 안에 성공 응답이 없으면 그 프로세스의 교체를 멈추고, 재실행이 이미 받은 토큰을 쓴다.
- 호스트가 두 번 모두 쓸 수 있는 토큰을 주지 못하면 CLI를 중단하고, 고정 자리표시 값(`codex-manager-login-stopped`, 토큰 아님)으로 CLI의 60초 대기를 깨운다. 실제 CLI에서 401 뒤 약 37초에 구체 오류로 끝났다.
- 런타임 예산(러너마다): 만료 2회, 전환 재실행 2회, 교체·실시간 전환 32회(15초 간격, 3초 여유), 알 수 없는 사유는 즉시 `available:false`. 미해결 요청은 하나만 둔다.

### R5 — 계정별 장기 토큰 (`claude setup-token`)
- **목적:** SSH Claude 작업이 8시간짜리 빌린 토큰 대신 계정마다 저장한 1년짜리 토큰을 쓴다. 저장하지 않으면 지금처럼 PC 로그인을 빌린다. 이 PC에서 실행하는 Claude 작업에는 영향이 없다.
- **저장(`claude_long_lived_auth.py`, Windows 전용, SSH helper에 포함하지 않음):**
  - 현재 Windows 사용자 DPAPI 블롭 `work/control-center/credentials/claude/<profile>/<credential>.dpapi`. entropy는 고정 접두사와 프로필 ID다.
    블롭은 `{v, profile_id, credential_id, token}`이고 두 ID가 메타데이터와 맞을 때만 쓴다.
  - 프로필의 `claude_long_lived_token` 메타데이터에는 토큰도 해시도 없다. 유효 기간은 사용자가 발급 화면의 값(기본 365일, 1~366일)을 넣는다.
  - 만료 30일 전부터 알림, 계산한 만료 하루 전부터 빌려 주지 않음. 계산한 만료 ±2일 안의 401은 거부가 아니라 만료로 분류한다.
  - 프로필별 자격 증명 잠금을 항상 store 잠금보다 먼저 잡는다. 빌려 주는 쪽은 잠금 없이 읽고, 교체 중 블롭이 사라지면 메타데이터를 한 번 더 읽는다.
  - DPAPI 호출은 모듈 전용 라이브러리 핸들과 한 번만 정한 함수 형식을 써서 여러 작업자에서 동시에 호출해도 안전하다. 복호화 실패는 "읽을 수 없음"으로 표시하고 PC 로그인으로 대신한다.
  - 계정을 목록에서 제거해도 토큰은 남고, 제거 메시지가 그렇게 알린다.
- **관리 명령(서비스 허용 목록, 프로필별 직렬화):**
  - `claude.token.save {profile_id, token, attested, attested_account, minted_on?, validity_days?}` — 토큰을 담는 유일한 요청이다. 서비스 로그·작업 표에 토큰이 남지 않음을 시험한다.
  - `claude.token.remove`, `claude.token.retry`(거부 기록 지우기), `claude.token.issue`(발급 창 열기).
- **저장 조건:**
  - 이 프로필의 PC 로그인이 확인되어야 한다.
  - 사용자가 "이 토큰은 <가린 계정>으로 발급했습니다"를 확인해야 한다. 확인란은 표시된 로그인·토큰 칸이 바뀌거나 저장이 실패하면 해제된다.
    저장 순간의 로그인이 확인한 계정과 다르면 `claude_login_changed`로 거절한다.
  - 형식: `sk-ant-oat…`, 40~4,096자. 공백·줄바꿈·폭 없는 문자는 지운다. 갱신 토큰(`sk-ant-ort…`)과 API 키(`sk-ant-api…`)는 거절한다.
- **발급 창:** `cmd.exe /d /s /k ""<cli>" setup-token"`을 새 콘솔로 연다.
  - 인증 환경 변수를 지우고, 버리는 설정 폴더와 작업 폴더를 쓴다. 파이프를 연결하지 않아 관리 앱은 창 내용을 읽지 않는다.
  - setup-token은 토큰을 보여 주고 0.5초 뒤 끝나므로, 셸을 남겨 CLI가 끝난 뒤에도 창이 열려 있게 한다. CLI 경로에 `%`가 있으면 거절한다.
- **설정 패널(`ClaudeLongLivedTokenPanel.cs`, Claude 로그인·설정 창):**
  - 상태: 없음, 설정됨, 만료 임박, 만료(사용 중지), 만료됨, 거부됨, 다른 계정용, PC 로그인 확인 필요, 읽을 수 없음.
  - 쓰지 못하는 상태에는 대신 쓰는 경로("지금은 Windows 로그인 토큰 사용" / "Windows 로그인도 필요")를 붙인다.
  - 이 계정을 쓰는 준비된 SSH 연결 수, 발급 안내, 해지 안내, 위협 안내를 보여 준다. 카드와 확인 목록에는 "토큰 확인" 알림이 뜬다.
  - 저장 뒤 클립보드에 남은 토큰을 지운다.
- **빌려 주기(SSH 중개자):**
  - 런타임 initialize의 `executionPresetCredentialSources`가 2이고, 그 역할의 준비된 권한 정보에 `credential_sources: 1`이 있을 때만 장기 토큰을 빌려 준다. 아니면 응답은 기존 형태 그대로다.
  - 응답에 `credentialSource`(`windowsLogin`/`longLivedToken`)와, 장기 토큰이면 `credentialId`를 붙인다.
  - 장기 토큰도 PC 로그인이 같은 계정으로 확인된 동안만 쓴다. 확인되지 않으면 -32042와 `claude_pc_login_required`를 돌려준다.
  - 장기 토큰은 미리 교체하지 않고, 실행 중인 CLI의 stdin에 넣지 않는다. 다음 실행이 디스크립터로 받는다.
- **거부 처리:**
  - 러너가 `auth_rejected {credential_id}`(와 `done.error.credential_id`)를 보내면, 런타임이 턴마다 한 번 `rejectedCredentialId`와 `reportOnly: true`로 알린다.
    중개자는 이 연결이 빌려 준 ID만 기록하고, 토큰 없이 `claude_long_lived_recorded`로 답한다.
  - 그 턴의 이후 읽기는 그 ID를 빼기만 하고 다시 기록하지 않는다. 그래서 사용자가 "다시 시도"를 누른 뒤 아직 실행 중인 턴이 거부를 되살리지 않는다.
  - 대신 PC 로그인을 빌려 준다. CLI가 기다리는 중이면 실시간으로 전환하고(PC 로그인 토큰만 stdin으로 넣는다), 아니면 전환 재실행 한 번으로 이어 한다.
  - 쓸 수 있는 것이 없으면 `claude_long_lived_rejected`/`claude_long_lived_expired`로 거절하고, 사용자에게 교체·삭제 또는 Windows 로그인을 안내한다.
  - 중개자 내부 예외는 그 연결의 읽기를 멈추지 않고 일반 거절이 된다.
- **SSH 준비:** `render_for_host`가 Claude 계정 권한 정보에 `credential_sources: 1`을 넣는다. 런타임 실행 파일에 `claude_pc_login_required` 문자열이 있으면
  바인딩에 표시용 `claude_credential_sources: 1`을 남긴다(패널의 SSH 줄). 중개자는 표시값이 아니라 initialize와 권한 정보를 매번 직접 확인한다.

#### 위협 모델과 남은 노출
- **Windows:** DPAPI는 다른 Windows 사용자와 관리 폴더의 오프라인 복사본으로부터 보호한다. entropy는 공개 값이라, 같은 Windows 사용자로 실행되는 코드로부터는 보호하지 못한다.
  저장한 API 키와 공식 CLI 로그인 파일과 같은 수준이다.
- **SSH 호스트:** 토큰은 파일·환경 변수·명령 인수·기록·로그에 남지 않고 소켓으로만 실행 중인 Claude에 전달된다. 그래도 같은 Linux 사용자의 프로세스(에이전트가 실행한 도구 포함)가
  턴 중 토큰을 읽을 수 있는 경로가 남는다.
  - SSH 연결이 시작되는 짧은 구간.
  - `ptrace_scope=0`인 호스트에서의 CLI·데몬 메모리.
  - 패널이 이 사실을 그대로 경고한다.
- **노출 기간:** 빌린 토큰은 최대 8시간, 장기 토큰은 최대 1년이다. 범위는 추론 전용(`user:inference`)이다.
- **해지:** "장기 토큰 삭제"는 이 PC에서만 지운다. 공식 문서에 setup-token 해지 방법이 없고, claude.ai 설정의 Claude Code 항목에서 해지할 수 있다는 것은 사용자 보고뿐이다.
  패널은 이를 사용자 보고로 안내한다. 실행 중인 SSH 작업은 끝날 때까지 기존 토큰을 쓴다.
- **계정 확인:** 추론 전용 토큰으로는 계정을 확인할 수 없다. 발급 계정은 사용자의 확인에 의존한다.

## 4. 기능 협상과 버전 조합

| 신호 | 보내는 쪽 | 없을 때 |
|---|---|---|
| hello `auth_refresh: 1` | 런타임(토큰을 빌려 준 턴) | 러너가 갱신하지 않고 R1 오류만 |
| hello `auth_resume: 1` | 런타임 | `auth_stop`을 쓰지 않고 dirty 기록은 새 세션 |
| hello `auth_rotate: 1` | 런타임 | 교체·401 대기 없음 |
| hello `auth_sources: 1` | 런타임 | 러너가 `auth_rejected`를 보내지 않음(그런 런타임은 장기 토큰도 받지 않음) |
| `auth_channel` 응답 | 러너 | 이전 helper는 환경 변수에 자격 증명이 없어 거절하고, 런타임이 `CODEX_MANAGER_CLAUDE_AUTH`로 한 번 다시 시작 |
| initialize `executionPresetCredentialSources: 2` | 런타임 | 중개자가 장기 토큰을 빌려 주지 않음 |
| 권한 정보 `credential_sources: 1` | 수정 121 SSH 준비 | 중개자가 장기 토큰을 빌려 주지 않음 |

확인한 조합(실제 app-server·중개자, SSH식 파이프의 가짜 CLI):

| 런타임 | SSH helper(러너) | 결과 |
|---|---|---|
| 수정 121 | 수정 121 | 장기 토큰 빌림 → 거부 → 한 번 알림 → 같은 턴이 PC 로그인으로 이어짐 |
| 수정 121 | 수정 121 중간본(S1+R6) | 네 키 자격 증명, 정상 |
| 수정 121 | S1 이전 | stdin 채널 거절 → 환경 변수로 한 번 다시 시작, 정상 |
| 수정 121 중간본(capability 1, 배포 안 함) | 수정 121 | PC 로그인만 |
| 현재 배포 런타임(트리 `99d6d6c7…`) | 수정 121 | 환경 변수로 받고 CLI에는 디스크립터. 갱신·교체·이어 쓰기·장기 토큰 없음 |

- 수정 120 helper는 시험한 S1 이전 helper와 같은 방식으로 다시 시작된다(코드 확인). 이전 러너라 갱신·이어 쓰기는 없다.

- 관리 앱만 새것이면 R3만 적용되고 응답 형태는 그대로다. 이전 관리 앱의 중개자는 initialize 값을 모르므로 장기 토큰을 빌려 주지 않는다.
- 러너·`claude_auth`·`claude_remote`는 하나의 helper 묶음으로 설치되어 서로 다른 판이 섞이지 않는다.

## 5. 적용 순서

이 수정은 런타임 패치와 Windows·SSH 양쪽 helper를 함께 바꾼다. 각 단계는 앞 표의 조합으로 안전하게 동작하므로, 중간에 멈춰도 지금보다 나빠지지 않는다.

1. **런타임 패치:** 결과 트리 `0511f3be…`, 패치 SHA256 `e224215e…`, 420개 파일. `restore_runtime.py`가 빌드 서버에서 같은 트리를 재현했다.
2. **Linux 번들:** 빌드 서버에서 `package_runtime.py --build`.
   - 격리 SIGHUP 종료 감사를 통과한 `codex` 지문을 `DRAIN_AUDITED_SHA256`에 추가한다.
   - 번들은 `work/remote-candidates/`에 두고 완전 종료 뒤 `artifacts/remote/`로 바꾼다.
3. **Windows 런타임:** 빌드 서버 교차 빌드(CRLF 체크아웃) 또는 이 PC 릴리스 빌드.
   - `stage_manager_runtime.py`로 후보를 만들고, 공유 편집·공통 저장소 무인 검증과 마이그레이션 지문 비교를 통과해야 한다.
4. **관리 앱:** `build-manager.ps1`(사용자 승인). 장기 토큰 패널, 서비스 허용 목록, 중개자·러너·SSH helper가 들어간다.
5. **완전 종료 뒤 다시 열기:** 새 관리 앱과 Windows 런타임을 활성화한다. R3과 Windows 쪽 장기 토큰 저장은 이때부터 동작한다.
6. **SSH:** SSH 업데이트로 새 번들을 적용하면 준비가 다시 실행되어 새 helper와 `credential_sources: 1` 권한 정보가 설치된다.
   - 그렇지 않은 Claude 바인딩은 직접 다시 준비한다.
   - Claude 로그인·설정 창의 SSH 줄("SSH 연결 N곳에서 사용합니다")로 확인한다.
7. **선택:** Claude 프로필마다 "발급 창 열기"로 장기 토큰을 발급해 저장한다. 계정마다 한 번이며, 1년 뒤 같은 절차로 교체한다.

## 6. 시험과 근거

- **Python(Windows) 전체(`test_launchers` 제외):** 2,375개 실행, 실패 0, 건너뜀 74. 알려진 `test_manager_ssh_pump` setUpClass 오류 1건.
  - Linux에서는 러너·원격·실행기·기록 복구·유지 보수·중개자·장기 토큰 모듈 206개를 실행했다. 실제 디스크립터·소켓·`/proc` 경로를 쓴다.
  - 새 시험은 고친 코드를 되돌리면 실패함을 확인했다. 토큰은 모두 `dummy-token-N` 값이다.
  - 출력·오류·예외·기록·`state.json`·서비스 로그에 토큰이 없는지 함께 확인한다.
- **Rust(빌드 서버, 공유 잠금·64스레드 제한):**
  - codex-core lib 2,570개 통과, 4개 실패. 실패 4개(`multi_agents` 2, `environment_selection`·`turn_tests`)는 현재 배포 트리에서도 실패하고 이 변경과 관계없다.
  - Claude 작업 시험 39개는 다섯 번 반복해 모두 통과했다. `claude_code` 스위트 5개도 통과했다. 스위트는 `RUST_MIN_STACK=16777216`이 필요하며, 배포 트리도 같다.
  - protocol lib 298, app-server `execution_preset_auth` 6, `v2::initialize` 7, transport lib 149(파싱 오류 로그에 토큰 없음). clippy는 새 경고가 없다.
  - 시험한 트리 `10ee6afa…`와 최종 트리 `0511f3be…`는 런타임 README 한 문단만 다르다.
- **관리 서비스:** `cargo test` 통과. 장기 토큰 명령의 토큰이 `rust-service.jsonl`과 작업 표에 남지 않는다. 시간 시험은 측정 전에 백엔드를 한 번 깨워 6/6 통과했다.
- **Shell:** 임시 폴더 빌드에서 경고 0, 오류 0. 패널 자체 시험 항목은 추가했지만 시험 창을 띄우므로 실행하지 않았다.
- **가짜 API 탐침(Linux CLI 2.1.282, 로컬 가짜 API, 더미 토큰, 격리 home):**
  - CLI는 SessionStart 훅 전에 디스크립터 토큰을 읽어 비운다. Bash·훅·MCP 자식 환경에는 토큰도 디스크립터 변수도 없다. `/proc/<cli>/environ`에도 토큰이 없다.
  - stdin으로 넣은 토큰이 디스크립터 토큰을 대신한다. 401 뒤 새 토큰을 넣으면 같은 턴이 약 0.6초 뒤 회복했다. `--resume` 재실행도 자기 디스크립터로 동작했다.
  - 실제 `claude_remote.py`를 런타임처럼 실행한 경우: 한 턴·한 프로세스에서 `A 200, B 200, B 401, C 200` 뒤 commit했다(미리 교체 A→B, 거부 뒤 교체 B→C).
  - 소켓 쌍은 `/proc/<pid>/fd`로 열면 ENXIO다. CLI core 제한은 0/0이었고, 형제 프로세스가 올리지 못했다. 러너의 `/proc` 파일은 부모도 읽지 못한다.
  - 장기 토큰 거부 → 실시간 전환(CLI 1개)과 재실행 전환(CLI 2개) 모두 성공했다. 로그인이 없으면 401 뒤 36.6초에 구체 오류로 끝났다.
  - 어느 경우에도 러너 출력·stderr·파일에 토큰이 없었다.
- **Windows:** 발급 창 시험은 실제 `cmd.exe`를 창 없이 실행한다. 공백·`(x86)`·`&`가 든 경로에서 CLI가 끝난 뒤에도 프롬프트가 남는지 확인한다.
  DPAPI는 8개 스레드 동시 시험(이전 코드에서 실패 확인)과 실제 왕복 시험을 한다.
- **검토:** 보안·수명 주기·호환 검토의 지적(열린 도구 호출, 체크포인트 만료, 시계 차이, 갱신 간격, 실시간 전환 예산, 토큰 파이프 노출, 강제 core, 거부 재기록,
  발급 창 닫힘, 계정 확인란, DPAPI 동시 호출, 확인 창 중 갱신 지연 등)을 모두 반영했다.

## 7. 남은 과제

- **문서화되지 않은 CLI 동작에 의존한다:** `update_environment_variables`, `CLAUDE_CODE_OAUTH_401_WAIT_MS`, 소켓 디스크립터 토큰, `CLAUDE_CODE_OAUTH_SCOPES`.
  - CLI 2.1.282에서만 확인했다. SSH 바인딩은 CLI 버전이 바뀌면 `cli_changed`로 다시 준비를 요구하므로, 그때 탐침을 다시 돌린다.
  - 재실행·이어 쓰기는 CLI가 첫 API 요청 전에 요청을 기록에 쓴다고 가정한다.
- **만료 전 폐기:** Windows 갱신이 이전 접근 토큰을 만료 전에 폐기하면 `claude_auth_rejected`로 분류되어 자동 갱신하지 않는다. 다음 턴의 이어 쓰기(R4)만 동작한다.
- **동시 갱신:** 갱신 잠금·재시도 간격은 SSH 연결(프로세스)마다다. 서로 다른 연결은 같은 로그인을 동시에 갱신할 수 있다.
- **SSH 호스트 노출:**
  - 런타임 데몬은 유지 보수 때문에 dumpable이다.
  - `ptrace_scope` 준비 경고는 만들지 않았다.
  - core 제한은 장기 토큰을 저장하지 않은 Claude 계정 프로필에도 적용된다. 저장한 프로필로만 좁히려면 중개자·준비 변경이 필요하다.
- **이어 쓰기 한계:** 멈춘 turn의 편집은 감지하지 못한다(되돌리기는 감지). 강제 종료, 갱신 중 중단, Claude 출력 전 멈춤은 새 세션으로 시작한다.
- **실제 환경 확인:** 실제 토큰·실제 SSH 작업 확인과 Shell 자체 시험은 배포 뒤 사용자 확인이 필요하다. 사고 때 dirty로 남은 기록 6개는 이전 러너가 표시를 남기지 않아 새 세션으로 시작한다.
- **시간 시험:** 부하 중 시간 시험(warmup gate, `permission_selection`, `multi_agent_v2_wait_agent_uses_configured_default_timeout`)이 가끔 실패한다. 이 변경 밖의 파일이며, 다시 실행하면 통과한다.
