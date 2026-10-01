# 기능과 계약

이 MVP는 합성 IR 또는 로컬 OCR 후보 IR을 받아 Notion 게시 **계획**을 만든다. 실제 PDF 검사와
Mac의 로컬 Vision OCR을 제공한다. 생성 모델 어댑터는 기본 차단 상태이며 모의 응답으로만 검증했다.
앱 내부의 실제 Notion 쓰기는 아직 구현하지 않았다.
외부 문서 내용은 이 파일에 포함하지 않는다.

| 기능 | 입력 → 출력 | 책임과 검증 |
|---|---|---|
| 추출 경계 | 사전 추출 IR → DocumentIR | 텍스트·이미지·표·코드, 문서 버전·블록 ID·페이지·좌표 보존. 합성 IR과 OCR IR의 검증 경계 |
| PDF 검사/OCR | 로컬 PDF → 검사 보고서/OCR 후보 IR | 전체 페이지 검사. Mac OCR의 행·좌표·신뢰도와 페이지 래스터 보존. 페이지별 캐시 재개 |
| 목차 계획 | DocumentIR → Outline | 전체 블록을 읽는 LangChain Runnable 체인. 제목 기준 절 분할 |
| 절별 복원 | DocumentIR + Outline → RestoredDocument | LangChain Runnable 체인으로 절을 차례대로 복원. 원본 블록과 출처를 그대로 복사 |
| 독립 검증 | IR + 목차 + 복원 → ValidationReport | 계획/복원 어댑터와 별개 코드. 블록 누락·중복·순서·내용·출처·절 구성 확인 |
| 게시 계획 | 검증 통과 복원 → PublishPlan | 블록별 결정적 작업 키와 출처를 포함한 중립적 payload. API 호출 없음 |
| 작업 상태 | API/CLI → SQLite Job | LangGraph StateGraph 노드, 명시적 상태 전이, 단계별 원자적 저장, 프로세스 재시작 후 재개 |

## 가장 작은 수직 경로

`queued → extracted → planned → restored → validated → ready`

부분 OCR이나 검증 실패는 `rejected`로 종료하여 게시 계획을 만들지 않는다. 실행 예외는 `failed`와
마지막 성공 상태를 저장한다. resume은 마지막 성공 상태로 돌아가며 성공했던 단계는
다시 실행하지 않는다. 한 advance 요청은 LangGraph 노드 하나를 실제로 실행한다.
SQLite BEGIN IMMEDIATE로 읽기·노드 실행·저장을 묶는다. 프로세스가 중단되면 해당 단계가
rollback되어 재실행 가능하다. 현재 노드는 로컬 순수 연산이므로 적합하다. 외부 API를
추가할 때는 이 잠금을 오래 유지하지 말고 lease/outbox와 외부 멱등 키를 먼저 도입해야 한다.

DocumentIR의 정규화 JSON과 pipeline_version의 SHA-256이 작업 fingerprint다. 같은 입력은
다른 request key로 보내도 같은 작업을 재사용한다. 같은 request key에 다른 입력은 409다.
변경된 문서 버전이나 파이프라인 버전은 새 작업이다. 프로세스별 메모리 캐시는 사용하지 않는다.

## 교체 경계와 제한

Extractor, KnowledgeAdapter, PublishPlanner Protocol로 라이브러리/서비스를 교체한다.
LangGraph는 실제 상태 라우팅과 노드 실행에 쓰고 LangChain Core는 실제 LCEL 체인 실행에
쓴다. fake LLM조차 필요 없는 결정적 mock이 기본이다. 의미적 학습 설명 생성은 검증하지 않는다.
SQLite가 복구의 기준이며 LangGraph 원격 체크포인터나 서버는 쓰지 않는다.

어댑터에는 입력·IR·목차·복원물의 deepcopy를 전달하고 반환값을 재검증한다. 독립 검증은
사전 추출된 원입력 IR을 기준으로 삼아 추출물·복원물과 대조한다. 게시 계획 자체도 검증된
복원물과 블록별로 대조한다. 어댑터의 내부 변경·삭제·추가가 기준 데이터를 바꾸지 못한다.

일반 API는 서버 정책으로 합성 입력의 생성·실행·재개·게시 계획 조회를 금지한다. 합성 데모는
별도 create_demo_app에서 서버가 명시적으로 선택하며 요청 본문으로 활성화할 수 없다.
합성 IR은 합성 출처·가상 이미지 참조만 허용한다. OCR IR은 OCR/래스터 출처, confidence,
검토 필요 플래그와 실제 신뢰 폴더 내 에셋 검증을 요구한다. 데이터는 사전 추출 IR이며
검증은 원문 PDF 대비 의미적 충실도나 추출 출처의 암호학적 증명이 아니다.

좌표는 페이지 회전을 반영한 top-left PDF point 좌표로 정규화해야 한다. IR은 페이지 크기,
문서 버전, 페이지 범위, 블록 유일성, bounding box 범위를 검증한다. 이미지에는 asset 참조와
SHA-256을 저장한다. 합성 이미지 참조는 실제 파일을 요구하지 않는다. OCR은 페이지마다 정확히
하나의 전체 페이지 래스터를 요구하고 실제 블록 페이지 커버리지를 검사한다. 설정된 신뢰 에셋
폴더 안의 실제 파일 존재와 SHA-256을 검증하며 범위 밖 경로·중복 래스터·누락을 차단한다.
OCR confidence는 보존하되 의미적 정확성 판단에 쓰지 않는다. 원본 페이지 래스터가 표·코드·그림을
보존하며 OCR 텍스트를 해당 의미적 타입으로 승격하지 않는다. 전체 페이지 처리를 검증하고
부분 OCR은 차단한다. 표 병합·수식·읽기 순서·OCR 원문 충실도는 별도로 검토해야 한다.
READY 전이 직전에 현재 에셋·신뢰 폴더를 다시 검증한다. 검증 이후 파일이 사라지거나
바뀌면 게시 계획을 차단한다. 게시 어댑터는 human_review_required를 false로 낮출 수 없다.
로컬 파일을 지속적으로 불변 보관하는 것은 아니다. READY는 마지막 검사 시점의 계획 준비를
뜻한다. 후속 외부 쓰기에는 에셋 재검증과 불변 스냅샷 경계가 필요하다.
검증 통과는 IR 대비 보존 통과이며 원본 PDF 대비 추출 완전성이나 의미적 정확성을 뜻하지 않는다.

## 로컬 실행 경계

API는 기본 localhost에서 실행하며 인증/다중 사용자 접근은 제공하지 않는다. 작업 실행은
동기식 명시적 요청이다. 원본 입력과 참조 문서는 프로젝트 밖에 보관한다. 비공개 참조 자료,
원본 PDF, 외부 설계서, 비밀·자격증명은 코드/fixture/문서/이슈/PR에 넣지 않는다.
후속 범위는 의미적 PDF 구조 복원, 사용자 승인된 모델·Notion 통합, 그래프 해설 토글·문제·RAG다.


## Explicit hierarchy and review overlay

The optional hierarchy adapter validates parent/leaf ownership, preorder, full source coverage and input IR order before
flattening leaves into the existing restoration contract. Parent nodes never repeat block IDs. Front matter can be its own leaf.
The separate review command retains the complete original DocumentIR and produces effective text plus reviewed/candidate
layout fragments. Every correction binds to the original text, provenance and document digest, with a same-page image region.
Candidate corrections do not modify effective text; confirmed fragments cannot cite candidate corrections. Fragment text is an
explicit review assertion, not a semantic-verification result. Original blocks are neither replaced nor executed. Review output
is local and needs human review. This path does not extend the generic HTTP API or implement an automated Notion writer.


## Configurable providers

ProviderSettings selects Gemini by default or OpenAI explicitly. It has no model default and requires
an explicit user-approved model allowlist in addition to QuizPolicy capability approval. Provider choice,
model ID syntax and allowlist are checked before key loading. Keys use a selected-name loader, optionally
from an explicitly supplied project-root .env only after the delegate's network/byte/output/call gates.
There are no custom endpoint settings or automatic cross-provider retries. Provider-specific SDK adapters
share the existing QuizGenerator/result/verifier contracts, so further providers require new explicit
adapters and registry selection, not endpoint substitution. Tracing is disabled in the quiz graph context.

Gemini uses the current Google GenAI SDK through LangChain with native JSON schema, explicit Developer
API backend, zero SDK retries, seconds passed to LangChain (which converts to SDK milliseconds), fixed endpoint and finalized request byte guard.
One invocation permits one physical request. Tests inject fabricated keys and HTTP MockTransport only;
no account, actual model capability, price or live response was verified. See PROVIDERS.md for exact
configuration semantics, remaining budgets and official documentation.

## Grounded quiz generation (OpenAI optional)

A separate LangGraph executes generation and independent extractive validation with at most three attempts. No existing
job/API or review record changes its meaning. The context is rebuilt from the explicit FixtureInput, outline and review layer;
saved review output cannot certify itself. Synthetic evidence is authored, OCR evidence requires confirmed transcription
records, and pending corrections cannot supply evidence. Source identity/version/IR digest and evidence digest remain in output.
The first exercise type is cloze; exact source quote and answer/question/explanation checks bound unsupported assertions.
This is provenance/extractive fidelity, not semantic grading or a factual-truth guarantee. Results always need human review.

The optional LangChain OpenAI adapter defers key/client construction until explicit live/budget/capability approval and positive
limits. It uses native JSON Schema, required fields, additionalProperties=false, timeout and zero SDK retries. The final request
body is size-checked before transport, with the official endpoint and no inherited proxy configuration. Bounded attempts and an
in-memory adapter call ledger limit requests, not dollar spend or cross-process retries. The offline CLI cannot enable it.
Raw OCR, reviewed derivation and generated output are separate. Document instructions remain untrusted data and no tools or code
execution are offered. Vision remains a future evidence-adapter extension; source raster contracts remain available separately.

Official capability references checked on 2026-10-01 (documentation checks only; no account/API request):
- https://developers.openai.com/api/docs/guides/structured-outputs (required fields, native schema and refusal/incomplete handling)
- https://developers.openai.com/api/docs/models/gpt-4.1-mini (documented text/image input and structured outputs example; not a default or recommendation)
- https://developers.openai.com/api/docs/guides/images-vision (image-capable model/detail compatibility and limitations; no image sent here)
- https://docs.langchain.com/oss/python/integrations/chat/openai (native JSON Schema, timeout, retries and explicit base URL)

The available session had no callable OpenAI docs MCP or platform-api-key guide. Official web docs were used. The user's explicit
instruction to provide a key later kept credential setup and live requests out of this work. No pricing table or model default is
embedded. Capabilities and account access must be confirmed again for the chosen model before any live call.

## Mock-only question toggles

`notion_quiz` re-derives evidence from explicit source inputs, checks saved result provenance and repeats
the independent cloze verifier. It creates bounded native toggle objects with mock disclosure, answers,
extractive explanations and source/version/page/bbox plus separate raw/effective evidence. Provider IDs
do not define operation identity. Content/source hashes do; changing evidence creates a new revision.

The replaceable page gateway exposes only complete snapshots and appends; no update/delete operation.
A visible ownership marker inside the explanation makes sequential retries discoverable without a local
receipt. Conflicting edits or duplicate markers stop publication. Ambiguous append failure stops until
a new run reads the remote page. This does not claim atomic Notion transactions, concurrency control or
exactly-once real writes. Only the in-memory fake is implemented; future gateways must verify hub/page
ancestry, hydrate nested blocks, exhaust pagination and normalize read metadata to the request shape.

Native payload shape follows [Notion block reference](https://developers.notion.com/reference/block).
Complete snapshots follow [retrieve children pagination](https://developers.notion.com/reference/get-block-children).
Neither external Notion writes nor remote API acceptance were tested.
