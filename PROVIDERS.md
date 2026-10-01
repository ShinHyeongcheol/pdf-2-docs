# 제공자 설정: Gemini 기본, OpenAI 선택형

지원하는 제공자는 `gemini`, `openai`입니다. 다른 API, 임의 endpoint, 자동 provider fallback은
구현하지 않았습니다. 두 어댑터는 같은 `QuizGenerator` 계약과 LangGraph 생성 → 독립 검증 → 제한
재시도 경로를 사용합니다. 원문 인용·답·질문·해설·출처 블록 ID를 검증하며 의미 진위는 검증하지 않습니다.

| 선택 | 키 이름 | 네이티브 구조화 출력 | 고정 전송 대상 |
| --- | --- | --- | --- |
| gemini (기본) | `GEMINI_API_KEY` | Gemini JSON schema | `generativelanguage.googleapis.com:443` |
| openai | `OPENAI_API_KEY` | OpenAI strict JSON schema | `api.openai.com:443` |

모델명 기본값은 없습니다. 선택할 모델의 **텍스트 입력과 JSON schema 지원**을 공식 문서에서 확인한
후 `approved_models`에 넣어야 합니다. 이것은 사용자의 명시적 capability 승인 목록이며 계정 접근이나
실제 모델 존재·가격·성능을 자동 검증한 목록이 아닙니다. 알 수 없는 모델은 실제 API에서 거절될 수
있으며 다른 제공자로 자동 전송되지 않습니다. 설정 예시의 모델 자리표시자는 실행 가능한 모델명이 아닙니다.

## 사용자가 키를 넣을 위치

저장소 루트 `pdf-2-docs/.env`에 아래 형식으로 직접 입력하세요. 기존 파일이 있다면 덮어쓰지 말고
해당 키 줄만 수정하세요. 키를 채팅·Git·PR·로그에 붙이지 마세요. `.env`는 Git에서 제외됩니다.

```dotenv
GEMINI_API_KEY=사용자가_직접_입력
# OpenAI를 선택할 때만 필요한 별도 키
OPENAI_API_KEY=사용자가_직접_입력
```

`build_provider(..., project_root=Path.cwd())`로 저장소 루트를 명시한 **승인된 호출**에서만 `.env`의
선택된 키를 읽습니다. 프로세스 환경변수에 같은 이름의 키가 있으면 그것이 우선합니다. 다른 디렉터리,
키체인, 기존 자격증명 파일을 검색하지 않습니다. 따옴표는 지원하지만 쉘 실행·변수 확장·보간·중복
키·심볼릭 링크는 지원하지 않습니다. 키를 `os.environ`에 자동 주입하지 않습니다.

이것은 전체 `.env` 자동 로딩이 아닙니다. provider/model/승인 설정은 명시적인 `ProviderSettings` 또는
아래 **비밀이 아닌 프로세스 환경변수**로 지정합니다. `.env`에 넣어도 이 설정들은 읽지 않습니다.

```sh
export PDF_NOTION_PROVIDER=gemini
export PDF_NOTION_MODEL='<공식 문서에서 확인한 모델 ID>'
export PDF_NOTION_APPROVED_MODELS='<지원 기능을 승인한 모델 ID>'
.venv/bin/python -m pdf_notion_mvp.provider_cli
```

`provider_cli`는 `--provider gemini|openai`, `--model ...`도 받습니다. 설정 여부만 확인하고 키 조회,
파일 로딩, SDK 생성과 네트워크 호출은 하지 않습니다. 기존 `quiz_cli`는 authored mock 전용입니다.
**현재 유료/live 실행 CLI는 없습니다.** 키를 저장하거나 이 명령을 실행해도 자료가 전송되지 않습니다.

## 승인 후 프로그램에서 사용하는 경계

```python
from pathlib import Path
from pdf_notion_mvp.providers import ProviderSettings, build_provider
from pdf_notion_mvp.quiz import QuizWorkflow, QuizPolicy

settings = ProviderSettings.from_env()
adapter = build_provider(settings, project_root=Path.cwd())
workflow = QuizWorkflow(adapter)
# 생성 시 명시적 입력 source/hierarchy/review_layer/section_id와 QuizPolicy를 전달합니다.
# QuizPolicy() 기본값은 network off, budget/capability 미승인, 호출·입출력 예산 0입니다.
```

네트워크·자료 전송 승인, 가격 확인과 호출/요청 바이트/출력 토큰 예산, 모델 capability 승인이 모두
필요합니다. gate가 닫혀 있거나 모델이 승인 목록에 없으면 키를 읽기 전에 차단합니다. Gemini는
Developer API로 고정하고 Vertex/Cloud credentials 자동 선택은 하지 않습니다. 최종 SDK 요청의
HTTPS 호스트·443 포트·모델 경로·크기를 전송 전에 검사합니다. Gemini SDK 재시도는 끄며 추가
physical request도 guard에서 거절합니다. 호출 예산은 어댑터 인스턴스에서 원자적으로 예약하고 실패도
차감합니다. 영속 비용 원장/달러 상한/다중 프로세스 예산은 아직 없습니다.

기존 `OpenAIQuizAdapter` 직접 사용과 `PDF_NOTION_OPENAI_MODEL` 환경변수는 유지됩니다.
`GeminiQuizAdapter` 직접 사용은 `PDF_NOTION_GEMINI_MODEL`을 지원합니다. 사용자 기본 경로는
공통 `build_provider`이며 `PDF_NOTION_MODEL`과 승인 목록을 사용합니다.

설치: `uv sync --extra test --extra openai --extra gemini`. 실제 모델·계정·가격·latency·vision은
미검증입니다. 테스트는 가짜 키와 MockTransport만 사용하고 실제 API 호출과 비용은 0입니다.

[Google 키 안내](https://ai.google.dev/gemini-api/docs/api-key),
[Google 구조화 출력](https://ai.google.dev/gemini-api/docs/structured-output),
[LangChain Gemini 어댑터](https://docs.langchain.com/oss/python/integrations/chat/google_generative_ai).
