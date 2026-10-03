"""One entry point for reviewed inputs, local reading, generation and independent review."""
import argparse
import json
import os
from pathlib import Path

from pydantic import Field

from .contracts import Contract
from .lesson_generation import (LessonApproval, create_proposal, execute, fingerprint,
                                operation_key, safe_error_code, write_reading_bundle)
from .rag_files import load_review_files, read_json_input
from .study_bundle import private_destination, run as local_bundle, sha
from .free_execution import FreeTierEvidence


BLOCKERS = {
    'explicit run approvals required': 'approval_required',
    'approval expired': 'approval_expired',
    'cumulative cost budget exhausted': 'paid_budget_exhausted',
    'run consumed or ambiguous; no automatic retry': 'consumed_run_no_retry',
    'session scope changed; use a new session directory': 'new_session_required',
    'session inputs changed; start a new session': 'new_session_required',
    'approval differs from session scope': 'approval_scope_mismatch',
    'complete independent content comparison required': 'independent_review_required',
    'confirmed global outline required': 'outline_review_required',
}


class StudySession(Contract):
    version: str = Field(default='1', pattern='^1$')
    session_dir: str
    question: str = Field(min_length=1, max_length=2000)
    proposal: LessonApproval
    offline_bundle: str


def _save(path, packet):
    data=(json.dumps(packet,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError('regular session path required')
    if path.exists():
        if not path.is_file() or path.stat().st_nlink!=1 or path.read_bytes()!=data:
            raise ValueError('existing session artifact changed; preserve it')
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


def _load(directory):
    packet=read_json_input(Path(directory)/'session.json')
    session=StudySession.model_validate(packet['session'])
    if packet['sha256']!=fingerprint(session.model_dump(mode='json')):
        raise ValueError('session changed')
    root=private_destination(Path(directory),[Path(p) for p in session.proposal.spec.input_paths])
    if str(root)!=session.session_dir or session.proposal.spec.output_dir!=str(root/'model-results'):
        raise ValueError('session paths changed')
    if any(getattr(session.proposal,k) for k in ('user_approved','data_transfer_confirmed','budget_confirmed','pricing_capabilities_confirmed')):
        raise ValueError('session proposals must remain unapproved')
    spec=session.proposal.spec
    if session.proposal.plan_sha256!=fingerprint(spec.model_dump(mode='json')):
        raise ValueError('session proposal changed')
    files=load_review_files(*[Path(p) for p in spec.input_paths],[],root/'unused.json')
    if any(node.review_status!='confirmed' for node in files.hierarchy.nodes):
        raise ValueError('confirmed global outline required')
    if [sha(Path(p).read_bytes()) for p in spec.input_paths]!=spec.input_sha256:
        raise ValueError('session inputs changed; start a new session')
    return session


def _scope(spec):
    return {k:v for k,v in spec.model_dump(mode='json').items()
            if k not in {'run_id','created_at','expires_at'}}


def _plan_path(session, proposal):
    return Path(session.session_dir)/'plans'/(proposal.plan_sha256+'.json')


def prepare(paths, section, question, directory, budget, key_root, exclude_pages=(), *, now=None,free_tier_evidence=None,free_ledger=None):
    paths=[Path(p) for p in paths]
    root=private_destination(Path(directory),paths)
    files=load_review_files(*paths,[],root/'unused.json')
    if any(node.review_status!='confirmed' for node in files.hierarchy.nodes):
        raise ValueError('confirmed global outline required')
    proposal=create_proposal(paths,section,exclude_pages,output_dir=root/'model-results',
                             budget_ledger=budget,key_project_root=key_root,now=now,
                             free_tier_evidence=free_tier_evidence,free_ledger=free_ledger)
    if (root/'session.json').exists() or (root/'session.json').is_symlink():
        session=_load(root)
        if _scope(session.proposal.spec)!=_scope(proposal.spec) or session.question!=question:
            raise ValueError('session scope changed; use a new session directory')
        final,_=local_bundle(*paths,section,question,root/'source-reading')
        if str(final)!=session.offline_bundle:
            raise ValueError('saved local reading changed')
        return session,'unchanged'
    final,_=local_bundle(*paths,section,question,root/'source-reading')
    session=StudySession(session_dir=str(root),question=question,proposal=proposal,offline_bundle=str(final))
    _save(_plan_path(session,proposal),proposal.model_dump(mode='json'))
    packet=session.model_dump(mode='json')
    _save(root/'session.json',{'session':packet,'sha256':fingerprint(packet)})
    return session,'written'


def plan(session, *, now=None):
    spec=session.proposal.spec
    proposal=create_proposal(spec.input_paths,spec.section_id,spec.exclude_pages,
        output_dir=spec.output_dir,budget_ledger=spec.budget_ledger,key_project_root=spec.key_project_root,now=now,
        free_tier_evidence=spec.free_tier_evidence,free_ledger=spec.free_ledger)
    if _scope(proposal.spec)!=_scope(spec):
        raise ValueError('session request changed; prepare a new session')
    _save(_plan_path(session,proposal),proposal.model_dump(mode='json'))
    return proposal


def generate(session, approval_path, expected_sha, **kwargs):
    approval=LessonApproval.model_validate(read_json_input(Path(approval_path)))
    if _scope(approval.spec)!=_scope(session.proposal.spec):
        raise ValueError('approval differs from session scope')
    spec=approval.spec
    return execute(approval,expected_sha,Path(spec.budget_ledger),Path(spec.output_dir),
                   Path(spec.key_project_root),**kwargs)


def render(session, content_review, result_path=None):
    spec=session.proposal.spec
    result_path=Path(result_path) if result_path else Path(spec.output_dir)/(operation_key(spec)+'.json')
    result=read_json_input(result_path,max_bytes=500000)
    if (result['operation_key']!=operation_key(spec) or result['input_paths']!=spec.input_paths
            or result['input_sha256']!=spec.input_sha256 or result['context_digest']!=spec.context_digest
            or result.get('excluded_pages',[])!=spec.exclude_pages):
        raise ValueError('result differs from session scope')
    root=Path(session.session_dir)/'reading'
    files=load_review_files(*[Path(p) for p in spec.input_paths],[],root/'unused.json')
    return write_reading_bundle(result_path,Path(content_review),files,spec.section_id,
                                session.question,root,budget_path=Path(spec.budget_ledger))


def summary(session, proposal=None):
    proposal=proposal or session.proposal
    return dict(session_dir=session.session_dir,index_html=str(Path(session.offline_bundle)/'index.html'),
                proposal_path=str(_plan_path(session,proposal)),plan_sha256=proposal.plan_sha256,
                expires_at=proposal.spec.expires_at.isoformat(),
                generation_requires_separate_approval=True,
                billing_tier_verified_by_session=False,notion_status='not_requested')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    prepare_parser=sub.add_parser('prepare',help='Read reviewed inputs and write an unapproved generation plan; no API calls')
    for flag in ('source','outline','review','session-dir','budget-ledger','key-project-root'):
        prepare_parser.add_argument('--'+flag,type=Path,required=True)
    for flag in ('section','question'):
        prepare_parser.add_argument('--'+flag,required=True)
    prepare_parser.add_argument('--exclude-page',type=int,action='append',default=[])
    prepare_parser.add_argument('--free-evidence',type=Path)
    prepare_parser.add_argument('--free-ledger',type=Path)
    for action in ('plan','generate','render'):
        child=sub.add_parser(action)
        child.add_argument('--session-dir',type=Path,required=True)
        if action=='generate':
            child.add_argument('--approval',type=Path,required=True)
            child.add_argument('--expected-plan-sha256',required=True)
        elif action=='render':
            child.add_argument('--content-review',type=Path,required=True)
            child.add_argument('--result',type=Path)
    args=parser.parse_args(argv);session=None
    try:
        if args.action=='prepare':
            evidence=FreeTierEvidence.model_validate(read_json_input(args.free_evidence)) if args.free_evidence else None
            session,status=prepare([args.source,args.outline,args.review],args.section,args.question,
                args.session_dir,args.budget_ledger,args.key_project_root,args.exclude_page,
                free_tier_evidence=evidence,free_ledger=args.free_ledger)
            packet=dict(status=status,model_requests=0,key_reads=0,notion_requests=0,**summary(session))
        else:
            session=_load(args.session_dir)
            if args.action=='plan':
                proposal=plan(session)
                packet=dict(status='unapproved_plan',model_requests=0,key_reads=0,notion_requests=0,**summary(session,proposal))
            elif args.action=='generate':
                result,status=generate(session,args.approval,args.expected_plan_sha256)
                packet=dict(status=status,content_status=result['status'],operation_key=result['operation_key'],
                            result_path=str(Path(session.proposal.spec.output_dir)/(result['operation_key']+('-ids.json' if result.get('id_recovery') else '.json'))),
                            execution_mode=result['execution_mode'],notion_status='not_requested')
            else:
                final,status=render(session,args.content_review,args.result)
                packet=dict(status=status,index_html=str(final/'index.html'),model_requests=0,
                            key_reads=0,notion_requests=0,notion_status='not_requested')
        print(json.dumps(packet,ensure_ascii=False))
    except Exception as exc:
        code=BLOCKERS.get(str(exc),safe_error_code(exc)) if isinstance(exc,ValueError) else safe_error_code(exc)
        packet=dict(status='blocked',error_code=code)
        if session is not None and args.action=='generate':
            error_path=Path(session.proposal.spec.output_dir)/(operation_key(session.proposal.spec)+'-error.json')
            if error_path.is_file() and not error_path.is_symlink():packet['error_receipt']=str(error_path)
        parser.exit(1,json.dumps(packet,ensure_ascii=False)+'\n')


if __name__=='__main__':main()
