# Provider 결과에서 절별 검토 게시 계획으로

실제 API 시험은 보류 중입니다. 이 경로는 완료된 제공자 어댑터 결과를 다시 검증해 native Notion
문제·정답·해설·출처 토글 **계획**으로 연결합니다. 실제 모델 호출과 Notion 읽기·쓰기를 하지 않습니다.
기본 실행은 계획 준비이며 선택적인 게시는 로컬 영속 mock 페이지에만 적용합니다.

## 입력과 신뢰 경계

새 단일 실행기는 완료 결과에 `execution_mode`를 넣고 결과 전체의 SHA-256을 실행기가 소유한
`output/live-runs/RUN_ID.json` 완료 원장에 기록합니다. 이 원장은 run ID·승인 plan digest·provider·
모델·실행 mode·원문/근거 digest·검증된 최종 요청 시도 수를 결과와 묶습니다. 실행 예약은 완료·실패
후에도 유지하며 완료 원장 저장이 실패해도 생성 요청을 다시 보내지 않습니다.

게시 준비는 단순 `QuizResult`나 저장된 ready 문자열을 받지 않습니다. `RunCompletion` 전체와
생성 당시 `RunApproval`, 실행기가 소유한 완료 원장, 명시적 절 페이지 binding이 모두 필요합니다.
원장은 CLI 인자로 임의 위치를 지정하지 않고 현재 프로젝트의 고정 run-ID 경로에서 읽습니다.
완료 원장 없이 만든 파일, reserved-only 기록, digest 변조·실행 mode/provider 불일치, 실패 결과,
검증 이벤트 누락, 현재 입력 파일 변경과 근거 없는 질문·답·해설·출처를 거절합니다.

검증 시 현재 fixture로 공통 `FixtureInput`·`HierarchicalOutline`·`ReviewLayer`를 다시 읽고
`prepare_context`와 `verify_quiz`를 재사용합니다. 원문·context·출처 digest, 원문 인용, 첫 답의 빈칸
대체, 해설 형식, 출처 블록·페이지·좌표와 절 연결을 검사합니다. 의미 진위와 학습 품질은 검증하지
않으며 결과에는 계속 `human_review_required=true`, `semantic_correctness_verified=false`를 둡니다.

완료 원장 디렉터리는 신뢰하는 로컬 실행기의 경계입니다. 결과 파일만 변조하거나 임의 provider
표시를 바꾸는 것은 차단하지만 로컬 코드·원장까지 쓰기 가능한 공격자를 막는 서명/외부 인증은
아닙니다. `provider_attestation_verified=false`가 유지되며 실제 제공자 응답·청구를 인증하지 않습니다.
새 실행기의 기존 Gemini 합성 경로만 이 첫 연결에서 지원합니다. OpenAI/OCR 입력은 승인된 실행
계약을 확장하는 별도 작업이며 자동 전환하지 않습니다. 이전 버전의 완료 원장이 없는 결과는
자동 승격하지 않습니다. API 시험 보류 중 이 원장을 만들기 위해 실제 생성 호출을 하지 마세요.

## 표시와 재실행

의존성/transport가 주입된 실행은 `execution_mode=injected`이며 `[주입 실행 · 검토 필요]`를 붙입니다.
MockTransport의 Gemini SDK 결과도 이 표시를 사용해 실제 모델 호출을 했다고 오인시키지 않습니다.
주입 없는 실행도 `network_unattested`와 `[제공자 결과 · 검토 필요]`로 표시하며 외부 인증을 주장하지
않습니다. 전송 주입 자체는 mock을 증명하지 않으므로 주입된 실행의 실제 호출 여부도 미인증입니다.

원문 전사와 학습 근거는 별도로 유지합니다. 토글 provenance에는 provider·설정 모델·실행 mode·
문서 버전·원문/근거 digest·블록·페이지·좌표를 담습니다. 계획 상단은 정확한 완료 결과 digest와
run ID도 보관합니다. 중복 표식은 원문 context·실제 질문 내용·provider/model/mode에 기반하고,
제공자가 바꾼 question ID나 새로운 실행 ID는 같은 검토 토글을 중복 생성하지 않습니다.

사용자의 기존 메모는 보존합니다. 앱 토글에 사용자 수정이 있거나 중복 표식·불완전 페이지가 있으면
덮어쓰지 않고 검토로 중단합니다. mock 작성 응답이 유실돼도 같은 원격 표식을 읽어 중복 추가를
막습니다. mock 페이지는 같은 디렉터리 임시 파일에 전체 snapshot을 저장·flush·fsync한 뒤 atomic replace하며,
저장 중 오류나 replace 실패 시 기존 메모/페이지 파일을 보존하고 임시 파일을 정리합니다.
영속 mock 페이지 CLI는 단일 작성자 리허설용이며 다중 프로세스 페이지 잠금을 제공하지
않습니다. 생성 승인 유효시간은 **새 호출**을 제한합니다. 과거에 완료된 결과를 나중에 검토하는 것은
승인을 갱신하거나 모델을 재호출하지 않습니다.

## 비용 없는 게시 준비

현재는 fake SDK 테스트에서 만든 파일이나 이미 신뢰된 완료 run만 사용하세요. 아래 경로들은
각각 `RunApproval`, `RunCompletion`, `SectionPage`의 로컬 JSON입니다. 모두 Git 제외 `output/`에
보관하고 실제 ID·원문·결과를 공개 저장소에 올리지 마세요.

```sh
.venv/bin/python -m pdf_notion_mvp.provider_publication_cli \
  --approval output/completed-approval.json \
  --result output/completed-result.json \
  --binding output/section-binding.json \
  --output output/provider-notion-plan.json
```

첫 명령은 계획만 씁니다. 로컬 mock 페이지에 적용하고 다시 실행하려면 같은 명령에 다음을 붙입니다.

```sh
--mock-publish --mock-page output/mock-provider-page.json
```

첫 실행은 `mock_written`, 다음 실행은 `unchanged`입니다. stdout에는 원문·키·provider 오류 본문을
출력하지 않습니다. 출력과 mock 페이지 경로는 입력·서로·실행 원장을 덮어쓰지 않으며 symlink와
hardlink를 거절합니다. 실제 Notion gateway는 계속 차단됩니다. 기존 `notion_mcp_cli`의 raw provider
결과 입력 제한도 유지합니다. 호스트 연결로 실제 provider 결과를 게시하는 통합은 후속 검토 범위입니다.

## 검증

```sh
.venv/bin/python -m pytest tests/test_provider_publication.py -q
```

이 테스트는 실제 Gemini SDK와 가짜 키·MockTransport로 생성→독립 검증→완료 원장→검토 계획→
영속 mock 게시→재실행을 검사합니다. 실제 키·외부 모델·Notion 요청은 하지 않습니다.
그래프 해설 토글과 RAG는 이번 변경에 포함하지 않았습니다.
