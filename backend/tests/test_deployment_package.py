"""The deployment artifact contains service code, never runtime facts or venvs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('build_deployment',ROOT/'_ci/build_deployment.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


def test_deployment_archive_is_allowlisted_and_manifest_matches(tmp_path):
    archive=tmp_path/'platform.zip';result=builder.build(archive)
    assert result['bytes']>0
    with zipfile.ZipFile(archive) as z:
        names=set(z.namelist())
        assert 'platform/install.ps1' in names and 'platform/frontend/migration.js' in names
        assert 'platform/backend/app/config_migration.py' in names
        assert 'platform/frontend/help/cross-pc-acceptance.html' in names
        assert {n for n in names if n.startswith('platform/docs/') and n.endswith('.md')} == {
            'platform/docs/安装与配置迁移.md', 'platform/docs/新项目登记与执行.md',
            'platform/docs/换电脑与路径配置.md', 'platform/docs/平台使用说明.md',
        }
        assert not any(n.startswith('platform/docs/异机验收材料/') for n in names)
        forbidden=('platform/data/','platform/.venv','platform/backend/tests/','platform/_v0.','platform/_ci_out/')
        assert not any(n.startswith(forbidden) for n in names)
        assert not any(n.endswith(('.sqlite','.pyc','.log')) for n in names)
        manifest=json.loads(z.read('platform/installation-manifest.json'))
        assert not manifest['includes_user_data'] and not manifest['includes_project_code']
        for name,expected in manifest['files'].items(): assert hashlib.sha256(z.read(name)).hexdigest()==expected
    try: builder.build(archive)
    except FileExistsError: pass
    else: raise AssertionError('Existing artifacts must not be overwritten silently')
