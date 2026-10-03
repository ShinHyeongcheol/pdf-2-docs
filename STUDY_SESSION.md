# 한 절의 학습 실행 연결

`study_session`은 검토 입력·절·출력·공유 비용 원장을 한 비공개 세션에 묶습니다.
원본 읽기 자료와 미승인 생성 계획을 함께 만들고, 별도 승인 생성과 독립 검수 후
렌더링을 같은 실행 진입점에서 이어갑니다. 기존 PDF 추출기와 Notion 연결 호스트는
별도 경계로 유지합니다. 실제 새 PDF 전체 자동 게시 완료를 뜻하지 않습니다.

| 단계 | 실행과 확인 |
| --- | --- |
| PDF 추출 | `native_pdf` 또는 Mac OCR로 전체 IR·페이지 PNG를 보존합니다. |
| 목차·교정 | 전체 목차와 교정 레이어를 대조합니다. 세션은 목차의 모든 노드가 `confirmed`여야 준비됩니다. 후보를 자동 확정하지 않습니다. |
| `prepare` | 원본 확인 HTML·미승인 생성 계획·세션 지문을 만듭니다. 키 조회·모델·Notion 호출 0건입니다. |
| `generate` | 별도 승인 파일과 정확한 계획 SHA를 확인한 뒤 기존 단일 요청 실행기를 사용합니다. |
| `render` | 출처 대조를 마친 `ContentReview`와 원장·현재 입력이 일치해야 설명·문제 HTML을 만듭니다. |
| Notion | 현재 별도의 검토 게시 계획과 연결 호스트가 필요합니다. 세션의 성공은 Notion 게시 성공이 아닙니다. |

## 먼저 비용 없이 준비

기본 Mac 체크아웃에서 실행합니다. 아래 입력·절·질문·공유 원장 경로는 실제
검토 자료의 값으로 바꾸세요. 세션은 Git 및 입력 폴더 밖의 새 절대 경로입니다.
원장을 새로 만들어 실제 사용 이력을 버리지 마세요.

```sh
cd ~/Desktop/pdf-2-docs
export PDF_STUDY_SESSION="$HOME/Documents/pdf-notion-session"
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session prepare \
  --source /absolute/private/ocr/document-ir.json \
  --outline /absolute/private/planning/hierarchical-outline.json \
  --review /absolute/private/planning/review-layer.json \
  --section YOUR_CONFIRMED_LEAF_ID --question '확정 본문에서 찾을 키워드' \
  --session-dir "$PDF_STUDY_SESSION" \
  --budget-ledger /absolute/private/shared/budget.sqlite \
  --key-project-root "$PWD"
```

결과의 `index_html`을 열면 원본·교정·후보와 로컬 문제를 볼 수 있습니다.
`proposal_path`에는 승인 값이 모두 false인 생성 계획이 있습니다. 같은 입력으로
준비를 반복하면 `unchanged`이고 기존 세션을 보존합니다. 입력·질문·절이 달라지면
새 세션을 사용해야 합니다. 의미 검토가 끝났다는 판정은 자동으로 만들지 않습니다.

계획이 만료됐다면 같은 입력으로 새 계획만 만듭니다. 기존 계획과 원장은 보존합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session plan \
  --session-dir "$PDF_STUDY_SESSION"
```

## 별도 승인 생성과 독립 검수

자료 전송·모델·비용 조건을 확인한 승인 파일을 제공해야 합니다. 아래 명령은
승인을 대신하지 않습니다. 기본 실행기는 기존 보수적 비용 예약과 누적 $10
정책을 사용합니다. 무료 계층을 자동 판정하지 않습니다. 운영자가 직접 확인한
증거와 실행 직전 키 프로젝트 검증을 결합한 무료 경로는 아래에서 별도로 선택합니다. 무료 한도가 남았다는 이유로 원장을 초기화하거나
실패 요청을 자동 재호출하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session generate \
  --session-dir "$PDF_STUDY_SESSION" \
  --approval /absolute/private/approvals/approved.json \
  --expected-plan-sha256 APPROVED_PLAN_SHA
```

성공 결과의 `result_path`를 독립적으로 원문과 대조합니다. `ContentReview`에는
결과 지문, 모든 설명·문제·해설 ID, 검토자, `accepted` 또는 `needs_changes`,
검토 메모가 필요합니다. 형식은 [MODEL_LESSON.md](MODEL_LESSON.md)에 있습니다.
`needs_changes` 또는 누락된 검수는 렌더링을 막습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session render \
  --session-dir "$PDF_STUDY_SESSION" \
  --content-review /absolute/private/reviews/content-review.json
```

결과의 `index_html`에 선택 절 제목·설명·문제·원문 위치를 표시합니다.
복구된 ID 결과를 검수했다면 `--result`로 그 파일을 명시할 수 있습니다.
반복 생성은 저장된 완료 결과를 반환하며 키를 다시 읽지 않습니다. 실패·불확실
작업은 소비된 상태로 남습니다. HTTP 오류의 안전 진단이 있으면 차단 응답의
`error_receipt`에서 확인할 수 있습니다. 동일 오류의 맹목적 재시도를 하지 않습니다.

Notion은 [NOTION_BRIDGE.md](NOTION_BRIDGE.md)의 별도 승인 계획·연결 호스트·읽기
확인을 사용합니다. 독립 실행 JSON-lines CLI는 호스트 입력을 기다리므로 단독 실행하면
게시를 완료할 수 없습니다. 기존 8개 학습 페이지와 사용자 메모를 이 세션이 수정하지 않습니다.

공개 회귀 테스트는 직접 작성한 합성 입력과 실제 SDK의 `MockTransport`만 사용하며
네트워크 연결을 금지합니다. 실제 계정 호출이나 새 Notion 게시를 검증한 테스트가 아닙니다.


## 확인한 무료 등급으로 준비

`prepare`에 `--free-evidence /absolute/private/free-tier-evidence.json`과
`--free-ledger /absolute/private/shared/free-runs.sqlite`를 함께 전달하면 검증된
무료 정책을 생성 계획과 세션 지문에 결합합니다. `--budget-ledger`에는 사용 이력이
있는 기존 유료 원장을 그대로 지정합니다. `plan`으로 계획을 갱신해도 이 정책과
원장 경로를 유지합니다. 이후 `generate`·`render` 명령은 동일합니다.

증거 필드와 승인된 프로젝트 검증기 계약은 [MODEL_LESSON.md](MODEL_LESSON.md)의
검증된 무료 실행을 따릅니다. 실행 전 PNG·검증기 지문, 증거 만료, 선택된 키의
프로젝트를 확인하고 무료 원장에 비용 0 receipt를 남깁니다. 기존 유료 원장은
읽기 전용이며 키 자동 교체·유료 fallback·한도 오류 자동 재시도는 없습니다.
완료된 저장 결과의 반복 사용에는 새 키 조회나 모델 호출이 필요하지 않습니다.

공개 테스트의 무료 경로도 합성 PNG·가짜 키·검증기와 SDK MockTransport를
사용합니다. 실제 무료 등급·청구 또는 Notion 게시 성공은 운영자가 별도의 실행
receipt와 완전한 게시 readback으로 확인해야 합니다.

새 PDF 추출·후보 목차부터 검수된 생성 결과의 실제 Notion 호스트 연결까지는
[REVIEWED_PDF_WORKFLOW.md](REVIEWED_PDF_WORKFLOW.md)의 순서와
`lesson_notion prepare/bind` 명령을 사용합니다.
