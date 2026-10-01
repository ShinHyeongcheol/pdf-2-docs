# 합성 그래프 설명 토글

첫 그래프 기능은 **직접 작성한 SVG·근거·모의 응답**으로 동작합니다. 실제 PDF 이미지 추출,
OCR/vision 모델 판독, 모델 API 호출, Notion 게시를 검증한 결과가 아닙니다. 실제 API 시험은
보류 상태이며 이 CLI는 키를 읽거나 모델/Notion 연결을 생성하지 않습니다.

## 실행과 확인

프로젝트의 기존 격리 환경에서 실행합니다. 새 의존성은 필요하지 않습니다.

```sh
.venv/bin/python -m pdf_notion_mvp.graph_cli
.venv/bin/python -m pdf_notion_mvp.graph_cli --mock-publish
.venv/bin/python -m pdf_notion_mvp.graph_cli --mock-publish
```

첫 명령은 `plan_ready`로 계획만 저장합니다. `--mock-publish`의 최초 실행은 `mock_written`,
같은 입력으로 재실행하면 `unchanged`입니다. 이미 같은 모의 페이지가 있으면 최초 게시 명령도
`unchanged`일 수 있습니다. 재실행은 작성한 모의 응답을 다시 검증하며 실제 모델을 호출하지 않습니다.

- `fixtures/synthetic-graph.svg`: 작성한 A=10, B=20, C=15 막대 그래프. 단위와 한 눈금은 미확정입니다.
- `fixtures/synthetic-graph.json`: 원본 IR·계층 목차·검토층·관찰 근거·모의 응답·가짜 페이지 연결입니다.
- `output/mock-graph-plan.json`: 독립 검증 결과와 native toggle 계획, 모의 게시 영수증입니다.
- `output/mock-graph-page.json`: 선택적 영속 모의 페이지입니다. 기존 메모를 보존합니다.

계획의 `plan.operations[0].block.toggle.children`에서 추출 후보, 확인된 관찰, 추론 후보,
검토 필요 사항을 확인할 수 있습니다. 토글 제목은 `[합성 그래프 설명 · 검토 필요]`이고,
모든 결과는 `human_review_required=true`, `actual_vision_verified=false`,
`semantic_correctness_verified=false`입니다. 이미지 자체는 synthetic asset 참조·해시·원본 좌표를
담은 출처 문단으로 보존합니다. 실제 Notion 이미지 업로드는 구현하지 않았습니다.

모의 페이지의 사용자 메모 문단을 추가한 뒤 재실행해도 메모는 남습니다. 앱이 작성한 그래프
토글을 직접 수정하면 `failed_human_review`로 중단하고 페이지 파일을 덮어쓰지 않습니다.

## 근거와 검증 경계

`prepare_graph_context`는 기존 원본 보존 검증과 검토층을 재적용합니다. 선택한 목차 **leaf**가
소유한 이미지 블록만 허용하고, 문서·버전·원본 digest·실제 SVG bytes의 SHA-256·블록 ID를
대조합니다. 관찰 좌표는 이미지의 원본 페이지 bbox 안에 있어야 합니다. 모의 멀티모달 입력에는
해당 이미지 bytes의 base64와 별도 관찰 근거를 넣습니다. 이 확인은 파일 일치 검증이며,
픽셀에서 수치나 의미를 읽었다는 증거가 아닙니다.

| 분류 | 현재 근거 | 토글 표시 |
|---|---|---|
| 추출 후보 | mock extractor가 작성한 미확정 항목 | 추출 후보 · 미확정 |
| 확인된 관찰 | 작성자가 정한 합성 값·범주·비교 | 확인된 관찰 · 합성 근거 |
| 판독 불가 | 값/추세 텍스트 없이 좌표만 보존 | 검토 필요 사항 |
| 추론 후보 | 확인된 값/비교 ID를 참조하는 닫힌 모의 템플릿 | 모의 모델 추론 후보 · 미확정 |

모의 생성기는 관찰 ID와 추론 종류만 선택합니다. 새로운 자유 서술 수치·추세 필드는
계약에서 거부하고, 후보/판독 불가 항목을 확정 관찰이나 추론 근거로 승격하면 독립 검증이
실패합니다. 확인된 관찰이 없으면 빈 관찰 토글과 명시적 검토 표시를 내고 수치를 채우지 않습니다.
현재 추론은 측정 조건 확인·다른 요인 검토 두 가지 문구만 지원합니다.

생성 단계는 독립된 context 복사본을 받습니다. 검증은 생성 전 snapshot을 사용하고, 게시 시
전달된 원본·근거·이미지로 context를 다시 준비하고 결과의 digest·검토 플래그·상태·이벤트를 재검증합니다.
저장한 계획을 수정해도 게시 근거로 사용하지 않습니다. 상속된 LangSmith tracing도 비활성화합니다.

근거 fixture는 신뢰하는 로컬 작성자 입력입니다. 로컬 작성자가 근거와 결과를 함께 바꾸는 상황을
서명으로 막거나 실제 시각/의미 진위를 증명하지 않습니다. 실제 PDF/OCR/vision 결과를 연결하려면
별도의 추출·모델 어댑터, 시각 검증, 사용자 검토 계약이 필요합니다. `GraphAdapter` 경계는 있지만
현재 실행은 `mock_multimodal` 모드만 허용합니다.

## 모의 게시와 실패 처리

그래프 표식 `pdf-notion-graph:v1:`은 문제 토글 표식과 별개입니다. 같은 그래프 내용과 근거는
같은 키를 사용합니다. 완전한 페이지 snapshot을 확인한 뒤 기존 표식과 블록 내용을 대조하고,
메모와 기존 문제 토글을 보존하며 새 그래프 토글만 append합니다. 중복/깨진 표식·사용자 편집·
불완전하거나 다른 페이지는 검토 실패로 중단합니다. append 응답이 유실되면 재실행 시 저장된
표식을 읽어 중복을 피합니다. 근거 또는 설명 내용이 달라지면 별도 키를 가지며 기존 내용을 삭제하지 않습니다.

페이지/계획 JSON은 같은 디렉터리의 임시 파일에 쓰고 flush·fsync한 뒤 atomic replace합니다.
부분 저장·replace 실패는 기존 파일을 보존하고 임시 파일을 정리합니다. 페이지 저장 후 계획 저장이
실패해도 다음 실행은 페이지 표식으로 재조정합니다. 둘을 묶는 transaction이나 다중 작성자 잠금,
전원 장애 후 디렉터리 내구성 보장은 제공하지 않습니다. 모의 페이지는 단일 작성자 리허설용입니다.

산출물은 무시되는 `output/` 아래 JSON만 허용하며 fixture 덮어쓰기, ledger 경로, symlink와
hardlink를 차단합니다. CLI 오류는 종류만 표시합니다.

```sh
.venv/bin/python -m pytest tests/test_graph_review.py -q
.venv/bin/python -m pytest -q
```

테스트는 소켓 연결을 차단합니다. 그래프 전용 66개 테스트는 출처 변경·잘못된 이미지/좌표·
새 수치/추세·후보 승격·판독 불가 추론·검토 누락·context 변조·저장 결과 변조·tracing 상속·
모의 페이지 충돌/응답 유실·재실행·부분 파일 저장/replace 실패를 검증합니다. 실제 API 및
연결된 Notion 검증은 미실행입니다. RAG는 별도 후속 기능입니다.
