"""A local freshness renewal never bypasses provider expiry, changes or quotas."""
from datetime import timedelta

import pytest

from pdf_notion_mvp.free_execution import reuse_recent_evidence,validate_evidence,verified_project
from test_free_execution import free_plan
from test_lesson_generation import inputs,NOW


def test_project_match_rechecks_evidence_before_passing_key_to_executable(inputs,tmp_path,monkeypatch):
    paths,_=inputs;approval,_=free_plan(paths,tmp_path);e=approval.spec.free_tier_evidence
    from pathlib import Path
    Path(e.image_paths[0]).write_bytes(b'changed screenshot')
    monkeypatch.setattr('pdf_notion_mvp.free_execution.subprocess.run',
        lambda *a,**kw:pytest.fail('unverified matcher must not receive the key'))
    with pytest.raises(ValueError,match='screenshot changed'):
        verified_project('fabricated-private-key',e)


def test_same_recent_authorized_evidence_reuses_original_observation_without_sliding(inputs,tmp_path):
    paths,_=inputs;approval,_=free_plan(paths,tmp_path);e=approval.spec.free_tier_evidence
    original=e.model_dump_json()
    now=NOW+timedelta(hours=3)
    with pytest.raises(ValueError,match='expired'):validate_evidence(e,now)
    new=reuse_recent_evidence(e,current_project_id=e.project_id,
        same_authorized_scope_confirmed=True,local_expiry_only_confirmed=True,now=now)
    assert e.model_dump_json()==original and new.verified_at==e.verified_at
    assert new.image_sha256==e.image_sha256 and new.expires_at==NOW+timedelta(hours=24)
    repeated=reuse_recent_evidence(new,current_project_id=e.project_id,
        same_authorized_scope_confirmed=True,local_expiry_only_confirmed=True,now=NOW+timedelta(hours=20))
    assert repeated==new  # renewing later cannot keep old evidence alive forever


@pytest.mark.parametrize('change',['different_project','key_changed','tier_changed','unconfirmed_scope',
                                  'provider_expiry','too_old','modified_image','modified_matcher'])
def test_reuse_rejects_changed_or_unapproved_proof(inputs,tmp_path,change):
    paths,_=inputs;approval,_=free_plan(paths,tmp_path);e=approval.spec.free_tier_evidence
    kwargs=dict(current_project_id=e.project_id,same_authorized_scope_confirmed=True,
                local_expiry_only_confirmed=True,now=NOW+timedelta(hours=3))
    if change=='different_project':kwargs['current_project_id']='another-project'
    elif change=='key_changed':kwargs['known_key_changed']=True
    elif change=='tier_changed':kwargs['known_tier_changed']=True
    elif change=='unconfirmed_scope':kwargs['same_authorized_scope_confirmed']=False
    elif change=='provider_expiry':kwargs['local_expiry_only_confirmed']=False
    elif change=='too_old':kwargs['now']=NOW+timedelta(hours=24)
    elif change=='modified_image':
        from pathlib import Path
        Path(e.image_paths[0]).write_bytes(b'not approved proof')
    else:e.project_matcher_path=str(tmp_path/'missing-matcher');e.project_matcher_sha256='0'*64
    with pytest.raises(ValueError):reuse_recent_evidence(e,**kwargs)
