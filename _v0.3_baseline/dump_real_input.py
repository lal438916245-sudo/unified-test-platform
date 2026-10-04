"""导出真实运行库的计划与受控目录，供前端往返测试台使用。

只读：以 mode=ro 打开真实库；不写任何数据。
实体统一用平台的 mask_* 函数，保证与前端拿到的 API 形态一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(_HERE)                      # platform
sys.path.insert(0, os.path.join(PLATFORM, "backend"))

from app import config_center as cc  # noqa: E402

# 真实运行库与输出位置：默认按仓库根派生，可用环境变量覆盖（不硬编码本机路径）
DB = os.environ.get("PLATFORM_REAL_DB") or os.path.join(PLATFORM, "data", "db.sqlite")
OUT = os.environ.get("FE_INPUT_OUT") or os.path.join(_HERE, "fe_input.json")
import tempfile  # noqa: E402
import shutil  # noqa: E402
from app import db as appdb  # noqa: E402

con = sqlite3.connect(f"file:{DB.replace(chr(92), '/')}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

plans = []
for r in con.execute("SELECT * FROM plans ORDER BY id"):
    plans.append({
        "id": r["id"], "name": r["name"], "description": r["description"] or "",
        "environment_id": r["environment_id"], "executor_id": r["executor_id"],
        "asset_source_id": r["asset_source_id"] or 0, "fail_fast": bool(r["fail_fast"]),
        "owner": r["owner"] or "local", "revision": r["revision"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "steps": json.loads(r["steps"]),
        "steps_raw": r["steps"],
    })
con.close()

# 计划里的 steps 直接来自 DB（未经文本转义），实体则复用平台脱敏函数
tmp = tempfile.mkdtemp(prefix="pe_dump_")
try:
    p2 = os.path.join(tmp, "db.sqlite")
    shutil.copy2(DB, p2)
    payload = {
        "plans": plans,
        "envs": [cc.mask_environment(e) for e in appdb.list_environments(p2)],
        "execs": [cc.mask_executor(x) for x in appdb.list_executors(p2)],
        "assets": [cc.mask_asset_source(a) for a in appdb.list_asset_sources(p2)],
        "catalog": cc.public_catalog(),
    }
finally:
    # gc + 重试：sqlite 句柄释放滞后的规避（见 P1 技术债）
    import gc
    import time
    for _ in range(8):
        gc.collect()
        shutil.rmtree(tmp, ignore_errors=True)
        if not os.path.exists(tmp):
            break
        time.sleep(0.2)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False, indent=1)

print(f"  已导出 {len(plans)} 个计划")
print(f"  execs[0].allowed_runners = {payload['execs'][0].get('allowed_runners')!r}")
print(f"  含 cwd 的 pytest 步骤数 = {sum(1 for p in plans for s in p['steps'] if 'cwd' in (s.get('params') or {}))}")
print(f"  Locust 步骤数 = {sum(1 for p in plans for s in p['steps'] if s.get('engine') == 'locust')}")
