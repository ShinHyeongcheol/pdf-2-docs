# 절 학습 묶음 → 지정 Notion 허브

`study_notion`은 기존 source IR·목차·검토 자료에서 `study_bundle.build_bundle`을 다시 실행하고, 한 절의 **새 하위 페이지 생성 인수**를 준비합니다. 내부 Notion/API 클라이언트·키·모델 호출은 없습니다. 실제 호출은 연결된 호스트 MCP가 수행합니다. 기존 원문 샘플이나 메모를 수정하는 action을 만들지 않습니다.

호스트 입력:

- `load_review_files`로 읽은 전체 입력과 선택 leaf·로컬 검색 질문
- 지정 허브 UUID와 그 허브의 완전한 현재 `fetch` 결과
- 원본 페이지마다 `UploadedPageImage(page, sha256, file_upload_id, filename)` 한 개. 이미 업로드·첨부한 파일의 사설 기록을 재사용합니다. 페이지 수·원본 SHA가 정확히 맞아야 합니다. 업로드 ID·개인 매핑은 Git 밖에 보관하세요.
- Git 밖의 비공개 체크포인트 경로. 체크포인트에는 원문과 개인 식별자가 들어갑니다.

처리 순서:

1. `prepare_study(files, section_id, question, hub_id, hub_packet, images)`로 action 인수·pending 체크포인트·검증한 이미지 binding을 얻습니다. 전체 IR/목차/래스터, 확정 교정, 규칙 문제, 로컬 인용 계약을 먼저 검증합니다. 선택한 절의 이미지와 원본/유효 전사, 후보 제안, 코드 상태, 정답·인용·출처를 제한된 Markdown에 담습니다. 출처의 읽기 문장·후보·문제·인용은 literal code 블록으로 표시하여 자동 목록 번호 변경·자동 링크·들여쓰기 손실을 막습니다. 문서 버전은 공통 계보에, 페이지·좌표와 원본/교정은 페이지별 기록에 한 번씩 보존합니다.
2. **dispatch 전에** `save_study_checkpoint(path, checkpoint)`를 호출합니다. action은 `notion_create_pages`의 인수이며 `parent.page_id`가 지정 허브입니다. 호스트는 정확히 한 번 생성하고 반환한 페이지 ID를 pending 체크포인트에 바인딩하여 저장합니다.
3. 새 페이지를 MCP `fetch`로 읽습니다. `prepare_study(..., checkpoint=saved, remote_packet=fetched)`를 재호출합니다. 입력·대상·내용의 식별자가 바뀌면 중단합니다. 정확한 읽기 확인은 `(None, confirmed_checkpoint, images)`를 반환합니다. confirmed 상태를 저장하세요.
4. 재실행도 원격 읽기 확인을 먼저 해야 합니다. 확인되면 action이 없습니다. 결과가 불확실하거나 마커·원문·후보·코드·인용·이미지가 다르면 pending을 유지하고 자동 재생성하지 않습니다. timeout 뒤에는 허브에서 해당 고유 제목의 페이지를 확인하는 사람의 조정이 필요합니다.

읽기 확인은 새 페이지의 직접 부모·제목·관리 마커·텍스트·literal code·이미지 위치/파일명과 호스트의 완전한 응답을 대조합니다. 서명 이미지 URL의 만료 query는 비교하지 않습니다. 호스트가 마커의 하이픈을 이스케이프하고, 코드 payload를 토글 들여쓰기 장식 없이 반환하는 실제 형식을 처리합니다. 코드의 원래 공백·탭·역슬래시와 블록 중첩은 비교합니다. 원격 이미지 바이트를 다시 내려받아 SHA를 검증하지는 않으므로 그 검증을 완료했다고 표현하면 안 됩니다. 마커 바깥 사용자 메모의 추가·수정은 보존됩니다. 관리 자료가 편집되면 덮어쓰지 않고 중단합니다. 페이지 소유권·MCP 응답을 적대적인 사용자나 서버가 위조하는 환경을 위한 암호학적 인증은 아닙니다.

기존 업로드 재사용은 [Notion의 업로드 안내](https://developers.notion.com/guides/data-apis/uploading-small-files)와 [파일 객체 문서](https://developers.notion.com/reference/file-object)를 따릅니다. 이미 첨부한 업로드는 지속적으로 재사용할 수 있습니다. 업로드 권한이나 첨부가 거절되면 이 연결은 중단하며 직접 HTTP·자격증명 조회로 우회하지 않습니다. Enhanced Markdown 문법은 실제 호스트의 `notion://docs/enhanced-markdown-spec`을 읽고 적용해야 합니다.

한 절의 호스트 연결만 구현합니다. 전체 문서 자동 게시, 범용 OCR/이미지 업로더, 모델 생성, 의미·코드 실행 검증, 사용자 수정 병합, 다중 작업자 잠금은 구현하지 않습니다. HTML 묶음은 [STUDY_BUNDLE.md](STUDY_BUNDLE.md)를 참고하세요.

```sh
PYTHONPATH=src .venv/bin/python -m pytest tests/test_study_notion.py -q
```

실제 게시 검증의 상태는 비공개 checkpoint/readback 기록으로 판단하세요. generic 테스트 통과나 create 성공만으로 confirmed가 되지 않습니다. 승인 검토가 쓰기를 거절하면 pending을 유지하고, 그 사유와 정확한 대상·내용을 사용자에게 알린 뒤 명시적 승인까지 재시도하지 않습니다. 공개 저장소에는 실제 payload·개인 매핑·거절된 action을 넣지 않습니다.
