# 전체 문서의 예산 제한 Gemini 생성

`document_lesson.prepare`는 명시한 전체 IR·목차·검토 JSON, 정렬된 최대8쪽,
선택한 `DiagramEvidence`를 요청 digest에 결합합니다. 미확정 OCR 원문은 그대로
유지하고 후보 proposed text는 별도 표시합니다. 확정 교정만 본문으로 적용합니다.
교정 상태를 승격하지 않습니다. 코드 실행·샘플 모델 출력·기술 의미 확정을
주장하지 않습니다. 생성물은 독립 내용 검토 대기 상태입니다.

`BatchApproval`의 승인 네 필드는 기본 false입니다. 실행에는 승인된 Google
전송 범위와 예산, 최근 공식 가격·모델 기능 확인 및 변경 없는 plan SHA가
필요합니다. 1시간 유효한 승인을 검증하고 기존 공유 SQLite 비용 원장의 누적
예약액을 확인한 뒤에만 선택한 프로젝트의 `GEMINI_API_KEY`를 읽습니다.

텍스트와 선택한 원본 PNG만 Google generateContent에 전송합니다. 이미지
SHA·문서 버전·좌표·관찰 ID를 확인하며 로컬 경로는 요청에서 제외합니다.
미확인 이미지나 외부 이미지 URL, 도구·검색·임베딩·파일 업로드는 없습니다.
`httpx`는 Gemini 선택 의존성입니다. 어댑터 경계는 `client_factory`로 모의
HTTP 전송을 주입할 수 있고 그 실행은 injected로 표시합니다.

모델은 `gemini-3.1-flash-lite`, 공식 Standard 가격은 입력(text/image/video)
US$0.25/1M, 출력(생각 토큰 포함) US$1.50/1M입니다. 기존 검토된 전체 모델
용량을 기준으로 요청당 US$0.360448를 예약하고 기존 요청을 포함해 US$10을
초과하는 실행을 거부합니다. 예약액은 실제 청구액을 뜻하지 않습니다.
[가격](https://ai.google.dev/gemini-api/docs/pricing)과
[모델 한도](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)는
실행 시점에 다시 확인해야 합니다. 다른 모델/유료 기능으로 변경하려면 계약과
예산 확인을 다시 해야 합니다.

최종 endpoint·요청 digest·60초 timeout을 HTTP hook에서 확인하고 요청 수를
원장에 기록합니다. HTTP 오류·출력 절단·불확실 결과·근거 오류는 요청을 소비한
상태로 보존하며 자동 재시도하지 않습니다. 동일 성공 결과는 digest와 현재
입력을 다시 검증한 뒤 키나 API를 읽지 않고 재사용합니다. 원장 삭제·변경은
새 예산으로 초기화하지 않습니다. 오류 본문/키를 원장에 저장하지 않습니다.

모든 페이지 순서와 도표 커버리지, 같은 페이지의 근거 ID, 정확한 원문 부분
문자열 정답을 검증합니다. 빈칸 문장은 로컬에서 원문으로 구성합니다. 이 검증은
AI 설명의 의미 정확성을 대신하지 않습니다. 설명/문제/도표는 독립 검토 전
Notion에 확정 학습 자료로 게시하지 마세요. 원문·이미지·미확정 상태·사용자
메모와 생성물은 각각의 계보와 표시 상태를 유지해야 합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_document_lesson.py -q
```

이 테스트는 작성한 합성 입력/PNG와 모의 HTTP만 사용하며 실제 비용이 없습니다.
기존 Model 결과의 재사용은 기존 결과 receipt·digest와 내용 검토를 별도로
확인해야 합니다. 페이지 목록만으로 생성 완료를 판정하지 않습니다.
