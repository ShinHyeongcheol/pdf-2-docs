# 실제 검토 파일의 로컬 규칙 빈칸 문제

이 명령은 **지정한 한 절의 확인된 텍스트로 결정적 규칙을 적용**합니다. 실제 모델 생성이 아닙니다.
API/.env/키를 읽지 않고 Notion 요청도 하지 않습니다. 외부 모델 시험은 계속 보류 상태입니다.
기존 합성 `quiz_cli`와 단일 승인 모델 CLI는 그대로 두고 별도 입력 연결을 제공합니다.

```sh
.venv/bin/python -m pdf_notion_mvp.quiz_files_cli \
  --source /absolute/private/ocr/document-ir.json \
  --outline /absolute/private/hierarchical-outline.json \
  --review /absolute/private/confirmed-review.json \
  --section '<목차 leaf ID>' \
  --output output/private-local-quiz.json
```

필수 입력은 source IR·전체 계층 목차·별도 교정층·한 leaf ID입니다. 기본 문제 수는 1개이고
`--max-questions 1|2|3`으로 최대 3개를 요청할 수 있습니다. 다른 절·부모 노드는 근거로 쓰지 않습니다.
문서 전체의 출처·블록 보존·목차·교정 lineage·OCR 래스터 검증을 먼저 통과해야 합니다.
OCR 에셋의 신뢰 범위는 source JSON 부모 폴더이며 다른 폴더로 넓히는 옵션은 없습니다.

확정 교정이 있는 OCR body만 사용하며 무교정 OCR·후보 교정·heading·표·코드·그림·파생 해설은
문제 근거에서 제외합니다. 합성 입력의 body는 작성한 근거로 취급합니다. 원본 IR과 OCR 전사는
덮어쓰지 않습니다. 결과마다 원본 전사와 학습용 effective text를 따로 보존합니다.

로컬 규칙은 원문 순서대로 근거를 살펴보고 2개 이상의 문자/한글 토큰이 있는 문장에서
2글자 이상의 첫 토큰을 정답으로 고릅니다. 전체 인용문에서 그 토큰의 첫 등장만 `[빈칸]`으로
바꾸고, 해설은 `근거 원문:` 뒤의 정확한 인용으로 제한합니다. 숫자만 있는 줄·단일 토큰·
기존 `[빈칸]` 표식·2,000자를 넘는 인용은 건너뜁니다. 질문/근거 중복도 건너뜁니다.
이 규칙은 문맥을 이해하거나 중요한 개념을 고르지 않습니다. 전혀 만들 수 없으면 더미 문제를
채우지 않고 `failed_human_review` 및 `no_eligible_local_rule_question`으로 결과를 표시합니다.
확정 근거 자체가 없거나 입력 검증이 실패하면 계획을 생성하지 않고 오류 종류만 출력합니다.

`output/private-local-quiz.json`은 **중립적 검토 계획**입니다. HTTP/native Notion payload와
호스트 실행 액션은 제공하지 않습니다. `plan.generation_mode=deterministic_cloze_rule`,
`provider=null`, `target=null`, `actual_model_generation=false`로 표시합니다. 기존 모의/provider
`QuizResult`로 자동 승격하거나 게시 경계에 전달하지 않습니다.

`plan.questions`에는 질문·정답·인용·해설, 문서 버전·블록 ID·절·페이지·좌표, 교정 ID와 raw/effective
텍스트가 있습니다. 원문/context/review digest와 규칙 버전으로 결정적 operation key를 만듭니다.
같은 입력의 재실행은 같은 계획을 만들며, 입력·근거·교정층이 바뀌면 해당 식별도 달라집니다.

계획 발급 전 원본에서 context를 재준비하고 기존 `verify_quiz`로 문제 수·인용·정답·빈칸 형태·
해설·절 근거·중복을 독립 검증합니다. `human_review_required=true`,
`semantic_correctness_verified=false`는 유지합니다. 검토층의 확정 상태는 신뢰하는 로컬 입력이며
이 명령이 OCR 전사나 기술적 주장의 진위를 새로 확인하거나 코드를 실행하지 않습니다.

파일 입력/에셋/출력 보호는 RAG 파일 입력 계약을 재사용합니다. 명시적 regular JSON(최대 16 MiB),
입력 링크/`.env` 이름 차단, trusted raster 검증, 입력/래스터 alias 덮어쓰기 차단을 적용합니다.
결과는 ignored `output/`의 JSON에 atomic replace로 저장합니다. 저장 실패 시 이전 결과는 남으므로
실패 실행 뒤에 이전 파일을 새로운 성공 결과로 해석하지 마세요. 원문·교정·문제·mapping은
공개 Git에 포함하지 마세요. 단일 작성자용이며 자동 게시·파일 잠금은 없습니다.

```sh
.venv/bin/python -m pytest tests/test_local_quiz.py -q
```

합성 테스트는 결정성·raw/effective 출처·다른 절/후보/새 주장/오인용 차단·짧거나 불가능한 근거·
중복·입력 보호·atomic 저장·키 접근 차단·OCR 모양 IR의 확정 전사만 사용을 검증합니다.
별도 비공개 162페이지 OCR IR의 기존 Model 절 확정 텍스트를 연결하여 작은 계획이 나오고
기존 인용/정답/해설/출처 검증을 통과함을 확인했습니다. 원문·실제 질문·매핑은 공개하지 않습니다.
이는 로컬 파일 연결/인용 충실도 증거이며 실제 모델·학습 품질·의미 정답 검증 결과는 아닙니다.
