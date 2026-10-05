import copy
import io
import json
import sqlite3

import pytest
from docx import Document
from fastapi.testclient import TestClient

from recruit_agent.actions import ActionBroker
from recruit_agent.api import create_app
from recruit_agent.common import ROOT,BINDINGS,DomainError,canonical,now,read_json,uid
from recruit_agent.demo import seed,publish_fixture
from recruit_agent.platforms import BossAgentCliAdapter,resume_text
from recruit_agent.resumes import parse_file


def test_http_closed_loop_and_origin_guard(tmp_path):
    with TestClient(create_app(tmp_path,False)) as client:
        assert client.get('/api/health/live').json()['alive']
        assert client.post('/api/demo/seed').json()['count']==10
        result=client.post('/api/demo/fixture').json()
        assert result['report']['completed']==10
        candidates=client.get('/api/candidates').json()
        assert len(candidates)==10
        detail=client.get('/api/candidates/candidate-001?job_id=job-demo-001').json()
        assert detail['runs'][0]['harness']=='offline_fixture'
        action=next(a for a in detail['actions'] if a['kind']=='resume_view')
        assert client.post('/api/actions/'+action['action_id']+'/resolve',json={'candidate_id':'candidate-002','job_id':'job-demo-001'}).status_code==403
        assert client.post('/api/demo/seed',headers={'Origin':'https://attacker.invalid'}).status_code==403
        assert client.get('/api/status',headers={'Host':'attacker.invalid'}).status_code==400
        assert client.get('/api/exports/job-demo-001').status_code==200
        assert client.get('/api/candidates/candidate-001?job_id=other-job').status_code==404


def test_http_rejects_unverified_live_import_and_invalid_shapes(tmp_path):
    with TestClient(create_app(tmp_path,False)) as client:
        assert client.post('/api/imports',json={}).status_code==400
        config=client.get('/api/config').json()['config'];config['harness']=None
        assert client.put('/api/config',json=config).status_code==400
        assert client.post('/api/imports',json={'batch':{'mode':'live'}}).json()['error']['code']=='SOURCE_NOT_VERIFIED'
        folder=tmp_path/'attachments';folder.mkdir()
        retained=folder/'candidate-001-attachment-existing.pdf';retained.write_bytes(b'existing attachment')
        assert client.delete('/api/candidates/*').status_code==404
        assert retained.read_bytes()==b'existing attachment'


def test_external_result_cannot_claim_server_model_execution(tmp_path):
    from recruit_agent.demo import fixture_evaluation
    with TestClient(create_app(tmp_path,False)) as client:
        service=client.app.state.service;seed(service)
        lease=client.post('/api/evaluation-batches/claim',json={'job_id':'job-demo-001'}).json()
        envelope=fixture_evaluation(lease['job'],lease['batch'])
        submitted=client.post('/api/evaluation-batches/'+lease['batch']['batch_id']+'/submit',json={
            'lease_token':lease['lease_token'],'fencing_token':lease['fencing_token'],'evaluation':envelope,
            'metadata':{'harness':'external','verification':'real_cli_execution','server_invoked_model':True}})
        assert submitted.status_code==200
        metadata=service.batch_report(lease['batch']['batch_id'])['members'][0]['harness_metadata']
        assert metadata['verification']=='api_client_submission' and metadata['server_invoked_model'] is False


def test_text_docx_parsing_and_explicit_ocr_failure():
    raw=parse_file('姓名：王某\n\n编写 Python 脚本完成测试交付。'.encode(),'resume.txt')
    assert raw['completeness']=='full_text'
    stream=io.BytesIO();doc=Document();doc.add_paragraph('Implemented Python automation.');doc.save(stream)
    parsed=parse_file(stream.getvalue(),'resume.docx')
    assert 'Python automation' in parsed['text']
    with pytest.raises(DomainError):parse_file(b'not a pdf','resume.pdf')
    with pytest.raises(DomainError) as caught:parse_file(b'not executable','resume.exe')
    assert caught.value.code=='NOT_SUPPORTED'


def test_structured_resume_fields_exclude_demographics_and_brands():
    parsed={'basic':{'name':'PRIVATE','gender':'男','age':'35'},'work_experience':[{'company':'BRAND','position':'Developer','responsibility':'Python automation','performance':'Delivered'}], 'project_experience':[], 'education':[{'school':'FAMOUS'}]}
    text=resume_text(parsed)
    assert 'Python' in text and all(x not in text for x in ['PRIVATE','男','35','BRAND','FAMOUS'])


class FakePlatform:
    def refresh_locator(self,action):return {'platform_id':'candidate-platform','friend_id':'123'}
    def call(self,*args,**kwargs):raise DomainError('NETWORK_TIMEOUT','response lost')


def test_write_response_loss_stays_unknown_and_never_auto_retries(service,job,batch):
    job['is_synthetic']=False;service.create_job(job);job=service.activate_job(job['job_id'])
    batch.update({k:job[k] for k in BINDINGS});batch['mode']='manual';batch['candidates']=batch['candidates'][:1]
    candidate=batch['candidates'][0]
    service.import_batch(batch,verified_identity={candidate['candidate_id']:'candidate-platform'})
    service.create_drafts(job['job_id'],[{'candidate_id':candidate['candidate_id'],'text':'测试消息'}])
    draft=service.db.one("SELECT * FROM outbox LIMIT 1")
    cfg=service.config;cfg['actions']['send_enabled']=True;service.update_config(cfg)
    with service.db.transaction() as con:
        con.execute("UPDATE outbox SET state='APPROVED',approved_until=? WHERE item_id=?",(now()+60,draft['item_id']))
    broker=ActionBroker(service,FakePlatform())
    with pytest.raises(DomainError) as caught:broker.execute(draft['item_id'])
    assert caught.value.code=='ACTION_OUTCOME_UNKNOWN'
    assert service.db.one("SELECT state FROM outbox WHERE item_id=?",(draft['item_id'],))['state']=='OUTCOME_UNKNOWN'
    with pytest.raises(DomainError) as caught:broker.execute(draft['item_id'])
    assert caught.value.code=='APPROVAL_REQUIRED'


def test_expired_or_synthetic_targets_are_not_fabricated(seeded):
    action=seeded.db.one("SELECT * FROM action_targets WHERE kind='chat_open' LIMIT 1")
    with pytest.raises(DomainError) as caught:ActionBroker(seeded).resolve(action['action_id'],action['candidate_id'],action['job_id'])
    assert caught.value.code=='DEMO_ACTION_UNAVAILABLE'
    draft=seeded.create_drafts('job-demo-001',[{'candidate_id':'candidate-001','text':'合成草稿'}])
    item=seeded.db.one("SELECT * FROM outbox WHERE item_id=?",(draft['item_ids'][0],))
    with pytest.raises(DomainError) as caught:seeded.approve_drafts([{'item_id':item['item_id'],'candidate_id':item['candidate_id'],'text_hash':item['text_hash']}])
    assert caught.value.code=='IDENTITY_UNVERIFIED'


def test_platform_partial_errors_and_page_tail_are_visible(service,job,monkeypatch):
    job['is_synthetic']=False;service.create_job(job,'platform-job');service.activate_job(job['job_id'])
    with service.db.transaction() as con:service.ensure_account(con,'real-test-account')
    cfg=service.config;cfg['platform']['live_enabled']=True;service.update_config(cfg)
    adapter=BossAgentCliAdapter(service)
    records=[{'geekId':str(i),'encryptGeekId':'enc-'+str(i),'friendId':i,'securityId':'security','encryptJobId':'platform-job','name':'same-name'} for i in range(25)]
    def fake_call(args,account_id=None,**kw):
        if args[0]=='status':return {'data':{'logged_in':True}}
        if args[1]=='applications':return {'data':{'friendList':records,'hasMore':False}}
        return {'data':{'work_experience':[{'position':'Developer','responsibility':'使用 Python 完成真实测试任务。','performance':'上线交付'}]}}
    monkeypatch.setattr(adapter,'call',fake_call)
    first=adapter.collect('real-test-account',job['job_id'])
    assert len(first['imported'])==10 and first['next_page']==1 and first['next_offset']==10
    second=adapter.collect('real-test-account',job['job_id'],offset=10)
    third=adapter.collect('real-test-account',job['job_id'],offset=20)
    assert len(second['imported'])==10 and len(third['imported'])==5
    assert service.status()['candidates']==25
