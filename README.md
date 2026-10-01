# pdf-2-docs

문서 구조와 출처를 유지하며 `목차 → 절별 복원 → 독립 검증 → 게시 계획`을 만드는 Python 프로젝트입니다.
LangGraph가 실제 작업 노드를 실행하고, LangChain Core의 LCEL 체인이 목차 계획·복원을 실행합니다.
기본 어댑터는 직접 작성한 합성 데이터에 대한 결정적 로컬 구현입니다. 모델 API 비용과 자격증명이 필요 없습니다.

**현재 가능한 것:** 합성 IR의 텍스트·이미지 참조·표·코드 보존, 출처/좌표 검증, SQLite 상태 저장,
중간 실패 후 재개, 동일 입력 중복 방지, FastAPI 상태 조회, 실제 PDF의 텍스트 레이어/OCR 필요 여부 검사,
Mac 내장 Apple Vision OCR의 텍스트·좌표·신뢰도와 원본 페이지 래스터 보존.
**아직 없는 것:** OCR의 의미적 정확성 검증, PDF 표·코드·그림의 자동 의미적 복원, 실제 모델 응답 품질 검증,
앱 내부 Notion API 쓰기, 의미적 설명 생성, RAG.
게시 계획은 Notion HTTP payload가 아닌 중립적 검토 자료입니다. `ready`는 이 dry-run 자료가 준비되었다는 뜻입니다.

## 시작

Python 3.11 이상과 uv를 사용합니다. 설치 시 PyPI 접근이 필요하며, 이후 합성 경로는 네트워크 없이 실행됩니다.

```bash
cd ~/Desktop/pdf-2-docs
UV_CACHE_DIR=/tmp/pdf-notion-uv-cache uv sync --frozen --extra test --extra pdf
.venv/bin/python -m pytest -q
.venv/bin/python -m pdf_notion_mvp.cli fixtures/synthetic.json
```

같은 명령을 다시 실행하면 같은 job_id와 revision=5를 재사용합니다. `output/run.json`에서 2개 절,
7개 게시 작업과 각 블록의 출처를 확인할 수 있습니다. 원본 PDF나 참조 자료는 저장소 밖에 보관합니다.

```bash
.venv/bin/python -m pdf_notion_mvp.pdf_inspect /absolute/private/input.pdf --output /absolute/private/inspection.json
PDF_NOTION_TEST_PDF=/absolute/private/input.pdf .venv/bin/python -m pytest -q
```

검사 명령의 종료 코드 2는 OCR이 필요하거나 텍스트가 없다는 뜻입니다. 검사 파일은 생성됩니다.
PDF 본문을 원격 서비스에 전송하지 않습니다. 전체 페이지의 텍스트 수와 이미지 수를 점검하되
본문 추출 정확성이나 표·수식·코드 인식의 완전성을 보장하지 않습니다.

## Mac의 무료 로컬 OCR

Xcode Command Line Tools와 한국어/영어 Vision OCR 지원이 필요합니다. 외부 API 키가 필요 없습니다.
다음은 **검토 후보**를 만들며 OCR confidence를 정확성 보증으로 쓰지 않습니다.

```bash
xcrun swiftc -module-cache-path /tmp/pdf-notion-swift-cache scripts/vision_ocr.swift -o /tmp/pdf-notion-vision-ocr
.venv/bin/python -m pdf_notion_mvp.ocr_cli /absolute/private/input.pdf --work-dir /absolute/private/ocr --vision-binary /tmp/pdf-notion-vision-ocr --pages 1,6
# 전체 문서: --pages all. 원문과 OCR 캐시는 저장소 밖 work-dir에 보존합니다.
.venv/bin/python -m pdf_notion_mvp.cli /absolute/private/ocr/HASH/apple-vision-r3-150dpi/document-ir.json --output /absolute/private/run.json
```

OCR은 문서 SHA·엔진·DPI별 페이지 캐시에서 재개합니다. 첫 OCR 행을 슬라이드 제목 후보로 사용하며
전역 의미적 목차 생성은 아직 제공하지 않습니다. 코드·표·그림은 페이지 이미지와 OCR 텍스트로 남습니다.
부분 페이지 OCR은 `rejected`로 게시 계획을 차단합니다. 전체 페이지 보존 검증을 통과해도
`human_review_required=true`이며 OCR 오탈자·인덴트·수식·다단 읽기 순서는 사람이 확인해야 합니다.
자동 Notion 게시자는 없으며 MCP를 통한 검토 샘플 게시는 이 로컬 앱의 통합 테스트와 별개입니다.
OCR 검증은 실제 페이지별 전체 래스터, 파일 존재와 SHA-256, 좌표 범위를 확인합니다.
CLI는 IR 파일의 부모 폴더를 신뢰할 에셋 범위로 사용합니다. API에서 OCR IR을 검증하려면
서버의 `PDF_NOTION_ASSET_ROOT`에 해당 비공개 OCR 폴더를 지정해야 합니다. 설정하지 않거나
그 범위 밖 파일을 참조하면 `rejected`이며, API가 임의 로컬 파일을 읽지 않습니다.

## 상태 API 확인

```bash
.venv/bin/python -m uvicorn pdf_notion_mvp.api:create_demo_app --factory --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/docs`에서 아래 순서로 확인할 수 있습니다.

위 명령은 서버가 명시적으로 켜는 **합성 데모**입니다. 일반 `create_app`은 합성 입력의 생성·
실행·재개·게시 계획 조회를 거절하며, 요청 본문으로 데모 모드를 활성화할 수 없습니다.
일반 앱의 OCR 검증은 서버에서 지정한 신뢰 에셋 폴더를 사용합니다. 합성 모드는 합성 출처와
`synthetic://` 이미지 참조만 허용하며 OCR/래스터 출처와 혼합할 수 없습니다.

1. `POST /jobs`: `{"request_key":"local-demo","input": <fixtures/synthetic.json의 전체 객체>}`.
2. `GET /jobs/{job_id}`: 현재 상태·revision·artifact·history 확인.
3. `POST /jobs/{job_id}/advance`: 현재 `{"expected_revision":0}`부터 revision을 갱신하며 5회 실행.
4. `GET /jobs/{job_id}/publish-plan`: 검증 통과 후만 확인 가능.

`failed`는 `POST /jobs/{job_id}/resume`에 현재 revision을 보내 마지막 성공 상태로 복귀할 수 있습니다.
`rejected`는 수정된 입력이나 새 pipeline version으로 다시 만들어야 합니다. 같은 request key에 다른
입력은 409, 이전 revision의 mutation도 409입니다. 상태가 READY인 작업 재실행은 변경을 만들지 않습니다.

기본 DB는 `output/jobs.sqlite`이며 `PDF_NOTION_DB`로 바꿀 수 있습니다. 서버는 로컬 확인용이며 인증을
제공하지 않습니다. 기본 경로는 tracing 업로드를 비활성화하고 모델/Notion 클라이언트를 생성하지 않습니다.

기능별 계약과 한계는 [DESIGN.md](DESIGN.md)에 있습니다. `tests/`는 상태 전이·재시작·예외 복구·동시성·
중복 방지·검증 차단·API 동작을 검증합니다. 실제 원문 통합 테스트는 로컬 환경변수로 선택하며
원문과 추출 결과를 fixture 또는 Git에 포함하지 않습니다.

## 계층 목차와 별도 교정 레이어

```bash
.venv/bin/python -m pdf_notion_mvp.review_cli fixtures/synthetic.json --outline fixtures/synthetic-outline.json --layer fixtures/synthetic-review.json --output output/synthetic-review.json
```

`HierarchicalOutline`은 부모 노드와 블록을 소유하는 잎을 구분합니다. 트리의 잎을 펼쳤을 때
원본 IR 전체 블록이 정확히 한 번, 입력 순서대로 나타나야 합니다. 선언 페이지는 실제 블록의 페이지와
일치해야 하며 문서 전체를 포함해야 합니다. 페이지가 여러 개인 절도 허용합니다. `ProvidedHierarchyAdapter`로
명시적 목차를 기존 복원·검증 경계에 적용할 수 있습니다. 목차 스냅샷의 SHA를 workflow version에
포함하므로 같은 원본에 다른 목차를 사용한 작업은 구분하고, 동일 목차의 재시작은 재사용합니다. 기본 자동 계획을 대체하거나 전역 설정을 바꾸지 않습니다.

`apply_review` 공개 함수는 명시적 `FixtureInput`을 받아 입력 모드·OCR/합성 출처 계약을
함수 진입에서 다시 검증합니다. `DocumentIR` 단독 입력은 허용하지 않습니다.

`ReviewLayer`는 문서 버전·전체 IR digest에 결합된 별도 데이터입니다. 교정은 block ID, 원본 OCR,
출처 page/bbox, 제안 텍스트, 근거 이미지 ID/영역, 확인 상태를 저장합니다. 확인된 교정만 `effective_text`에
적용하고 후보는 `pending_corrections`로 분리합니다. 원본 블록과 출처는 수정하지 않습니다.
표·코드를 표시용으로 조립한 파생 fragment는 원본 block ID와 교정 ID의 계보를 유지합니다.
원문 전사 확인과 기술적 주장의 정확성 검증은 다릅니다. 확인 상태는 검토자의 명시적 판단이며
코드/API 실행이나 진위 판정 기능이 아닙니다. 공백 정규화·애매한 글자·잘린 출력은 근거와 후보에 기록합니다.

OCR 입력은 기존 페이지 래스터/파일 SHA/신뢰 폴더 검증도 통과해야 합니다. CLI 출력이 원본·목차·교정
입력 파일과 같은 경로면 실행을 거절합니다. `review_cli`는 모델 호출이나 Notion 쓰기를 하지 않습니다.
실제 자료의 목차·교정 파일·결과는 저장소 밖에 보관하고 공개 fixture는 직접 작성한 합성 자료만 사용합니다.


## OpenAI 선택 어댑터와 모의 문제 생성

```bash
UV_CACHE_DIR=/tmp/pdf-notion-uv-cache uv sync --frozen --extra test --extra pdf --extra openai
.venv/bin/python -m pdf_notion_mvp.quiz_cli
.venv/bin/python -m pytest -q
```

기본 CLI는 직접 작성한 합성 응답을 LangChain runnable로 생성하고 LangGraph의 생성 → 독립 검증 →
제한 재시도 분기로 처리합니다. 실제 OpenAI 요청을 켜는 CLI 옵션은 없습니다. 결과는
`output/mock-quiz.json`에 남고 검증 성공도 `ready_for_review`, 반복 실패는 `failed_human_review`입니다.

첫 문제 유형은 근거 인용에 따른 빈칸 문제입니다. 답은 정확한 인용문 일부, 해설은 원문 인용으로
제한합니다. 출처 없는 참조·인용·정답·질문/해설의 추가 주장·빈 답·문제 중복을 독립적으로 차단합니다.
자유로운 해설이나 원문의 사실성 판정은 제공하지 않습니다. 성공해도 사람이 검토해야 합니다.
OCR 근거는 확인된 교정 기록의 body text만 사용하고 미확인/교정 후보는 제외합니다. raw IR은 보존하며
LLM context에는 확인된 파생 텍스트와 출처만 담습니다. 그림·표·코드의 의미 생성은 추후 별도 근거 어댑터로
확장할 범위이고 이번 어댑터는 텍스트만 사용합니다. PDF의 지시문은 신뢰 명령이 아닌 인용 자료입니다.

`OpenAIQuizAdapter`는 LangChain `ChatOpenAI.with_structured_output`을 사용합니다. 생성자에서 SDK/키를
초기화하지 않습니다. 기본 `QuizPolicy`는 네트워크를 차단합니다. 실제 요청은 사용자가 키·모델·기능 지원·
전송 자료·예산을 확인하고 `allow_network`, `budget_confirmed`, `capabilities_confirmed`를 명시적으로
설정해야 가능하며, 이번 개발에서 실제 요청은 실행하지 않았습니다.

키는 승인된 live 호출 경로 안에서만 `OPENAI_API_KEY` 환경변수로 받습니다. 실제 `.env`는 Git 제외,
값 없는 `.env.example`만 포함합니다. 파일·키체인에서 키를 찾거나 `.env`를 자동 로드하지 않습니다.
모델은 `QuizPolicy.model` 또는 승인된 live 경로의 `PDF_NOTION_OPENAI_MODEL`로 지정하고 기본 모델은 없습니다.
설정과 key를 결과·로그에 저장하지 않습니다. 문서상의 기능 지원이 해당 계정의 모델 접근을 보장하지는 않습니다.

호출 수·최종 요청 바이트·출력 토큰·timeout 한도를 명시해야 하며, SDK 자동 재시도는 0입니다.
Graph 시도는 최대 3회이고 실패도 호출 예산을 소비합니다. 예산은 adapter 인스턴스 수명 내 요청 수 한도이며
분산/영속 비용 원장이나 달러 단위 요금 상한은 아닙니다. 실제 시작 전 선택 모델의 현재 가격과 예산 승인을
따로 확인해야 합니다. 공식 OpenAI endpoint를 명시하고 실제 SDK 요청 크기를 전송 전에 검사합니다.

가짜 HTTP transport 테스트는 실제 SDK 직렬화와 모의 응답 해석만 검증합니다. API 키 유효성, 계정 권한,
모델 가용성, 실제 응답 품질·요금·latency·vision 통합은 아직 검증하지 않았습니다.

## 모의 문제의 절별 Notion 토글 계획

```sh
.venv/bin/python -m pdf_notion_mvp.notion_quiz_cli
```

`output/mock-notion-quiz.json`에서 네이티브 문제 토글 → 정답/해설 토글과 원문 출처를 확인합니다.
출력은 직접 작성한 **모의 문제**이고 실제 모델 생성 결과가 아닙니다. 저장된 QuizResult의 ready
표시를 신뢰하지 않고 원문·교정·절 입력에서 근거 digest와 질문을 다시 검증합니다. 원본 전사와
학습 근거를 따로 표시하고 문서 버전·블록 ID·페이지·좌표를 보존합니다.

현재 실행 대상은 가짜 페이지뿐입니다. 실제 Notion transport와 허브 연결은 구현되지 않았습니다.
`SectionPage`는 지정 허브와 절 페이지의 연결 계약이며 향후 gateway가 실제 계층을 검증해야 합니다.
완전한 페이지 snapshot에서 앱 토글의 표식을 먼저 읽고 누락된 문제만 추가합니다. 사용자 메모와
원문 블록은 수정하지 않습니다. 앱 토글이 편집되거나 표식이 중복되면 사람 검토로 중단합니다.
원격 성공 후 응답 유실은 다음 실행에서 표식을 읽어 복구하며 자동 재시도하지 않습니다.

검증 범위는 순차 재실행입니다. 다중 작성자의 동시 게시, 표식 삭제, 실제 API의 부분 쓰기·페이지
권한·계층·pagination은 미검증이며 실제 통합 전에 단일 작성자와 완전한 원격 읽기가 필요합니다.
