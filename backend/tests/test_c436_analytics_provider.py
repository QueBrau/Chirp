"""Realistic offline BigQuery protocol tests for the dormant analytics adapter."""
from __future__ import annotations
import json, time, uuid
import httpx
import pytest
from app.services.account_provider_analytics import (
 AnalyticsExclusion, AnalyticsFreezeReceipt, AnalyticsProviderUnavailable,
 AnalyticsSnapshot, AnalyticsTable, BigQueryAnalyticsProvider, PROBE, _digest,
)
USER='12345678-1234-4234-8234-123456789abc'; PROJECT='chirps-prod'; DATASET='chirps_analytics'
TABLE='run_googleapis_com_stdout_20261012'; RID='11111111-1111-4111-8111-111111111111'; KEY='c436:'+RID

def user_schema(): return {'fields':[{'name':'jsonPayload','type':'RECORD','mode':'NULLABLE','fields':[{'name':'analytics','type':'BOOLEAN','mode':'NULLABLE'},{'name':'user_id','type':'STRING','mode':'NULLABLE'}]}]}
def probe_schema(): return {'fields':[{'name':'jsonPayload','type':'RECORD','mode':'NULLABLE','fields':[{'name':x,'type':t,'mode':'NULLABLE'} for x,t in [('schema_version','STRING'),('emission_id','STRING'),('severity','STRING'),('event','STRING'),('emitted_at','TIMESTAMP'),('analytics','BOOLEAN'),('probe_id','STRING')]]}]}
def fr(): return AnalyticsFreezeReceipt(USER,'freeze',time.time()+600,True,True)
def exclusion(): return AnalyticsExclusion(PROBE,_digest(probe_schema()),True)
def meta(name=TABLE, schema=None): return {'type':'TABLE','tableReference':{'projectId':PROJECT,'datasetId':DATASET,'tableId':name},'schema':schema or user_schema()}
def capture_handler(request, *, count=2, added=False, stream=False):
 p=request.url.path
 if p.endswith('/datasets/'+DATASET): return httpx.Response(200,json={'datasetReference':{'projectId':PROJECT,'datasetId':DATASET},'location':'US','maxTimeTravelHours':'168'})
 if p.endswith('/tables'):
  rows=[meta()]
  return httpx.Response(200,json={'tables':rows,'nextPageToken':None})
 if '/tables/'+TABLE in p: return httpx.Response(200,json=meta())
 if p.endswith('/jobs'):
  b=json.loads(request.content); q=b['configuration']['query']['query']
  if b['configuration'].get('dryRun'): return httpx.Response(200,json={'jobReference':b['jobReference'],'configuration':b['configuration'],'status':{'state':'DONE'},'statistics':{'totalBytesProcessed':'10000000','query':{'totalBytesProcessed':'10000000','totalBytesProcessedAccuracy':'PRECISE'}}})
  return httpx.Response(200,json={'jobReference':b['jobReference'],'configuration':b['configuration'],'status':{'state':'DONE'},'statistics':{'query':{'totalBytesProcessed':'10000000','numDmlAffectedRows':str(count)}}})
 if '/queries/' in p:
  jid=p.rsplit('/',1)[-1]; value=0 if jid.startswith('chirp_verify_') else count; return httpx.Response(200,json={'jobReference':{'projectId':PROJECT,'location':'US','jobId':jid},'jobComplete':True,'totalRows':'1','schema':{'fields':[{'name':'remaining','type':'INTEGER','mode':'NULLABLE'}]},'rows':[{'f':[{'v':str(value)}]}]})
 return httpx.Response(500)

def snap(count=2, bytes_=50_000_000, schema_digest=None, exclusion=()):
 t=AnalyticsTable(TABLE,schema_digest or _digest(user_schema()),count,bytes_,10_000_000)
 s=AnalyticsSnapshot(RID,USER,PROJECT,DATASET,(t,),tuple(exclusion),'freeze',time.time(),168)
 return AnalyticsSnapshot(**{**s.__dict__,'snapshot_digest':_digest(s.payload())})

def make_provider(handler, s=None):
 actual=s or snap(); return BigQueryAnalyticsProvider(token='tok',project=PROJECT,dataset=DATASET,snapshot=actual,freeze=fr(),reviewed_snapshot_digest=actual.snapshot_digest,transport=httpx.MockTransport(handler),timeout_seconds=2)

@pytest.mark.asyncio
async def test_capture_inventory_preview_dryrun_and_query_envelope():
 async def h(req): return capture_handler(req)
 s=await BigQueryAnalyticsProvider.capture_preview_snapshot(token='tok',project=PROJECT,dataset=DATASET,request_id=RID,user_id=USER,freeze=fr(),reviewed_tables=(TABLE,),transport=httpx.MockTransport(h),timeout_seconds=2)
 assert s.tables[0].preview_count==2 and s.tables[0].max_bytes_billed==50_000_000

@pytest.mark.asyncio
async def test_apply_uses_count_guard_bytes_and_fresh_readback():
 calls=[]
 async def h(req):
  calls.append(req)
  return capture_handler(req,count=2)
 result=await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)
 posts=[json.loads(r.content) for r in calls if r.method=='POST']
 assert result['confirmed'] and result['retained_with_reason']==['bigquery_time_travel','bigquery_fail_safe']
 assert any('= @reviewed_count' in p['configuration']['query']['query'] for p in posts)
 assert all(p['configuration']['query']['maximumBytesBilled']=='50000000' for p in posts)

@pytest.mark.asyncio
async def test_inventory_added_table_blocks_before_delete():
 async def h(req):
  if req.url.path.endswith('/tables'):
   return httpx.Response(200,json={'tables':[meta(),meta('run_googleapis_com_stdout_20261013')]})
  return capture_handler(req)
 with pytest.raises(AnalyticsProviderUnavailable,match='inventory_drift'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_streaming_buffer_presence_blocks_even_empty():
 async def h(req):
  if '/tables/'+TABLE in req.url.path:
   x=meta(); x['streamingBuffer']={}; return httpx.Response(200,json=x)
  return capture_handler(req)
 with pytest.raises(AnalyticsProviderUnavailable,match='streaming_buffer_present'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_foreign_poll_reference_and_malformed_query_rows_fail():
 async def h(req):
  x=capture_handler(req)
  if req.url.path.endswith('/jobs'):
   b=json.loads(req.content); x= httpx.Response(200,json={'jobReference':{'projectId':'foreign','location':'US','jobId':b['jobReference']['jobId']},'configuration':b['configuration'],'status':{'state':'DONE'}})
  return x
 with pytest.raises(AnalyticsProviderUnavailable,match='job_binding_invalid'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_changed_delete_count_never_confirms():
 async def h(req): return capture_handler(req,count=1)
 with pytest.raises(AnalyticsProviderUnavailable,match='reviewed_count_changed'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_query_result_malformed_cells_fail_closed():
 async def h(req):
  if '/queries/' in req.url.path:
   jid=req.url.path.rsplit('/',1)[-1]
   return httpx.Response(200,json={'jobReference':{'projectId':PROJECT,'location':'US','jobId':jid},'jobComplete':True,'totalRows':'1','schema':{'fields':[{'name':'remaining','type':'INTEGER'}]},'rows':[{'f':[]}]})
  return capture_handler(req)
 with pytest.raises(AnalyticsProviderUnavailable,match='query_result_invalid'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_poll_location_mismatch_fails_closed():
 async def h(req):
  if req.url.path.endswith('/jobs'):
   b=json.loads(req.content); return httpx.Response(200,json={'jobReference':{'projectId':PROJECT,'location':'EU','jobId':b['jobReference']['jobId']},'configuration':b['configuration'],'status':{'state':'DONE'}})
  if '/jobs/' in req.url.path and '/queries/' not in req.url.path:
   jid=req.url.path.rsplit('/',1)[-1]
   return httpx.Response(200,json={'jobReference':{'projectId':PROJECT,'location':'EU','jobId':jid},'configuration':{'dryRun':False},'status':{'state':'DONE'}})
  return capture_handler(req)
 with pytest.raises(AnalyticsProviderUnavailable,match='job_binding_invalid'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_redirect_is_rejected_without_following():
 async def h(req): return httpx.Response(302,headers={'location':'https://evil.invalid'})
 with pytest.raises(AnalyticsProviderUnavailable,match='redirect_rejected'):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)

@pytest.mark.asyncio
async def test_lost_post_conflict_resolves_same_stable_plan_and_delete_ids():
 calls=[]; posted=[]
 async def h(req):
  calls.append(req)
  if req.method=='POST':
   b=json.loads(req.content); posted.append(b['jobReference']['jobId']); return httpx.Response(409,json={'error':{}})
  if '/jobs/' in req.url.path and '/queries/' not in req.url.path:
   jid=req.url.path.rsplit('/',1)[-1]; return httpx.Response(200,json={'jobReference':{'projectId':PROJECT,'location':'US','jobId':jid},'configuration':{'dryRun':False,'query':{'query':'x','useLegacySql':False}},'status':{'state':'DONE'},'statistics':{'query':{'numDmlAffectedRows':'2'}}})
  return capture_handler(req)
 with pytest.raises(AnalyticsProviderUnavailable):
  await make_provider(h).delete_account_data(user_id=USER,idempotency_key=KEY)
 assert len(posted)==1

@pytest.mark.parametrize('bad',[-1,1.0,True])
def test_snapshot_row_budget_types_rejected(bad):
 t=AnalyticsTable(TABLE,_digest(user_schema()),bad,50_000_000,10_000_000)
 s=AnalyticsSnapshot(RID,USER,PROJECT,DATASET,(t,),(),'freeze',time.time(),168)
 with pytest.raises(AnalyticsProviderUnavailable):
  BigQueryAnalyticsProvider(token='tok',project=PROJECT,dataset=DATASET,snapshot=s,freeze=fr(),reviewed_snapshot_digest='x',transport=httpx.MockTransport(lambda r:httpx.Response(500)))

@pytest.mark.asyncio
async def test_subject_credentials_and_request_binding_fail_closed():
 with pytest.raises(AnalyticsProviderUnavailable): await make_provider(lambda r:httpx.Response(500)).delete_account_data(user_id=USER,idempotency_key='other:'+RID)
 with pytest.raises(AnalyticsProviderUnavailable): BigQueryAnalyticsProvider(token='bad\n',project=PROJECT,dataset=DATASET,snapshot=snap(),freeze=fr(),reviewed_snapshot_digest=snap().snapshot_digest,transport=httpx.MockTransport(lambda r:httpx.Response(500)))
