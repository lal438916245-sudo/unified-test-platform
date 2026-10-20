"""Build an allowlisted archive without runtime DBs, venvs or external projects."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def sources():
    files={ROOT/name for name in ('README.md','UI_ENGINE.md','RUNTIME_REQUIREMENTS.md','start.ps1','start.bat','install.ps1','install.bat','requirements-core.txt',
        'requirements-ui.txt','requirements-locust.txt','requirements-vision.txt','backend/run.py','backend/preset.py','backend/platform_ui_guard.py')}
    for directory in ('backend/app','backend/demo','frontend','docs/images','docs/异机验收材料'):
        for path in (ROOT/directory).rglob('*'):
            if path.is_file() and not any(p.startswith('.') or p in ('__pycache__','reports','logs') for p in path.relative_to(ROOT).parts) and path.suffix not in ('.pyc','.log','.sqlite'):
                if not path.resolve().is_relative_to(ROOT): raise ValueError('Package source escapes the platform directory')
                files.add(path)
    for name in ('安装与配置迁移.md','新项目登记与执行.md','换电脑与路径配置.md',
                 '平台使用说明.md','异机完整验收操作手册.md','异机完整验收检查清单.md'):
        if (ROOT/'docs'/name).is_file(): files.add(ROOT/'docs'/name)
    return sorted(files)


def build(output,wheelhouse=None):
    output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    records={}
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sources():
            if not path.resolve().is_relative_to(ROOT): raise ValueError('Package source escapes the platform directory')
            name='platform/'+path.relative_to(ROOT).as_posix();data=path.read_bytes()
            archive.writestr(name,data);records[name]=hashlib.sha256(data).hexdigest()
        if wheelhouse:
            for path in sorted(Path(wheelhouse).glob('*.whl')):
                data=path.read_bytes();name='platform/wheelhouse/'+path.name
                archive.writestr(name,data);records[name]=hashlib.sha256(data).hexdigest()
        manifest=dict(format='unified-test-platform/deployment',version=1,
            built_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),files=records,
            includes_user_data=False,includes_project_code=False)
        archive.writestr('platform/installation-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    return dict(archive=str(output),files=len(records),bytes=output.stat().st_size,sha256=hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--wheelhouse')
    args=parser.parse_args();print(json.dumps(build(args.output,args.wheelhouse),ensure_ascii=False))
