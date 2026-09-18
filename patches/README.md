# Rust 런타임 복원

`codex-0.153.4-cross-provider.patch`는 공식 OpenAI Codex 기반 커밋부터 현재 작업 공간용 수정까지 포함하는 **누적 패치**입니다. `runtime-source.json`에 기반 커밋, 패치 SHA256, 적용 후 Git 트리 ID를 기록합니다. 외부 에이전트, 공통 기록, 프로젝트 메타데이터, 관리 실행 수명과 프로토콜 스키마 변경을 포함합니다.

공식 소스를 `upstream/`에 준비한 뒤 실행합니다. 복원 대상은 존재하지 않는 경로여야 합니다.

```powershell
python scripts/restore_runtime.py --source upstream --destination runtime
```

복원기는 패치 해시를 확인하고 기반 커밋의 별도 worktree를 만든 뒤 패치를 적용합니다. 적용 결과의 트리 ID가 manifest와 일치해야 성공합니다. 기존 작업 폴더·현재 인덱스·로그인 설정은 변경하지 않습니다. 실패한 새 worktree는 원인 확인을 위해 남깁니다.

바이너리 스키마 두 개도 패치에 포함합니다. 임시 `*.snap.new`와 `*.pending-snap`은 시험 부산물이므로 제외합니다. 패치 복원 성공은 Rust 전체 회귀 시험의 통과를 의미하지 않습니다.

빌드에는 별도의 Rust/MSVC 및 V8 의존성이 필요합니다. `scripts/build-env.ps1`이 요구하는 공식 MSVC 도우미와 검증된 rusty-v8 아카이브·바인딩을 로컬에 준비해야 합니다. 빌드 산출물과 다운로드한 의존성은 Git에 포함하지 않습니다. 자세한 명령은 [기술 문서](../docs/technical-reference.md)를 참고하세요.
