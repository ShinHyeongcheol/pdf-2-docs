# pdf-2-docs

문서 구조와 출처를 유지하며 `목차 → 절별 복원 → 독립 검증 → 게시 계획`을 만드는 Python 프로젝트입니다.
LangGraph가 실제 작업 노드를 실행하고, LangChain Core의 LCEL 체인이 목차 계획·복원을 실행합니다.
기본 어댑터는 직접 작성한 합성 데이터에 대한 결정적 로컬 구현입니다. 모델 API 비용과 자격증명이 필요 없습니다.

**현재 가능한 것:** 합성 IR의 텍스트·이미지 참조·표·코드 보존, 출처/좌표 검증, SQLite 상태 저장,
중간 실패 후 재개, 동일 입력 중복 방지, FastAPI 상태 조회, 실제 PDF의 텍스트 레이어/OCR 필요 여부 검사,
Mac 내장 Apple Vision OCR의 텍스트·좌표·신뢰도와 원본 페이지 래스터 보존.
**아직 없는 것:** OCR의 의미적 정확성 검증, PDF 표·코드·그림의 의미적 복원, 생성 모델,
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
