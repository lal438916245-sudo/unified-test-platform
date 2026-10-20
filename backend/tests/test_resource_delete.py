"""Entry/runtime deletion preserves files, dependencies and execution history."""
import concurrent.futures
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from test_project_registry import setup, entry, execute


def resource(setup, kind):
    if kind!='runtime':
        return entry(setup,kind)
    response=setup[0].post('/api/config/project-resources',json=dict(name='Unused Python',kind='runtime',
        config=dict(python_path=sys.executable,modules=[])))
    assert response.status_code==200,response.text
    return response.json()


def refs(client,row):
    response=client.get(f"/api/config/project-resources/{row['id']}/references")
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.parametrize('kind',['runtime','pytest','airtest','locust','matcheval'])
def test_unused_resource_deleted_without_deleting_files_or_parent(setup,kind):
    client,path,root,project,*_=setup
    row=resource(setup,kind)
    files={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    python_stat=Path(sys.executable).stat()
    assert refs(client,row)['can_delete']
    response=client.delete(f"/api/config/project-resources/{row['id']}")
    assert response.status_code==200 and response.json()['deleted'],response.text
    assert row['id'] not in [r['id'] for r in client.get('/api/config/project-resources').json()]
    assert files=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert Path(sys.executable).is_file() and Path(sys.executable).stat().st_mtime_ns==python_stat.st_mtime_ns
    assert client.get('/api/config/projects').json()==[project]
    assert db.get_executor(path,setup[5]['id'])['python_ref']==setup[4]['key']
    assert client.delete(f"/api/config/project-resources/{row['id']}").status_code==404
    assert client.get(f"/api/config/project-resources/{row['id']}/references").status_code==404
    replacement=resource(setup,kind)
    assert replacement['id']>row['id']


def test_disabled_executor_and_archived_plan_block_runtime_deletion(setup):
    client,path,root,project,runtime,executor,env,*_=setup
    plan=client.post('/api/plans',json=dict(name='Runtime archived plan',environment_id=env,executor_id=executor['id'],
        steps=[dict(engine='pytest',params=dict(cwd_root=project['key'],args=['tests/test_sample.py']))])).json()
    assert client.post(f"/api/plans/{plan['id']}/archive").status_code==200
    assert client.put(f"/api/config/executors/{executor['id']}",json=dict(enabled=0)).status_code==200
    response=client.delete(f"/api/config/project-resources/{runtime['id']}")
    assert response.status_code==409
    sources=response.json()['references']
    assert any(r['type']=='执行机' and r['name']==executor['name'] and r['state']=='停用' for r in sources)
    assert any(r['type']=='测试计划' and r['name']=='Runtime archived plan' and r['state']=='已归档' for r in sources)
    assert client.put(f"/api/config/project-resources/{runtime['id']}",json=dict(enabled=0)).status_code==200
    assert not refs(client,runtime)['can_delete']


@pytest.mark.parametrize('kind,params',[('pytest',lambda r:dict(entry=r['key'])),('locust',lambda r:dict(locustfile=r['key']))])
def test_case_reference_blocks_entry_and_disabling_remains_allowed(setup,kind,params):
    client,*_=setup
    row=resource(setup,kind)
    case=client.post('/api/test-cases',json=dict(name='Disabled linked case',engine=kind,enabled=False,params=params(row)))
    assert case.status_code==200,case.text
    response=client.delete(f"/api/config/project-resources/{row['id']}")
    assert response.status_code==409
    assert any(r['type']=='测试用例' and r['name']=='Disabled linked case' and r['state']=='停用' for r in response.json()['references'])
    assert client.put(f"/api/config/project-resources/{row['id']}",json=dict(enabled=0)).status_code==200
    assert not refs(client,row)['can_delete']


def test_asset_child_keys_case_suite_and_plan_are_all_reported(setup):
    client,path,root,project,runtime,executor,env,*_=setup
    row=entry(setup,'matcheval')
    asset=client.post('/api/config/asset-sources',json=dict(name='Template child refs',kind='matcheval',
        tpl_ref=row['key']+'-tpl',scenes_ref=row['key']+'-scenes',ground_truth_ref='fixture-filename')).json()
    assert 'id' in asset,asset
    case=client.post('/api/test-cases',json=dict(name='Visual case',engine='matcheval',params=dict(dataset=row['key']))).json()
    ref={k:case[k] for k in ('id','revision','fingerprint')}
    suite=client.post('/api/suites',json=dict(name='Entry linked suite',cases=[ref])).json()
    plan=client.post('/api/plans',json=dict(name='Entry linked plan',environment_id=env,executor_id=executor['id'],
        steps=[dict(case_ref=ref)])).json()
    assert 'id' in plan and 'id' in suite,(plan,suite)
    assert client.post(f"/api/plans/{plan['id']}/archive").status_code==200
    sources=refs(client,row)['references']
    for kind,name in [('资产源','Template child refs'),('测试用例','Visual case'),('测试套件','Entry linked suite'),('测试计划','Entry linked plan')]:
        assert any(r['type']==kind and r['name']==name for r in sources),(kind,sources)
    assert client.delete(f"/api/config/project-resources/{row['id']}").status_code==409


def test_ui_requirement_blocks_entry_deletion(setup):
    client,*_=setup
    row=entry(setup,'airtest')
    response=client.post('/api/config/runtime-requirements',json=dict(name='UI linked requirements',suite=row['key'],modules=[],mongo_ref=''))
    assert response.status_code==200,response.text
    result=client.delete(f"/api/config/project-resources/{row['id']}")
    assert result.status_code==409
    assert any(r['type']=='公共运行要求' and r['name']=='UI linked requirements' for r in result.json()['references'])


def test_history_and_standalone_reports_block_entry_and_runtime_after_edits(setup):
    client,path,root,project,runtime,executor,*_=setup
    row=entry(setup)
    job,report=execute(setup,'pytest',dict(entry=row['key']))
    assert job['status']=='success'
    before=db.get_report(path,job['id'])['report_json']
    with db._LOCK,db.get_conn(path) as conn:
        conn.execute('DELETE FROM test_cases');conn.execute('DELETE FROM plans')
        conn.execute("UPDATE executors SET python_ref='anaconda',cwd_root='backend-demo'")
    for target in (row,runtime):
        response=client.delete(f"/api/config/project-resources/{target['id']}")
        assert response.status_code==409
        assert {'执行任务','运行记录','历史报告'} <= {r['type'] for r in response.json()['references']}
    with db._LOCK,db.get_conn(path) as conn:
        conn.execute('DELETE FROM jobs');conn.execute('DELETE FROM plan_runs')
    for target in (row,runtime):
        result=refs(client,target)
        assert not result['can_delete']
        assert any(r['type']=='历史报告' for r in result['references'])
    assert db.get_report(path,job['id'])['report_json']==before


def test_names_notes_and_similar_reference_keys_do_not_block(setup):
    client,path,*_=setup
    row=entry(setup)
    with db._LOCK,db.get_conn(path) as conn:
        conn.execute('INSERT INTO test_cases(name,engine,params) VALUES (?,?,?)',
            (row['key'],'pytest',json.dumps(dict(entry=row['key']+'0',args=[row['key']]))))
    assert refs(client,row)['can_delete']


def test_resource_delete_uses_existing_local_request_protection(setup):
    client,*_=setup
    row=resource(setup,'runtime');url=f"/api/config/project-resources/{row['id']}"
    assert client.delete(url,headers={'X-Platform-Config':''}).status_code==403
    assert client.delete(url,headers={'Origin':'https://foreign.example'}).status_code==403
    assert refs(client,row)['can_delete']


@pytest.mark.parametrize('kind',['runtime','pytest'])
def test_new_reference_cannot_race_with_deletion(setup,monkeypatch,kind):
    client,*_=setup
    row=resource(setup,kind)
    from app.api import router as api
    if kind == "runtime":
        from app.api import configuration as api
    entered,release,attempted=[threading.Event() for _ in range(3)]
    name='validate_executor_input' if kind=='runtime' else '_tc_errors'
    original=getattr(api,name)
    def validate(*args,**kwargs):
        entered.set();assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(api,name,validate)
    url='/api/config/executors' if kind=='runtime' else '/api/test-cases'
    data=dict(name='Concurrent resource executor',python_ref=row['key'],allowed_runners=['pytest']) if kind=='runtime' else dict(name='Concurrent resource case',engine='pytest',params=dict(entry=row['key']))
    def remove():
        attempted.set()
        return client.delete(f"/api/config/project-resources/{row['id']}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        creation=pool.submit(client.post,url,json=data)
        try:
            assert entered.wait(5)
            deletion=pool.submit(remove);assert attempted.wait(5)
            assert not deletion.done()
        finally:
            release.set()
        assert creation.result(timeout=5).status_code==200
        assert deletion.result(timeout=5).status_code==409


def test_deleted_runtime_cannot_be_selected_by_new_executor(setup):
    client,*_=setup
    row=resource(setup,'runtime')
    assert client.delete(f"/api/config/project-resources/{row['id']}").status_code==200
    response=client.post('/api/config/executors',json=dict(name='Stale runtime selector',python_ref=row['key'],allowed_runners=['pytest']))
    assert response.status_code==400,response.text
