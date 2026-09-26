# 수정 94: 완전 종료 뒤 남던 프로필 창

## 증상

2026-09-25 완전 종료 뒤 프로필 03의 Codex 프로세스 하나가 계속 남았다. 다시 실행한 관리창은
03을 "시작 안 됨"으로 보였고, 03을 열 때마다 새 프로세스가 남은 프로세스로 넘겨져 창을 찾지
못했다(`25초 안에 Codex 기본 창을 찾지 못했습니다`).

## 원인

- 시작 준비(warmup)가 03의 데스크톱을 띄운 직후, 식별 정보를 `state.json`에 저장하기 전에
  실패했다. `environment` 단계 87 ms 뒤 `prepare_and_spawn`이 `success:false`로 끝났다.
  저장소 잠금 대기(10 s)와는 시간이 맞지 않는다. 같은 시각 02·07도 빠르게 실패했으므로
  관리 서비스 pipe가 바빴던 것으로 본다(확정하지 못함).
- 저장되지 않은 프로세스는 아무 기록에도 없다. 실행이 실패한 뒤 누구도 멈추지 않았다.
- 완전 종료는 관찰된 PID가 있는 프로필에만 `profile.cleanup`을 보냈다. 8개 중 7개만 정리를
  요청했다. 요청했더라도 서비스의 `stop`은 기록된 주 프로세스가 죽었으면 프로필의
  `--user-data-dir`를 쓰는 프로세스를 찾아 끝내는 단계 전에 `already_stopped`로 돌아갔다.

## 변경

### 완전 종료가 기록 없는 프로세스도 찾음

- `MainWindow.cs`: 완전 종료는 `generation`이 있는 모든 프로필(보기 전용 포함)에
  `profile.cleanup`을 보낸다. 관찰된 프로세스가 없는 프로필의 실패는 종료를 막지 않고 경고로
  보인다. 이전 서비스가 PID 없는 정리를 거절하는 경우가 그렇다. 관찰된 프로세스가 있는
  프로필의 실패는 이전처럼 종료를 멈춘다.
- `processes.rs` `stop`: 기록된 주 프로세스가 없거나(null), 죽었거나, PID가 다른 프로세스에
  재사용됐으면 해당 프로필의 관리 복사본(`artifacts/managed-desktop`) 실행 파일이면서
  `--user-data-dir`가 이 프로필 하나뿐인 프로세스를 찾아 끝낸다. 다른 프로필·원래 앱은
  건드리지 않는다.

### 실패한 실행은 자기가 띄운 프로세스를 거둠

- `instances.py` `_show`: 프로세스 생성 뒤 식별 정보 저장까지 예외가 나면
  `process.abort_launch`로 방금 띄운 프로세스 트리만 끝내고 원래 오류를 그대로 보고한다.
- `process.abort_launch`(Rust 서비스): `process-owner.json`에 이번 generation으로 기록된 바로
  그 프로세스(PID·생성 시각·실행 파일·`--user-data-dir`)만 끝낸다. `state.json`이 이미 이
  generation을 가지고 있으면(저장된 실행) 거절한다. `--user-data-dir` 전체 정리는 하지 않는다.
- 실행 단계 기록(`profile-launch.performance.jsonl`)의 실패에 예외 클래스 이름(`error`)을
  남긴다. 내용·경로는 넣지 않는다.

### 바쁜 pipe에 바로 실패하지 않음

- `rust_service.py`: pipe 연결이 바쁨(`ERROR_PIPE_BUSY`, 또는 Python이 EINVAL로 보고)이면 아직
  아무것도 보내지 않았으므로 최대 2 s 다시 연결한다. pipe가 없으면 바로 실패한다.

## 검증

- Rust: 기록된 주 프로세스가 죽었거나 null일 때 `stop`이 같은 프로필의 가짜 데스크톱을 끝내고
  다른 프로필 것은 남기는 시험, `abort_launch`가 다른 PID·생성 시각·generation·저장된
  실행을 거절하고 기록된 프로세스만 끝내는 시험. 관리 서비스 시험 62개 통과.
- Python `tests/test_manager_launch_abort.py`: 저장 단계나 생성 직후 서비스 예외 때 abort 요청이
  프로필·generation·프로세스 식별과 함께 가고 원래 오류가 전파되는지, abort 실패가 원래 오류를
  가리지 않는지, 저장된 실행은 abort하지 않는지. `tests/test_manager_rust_service.py`: 바쁜 pipe는
  보내기 전에만 다시 시도하고 2 s 뒤 멈춘다.
- 전체 Python suite 1,544개 실패 0(기존 SshProxy 오류 1개), Shell 컴파일 경고·오류 0.

## 적용

관리 서비스가 바뀌므로 완전 종료 후 다시 실행해야 한다. 첫 완전 종료가 이전 서비스와 이전
관리창으로 돌면 새 동작은 다음 종료부터 적용된다.

## 남은 문제

- 첫 실패의 정확한 예외는 아직 모른다. 다음 실패부터 `error` 필드로 확인한다.
- 정리는 강제 종료(`TerminateProcess`)다. 저장되지 않은 입력은 사라질 수 있다. 식별이 저장되지
  않은 창에는 사용자가 입력할 시간이 거의 없다.
