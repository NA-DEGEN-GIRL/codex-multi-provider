# 수정 117 — Claude 토큰 자동 갱신, GPT 새 모델 목록, 작업 바로가기 순서 바꾸기

## 1. SSH·위임 Claude 작업의 로그인 토큰 자동 갱신

### 증상
SSH 프로젝트의 Claude 작업이 `Claude agent: the selected Claude account is unavailable`로 바로 실패했다.
Claude 프로필 카드에는 "Claude 사용량 조회 인증을 확인하지 못했습니다"가 계속 보였다.

### 원인
- 원격 런타임은 Claude 계정 토큰을 매니저에 요청하고(`account/executionPresetAuthTokens/read`), 매니저의 SSH 프록시
  (`ExecutionPresetAuthProxy`)가 Windows에 있는 그 프로필의 공식 Claude 로그인에서 현재 접근 토큰만 빌려 준다(`claude_borrowed_auth`).
  실패하면 -32042로 거절하고, 런타임은 위 오류를 낸다(원격 로그: 거절 직후 실패).
- 빌리는 쪽은 토큰을 갱신하지 않는다. 갱신은 공식 CLI가 로컬에서 실행될 때만 일어나는데, Claude 작업이 모두 SSH에서 돌면 로컬 CLI가 실행되지 않는다.
  세 Claude 프로필의 접근 토큰이 17~20시간 전에 만료된 채였다. 사용량 조회도 같은 토큰을 빌려 실패했다.
- `claude auth status`는 토큰을 갱신하지 않는다(확인). 공식 CLI의 내장 `/usage` 명령을 숨겨 실행하면 CLI가 시작하면서 토큰을 갱신한다(모델 요청 없음, 확인: 8시간 유효).

### 변경
- `claude_borrowed_auth.read_access_token`: 접근 토큰이 만료됐거나 2분 안에 만료되면 공식 CLI로 한 번 갱신한 뒤 다시 읽는다(`_refresh_with_cli`:
  숨겨진 `/usage` 실행). 갱신 토큰은 CLI 밖으로 나오지 않는다. 같은 프로필의 동시 요청은 한 번의 실행을 기다리고, 갱신에 실패하면 30초 동안 다시
  실행하지 않는다. 계정 확인·파일 변경 확인 등 기존 검사는 그대로다.
- SSH Claude 작업, 실행 프리셋의 Claude 위임, 사용량 조회가 모두 이 경로를 쓴다.
- 즉시 조치: 세 Claude 프로필을 같은 방식으로 갱신했다(각 8시간 유효).
- 다음 업데이트 전까지는 별도 도우미(`work/claude-token-keeper.py`, 커밋 안 함)가 10분마다 만료 1시간 이내 토큰을 같은 방식으로 갱신한다.
  3일 뒤 또는 `work/claude-token-keeper.stop` 파일이 생기면 스스로 끝난다.

## 2. GPT 계정에 새 모델(gpt-6.1-sol 등)이 보이지 않음
- 서비스는 모델 목록 요청의 `client_version`보다 최소 버전이 높은 모델을 보내지 않는다. 관리 런타임은 0.153.4로 요청해
  gpt-6-astra, 5.6 계열, 5.5만 받았고, 공식 앱(번들 CLI 0.160.0)은 gpt-6.1-sol·gpt-6-sol·gpt-6-luna도 받는다.
- 공식 0.160.0에 들어 있는 모델 목록(gpt-6.1-sol은 최소 0.153.0)을 이 런타임의 `ModelsResponse`로 그대로 읽을 수 있음을 확인했다(11개 모델).
- `codex-models-manager::client_version_to_whole`: 모델 목록 요청과 그 캐시에만 쓰는 버전을 `max(자기 버전, 0.160.0, CODEX_MANAGER_MODELS_CLIENT_VERSION)`으로
  보낸다. 추론 요청의 버전 정보는 그대로다. 환경 변수는 매니저가 다시 빌드 없이 더 올릴 때 쓴다.
- 실제 응답 요청이 새 모델에서 동작하는지는 프로필을 다시 열어 한 번 사용해 확인해야 한다.
- 런타임을 공식 0.160.0으로 옮기는 정식 업데이트는 별도 작업이다: 0.153.4→0.160.0 upstream 변경 5,294개 파일, 이 패치를 적용하면
  108개 파일 충돌과 upstream에서 삭제된 핵심 파일 3개(`core/src/compact_remote.rs`, `thread-store/src/local/writer_lock.rs` 등).

## 3. 작업 바로가기 끌어서 순서 바꾸기
- 저장 순서: `store.shortcut_reorder`(`shortcut.reorder`: 기준 바로가기 앞/뒤), 서비스 명령 허용 목록에 추가. 프로필 순서(`profile.move`)와 같은 방식.

## 적용 시점
- Windows 런타임 후보 `20261005-223421-1e5de1`: 공통 기록 편집(페이지 방식)·정본 저장소 무인 검증 PASS, Claude 끝단 시험 12개 통과.
  런타임을 활성화하면 다음에 관리창을 열 때 실행 중인 프로필을 작업이 끝난 뒤 다시 열어 새 런타임을 적용한다. 다른 작업이 진행 중이어서,
  활성화는 다음 완전 종료 때 별도 도우미(`work/swap-remote-bundle-on-exit.py`, 커밋 안 함)가 한다.
- Linux 묶음 `0.153.4-managed-e0436ba1928a0e59`(codex `ddfbc527…`): 격리 SIGHUP 종료 검사 통과 후 허용 목록에 추가(`work/validation/release-117`).
  같은 도우미가 완전 종료 뒤, 다음 매니저 릴리스가 이 바이너리를 허용 목록에 갖고 있을 때만 교체한다.
