# 새 PDF 한 절을 읽고 검수해 Notion에 게시하기

현재 지원 경로는 **로컬 PDF 추출 → 사람이 목차·전사 확인 → 승인된 한 절 생성 →
독립 원문 검수 → 읽기 HTML → 연결된 Notion 호스트의 단일 생성 → readback**입니다.
일반 PDF 전체를 무인으로 교재화하는 실행기는 아닙니다. 자동 후보와 검수 판정을
구분하며 기존 결과·메모를 수정하지 않고 새 직계 하위 페이지만 준비합니다.

2026-10-03 Mac 확인에서는 실제 162쪽 LangChain PDF의 전체 OCR/PNG와 원문 SHA를
검증하고, Model 절의 기존 실제 생성 receipt·출처·독립 검수를 재사용했습니다.
대표 절의 첫 요약 게시를 확인한 뒤, 기존 좋은 교재의 설명을 재사용·편집하여
Model을 정의·호출·설정/평가로 연결한 교재로 개선했습니다. 원본 p.23–28 이미지
6장과 보충 도식, 메모를 보존하고 본문 전체를 다시 대조했습니다. 이 편집은
검수된 기존 설명의 재사용이며 새 모델 생성 결과가 아닙니다. 합성 2쪽 PDF의 실제
생성·검수·게시도 별도로 확인했습니다. 새 설명 계약은 SDK의 합성 HTTP 요청으로
검증했으며, 새 스키마의 실제 Gemini 수용 여부는 아직 확인하지 않았습니다.
비공개 실제 페이지 링크와 원장은 공개 저장소에 포함하지 않습니다.

## 1. 새 비공개 작업 폴더와 입력

현재 Mac 체크아웃과 가상 환경을 사용합니다. `$PDF_STUDY_ROOT`는 Git과 입력
PDF 폴더 밖의 새 **정규 절대 경로**여야 합니다. 같은 문서의 재실행에서는 보존하고,
다른 문서·절·질문은 새 세션을 사용합니다. 기존 공유 비용 원장과 키 설정은 유지하세요.

```sh
cd ~/Desktop/pdf-2-docs
export PDF_STUDY_ROOT="$HOME/Documents/pdf-notion-work/my-new-pdf"
export PDF_STUDY_PDF="/absolute/path/to/input.pdf"
mkdir -p "$PDF_STUDY_ROOT"
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.pdf_inspect \
  "$PDF_STUDY_PDF" --output "$PDF_STUDY_ROOT/inspection.json"
```

검사 종료 코드 2는 image-only/mixed/empty 페이지 검토가 필요하다는 뜻입니다.
그 경우 native 명령으로 강행하지 마세요. 회전·암호화·잘못된 폰트 매핑도
native 지원 범위 밖입니다.

**텍스트 전용 PDF**: 암호화·회전 없는 읽을 수 있는 텍스트 페이지에 사용합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.native_pdf \
  "$PDF_STUDY_PDF" --assets "$PDF_STUDY_ROOT/ingest/assets" \
  --output "$PDF_STUDY_ROOT/ingest/source.json"
export PDF_STUDY_SOURCE="$PDF_STUDY_ROOT/ingest/source.json"
```

**이미지/혼합 PDF**: Mac Apple Vision 경로를 사용합니다. OCR 결과는 후보이며,
표·코드·도표를 자동으로 확정하지 않습니다. Vision IPC가 허용된 Mac 환경이 필요합니다.

```sh
swiftc scripts/vision_ocr.swift -o "$PDF_STUDY_ROOT/vision-ocr"
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.ocr_cli \
  "$PDF_STUDY_PDF" --work-dir "$PDF_STUDY_ROOT/ocr" \
  --vision-binary "$PDF_STUDY_ROOT/vision-ocr" --pages all
```

OCR 명령의 마지막 JSON에서 `output` 경로를 `PDF_STUDY_SOURCE`로 지정합니다.
전체 페이지 처리 결과가 필요합니다. 동일 PDF·dpi의 기존 PNG/페이지 JSON은
재사용하며, 경로를 옮기면 에셋 참조와 모든 후속 지문을 다시 검토해야 합니다.

## 2. 목차와 전사 검토

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.review_draft \
  --source "$PDF_STUDY_SOURCE" --output-dir "$PDF_STUDY_ROOT/review"
export PDF_STUDY_OUTLINE="$PDF_STUDY_ROOT/review/outline.json"
export PDF_STUDY_REVIEW="$PDF_STUDY_ROOT/review/review.json"
```

이 명령은 페이지별 leaf를 가진 **candidate 초안**만 만듭니다. 원본 페이지 PNG를
읽고 의미 있는 절로 leaf를 나누거나 합치며 제목·읽기 순서·페이지와 블록 소유권을
확인하세요. 모든 원본 블록은 원래 IR 순서로 정확히 한 번 포함해야 합니다.
부모는 자식보다 먼저, 자식은 트리 순서로 배치합니다. 부모의 페이지는 자식의
합집합이며 부모에는 block_ids를 넣지 않습니다. 충분히 대조한 노드만 confirmed로
바꿉니다. 초안 작성기를 재실행해 편집한 JSON을 덮어쓰지 않습니다.

native 줄의 표 셀·코드·헤딩도 원본과 대조해야 합니다. 코드 영역은 `fragments`에
kind=code로 표시하여 모델 정답 근거에서 제외하고, 표는 같은 절의 source_block_ids를
정확히 연결해 kind=table로 묶습니다. 후보 fragment는 생성 근거가 아닙니다.
OCR 본문은 같은 페이지 PNG·좌표에 연결된 **confirmed Correction**이 있어야
생성 근거에 들어갑니다. 올바른 전사도 original_text와 proposed_text를 같게 적고
독립 대조자·근거 이미지/좌표를 기록할 수 있습니다. confidence만으로 확정하지 않습니다.
계약과 합성 예시는 [review.py](src/pdf_notion_mvp/review.py)와 `fixtures/`에 있습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.review_cli "$PDF_STUDY_SOURCE" \
  --outline "$PDF_STUDY_OUTLINE" --layer "$PDF_STUDY_REVIEW" \
  --output "$PDF_STUDY_ROOT/validation/reviewed.json"
```

선택할 leaf ID를 `PDF_STUDY_SECTION`에 지정합니다. `page-0001`은 초안 ID 예시이며,
사람이 묶은 절 ID를 사용하는 것이 좋습니다. 전체 목차 검토가 끝나야 새 생성 세션을
준비할 수 있습니다. 원문 검색 질문은 확정 근거에서 찾을 짧은 키워드입니다.

## 3. 비용 없는 준비와 별도 승인 생성

```sh
export PDF_STUDY_SECTION="YOUR_CONFIRMED_LEAF_ID"
export PDF_STUDY_QUESTION="확정 본문의 키워드"
export PDF_STUDY_BUDGET="/absolute/private/shared/budget.sqlite"
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session prepare \
  --source "$PDF_STUDY_SOURCE" --outline "$PDF_STUDY_OUTLINE" \
  --review "$PDF_STUDY_REVIEW" --section "$PDF_STUDY_SECTION" \
  --question "$PDF_STUDY_QUESTION" --session-dir "$PDF_STUDY_ROOT/session" \
  --budget-ledger "$PDF_STUDY_BUDGET" --key-project-root "$PWD"
```

출력의 `index_html`로 원자료를 읽고, `proposal_path`에서 선택 근거·제외 페이지·
모델·실제 요청 지문·비용·만료를 검토합니다. 예제 모델 출력은 해당 절에 속한
`--exclude-page N`으로 명시적으로 제외하세요. 이미지와 파생 코드는 모델에 보내지
않습니다. 근거는 최대 200단위·75,000바이트, 최종 요청은 100,000바이트입니다.
넘으면 목차의 의미 있는 leaf 범위를 줄입니다. 반복해서 잘라 자동 호출하지 않습니다.

**현재 유료 예약 상한을 이유로 새 유료 호출을 하지 마세요.** 직접 확인한 현재
Free 프로젝트의 PNG 증거와 승인된 키/프로젝트 검증기가 있을 때만 위 prepare에
`--free-evidence /absolute/private/free-evidence.json`과
`--free-ledger /absolute/private/shared/free-runs.sqlite`를 함께 넣습니다.
무료 경로도 기존 유료 원장을 읽기 전용으로 유지합니다. 증거와 검증기 계약은
[MODEL_LESSON](MODEL_LESSON.md)을 따릅니다. 키 교체는 사용자가 직접 수행합니다.

사용자가 자료 전송·모델·비용 정책을 승인한 범위에서만 별도 approved.json에 승인
네 필드를 기록합니다. 명령이 사용자의 승인을 대신하지 않습니다. 만료된 계획은
`study_session plan --session-dir ...`으로 새로 검토해야 합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session generate \
  --session-dir "$PDF_STUDY_ROOT/session" --approval /absolute/private/approved.json \
  --expected-plan-sha256 APPROVED_PLAN_SHA
```

출력의 result_path를 `PDF_STUDY_RESULT`로 지정합니다. HTTP 오류·절단·타임아웃·
불확실 결과는 소비된 상태로 남습니다. 자동 재시도·유료 fallback·키 교체는 없습니다.
중복 설명 ID만 실패한 경우의 좁은 API 없는 복구는 MODEL_LESSON을 따릅니다.
새 자료의 근거 오류를 과거 캐시나 손으로 만든 성공 상태로 우회하지 않습니다.

## 4. 독립 내용 검수와 읽기 자료

각 설명·문제·해설을 원본 PNG와 확정 전사에 대조합니다. 질문의 의미·정답·
인용의 충분성·핵심 목표의 구분·누락·교정/표 관계를 확인하세요. 문자열 검사나
같은 생성 모델의 자체 평가는 독립 검수가 아닙니다.
`ContentReview` JSON의 result_digest는 `fingerprint(result)`이고, reviewed_ids에는
정의·필요성·작동 원리·예시·자료 읽기·오해 설명·질문의 모든 검수 ID를 넣습니다.
설명 중심 교재에서는 pedagogy_checks의 다섯 항목도 각각 실제로 검토해야 합니다.
reviewer, decision(accepted 또는 needs_changes), notes도 필요합니다.
검수가 끝나기 전에는 accepted로 표시하지 않습니다.

공식 계약으로 미검수 초안을 만들려면 다음을 실행합니다. 새 파일만 만들며,
reviewed_ids는 비어 있고 decision은 needs_changes입니다. 콘솔에 출력된 ID를
하나씩 대조한 뒤 JSON을 편집하세요.

```sh
export PDF_STUDY_CONTENT_REVIEW="$PDF_STUDY_ROOT/content-review/review.json"
PYTHONPATH=src .venv/bin/python - "$PDF_STUDY_RESULT" "$PDF_STUDY_CONTENT_REVIEW" <<'PY'
import json, sys
from pathlib import Path
from pdf_notion_mvp.lesson_generation import ContentReview, fingerprint, parse_draft, draft_review_ids
from pdf_notion_mvp.instructional_lesson import PEDAGOGY_CHECKS
from pdf_notion_mvp.rag_files import read_json_input
result = read_json_input(Path(sys.argv[1]), max_bytes=500000)
review = ContentReview(result_digest=fingerprint(result), reviewed_ids=[],
    reviewer="검토자 이름을 기록하세요", decision="needs_changes", notes=[])
path = Path(sys.argv[2]); path.parent.mkdir(parents=True, exist_ok=True)
with path.open("x", encoding="utf-8") as stream:
    stream.write(review.model_dump_json(indent=2) + "\n")
ids = draft_review_ids(parse_draft(result["draft"]))
print(json.dumps({"ids_to_compare": ids, "pedagogy_to_compare": sorted(PEDAGOGY_CHECKS), "review_template": str(path)}, ensure_ascii=False))
PY
```

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.study_session render \
  --session-dir "$PDF_STUDY_ROOT/session" --result "$PDF_STUDY_RESULT" \
  --content-review "$PDF_STUDY_CONTENT_REVIEW"
```

기존 검수된 캐시는 새 세션 없이 아래 게시 prepare에 바로 넣을 수 있습니다.
현재 원문·출처·review digest와 **원래 실행 receipt**를 재검증하며 모델을 부르지 않습니다.
다른 문서의 캐시나 편집된 생성 결과는 사용할 수 없습니다.

## 5. 연결된 Notion 호스트와 게시 확인

이 단계는 **Notion 앱 도구가 연결된 Codex/ChatGPT 호스트**가 수행해야 합니다.
터미널만으로 연결된 앱 인증을 가져올 수 없으며 새 Notion 토큰을 요구하지 않습니다.
먼저 호스트에서 `notion://docs/enhanced-markdown-spec`을 읽고 지정 허브를 fetch합니다.
완전한 원본 MCP 결과를 `$PDF_STUDY_ROOT/host-input/hub.json`에 저장하세요.
truncated/unknown_block 결과는 사용할 수 없습니다.

원본 페이지마다 `notion_create_file_upload`의 한 번짜리 URL과 모든 반환 헤더로
multipart POST(file 필드)를 한 번 전송합니다. 이미 첨부한 업로드 ID는 기존 기록을
재사용할 수 있습니다. 페이지 PNG의 SHA와 source page 번호·반환 file_upload_id·
filename을 다음 배열 형태로 `host-input/images.json`에 기록합니다. 실제 값만 사용하세요.

```json
[{"page":1,"sha256":"SOURCE_PNG_SHA256","file_upload_id":"RETURNED_UUID","filename":"source-p1.png"}]
```

```sh
export PDF_STUDY_HUB="DESIGNATED_HUB_UUID"
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.lesson_notion prepare \
  --result "$PDF_STUDY_RESULT" --content-review "$PDF_STUDY_CONTENT_REVIEW" \
  --budget-ledger "$PDF_STUDY_BUDGET" --question "$PDF_STUDY_QUESTION" \
  --reading-dir "$PDF_STUDY_ROOT/reading" --hub-id "$PDF_STUDY_HUB" \
  --hub-packet "$PDF_STUDY_ROOT/host-input/hub.json" \
  --images "$PDF_STUDY_ROOT/host-input/images.json" \
  --checkpoint "$PDF_STUDY_ROOT/publication/checkpoint.json" \
  --action-output "$PDF_STUDY_ROOT/publication/action.json"
```

pending checkpoint가 저장된 뒤 action_prepared가 반환됩니다. 호스트는 action.json을
그대로 **notion_create_pages에 정확히 한 번** 전달합니다. 기존 페이지 update는
사용하지 않습니다. 반환된 새 페이지 ID를 즉시 결합합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.lesson_notion bind \
  --checkpoint "$PDF_STUDY_ROOT/publication/checkpoint.json" --page-id RETURNED_PAGE_UUID
```

호스트가 새 페이지를 fetch해 완전한 MCP 결과를 `host-input/readback.json`에 저장한
뒤, 위 prepare 명령에 `--remote-packet "$PDF_STUDY_ROOT/host-input/readback.json"`을
추가하여 다시 실행합니다. confirmed / action_path:null이어야 게시 확인이 끝납니다.
이 검사는 결합된 페이지 ID·제목·직계 부모·본문·코드·인용·페이지 출처·그림 파일 참조를 비교합니다.
이미지 원격 바이트의 SHA 비교는 호스트가 따로 수행해야 합니다.

반복 실행에서는 원격을 **다시 fetch**하고 같은 prepare+remote 명령만 실행합니다.
새 action이 없으므로 생성 호출 0건입니다. 사용자는 사용자 메모 영역을 편집할 수 있습니다.
본문이 달라지면 덮어쓰지 않고 중단합니다. 생성 응답 유실 시 pending을 보존하고,
허브에서 고유 제목과 내용을 사람이 확인해 page ID를 결합하세요. 새 페이지를
자동 생성하지 않습니다. 한 로컬 checkpoint를 한 호스트에서 사용하며 분산 잠금은 없습니다.

## 완료와 남는 수동 단계

현재 완료 가능한 범위는 지원 PDF 한 절의 **사람이 검토하는 학습 생성·게시**입니다.
필수 수동 단계는 목차/전사·표/코드 분류, 자료 전송 승인과 현재 무료 등급 확인,
원문과 생성 결과의 독립 대조, 연결 호스트의 이미지 업로드·단일 create·fetch입니다.
CLI는 이 작업의 계약·파일·readback·재실행을 검증합니다. 162쪽 전체 캐시를 다시
생성할 필요는 없고, 원래 receipt와 검수가 유효한 대표 절은 즉시 재사용할 수 있습니다.
임의 PDF의 정확한 전역 목차·수식/다단 읽기 순서·모든 OCR 철자·기술 사실·코드 실행,
수치 그래프 의미 판독, 무인 호스트·분산 운영은 지원 완료로 표시하지 않습니다.


## 학습 화면과 내부 감사 기록

학습 페이지의 일반 원문·근거는 문단과 인용으로, 출처는 `원문 pN`으로 표시합니다.
검토한 표는 Notion 표로, 실제 코드만 코드 블록으로 렌더링합니다. 원문의 숫자
제목과 불릿은 인용 문단에 보존하여 Notion 자동 목록의 재번호 매김을 막습니다.
원문 교정은 원문 전사와 확정/후보 상태를 읽는 글로 구분합니다.
JSON·unit_id·좌표 객체·SHA·동기화 마커는 사용자 본문에 넣지 않습니다.

문서 버전·블록 ID·좌표·정확 인용·생성 receipt·검수 ID는 기존 로컬 JSON과
reading manifest 및 비공개 checkpoint에 그대로 남습니다. 원격 재실행은 로컬
페이지 ID와 기대 본문을 읽기 결과에 대조하며, 보이는 소유 마커가 필요하지 않습니다.
첫 생성 뒤 `bind`는 필수입니다. 이전 형식의 confirmed checkpoint는 보존하고,
형식 변경은 해당 페이지에 대한 별도 명시적 편집과 새 검증 기록으로 처리합니다.
새 checkpoint나 새 폴더를 만들어 기존 페이지를 자동 재생성하지 않습니다.


## 설명 중심 교재의 기본 계약

새 `study_session prepare`와 `lesson_generation --mode plan`의 기본 형식은
`instructional_v1`입니다. 묶인 학습 단원마다 정의 → 필요성 → 작동 원리 →
입력·기대 결과·해석이 있는 가상 예시 → 오해하기 쉬운 점을 본문에 연결합니다.
문제는 맨 뒤의 짧은 이유·적용 질문입니다. 원문 전사와 감사 기록은 보조 자료입니다.
확정 코드·표는 가까운 단원에 원본 이미지와 함께 배치합니다. 이 자동 배치가
의미상 적절한지는 검토자가 확인해야 하며, 미확정 코드는 본문에서 원본 이미지로
확인하고 후보 전사는 접힌 보조 자료에 보존합니다. 코드 실행은 검증하지 않습니다.

모든 제공 근거가 본문 검수 대상에 포함되어야 합니다. 이는 인용/범위 검사이며,
설명의 충분성이나 기술적 사실을 자동 검증하지 않습니다. 독립 검토자는 결과 ID뿐
아니라 `concept_coverage`, `connected_explanation`, `worked_examples`,
`source_and_supplement_labels`, `code_and_visual_interpretation`도 실제로 대조한 뒤
`pedagogy_checks`에 기록합니다. 비어 있거나 누락되면 게시 준비가 차단됩니다.
특히 Model 절에서는 Runnable/Chain, Chat/Completion, 초기화/호출/응답, 동기·비동기,
파라미터, 모델 교체, 캐싱·재시도를 본문에서 설명하는지 직접 확인하세요.

과거 `summary_v1` receipt와 요청 지문은 유지합니다. 과거 세션의 plan/캐시 재사용은
자동으로 새 형식으로 바뀌지 않습니다. 기존 짧은 요약을 새 설명 중심 결과로
간주하지 않습니다. 짧은 요약을 의도한 새 실험에만 `--lesson-format summary_v1`을
명시하세요. 형식 변경은 새 요청 지문과 별도 승인 대상이며, 기존 호출 한도·비용
상한·이미지 전송 금지·무재시도 정책은 그대로 적용됩니다. 가상 예시는 학습용으로
표시하며 새 API/성능/외부 사실은 만들어 넣지 않습니다.
