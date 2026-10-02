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


## 원응답 보존과 무료 수정

성공한 HTTP 200 packet은 structured parsing 이전에 별도 provider 파일로 저장합니다. 비정상 HTTP 응답은 상태·분류만 보존하고 키·헤더·원문 오류 본문은 남기지 않습니다. 초기 버전에서 이미 실패한 요청의 저장되지 않은 packet을 사후 복원했다고 주장하지 않습니다.

로컬 수정은 원응답을 덮어쓰지 않는 별도 overlay입니다. 원본 경로와 지문, 수정 draft 지문, 추가 호출 0 및 검토 전체 ID를 결합해 출처 대조를 확인해야 합니다. 실패 receipt는 비용을 유지합니다. `recover_whole_unit_exercises`는 단독 블록 전체를 정답으로 만든 문제만 제외하는 좁은 복구이며 다른 근거 오류는 계속 거부합니다. 이 복구는 교육 품질 검수를 대신하지 않습니다.

`Cloze.context_unit_ids`는 최대 4개 같은 페이지의 맥락 블록을 지정할 수 있습니다. 렌더러는 원문 블록 순서로 문제를 구성하고 주 정답 블록에서만 정답을 가립니다. 표 셀의 전체 값이 정답이어도 다른 셀의 유의미한 단서가 있어야 합니다. 동일 페이지·정확 부분 문자열·남은 단서 검증은 셀 관계의 의미를 증명하지 않으므로 원본 표 대조가 별도로 필요합니다.

## 검토 후 렌더링

`document_study.accept_batch(result_path, review_path, budget_path, revision_path=...)`는 현재 세 입력 SHA, context, PNG hash와 실제 SQLite receipt를 읽기 전용으로 재검증합니다. 독립 자료 대조가 accepted여야 하며 설명·문제·도표·로컬 시각 후보의 전체 ID가 정확히 일치해야 합니다. 부분 검토나 변경된 draft는 거부합니다.

반환한 material은 곧바로 `render_batch(material, source_url, diagram_urls=...)`에 전달합니다. 렌더 직전에 같은 파일·receipt·검토를 새로 읽어 material 전체가 여전히 일치하는지 확인합니다. 불변 JSON을 저장했다는 이유만으로 과거 검토를 신뢰하지 않습니다. 원문 URL은 허용 Notion host의 페이지 UUID로 정규화하고 이미지 참조는 검증한 `file-upload://UUID`를 명시합니다. 리치 텍스트의 dotted identifier는 코드로 표시해 원치 않는 웹 링크를 막습니다.

이 함수는 네이티브 Notion 호출을 하지 않습니다. 호스트는 pending 체크포인트를 만들고 단 한 번 생성한 페이지 ID/receipt를 보존한 뒤 원격 본문을 다시 읽어 확인해야 합니다. 불확실 생성·업로드에서 새 페이지/파일을 자동 생성하지 않습니다. 기존 Model·메모·원문과 후보를 보존합니다.

```python
from pdf_notion_mvp.document_study import accept_batch, render_batch

material = accept_batch(result_path, review_path, budget_path,
                        revision_path=revision_path)
content = render_batch(material, source_page_url,
                       diagram_urls=verified_upload_references)
```

실제 비용 없이 관련 계약을 검증하려면 다음을 실행합니다.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_document_lesson.py tests/test_document_study.py -q
```

## 학습노트와 요청 배치의 구분

8쪽 제한은 생성·검수 단위이며 최종 교재 목차가 아닙니다. 원자료의 중요한 주제를 빠짐없이 포함하되 관련 절을 큰 학습 흐름으로 묶고, 필요성·용어·원리·단계·코드/표/그림 해석을 본문에서 연결해야 합니다. 세부 절이나 요청 배치의 개수는 최종 페이지 수를 정하는 기준이 아닙니다. 출처는 펼침 참고 영역에 두고 문제는 보조 확인으로 구성합니다. 별도 예시와 외부 보충은 원자료와 구분하고 검증 근거를 표시합니다. 모든 절에 동일한 서식을 강제하거나 문자열 검사·테스트 통과를 교육 품질 검수로 대체하지 않습니다.
