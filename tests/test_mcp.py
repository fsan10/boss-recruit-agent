import asyncio
import json
import os
import sys

from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

from recruit_agent.common import ROOT
from recruit_agent.demo import fixture_evaluation


def value(result):
    data=result.model_dump(by_alias=True)
    if data.get('structuredContent'):
        return data['structuredContent']
    return json.loads(data['content'][0]['text'])


def test_official_mcp_stdio_roundtrip_shares_sqlite(seeded):
    cfg=seeded.config;cfg['harness']['primary']='mcp';cfg['harness']['model_id']='test-client-model';seeded.update_config(cfg)
    seeded.activate_job('job-demo-001')
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-m','recruit_agent','mcp','--runtime',str(seeded.runtime)],env={**os.environ,'PYTHONPATH':str(ROOT)})
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools=await session.list_tools()
                names={t.name for t in tools.tools}
                assert {'get_next_batch','submit_batch_evaluation','heartbeat_batch','get_batch_report'}<=names
                assert not any('send' in name or 'approve' in name for name in names)
                claim=value(await session.call_tool('get_next_batch',{'job_id':'job-demo-001'}))
                assert len(claim['batch']['candidates'])==10
                assert all(c['candidate_label'].startswith('候选人 candidate-') for c in claim['batch']['candidates'])
                other=seeded.claim_batch('parallel-worker',job_id='job-demo-001',force=False)
                assert other['batch'] is None
                hb=value(await session.call_tool('heartbeat_batch',{'batch_id':claim['batch']['batch_id'],'lease_token':claim['lease_token'],'fencing_token':claim['fencing_token']}))
                assert hb['fencing_token']==claim['fencing_token']
                output=fixture_evaluation(claim['job'],claim['batch'])
                submitted=value(await session.call_tool('submit_batch_evaluation',{'batch_id':claim['batch']['batch_id'],'lease_token':claim['lease_token'],'fencing_token':claim['fencing_token'],'evaluation':output,'harness_name':'test_mcp_client_no_model'}))
                assert len(submitted['accepted'])==10
                report=value(await session.call_tool('get_batch_report',{'batch_id':claim['batch']['batch_id']}))
                assert report['completed']==10
    asyncio.run(run())
    assert len([c for c in seeded.list_candidates() if c['score']])==10
