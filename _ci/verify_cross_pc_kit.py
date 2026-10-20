"""Verify shipped selectors/JSON via isolated registration, import and execution."""
import json
from pathlib import Path
import shutil
import sys
import tempfile

from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app import db,config_migration
from app.api import build_app,router as api
from app.orchestration import scheduler


def main():
    output=Path(tempfile.mkdtemp(prefix='cross-pc-kit-',dir=ROOT/'_ci_out'))
    project=output/'目标机 不同目录项目'
    shutil.copytree(ROOT/'docs/异机验收材料/示例项目',project)
    path=str(output/'data/db.sqlite');Path(path).parent.mkdir()
    db.init_db(path)
    api.DATA_DIR=scheduler.DATA_DIR=str(output/'data')
    app=build_app(str(ROOT/'frontend'));app.state.db_path=path
    app.state.orchestrator=scheduler.Orchestrator(path)
    document=json.loads((ROOT/'docs/异机验收材料/自有用例与计划示例.json').read_text(encoding='utf-8'))
    config_migration.validate_document(document)
    def checked(response):
        assert response.status_code==200,(response.status_code,response.text)
        return response.json()
    with TestClient(app,headers={'X-Platform-Config':'1'}) as c:
        p=checked(c.post('/api/config/projects',json=dict(name='异机验收项目',root_path=str(project))))
        runtime=checked(c.post('/api/config/project-resources',json=dict(name='异机验收 Python',kind='runtime',config=dict(python_path=sys.executable))))
        ex=checked(c.post('/api/config/executors',json=dict(name='异机验收执行机',python_ref=runtime['key'],cwd_root=p['key'],allowed_runners=['pytest'])))
        env=checked(c.post('/api/config/environments',json=dict(name='异机离线校准环境',kind='offline',host='',port=0)))
        mapping={'own-env':str(env['id']),'own-executor':str(ex['id'])}
        for suffix,selector in [('pass','test_should_pass'),('fail','test_should_fail')]:
            entry=checked(c.post('/api/config/project-resources',json=dict(name='entry-'+suffix,kind='pytest',project_id=p['id'],config=dict(path='tests/test_acceptance.py::'+selector))))
            mapping['own-'+suffix+'-entry']=entry['key']
        batch=checked(c.post('/api/config/migration/imports',json=dict(name='操作包验证',document=document)))
        preview=checked(c.post(f"/api/config/migration/imports/{batch['id']}/preview",json=dict(mapping=mapping)))
        assert preview['ok'] and preview['counts']==dict(cases=2,suites=1,plans=2),preview
        created=checked(c.post(f"/api/config/migration/imports/{batch['id']}/commit",json=dict(fingerprint=preview['fingerprint'])))
        assert not db.list_runs_meta(path)
        jobs=[]
        for i,plan in enumerate(created['plans']):
            precheck=checked(c.post(f"/api/plans/{plan['id']}/precheck"))
            assert precheck['ok'],precheck
            assert len(db.list_runs_meta(path))==i
            run=checked(c.post(f"/api/plans/{plan['id']}/run"))
            job=db.list_jobs_by_run(path,run['run_id'])[0]
            app.state.orchestrator._process(job)
            job=db.get_job(path,job['id']);expected='success' if i==0 else 'failed'
            assert job['status']==expected,job
            record=checked(c.get(f"/api/reports/{job['id']}"))
            report=json.loads(record['report_json'])
            assert report['status']==expected and report['summary']['tests']==1,report
            assert report['summary']['passed' if i==0 else 'failed']==1
            log=Path(job['log_path']).read_text(encoding='utf-8')
            assert ('1 passed' if i==0 else 'CROSS_PC_EXPECTED_FAILURE') in log,log
            assert 'test_should_'+('pass' if i==0 else 'fail') in Path(job['junit_path']).read_text(encoding='utf-8')
            jobs.append(dict(id=job['id'],status=job['status'],summary=report['summary']))
        # The resource rejection described in the guide must precede run creation.
        checked(c.put(f"/api/config/executors/{ex['id']}",json={'enabled':0}))
        plan=created['plans'][0]
        assert not checked(c.post(f"/api/plans/{plan['id']}/precheck"))['ok']
        count=len(db.list_runs_meta(path))
        assert c.post(f"/api/plans/{plan['id']}/run").status_code==400
        assert len(db.list_runs_meta(path))==count
        checked(c.put(f"/api/config/executors/{ex['id']}",json={'enabled':1}))
        assert checked(c.post(f"/api/plans/{plan['id']}/precheck"))['ok']
    result=dict(output=str(output),registered_and_imported=True,precheck_no_execution=True,
                success_and_expected_failure=jobs,disabled_executor_rejected=True)
    (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
