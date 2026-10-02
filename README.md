# pdf-2-docs

PDF의 원본과 출처를 보존하면서 `목차 → 절별 복원 → 독립 검증 → 게시 계획`을 만드는 Python 프로젝트입니다. Pydantic 계약, LangChain Core LCEL, LangGraph 작업 흐름과 SQLite 상태 저장을 사용합니다. 기본 합성 경로와 로컬 학습 경로는 키·모델 API·Notion 연결 없이 실행됩니다.

현재는 **로컬 구현 체크포인트**입니다. 전체 PDF를 의미적으로 검수하여 Notion 학습 자료로 자동 완성하는 목표는 아직 완료하지 않았습니다. 전달물·검증·남은 작업은 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)에 정리했습니다. 실제 모델 API 시험과 Notion 표시 보정·최종 읽기 확인은 보류 중입니다.

| 경로 | 지금 할 수 있는 일 | 검증의 범위 |
| --- | --- | --- |
| 구조·작업 상태 | 텍스트·이미지·표·코드 계약, 문서 버전·블록 ID·페이지·좌표, 재개·중복 방지·상태 API | 출처와 구조 검증; 의미 정확성은 별도 |
| PDF·Mac OCR | 텍스트 레이어 검사, Apple Vision OCR, 페이지 PNG·좌표·신뢰도·SHA 보존 | 원본 페이지 보존; 자동 표·코드·그림 해석 없음 |
| 목차·교정 | 제공한 전체 계층 목차와 별도 검토 레이어 검증, 확정 교정 적용 | 자동 전역 주제 목차 생성이나 새 의미 검수 없음 |
| 규칙 문제·로컬 RAG | 선택 절의 확정 본문으로 빈칸 문제·키워드 검색·정확한 인용 검토 | 결정적 로컬 결과; 모델 설명·임베딩·벡터 검색 없음 |
| 오프라인 학습 묶음 | 원본 이미지·전사/교정·후보·코드 검토·규칙 문제·인용을 정적 HTML로 표시 | 한 절의 읽기 자료; 전체 문서 학습 품질 완료 아님 |
| Notion 연결 경계 | 새 절 페이지 생성 인수, pending 체크포인트, 읽기 확인·재실행 차단 | 호스트 MCP가 실행; 실제 표시 보정·최종 확인 보류 |
| 그래프·provider | 합성 SVG 모의 해설, 선택형 SDK·승인 원장·모의 게시 계획 | 실제 PDF 그래프 해석·실제 모델 결과 게시 미검증 |

## 기존 환경에서 비용 없이 확인

사용자 Mac의 기본 체크아웃과 격리 환경에서 실행합니다. `PYTHONPATH=src`는 현재 체크아웃의 코드를 사용하게 합니다.

```sh
cd ~/Desktop/pdf-2-docs
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.cli fixtures/synthetic.json
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.review_cli fixtures/synthetic.json \
  --outline fixtures/synthetic-outline.json --layer fixtures/synthetic-review.json \
  --output output/synthetic-review.json
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.quiz_cli
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.notion_quiz_cli
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.graph_cli
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.rag_cli --eval
```

기본 흐름을 다시 실행하면 같은 job ID와 `revision=5`를 재사용합니다. `output/run.json`은 합성 2개 절·7개 중립적 게시 작업을 담습니다. `ready`는 이 검토 자료의 준비 상태이며 Notion 게시 성공을 뜻하지 않습니다. 문제는 `ready_for_review`, 근거 없는 RAG 답변은 `unknown`입니다. 위 Notion·그래프 명령도 모의 경로입니다.

새 환경에서만 Python 3.11 이상과 uv로 설치합니다. 설치는 PyPI 접근이 필요합니다. 기본 로컬 경로에는 선택형 provider SDK가 필요 없습니다.

```sh
UV_CACHE_DIR=/tmp/pdf-notion-uv-cache uv sync --frozen --extra test --extra pdf
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## 기존 비공개 검토 입력으로 한 절 읽기

먼저 아래 **예시 경로와 leaf ID·질문을 기존 파일의 값으로 바꾸세요.** `document-ir.json`은 전체 문서 IR이고 부모 폴더에 검증 가능한 페이지 PNG가 있어야 합니다. 목차와 교정 레이어는 이미 검토한 별도 입력입니다. 이 명령이 생성하거나 확정하지 않습니다.

```sh
export PDF_STUDY_SOURCE=/absolute/private/ocr/document-ir.json
export PDF_STUDY_OUTLINE=/absolute/private/planning/hierarchical-outline.json
export PDF_STUDY_REVIEW=/absolute/private/planning/review-layer.json
export PDF_STUDY_SECTION='YOUR_LEAF_ID'
export PDF_STUDY_QUESTION='확정 본문에서 찾을 키워드'
export PDF_STUDY_OUTPUT=/absolute/private/study-output

PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_bundle \
  --source "$PDF_STUDY_SOURCE" --outline "$PDF_STUDY_OUTLINE" \
  --review "$PDF_STUDY_REVIEW" --section "$PDF_STUDY_SECTION" \
  --question "$PDF_STUDY_QUESTION" --output-dir "$PDF_STUDY_OUTPUT"
```

출력 경로는 절대 경로이고 Git과 모든 입력 폴더 밖이어야 합니다. symlink를 허용하지 않으므로 Mac 임시 폴더를 쓸 때도 정규 절대 경로를 지정하세요. 출력의 `index_html`을 브라우저에서 여세요. 같은 입력·절·질문으로 재실행하면 `unchanged`이며 파일 바이트·수정 시간을 보존합니다. 기존 출력이 수정·누락되면 보존하고 중단합니다. 원본 PNG·HTML·JSON·manifest가 하나의 내용 SHA 디렉터리에 생성됩니다. [STUDY_BUNDLE.md](STUDY_BUNDLE.md)에 상세 계약이 있습니다.

문제와 RAG JSON만 따로 확인하려면 같은 변수로 아래 함수를 정의합니다. 각 CLI의 `project_root`를 비공개 출력 루트로 명시하여 결과를 공개 체크아웃 밖의 `output/`에 저장합니다.

```sh
run_local_review() {
  PYTHONPATH=src .venv/bin/python - "$1" <<'PY'
import os
import sys
from pathlib import Path
from pdf_notion_mvp import quiz_files_cli, rag_cli

args = ["--source", os.environ["PDF_STUDY_SOURCE"],
        "--outline", os.environ["PDF_STUDY_OUTLINE"],
        "--review", os.environ["PDF_STUDY_REVIEW"],
        "--section", os.environ["PDF_STUDY_SECTION"]]
root = Path(os.environ["PDF_STUDY_OUTPUT"])
if sys.argv[1] == "quiz":
    quiz_files_cli.main(args + ["--output", "output/local-rule-quiz.json"], project_root=root)
elif sys.argv[1] == "rag":
    rag_cli.main(args + ["--question", os.environ["PDF_STUDY_QUESTION"],
                         "--output", "output/local-rag.json"], project_root=root)
else:
    raise SystemExit("choose quiz or rag")
PY
}
run_local_review quiz
run_local_review rag
```

규칙 문제는 기본 1개·최대 3개입니다. RAG는 선택한 leaf의 확정 본문만 색인하며 200항목을 초과하면 범위를 줄여야 합니다. 후보·파생 코드·표는 정답 근거로 승격하지 않습니다. 출처·인용 검증은 기술적 주장이나 코드 실행의 정확성을 판정하지 않습니다. [LOCAL_QUIZ.md](LOCAL_QUIZ.md), [LOCAL_RAG.md](LOCAL_RAG.md)를 참고하세요.

원본 PDF·OCR·교정·개인 매핑·질문·결과는 Git 밖에 보관하세요. 공개 fixture는 직접 작성한 합성 자료만 포함합니다. `.gitignore`만으로 비공개 자료의 공개 방지가 보장되지는 않습니다.

## PDF 검사와 Mac 무료 OCR

PDF 검사에는 `pdf` extra, OCR에는 Xcode Command Line Tools와 한국어/영어 Apple Vision 지원이 필요합니다. 이미 준비된 검토 입력을 읽는 위 명령에서는 OCR을 다시 실행하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.pdf_inspect \
  /absolute/private/input.pdf --output /absolute/private/inspection.json
xcrun swiftc -module-cache-path /tmp/pdf-notion-swift-cache \
  scripts/vision_ocr.swift -o /tmp/pdf-notion-vision-ocr
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.ocr_cli \
  /absolute/private/input.pdf --work-dir /absolute/private/ocr \
  --vision-binary /tmp/pdf-notion-vision-ocr --pages all
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.cli \
  /absolute/private/ocr/HASH/apple-vision-r3-150dpi/document-ir.json \
  --db /absolute/private/ocr-jobs.sqlite --output /absolute/private/run.json
```

검사 종료 코드 2는 OCR이 필요하거나 텍스트가 없거나 혼합·빈 페이지 등 검토가 필요한 경우를 뜻하며 검사 파일은 생성됩니다. OCR은 문서 SHA·엔진·DPI별 캐시에서 재개합니다. 첫 행은 제목 후보로만 사용하고 전역 의미적 목차를 자동 생성하지 않습니다. 부분 페이지 OCR은 게시 계획을 `rejected`로 차단합니다. 전체 페이지 보존 검증을 통과해도 `human_review_required=true`입니다. OCR 오탈자·인덴트·수식·다단 읽기 순서, 표·코드·그림의 의미 복원은 별도 검토가 필요합니다.

CLI는 IR 부모 폴더를 신뢰 에셋 범위로 사용합니다. API는 서버에서 지정한 `PDF_NOTION_ASSET_ROOT` 밖 파일을 읽지 않고 해당 설정이 없으면 OCR 입력을 거절합니다. 원문 통합 테스트는 명시적으로 `PDF_NOTION_TEST_PDF`를 지정해야 실행되며 기본 테스트에서는 건너뜁니다.

## 로컬 작업 상태 API

```sh
PYTHONPATH=src .venv/bin/python -m uvicorn pdf_notion_mvp.api:create_demo_app \
  --factory --host 127.0.0.1 --port 8000
```

`http://127.0.0.1:8000/docs`에서 합성 작업의 상태를 확인합니다. 일반 `create_app`은 합성 입력을 거절하며 요청 본문으로 데모 모드를 켤 수 없습니다. 이 API는 구조 검증 작업용입니다. HTML·규칙 문제·RAG·Notion까지 연결하는 전체 자동 실행기는 아닙니다.

1. `POST /jobs`: `{"request_key":"local-demo","input": <fixtures/synthetic.json 전체 객체>}`.
2. `GET /jobs/{job_id}`: status·revision·artifact·history 확인.
3. `POST /jobs/{job_id}/advance`: `{"expected_revision":0}`부터 갱신된 revision으로 5회 실행.
4. `GET /jobs/{job_id}/publish-plan`: 검증 통과 후 조회.

`failed`는 현재 revision으로 `/resume`을 호출하여 마지막 성공 상태로 복귀합니다. `rejected`는 수정 입력이나 새 pipeline version이 필요합니다. 동일 request key의 다른 입력과 오래된 revision은 409이고 READY 재실행은 변경이 없습니다. 기본 DB는 `output/jobs.sqlite`이며 `PDF_NOTION_DB`로 지정합니다. 로컬 서버에는 인증이 없습니다. 기본 경로는 tracing 업로드를 끄고 모델·Notion 클라이언트를 생성하지 않습니다.

## 연결 경계와 상세 문서

- [DESIGN.md](DESIGN.md): 중간 표현·교체 가능한 어댑터·상태 전이·구조 검증.
- [GRAPH_REVIEW.md](GRAPH_REVIEW.md): 합성 SVG의 모의 해설 토글; 실제 PDF 그래프 입력은 미연결.
- [PROVIDERS.md](PROVIDERS.md), [LIVE_RUN.md](LIVE_RUN.md): Gemini 기본·OpenAI 선택형 제공자, 별도 승인과 단일 실행 원장. 실제 API 시험은 보류 중이며 위 로컬 명령은 키를 읽지 않습니다.
- [PROVIDER_PUBLICATION.md](PROVIDER_PUBLICATION.md): 완료 원장·승인·현재 근거를 재검증하는 provider 결과의 중립적 검토 게시 계획. fake SDK 결과는 실제 모델 응답이 아닙니다.
- [NOTION_MCP.md](NOTION_MCP.md): 모의 문제 토글의 호스트 준비·읽기 확인 경계.
- [STUDY_NOTION.md](STUDY_NOTION.md): 한 절의 새 페이지 인수·pending/confirmed 체크포인트·동일 작업 보존. 실제 MCP dispatch는 호스트가 수행하며 독립 실행 게시 CLI는 없습니다.

실제 Notion create 성공만으로 완료하지 않습니다. 최종 내용·직접 부모·마커·코드·이미지 읽기 확인이 필요합니다. 현재 표시 보정 쓰기는 자동 승인 검토에서 거절되어 중단했고 최종 확인은 pending입니다. 모델·Notion 외부 검증이 재개되기 전에는 실제 모델 결과의 최종 게시가 검증됐다고 표현하지 않습니다.

전체 문서의 외부 호출 없는 감사·검수 검색·페이지 체크포인트·PNG 도표 후보 계약은 [DOCUMENT_LOCAL.md](DOCUMENT_LOCAL.md)를 참고하세요.

예산 제한 전체 문서 생성·원응답 보존·무료 수정·독립 검토 후 렌더링은 [DOCUMENT_LESSONS.md](DOCUMENT_LESSONS.md)를 참고하세요. 요청 배치와 최종 주제별 교재 목차를 구분합니다.
