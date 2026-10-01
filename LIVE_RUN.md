# 한 번의 합성 문제 실행

기본값은 **mock**입니다. 첫 live CLI는 직접 작성한 `fixtures/synthetic*.json`과
`gemini-3.1-flash-lite`만 지원합니다. OCR·PDF·Notion 자료, 임의 파일·endpoint·자동 provider
fallback을 받지 않습니다. 기존 OpenAI 어댑터는 유지하지만 이 첫 실행 CLI에는 연결하지 않았습니다.

## 비용 없는 확인

```sh
.venv/bin/python -m pdf_notion_mvp.live_run_cli
.venv/bin/python -m pdf_notion_mvp.live_run_cli --mode plan \
  --model gemini-3.1-flash-lite --budget-usd 0.37 \
  --output output/live-run-approval-proposal.json
```

두 명령은 키·`.env` 조회와 네트워크 전송을 하지 않습니다. 첫 명령은 문제 1개를 mock으로 검증합니다.
둘째는 **승인이 모두 false인 제안**을 만들며 실행하지 않습니다. 같은 plan 출력 파일은 덮어쓰지
않으므로 다른 파일명을 쓰세요. 공개 저장소에 승인 파일·결과·실행 원장을 올리지 마세요.

제안에는 네 입력 파일 SHA-256, 원문·검증된 텍스트 context digest, 최종 SDK 요청 계약 digest,
provider/model, 가격·모델 한도 snapshot, 요청 1회·검증 시도 1회·문제 1개, 요청 최대 12,000바이트,
출력 설정 최대 512토큰과 HTTP 각 단계 timeout 20초가 함께 들어갑니다. 더 작은 요청/출력/timeout은
plan 옵션으로 지정할 수 있습니다. live에서는 승인 내용의 변경·덮어쓰기를 받지 않습니다.

## 별도 승인 뒤 실행

실제 키를 읽거나 호출하기 **전에** 사람에게 데이터 전송, 선택 모델의 텍스트/JSON schema 기능,
현재 가격·토큰 한도·계정 요금과 조건부 비용 범위를 함께 확인받아야 합니다. 실제 모델 호출 승인은
코드 작성·Git push/merge 승인과 별개입니다. 현재 저장된 키를 자동으로 검사하거나 수정하지 않습니다.

확인된 승인 제안의 `user_approved`, `data_transfer_confirmed`, `pricing_and_limits_confirmed`,
`model_capabilities_confirmed`, `conditional_cost_understood`만 true로 바꿉니다. 코드나 문서가
승인을 대신하지 않습니다. spec을 바꾸려면 새 제안과 새 승인이 필요합니다. 제안은 생성 후 1시간만
유효합니다. 아래 명령은 **그 별도 승인을 받은 후에만** 실행합니다.

```sh
.venv/bin/python -m pdf_notion_mvp.live_run_cli --mode live \
  --approval output/live-run-approval-proposal.json \
  --expected-plan-sha256 '<승인 파일의 plan_sha256>'
```

키 조회 전에 모든 승인·입력·가격·예산 gate를 확인하고 `output/live-runs/RUN_ID.json`을 독점 생성합니다.
같은 실행 ID는 별도 프로세스 재시작·동시 실행에서도 한 번만 예약됩니다. 키 누락·SDK 실패·타임아웃·
응답 검증 실패도 예약을 취소하지 않습니다. 결과 파일이 없다고 재호출하거나 원장을 삭제하지 마세요.
사람이 상태를 확인한 뒤 새 요청은 별도 새 ID·예산·승인을 받아야 합니다. 다른 저장소 복사본이나
원장 삭제까지 막는 분산 비용 원장은 아닙니다.

예약 후에만 선택된 Gemini 키를 기존 프로세스 환경 또는 명시된 프로젝트 루트 `.env`에서 읽습니다.
최종 SDK 요청은 승인된 텍스트·schema·출력 설정 전체와 일치해야 하며, 고정 HTTPS host/model path,
443 포트, 최종 바이트 수와 각 HTTP timeout을 전송 직전에 검사합니다. SDK/검증 재시도는 없습니다.
SDK 직렬화가 바뀌면 묵인하지 않고 전송 전에 중단합니다. 출력의 `request_count`는 transport 직전
검사를 통과한 시도 수이며, provider가 성공·과금했다고 증명하는 수치가 아닙니다.

## 비용 계산의 의미

바이트 수나 요청 1회만으로 작은 달러 상한을 보장하지 않습니다. 정확한 입력 토큰 수를 얻는
`countTokens`도 외부 요청이므로 이 실행 경로에서는 자동 호출하지 않습니다.

현재 확인한 standard paid text 가격은 입력 100만 토큰당 $0.25, 출력(사고 토큰 포함) $1.50입니다.
모델의 전체 입력 1,048,576·출력 65,536 토큰 한도를 보수적인 계산 범위로 사용합니다. 512는 실제
생성 요청의 출력 설정이며, 이를 전체 billable output 보증으로 간주하지 않습니다.

`(1,048,576 × 0.25 + 65,536 × 1.50) / 1,000,000 = $0.360448`

예시 예산 $0.37은 이 **조건부 계산 범위**를 승인하는 값이며 실제 샘플 예상 청구액이 아닙니다.
더 작은 입력 토큰 추정치를 임의로 넣거나 무료 요금이라고 가정할 수 없습니다. 계산은 확인한
모델 한도·현재 standard 가격·text-only·candidate 1개·tool/cache/grounding 없음 조건에 의존합니다.
가격·계정·provider 계산이 달라질 수 있으므로 계정 청구에 대한 강제 달러 상한은 아닙니다.
공식 snapshot은 2026-10-01 확인 기준이며 7일 이후에는 재검토·코드 갱신 없이는 plan/live가 차단됩니다.
승인 시에도 최신 공식 가격을 다시 확인해야 합니다. 작은 입력 토큰 상한이 필요하면 정확한 요청에
대해 별도 승인된 token counting 또는 검증된 로컬 tokenizer를 연결하는 후속 작업이 필요합니다.

공식 근거: [가격](https://ai.google.dev/gemini-api/docs/pricing),
[모델 한도·기능](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite),
[토큰 확인](https://ai.google.dev/gemini-api/docs/tokens),
[구조화 출력](https://ai.google.dev/gemini-api/docs/structured-output).

## 검증 범위

기본 mock·승인 제안·승인 gate·실제 SDK 직렬화와 fake transport 실행·동시 예약·CLI 재시작·
503/타임아웃/refusal/불완전 JSON/출처 오류·변조된 요청 차단을 오프라인으로 검증합니다.
실제 키 조회·모델 가용성·실제 응답 품질·계정 요금·실제 청구·Notion 게시 통합은 검증하지 않았습니다.
출력도 검토 후보이며 `human_review_required=true`, 의미 진위 검증=false입니다. 완료 결과의 digest와
provider/실행 mode를 실행기가 소유한 완료 원장에 기록합니다. 주입된 client/key 의존성은 injected,
주입 없는 실행도 network_unattested로 표시하며 실제 제공자 호출/청구를 인증하지 않습니다.
이 CLI는 결과를 Notion에 쓰지 않습니다. [별도 검토 게시 계획](PROVIDER_PUBLICATION.md)은 완료
원장·승인·현재 출처를 다시 검증해 mock 게시에 연결하고, 기존 raw-result MCP 경계는 provider 결과를
계속 거절합니다. 실제 API 시험은 현재 보류 중이며 이 경로를 확인하기 위해 호출하지 마세요.
