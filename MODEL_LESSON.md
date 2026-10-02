# 한 절의 실제 모델 학습 노트

`lesson_generation`은 기존 IR·전체 목차·확정 교정 레이어에서 한 leaf 절의 텍스트와 출처만 준비합니다. 코드·후보 fragment를 정답 근거에서 제외하고, 확정 표의 셀 관계는 별도 텍스트 단위로 보존합니다. `--exclude-page`로 예제 출력 등 학습 사실로 사용할 수 없는 페이지를 명시적으로 제외합니다. 원본 이미지·파일 경로·키는 모델 요청에 담지 않습니다.

현재 모델은 Gemini 3.1 Flash-Lite로 고정합니다. 한 승인당 요청 1회, 자동 재시도 없음, 출력 8,192토큰·최종 요청 100,000바이트·HTTP 단계별 timeout 60초입니다. 텍스트/구조화 출력과 standard 가격은 기존 `ModelSnapshot`으로 검증합니다. 전체 모델 입력·출력 한도로 계산한 한 회의 보수적 비용 예약은 $0.360448이며 실제 예상 청구액이나 계정 강제 상한이 아닙니다.

승인에는 입력 파일 SHA, 선택 절·제외 페이지, 실제 최종 SDK 요청 digest, 모델/가격/한도와 비공개 출력·비용 원장·키 프로젝트 루트가 결합됩니다. plan은 키나 모델을 읽지 않으며 승인 값은 false로 시작하고 1시간 유효합니다. 별도로 확인한 모델·자료 전송·비용 승인이 있어야 live를 실행할 수 있습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.lesson_generation --mode plan \
  --source /absolute/private/ocr/document-ir.json \
  --outline /absolute/private/planning/hierarchical-outline.json \
  --review /absolute/private/planning/review-layer.json --section YOUR_LEAF_ID \
  --exclude-page 26 --exclude-page 27 \
  --output-dir /absolute/private/model-execution/results \
  --budget-ledger /absolute/private/model-execution/budget.sqlite \
  --key-project-root "$PWD"
```

예시 페이지 번호는 실제 선택 절에 맞게 바꾸세요. 결과·원장·승인 파일은 Git과 입력 폴더 밖의 정규 절대 경로에 보관합니다. 출력·비용 원장·키 루트를 바꿔 같은 승인을 재사용할 수 없습니다.

비용 원장은 SQLite의 단일 트랜잭션으로 최대 누적 $10을 예약하고 실패도 환불하지 않습니다. 이전 실제 합성 실행은 `seed_previous_run(ledger, completed_receipt)`로 동일 공유 원장에 한 번 포함해야 합니다. 주입된 mock 원장은 실제 비용으로 seed할 수 없습니다. 모든 실제 호출에서 이 같은 원장을 사용하세요. 계정의 다른 앱 호출이나 원장 삭제·복사까지 방지하는 전역 결제 상한은 아닙니다.

별도 승인을 확인한 제안의 네 boolean만 true로 저장하고 아래를 실행합니다. 이 명령 자체는 승인을 대신하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.lesson_generation --mode live \
  --approval /absolute/private/model-execution/approved.json \
  --expected-plan-sha256 APPROVED_PLAN_SHA \
  --output-dir /absolute/private/model-execution/results \
  --budget-ledger /absolute/private/model-execution/budget.sqlite \
  --key-project-root "$PWD"
```

API 전송 전에 최종 HTTPS host/model path·전체 request digest·요청 크기·timeout을 확인합니다. 모델 호출은 source에 결합된 operation key로 영속 예약됩니다. 같은 작업의 완료 결과는 digest·현재 출처를 재검증하고 반환하며 키를 다시 읽지 않습니다. 실패·타임아웃·불확실 결과는 자동 재호출하지 않습니다. SDK usage token과 가격 기반 추정치, 보수적 누적 예약액, 관측 요청 수를 기록하고 실제 청구 확인은 false로 유지합니다.

생성 내용은 3–6개 주제, 주제당 1–3개 설명, 3–5개 빈칸/짧은 답 문제입니다. 모든 설명과 해설의 인용은 제공한 단위의 정확한 부분 문자열이어야 하고, 정답은 문제 근거 인용의 일부여야 합니다. 출처 단위에는 버전·원본 블록 ID·페이지·좌표·교정 ID를 보존합니다. 이 검증은 의미·코드 실행의 정확성을 보장하지 않습니다.

`ContentReview`에 결과 digest, 모든 설명·문제·해설 ID, 독립 대조자·판정·주의 사항을 기록한 뒤 `write_reading_bundle(..., budget_path=shared_ledger)`로 제작합니다. 원래 실행 원장과 현재 출처, 모든 ID를 확인하고 통과한 자료만 `index.html`·`lesson.json`·원본 확인용 `source.html`/`study.json`·원본 PNG·manifest로 만듭니다. 설명·문제는 HTML을 escape하고 외부 자원이나 스크립트를 사용하지 않습니다. 동일 자료 재제작은 파일과 수정 시간을 보존하고 수정·누락된 기존 결과는 덮어쓰지 않습니다.

Notion을 호출하거나 이미지·전체 PDF를 모델에 보내지 않습니다. 사용자 확인 전 상태를 유지하며, 전 문서 의미 검수·최신 API 일반 정확성·실제 Notion 게시 완료로 표시하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_lesson_generation.py -q
```

공개 테스트는 작성한 합성 fixture와 가짜 키/실제 SDK MockTransport만 사용합니다. 실제 입력·승인·키·생성 내용·검토·비용 원장은 공개 Git에 포함하지 않습니다.

Provider requests use a compact JSON schema without titles or length/count bounds.
Full Pydantic length/count limits still apply after parsing, before any result can
be accepted. An allowlisted error category is retained for rejected requests;
provider exception bodies and credential-bearing messages are never persisted.
A failed operation stays consumed and costed. A changed, approved request is a
new operator execution, not an automatic retry or a budget refund.
