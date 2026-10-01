# 승인된 Notion 연결의 문제 토글 게시 경계

`notion_mcp`는 로컬 Notion 토큰이나 HTTP 클라이언트 없이 호스트의 기존 Notion MCP 연결에 전달할
액션을 준비합니다. 실제 tool 실행은 호스트가 담당합니다. 기본 CLI 자체는 원격 쓰기와 모델 호출을
하지 않습니다. 현재 게시 입력은 검증된 authored mock 결과만 지원하며 실제 모델 생성이라고 표시하지 않습니다.

절 연결 파일 `SectionPage`에는 지정 허브 UUID, 절 페이지 UUID, 문서 ID·버전·절 ID가 들어갑니다.
페이지는 지정 허브 하위여야 하고 `section_marker(binding)`으로 생성한 절 연결 식별을 포함해야 합니다.
다른 허브·페이지·절, 잘린 결과와 unknown block을 거절합니다. 실제 ID와 fetch 결과·체크포인트는
Git 제외 `output/` 같은 로컬 위치에 보관하고 공개 저장소에 넣지 않습니다.

## 준비와 호스트 실행

먼저 연결된 Notion의 enhanced Markdown spec을 읽고 대상 페이지를 fetch합니다. fetch tool의 전체
결과 envelope를 로컬 JSON으로 저장합니다. 현재 기본 fixture는 직접 작성한 합성 절입니다.

```sh
.venv/bin/python -m pdf_notion_mvp.notion_mcp_cli \
  --binding output/section-binding.json \
  --fetch-result output/page-before.json \
  --checkpoint output/section-checkpoint.json \
  --output output/section-prepared.json
```

`--source`, `--outline`, `--review`, `--quiz-result`로 명시적 입력을 바꿀 수 있습니다. 저장된 ready
결과도 원문·교정 입력과 다시 대조합니다. 기본 fixture 외의 입력은 절 연결과 정확히 일치해야 합니다.

호스트는 출력 `status=append_required`일 때만 `action.arguments`를 기존 Notion update-page tool에
전달합니다. command는 페이지 끝 `insert_content`이고 update/delete/full replacement는 없습니다.
`unchanged`와 `failed_human_review`에는 실행할 action이 없습니다. CLI는 tool 실행 전에 pending
체크포인트를 저장합니다. 같은 페이지에는 단일 작성자를 사용해야 합니다.

호스트가 쓰기를 거절하거나 응답이 유실됐거나 async 상태가 불확실하면 pending을 유지하세요.
async task가 반환되면 호스트가 task 상태를 확인하며 추가 쓰기를 보내지 않습니다. 페이지를 다시
fetch해서 완전한 문제 토글이 확인된 경우에만 다음 명령으로 확인 상태를 저장합니다.

```sh
.venv/bin/python -m pdf_notion_mvp.notion_mcp_cli \
  --binding output/section-binding.json \
  --fetch-result output/page-after.json \
  --checkpoint output/section-checkpoint.json \
  --output output/section-confirmed.json --confirm
```

기존 페이지 내용이 prefix로 보존되고 원격 토글 전체가 기대한 질문·정답·해설·출처와 일치해야 합니다.
확인 후 다시 준비하면 `unchanged`입니다. pending 상태에서 원격 표식이 없으면 자동 재시도하지
않습니다. 실제 실패인지 지연된 성공인지 사람이 확인해야 합니다. 앱 토글의 사용자 편집, 중복 표식,
불완전한 구조도 덮어쓰지 않고 검토로 중단합니다. 체크포인트를 삭제해서 재시도를 강제하지 마세요.

## 검증 범위와 한계

기존 출처와 사용자 메모를 보존하는 append 액션, 특수문자/개행 literal 처리, 원격 성공 후 로컬
응답 유실, pending 재시작, 부모/절 제한과 cursor chain은 오프라인으로 검증합니다.

현재 연결의 fetch는 페이지 단위의 enhanced Markdown을 제공합니다. `collect_parts`의 cursor-chain
완전성은 mock 테스트이고 이 연결에서 실제 native block pagination을 증명한 것이 아닙니다.
명시적 truncation/unknown 신호와 불완전 wrapper는 차단하지만 생략된 메타데이터를 통해 숨은 블록이
없다는 사실까지 증명할 수는 없습니다. 초기 실제 샘플은 지원되는 작은 블록만 사용합니다.

호스트 연결로 작은 합성 절의 페이지 생성·문제 토글 추가·read-back을 확인했습니다. 원격 문제는
1개이며 정답·해설·문서 버전·블록·페이지·좌표를 기대한 native payload와 대조했습니다. 게시 전
출처·합성 메모 영역은 그대로 보존됐고, 확인 후 재실행은 unchanged/action 없음으로 두 번째
쓰기를 만들지 않았습니다. 기존 원문 페이지와 키 설정은 변경하지 않았습니다. 이 샘플 검증은
임의 원문·사용자 편집·native pagination·동시 작성자의 실제 게시까지 검증한 것이 아닙니다.

현재 live 모델 실행 CLI, 실제 모델 응답 품질·가격·의미 진위 검증은 없습니다. 이 게시 연결은
모델 호출 승인을 대신하지 않습니다. 다중 작성자 동시 게시와 native pagination은 후속 검증 범위입니다.
