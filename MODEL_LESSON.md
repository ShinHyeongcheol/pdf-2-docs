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

생성 내용은 근거 있는 학습 목표에 대응하는 1–6개 주제, 주제당 1–3개 설명, 3–5개 빈칸/짧은 답 문제입니다. 이전 구현의 최소 3개 주제는 짧은 절에도 동일한 개수를 강제하는 제약이었습니다. 짧은 자료의 센서 설정과 체크포인트처럼 두 목표로 구성할 수 있도록 고정 최소 개수를 제거했습니다. 빈 응답·빈 근거·중복 주제는 거부하며, 목표의 의미 있는 구분과 원문 핵심 범위의 충분한 설명은 독립 내용 검수에서 판단합니다. 주제를 억지로 나누거나 반복해 개수를 채우지 않습니다. 모든 설명과 해설의 인용은 제공한 단위의 정확한 부분 문자열이어야 하고, 정답은 문제 근거 인용의 일부여야 합니다. 출처 단위에는 버전·원본 블록 ID·페이지·좌표·교정 ID를 보존합니다. 이 검증은 의미·코드 실행의 정확성을 보장하지 않습니다.

`ContentReview`에 결과 digest, 모든 설명·문제·해설 ID, 독립 대조자·판정·주의 사항을 기록한 뒤 `write_reading_bundle(..., budget_path=shared_ledger)`로 제작합니다. 원래 실행 원장과 현재 출처, 모든 ID를 확인하고 통과한 자료만 `index.html`·`lesson.json`·원본 확인용 `source.html`/`study.json`·원본 PNG·manifest로 만듭니다. 설명·문제는 HTML을 escape하고 외부 자원이나 스크립트를 사용하지 않습니다. 동일 자료 재제작은 파일과 수정 시간을 보존하고 수정·누락된 기존 결과는 덮어쓰지 않습니다.

Notion을 호출하거나 이미지·전체 PDF를 모델에 보내지 않습니다. 사용자 확인 전 상태를 유지하며, 전 문서 의미 검수·최신 API 일반 정확성·실제 Notion 게시 완료로 표시하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_lesson_generation.py -q
```

공개 테스트는 작성한 합성 fixture와 가짜 키/실제 SDK MockTransport만 사용합니다. 실제 입력·승인·키·생성 내용·검토·비용 원장은 공개 Git에 포함하지 않습니다.

Provider requests omit metadata titles and string length bounds, while retaining
array count bounds so the provider schema and local Pydantic contract agree.
Full Pydantic limits still apply after parsing, before any result can
be accepted. An allowlisted error category is retained for rejected requests;
provider exception bodies and credential-bearing messages are never persisted.
A failed operation stays consumed and costed. A changed, approved request is a
new operator execution, not an automatic retry or a budget refund.

HTTP 200 응답은 SDK의 JSON 파싱과 Pydantic 검증 전에 `OPERATION-provider.json`에
0600 권한으로 배타 저장하고 지문을 비용 원장에 결합합니다. 실패 시에도
원응답과 실패 receipt를 보존하므로 추가 API 호출 없이 원인과 원문 근거를
검토할 수 있습니다. 기존 응답은 덮어쓰지 않으며, HTTP 오류 본문·요청 헤더·키는
저장하지 않습니다. 원응답의 보존은 생성 내용의 승인이나 자동 재시도를 뜻하지 않습니다.
HTTP 200 본문이 선택된 키를 되돌려 보내는 경우에는 JSON escape도 확인해
키를 삭제한 안전 사본만 보존하고 해당 요청을 소비된 실패로 처리합니다.
키가 포함된 원문이나 base64 사본은 보존하지 않습니다.

HTTP 오류 응답은 원문 대신 `OPERATION-error.json`에 HTTP 상태, 허용된 Google
오류 상태·ErrorInfo 사유, 승인 요청에 있는 필드 식별자, 알려진 quota metric과
짧은 retry delay만 기록합니다. 메시지·설명·임의 metadata·프로젝트 ID·헤더는
저장하지 않습니다. 이 파일도 0600·배타 저장·원장 지문 결합을 사용하고 실패는
소비된 상태와 기존 비용 예약을 유지합니다. retry delay를 기록해도 자동 재시도는
하지 않습니다. free-tier quota metric이 관측되어도 계정의 무료 등급이나 무료
한도 소진을 확정하지 않습니다. 계정 등급은 별도로 확인해야 합니다.
이 기록이 없던 과거 HTTP 400의 상세 원인은 소급 복원할 수 없습니다.


`recover_positional_ids(result_path, budget_path=shared_ledger)`는 인용·정답 등
검증은 통과했지만 설명 ID만 중복된 저장 결과에 한해 API 없이 복구합니다.
원래 실패 파일을 보존하고 원장 digest/요청 1회를 확인한 뒤 위치 기반 ID만
변경한 별도 파일과 before/after 이력을 저장합니다. 텍스트·인용은 바꾸지 않고,
다른 검증 실패·출처 변경·원장 불일치는 거부합니다. 비용 예약은 유지하며
복구된 결과도 완전한 독립 내용 대조를 거쳐야 읽기 묶음을 만들 수 있습니다.
