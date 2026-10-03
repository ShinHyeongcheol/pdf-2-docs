import json,re
from pathlib import Path
from uuid import UUID
import pytest

from pdf_notion_mvp.lesson_generation import ContentReview,fingerprint
from pdf_notion_mvp.lesson_notion import prepare_lesson,main
from pdf_notion_mvp.rag_files import load_review_files
from pdf_notion_mvp.study_notion import save_study_checkpoint
from test_lesson_generation import inputs,run,NOW,ROOT
from pdf_notion_mvp.lesson_generation import create_proposal
from pdf_notion_mvp.contracts import FixtureInput
from pdf_notion_mvp.review import document_digest
from test_study_notion import HUB,PAGE,packet

@pytest.fixture
def reviewed(inputs,tmp_path):
    paths,_=inputs
    source=json.loads(paths[0].read_text());image=next(b for b in source['document']['blocks'] if b['kind']=='image')
    import copy
    second=copy.deepcopy(image);second['block_id']='authored-second-page';second['source']['page']=2
    source['document']['blocks'].append(second);paths[0].write_text(json.dumps(source))
    outline=json.loads(paths[1].read_text());outline['nodes'][-1]['block_ids'].append(second['block_id']);paths[1].write_text(json.dumps(outline))
    layer=json.loads(paths[2].read_text());layer['source_digest']=document_digest(FixtureInput.model_validate(source).document);paths[2].write_text(json.dumps(layer))
    a=create_proposal(paths,'unit.part',output_dir=tmp_path/'results',budget_ledger=tmp_path/'ledger/budget.sqlite',key_project_root=ROOT,now=NOW)
    for name in ['user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed']:setattr(a,name,True)
    seen=[];result,_=run(tmp_path,a,seen)
    ids=[c['claim_id'] for t in result['draft']['topics'] for c in t['claims']]
    ids += [q['question_id'] for q in result['draft']['exercises']]+[q['explanation']['claim_id'] for q in result['draft']['exercises']]
    review=ContentReview(result_digest=fingerprint(result),reviewed_ids=ids,reviewer='authored independent test',decision='accepted',notes=['authored scope'])
    rp=tmp_path/'reviewed/review.json';rp.parent.mkdir();rp.write_text(review.model_dump_json())
    images=[dict(page=b.source.page,sha256=b.sha256,file_upload_id=str(UUID(int=100+b.source.page)),filename=f'authored-p{b.source.page}.png')
            for b in load_review_files(*paths,[],tmp_path/'unused.json').source.document.blocks if b.kind=='image']
    args=[Path(a.spec.output_dir)/(result['operation_key']+'.json'),rp,tmp_path/'ledger/budget.sqlite','체크포인트',tmp_path/'reading',HUB,packet(HUB,'existing hub'),images]
    return args,result,seen


def remote(cp,bindings):
    body=cp.expected_content
    for i in bindings:body=body.replace('file-upload://'+str(i.file_upload_id),'https://private.s3.amazonaws.com/'+i.filename+'?fresh=1')
    def native(m):
        prefix,language,payload=m.groups()
        return prefix+'```'+language+'\n'+'\n'.join(line[len(prefix):] for line in payload.split('\n'))+'\n'+prefix+'```'
    body=re.sub(r'(?m)^(\t*)```([^`\n]*)\n(.*?)\n\1```',native,body,flags=re.S)
    return packet(PAGE,body+'\n## 사용자 메모\nmy notes',cp.title)


def test_reviewed_model_source_publication_readback_repeat_and_memo_preservation(reviewed,tmp_path):
    args,result,seen=reviewed;action,cp,bindings,final=prepare_lesson(*args)
    assert len(seen)==1 and len(action['pages'])==1 and action['parent']=={'page_id':str(HUB)}
    assert '학습 설명' in cp.expected_content and '원문 읽기' in cp.expected_content
    assert '```json' not in cp.expected_content and '```text' not in cp.expected_content and 'pdf-notion-' not in cp.expected_content
    assert 'unit_id' not in cp.expected_content and 'bbox' not in cp.expected_content
    assert str(tmp_path) not in cp.expected_content and not any('asset_ref' in line for line in cp.expected_content.splitlines())
    assert (final/'index.html').is_file()
    with pytest.raises(ValueError,match='do not repeat create'):prepare_lesson(*args,checkpoint=cp)
    cp=cp.model_copy(update={'page_id':PAGE})
    fetched=remote(cp,bindings);action,confirmed,_,_=prepare_lesson(*args,checkpoint=cp,remote_packet=fetched)
    assert action is None and confirmed.status=='confirmed' and confirmed.page_id==PAGE
    fetched['text']=fetched['text'].replace('my notes','edited notes').replace('fresh=1','fresh=2')
    assert prepare_lesson(*args,checkpoint=confirmed,remote_packet=fetched)[0] is None and len(seen)==1


@pytest.mark.parametrize('damage',['review','result','quote','parent','image'])
def test_changed_evidence_or_remote_blocks_without_new_create(reviewed,damage):
    args,result,_=reviewed;_,cp,bindings,_=prepare_lesson(*args)
    cp=cp.model_copy(update={'page_id':PAGE})
    fetched=remote(cp,bindings)
    if damage=='review':
        review=json.loads(args[1].read_text());review['decision']='needs_changes';args[1].write_text(json.dumps(review))
    elif damage=='result':
        result['draft']['topics'][0]['title']='changed';args[0].write_text(json.dumps(result))
    elif damage=='quote':fetched['text']=fetched['text'].replace(result['draft']['topics'][0]['claims'][0]['citations'][0]['quote'],'changed')
    elif damage=='parent':fetched['text']=fetched['text'].replace(HUB.hex,PAGE.hex)
    else:fetched['text']=fetched['text'].replace('authored-p1.png','changed.png')
    with pytest.raises(ValueError):prepare_lesson(*args,checkpoint=cp,remote_packet=fetched)
    assert cp.status=='pending_readback'


def test_cli_pending_bind_and_readback_are_runnable_without_host_credentials(reviewed,tmp_path,capsys):
    args,_,_=reviewed;folder=tmp_path/'host-input';folder.mkdir()
    hub=folder/'hub.json';hub.write_text(json.dumps(args[6]));images=folder/'images.json';images.write_text(json.dumps(args[7]))
    checkpoint=tmp_path/'publication/checkpoint.json';action=tmp_path/'publication/action.json'
    cli=['prepare','--result',str(args[0]),'--content-review',str(args[1]),'--budget-ledger',str(args[2]),
         '--reading-dir',str(args[4]),'--hub-id',str(HUB),'--hub-packet',str(hub),'--images',str(images),
         '--question',args[3],'--checkpoint',str(checkpoint),'--action-output',str(action)]
    main(cli);assert 'action_prepared' in capsys.readouterr().out
    original=checkpoint.read_bytes()
    with pytest.raises(SystemExit):main(cli)
    assert checkpoint.read_bytes()==original
    main(['bind','--checkpoint',str(checkpoint),'--page-id',str(PAGE)]);capsys.readouterr()
    from pdf_notion_mvp.study_notion import StudyCheckpoint,UploadedPageImage
    cp=StudyCheckpoint.model_validate_json(checkpoint.read_text());bindings=[UploadedPageImage.model_validate(i) for i in args[7]]
    fetched=folder/'remote.json';fetched.write_text(json.dumps(remote(cp,bindings)))
    main(cli+['--remote-packet',str(fetched)]);assert json.loads(capsys.readouterr().out)['status']=='confirmed'
    assert checkpoint.read_bytes()!=original


def test_readback_requires_returned_page_binding_and_has_no_visible_tracking(reviewed):
    args,_,_=reviewed;_,cp,bindings,_=prepare_lesson(*args)
    with pytest.raises(ValueError,match='page binding'):
        prepare_lesson(*args,checkpoint=cp,remote_packet=remote(cp,bindings))


def test_original_numbering_and_bullet_glyphs_do_not_become_renumbered_native_lists():
    from pdf_notion_mvp.lesson_notion import render_sources
    from pdf_notion_mvp.study_notion import UploadedPageImage
    source=dict(selected_source_pages=[2],fragments=[],blocks=[dict(original=dict(block_id='authored-title',kind='text',source={'page':2},text='2. Resume lesson'),effective_text='2. Resume lesson',correction=None),dict(original=dict(block_id='authored-bullet',kind='text',source={'page':2},text='• Preserve the source'),effective_text='• Preserve the source',correction=None)])
    image=UploadedPageImage(page=2,sha256='0'*64,file_upload_id=PAGE,filename='authored-p2.png')
    text=render_sources(source,[image])
    assert '> 2. Resume lesson' in text and '> • Preserve the source' in text
    assert '```text' not in text and '```json' not in text
