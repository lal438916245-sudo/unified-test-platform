"""Local configuration of project paths, runtimes and engine entry points."""

import ast
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import machine_paths, project_registry as registry


def local_config(request: Request):
    # These APIs intentionally accept host paths; they belong to the local admin UI.
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "项目路径配置仅允许本机访问")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "拒绝跨站配置请求")
    if (
        request.method in ("POST", "PUT", "DELETE")
        and request.headers.get("x-platform-config") != "1"
    ):
        raise HTTPException(403, "配置请求缺少本机平台标记，请刷新平台页面")


router = APIRouter(prefix="/api/config", dependencies=[Depends(local_config)])


@router.get("/machine-paths")
def machine_path_list():
    return machine_paths.public_records()


@router.put("/machine-paths/{key}")
def machine_path_save(key: str, data: dict):
    try:
        return machine_paths.save(key, data)
    except (ValueError, OSError, TypeError) as exc:
        raise HTTPException(
            400, str(exc) if isinstance(exc, ValueError) else "本机路径不可用"
        ) from None


def require(table, ident):
    row = next((r for r in registry.records(table) if r["id"] == ident), None)
    if not row:
        raise HTTPException(404, "登记记录不存在")
    return row


@registry.locked
def save(table, data, ident=None):
    old = require(table, ident) if ident else None
    fields = (
        ("name", "root_path", "notes", "enabled")
        if table == "projects"
        else ("name", "project_id", "kind", "config", "enabled")
    )
    merged = {**{k: old[k] for k in fields if old and k in old}, **data}
    try:
        cleaned = (
            registry.validate_project(merged, old)
            if table == "projects"
            else registry.validate_resource(merged, old)
        )
        return registry.save(table, cleaned, ident)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "已存在同名项目或入口 / 运行环境") from None
    except (ValueError, OSError, TypeError) as exc:
        # Validation messages are controlled and do not include host file contents.
        detail = str(exc) if isinstance(exc, ValueError) else "本机路径不可用或字段类型不正确"
        raise HTTPException(400, registry.redact(detail)) from None


@router.get("/projects")
def projects():
    return registry.records("projects")


@router.post("/projects")
def create_project(data: dict):
    return save("projects", data)


@router.put("/projects/{ident}")
def update_project(ident: int, data: dict):
    return save("projects", data, ident)


@router.get("/projects/{ident}/references")
def project_references(ident: int):
    from ..project_references import inspect

    try:
        return inspect(ident)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


@router.delete("/projects/{ident}")
def delete_project(ident: int):
    from ..project_references import delete

    try:
        result = delete(ident)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    if not result["can_delete"]:
        return JSONResponse(status_code=409, content={**result, "detail": result["message"]})
    return {**result, "deleted": True}


@router.get("/project-resources")
def resources():
    return registry.records("project_resources")


@router.post("/project-resources")
def create_resource(data: dict):
    return save("project_resources", data)


@router.put("/project-resources/{ident}")
def update_resource(ident: int, data: dict):
    return save("project_resources", data, ident)


@router.get("/project-resources/{ident}/references")
def resource_references(ident: int):
    from ..project_references import inspect

    try:
        return inspect(ident, resource=True)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None


@router.delete("/project-resources/{ident}")
def delete_resource(ident: int):
    from ..project_references import delete

    try:
        result = delete(ident, resource=True)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    if not result["can_delete"]:
        return JSONResponse(status_code=409, content={**result, "detail": result["message"]})
    return {**result, "deleted": True}


@router.post("/projects/{ident}/discover")
def discover(ident: int):
    project = require("projects", ident)
    root = Path(project["root_path"])
    candidates, scanned = [], 0
    ignored = {".git", ".venv", "venv", "node_modules", "__pycache__", "reports", "data"}
    for path in root.rglob("test_*.py"):
        relative = path.relative_to(root)
        if any(p.startswith(".") or p in ignored for p in relative.parts):
            continue
        scanned += 1
        if scanned > 300:
            break
        try:
            registry.contained(root, relative.as_posix())
            if path.stat().st_size > 1024 * 1024:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            ui = any(
                (
                    isinstance(n, (ast.Import, ast.ImportFrom))
                    and any(
                        s.startswith(("airtest", "poco"))
                        for s in (
                            [n.module or ""]
                            if isinstance(n, ast.ImportFrom)
                            else [a.name for a in n.names]
                        )
                    )
                )
                or (isinstance(n, ast.arg) and n.arg in ("platform_poco", "init_game_poco"))
                for n in ast.walk(tree)
            )
            selectors = [
                n.name
                for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("test_")
            ]
            selectors += [
                n.name + "::" + m.name
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name.startswith("Test")
                for m in n.body
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test_")
            ]
            for selector in selectors:
                candidates.append(
                    dict(
                        path=relative.as_posix() if ui else relative.as_posix() + "::" + selector,
                        engine="airtest" if ui else "pytest",
                        function=selector,
                    )
                )
        except (OSError, ValueError, SyntaxError, UnicodeError):
            continue
    return {
        "project": project["key"],
        "candidates": candidates,
        "partial": scanned > 300,
        "note": "仅静态读取 Python 语法，未导入项目、未执行 pytest / fixture；参数化用例数量以实际运行收集为准。",
    }


@router.post("/projects/{ident}/precheck")
def check_project(ident: int):
    row = require("projects", ident)
    ok = Path(row["root_path"]).is_dir() and bool(row["enabled"])
    return dict(
        ok=ok,
        checks=[
            dict(
                name="项目目录与启用状态", ok=ok, detail="可用" if ok else "目录不可用或项目已停用"
            )
        ],
    )


@router.post("/project-resources/{ident}/precheck")
def check_resource(ident: int):
    row = require("project_resources", ident)
    if row["kind"] == "runtime":
        from ..config_center import precheck_executor

        return precheck_executor(
            dict(name=row["name"], python_ref=row["key"], allowed_runners=["pytest"])
        )
    try:
        registry.validate_resource(
            {k: row[k] for k in ("name", "project_id", "kind", "config", "enabled")}, row
        )
        category = {
            "pytest": "pytest",
            "airtest": "ui",
            "locust": "locust",
            "matcheval": "datasets",
        }[row["kind"]]
        ok = row["key"] in registry.catalog(category)
    except (ValueError, OSError):
        ok = False
    return dict(
        ok=ok,
        checks=[
            dict(
                name="入口 / 资产路径与启用状态",
                ok=ok,
                detail=(
                    "文件与引用可用，未运行测试；引擎依赖和设备状态请在计划预检中检查"
                    if ok
                    else "项目或入口停用、文件移动或配置不可用"
                ),
            )
        ],
    )
