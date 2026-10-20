"""Resource configuration endpoints: catalogs, readiness, devices and requirements."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import db, project_registry as registry
from ..config_center import (
    engine_readiness,
    mask_asset_source,
    mask_environment,
    mask_executor,
    precheck_asset_source,
    precheck_environment,
    precheck_executor,
    public_catalog,
    validate_asset_input,
    validate_environment_input,
    validate_executor_input,
)
from ..ui_config import device_binding_errors
from ..ui_devices import device_payload, precheck_device, probe_windows, validate_device_record
from .payloads import (
    asset_payload as _asset_payload,
    environment_payload as _env_payload,
    executor_payload as _exec_payload,
)

router = APIRouter()


def _app(request: Request):
    return request.app.state


# ---------- 资源（脱敏视图） ----------
@router.get("/environments")
def list_environments(request: Request):
    return [mask_environment(e) for e in db.list_environments(_app(request).db_path)]


@router.get("/executors")
def list_executors(request: Request):
    return [mask_executor(x) for x in db.list_executors(_app(request).db_path)]


@router.get("/asset-sources")
def list_asset_sources(request: Request):
    return [mask_asset_source(a) for a in db.list_asset_sources(_app(request).db_path)]


# ---------- 配置中心：目录 / 就绪度 ----------
@router.get("/config/catalog")
def config_catalog(request: Request):
    return public_catalog(_app(request).db_path)


@router.get("/config/readiness")
def config_readiness(request: Request):
    st = _app(request)
    return {"engines": engine_readiness(db, st.db_path)}


# ---------- 配置中心：Environment CRUD / 启停 / 预检 ----------
@router.get("/ui-devices")
def list_ui_devices(request: Request):
    return db.list_ui_devices(_app(request).db_path)


@router.post("/config/ui-devices")
def create_ui_device(payload: dict, request: Request):
    path = _app(request).db_path
    data = device_payload(payload)
    errors = validate_device_record(path, data)
    if errors:
        raise HTTPException(400, "；".join(errors))
    did = db.create_ui_device(path, data)
    return db.get_ui_device(path, did)


@router.put("/config/ui-devices/{did}")
def update_ui_device(did: int, payload: dict, request: Request):
    path = _app(request).db_path
    row = db.get_ui_device(path, did)
    if not row:
        raise HTTPException(404, "UI device not found")
    data = device_payload(payload, row)
    errors = validate_device_record(path, data, did)
    if errors:
        raise HTTPException(400, "；".join(errors))
    db.update_ui_device(path, did, data)
    return db.get_ui_device(path, did)


@router.post("/config/ui-devices/{did}/toggle")
def toggle_ui_device(did: int, request: Request):
    path = _app(request).db_path
    row = db.get_ui_device(path, did)
    if not row:
        raise HTTPException(404, "UI device not found")
    db.update_ui_device(path, did, {"enabled": 0 if row["enabled"] else 1})
    return db.get_ui_device(path, did)


@router.post("/config/ui-devices/{did}/precheck")
def precheck_ui_device(did: int, request: Request):
    path = _app(request).db_path
    row = db.get_ui_device(path, did)
    if not row:
        raise HTTPException(404, "UI device not found")
    return precheck_device(path, row)


@router.post("/config/ui-windows/probe")
def discover_ui_windows(payload: dict, request: Request):
    path = _app(request).db_path
    xid = payload.get("executor_id")
    if not isinstance(xid, int) or isinstance(xid, bool) or xid <= 0:
        raise HTTPException(400, "请先选择关联执行机（需允许 airtest）")
    ex = db.get_executor(path, xid)
    if not ex:
        raise HTTPException(400, "关联执行机不存在，请重新选择")
    errors = device_binding_errors({"executor_id": xid}, xid, path)
    if errors:
        raise HTTPException(400, "；".join(errors))
    try:
        # Inventory only. Payload cannot supply commands, URIs, paths or regex.
        return probe_windows(ex, {})
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/config/ui-devices/{did}/probe")
def probe_ui_device(did: int, request: Request):
    path = _app(request).db_path
    row = db.get_ui_device(path, did)
    if not row:
        raise HTTPException(404, "UI device not found")
    check = precheck_device(path, row)
    if not check["ok"]:
        return check
    try:
        result = probe_windows(db.get_executor(path, row["executor_id"]), row)
        count = len(result["windows"])
        check.update(ok=count == 1, windows=result["windows"])
        check["checks"].append(
            {
                "name": "窗口唯一匹配",
                "ok": count == 1,
                "detail": f"匹配 {count} 个窗口；此探测不连接 Poco、不点击",
            }
        )
        if count != 1:
            check["missing"] = ["窗口必须恰好匹配 1 个"]
            check["next_steps"] = ["启动客户端，或调整标题/进程/类名规则"]
        return check
    except (ValueError, OSError, TimeoutError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/config/ui-devices/{did}/runtime-probe")
def probe_ui_device_runtime(did: int, request: Request):
    path = _app(request).db_path
    row = db.get_ui_device(path, did)
    if not row:
        raise HTTPException(404, "UI device not found")
    static = precheck_device(path, row)
    if not static["ok"]:
        return static
    from ..runtime_precheck import precheck_runtime

    result = precheck_runtime(
        path,
        db.get_executor(path, row["executor_id"]),
        {"suite": "unity-smoke", "device": f"ui-device-{did}"},
        realtime=True,
    )
    return {
        **result,
        "entity": "UI 设备实时探测",
        "name": row["name"],
        "missing": [r["name"] for r in result["checks"] if r["status"] in ("failed", "busy")],
        "next_steps": [r["next_step"] for r in result["checks"] if r.get("next_step")],
    }


@router.post("/config/environments")
def create_environment(payload: dict, request: Request):
    st = _app(request)
    errs = validate_environment_input(payload, partial=False)
    if errs:
        raise HTTPException(400, "；".join(errs))
    data = _env_payload(payload)
    eid = db.create_environment(st.db_path, data)
    return mask_environment(db.get_environment(st.db_path, eid))


@router.put("/config/environments/{eid}")
def update_environment(eid: int, payload: dict, request: Request):
    st = _app(request)
    if not db.get_environment(st.db_path, eid):
        raise HTTPException(404, "environment not found")
    errs = validate_environment_input(payload, partial=True)
    if errs:
        raise HTTPException(400, "；".join(errs))
    db.update_environment(st.db_path, eid, _env_payload(payload, partial=True))
    return mask_environment(db.get_environment(st.db_path, eid))


@router.post("/config/environments/{eid}/toggle")
def toggle_environment(eid: int, request: Request):
    st = _app(request)
    e = db.get_environment(st.db_path, eid)
    if not e:
        raise HTTPException(404, "environment not found")
    db.update_environment(st.db_path, eid, {"enabled": 0 if int(e.get("enabled", 1)) else 1})
    return mask_environment(db.get_environment(st.db_path, eid))


@router.post("/config/environments/{eid}/precheck")
def precheck_env(eid: int, request: Request):
    st = _app(request)
    e = db.get_environment(st.db_path, eid)
    if not e:
        raise HTTPException(404, "environment not found")
    return precheck_environment(e)


# ---------- 配置中心：Executor CRUD / 启停 / 预检 ----------
@router.post("/config/executors")
@registry.locked
def create_executor(payload: dict, request: Request):
    st = _app(request)
    errs = validate_executor_input(payload, partial=False)
    if errs:
        raise HTTPException(400, "；".join(errs))
    xid = db.create_executor(st.db_path, _exec_payload(payload))
    return mask_executor(db.get_executor(st.db_path, xid))


@router.put("/config/executors/{xid}")
@registry.locked
def update_executor(xid: int, payload: dict, request: Request):
    st = _app(request)
    if not db.get_executor(st.db_path, xid):
        raise HTTPException(404, "executor not found")
    errs = validate_executor_input(payload, partial=True)
    if errs:
        raise HTTPException(400, "；".join(errs))
    db.update_executor(st.db_path, xid, _exec_payload(payload, partial=True))
    return mask_executor(db.get_executor(st.db_path, xid))


@router.post("/config/executors/{xid}/toggle")
def toggle_executor(xid: int, request: Request):
    st = _app(request)
    x = db.get_executor(st.db_path, xid)
    if not x:
        raise HTTPException(404, "executor not found")
    db.update_executor(st.db_path, xid, {"enabled": 0 if int(x.get("enabled", 1)) else 1})
    return mask_executor(db.get_executor(st.db_path, xid))


@router.post("/config/executors/{xid}/precheck")
def precheck_exec(xid: int, request: Request):
    st = _app(request)
    x = db.get_executor(st.db_path, xid)
    if not x:
        raise HTTPException(404, "executor not found")
    return precheck_executor(x)


# ---------- 配置中心：AssetSource CRUD / 启停 / 预检 ----------
@router.post("/config/asset-sources")
@registry.locked
def create_asset_source(payload: dict, request: Request):
    st = _app(request)
    errs = validate_asset_input(payload, partial=False)
    if errs:
        raise HTTPException(400, "；".join(errs))
    aid = db.create_asset_source(st.db_path, _asset_payload(payload))
    return mask_asset_source(db.get_asset_source(st.db_path, aid))


@router.put("/config/asset-sources/{aid}")
@registry.locked
def update_asset_source(aid: int, payload: dict, request: Request):
    st = _app(request)
    if not db.get_asset_source(st.db_path, aid):
        raise HTTPException(404, "asset source not found")
    errs = validate_asset_input(payload, partial=True)
    if errs:
        raise HTTPException(400, "；".join(errs))
    db.update_asset_source(st.db_path, aid, _asset_payload(payload, partial=True))
    return mask_asset_source(db.get_asset_source(st.db_path, aid))


@router.post("/config/asset-sources/{aid}/toggle")
def toggle_asset_source(aid: int, request: Request):
    st = _app(request)
    a = db.get_asset_source(st.db_path, aid)
    if not a:
        raise HTTPException(404, "asset source not found")
    db.update_asset_source(st.db_path, aid, {"enabled": 0 if int(a.get("enabled", 1)) else 1})
    return mask_asset_source(db.get_asset_source(st.db_path, aid))


@router.post("/config/asset-sources/{aid}/precheck")
def precheck_asset(aid: int, request: Request):
    st = _app(request)
    a = db.get_asset_source(st.db_path, aid)
    if not a:
        raise HTTPException(404, "asset source not found")
    return precheck_asset_source(a)


# ---------- 公共运行要求 ----------
# 公共要求是资源配置；用例只保存受控引用，执行时固化其内容。
@router.get("/runtime-requirements")
def list_runtime_requirements(request: Request):
    from ..runtime_requirements import catalog

    return catalog(_app(request).db_path)


@registry.locked
def _save_runtime_requirement(request, body, rid=None):
    from ..runtime_requirements import get_requirement, payload, validate_record

    path = _app(request).db_path
    existing = db.get_runtime_requirement(path, rid) if rid is not None else None
    if rid is not None and not existing:
        raise HTTPException(404, "runtime requirement not found")
    data = payload(body, existing)
    errors = validate_record(data)
    if isinstance(data.get("name"), str):
        data["name"] = data["name"].strip()
    if any(
        r["name"] == data.get("name") and r["id"] != rid for r in db.list_runtime_requirements(path)
    ):
        errors.append("已存在同名公共运行要求")
    if errors:
        raise HTTPException(400, "；".join(errors))
    data["name"] = data["name"].strip()
    data["modules"] = sorted(set(data["modules"]))
    rid = db.save_runtime_requirement(path, data, rid)
    return get_requirement(f"requirement-{rid}", path)


@router.post("/config/runtime-requirements")
def create_runtime_requirement(body: dict, request: Request):
    return _save_runtime_requirement(request, body)


@router.put("/config/runtime-requirements/{rid}")
def update_runtime_requirement(rid: int, body: dict, request: Request):
    return _save_runtime_requirement(request, body, rid)


@router.post("/config/runtime-requirements/{rid}/toggle")
def toggle_runtime_requirement(rid: int, request: Request):
    row = db.get_runtime_requirement(_app(request).db_path, rid)
    if not row:
        raise HTTPException(404, "runtime requirement not found")
    return _save_runtime_requirement(request, {"enabled": 0 if row["enabled"] else 1}, rid)
