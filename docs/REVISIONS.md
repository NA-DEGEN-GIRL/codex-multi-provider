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
