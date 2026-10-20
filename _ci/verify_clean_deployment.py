"""Extract and install an archive, then import/run user-authored definitions.

Everything created by this verification stays under the selected workspace
output directory. It uses its own port, venv, data directory and test project.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request,build_opener,ProxyHandler
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--archive',required=True);parser.add_argument('--output',required=True);parser.add_argument('--port',type=int,default=18085)
    args=parser.parse_args();output=Path(args.output).resolve()
    if not output.is_relative_to(ROOT/'_ci_out'): raise ValueError('Verification output must stay under _ci_out')
    output.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(args.archive) as archive:
        for name in archive.namelist():
            if not (output/name).resolve().is_relative_to(output): raise ValueError('Archive path escapes verification folder')
        archive.extractall(output)
    platform=output/'platform'
    log=output/'install.log'
    with log.open('w',encoding='utf-8') as stream:
        process=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(platform/'install.ps1'),'-Offline','-PythonExe',sys.executable],cwd=platform,stdout=stream,stderr=subprocess.STDOUT,timeout=240)
    if process.returncode: raise RuntimeError('Clean install failed; see '+str(log))
    python=platform/'.venv/Scripts/python.exe'
    assert python.is_file()
    base=f'http://127.0.0.1:{args.port}'
    local_client=build_opener(ProxyHandler({}))  # Loopback must not use system HTTP proxies.
    def api(path,data=None,method=None):
        headers={'X-Platform-Config':'1'}
        if data is not None: headers['Content-Type']='application/json'
        request=Request(base+path,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,headers=headers,method=method)
        with local_client.open(request,timeout=20) as response: return json.load(response)
    server_log=(output/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([str(python),'-u','-B',str(platform/'backend/run.py'),'--port',str(args.port),'--no-browser','--data-dir',str(output/'service data'),'--db',str(output/'service data/db.sqlite')],cwd=platform,stdout=server_log,stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try: api('/health');break
            except Exception:
                if server.poll() is not None: raise RuntimeError('Clean service failed; see server.log')
                time.sleep(0.5)
        else: raise RuntimeError('Service did not become ready')
        assert api('/api/test-cases')==[] and api('/api/plans')==[] and api('/api/executors')==[]
        project=output/'用户自己的项目';(project/'tests').mkdir(parents=True)
        (project/'tests/test_owned.py').write_text('def test_owned():\n    assert 40 + 2 == 42\n',encoding='utf-8')
        p=api('/api/config/projects',dict(name='My own project',root_path=str(project)))
        runtime=api('/api/config/project-resources',dict(name='Clean Python',kind='runtime',config=dict(python_path=str(python))))
        executor=api('/api/config/executors',dict(name='Own pytest executor',python_ref=runtime['key'],cwd_root=p['key'],allowed_runners=['pytest']))
        env=api('/api/config/environments',dict(name='Own offline environment',kind='offline',host='',port=0))
        entry=api('/api/config/project-resources',dict(name='Own pytest entry',kind='pytest',project_id=p['id'],config=dict(path='tests/test_owned.py::test_owned')))
        document=api('/api/config/migration/template')
        document['cases'][0]['name']='User-authored own test';document['plans'][0]['name']='User-authored own plan'
        batch=api('/api/config/migration/imports',dict(name='Own definitions on clean install',document=document))
        mapping={'local-env':str(env['id']),'local-executor':str(executor['id']),'login-entry':entry['key']}
        check=api(f"/api/config/migration/imports/{batch['id']}/preview",dict(mapping=mapping))
        assert check['ok'],check
        created=api(f"/api/config/migration/imports/{batch['id']}/commit",dict(fingerprint=check['fingerprint']))
        plan=created['plans'][0]
        run=api(f"/api/plans/{plan['id']}/run",{})
        for _ in range(90):
            detail=api(f"/api/runs/{run['run_id']}")
            if detail['run']['status'] not in ('queued','running'): break
            time.sleep(0.5)
        assert detail['run']['status']=='success',detail
        result=dict(clean_venv=True,offline_install=True,empty_start=True,user_authored_import=True,
            own_project_executed=True,run=detail,platform=str(platform),python=str(python))
        (output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='run'},ensure_ascii=False),flush=True)
    finally:
        server.terminate()
        try: server.wait(timeout=10)
        except subprocess.TimeoutExpired: server.kill();server.wait(timeout=5)
        server_log.close()


if __name__=='__main__':main()
