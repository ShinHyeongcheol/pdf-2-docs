# 승인 결과의 무료 재생

`offline_replay_cli`는 원본 PDF를 로컬에서 검사하고, 이미 저장한 추출·목차·교정·독립 검토·생성 receipt를 검증한 뒤 SQLite 모의 서비스에 저장하고 다시 읽습니다. API 키를 읽거나 모델·임베딩·Notion으로 전송하지 않습니다. 새 PDF의 추출이나 새로운 설명 생성은 포함하지 않습니다.

```sh
PYTHONPATH=src .venv/bin/python -m pdf_notion_mvp.offline_replay_cli \
  --spec /absolute/private/replay-inputs/spec.json \
  --output-dir /absolute/private/replay-output
```

명세의 파일 경로는 기존 비공개 자료의 절대 경로로 지정합니다. 출력은 Git과 모든 입력 폴더 밖의 별도 디렉터리여야 합니다. 입력 예시는 다음과 같습니다.

```json
{
  "pdf_path": "/absolute/private/source/original.pdf",
  "source_path": "/absolute/private/source/document-ir.json",
  "outline_path": "/absolute/private/planning/outline.json",
  "review_path": "/absolute/private/planning/review.json",
  "budget_path": "/absolute/private/receipts/budget.sqlite",
  "budget_cap_micro_usd": 10000000,
  "batches": [
    {
      "result_path": "/absolute/private/results/batch.json",
      "review_path": "/absolute/private/results/batch-review.json",
      "revision_path": null
    }
  ],
  "reused_reading": []
}
```

모든 페이지가 `batches`의 승인 범위와 `reused_reading` 범위에 정확히 한 번 포함되어야 합니다. 기존 무료 수정 overlay가 있는 배치는 `revision_path`를 지정합니다. 독립 검토는 현재 결과 지문·전체 검토 ID·원장 단일 요청 receipt에 결합되어야 합니다.

이전에 검수한 HTML이 필요한 경우 `reused_reading`에 `root`, `review_path`, `module_key`, `pages`를 지정합니다. 검토 파일의 `decision=accepted`, `bound_reading_sha256` 전체 파일 목록과 `manifest.json`의 원본 SHA·단원·페이지가 일치해야 합니다. HTML은 원문 그대로 `reviewed_html_artifact`로 저장합니다. 배치 본문은 `reviewed_native_batch`입니다. 이는 서로 다른 모의 저장 표현이며 HTML을 실제 Notion 블록으로 변환하지 않습니다. 기존 HTML의 정적 참조 파일은 hash 검증되지만 SQLite fetch는 HTML 본문만 반환합니다.

## 재개와 보존

1. 원본 PDF SHA·페이지 수와 추출 캐시·전체 목차·원본 래스터를 검사합니다.
2. 현재 입력·receipt·독립 검토에 결합된 승인 결과를 재사용합니다.
3. 새 생성 요청 0건인 모의 게시 계획을 저장합니다.
4. 저장된 항목의 소유 키·ID·제목·본문을 다시 읽어 정확히 비교합니다.

네 단계는 SQLite 체크포인트에 저장됩니다. 완료한 실행도 입력과 저장 본문을 다시 검증합니다. 파일과 원장, 원본 래스터, 검수한 HTML 파일 목록의 변경을 단계별·게시 직전·읽기 이후에 차단합니다. 저장된 본문이나 제목이 바뀌면 그대로 보존하고 중단하며 개인 메모는 수정하지 않습니다.

모의 서비스는 첫 초기화를 파일 잠금으로 직렬화하고 소유 키에 유일 제약을 둡니다. 생성이 저장된 직후 응답이 유실되어도 재실행에서 같은 소유 키를 찾으므로 중복 생성하지 않습니다. 알 수 없는 기존 DB는 자동 초기화하지 않습니다. 이 소유 키 검색 계약을 실제 Notion 서비스가 구현한 것으로 주장하지 않습니다. 운영용 다중 사용자 잠금·인증·실제 Notion 어댑터는 별도 범위입니다.

## 확인 결과와 확장 조건

비공개 실제 162쪽 PDF에서 기존 20개 승인 배치와 Model 6쪽의 검수 HTML을 재사용하여 21개 모의 항목을 저장·재독해했습니다. 재실행 생성 0건, 생성 직후 응답 유실 복구, 계획 체크포인트 중단 복구, 전체 162쪽 중복 없는 범위를 확인했습니다. 기존 25건 원장 바이트는 유지됐고 추가 모델·Notion·임베딩 호출과 키 읽기는 0건입니다. 전체 OCR의 의미 정확성이나 새로운 원문 추출을 검증한 결과는 아닙니다.

| 확장 | 현재 확인 | 추가 실행 조건 |
| --- | --- | --- |
| 그래프 해설 | 합성 수치 관찰·추론 구분과 출처 검증 계약. 실제 자료의 선택 도식 설명은 기존 검수 범위 | 전체 수치 그래프의 위치·축·단위·값 근거와 독립 판독 필요. 이미지 외부 전송은 별도 승인 |
| SDK 예제 | 로컬 LangChain Core/LangGraph 실행과 모의 provider 경계 | 대상 SDK·버전·예제·모델 및 전송 범위를 지정하고 호출 예산 확인. 현재 교재 코드의 실제 provider 실행은 미실행 |
| RAG | 실제 자료의 확정 본문으로 BM25 검색과 인용 검증을 동반한 로컬 추출형 모의 답변 | 임베딩·벡터 저장소·생성 답변은 미구현. 외부 임베딩은 제공자·전송 텍스트·문서/질문 호출 수·비용 상한 별도 승인 필요 |

로컬 검색 시험은 선택한 네 개 정확 키워드와 근거 없는 한 질문의 범위·상태 확인입니다. 전체 검색 재현율이나 의미적 답변 품질 평가로 일반화하지 않습니다. 기존 API 비용 예약액은 US$9.011200 / US$10이며 실제 청구액은 확인되지 않았습니다. 이 재생 작업은 기존 원장을 읽기 전용으로 사용합니다.
