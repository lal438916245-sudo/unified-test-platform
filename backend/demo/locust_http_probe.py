"""隔离 fixture：验证 locust_runner 端到端链路的真实 Locust 场景（不依赖 Colyseus/Unity）。

目标 host 由平台按 Environment 注入（--host <env>）。本场景只请求目标服务的只读 JSON 接口，
再三确认：若目标不可达（返回非 2xx / 无响应），Locust 全部请求失败 → 平台会诚实归档为 failed，
绝不用模拟成功掩盖环境问题。
"""
from locust import HttpUser, task, between


class FixtureHttpProbe(HttpUser):
    wait_time = between(0.5, 1.5)

    @task(3)
    def list_environments(self):
        self.client.get("/api/environments")

    @task(2)
    def list_executors(self):
        self.client.get("/api/executors")

    @task(1)
    def list_plans(self):
        self.client.get("/api/plans")