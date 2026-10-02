# 전체 문서 로컬 감사와 검수 검색

`document_local_cli`는 기존 전체 IR·계층 목차·검토 레이어를 검증하고,
페이지 커버리지, 미확정 OCR/낮은 confidence/코드 후보, 표·그림 키워드
후보를 정리합니다. 원본 PDF SHA와 IR 버전을 비교할 수 있습니다.
실제 PDF 페이지 번호를 출처로 사용하며 슬라이드 인쇄 번호로 바꾸지 않습니다.
원본·기존 교정 상태를 변경하거나 confidence로 확정 승격하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.document_local_cli \
  --source /absolute/private/input/ocr/document-ir.json \
  --outline /absolute/private/input/hierarchical-outline.json \
  --review /absolute/private/input/review-layer.json \
  --pdf /absolute/private/input/original.pdf \
  --output-dir /absolute/private/work/local-document \
  --query RunnableSerializable --include-unreviewed
```

출력은 입력 파일의 폴더와 Git 밖이어야 합니다. `index.html`에서 페이지
커버리지·후보·검색을 읽고 `audit.json`, `search.json`, `batch-plan.json`으로
출처와 상태를 확인합니다. 미검수 검색은 명시 옵션일 때만 포함하며 생성 답변을
만들지 않습니다. 후보 교정의 proposed text는 검색 본문에 적용하지 않습니다.

`local_search`는 단어·문자 bigram BM25 순위를 계산합니다. 모델/임베딩을
호출하거나 다운로드하지 않으며 의미 검색을 구현했다고 표시하지 않습니다.
기본 검색은 확정 body/작성한 합성 근거만 포함합니다. 후보·코드 fragment는
기본 답변 근거에서 제외하고, 저장 인덱스는 현재 전체 입력과 비교합니다.

`LocalRunStore`는 입력·설정 fingerprint와 페이지별 payload digest를 SQLite에
보존합니다. 로컬 실패 후 다시 실행하면 성공 페이지는 재사용하고 실패한
페이지부터 처리합니다. 같은 페이지의 동시 실행은 한 번 처리하며 변경된
저장 결과를 거부합니다. 임의 사용자 SQLite를 덮어쓰지 않습니다.
이 로컬 재실행 규칙을 비용 있는 외부 호출에 적용하지 마세요. 외부 실행에는
별도의 승인·최종 요청 digest·비용 원장·불확실 결과 차단이 필요합니다.

`diagram_local`은 실제 PNG SHA·문서 버전·원본 좌표에 결합된 도표의
독립 시각 관찰 또는 OCR 후보를 받아, 모의 어댑터에서 그대로 인용하는
해설 계약을 검증합니다. 근거 없는 관찰/인용·인과 추론·외부 어댑터를 거부합니다.
관찰은 candidate로 남고 실제 vision 호출·기술 의미 정확성을 주장하지 않습니다.
PNG 헤더·원본 SHA 확인은 모든 이미지 픽셀의 의미 판독을 대신하지 않습니다.

`plan_batches`는 감사 digest와 정확한 페이지 커버리지에 결합된 최대8쪽 계획을
만들고 이미 검수한 페이지를 `--reuse-page`로 재사용하도록 표시합니다.
외부 실행 승인은 false입니다. 기존 생성 자료는 별도 결과 digest와 내용 검토를
확인한 뒤 재사용해야 하며, 페이지 목록만으로 생성 검수를 완료했다고 취급하지 않습니다.

로컬 테스트는 소켓을 차단하고 합성 입력만 사용합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_document_local.py tests/test_diagram_local.py -q
```

원본 래스터 보존과 목차 커버리지가 통과해도 모든 OCR 철자·표 행/열·코드
구문·그림 화살표·최신 기술 사실이 확정되는 것은 아닙니다. 자동 후보 분류는
놓치는 영역이 있을 수 있고, 실제 그래프 설명과 전 문서 생성·Notion 반영은
별도 외부 승인·검증 경로에서 이어집니다. 키를 읽거나 외부 API를 호출하지 않습니다.
