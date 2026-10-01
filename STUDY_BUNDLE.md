# 비공개 오프라인 절 학습 묶음

기존 source IR / 전체 계층 목차 / 검토 레이어에서 **한 leaf 절**을 읽을 수 있는 정적 HTML로 연결합니다. 원본 페이지 PNG, OCR 전사, 확정 교정, 미확정 제안, 파생 코드·표·텍스트의 검토 상태, 로컬 빈칸 규칙 문제, 로컬 키워드 검색의 인용답변을 구분합니다. 같은 JSON에는 문서 버전, 블록 ID, 페이지·좌표, 교정 ID와 원본/유효 텍스트가 남습니다. 코드의 실행·기술 정확성이나 문장의 의미를 새로 검수하지 않습니다.

프로젝트의 격리 환경에서 실행합니다. 세 입력은 기존 검증 경로와 동일한 명시적 JSON입니다. OCR 입력에서는 source JSON의 부모가 신뢰하는 이미지 루트이며 전체 IR의 래스터 SHA/출처 검증을 먼저 통과해야 합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_bundle \
  --source /private/input/ocr/document-ir.json \
  --outline /private/input/planning/hierarchical-outline.json \
  --review /private/input/planning/review-layer.json \
  --section YOUR_LEAF_ID \
  --question '확정된 본문에서 찾을 키워드' \
  --output-dir /private/study-output
```

`--output-dir`은 절대 경로이며 모든 입력 폴더와 Git 체크아웃 밖에 있어야 합니다. symlink 경로를 허용하지 않습니다. 출력은 `study-<내용 SHA>/index.html`, `study.json`, `manifest.json`, `assets/*.png`입니다. CLI가 알려주는 `index_html`을 브라우저에서 직접 여세요. 서버, JavaScript, 외부 스타일, 키, 모델·임베딩 API, Notion 연결이 필요 없습니다. 이미지는 검증한 원본 페이지 바이트를 그대로 복사하며 편집하지 않습니다. 합성 fixture의 이미지 참조는 실제 이미지가 없는 자리로 표시합니다.

같은 입력·절·질문으로 다시 실행하면 파일 바이트와 manifest를 확인하고 `unchanged`를 반환합니다. 수정·누락·추가 파일이나 symlink/hardlink가 있는 기존 묶음은 사람의 검토가 필요한 충돌로 중단하고 보존합니다. 다른 질문이나 검토 자료는 별도 묶음이 됩니다. 개인 메모는 생성 디렉터리 밖에 보관하세요. 새 출력은 임시 디렉터리에 완성한 뒤 이동합니다. 중단된 임시 디렉터리는 재실행의 완성된 결과로 사용하지 않습니다. 로컬 단일 실행 경로이며 다중 프로세스 잠금이나 장기 작업 서버는 구현하지 않았습니다.

범위와 한계:

- 전체 IR/목차/검토를 검증하지만 **선택한 절의 읽기 출력**만 만듭니다. 전체 문서 변환·의미 검수·학습 품질 완료를 뜻하지 않습니다.
- 퀴즈와 RAG는 기존 확정 본문만 사용하며 후보·파생 코드·표는 정답 근거로 승격하지 않습니다. 규칙 문제는 기본 1개입니다. 적합한 질문이 없으면 실패 상태를, 키워드 근거가 없으면 `unknown`을 표시합니다.
- 인용의 문자열·출처 계약을 검증합니다. 답변은 모의 인용 조립이며 실제 모델의 설명이나 의미 판단이 아닙니다. 이미지에서 그래프를 해석하거나 OCR을 다시 실행하지 않습니다.
- OCR 페이지 복사는 PNG로 제한합니다. 모든 원본 데이터와 출력은 공개 저장소에 넣지 마세요.

## 최종 Notion 연결에 남은 작업

목적지 학습 허브/절 페이지 binding, 원본 이미지 업로드 방식과 공개 범위, 읽기 순서·검토 표시를 보존하는 제한된 게시 계획, 작업 소유권·중복 방지·읽기 확인을 이어야 합니다. 현재 Notion MCP 경로는 작성한 mock quiz 전용입니다. 이 학습 묶음이나 로컬 규칙/RAG 결과를 게시 입력으로 자동 전환하지 않습니다. 후보와 모의 결과의 표시를 유지하는 검토 가능한 게시 어댑터가 필요합니다. 이번 경로는 Notion 읽기·쓰기를 수행하지 않습니다.

검증:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_study_bundle.py -q
PYTHONPATH=src .venv/bin/python -m pytest -q
```

공개 테스트는 작성한 합성 fixture와 작은 PNG 바이트만 사용합니다. 실제 문서 확인은 비공개 출력의 파일 바이트, 출처 계보, 재실행, 입력 보존에 한정합니다.
