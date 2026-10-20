"""Delete registration only; preserve files and protect live/historical refs."""
import concurrent.futures
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, project_registry as registry
from test_project_registry import setup, entry, execute


def candidate(setup, **extra):
    client,_,root,*_=setup
    response=client.post('/api/config/projects',json=dict(name='Unused registration',root_path=str(root),**extra))
    assert response.status_code==200,response.text
    return response.json()


def references(client, project):
    response=client.get(f"/api/config/projects/{project['id']}/references")
    assert response.status_code==200,response.text
    return response.json()


def test_delete_unused_registration_keeps_every_file_and_other_records(setup):
    client,path,root,*_=setup
    project=candidate(setup)
    before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert references(client,project)['can_delete']
    response=client.delete(f"/api/config/projects/{project['id']}")
    assert response.status_code==200 and response.json()['deleted']
    assert root.is_dir() and before=={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert client.get('/api/config/projects').json()==[setup[3]]
    assert len(db.list_executors(path))==1
    assert client.delete(f"/api/config/projects/{project['id']}").status_code==404
    assert client.get(f"/api/config/projects/{project['id']}/references").status_code==404
    replacement=candidate(setup)
    assert replacement['id']>project['id']


def test_disabled_entry_still_blocks_project_deletion_and_allows_disable(setup):
    client,path,root,project,*_=setup
    resource=entry(setup)
    assert client.put(f"/api/config/project-resources/{resource['id']}",json=dict(enabled=0)).status_code==200
    response=client.delete(f"/api/config/projects/{project['id']}")
    assert response.status_code==409
    rows=response.json()['references']
    assert any(r['type']=='测试入口 / 资产' and r['name']==resource['name'] and r['state']=='停用' for r in rows)
    assert any(r['type']=='执行机' for r in rows)
    assert client.put(f"/api/config/projects/{project['id']}",json=dict(enabled=0)).status_code==200
    assert not references(client,project)['can_delete']
    with registry.registry_scope(path):
        assert registry.find(resource['key']) and registry.find(project['key'])['enabled']==0


@pytest.mark.parametrize('kind',['executor','case','archived_plan'])
def test_direct_refs_block_even_without_project_entries(setup,kind):
    client,path,root,*_=setup
    project=candidate(setup)
    if kind=='executor':
        response=client.post('/api/config/executors',json=dict(name='Disabled project executor',python_ref=setup[4]['key'],cwd_root=project['key'],allowed_runners=['pytest'],enabled=0))
        expected='执行机'
    elif kind=='case':
        response=client.post('/api/test-cases',json=dict(name='Project case',engine='pytest',params=dict(cwd_root=project['key'],args=['tests/test_sample.py'])))
        expected='测试用例'
    else:
        response=client.post('/api/plans',json=dict(name='Archived project plan',environment_id=setup[6],executor_id=setup[5]['id'],
            steps=[dict(engine='pytest',params=dict(cwd_root=project['key'],args=['tests/test_sample.py']))]))
        assert response.status_code==200,response.text
        assert client.post(f"/api/plans/{response.json()['id']}/archive").status_code==200
        expected='测试计划'
    assert response.status_code==200,response.text
    result=client.delete(f"/api/config/projects/{project['id']}")
    assert result.status_code==409
    name='Archived project plan' if kind=='archived_plan' else response.json()['name']
    assert any(r['type']==expected and r['name']==name for r in result.json()['references'])
    if kind=='archived_plan':
        assert any(r['state']=='已归档' for r in result.json()['references'])


def test_transitive_case_suite_plan_and_asset_refs_are_named(setup):
    client,path,root,project,runtime,executor,env,app=setup
    visual=entry(setup,'matcheval')
    asset=client.post('/api/config/asset-sources',json=dict(name='Visual asset source',kind='matcheval',dataset_ref=visual['key'])).json()
    case=client.post('/api/test-cases',json=dict(name='Visual business case',engine='matcheval',params=dict(dataset=visual['key']))).json()
    ref={k:case[k] for k in ('id','revision','fingerprint')}
    suite=client.post('/api/suites',json=dict(name='Visual suite',cases=[ref])).json()
    plan=client.post('/api/plans',json=dict(name='Visual plan',environment_id=env,executor_id=executor['id'],asset_source_id=asset['id'],steps=[dict(case_ref=ref)])).json()
    assert 'id' in suite and 'id' in plan,(suite,plan)
    rows=references(client,project)['references']
    for kind,name in [('测试入口 / 资产',visual['name']),('资产源',asset['name']),('测试用例',case['name']),('测试套件','Visual suite'),('测试计划','Visual plan')]:
        assert any(r['type']==kind and r['name']==name for r in rows),(kind,rows)


def test_historical_snapshots_protect_after_current_refs_are_removed(setup):
    client,path,root,project,*_=setup
    resource=entry(setup)
    job,report=execute(setup,'pytest',dict(entry=resource['key']))
    assert job['status']=='success'
    original_report=db.get_report(path,job['id'])['report_json']
    # Simulate an older imported DB where current definitions no longer exist.
    with db._LOCK,db.get_conn(path) as conn:
        for table in ('project_resources','test_cases','plans'):
            conn.execute('DELETE FROM '+table)
        conn.execute("UPDATE executors SET cwd_root='backend-demo'")
    response=client.delete(f"/api/config/projects/{project['id']}")
    assert response.status_code==409
    kinds={r['type'] for r in response.json()['references']}
    assert {'运行记录','执行任务','历史报告'}<=kinds
    assert db.get_report(path,job['id'])['report_json']==original_report


def test_names_notes_and_similar_keys_are_not_references(setup):
    client,path,root,*_=setup
    project=candidate(setup)
    with db._LOCK,db.get_conn(path) as conn:
        conn.execute('INSERT INTO test_cases(name,engine,params) VALUES (?,?,?)',
            (project['key'],'pytest',json.dumps(dict(cwd_root=project['key']+'0',args=[project['key']]))))
    assert references(client,project)['can_delete']
    assert client.delete(f"/api/config/projects/{project['id']}").status_code==200


def test_delete_requires_local_request_header_and_same_origin(setup):
    client,*_=setup
    project=candidate(setup)
    url=f"/api/config/projects/{project['id']}"
    assert client.delete(url,headers={'X-Platform-Config':''}).status_code==403
    assert client.delete(url,headers={'Origin':'https://foreign.example'}).status_code==403
    assert references(client,project)['can_delete']


def test_concurrent_entry_registration_finishes_before_delete_check(setup,monkeypatch):
    client,path,root,*_=setup
    project=candidate(setup)
    entered,release,attempted=[threading.Event() for _ in range(3)]
    original=registry.validate_resource
    def validate(data,existing=None):
        if data.get('name')=='Concurrent entry':
            entered.set();assert release.wait(5)
        return original(data,existing)
    monkeypatch.setattr(registry,'validate_resource',validate)
    def remove():
        attempted.set()
        return client.delete(f"/api/config/projects/{project['id']}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        creation=pool.submit(client.post,'/api/config/project-resources',json=dict(name='Concurrent entry',kind='pytest',project_id=project['id'],config=dict(path='tests/test_sample.py')))
        try:
            assert entered.wait(5)
            deletion=pool.submit(remove);assert attempted.wait(5)
            assert not deletion.done()
        finally:
            release.set()
        assert creation.result(timeout=5).status_code==200
        response=deletion.result(timeout=5)
    assert response.status_code==409
    assert any(r['name']=='Concurrent entry' for r in response.json()['references'])


def test_new_entry_cannot_reference_an_already_deleted_project(setup):
    client,*_=setup
    project=candidate(setup)
    assert client.delete(f"/api/config/projects/{project['id']}").status_code==200
    response=client.post('/api/config/project-resources',json=dict(name='Late entry',kind='pytest',project_id=project['id'],config=dict(path='tests/test_sample.py')))
    assert response.status_code==400


def test_legacy_materialized_cwd_counts_as_a_reference(setup):
    client,path,root,project,*_=setup
    with db._LOCK,db.get_conn(path) as conn:
        conn.execute("UPDATE executors SET cwd_root='backend-demo'")
        conn.execute('INSERT INTO test_cases(name,engine,params) VALUES (?,?,?)',
            ('Legacy materialized case','pytest',json.dumps(dict(cwd=str(root).replace('\\','/'),args=['tests/test_sample.py']))))
    result=client.delete(f"/api/config/projects/{project['id']}")
    assert result.status_code==409
    assert any(r['name']=='Legacy materialized case' for r in result.json()['references'])
