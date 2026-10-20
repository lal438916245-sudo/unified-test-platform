"""Isolated browser acceptance server. Executes only tests registered in this DB."""
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
DATA=Path(os.environ.get('PLATFORM_PROJECT_WEB_DATA') or ROOT/'_ci_out/project-registry-web').resolve()
DATA.mkdir(parents=True,exist_ok=True)
os.environ['PLATFORM_DATA_DIR']=str(DATA)
sys.path.insert(0,str(ROOT/'backend'))
from app import db
from app.api import build_app
from app.orchestration.scheduler import Orchestrator
import uvicorn


def main():
    project=DATA/'new-local-project'
    (project/'tests').mkdir(parents=True,exist_ok=True)
    (project/'tests/test_example.py').write_text('''import os
def test_example():
    assert 2 + 2 == 4
    assert os.environ['PLATFORM_ENV_KIND']=='offline'
''',encoding='utf-8')
    path=str(DATA/'db.sqlite');db.init_db(path)
    if not db.list_environments(path):
        db.create_environment(path,dict(name='离线接入验收',kind='offline',host='',port=0,enabled=1))
    app=build_app(str(ROOT/'frontend'));app.state.db_path=path
    orch=Orchestrator(path);app.state.orchestrator=orch
    orch.start()
    print('Isolated project: '+str(project),flush=True)
    try:uvicorn.run(app,host='127.0.0.1',port=18084,log_level='warning')
    finally:orch.shutdown()


if __name__=='__main__':main()
