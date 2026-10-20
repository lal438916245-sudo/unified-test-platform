"""Local configuration exchange; staged imports never start a runner."""

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config_migration as migration, db, project_registry as registry
from .projects import local_config

router = APIRouter(prefix="/api/config/migration", dependencies=[Depends(local_config)])


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except migration.MigrationConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, TypeError, RecursionError) as exc:
        raise HTTPException(
            400, str(exc) if isinstance(exc, ValueError) else "迁移数据类型或结构不正确"
        ) from None
    except sqlite3.IntegrityError:
        raise HTTPException(409, "数据库拒绝本次导入，定义写入已回滚；请重新预览后重试") from None


@router.get("/catalog")
def catalog(request: Request):
    path = request.app.state.db_path
    return dict(
        targets=migration.catalog(path),
        cases=[
            {k: r[k] for k in ("id", "name", "engine", "enabled")} for r in db.list_test_cases(path)
        ],
        suites=[{k: r[k] for k in ("id", "name", "enabled")} for r in db.list_suites(path)],
        plans=[{k: r[k] for k in ("id", "name", "archived")} for r in db.list_plans(path)],
    )


@router.get("/template")
def template():
    return migration.template()


@router.post("/export")
def export(data: dict, request: Request):
    document = call(migration.export_document, request.app.state.db_path, data)
    return JSONResponse(
        document,
        headers={"Content-Disposition": 'attachment; filename="platform-definitions.json"'},
    )


@router.get("/imports")
def batches(request: Request):
    return migration.batches(request.app.state.db_path)


@router.post("/imports")
def stage(data: dict, request: Request):
    call(migration._fields, data, ("name", "document"), ("name", "document"))
    return call(migration.stage, request.app.state.db_path, data["name"], data["document"])


@router.get("/imports/{ident}")
def batch(ident: int, request: Request):
    return call(migration.get_batch, request.app.state.db_path, ident)


@router.post("/imports/{ident}/preview")
def preview(ident: int, data: dict, request: Request):
    call(migration._fields, data, ("mapping", "prefix"), ("mapping",))
    return call(
        migration.preview, request.app.state.db_path, ident, data["mapping"], data.get("prefix", "")
    )


@router.post("/imports/{ident}/commit")
def commit(ident: int, data: dict, request: Request):
    call(migration._fields, data, ("fingerprint",), ("fingerprint",))
    return call(migration.commit, request.app.state.db_path, ident, data["fingerprint"])


@router.delete("/imports/{ident}")
@registry.locked
def discard(ident: int, request: Request):
    with db._LOCK, db.get_conn(request.app.state.db_path) as conn:
        row = conn.execute("SELECT status FROM migration_imports WHERE id=?", (ident,)).fetchone()
        if not row:
            raise HTTPException(404, "导入批次不存在")
        if row["status"] != "pending":
            raise HTTPException(409, "已提交批次保留导入回执，正式记录请在各自页面管理")
        conn.execute("DELETE FROM migration_imports WHERE id=?", (ident,))
    return {"discarded": True, "no_definition_deleted": True}
