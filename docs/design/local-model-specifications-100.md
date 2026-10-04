# 수정 100 — 로컬 모델의 공식 사양 프리셋

확인일: **2026-09-29**. 아래 정보는 공식 모델 저장소와 추론 엔진 문서를 읽어
확인했다. 모델 추론 요청이나 실행 중인 서버 변경 없이 조사했다.

이 문서와 `scripts/manager_core/local_model_presets.py`는 체크포인트 사양만 제공한다.
사용자의 base URL, 프로토콜, 실제 served model ID, 양자화 버전, 엔진 빌드,
서버 컨텍스트 설정은 아직 별도 확인 대상이다. 이름이 비슷한 이전 모델이나
동일 계열의 다른 모델로 자동 대체하지 않는다.

## 데이터와 조회 API

`list_presets()`는 Qwen, DeepSeek, GLM 순서의 일반 딕셔너리 목록을 반환한다.
`get_preset(preset_id)`는 아래의 정확한 안정 ID 하나만 받으며, 다른 값이나 타입에는
입력 내용을 되풀이하지 않는 `ValueError`를 발생시킨다. 표시 별칭과 공식 체크포인트 ID는
조회 키가 아니다. 모든 반환값은 깊은 복사이므로 호출자가 중첩 항목을 바꿔도 이후 조회는
영향받지 않는다. import 및 조회에 네트워크, 서버 탐색, 환경별 용량 계산을 사용하지 않는다.

| 필드 | 의미 |
| --- | --- |
| `id` | `qwen3.8-flash-next`, `deepseek-v4.1-flash`, `glm-5.3-flash` |
| `display_name`, `aliases` | 표시 이름과 표시 전용 별칭 |
| `model_id` | 확인한 공식 체크포인트 ID. 서버가 공개하는 별칭으로 추정하지 않음 |
| `verified_at`, `sources` | 확인일과 정확한 일차 출처 URL |
| `native_context_tokens` | 공개 체크포인트의 기본 컨텍스트 길이. 학습·설정에 이미 포함된 확장도 포함 |
| `max_context_tokens` | 공식 문서에서 확인한 최대 컨텍스트. 실제 서버 지원을 증명하지 않음 |
| `context_extension_required` | 기본값에서 위 최대 길이까지 추가 확장이 필요한지 여부 |
| `context_extension` | YaRN 방식·배율·원래 위치 길이·체크포인트 적용 여부. 확인한 추가 설정이 없으면 `None` |
| `default_reasoning_effort` | 앱의 논리적 기본값 `max` |
| `native_reasoning_effort` | 해당 모델에 실제로 요청할 최대 강도 값 |
| `native_reasoning_levels` | 지원 문자열 목록 또는 수치 범위. 일반 OpenAI enum으로 추정하지 않음 |
| `max_output_tokens` | 확인된 하드 출력 한도. 세 모델 모두 미확인이므로 `None` |
| `output_recommendation` | 모델 카드의 권장 생성 예산. 출력 한도와 별개이며 없으면 `None` |
| `engine_support`, `caveats` | 확인한 엔진·파서와 버전/템플릿 차이 |

프리셋은 하드웨어나 서버 응답에 따라 컨텍스트를 자동으로 낮추지 않는다. 이후 연결 계층은
요청 사양과 실제 서버의 허용 길이를 구분해야 한다. `max_output_tokens=None`은 무제한을
뜻하지 않는다. 서버의 출력 예산과 입력·생성 전체의 컨텍스트 제약은 연결 후 확인해야 한다.

## Qwen3.8-Flash-Next

- 정확한 ID: `Qwen/Qwen3.8-Flash-Next`.
- 기본 컨텍스트 **262,144**, 추가 YaRN 설정으로 최대 **1,000,000**.
  공식 예시는 factor 4.0, original_max_position_embeddings 262144와 서버의
  max-model-len/context-length 1000000을 함께 설정한다. 1,048,576으로 임의 반올림하지 않는다.
- 논리적 `max`는 실제 `xhigh`로 대응한다. 현재 템플릿 지원값은 `low`, `medium`,
  `xhigh`이며, `max`를 그대로 전달하면 거부한다. `enable_thinking=false`를 지원한다.
- 1M 컨텍스트에서 별도 예산을 지원하는 엔진에 추론 **262,144**, 최종 응답 **131,072**를
  권장한다. 이것은 출력 하드 한도가 아니다.
- vLLM recipe는 0.29.0+와 전용 `qwen38-flash-next` Docker 이미지를 안내한다.
  도구 파서는 `qwen3_coder`, 추론 파서는 `qwen3`이며 자동 도구 선택을 활성화한다.
  일반 pip 버전 번호만 보고 호환된다고 판단하지 않는다.
- 일부 SGLang 설명의 항상-thinking 주장과 달리 현재 공식 템플릿은 끄기 분기를 갖는다.
  실제 배포된 체크포인트·템플릿·엔진을 기준으로 기능을 확인한다. 호스팅 상품
  `Qwen3.8-Flash`와 다른 크기의 Qwen3.8 체크포인트는 이 ID와 구분한다.

출처: [공식 모델 카드](https://huggingface.co/Qwen/Qwen3.8-Flash-Next),
[공식 chat template](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/chat_template.jinja),
[vLLM recipe](https://recipes.vllm.ai/Qwen/Qwen3.8-Flash-Next).

## DeepSeek-V4.1-Flash

- 정확한 ID: `deepseek-ai/DeepSeek-V4.1-Flash`.
- 공개 설정의 컨텍스트는 **1,048,576**이다. 설정 자체에 original_max_position_embeddings
  **65,536**, YaRN factor **16**이 있으며 모델 카드도 학습 중 1M 확장을 설명한다.
  프리셋의 기본값은 이 공개 체크포인트 길이이고, 사용자가 factor 16을 다시 적용하지 않는다.
- 논리적 `max`는 수치 **100**에 대응한다. 실제 연속 강도는 정수 **1–100**이다.
  추론을 켜기만 하는 것과 최대 강도를 요청하는 것은 다르다.
- 공식 참조 인코더는 `low/high/max`를 50/75/100으로 설명하지만, 확인한 vLLM recipe는
  `low/high/xhigh/max`를 25/50/75/100으로 설명한다. 최대값 100은 일치하지만 낮은 별칭을
  엔진과 무관하게 통일하지 않는다. 강도 접두사는 thinking 모드에서 대화 시작 부분에 적용된다.
- `max_tokens >= 256K`는 권장 생성 예산이고 하드 출력 한도는 미확인이다.
- 공개 배포물에는 Jinja chat template이 없다. 공식 인코딩 구현 또는 `deepseek-recipe`
  프로토콜 변환을 지원해야 하며, DSML 도구 호출을 일반 JSON 텍스트로만 취급하지 않는다.
- vLLM recipe는 0.30.0+ 지원 nightly와 tokenizer/tool/reasoning parser
  `deepseek_v41`을 명시한다. 자동 도구 선택을 활성화하며 실제 빌드 지원을 확인한다.

출처: [공식 모델 카드](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash),
[공식 config](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/config.json),
[공식 인코딩 문서](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/main/encoding/README.md?code=true),
[공식 deepseek-recipe](https://github.com/deepseek-ai/deepseek-recipe),
[vLLM recipe](https://recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash).

## GLM-5.3-Flash

- 정확한 ID: `zai-org/GLM-5.3-Flash`.
- 공개 설정의 컨텍스트는 **1,048,576**이며, 이를 사용하기 위해 추가로 필요한 사용자
  YaRN 확장은 확인되지 않았다. BF16 및 별도 양자화 배포물은 이 정확한 ID와 구분한다.
- 논리적·실제 최대 강도는 모두 `max`이다. 지원값은 `low/high/max`, 기본값은 `max`이고
  현재 템플릿은 해당 강도 지시를 시스템 부분에 넣는다.
- 현재 공식 템플릿은 생성 시 `<think>`를 항상 연다. 일부 엔진 문서의 일반적인
  `thinking=false` 설명을 그대로 적용하지 않는다. `clear_thinking`은 과거 추론 보존
  설정이며 새 응답의 추론 끄기 옵션이 아니다.
- 하드 출력 한도와 일반 출력 권장값은 미확인이다. 모델 카드의 특정 평가에서 쓰인
  163,840 생성 토큰 등을 최대값 또는 모든 요청의 기본 예산으로 옮기지 않는다.
- vLLM recipe는 0.29.0+ 지원 Docker 빌드와 도구·추론 파서 `glm47`을 명시한다.
  SGLang recipe에서는 도구 `glm47`, 추론 `glm45`이므로 엔진별 파서를 구분한다.
  구조화된 `tool_calls`와 `reasoning_content`/최종 `content`를 분리해서 처리해야 한다.

출처: [공식 모델 카드](https://huggingface.co/zai-org/GLM-5.3-Flash),
[공식 config](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/config.json),
[공식 chat template](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/chat_template.jinja),
[vLLM recipe](https://recipes.vllm.ai/zai-org/GLM-5.3-Flash),
[SGLang recipe](https://docs.sglang.io/cookbook/autoregressive/GLM/GLM-5.3-Flash).

## 검증 범위

`tests/test_local_model_presets.py`는 정확한 ID 조회, 잘못된 입력 및 별칭 거부,
컨텍스트 확장의 별도 표현, 모델별 최대 추론 대응, 미확인 출력 한도 보존,
반환 데이터의 중첩 변경 격리, 엔진별 파서 차이, 네트워크 없는 import/조회를 확인한다.
이 모듈은 공급자·UI·연결 설정을 변경하지 않으며 실제 엔진 실행 성공을 주장하지 않는다.
