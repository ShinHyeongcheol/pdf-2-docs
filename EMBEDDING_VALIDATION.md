# 승인된 본문 임베딩 검증

`embedding_validation`은 확정된 설명 본문을 Gemini Embedding 2로 변환하고 로컬에서 검색 순위를 계산하는 별도 실험 경계입니다. 답변 생성이나 Notion 게시를 수행하지 않습니다. 기본 데모와 테스트는 외부 호출 없이 실행됩니다.

`create_proposal`에 원본 IR·목차·검수 계층·본문 manifest의 네 파일과 최대 두 질문을 전달합니다. Manifest는 확정 근거의 `evidence_id`, `source_block_id`, `page`, `text`가 정확히 일치하는 최대 20개 레코드입니다. 표·코드·후보 fragment의 원본 블록은 제외합니다. API로 보내는 각 요청에는 단일 텍스트 part와 공식 검색 prefix만 들어가며 이미지, 문서 파일, 로컬 경로와 출처 메타데이터는 전송하지 않습니다.

호스트는 검수한 계획 SHA, 명시적인 사용자 승인·자료 전송 승인·현재 가격 및 모델 검증을 `EmbeddingApproval`에 결합합니다. `execute`는 최대 한 시간인 계획 유효기간과 파일 SHA, 정확한 본문·질문, 요청 digest를 다시 검사합니다. 이 모듈에는 승인 정보를 자동으로 채우는 게시 CLI가 없습니다.

모델은 `gemini-embedding-2`, 출력은 768차원, 요청은 최대 22회이며 각 요청의 시도는 한 번입니다. 가격·입력 한도는 [Google 모델 문서](https://ai.google.dev/gemini-api/docs/models/gemini-embedding-2), [가격표](https://ai.google.dev/gemini-api/docs/pricing), [검색 prefix 문서](https://ai.google.dev/gemini-api/docs/embeddings)에서 확인해야 합니다. 변경되면 계약과 승인 계획을 다시 검토해야 합니다.

비용은 입력 최대 8,192토큰과 텍스트 US$0.20/백만 토큰을 기준으로 요청당 1,639 micro-USD를 먼저 예약합니다. 22회는 US$0.036058이며 이 실험의 US$0.04 한도와 기존 누적 US$10 원장을 함께 검사합니다. 기존 원장의 inode·장치·모든 기존 행을 결합하고 원장이 없거나 교체되면 중단합니다. 실패·타임아웃도 예약을 되돌리지 않으며 실제 청구액 검증으로 표현하지 않습니다.

모든 요청을 원장에 예약한 뒤 기존 `GEMINI_API_KEY`를 읽습니다. 키를 출력하지 않고 응답에 키가 들어오면 JSON의 키·값까지 가립니다. 정확한 endpoint·본문·타임아웃을 전송 직전에 확인하며 redirect·환경 proxy·자동 재시도는 사용하지 않습니다. 사설 출력 디렉터리에 요청·응답 receipt와 출처를 보존하고 벡터의 차원·숫자·유한성·영벡터 여부를 검증합니다.

같은 본문·질문·모델은 승인 시각이나 manifest의 공백이 달라져도 같은 작업입니다. 완료된 receipt가 일치하면 키를 읽거나 API를 호출하지 않고 재사용합니다. 부분 성공·불확실 상태는 자동 재전송하지 않습니다. 출처 정보는 로컬 순위 결과에 남습니다. 코사인 유사도는 답변 근거의 충분성을 증명하지 않으므로 근거가 없는 질문은 별도 검수에서 판정해야 합니다.

오프라인 검증:

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_embedding_validation.py -q
```

테스트는 MockTransport와 합성 키를 사용합니다. 실제 API 응답·검색 품질·요금 청구·일반 문서 자동 처리의 증거는 별도의 사설 실행 자료로 관리합니다.
