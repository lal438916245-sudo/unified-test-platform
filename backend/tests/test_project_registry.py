"""Project onboarding on isolated directories/DBs; no real client or service."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, project_registry as registry, config_center as cc
from app.api import build_app
from app.orchestration.scheduler import Orchestrator
from test_airtest import fake_device

HEADERS = {'X-Platform-Config':'1'}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path/'db.sqlite')
    db.init_db(path)
    root = tmp_path/'任意位置的新项目'
    root.mkdir()
    (root/'tests').mkdir()
    (root/'tests/test_sample.py').write_text('''import os
def test_environment():
    assert os.environ['PLATFORM_ENV_KIND']=='offline'
    assert os.getcwd().endswith('任意位置的新项目')
    print('REGISTERED-PROJECT-EXECUTED')
''', encoding='utf-8')
    (root/'tests/test_ui.py').write_text('def test_ui(platform_poco):\n    assert platform_poco is not None\n')
    (root/'load.py').write_text('from locust import User, task, constant\nclass Probe(User):\n    wait_time=constant(0.01)\n    @task\n    def idle(self): pass\n')
    (root/'tpl').mkdir();(root/'scenes').mkdir()
    (root/'tpl/tpl_1_a.png').write_bytes(b'image');(root/'scenes/scene_a.png').write_bytes(b'image')
    (root/'evaluate.py').write_text('''import argparse,csv
p=argparse.ArgumentParser()
for k in ('tpl-dir','scenes-dir','out','thresholds','algorithms'):p.add_argument('--'+k)
p.add_argument('--png',action='store_true'); a=p.parse_args()
with open(a.out,'w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=['template','scene','algorithm','threshold','status','confidence','time_ms'])
 w.writeheader();w.writerow(dict(template='tpl_1_a.png',scene='scene_a.png',algorithm='tpl',threshold='0.8',status='hit',confidence='0.99',time_ms='1'))
''')
    from app.api import router as api
    from app.orchestration import scheduler
    data = tmp_path/'data'
    monkeypatch.setattr(api, 'DATA_DIR', str(data))
    monkeypatch.setattr(scheduler, 'DATA_DIR', str(data))
    app = build_app(str(tmp_path/'static'))
    app.state.db_path=path;app.state.orchestrator=Orchestrator(path)
    with TestClient(app, headers=HEADERS) as c:
        p=c.post('/api/config/projects',json=dict(name='New project',root_path=str(root))).json()
        r=c.post('/api/config/project-resources',json=dict(name='New Python',kind='runtime',config=dict(python_path=sys.executable,modules=['json']))).json()
        ex=c.post('/api/config/executors',json=dict(name='New executor',python_ref=r['key'],cwd_root=p['key'],allowed_runners=['pytest','locust','matcheval','airtest'])).json()
        env=db.create_environment(path,dict(name='offline',kind='offline',host='',port=0,enabled=1))
        yield c,path,root,p,r,ex,env,app


def entry(setup, kind='pytest', **config):
    c,_,_,project,*_=setup
    defaults={'pytest':dict(path='tests/test_sample.py::test_environment'), 'airtest':dict(path='tests/test_ui.py'),
              'locust':dict(path='load.py'), 'matcheval':dict(tpl_dir='tpl',scenes_dir='scenes',script='evaluate.py')}
    response=c.post('/api/config/project-resources',json=dict(name='Entry '+kind,kind=kind,project_id=project['id'],config={**defaults[kind],**config}))
    assert response.status_code==200,response.text
    return response.json()


def execute(setup, engine, params):
    c,path,root,p,r,ex,env,app=setup
    tc=c.post('/api/test-cases',json=dict(name='Business '+engine,engine=engine,params=params))
    assert tc.status_code==200,tc.text
    ref={k:tc.json()[k] for k in ('id','revision','fingerprint')}
    plan=c.post('/api/plans',json=dict(name='Registered '+engine,environment_id=env,executor_id=ex['id'],steps=[dict(case_ref=ref)]))
    assert plan.status_code==200,plan.text
    run=c.post(f"/api/plans/{plan.json()['id']}/run")
    assert run.status_code==200,run.text
    job=db.list_jobs_by_run(path,run.json()['run_id'])[0]
    app.state.orchestrator._process(job)
    return db.get_job(path,job['id']),json.loads(db.get_report(path,job['id'])['report_json'])


def test_new_pytest_project_runs_and_reports_without_source_changes(setup):
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup)
    cat=c.get('/api/config/catalog').json()
    assert any(x['key']==p['key'] for x in cat['workdir_roots'])
    assert any(x['key']==r['key'] for x in cat['interpreters'])
    assert cat['pytest_entries'][0]['selector']=='tests/test_sample.py::test_environment'
    job,report=execute(setup,'pytest',dict(entry=e['key'],timeout_sec=30))
    assert job['status']=='success',job['error']
    assert report['summary']['passed']==1
    assert job['config_snapshot']['registration']
    assert job['params'].get('cwd_root') == p['key'] and 'cwd' not in job['params']
    assert str(root) not in json.dumps(job['config_snapshot'])
    assert '[project]' in ''.join(app.state.orchestrator.get_logs(job['id']))
    assert '1 passed' in ''.join(app.state.orchestrator.get_logs(job['id']))


def test_registered_visual_adapter_and_asset_execute(setup):
    c,path,root,p,*_=setup
    e=entry(setup,'matcheval')
    asset=c.post('/api/config/asset-sources',json=dict(name='New images',kind='matcheval',dataset_ref=e['key']))
    assert asset.status_code==200,asset.text
    assert c.post(f"/api/config/asset-sources/{asset.json()['id']}/precheck").json()['ok']
    job,report=execute(setup,'matcheval',dict(dataset=e['key'],algorithm=['tpl'],threshold=0.8,output_level='full',timeout_sec=30))
    assert job['status']=='success',job['error']
    assert report['metrics']['f1']==1


def test_registered_locust_uses_configured_script(setup):
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup,'locust')
    with registry.registry_scope(path):
        cc.resolve_workdir(p['key'])==str(root)
        assert cc.resolve_asset_ref('locust_scenario',e['key'])==str(root/'load.py')
        fp=cc.build_content_fingerprint('locust',dict(locustfile=e['key']))
    assert fp['sources']['locust_script']['exists']
    asset=c.post('/api/config/asset-sources',json=dict(name='New load',kind='locust',scenario_ref=e['key']))
    assert asset.status_code==200 and c.post(f"/api/config/asset-sources/{asset.json()['id']}/precheck").json()['ok']


def test_registered_locust_real_subprocess_against_isolated_http(setup):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    c,path,root,p,r,ex,env,app=setup
    python=cc.resolve_interpreter('locust-venv')
    if not python:
        pytest.skip('Locust runtime unavailable')
    (root/'load.py').write_text('from locust import HttpUser,task,constant\nclass Probe(HttpUser):\n    wait_time=constant(0.05)\n    @task\n    def probe(self): self.client.get("/probe")\n')
    entry_row=entry(setup,'locust')
    runtime=c.post('/api/config/project-resources',json=dict(name='Locust Python',kind='runtime',config=dict(python_path=python,modules=[]))).json()
    assert c.put(f"/api/config/executors/{ex['id']}",json=dict(python_ref=runtime['key'],allowed_runners=['locust'])).status_code==200
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'ok')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        db.update_environment(path,env,dict(kind='http',host='127.0.0.1',port=server.server_port))
        # Leave startup margin: Locust's run-time clock may include module import.
        job,report=execute(setup,'locust',dict(locustfile=entry_row['key'],users=1,spawn_rate=1,run_time='8s',timeout_sec=30))
        assert job['status']=='success',job['error']
        assert report['summary']['requests']>0 and report['summary']['failures']==0
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_registered_ui_entry_runs_in_worker_with_fake_device(setup,fake_device):
    c,path,_,p,r,ex,env,app=setup
    root,_=fake_device
    (root/'test_ui.py').write_text('def test_new_ui(platform_poco):\n    assert platform_poco.agent.hierarchy.dump()\n')
    project=c.post('/api/config/projects',json=dict(name='New UI project',root_path=str(root))).json()
    response=c.post('/api/config/project-resources',json=dict(name='New UI entry',kind='airtest',project_id=project['id'],config=dict(path='test_ui.py')))
    assert response.status_code==200,response.text
    e=response.json()
    job,report=execute(setup,'airtest',dict(suite=e['key'],case='test_ui.py',device='fake-device',wait_timeout=2,reset='none',timeout_sec=30))
    assert job['status']=='success',job['error']
    assert report['summary']['passed']==1
    assert report['engine_data']['connection_cleanup']['closed']
    assert report['artifacts']


def test_ui_entry_bound_to_airtest_including_default_collection(setup):
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup,'airtest')
    dev=c.post('/api/config/ui-devices',json=dict(name='Fake window',executor_id=ex['id'],title_match='NEVER_ATTACH')).json()
    ui=dict(suite=e['key'],case='tests/test_ui.py',device=f"ui-device-{dev['id']}",reset='none')
    assert c.post('/api/test-cases',json=dict(name='UI',engine='airtest',params=ui)).status_code==200
    for args in (['tests/test_ui.py'],['tests'],['.']):
        response=c.post('/api/test-cases',json=dict(name='Bypass',engine='pytest',params=dict(cwd_root=p['key'],args=args)))
        assert response.status_code==400
    # Explicit default pytest collection is refused even if the selected entry is absent.
    from app.executors.pytest_runner import PytestRunner
    with registry.registry_scope(path):
        result=PytestRunner().run(dict(params=dict(cwd=str(root),args=['tests','-o','addopts='])),{},dict(python_executable=sys.executable),str(root/'junit.xml'),lambda x:None,lambda:False)
    assert result.status=='failed'


def test_discovery_is_static_and_fixtures_are_not_imported(setup):
    c,path,root,p,*_=setup
    marker=root/'FIXTURE-RAN'
    (root/'conftest.py').write_text(f"raise RuntimeError('DO NOT IMPORT')\n")
    response=c.post(f"/api/config/projects/{p['id']}/discover")
    assert response.status_code==200,response.text
    rows=response.json()['candidates']
    assert any(x['engine']=='airtest' and x['path']=='tests/test_ui.py' for x in rows)
    assert any(x['path']=='tests/test_sample.py::test_environment' for x in rows)
    assert not marker.exists() and db.list_runs_meta(path)==[]


@pytest.mark.parametrize('path',['../escape.py','/absolute.py','C:/outside.py','tests\\test_ui.py','tests/test_ui.py;whoami','tests/missing.py','tests/test_sample.py::test_missing'])
def test_entry_path_escape_and_missing_files_rejected(setup,path):
    c,_,_,p,*_=setup
    response=c.post('/api/config/project-resources',json=dict(name='Bad',kind='pytest',project_id=p['id'],config=dict(path=path)))
    assert response.status_code==400,response.text


def test_disabled_resources_and_changes_cannot_redirect_queued_job(setup):
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup)
    body=dict(name='Frozen',environment_id=env,executor_id=ex['id'],steps=[dict(engine='pytest',params=dict(entry=e['key']))])
    plan=c.post('/api/plans',json=body).json()
    original_plan = c.get(f"/api/plans/{plan['id']}").json()
    run=c.post(f"/api/plans/{plan['id']}/run").json()
    job=db.list_jobs_by_run(path,run['run_id'])[0]
    original=db.get_run(path,run['run_id'])
    assert c.put(f"/api/config/projects/{p['id']}",json=dict(notes='changed binding revision')).status_code==200
    app.state.orchestrator._process(job)
    finished=db.get_job(path,job['id'])
    assert finished['status']=='failed' and '配置已变化' in finished['error']
    assert db.get_run(path,run['run_id'])['plan_snapshot']==original['plan_snapshot']
    assert c.put(f"/api/config/project-resources/{e['id']}",json=dict(enabled=0)).status_code==200
    assert c.post('/api/test-cases',json=dict(name='disabled',engine='pytest',params=dict(entry=e['key']))).status_code==400
    assert c.get(f"/api/plans/{plan['id']}").json()==original_plan


def test_local_configuration_rejects_foreign_origin_and_plain_posts(setup):
    c,path,root,p,*_=setup
    response=c.post('/api/config/projects',headers={'Origin':'https://foreign.example'},json=dict(name='foreign',root_path=str(root)))
    assert response.status_code==403
    with TestClient(build_app(str(root)),client=('203.0.113.1',5000)) as remote:
        assert remote.get('/api/config/projects').status_code==403
    assert c.post('/api/config/projects',headers={'X-Platform-Config':''},json={}).status_code==403


def test_per_database_catalog_isolation_and_persistence(setup,tmp_path):
    c,path,root,p,*_=setup
    entry(setup)
    other=str(tmp_path/'other.sqlite');db.init_db(other)
    app=build_app(str(tmp_path/'no-static'));app.state.db_path=other
    with TestClient(app) as second:
        def get(client):return client.get('/api/config/catalog').json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a,b=list(pool.map(get,(c,second)))
        assert a['pytest_entries'] and b['pytest_entries']==[]
        assert not any(x['key']==p['key'] for x in b['workdir_roots'])
    db.init_db(path)
    assert c.get('/api/config/projects').json()[0]['root_path']==str(root)


def test_runtime_modules_and_unavailable_project_are_reported(setup):
    c,path,root,p,r,ex,env,app=setup
    changed=c.put(f"/api/config/project-resources/{r['id']}",json=dict(config=dict(python_path=sys.executable,modules=['not_installed_for_registry_test'])))
    assert changed.status_code==200
    checked=c.post(f"/api/config/project-resources/{r['id']}/precheck").json()
    assert checked['ok'] is False and '运行环境补充依赖' in checked['missing']
    assert c.put(f"/api/config/projects/{p['id']}",json=dict(enabled=0)).status_code==200
    assert c.post('/api/plans',json=dict(name='disabled',environment_id=env,executor_id=ex['id'],steps=[dict(engine='pytest',params=dict(args=['demo/test_platform_demo.py']))])).status_code==400


def test_symlink_escape_is_rejected(setup,tmp_path):
    c,path,root,p,*_=setup
    outside=tmp_path/'outside.py';outside.write_text('def test_outside():pass')
    link=root/'outside.py'
    try:link.symlink_to(outside)
    except OSError:pytest.skip('Symlink creation requires Windows permission')
    response=c.post('/api/config/project-resources',json=dict(name='symlink',kind='pytest',project_id=p['id'],config=dict(path='outside.py')))
    assert response.status_code==400


def test_moved_project_and_file_can_be_disabled_without_losing_definitions(setup):
    c,path,root,p,*_=setup
    e=entry(setup)
    (root/'tests/test_sample.py').unlink()
    assert c.put(f"/api/config/project-resources/{e['id']}",json=dict(enabled=0)).status_code==200
    moved=root.with_name('moved-project')
    root.rename(moved)
    assert c.put(f"/api/config/projects/{p['id']}",json=dict(enabled=0)).status_code==200
    assert c.get('/api/config/projects').json()[0]['root_path']==str(root)


def test_readiness_accepts_registered_assets_and_checks_runtime_modules(setup,monkeypatch):
    c,path,root,p,r,ex,*_=setup
    entry(setup,'locust');entry(setup,'matcheval')
    monkeypatch.setattr(cc,'_run_py',lambda py,args,timeout=6: (-1,'missing') if 'missing_registered_module' in ' '.join(args) else (0,'ok'))
    engines={e['engine']:e for e in c.get('/api/config/readiness').json()['engines']}
    assert engines['locust']['runnable'] and engines['matcheval']['runnable']
    assert c.put(f"/api/config/project-resources/{r['id']}",json=dict(config=dict(python_path=sys.executable,modules=['missing_registered_module']))).status_code==200
    engines={e['engine']:e for e in c.get('/api/config/readiness').json()['engines']}
    assert engines['pytest']['runnable'] is False


def test_empty_registered_visual_assets_are_visible_in_precheck(setup):
    c,path,root,p,*_=setup
    e=entry(setup,'matcheval')
    (root/'tpl/tpl_1_a.png').unlink()
    asset=c.post('/api/config/asset-sources',json=dict(name='Empty new assets',kind='matcheval',dataset_ref=e['key'])).json()
    checked=c.post(f"/api/config/asset-sources/{asset['id']}/precheck").json()
    assert checked['ok'] is False and '模板图片已就位' in checked['missing']


def test_binding_edit_waits_for_child_start_but_not_whole_test(setup,monkeypatch):
    import threading
    from app.orchestration import scheduler
    from app.executors.base import RunnerResult
    c,path,root,p,r,ex,env,app=setup
    e=entry(setup)
    plan=c.post('/api/plans',json=dict(name='binding race',environment_id=env,executor_id=ex['id'],steps=[dict(engine='pytest',params=dict(entry=e['key']))])).json()
    run=c.post(f"/api/plans/{plan['id']}/run").json()
    job=db.list_jobs_by_run(path,run['run_id'])[0]
    ready,allow_start,allow_finish,editing,edited=[threading.Event() for _ in range(5)]
    class Runner:
        def run(self,job,env,executor,junit,log,cancelled,register):
            ready.set();assert allow_start.wait(5)
            register(None)
            assert allow_finish.wait(5)
            return RunnerResult(status='success',summary=dict(tests=1,passed=1))
    monkeypatch.setattr(scheduler,'get_runner',lambda engine:Runner())
    def edit():
        editing.set()
        result=c.put(f"/api/config/projects/{p['id']}",json=dict(notes='edit after launch'))
        edited.set();return result.status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        execution=pool.submit(app.state.orchestrator._process,job)
        try:
            assert ready.wait(5)
            change=pool.submit(edit);assert editing.wait(5)
            assert not edited.wait(0.1)
            allow_start.set()
            assert edited.wait(5) and not execution.done()
            assert change.result()==200
        finally:
            allow_start.set();allow_finish.set()
        execution.result(timeout=5)
