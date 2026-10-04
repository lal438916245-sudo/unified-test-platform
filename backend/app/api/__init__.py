import os

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .router import router as api_router


def build_app(static_dir: str) -> FastAPI:
    app = FastAPI(title="统一游戏测试平台 MVP")

    @app.get("/health", include_in_schema=False)
    def _health() -> dict[str, str]:
        """最小存活探针：只证明 HTTP 服务已监听。

        严格零副作用 —— 不访问数据库、不创建 PlanRun/Job、不读取被测资产、
        不依赖 Environment / Executor / AssetSource，也不依赖 Locust / MatchEval。

        为什么必须存在：CI（self-hosted runner）启动 backend 后需要一个可靠的就绪判据。
        本机 `netstat` / `tasklist` 都会静默返回空，无法判断端口状态，只能靠 HTTP 探活。

        为什么挂在根路径：刻意绕开业务 router（/api/*）的依赖链，
        保证"服务已起来"与"配置/资产是否就绪"两件事互不牵连。
        """
        return {"status": "ok"}

    app.include_router(api_router)
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def _index():
            return RedirectResponse("/static/index.html")
    return app