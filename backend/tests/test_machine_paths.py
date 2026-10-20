"""Deployment bindings use independent layouts, databases and child processes."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, config_center as cc, machine_paths as mp, project_registry as registry
from test_project_registry import setup, entry, execute


def bind(client, key, value):
    response = client.put('/api/config/machine-paths/'+key, json={'path': str(value)})
    assert response.status_code == 200, response.text
    return response.json()


def test_bindings_persist_per_database_and_override_environment(setup, tmp_path, monkeypatch):
    client, path, root, *_ = setup
    row = bind(client, 'workdir.gameautotest', root)
    monkeypatch.setenv('PLATFORM_PYTHON', str(tmp_path/'unavailable/python.exe'))
    db.init_db(path)
    with registry.registry_scope(path):
        assert cc.resolve_workdir('gameautotest') == str(root.resolve())
        assert cc.UI_SUITES['gameautotest-ui']['root'] == str(root.resolve())
        assert cc.resolve_interpreter('anaconda') == sys.executable
        assert next(r for r in mp.records() if r['key']==row['key'])['revision'] == row['revision']
    other = str(tmp_path/'another.sqlite')
    db.init_db(other)
    with registry.registry_scope(other):
        assert cc.resolve_workdir('gameautotest') != str(root.resolve())
    bind(client, 'python.anaconda', '')
    with registry.registry_scope(path):
        assert not cc.resolve_executor_python(dict(python_ref='anaconda', python_executable=sys.executable))


@pytest.mark.parametrize('key,value', [
    ('unknown', ''), ('python.anaconda','relative/python.exe'),
    ('workdir.gameautotest', 'missing-directory'), ('python.anaconda','not-python'),
    ('scenario.colyseus-bot','not-python'), ('asset.airtest-master','not-airtest'),
])
def test_invalid_paths_cannot_be_saved(setup, key, value):
    client,_,root,*_=setup
    if value=='not-python':
        value=root/'program.exe'; value.write_bytes(b'not executable')
    if value=='not-airtest': value=root
    response=client.put('/api/config/machine-paths/'+key,json={'path':str(value)})
    assert response.status_code==400,response.text


def test_cleared_project_does_not_fall_back_to_executor_directory(setup):
    client,path,root,*_=setup
    bind(client,'workdir.gameautotest','')
    from app.executors.pytest_runner import PytestRunner
    with registry.registry_scope(path):
        result=PytestRunner().run(dict(params=dict(cwd_root='gameautotest',args=['tests/test_sample.py'])),
            {},dict(python_executable=sys.executable,cwd=str(root)),str(root/'junit.xml'),lambda _:None,lambda:False)
    assert result.status=='failed' and '目录' in result.error


def test_legacy_reference_runs_after_directory_rebinding(setup):
    client,path,root,p,r,ex,env,app=setup
    bind(client,'workdir.gameautotest',root)
    job,report=execute(setup,'pytest',dict(cwd_root='gameautotest',args=['tests/test_sample.py','-o','addopts=']))
    assert job['status']=='success',job['error']
    assert report['summary']['passed']==1
    assert job['params']['cwd_root']=='gameautotest' and 'cwd' not in job['params']
    assert 'path:workdir.gameautotest' in job['config_snapshot']['registration']
    assert str(root) not in json.dumps(job['config_snapshot'])


def test_rebinding_prevents_queued_job_from_changing_target(setup):
    client,path,root,p,r,ex,env,app=setup
    bind(client,'workdir.gameautotest',root)
    plan=client.post('/api/plans',json=dict(name='Pending binding',environment_id=env,executor_id=ex['id'],
        steps=[dict(engine='pytest',params=dict(cwd_root='gameautotest',args=['tests/test_sample.py']))])).json()
    run=client.post(f"/api/plans/{plan['id']}/run").json()
    job=db.list_jobs_by_run(path,run['run_id'])[0]
    bind(client,'workdir.gameautotest',root)  # Revision changes even if the path is reused.
    app.state.orchestrator._process(job)
    finished=db.get_job(path,job['id'])
    assert finished['status']=='failed' and '配置已变化' in finished['error']
    assert not list(root.rglob('junit.xml'))


def test_legacy_visual_dataset_uses_configured_external_paths(setup):
    client,path,root,*_=setup
    for key,value in [('script.matcheval',root/'evaluate.py'),('asset.privahigh-real-tpl',root/'tpl'),
                      ('asset.privahigh-real-scenes',root/'scenes'),('asset.airtest-master','')]:
        bind(client,key,value)
    with registry.registry_scope(path):
        ds=cc.resolve_dataset('privahigh-real')
        assert ds['script']==str(root/'evaluate.py')
        assert ds['tpl_dir']==str(root/'tpl')
        assert cc.precheck_asset_source(dict(kind='matcheval',dataset_ref='privahigh-real'))['ok']
    job,report=execute(setup,'matcheval',dict(dataset='privahigh-real',algorithm=['tpl'],threshold=0.8))
    assert job['status']=='success',job['error']
    assert report['metrics']['f1']==1
    assert 'path:script.matcheval' in job['config_snapshot']['registration']


def test_external_visual_adapter_preloads_configured_airtest(setup,tmp_path):
    client,path,root,*_=setup
    external=tmp_path/'separate engine and assets';external.mkdir()
    configured=external/'selected-source';(configured/'airtest').mkdir(parents=True)
    (configured/'airtest/__init__.py').write_text("ORIGIN='selected'\n")
    legacy=external/'obsolete-source';(legacy/'airtest').mkdir(parents=True)
    (legacy/'airtest/__init__.py').write_text("raise RuntimeError('WRONG COPY OF AIRTEST')\n")
    code=(root/'evaluate.py').read_text()
    (external/'adapter.py').write_text(f"import sys\nsys.path.insert(0,{str(legacy)!r})\nimport airtest\nassert airtest.ORIGIN=='selected'\n"+code,encoding='utf-8')
    response=client.post('/api/config/project-resources',json=dict(name='Independent visual layout',kind='matcheval',
        project_id=setup[3]['id'],config=dict(script=str(external/'adapter.py'),tpl_dir=str(root/'tpl'),
            scenes_dir=str(root/'scenes'),airtest_source=str(configured))))
    assert response.status_code==200,response.text
    resource=response.json()
    job,report=execute(setup,'matcheval',dict(dataset=resource['key'],algorithm=['tpl'],threshold=0.8))
    assert job['status']=='success',job['error']
    assert report['metrics']['f1']==1
    assert str(external) not in ''.join(setup[-1].state.orchestrator.get_logs(job['id']))


def test_path_configuration_rejects_cross_site_and_missing_header(setup):
    client,*_=setup
    assert client.put('/api/config/machine-paths/python.anaconda',json=dict(path=sys.executable),
        headers={'Origin':'https://foreign.example'}).status_code==403
    assert client.put('/api/config/machine-paths/python.anaconda',json=dict(path=sys.executable),
        headers={'X-Platform-Config':''}).status_code==403


def test_inherited_asset_and_legacy_cwd_are_canonicalized_before_snapshot(setup):
    client,path,root,p,r,ex,env,app=setup
    resource=entry(setup,'matcheval')
    asset=client.post('/api/config/asset-sources',json=dict(name='Inherited visual input',kind='matcheval',dataset_ref=resource['key'])).json()
    plan=client.post('/api/plans',json=dict(name='Inherited asset',environment_id=env,executor_id=ex['id'],asset_source_id=asset['id'],
        steps=[dict(engine='matcheval',params=dict(algorithm=['tpl']))])).json()
    run=client.post(f"/api/plans/{plan['id']}/run").json()
    job=db.list_jobs_by_run(path,run['run_id'])[0]
    assert job['params']['dataset']==resource['key']
    app.state.orchestrator._process(job)
    assert db.get_job(path,job['id'])['status']=='success'
    from app.api.router import _resolve_step_params
    bind(client,'workdir.gameautotest',root)
    with registry.registry_scope(path):
        assert _resolve_step_params(dict(params=dict(cwd=str(root))),'pytest',{},None)['cwd_root'] in ('gameautotest',p['key'])
        assert _resolve_step_params(dict(params={}), 'matcheval',{},None)['dataset']=='fixture-small'
        assert cc.resolve_pytest_workdir({},dict(cwd_root='gameautotest',cwd='wrong'))==str(root)
        mp.save('workdir.gameautotest',dict(path=''))
        assert cc.resolve_pytest_workdir({},dict(cwd_root='gameautotest',cwd=str(root)))==''


def test_startup_and_runtime_sources_have_no_developer_drive_paths():
    platform=Path(__file__).resolve().parents[2]
    files=[platform/'start.ps1',platform/'start.bat',platform/'backend/preset.py']
    files+=list((platform/'backend/app').rglob('*.py'))
    for file in files:
        source=file.read_text(encoding='utf-8-sig')
        assert 'F:\\Anaconda3' not in source and 'F:\\Colyseus_SDET_Project' not in source,file
