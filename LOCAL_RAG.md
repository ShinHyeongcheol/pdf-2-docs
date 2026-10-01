# 로컬 근거 인용 Q&A

RAG 기능은 **무료 로컬 키워드 검색 + 모의 인용 답변 조립 + 독립 출처 검증**입니다.
기본 합성 데모와 명시적 로컬 검토 파일 입력을 구분합니다.
실제 임베딩을 생성하지 않았고 벡터 검색도 구현하지 않았습니다. 모델 API 시험은 보류 상태이며,
이 CLI는 키/.env를 읽거나 모델·Notion 연결을 생성하지 않습니다.

## 실행과 확인

기존 프로젝트 격리 환경에서 실행합니다. 추가 의존성은 필요하지 않습니다.

```sh
.venv/bin/python -m pdf_notion_mvp.rag_cli --question '재개 체크포인트'
.venv/bin/python -m pdf_notion_mvp.rag_cli --question '해왕성 질량' --output output/mock-rag-unknown.json
.venv/bin/python -m pdf_notion_mvp.rag_cli --eval --output output/mock-rag-eval.json
```

첫 질문은 `ready_for_review`로 원문 인용과 출처를 반환합니다. 두 번째는 `unknown`으로
“근거를 찾지 못했습니다. 모릅니다.”를 반환하고 모의 생성기도 호출하지 않습니다.
세 번째는 아래 합성 평가 9건의 결과를 저장합니다.

- `fixtures/synthetic-rag.json`: 작성한 원본 IR·계층 목차·확정/미확정 교정층·가짜 페이지 연결입니다.
- `fixtures/synthetic-rag-eval.json`: 답변 가능·근거 없음·오인용·후보 유입·재색인·출처 혼합 평가입니다.
- `output/mock-rag.json`: 질문, 답변, 원문 인용, source/section/page/bbox, 검증 이벤트입니다.
- `output/mock-rag-eval.json`: 각 평가의 기대 상태·실제 상태·근거 블록·성공 여부입니다.

예시 답변은 `근거 원문:` 뒤에 “재개 기능은 마지막 체크포인트부터 다시 실행합니다.”를 표시합니다.
`result.draft.citations`에서 문서·버전·블록·절·페이지·좌표·원본/검토층/색인 digest를 확인할 수 있습니다.
페이지 연결이 주어졌으면 해당 section의 Notion URL을 보존하며, 없으면 `null`입니다. 합성 fixture의
UUID와 URL은 가짜이며 실제 페이지가 아닙니다. 링크 접근 가능 여부도 검증하지 않았습니다.

모든 결과는 `human_review_required=true`, `semantic_correctness_verified=false`,
`notion_links_verified=false`, `actual_embeddings_used=false`, `vector_search_implemented=false`입니다.

## 색인과 검색 경계

`prepare_index`는 기존 원본 보존·계층 목차·교정 lineage 검증을 재적용합니다. 텍스트 body만 대상으로
작성한 합성 원문이나 **확정된 교정 텍스트**를 색인하며 원본 전사는 별도로 보존합니다. 미확정 교정
대상은 원문까지 제외합니다. heading·코드·표·이미지·파생 fragment 후보/해설은 색인하지 않습니다.
OCR IR은 확정 교정이 있는 텍스트만 허용하고 기존 trusted raster 검증을 거칩니다. 테스트의 OCR 모양
IR/raster bytes는 작성한 가짜 데이터이며 실제 OCR/이미지 판독 통합을 검증한 결과가 아닙니다.

| 경계 | 현재 구현 | 후속 교체 지점 |
|---|---|---|
| `RetrievalAdapter.build(RagIndex)` | `LocalLexicalAdapter` → LangChain `BaseRetriever` | 승인된 embedding/vector adapter |
| `LexicalRetriever` | 대소문자 정규화·단어 토큰 교집합 점수, 상위 3개 | 의미 검색·reranking |
| `AnswerAdapter.generate(RagContext)` | `MockAnswerAdapter` → LCEL `RunnableLambda` | 승인된 모델 adapter |
| `RagWorkflow` | LangGraph retrieve → generate → independent_verify | 검토된 별도 실행 정책 |

현재는 `lexical_local` 검색과 `mock` 생성 모드만 실행할 수 있습니다. 임베딩/외부 모델 모드는
adapter 호출 전에 차단합니다. 단어 형태·조사·동의어가 다르면 관련 문장이 있어도 검색하지 못할 수
있습니다. 단어가 겹친 문장이 질문에 의미상 충분히 답하는지도 판단하지 않습니다. 따라서 이 기능의
답변은 인용을 보여주는 검토 후보이며 자연어 추론/의미 정답 판정 결과가 아닙니다.

문서·버전·교정층·목차·페이지 연결·색인 내용이 달라지면 이전 색인을 거부합니다. `run`은 매번 현재
입력으로 색인을 다시 만들며, 저장한 `RagIndex`를 넘기면 완전히 같은 색인인지 비교한 뒤 사용합니다.
이 baseline은 메모리 색인이고 vector DB·incremental update·장기 검색 저장소는 없습니다.
선택된 leaf 목록도 색인 digest에 묶이며 범위가 달라지면 이전 색인을 거부합니다.

## 답변과 검증

모의 생성기는 검색된 근거의 전체 문장을 인용합니다. 자유 서술 결론은 지원하지 않습니다. 독립
검증기는 인용 문장뿐 아니라 evidence ID와 문서·버전·블록·절·페이지·좌표·교정층·색인 digest·Notion
URL이 검색된 권위 근거와 일치하는지 대조합니다. 답변 본문은 해당 인용을 연결한 문구와 같아야 합니다.
새 수치/결론·오인용·다른 문서 또는 검색하지 않은 절의 출처·후보 인용은 검토 실패로 중단합니다.

검색 adapter가 반환한 LangChain `Document`도 색인 snapshot과 대조합니다. 생성기와 검색기는 복사본을
받으며, 독립 검증은 이전 snapshot을 사용합니다. 상속된 LangSmith tracing은 전체 흐름에서
비활성화합니다. 근거가 없으면 로컬에서 모름 응답을 만들고 생성기를 건너뜁니다. 오류가 나면 메시지
본문 대신 오류 종류만 남기고 자동 재호출하지 않습니다.

로컬 작성자의 원본·교정 상태는 신뢰 입력입니다. 서명이나 실제 의미 진위를 증명하지 않으며,
여러 문서를 섞은 corpus 검색도 현재 범위 밖입니다. 실제 원문·교정·출처 매핑과 답변은 공개 Git에
포함하지 않는 비공개 경로에 보관합니다. CLI는 합성 데모 또는 아래 명시적 검토 파일을 읽습니다.

산출물은 무시되는 `output/`의 JSON만 허용합니다. fixture·ledger 덮어쓰기와 symlink/hardlink를
차단합니다. 임시 파일에 쓰고 flush·fsync 후 atomic replace하여 부분 저장/replace 실패 시 기존
결과를 보존합니다. 단일 작성자용이며 파일 잠금이나 전원 장애 후 디렉터리 내구성 보장은 없습니다.

## 모의 평가

```sh
.venv/bin/python -m pytest tests/test_rag.py -q
.venv/bin/python -m pytest -q
```

작성한 평가 9건은 원문/확정 교정/두 번째 페이지 질문, 근거 없는 질문, 미확정/파생 후보 질문,
오인용, 다른 문서 출처 혼합, 수정 문서의 오래된 색인 거부와 재색인 답변을 다룹니다. 기대값은
직접 작성했으며 실제 사용자 문서·모델·임베딩 품질의 성능 지표가 아닙니다.

전용 테스트 64개는 평가 외에도 인용 필드 변조·검색 결과 주입·후보 제외·stale index·context 변조·
추적 상속·실제 모델/embedding 모드 차단·키 없는 CLI·출력 경로·저장 실패를 검증합니다. 테스트 중
소켓 연결은 차단됩니다. 실제 API/OCR/Notion 통합 및 의미 검색 품질 검증은 미실행입니다.


## 명시적 검토 파일 입력

```sh
.venv/bin/python -m pdf_notion_mvp.rag_cli \
  --source /absolute/private/ocr/document-ir.json \
  --outline /absolute/private/hierarchical-outline.json \
  --review /absolute/private/confirmed-review.json \
  --section '<leaf ID>' --question '<질문>' \
  --output output/private-rag-review.json
```

`--source`, `--outline`, `--review`, 하나 이상의 `--section`, 명시적 `--question`을 함께 전달합니다.
`--section`은 반복할 수 있고 부모/알 수 없는/중복 ID는 거절합니다. 선택은 전체 원문을 자르는
작업이 아닙니다. 전체 페이지·블록·목차·교정 lineage·모든 OCR 래스터를 먼저 검증한 뒤 선택한
leaf의 확인된 body 텍스트만 색인합니다. 미확정 교정 대상·무교정 OCR·파생 fragment는 제외합니다.
원본 전사와 전체 IR은 그대로 두며 산출물에는 전체 페이지/블록 수와 선택 범위·색인 항목 수를 기록합니다.

선택 항목은 최대 **200개**입니다. 정확히 200개는 허용하며 초과 시 `IndexLimitExceeded`와
`index_limit=200 select_fewer_sections`로 중단합니다. 조용한 자르기·자동 scope 확장은 없습니다.
더 작은 leaf를 선택하거나 검토된 목차에서 절을 나누어 다시 실행하세요. 선택 절에 확정 근거가 없거나
질문의 토큰과 겹치는 근거가 없으면 생성기 호출 없이 `unknown`입니다.

선택적 `--binding /absolute/private/section-binding.json`을 반복해서 주면 해당 절의 Notion 링크를
보존합니다. 각 파일은 `SectionPage` 객체이며 문서/버전/leaf가 일치해야 합니다. 연결 없이도 실행할 수
있으며 이 경우 링크는 `null`입니다. 접근 권한이나 원격 페이지 존재는 확인하지 않습니다.

OCR IR의 **source JSON 부모 폴더**를 trusted asset root로 사용합니다. 래스터가 없거나 SHA가 다르거나
범위 밖에 있으면 전체 검증을 중단합니다. 임의 다른 root로 범위를 넓히는 CLI 옵션은 없습니다.
명시적 입력 JSON은 최대 16 MiB(페이지 제한과는 별개), binding은 100,000바이트입니다.
빈/손상/누락 파일·symlink/hardlink·`.env` 이름을 거부하고 키 설정 파일을 검색하지 않습니다.
출력은 ignored `output/` JSON만 허용하며 입력/연결 파일과 OCR 래스터를 덮어쓰지 못합니다.
오류 시 기존 결과 파일을 보존하므로 실패한 실행 뒤에는 이전 결과를 새 답변으로 해석하지 마세요.

`--eval`은 작성한 합성 평가 전용이며 파일 입력/범위/binding과 섞을 수 없습니다. 실제 문서 실행도
모의 답변 adapter가 원문 인용을 조립하는 방식이고 외부 LLM 생성·임베딩은 사용하지 않습니다.

파일 입력 테스트는 작성한 162페이지 대용량 IR, 전체 보존/선택 범위, 200/201 경계, 누락/손상/후보/
밖의 에셋/입력 덮어쓰기 차단을 검증합니다. 별도 비공개 자료의 기존 162페이지 OCR IR과 확정 교정층을
선택 절로 연결해 로컬 검색·정확한 인용을 확인했습니다. 이 확인은 기존 검토 상태를 신뢰한 파일 연결
검증이며 OCR/교정의 의미 진위나 전체 문서 검색 품질·벡터 RAG 성능을 새로 검증한 결과가 아닙니다.
