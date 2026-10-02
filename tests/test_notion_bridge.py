import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID
import pytest
from pdf_notion_mvp.notion_bridge import BridgeSpec,run_connected,plan_sha256,canonical_native
from pdf_notion_mvp.study_bundle import sha
from test_study_notion import packet,HUB,PAGE

class Connected:
    mode='host_connected_mcp'
    def __init__(self,child):self.child=child;self.body='기존 목차\n## 사용자 메모\n개인 의견';self.appends=0
    def call(self,tool,args):
        if tool=='notion_fetch':return packet(HUB,self.body,'hub') if args['id']==str(HUB) else self.child
        assert args['page_id']==str(HUB) and args['command']=='insert_content' and args['position']=={'type':'end'}
        self.appends+=1;self.body+='\n'+args['content'];return {'ok':True}

@pytest.fixture
def configured(tmp_path):
    inputs=tmp_path/'inputs';inputs.mkdir();child=packet(PAGE,'확정 원문\n## 사용자 메모\n원래 의견','child')
    baseline=inputs/'baseline.json';baseline.write_text(json.dumps(child))
    evidence=inputs/'source.json';evidence.write_text('{"pages":162}')
    review=inputs/'review.json'
    spec=BridgeSpec(hub_id=HUB,hub_title='hub',pages=[dict(page_id=PAGE,title='child',baseline_path=baseline,baseline_sha256=sha(baseline.read_bytes()))],title='검증 기록',paragraphs=['기존 내용의 읽기 검증'],input_sha256={evidence:sha(evidence.read_bytes())},review_path=review)
    review.write_text(json.dumps(dict(decision='accepted',plan_sha256=plan_sha256(spec))))
    return spec,Connected(child),tmp_path/'publication'


def test_connected_append_readback_repeat_and_memo_edits_preserved(configured):
    spec,gateway,output=configured
    first=run_connected(spec,output_dir=output,gateway=gateway)
    assert first['status']=='published_readback_verified' and first['actual_notion_appends']==1
    assert '개인 의견' in gateway.body and '원래 의견' in gateway.child['text']
    gateway.body=gateway.body.replace('개인 의견','새 개인 의견')
    gateway.child['text']=gateway.child['text'].replace('원래 의견','새 학습 의견')
    assert run_connected(spec,output_dir=output,gateway=gateway)['status']=='unchanged'
    assert gateway.appends==1 and '새 개인 의견' in gateway.body and '새 학습 의견' in gateway.child['text']


def test_lost_response_after_commit_reads_back_without_append_retry(configured):
    spec,gateway,output=configured;original=gateway.call
    def lost(tool,args):
        value=original(tool,args)
        if tool=='notion_update_page':raise TimeoutError('authored lost response')
        return value
    gateway.call=lost
    with pytest.raises(TimeoutError):run_connected(spec,output_dir=output,gateway=gateway)
    gateway.call=original
    assert run_connected(spec,output_dir=output,gateway=gateway)['status']=='unchanged' and gateway.appends==1


def test_error_before_commit_retains_unknown_write_and_blocks_retry(configured):
    spec,gateway,output=configured;original=gateway.call
    def failed(tool,args):
        if tool=='notion_update_page':return {'isError':True,'error':'authored denied'}
        return original(tool,args)
    gateway.call=failed
    with pytest.raises(ValueError,match='failure'):run_connected(spec,output_dir=output,gateway=gateway)
    gateway.call=original
    with pytest.raises(ValueError,match='do not blindly retry'):run_connected(spec,output_dir=output,gateway=gateway)
    assert gateway.appends==0


@pytest.mark.parametrize('damage',['content','title','parent','truncated','unknown','review','source'])
def test_changed_or_incomplete_inputs_block_before_mutation(configured,damage):
    spec,gateway,output=configured
    if damage=='content':gateway.child['text']=gateway.child['text'].replace('확정 원문','edited')
    elif damage=='title':gateway.child['title']='edited'
    elif damage=='parent':gateway.child['text']=gateway.child['text'].replace(HUB.hex,PAGE.hex)
    elif damage=='truncated':gateway.child['truncated']=True
    elif damage=='unknown':gateway.child['unknown_block_count']=1
    elif damage=='review':spec.review_path.write_text('{"decision":"rejected"}')
    else:next(iter(spec.input_sha256)).write_text('changed')
    with pytest.raises(ValueError):run_connected(spec,output_dir=output,gateway=gateway)
    assert gateway.appends==0


def test_completed_owned_content_edit_is_not_overwritten(configured):
    spec,gateway,output=configured;run_connected(spec,output_dir=output,gateway=gateway)
    gateway.body=gateway.body.replace('기존 내용의 읽기 검증','edited')
    with pytest.raises(ValueError,match='edited'):run_connected(spec,output_dir=output,gateway=gateway)
    assert gateway.appends==1 and 'edited' in gateway.body


def test_same_shared_journal_concurrent_runs_append_once(configured):
    spec,gateway,output=configured
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:run_connected(spec,output_dir=output,gateway=gateway),range(4)))
    assert sum(v['actual_notion_appends'] for v in results)==1 and gateway.appends==1


def test_native_image_fresh_signature_allowed_path_and_literal_code_changes_blocked():
    baseline='![](https://private.s3.amazonaws.com/path/page.png?old=1)\n```python\nvalue="\\n"\n```'
    expected,images=canonical_native(baseline)
    assert canonical_native(baseline.replace('old=1','new=2'),image_bindings=images)[0]==expected
    with pytest.raises(ValueError,match='reference'):canonical_native(baseline.replace('page.png','other.png'),image_bindings=images)
    assert canonical_native(baseline.replace('value=','other='),image_bindings=images)[0]!=expected


def test_review_changes_during_remote_read_block_dispatch(configured):
    spec,gateway,output=configured;original=gateway.call
    def change(tool,args):
        value=original(tool,args)
        if tool=='notion_fetch' and args['id']==str(HUB):spec.review_path.write_text('{"decision":"rejected"}')
        return value
    gateway.call=change
    with pytest.raises(ValueError,match='changed'):run_connected(spec,output_dir=output,gateway=gateway)
    assert gateway.appends==0


def test_stdio_response_wrong_request_id_is_rejected_without_secret_output(monkeypatch,capsys):
    import io
    from pdf_notion_mvp.notion_bridge_cli import StdioMcpGateway
    monkeypatch.setattr('sys.stdin',io.StringIO('{"id":"wrong","packet":{"secret":"fabricated"}}\n'))
    with pytest.raises(ValueError,match='identity'):StdioMcpGateway().call('notion_fetch',{'id':str(HUB)})
    assert 'fabricated' not in capsys.readouterr().out


def test_resolved_native_mention_title_changes_preserve_reference_identity():
    url='https://app.notion.com/p/'+HUB.hex
    old='목차 · <mention-page url="'+url+'">old title</mention-page>'
    new='목차 · <mention-page url="'+url+'">new title</mention-page>'
    assert canonical_native(old)[0]==canonical_native(new)[0]
    assert canonical_native(new.replace(HUB.hex,PAGE.hex))[0]!=canonical_native(old)[0]
    assert canonical_native('`'+old+'`')[0]!=canonical_native('`'+new+'`')[0]
    assert canonical_native(old.replace('<','\\<'))[0]!=canonical_native(new.replace('<','\\<'))[0]
