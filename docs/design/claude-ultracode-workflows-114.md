# 수정 114 — Claude Ultracode 선택 메뉴 표시와 백그라운드 워크플로 실행

## 증상
Claude 프로필에서 추론 강도 "Ultracode"를 고를 수 없었다. 9월 28일 이후 기록된 Claude 턴은 모두 max/xhigh/high였고,
런타임·실행기·CLI 어디에도 ultracode가 도착한 적이 없다.

## 원인
1. **데스크톱 메뉴(주원인).** 모든 프로필은 관리용 26.917 사본(`26.917.9434.0-1c21c2fbbeb537bb`)으로 실행된다(설치된 26.930은
   ASAR 무결성 보호 때문에 관리용 사본을 만들 수 없어 보류). 이 사본에는 Ultracode 패치 3개(선택기 필터, 입력창 목록·선택 유지,
   표시 이름)가 들어 있었지만, 실제 화면의 모델·추론 강도 메뉴는 별도 함수(`npa`, app-initial)가 행을 만들고 이 함수가
   기본 검사기(`vw`)로 `ultracode`를 걸러냈다. 패치된 목록은 쓰이지 않는 옛 메뉴 분기와 단축키에만 연결돼 있었다.
2. **아이콘 표.** 입력창의 추론 강도 아이콘 표(`Kit`)에 `ultracode`가 없어, 선택되면 작은 입력창에서 정의되지 않은 아이콘을 그릴 수 있었다.
3. **실행기.** Ultracode는 Workflow 도구로 일한다(Claude CLI 2.1.282 코드 분석).
   - Workflow의 권한 검사는 항상 "확인 요청"이고, `--permission-prompts none`(Codex 승인 정책 `never`)에서는 모든 확인 요청이 거부된다.
   - Workflow는 백그라운드로 실행되고 즉시 반환한다. CLI는 첫 `result`를 바로 내보내고, 워크플로가 끝나면 스스로 다음 턴을 시작한다.
     실행기는 첫 `result`에서 입력을 닫고 5초 뒤 프로세스를 종료해 워크플로를 잃었다.
4. **계정 전환 복원.** 다른 계정으로 전환했다 Claude로 돌아올 때 마지막 추론 강도를 복원하는 목록(`serve_ledger.EFFORTS`)에 ultracode가 없었다.

설정 문제가 아니었다. Claude 프로필의 config.toml, 모델 목록, 바인딩은 모두 ultracode를 알리고 있었다.

## 변경
- `scripts/manager_core/desktop_reasoning_ui.py`
  - `_power_choices`: 메뉴 행 생성 함수의 검사기에 `ultracode`만 추가로 허용한다. 입력 모델 목록은 이미 패치된 선택기 필터
    (사용자 정의 공급자가 알린 경우에만 ultracode 통과)를 거치므로 GPT 목록은 바뀌지 않는다. 정확히 한 곳이 아니면 실패한다.
  - `patch_composer`: 아이콘 표에 `ultracode`를 xhigh 아이콘으로 추가한다. 형태가 다르면 실패한다.
  - `patch`(새 설치 경로)와 `upgrade_managed`(관리용 사본 경로) 모두 적용한다.
- `scripts/manager_core/desktop_managed_upgrade.py`: 업그레이드 종류 `managed-reasoning-and-plan-ui-v3`. 같은 감사 기준 사본
  `26.917.9434.0-4b9ccd6b8c213650`에서 새 사본 `26.917.9434.0-215877556fad4160`을 게시했다(`work/desktop-upgrade-114.json`).
  기존 사본은 그대로 두며, 이전 릴리스는 자기 식별값에 맞는 이전 사본을 계속 쓴다.
- `scripts/manager_core/claude_runner.py`
  - Ultracode이고 승인 창이 없을 때(`prompts == 'none'`)만 `--settings`에 `permissions.allow: ["Workflow"]`를 넣는다.
    워크플로 안의 도구 호출은 세션 권한 검사를 그대로 받는다. 승인 창이 있으면(`host`) 기존처럼 Codex 승인 요청으로 묻는다.
  - `system/background_tasks_changed`(살아 있는 백그라운드 작업 전체 목록)로 워크플로·백그라운드 에이전트를 추적한다.
    살아 있는 작업이 있으면 `result`가 와도 기다리고, CLI가 시작하는 후속 턴의 `result`를 최종 결과로 쓴다. 이미 보고가 대기 중인
    경우를 위해 백그라운드 작업이 있었던 턴만 결과 뒤 3초 동안 다음 턴 시작을 기다린다. 다른 턴에는 지연이 없다.
  - 여러 `result`의 사용량은 합산하고, 세션 누적 비용은 마지막 값을 쓴다.
  - Ultracode일 때 `get_settings` 제어 요청으로 CLI가 실제 적용한 설정을 확인하고, `applied.ultracode`가 false이면
    (예: 동적 워크플로가 꺼진 계정) 알림을 보낸다. 턴은 xhigh로 계속된다.
- `scripts/manager_core/claude_protocol.py`: 워크플로 시작(`task_started`, `local_workflow`)과 백그라운드 작업 완료(`task_notification`)를 알림으로 표시한다.
- `scripts/manager_core/serve_ledger.py`: `EFFORTS`에 `ultracode` 추가. 복원은 같은 공급자·모델의 기록에만 적용된다.
- SSH 원격 Claude 작업은 같은 실행기 코드를 매니저가 올려 보내 쓰므로 Linux 런타임 재빌드는 필요 없다.

## 검증
- 실제 26.917 기준 사본의 JS에 새 패치를 적용한 결과는 현재 사용 중인 사본과 의도한 두 곳(19바이트, 13바이트)만 다르고,
  `node --check`를 통과했다. 번들의 실제 `vw`/`npa`를 Node로 실행하면 Claude 모델은 `[low, medium, high, xhigh, max, ultracode]`,
  GPT 모델은 기존과 같다.
- 작업용 루트에서 업그레이드를 먼저 시험한 뒤 실제 게시했다(같은 해시). 새 매니저 릴리스 `20261004-181656-508`의 식별값으로
  대체 사본 검색이 새 사본을 찾는다.
- Python 시험 2,093개 통과(새 시험: 메뉴 행, 아이콘, 형태 불일치 거부, Workflow 사전 허용 조건, 백그라운드 워크플로 대기 두 경우,
  Ultracode 미적용 알림, 계정 전환 후 ultracode 복원). Claude 끝단 시험 12개 통과. 매니저 자체 시험 통과.
- 실제 Claude 계정으로 워크플로를 실행하지는 않았다(구독 사용량 소모). CLI 동작은 2.1.282 코드 분석에 근거한다.
