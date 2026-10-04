# 수정 113 — GPT 긴 작업의 Claude 전환, Claude 이미지 입력, 공식 앱 업데이트 보류

## 1. GPT 작업을 Claude 계정으로 이어 갈 때 끝없이 생각 중이다가 실패

### 증상
긴 GPT 작업(실제 사례: 기록 797MB, GPT 압축 70회)에서 Claude 계정을 고르면 약 2분 동안 "생각 중"만 보이다가
`Claude agent: shared history exceeds the handoff budget; create a portable summary or increase the configured budget`로
끝났다. 같은 작업에서 21번 반복됐다.

### 원인
- 새 Claude 세션은 공통 기록을 한 번 글로 받아 시작한다. 기록 재구성기는 예산을 넘으면
  "가장 최근 이식용 요약 + 그 뒤 기록"을 만들고, 그래도 넘으면 "요약 + 들어가는 최근 기록"(`PortableCheckpointWithRecentItems`)을
  만들며 생략 사실을 기록 안내로 남긴다. GPT 압축은 이미 이식용 요약을 남기고 있었다(이 작업의 최근 압축들 모두 `portable.summary` 존재).
- Claude 전달 코드(`tasks/claude_code/context.rs`)가 이 "요약 + 최근 기록" 결과를 요약이 아예 없는 경우(`RecentItemsOnly`)와 똑같이 거부했다.
  긴 GPT 작업은 마지막 요약 뒤에도 기록이 길어 항상 이 경우에 해당한다.
- 재구성 뒤의 최종 크기 확인이 "글자 바이트 ≤ 예산 × 2"였다. 재구성기의 보수적 토큰 추정(영문 3바이트, 그 밖 2바이트당 1토큰)과
  기준이 달라, 예산에 맞춘 결과도 다시 거부할 수 있었다.
- Claude가 만든 검증 요약이 있는 작업에서는 그 요약을 적용하면서 기록의 체크포인트(=GPT 이식용 요약)를 모두 지웠다. 적용 뒤에도
  남은 기록이 예산을 넘으면 요약이 하나도 없는 상태가 되어 거부됐고, GPT로 다시 압축해도 같은 결과가 반복됐다.
- Claude 전달 코드는 끝나지 않은 턴(중단된 턴, 동시에 진행 중인 턴)을 빼면서, **처음 빠지는 항목 뒤의 체크포인트를 모두 버렸다**.
  실제 작업에는 9월 중순에 중단된 턴이 3개 있었고, 그 뒤의 GPT 압축 요약(10월 4일까지)이 전부 버려져 "요약 없음"으로 거부됐다.
  앞의 수정만 적용한 런타임으로 실제 기록 사본을 돌려도 같은 오류가 났다.
- 첫 전달 예산이 1M 모델에서 약 93만 토큰(컨텍스트 − 64,000)이었다. 성공하더라도 전환마다 구독 사용량을 크게 쓴다.

### 변경
- `context.rs`: "요약 + 최근 기록"을 받아들인다. 생략 안내는 `[History note: …]`로 Claude에 전달된다. 이식용 요약이 아예 없을 때만
  거부하며, "Codex 계정에서 이 작업을 한 번 /compact 한 뒤 다시 전환"하라고 안내한다.
- `context.rs`(`unseen_completed`): 끝나지 않은 항목은 빼되, 체크포인트 위치(`items_before`)를 남은 항목 기준으로 다시 세어 유지한다.
  Claude가 이미 본 턴 뒤의 체크포인트만 이전처럼 버린다(본 내용을 다시 보내지 않기 위해).
- 최종 크기 확인을 재구성과 같은 추정(`text_token_count`)으로 바꾸고, 역할 표시·도구 기록 직렬화를 위해 10% 여유를 둔다.
- `portable_context/agent_summary.rs`: Claude 검증 요약은 적용 뒤 남는 기록이 예산에 들어갈 때만 적용한다. 그렇지 않으면 체크포인트를
  유지해 GPT 이식용 요약 경로로 재구성한다. 재구성기·GPT 복귀 경로·Claude 전달 경로 세 곳에 같은 규칙을 적용했다.
- `scripts/manager_core/claude_profiles.py`: 첫 전달 예산 상한을 240,000토큰으로 둔다(`HANDOFF_CAP`). 이후 같은 Claude 세션에는 보지 않은 턴만 전달된다.

### 2분 대기의 원인: 최적화하지 않은 Windows 런타임
- 로그(`~/.codex/logs_2.sqlite`, "Claude shared history loaded and verified")에서 그 턴의 기록 읽기·검증이
  89,854 ms(39,063개 항목)였다. Claude는 별도 프로그램이라 매 턴 공통 기록을 디스크에서 읽어 Claude가 이미 본 턴을 확인한다.
  외부 API(DeepSeek 등)는 메모리에 있는 대화를 그대로 보내므로 이 읽기가 없다. 캐시와는 무관하다.
- 관리용 Windows 런타임은 `cargo build`(opt-level 0, 320 MB `codex.exe`)로 만든 디버그 빌드를 배포하고 있었다.
  Linux 런타임은 처음부터 release 빌드다. JSON 해석·해시처럼 CPU가 하는 일은 디버그 빌드에서 수십 배 느리다.
- `scripts/build-manager-runtime.ps1`은 `--release`로 빌드하고, `stage_manager_runtime.py`는 기본으로
  `work/target-runtime/release`를 스테이징한다(`--profile debug`로 이전 방식 선택 가능). 매니페스트에 `build_profile`을 기록한다.

### SSH 프로젝트
- 같은 수정(중단된 턴 수정 포함)을 담은 Linux 묶음 `0.153.4-managed-62c39321e32ce3be`(codex `1148581b…`)를 빌드 서버에서 만들고
  활성 배포로 교체했다. 중간 묶음 `d305e51ac94deedd`(codex `26715eda…`, 중단된 턴 수정 전)와 그 이전 묶음은
  `work/remote-candidates/`에 보관했다.
- 원격 정상 종료 허용 목록에 넣기 전에 두 바이너리 모두 수정 112와 같은 격리 검사(`audit_drain.py`: 가짜 모델 서버, 실행 중 턴에
  SIGHUP 두 번)를 각자 통과했다. 증거: `work/validation/release-113/drain-audit.json`.

## 2. Claude 모델에 이미지를 넣으면 "이 모델은 이미지 입력을 지원하지 않습니다"

- Claude 연결 런타임은 PNG/JPEG 첨부를 정확한 바이트의 로컬 파일로 저장하고 Claude가 Read 도구로 읽게 전달한다(`tasks/claude_code/images.rs`, 끝단 시험 있음).
- 그런데 관리 앱이 만드는 Claude 모델 목록이 외부 모델 기본값인 `input_modalities: ['text']`를 그대로 써서, 데스크톱이 이미지 첨부를 막았다.
- `claude_profiles.render`에서 Claude 모델의 `input_modalities`를 `['text', 'image']`로 알린다. PNG/JPEG 외 형식은 런타임이 명확한 오류로 거절한다.

## 3. 공식 Codex 앱 업데이트가 "실행 중인 Codex의 위치를 확인하지 못해" 보류

- 업데이트 전 검사는 이름이 `ChatGPT.exe`/`Codex.exe`인 프로세스의 실행 파일 위치를 WMI로 읽는다. 관리자 권한으로 실행된 같은 사용자 프로세스나
  다른 계정(예: 샌드박스 사용자)의 `codex.exe`는 위치가 비어 있어, 공식 앱만 갱신하는 경로도 무조건 보류했다.
- `updates.package_processes`: 위치가 비면 `PROCESS_QUERY_LIMITED_INFORMATION`으로 다시 조회한다. 관리자 권한으로 실행된 같은 사용자
  프로세스는 이 방법으로 위치를 읽을 수 있다.
- `managed_package_update.inspect`: 그래도 알 수 없는 프로세스는 다른 계정의 프로세스이므로 공식 앱만 갱신하는 경로에서는 막지 않고
  `unresolved_processes` 수만 기록한다. 설치 명령은 앱을 강제로 닫지 않으므로(`-ForceApplicationShutdown` 없음), 실제로 사용 중인 패키지는
  Windows가 등록을 미루거나 '사용 중'으로 거절할 뿐이다. 원본 앱 창(`package_in_use`)과 관리 사본 밖 실행(`unmanaged_instance`) 차단은 유지한다.

## 검증
- Claude 끝단 시험(`tests/test_manager_claude_runtime_e2e.py`, 새 런타임의 app-server, 가짜 CLI·응답 서버) 12개 통과. 새 시험:
  GPT 작업을 압축해 이식용 요약을 만든 뒤 예산(8,000)을 넘는 기록을 더 쌓고 Claude로 전환하면, 턴이 완료되고 Claude가 요약·기록 안내·최신 턴을 받는다.
- 중단된 턴 수정 시험: `tasks::claude_code::context` 단위 시험 2개(이른 중단 턴 뒤 요약 유지, 이미 본 턴 뒤 체크포인트는 제외).
  `cargo test -p codex-core --lib -- portable_context tasks::claude_code` 33개 통과.
- 실제 작업 기록 사본(1.6 GB, 284턴, 중단된 턴 3개)에서 가짜 CLI로 Claude 턴 하나를 측정했다:
  디버그 빌드·이전 코드 60.6초 후 실패 → release 빌드·이전 코드 8.6초 후 실패 → release 빌드·최종 코드 14.6초에 완료(요약 + 최근 기록 전달).
- 최종 release 런타임 후보 `20261004-170921-40c6c4`(codex SHA256 `8d74ed2e…`)의 공통 기록 편집(페이지 방식)·정본 저장소 무인 검증 PASS,
  Claude 끝단 시험 12개 통과 후 활성화. 중간 후보 `20261004-130442-9d38fc`(디버그)는 대체됐다.
- Python: Claude 프로필·실행기·원격 시험과 업데이트 시험 통과(UTF-8 모드). 실행기 시험은 UTF-8 모드가 아니면 가짜 CLI가 cp949로 출력해 실패한다(시험 환경 문제).
- 실제 Claude 계정 호출과 실제 공식 앱 설치는 이 환경에서 하지 않았다.
