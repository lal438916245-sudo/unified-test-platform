"""Definition exchange across independent DBs and project layouts; no real SUT."""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db,config_migration as migration,config_center as cc
from app.api import build_app
from app.orchestration.scheduler import Orchestrator
from test_project_registry import setup,entry,HEADERS


@pytest.fixture
def target(tmp_path,monkeypatch):
    root=tmp_path/'another computer project';(root/'tests').mkdir(parents=True)
    (root/'tests/test_target.py').write_text('def test_owned_target():\n    assert 6 * 7 == 42\n')
    (root/'tests/test_ui.py').write_text('def test_ui(platform_poco):\n    assert platform_poco is not None\n')
    (root/'conftest.py').write_text("from pathlib import Path\nPath(__file__).with_name('IMPORTED').write_text('fixture imported')\n")
    path=str(tmp_path/'target.sqlite');db.init_db(path)
    from app.api import router as api
    from app.orchestration import scheduler
    monkeypatch.setattr(api,'DATA_DIR',str(tmp_path/'target-data'))
    monkeypatch.setattr(scheduler,'DATA_DIR',str(tmp_path/'target-data'))
    app=build_app(str(tmp_path/'static'));app.state.db_path=path;app.state.orchestrator=Orchestrator(path)
    with TestClient(app,headers=HEADERS) as client:
        p=client.post('/api/config/projects',json=dict(name='Target project',root_path=str(root))).json()
        r=client.post('/api/config/project-resources',json=dict(name='Target Python',kind='runtime',config=dict(python_path=sys.executable))).json()
        ex=client.post('/api/config/executors',json=dict(name='Target executor',python_ref=r['key'],cwd_root=p['key'],allowed_runners=['pytest','airtest','locust','matcheval'])).json()
        env=client.post('/api/config/environments',json=dict(name='Target offline environment',kind='offline',host='',port=0)).json()
        e=client.post('/api/config/project-resources',json=dict(name='Target test entry',kind='pytest',project_id=p['id'],config=dict(path='tests/test_target.py::test_owned_target'))).json()
        yield dict(client=client,path=path,root=root,project=p,runtime=r,executor=ex,environment=env,entry=e,app=app)


def source_document(setup):
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup)
    case=c.post('/api/test-cases',json=dict(name='Portable case',engine='pytest',params=dict(entry=e['key'],cwd_root=p['key'],args=['tests/test_sample.py::test_environment','-o','addopts=','--tb=short']))).json()
    ref={k:case[k] for k in ('id','revision','fingerprint')}
    suite=c.post('/api/suites',json=dict(name='Portable suite',cases=[ref])).json()
    plan=c.post('/api/plans',json=dict(name='Portable plan',environment_id=env,executor_id=ex['id'],steps=[dict(case_ref=ref,override_params=dict(timeout_sec=25),name='Imported step')])).json()
    response=c.post('/api/config/migration/export',json=dict(case_ids=[],suite_ids=[suite['id']],plan_ids=[plan['id']]))
    assert response.status_code==200,response.text
    return response.json(),case,suite,plan


def mapping_for(doc,target):
    fixed=dict(environment=str(target['environment']['id']),executor=str(target['executor']['id']),pytest_entry=target['entry']['key'])
    return {r['id']:fixed.get(r['type'],r.get('builtin_key','')) for r in doc['resources']}


def stage(client,doc,name='Transferred definitions'):
    response=client.post('/api/config/migration/imports',json=dict(name=name,document=doc))
    assert response.status_code==200,response.text
    return response.json()


def preview(client,batch,mapping,prefix=''):
    response=client.post(f"/api/config/migration/imports/{batch['id']}/preview",json=dict(mapping=mapping,prefix=prefix))
    assert response.status_code==200,response.text
    return response.json()


def test_export_is_path_free_and_includes_dependencies(setup):
    doc,case,suite,plan=source_document(setup)
    assert len(doc['cases'])==len(doc['suites'])==len(doc['plans'])==1
    text=json.dumps(doc,ensure_ascii=False)
    assert str(setup[2]) not in text and sys.executable not in text
    assert 'args' not in doc['cases'][0]['params'] and 'cwd_root' not in doc['cases'][0]['params']
    assert doc['plans'][0]['steps'][0]['case']==doc['cases'][0]['id']
    assert 'fingerprint' not in text and 'python_path' not in text


def test_cross_database_import_rebuilds_refs_without_overwriting_and_runs_owned_test(setup,target):
    doc,*_=source_document(setup);client=target['client'];path=target['path']
    existing=client.post('/api/test-cases',json=dict(name='Portable case',engine='pytest',params=dict(entry=target['entry']['key']))).json()
    before=db.get_test_case(path,existing['id'])
    batch=stage(client,doc)
    assert db.list_runs_meta(path)==[]
    pending=preview(client,batch,{})
    assert not pending['ok'] and pending['errors'] and len(db.list_test_cases(path))==1
    mapping=mapping_for(doc,target)
    check=preview(client,batch,mapping)
    assert check['ok'],check
    assert not (target['root']/'IMPORTED').exists()
    assert check['names']['cases'][0]['name']=='Portable case (导入 2)'
    response=client.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert response.status_code==200,response.text
    created=response.json();case=db.get_test_case(path,created['cases'][0]['id'])
    suite=db.get_suite(path,created['suites'][0]['id']);plan=db.get_plan(path,created['plans'][0]['id'])
    assert db.get_test_case(path,existing['id'])==before
    assert case['id']!=existing['id'] and case['revision']==1
    assert suite['cases'][0]['id']==case['id'] and suite['cases'][0]['fingerprint']==case['fingerprint']
    assert plan['steps'][0]['case_ref']==suite['cases'][0]
    assert plan['steps'][0]['override_params']==dict(timeout_sec=25)
    assert plan['executor_id']==target['executor']['id'] and case['params']['entry']==target['entry']['key']
    retry=client.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert retry.json()==created and len(db.list_test_cases(path))==2
    assert not (target['root']/'IMPORTED').exists()
    run=client.post(f"/api/plans/{plan['id']}/run")
    assert run.status_code==200,run.text
    job=db.list_jobs_by_run(path,run.json()['run_id'])[0]
    target['app'].state.orchestrator._process(job)
    assert db.get_job(path,job['id'])['status']=='success'
    assert (target['root']/'IMPORTED').exists()


def test_user_authored_template_imports_without_source_platform(target):
    c=target['client'];doc=c.get('/api/config/migration/template').json()
    doc['cases'][0]['name']='My own scenario';doc['plans'][0]['name']='My own plan'
    batch=stage(c,doc,'User authored')
    check=preview(c,batch,mapping_for(doc,target),prefix='Own · ')
    assert check['ok'],check
    result=c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert result.status_code==200,result.text
    assert db.get_plan(target['path'],result.json()['plans'][0]['id'])['name']=='Own · My own plan'


def test_pending_mapping_survives_restart_and_stale_preview_is_rejected(target):
    c=target['client'];doc=migration.template();batch=stage(c,doc)
    check=preview(c,batch,mapping_for(doc,target))
    assert check['ok']
    db.init_db(target['path'])
    loaded=c.get(f"/api/config/migration/imports/{batch['id']}").json()
    assert loaded['mapping']==mapping_for(doc,target)
    assert c.put(f"/api/config/projects/{target['project']['id']}",json=dict(notes='Changed after preview')).status_code==200
    result=c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert result.status_code==409 and not db.list_test_cases(target['path'])


def test_atomic_rollback_if_database_refuses_plan_insert(target):
    c=target['client'];batch=stage(c,migration.template());check=preview(c,batch,mapping_for(batch['document'],target))
    assert check['ok']
    with db._LOCK,db.get_conn(target['path']) as conn:
        conn.execute("CREATE TRIGGER refuse_plan BEFORE INSERT ON plans BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    response=c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert response.status_code==409,response.text
    assert not db.list_test_cases(target['path']) and not db.list_suites(target['path']) and not db.list_plans(target['path'])
    assert c.get(f"/api/config/migration/imports/{batch['id']}").json()['status']=='pending'


@pytest.mark.parametrize('change',[
    lambda d:d.update(version=99),
    lambda d:d['resources'][0].update(path='C:/private'),
    lambda d:d['cases'][0]['params'].update(entry={'$resource':'missing'}),
    lambda d:d['plans'][0].update(environment=['bad']),
    lambda d:d['plans'][0]['steps'][0].update(engine='pytest'),
    lambda d:d['plans'][0]['steps'][0].update(override_params={'engine':'airtest'}),
])
def test_invalid_document_rejected_before_staging(target,change):
    doc=migration.template();change(doc)
    response=target['client'].post('/api/config/migration/imports',json=dict(name='Bad document',document=doc))
    assert response.status_code==400,response.text
    assert target['client'].get('/api/config/migration/imports').json()==[]


def test_unsafe_pytest_arguments_are_not_committed(target):
    doc=migration.template();doc['cases'][0]['params']=dict(cwd_root={'$resource':'project'},args=['../escape.py'])
    doc['resources'].append(dict(id='project',type='workdir',name='Target project'))
    c=target['client'];batch=stage(c,doc);mapping=mapping_for(doc,target);mapping['project']=target['project']['key']
    check=preview(c,batch,mapping)
    assert not check['ok'] and check['errors']
    assert not db.list_test_cases(target['path'])


def test_export_rejects_stale_case_versions_instead_of_changing_plan_meaning(setup):
    doc,case,suite,plan=source_document(setup);c=setup[0]
    assert c.put(f"/api/test-cases/{case['id']}",json=dict(name=case['name'],engine=case['engine'],params={**case['params'],'timeout_sec':99})).status_code==200
    result=c.post('/api/config/migration/export',json=dict(plan_ids=[plan['id']]))
    assert result.status_code==400 and '版本' in result.text


def test_pending_discard_and_local_api_protection(target):
    c=target['client'];batch=stage(c,migration.template())
    url=f"/api/config/migration/imports/{batch['id']}"
    assert c.delete(url,headers={'X-Platform-Config':''}).status_code==403
    assert c.delete(url,headers={'Origin':'https://foreign.example'}).status_code==403
    assert c.delete(url).json()['no_definition_deleted']
    assert c.get(url).status_code==404


@pytest.mark.parametrize('engine',['pytest','airtest','locust','matcheval'])
def test_each_engine_can_import_without_running_fixtures_or_probes(target,engine,monkeypatch):
    c=target['client'];resources=[];mapping={}
    if engine=='pytest':
        resources=[dict(id='entry',type='pytest_entry',name='Own entry')]
        params=dict(entry={'$resource':'entry'});mapping['entry']=target['entry']['key']
    elif engine=='airtest':
        device=c.post('/api/config/ui-devices',json=dict(name='No real device',executor_id=target['executor']['id'],title_match='NEVER_ATTACH',smoke_selector='safe',smoke_result='result')).json()
        resources=[dict(id='suite',type='ui_suite',name='Own UI'),dict(id='device',type='ui_device',name='Own device')]
        params=dict(suite={'$resource':'suite'},device={'$resource':'device'},case='test_unity_smoke.py',reset='none')
        mapping=dict(suite='unity-smoke',device='ui-device-'+str(device['id']))
    elif engine=='locust':
        resources=[dict(id='load',type='locust_scenario',name='Own load')]
        params=dict(locustfile={'$resource':'load'},users=2,spawn_rate=1,run_time='10s');mapping['load']='http-fixture'
    else:
        resources=[dict(id='images',type='dataset',name='Own images')]
        params=dict(dataset={'$resource':'images'},algorithm=['tpl'],threshold=0.8);mapping['images']='fixture-small'
    def forbidden(*args,**kwargs): raise AssertionError('Import must not execute probes')
    monkeypatch.setattr(cc,'_run_py',forbidden)
    doc=dict(format=migration.FORMAT,version=1,resources=resources,cases=[dict(id='own',name='Own '+engine,engine=engine,params=params)])
    batch=stage(c,doc);check=preview(c,batch,mapping)
    assert check['ok'],check
    response=c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint']))
    assert response.status_code==200,response.text
    assert not db.list_runs_meta(target['path']) and not (target['root']/'IMPORTED').exists()


def test_disabled_and_archived_definitions_are_preserved(target):
    doc=migration.template();doc['cases'][0]['enabled']=False;doc['suites'][0]['enabled']=False;doc['plans'][0]['archived']=True
    c=target['client'];batch=stage(c,doc);check=preview(c,batch,mapping_for(doc,target))
    assert check['ok'] and check['warnings']
    result=c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=check['fingerprint'])).json()
    assert db.get_test_case(target['path'],result['cases'][0]['id'])['enabled']==0
    assert db.get_suite(target['path'],result['suites'][0]['id'])['enabled']==0
    assert db.get_plan(target['path'],result['plans'][0]['id'])['archived']==1
    assert c.post(f"/api/plans/{result['plans'][0]['id']}/run").status_code==409
    assert not db.list_runs_meta(target['path'])


def test_legacy_workdir_and_secret_values_are_not_exported(setup,monkeypatch):
    c,path,root,p,r,ex,env,*_=setup
    monkeypatch.setenv('MIGRATION_SECRET_TEST','NEVER_EXPORT_THIS_VALUE')
    assert c.put(f'/api/config/environments/{env}',json=dict(secret_ref='MIGRATION_SECRET_TEST',secret_kind='env_var')).status_code==200
    case=c.post('/api/test-cases',json=dict(name='Legacy cwd case',engine='pytest',params=dict(cwd=str(root),args=['tests/test_sample.py']))).json()
    ref={k:case[k] for k in ('id','revision','fingerprint')}
    plan=c.post('/api/plans',json=dict(name='Legacy cwd plan',environment_id=env,executor_id=ex['id'],steps=[dict(case_ref=ref)])).json()
    response=c.post('/api/config/migration/export',json=dict(plan_ids=[plan['id']]))
    assert response.status_code==200,response.text
    text=json.dumps(response.json(),ensure_ascii=False)
    assert str(root) not in text and 'NEVER_EXPORT_THIS_VALUE' not in text
    assert 'secret_ref' not in text and 'python_executable' not in text
    assert 'cwd' not in response.json()['cases'][0]['params']
