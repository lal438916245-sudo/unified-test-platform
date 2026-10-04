"""配置中心：把 pytest / Locust / match_eval 依赖的环境变量、解释器路径、执行机、
数据集/资产目录与目标服务地址，收敛为平台内受控、可校验、可版本化引用的配置实体。

安全原则（与规格冻结约束一致）：
- 前端与 Plan 只提交 ID/key，绝不提交任意命令、路径、Python 可执行文件或素材目录；
  所有路径类输入一律是"服务端受控目录"里的 key。
- 只保存环境变量名或外部 secret reference，不保存明文密钥；
  对 secret reference 只校验"可用/不可用"，绝不返回值。
- 一切对外响应（API / 日志 / report.json / report_index / 比较元数据）必须脱敏：
  不返回明文绝对路径、Token、账号或密码。

本模块只做只读派生、受控解析与输入校验，不改写任何引擎算法逻辑。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
from typing import Any, Optional

# ---------- 基础路径（与既有 preset / runner 对齐，避免重复定义） ----------
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # backend
_ROOT = os.path.dirname(_BACKEND)                                        # platform
_WORKSPACE = os.path.dirname(_ROOT)                                      # 测试平台（工作区根）

LOCUST_VENV_PY = os.path.join(_ROOT, ".venv-locust", "Scripts", "python.exe")
TRAE_PY = os.path.join(os.environ.get("APPDATA", ""), "TRAE SOLO CN", "ModularData",
                       "ai-agent", "vm", "tools", "python", "python.exe")
# ---- 环境相关常量：集中在此，且允许用环境变量覆盖（换机器 / 隔离实例时不必改代码）----
DEFAULT_PYTHON = os.environ.get("PLATFORM_DEFAULT_PYTHON") or r"F:\Anaconda3\python.exe"

GAME_PROJ = os.path.join(_WORKSPACE, "GameAutoTest-Pro")

# 真实 SDET 项目根：PrivaHigh 游戏工程与 Airtest 源码所在（本机固定路径，与 DEFAULT_PYTHON 同风格）。
# 仅用作受控目录根的解析基准；绝不返回前端，也不落日志 / 快照。
SDET_PROJ = os.environ.get("PLATFORM_SDET_PROJ") or r"F:\Colyseus_SDET_Project"
# 已采集并实测验证的真实 PrivaHigh 素材（5 场景 / 23 模板，阈值 0.8 起 P=R=F1=1.00）
PRIVAHIGH_ASSETS = os.path.join(_WORKSPACE, "real_assets", "privahigh")

# 弃用环境变量登记（过渡兼容层：仍读取，但标注弃用并提示迁移到配置实体）
_DEPRECATED_ENV = ("PLATFORM_PYTHON", "PLATFORM_LOCUST_PYTHON",
                   "PLATFORM_MATCHEVAL_PYTHON", "PLATFORM_MATCHEVAL_SCRIPT",
                   "PLATFORM_AIRTEXT_DIR")
_seen_deprecated: set[str] = set()


def _env_legacy(name: str) -> str:
    """过渡层：读取旧环境变量（已弃用）。命中则登记，供 /config/catalog 提示迁移。"""
    v = os.environ.get(name) or ""
    if v:
        _seen_deprecated.add(name)
    return v


def deprecation_notices() -> list[str]:
    """返回本次进程内实际命中过的弃用环境变量名（不含值）。"""
    return sorted(_seen_deprecated)


def _h(*parts) -> str:
    """稳定脱敏指纹：规范化后 sha256 前 12 位，不落任何敏感原文。"""
    norm = "|".join(str(p) for p in parts if p not in (None, ""))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            got = json.loads(v)
            return got if isinstance(got, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


# ---------- 受控目录：key -> 解析（仅服务端使用，绝不返回前端） ----------
INTERPRETER_PROFILES: dict[str, dict] = {
    "anaconda": {"label": "Anaconda 主解释器", "deps": {"pytest": ["pytest"]}},
    "locust-venv": {"label": "Locust 隔离 venv", "deps": {"locust": ["locust"]}},
    "matcheval": {"label": "MatchEval 解释器（cv2/numpy）",
                  "deps": {"matcheval": ["cv2", "numpy"]}},
    "trae-py": {"label": "内置工具 Python", "deps": {}},
}

WORKDIR_ROOTS: dict[str, dict] = {
    "backend-demo": {"label": "平台 demo（backend）", "path": _BACKEND},
    "platform": {"label": "平台根目录", "path": _ROOT},
    "gameautotest": {"label": "GameAutoTest-Pro（需服务端）", "path": GAME_PROJ},
    "privahigh-backend": {"label": "PrivaHigh 后端（功能评估）",
                          "path": os.path.join(SDET_PROJ, "PrivaHigh", "backend")},
}

# 底层素材目录（key -> 绝对路径）。Job/Plan 只能引用 key，不能提交任意目录。
TPL_DIRS: dict[str, dict] = {
    "matcheval-fixture-tpl": {"label": "隔离 fixture 模板",
                              "path": os.path.join(_BACKEND, "demo", "matcheval_fixture", "tpl")},
    "matcheval-empty-tpl": {"label": "空模板（失败归档验证）",
                            "path": os.path.join(_BACKEND, "demo", "matcheval_fixture", "empty", "tpl")},
    "privahigh-real-tpl": {"label": "PrivaHigh 真实模板小图（23 张，1920×1080 采集）",
                           "path": os.path.join(PRIVAHIGH_ASSETS, "tpl")},
}
SCENES_DIRS: dict[str, dict] = {
    "matcheval-fixture-scenes": {"label": "隔离 fixture 场景",
                                 "path": os.path.join(_BACKEND, "demo", "matcheval_fixture", "scenes")},
    "matcheval-empty-scenes": {"label": "空场景（失败归档验证）",
                               "path": os.path.join(_BACKEND, "demo", "matcheval_fixture", "empty", "scenes")},
    "privahigh-real-scenes": {"label": "PrivaHigh 真实整屏截图（5 张，1920×1080 采集）",
                              "path": os.path.join(PRIVAHIGH_ASSETS, "scenes")},
}
GROUND_TRUTH_REFS: dict[str, dict] = {
    "fixture-filename": {"label": "文件名约定真值（tpl_{id}_{scene}.png）"},
    "none": {"label": "无真值（仅验证失败归档）"},
}
# 受控 dataset：与 matcheval_runner 的白名单一致（key -> 底层目录 key + 真值约定）。
DATASET_REFS: dict[str, dict] = {
    "fixture-small": {"label": "隔离合成 fixture（小样本）",
                      "tpl_dir": "matcheval-fixture-tpl",
                      "scenes_dir": "matcheval-fixture-scenes",
                      "ground_truth": "fixture-filename"},
    "fixture-empty": {"label": "空素材（失败归档验证）",
                      "tpl_dir": "matcheval-empty-tpl",
                      "scenes_dir": "matcheval-empty-scenes",
                      "ground_truth": "none"},
    "privahigh-real": {"label": "PrivaHigh 真实游戏素材（5 场景 / 23 模板）",
                       "tpl_dir": "privahigh-real-tpl",
                       "scenes_dir": "privahigh-real-scenes",
                       "ground_truth": "fixture-filename"},
}
# Airtest 资产目录：真实目录须由用户录入后经配置/环境变量登记，MVP 不擅自指向真实资产。
# 默认基准指向真实 SDET 项目根下的 Airtest 源码（= match_eval.py 内置 AIRTEST_SRC 的同一目录）。
AIRTEXT_DIRS: dict[str, dict] = {
    "airtest-master": {"label": "Airtest-master（真实源码目录）",
                       "path": _env_legacy("PLATFORM_AIRTEXT_DIR")
                       or os.path.join(SDET_PROJ, "Airtest-master")},
}
# Locust 场景：与 locust_runner 白名单一致（key 引用，不落绝对路径）。
LOCUST_SCENARIOS: dict[str, dict] = {
    "http-fixture": {"label": "平台自身 API（隔离 fixture）",
                     "path": os.path.join(_BACKEND, "demo", "locust_http_probe.py")},
    "colyseus-bot": {"label": "Colyseus-Storm 联机底稿（需 localhost:2567）",
                     "path": os.path.join(_WORKSPACE, "Colyseus-Storm", "locustfile.py")},
}

ENGINES = ("pytest", "locust", "matcheval")
ENV_KINDS = ("http", "colyseus", "offline")
ENV_PROTOCOLS = ("http", "https")
SECRET_KINDS = ("env_var", "external_ref")
ASSET_KINDS = ("matcheval", "locust")

# 引擎 -> 关键依赖模块（预检用）
_ENGINE_DEPS: dict[str, list[str]] = {
    "pytest": ["pytest"],
    "locust": ["locust"],
    "matcheval": ["cv2", "numpy"],
}


def public_catalog() -> dict:
    """对外目录：只暴露 key + label，绝不暴露任何绝对路径或密钥。"""
    def keys(d: dict) -> list[dict]:
        return [{"key": k, "label": v.get("label", k)} for k, v in d.items()]

    return {
        "interpreters": keys(INTERPRETER_PROFILES),
        "workdir_roots": keys(WORKDIR_ROOTS),
        "engines": list(ENGINES),
        "env_kinds": list(ENV_KINDS),
        "env_protocols": list(ENV_PROTOCOLS),
        "secret_kinds": list(SECRET_KINDS),
        "asset_kinds": list(ASSET_KINDS),
        "asset": {
            "datasets": keys(DATASET_REFS),
            "tpl_dirs": keys(TPL_DIRS),
            "scenes_dirs": keys(SCENES_DIRS),
            "ground_truths": keys(GROUND_TRUTH_REFS),
            "airtest_dirs": keys(AIRTEXT_DIRS),
            "locust_scenarios": keys(LOCUST_SCENARIOS),
        },
        "deprecated_env": {
            "names": list(_DEPRECATED_ENV),
            "seen": deprecation_notices(),
            "note": "以上环境变量为过渡兼容层，已弃用；请迁移为 Environment/Executor/AssetSource 配置实体。",
        },
    }


# ---------- 解析（仅服务端；结果绝不返回前端） ----------
def _can_import(py: str, mods: list[str], timeout: float = 10.0) -> bool:
    if not mods:
        return True
    try:
        p = subprocess.run([py, "-c", f"import {', '.join(mods)}"],
                           capture_output=True, timeout=timeout)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_MATCHEVAL_PY_CACHE: dict[str, str] = {}


def _resolve_matcheval_python() -> str:
    """MatchEval 解释器：在受控候选中优先选"确实能导入 cv2+numpy"的那个（进程内缓存）。
    若无候选满足，仍返回首个存在者，交由预检给出可行动提示（不掩盖配置缺失）。"""
    if "v" in _MATCHEVAL_PY_CACHE:
        return _MATCHEVAL_PY_CACHE["v"]
    cands = [_env_legacy("PLATFORM_MATCHEVAL_PYTHON"), DEFAULT_PYTHON,
             LOCUST_VENV_PY, TRAE_PY]
    existing = [c for c in cands if c and os.path.exists(c)]
    chosen = next((c for c in existing if _can_import(c, ["cv2", "numpy"])), "")
    _MATCHEVAL_PY_CACHE["v"] = chosen or (existing[0] if existing else "")
    return _MATCHEVAL_PY_CACHE["v"]


def resolve_interpreter(ref: str) -> str:
    """受控解释器引用 -> 绝对路径。ref 非法/不可用返回 ''。"""
    ref = (ref or "").strip()
    if ref == "anaconda":
        return _env_legacy("PLATFORM_PYTHON") or DEFAULT_PYTHON
    if ref == "locust-venv":
        return _env_legacy("PLATFORM_LOCUST_PYTHON") or (
            LOCUST_VENV_PY if os.path.exists(LOCUST_VENV_PY) else "")
    if ref == "matcheval":
        return _resolve_matcheval_python()
    if ref == "trae-py":
        return TRAE_PY if os.path.exists(TRAE_PY) else ""
    return ""


def resolve_executor_python(executor: dict) -> str:
    """执行机解释器：优先受控引用；旧记录（迁移前直接存绝对路径）回退到 legacy 列。"""
    p = resolve_interpreter(executor.get("python_ref") or "")
    if p:
        return p
    legacy = str(executor.get("python_executable") or "")
    return legacy if legacy and os.path.exists(legacy) else ""


def resolve_workdir(root_key: str) -> str:
    return (WORKDIR_ROOTS.get(root_key or "") or {}).get("path", "")


def resolve_asset_ref(kind: str, ref: str) -> str:
    table = {"tpl_dir": TPL_DIRS, "scenes_dir": SCENES_DIRS, "ground_truth": GROUND_TRUTH_REFS,
             "airtest_dir": AIRTEXT_DIRS, "locust_scenario": LOCUST_SCENARIOS}.get(kind, {})
    return (table.get(ref or "") or {}).get("path", "")


def resolve_dataset(dataset_ref: str) -> dict:
    """受控 dataset key -> {tpl_dir, scenes_dir, ground_truth} 绝对路径（仅服务端）。"""
    d = DATASET_REFS.get(dataset_ref or "")
    if not d:
        return {}
    return {
        "tpl_dir": resolve_asset_ref("tpl_dir", d.get("tpl_dir")),
        "scenes_dir": resolve_asset_ref("scenes_dir", d.get("scenes_dir")),
        "ground_truth": d.get("ground_truth"),
    }


def resolve_runtime_executor(executor: dict) -> dict:
    """把执行机配置解析为运行时可用视图（含明文路径，仅限后端内部使用，绝不外泄）。"""
    x = dict(executor or {})
    x["python_executable"] = resolve_executor_python(executor or {})
    x["cwd"] = resolve_workdir((executor or {}).get("cwd_root") or "") or (executor or {}).get("cwd") or ""
    x["allowed_runners"] = _json_list((executor or {}).get("allowed_runners")) or list(ENGINES)
    return x


def resolve_runtime_env(env: dict) -> dict:
    """被测环境运行时视图。offline 环境无需 host/port。"""
    e = dict(env or {})
    e["kind"] = e.get("kind") or "http"
    e["protocol"] = e.get("protocol") or "http"
    return e


def infer_interpreter_ref(path: str) -> str:
    """迁移辅助：把旧记录里的绝对路径反推为受控引用 key；无法反推返回 ''。"""
    p = (path or "").replace("\\", "/").lower()
    if not p:
        return ""
    if p == DEFAULT_PYTHON.replace("\\", "/").lower() or "anaconda" in p:
        return "anaconda"
    if ".venv-locust" in p:
        return "locust-venv"
    if "trae solo" in p:
        return "trae-py"
    return ""


def infer_workdir_root(path: str) -> str:
    p = (path or "").replace("\\", "/").rstrip("/").lower()
    for k, v in WORKDIR_ROOTS.items():
        if v["path"].replace("\\", "/").rstrip("/").lower() == p:
            return k
    return ""


# ---------- 脱敏 ----------
def mask_path(p: str) -> str:
    """只保留末段文件名/目录名，绝不返回明文绝对路径。"""
    s = str(p or "").replace("\\", "/")
    if not s:
        return ""
    tail = s.rstrip("/").split("/")[-1]
    return f"…/{tail}" if tail else "…"


def mask_executor(executor: dict) -> dict:
    """执行机对外视图：解释器/工作目录一律脱敏，只给受控 key。"""
    d = dict(executor)
    resolved = resolve_executor_python(executor)
    d["python_masked"] = mask_path(resolved)
    d["python_available"] = bool(resolved)
    d["python_executable"] = ""          # 绝不外泄明文路径
    d["cwd_masked"] = mask_path(resolve_workdir(executor.get("cwd_root") or "")
                                or executor.get("cwd") or "")
    d["cwd"] = ""
    d["allowed_runners"] = _json_list(executor.get("allowed_runners")) or list(ENGINES)
    return d


def mask_environment(env: dict) -> dict:
    """环境对外视图：只暴露 secret 引用名与可用性，绝不返回值。"""
    d = dict(env)
    st = secret_status(env.get("secret_ref") or "", env.get("secret_kind") or "")
    d["secret_ref"] = env.get("secret_ref") or ""     # 引用名本身可展示（非密钥值）
    d["secret_available"] = st["available"]
    d["secret_detail"] = st["detail"]
    return d


_ASSET_REF_KEYS = {
    "dataset_ref": "dataset", "tpl_ref": "tpl_dir", "scenes_ref": "scenes_dir",
    "ground_truth_ref": "ground_truth", "airtest_ref": "airtest_dir",
    "scenario_ref": "locust_scenario",
}


def mask_asset_source(src: dict) -> dict:
    """资产源对外视图：只给受控 key 与脱敏路径尾，绝不外泄素材绝对路径。"""
    d = dict(src)
    for k, kind in _ASSET_REF_KEYS.items():
        if kind == "dataset":
            ds = resolve_dataset(src.get(k) or "")
            d["dataset_masked"] = "、".join(mask_path(v) for v in ds.values() if v) or ""
        else:
            d[f"{k}_masked"] = mask_path(resolve_asset_ref(kind, src.get(k) or ""))
    return d


# ---------- 密钥：只校验可用性，绝不返回值 ----------
def secret_status(secret_ref: str, secret_kind: str) -> dict:
    ref = (secret_ref or "").strip()
    if not ref:
        return {"configured": False, "available": False, "detail": "未配置 secret 引用"}
    if secret_kind == "env_var":
        ok = bool(os.environ.get(ref))
        return {"configured": True, "available": ok,
                "detail": f"环境变量 {ref} " + ("已设置（可用）" if ok else "未设置（不可用）")}
    if secret_kind == "external_ref":
        return {"configured": True, "available": None,
                "detail": f"外部 secret 引用 {ref} 已登记，由部署侧解析（平台不持有其值）"}
    return {"configured": True, "available": False, "detail": "未知 secret 类型"}


# ---------- 指纹（非敏感） ----------
def environment_fingerprint(env: dict) -> str:
    return _h("env", env.get("name"), env.get("kind"), env.get("protocol"),
              env.get("host"), env.get("port"))


def executor_fingerprint(executor: dict) -> str:
    return _h("exec", executor.get("name"), executor.get("python_ref"),
              executor.get("cwd_root"), _json_list(executor.get("allowed_runners")))


def asset_fingerprint(kind: str, src: dict) -> str:
    """资产指纹：仅对受控 key 组合取哈希（不含绝对路径）。"""
    if kind == "locust":
        return _h("asset", "locust", src.get("scenario_ref"))
    return _h("asset", "matcheval", src.get("dataset_ref"), src.get("tpl_ref"),
              src.get("scenes_ref"), src.get("ground_truth_ref"), src.get("airtest_ref"))


def path_basename_only(p: str) -> str:
    return os.path.basename(str(p or "").replace("\\", "/").rstrip("/"))


def dir_fingerprint(path: str, limit: int = 4000) -> str:
    """目录级非敏感指纹：只对（文件名, 大小）排序后哈希，不含绝对路径、不读内容。"""
    if not path or not os.path.isdir(path):
        return ""
    entries = []
    try:
        for name in sorted(os.listdir(path))[:limit]:
            full = os.path.join(path, name)
            try:
                entries.append(f"{name}:{os.path.getsize(full) if os.path.isfile(full) else 'd'}")
            except OSError:
                entries.append(f"{name}:?")
    except OSError:
        return ""
    return _h(path_basename_only(path), *entries)


# ==================== v0.2 TestCase 指纹 ====================
# 两个概念严格分离（v0.2 架构要点）：
#   ① 定义指纹 test_case_fingerprint()  → "这个测试定义是什么"        → 存 test_cases.fingerprint
#   ② 内容指纹 build_content_fingerprint() → "本次执行面对的脚本/素材内容是什么" → 存 Job.config_snapshot
# 二者绝不互相代替，也不拼在一起。

# 单文件参与内容哈希的上限；超过则降级为 (name, size) 并标记 partial。
MAX_CONTENT_FILE_BYTES = 8 * 1024 * 1024
# 单目录参与内容哈希的条目上限（沿用 dir_fingerprint 的既有口径）。
CONTENT_DIR_LIMIT = 4000
# 进程内缓存：(kind, key, 廉价签名) -> 结果。避免同一 PlanRun 内重复读盘。
_CONTENT_FP_CACHE: dict[str, dict] = {}


def test_case_fingerprint(name: str, engine: str, params, requires_exclusive=False,
                          asset_kind: str = "") -> str:
    """TestCase **定义指纹**：表示"这个测试定义本身是什么"。

    参与：name / engine / params（规范序 JSON）/ requires_exclusive / asset_kind
    不参与：id / description / tags / enabled / owner / revision / created_at / updated_at
      —— 修改描述、标签、启用状态、负责人或 revision 本身，不应改变"定义内容指纹"。

    复用项目统一的 _h()（sha256 截断 12 位），不另造哈希规则。
    注意：**不把 revision 拼进指纹** —— 二者职责不同（revision 是版本号，指纹是内容标识）。
    """
    return _h("case", name, engine,
              json.dumps(params or {}, sort_keys=True, ensure_ascii=False),
              int(bool(requires_exclusive)), asset_kind or "")


def suite_fingerprint(name: str, cases: list) -> str:
    """TestSuite **定义指纹**：表示"这个**有序**用例集合是什么"。

    参与：name / cases（有序的 {id, revision, fingerprint} 列表）。
      ⚠️ **顺序参与指纹** —— 故对整个 list 直接 json.dumps（sort_keys 只规范元素内键序、
         不重排列表本身）。顺序变化会改变执行语义，因此必须改变指纹。
    不参与：id / description / tags / enabled / owner / revision / created_at / updated_at
      —— 与 test_case_fingerprint 完全一致的理由：改描述、标签、启停、负责人或 revision
         本身，不应改变"集合内容指纹"。

    契约：cases **必须**是 `db.normalize_suite_cases()` 的结果（cases 列的形状由存储层定义，
    规范化只有那一份实现）。本函数刻意不再做一次"投影"，以免出现两份等价实现而悄悄漂移；
    唯一生产调用方（router）在提交前先规范化。复用项目统一的 _h()，不另造哈希规则。
    """
    return _h("suite", name or "",
              json.dumps(list(cases or []), sort_keys=True, ensure_ascii=False))


def file_content_fingerprint(path: str) -> dict:
    """单文件内容指纹（读内容，不落绝对路径）。返回 {exists, fp, bytes, partial}。"""
    if not path or not os.path.isfile(path):
        return {"exists": False, "fp": "", "bytes": 0, "partial": False}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(MAX_CONTENT_FILE_BYTES)
        return {"exists": True,
                "fp": _h("file", path_basename_only(path), size,
                         hashlib.sha256(data).hexdigest()),
                "bytes": len(data), "partial": size > MAX_CONTENT_FILE_BYTES}
    except OSError:
        return {"exists": False, "fp": "", "bytes": 0, "partial": False}


def dir_content_fingerprint(path: str) -> dict:
    """目录**内容**指纹：读文件内容，而非仅 name+size。

    v0.1 的 dir_fingerprint() 只哈希 (name, size)，"文件内容变了但大小没变"检测不到 —— v0.2 修正该 P0 缺陷。
    返回 {exists, fp, files, partial}；不落绝对路径，超限文件降级并标 partial。
    """
    if not path or not os.path.isdir(path):
        return {"exists": False, "fp": "", "files": 0, "partial": False}
    entries, partial = [], False
    try:
        names = sorted(os.listdir(path))[:CONTENT_DIR_LIMIT]
        if len(os.listdir(path)) > CONTENT_DIR_LIMIT:
            partial = True
        for name in names:
            full = os.path.join(path, name)
            try:
                if os.path.isfile(full):
                    size = os.path.getsize(full)
                    if size > MAX_CONTENT_FILE_BYTES:
                        entries.append(f"{name}:{size}:big"); partial = True
                    else:
                        with open(full, "rb") as f:
                            entries.append(f"{name}:{size}:{hashlib.sha256(f.read()).hexdigest()[:16]}")
                else:
                    entries.append(f"{name}:d")
            except OSError:
                entries.append(f"{name}:?"); partial = True
    except OSError:
        return {"exists": False, "fp": "", "files": 0, "partial": False}
    return {"exists": True, "fp": _h(path_basename_only(path), *entries),
            "files": len(entries), "partial": partial}


def _dir_signature(path: str) -> str:
    """目录的廉价签名（不读内容），仅用于缓存失效判断。"""
    try:
        names = os.listdir(path)
        total = 0
        for n in names[:CONTENT_DIR_LIMIT]:
            p = os.path.join(path, n)
            try:
                total += os.path.getsize(p) if os.path.isfile(p) else -1
            except OSError:
                pass
        return f"{len(names)}:{total}"
    except OSError:
        return "?"


def _cached_content(key: str, builder) -> dict:
    hit = _CONTENT_FP_CACHE.get(key)
    if hit is not None:
        return hit
    val = builder()
    _CONTENT_FP_CACHE[key] = val
    return val


def build_content_fingerprint(engine: str, params: dict) -> dict:
    """本次执行实际面对的"受控脚本 / 素材内容"指纹（**不是** TestCase 定义的一部分）。

    v0.2 冻结范围：
      纳入：locust    -> LOCUST_SCENARIOS[scenario] 脚本文件内容
            matcheval -> DATASET_REFS[dataset] 派生出的 tpl_dir / scenes_dir 目录内容
      不纳入：整个项目目录 / WORKDIR_ROOTS（工作目录下既有被测源码又有测试文件，边界不可界定）
              / 解释器环境 / 被测系统全部源码
              / pytest（同上原因，v0.2 明确不做）

    只在 PlanRun 创建阶段调用；结果按（受控 key + 廉价签名）做进程内缓存，同一 run 内不重复读盘。
    """
    p = params or {}
    out: dict = {"engine": engine, "sources": {}, "fingerprint": ""}
    parts: list = ["content", engine]
    try:
        if engine == "locust":
            key = str(p.get("locustfile") or "")
            if key:
                path = LOCUST_SCENARIOS.get(key, {}).get("path", "")
                if path and os.path.isfile(path):
                    sig = f"{os.path.getsize(path)}:{os.path.getmtime(path)}"
                    info = _cached_content(f"file:{path}:{sig}",
                                           lambda: file_content_fingerprint(path))
                else:
                    info = {"exists": False, "fp": "", "bytes": 0, "partial": False}
                out["sources"]["locust_script"] = {"key": key, **info}
                parts.append(info.get("fp") or "")
        elif engine == "matcheval":
            key = str(p.get("dataset") or "")
            ds = resolve_dataset(key) if key else {}
            for label, fld in (("tpl_dir", "tpl_dir"), ("scenes_dir", "scenes_dir")):
                path = ds.get(fld) or ""
                if path and os.path.isdir(path):
                    sig = _dir_signature(path)
                    info = _cached_content(f"dir:{path}:{sig}",
                                           lambda path=path: dir_content_fingerprint(path))
                else:
                    info = {"exists": False, "fp": "", "files": 0, "partial": False}
                out["sources"][label] = {"key": key, **info}
                parts.append(info.get("fp") or "")
    except Exception as e:  # noqa: BLE001
        # 内容指纹失败绝不影响执行：如实标记，不伪造
        out["error"] = f"{type(e).__name__}: {e}"
    out["fingerprint"] = _h(*parts)
    return out


# ---------- 输入校验（只接受 ID/key；拒绝任意路径/命令） ----------
_PATH_CMD_RE = re.compile(r"[\\/;|&$`<>]|^[A-Za-z]:|\.exe\b", re.I)
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,62}[A-Za-z0-9])?$")
_ENVVAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,64}$")
_SECRETREF_RE = re.compile(r"^[A-Za-z0-9_.:/@\-]{1,120}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


def _reject_pathlike(field: str, value: str, errors: list[str]) -> None:
    if value and _PATH_CMD_RE.search(str(value)):
        errors.append(f"{field} 不接受任意路径/命令片段（只允许受控 ID/key）：{value!r}")


def validate_environment_input(data: dict, partial: bool = False) -> list[str]:
    """校验 Environment 输入。partial=True 时（编辑）只校验出现的字段。"""
    errors: list[str] = []
    def has(k): return (k in data) or not partial

    if has("name") and not str(data.get("name") or "").strip():
        errors.append("name 必填")
    if has("kind"):
        if str(data.get("kind") or "") not in ENV_KINDS:
            errors.append(f"kind 必须是 {list(ENV_KINDS)} 之一")
    if has("protocol"):
        if str(data.get("protocol") or "http") not in ENV_PROTOCOLS:
            errors.append(f"protocol 必须是 {list(ENV_PROTOCOLS)} 之一")
    kind = str(data.get("kind") or "http")
    if kind != "offline" and has("host"):
        host = str(data.get("host") or "").strip()
        if not host:
            errors.append("非 offline 环境 host 必填")
        else:
            _reject_pathlike("host", host, errors)
            if not errors and not _HOST_RE.match(host):
                errors.append(f"host 格式非法（仅允许主机名/IP）：{host!r}")
    if has("port"):
        try:
            port = int(data.get("port") or 0)
            if kind != "offline" and not (0 < port <= 65535):
                errors.append("port 必须在 1..65535")
        except (TypeError, ValueError):
            errors.append("port 必须是整数")
    sk = str(data.get("secret_kind") or "")
    if sk and sk not in SECRET_KINDS:
        errors.append(f"secret_kind 必须是 {list(SECRET_KINDS)} 之一")
    ref = str(data.get("secret_ref") or "").strip()
    if ref:
        if sk == "env_var" and not _ENVVAR_RE.match(ref):
            errors.append("secret_ref(env_var) 必须是合法环境变量名")
        elif sk != "env_var" and not _SECRETREF_RE.match(ref):
            errors.append("secret_ref 必须是合法引用名（平台不保存明文密钥）")
    labels = data.get("labels")
    if labels is not None:
        if not isinstance(labels, list):
            errors.append("labels 必须是字符串数组")
        else:
            for lb in labels:
                if not _LABEL_RE.match(str(lb)):
                    errors.append(f"标签仅允许短标识（字母数字_.-）：{lb!r}")
    return errors


def validate_executor_input(data: dict, partial: bool = False) -> list[str]:
    errors: list[str] = []
    def has(k): return (k in data) or not partial

    if has("name") and not str(data.get("name") or "").strip():
        errors.append("name 必填")
    if has("python_ref"):
        # 允许留空（未指定）；一旦填写，必须是受控引用 key，绝不接受任意可执行文件路径
        ref = str(data.get("python_ref") or "")
        if ref and ref not in INTERPRETER_PROFILES:
            errors.append(f"python_ref 必须是受控解释器引用 {list(INTERPRETER_PROFILES)} 之一")
    if has("cwd_root"):
        root = str(data.get("cwd_root") or "")
        if root and root not in WORKDIR_ROOTS:
            errors.append(f"cwd_root 必须是受控工作目录根 {list(WORKDIR_ROOTS)} 之一")
    if "allowed_runners" in data and data.get("allowed_runners") is not None:
        ar = data.get("allowed_runners")
        if not isinstance(ar, list) or any(a not in ENGINES for a in ar):
            errors.append(f"allowed_runners 必须是 {list(ENGINES)} 的子集")
    if has("max_concurrency"):
        try:
            if int(data.get("max_concurrency") or 1) < 1:
                errors.append("max_concurrency 必须 >= 1")
        except (TypeError, ValueError):
            errors.append("max_concurrency 必须是整数")
    return errors


def validate_asset_input(data: dict, partial: bool = False) -> list[str]:
    errors: list[str] = []
    def has(k): return (k in data) or not partial

    if has("name") and not str(data.get("name") or "").strip():
        errors.append("name 必填")
    kind = str(data.get("kind") or "")
    if has("kind") and kind not in ASSET_KINDS:
        errors.append(f"kind 必须是 {list(ASSET_KINDS)} 之一")
    if kind == "locust" or (not kind and not partial):
        if has("scenario_ref"):
            ref = str(data.get("scenario_ref") or "")
            if ref not in LOCUST_SCENARIOS:
                errors.append(f"scenario_ref 必须是受控场景引用 {list(LOCUST_SCENARIOS)} 之一")
    if kind == "matcheval" or (not kind and not partial):
        ds = str(data.get("dataset_ref") or "")
        if ds and ds not in DATASET_REFS:
            errors.append(f"dataset_ref 必须是受控 dataset 引用 {list(DATASET_REFS)} 之一")
        if not ds and has("dataset_ref"):
            # 允许回退到显式目录 key，但二者都需受控
            tpl = str(data.get("tpl_ref") or "")
            sc = str(data.get("scenes_ref") or "")
            if tpl and tpl not in TPL_DIRS:
                errors.append(f"tpl_ref 必须是受控模板目录 {list(TPL_DIRS)} 之一")
            if sc and sc not in SCENES_DIRS:
                errors.append(f"scenes_ref 必须是受控场景目录 {list(SCENES_DIRS)} 之一")
        gt = str(data.get("ground_truth_ref") or "")
        if gt and gt not in GROUND_TRUTH_REFS:
            errors.append(f"ground_truth_ref 必须是受控真值引用 {list(GROUND_TRUTH_REFS)} 之一")
    air = str(data.get("airtest_ref") or "")
    if air and air not in AIRTEXT_DIRS:
        errors.append(f"airtest_ref 必须是受控 Airtest 目录 {list(AIRTEXT_DIRS)} 之一")
    return errors


# ---------- 预检（低风险；绝不发起压测或完整测试） ----------
def _check(name: str, ok: Optional[bool], detail: str = "") -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _result(entity: str, ident: dict, checks: list[dict],
            missing: list[str], next_steps: list[str]) -> dict:
    real = [c for c in checks if c["ok"] is not None]
    return {
        "entity": entity, **ident,
        "ok": all(c["ok"] for c in real) if real else None,
        "checks": checks, "missing": missing, "next_steps": next_steps,
    }


def precheck_environment(env: dict, timeout: float = 2.0) -> dict:
    """按受控协议做低风险连通性检查：仅 TCP 连接探测，不发送任何业务报文。"""
    kind = env.get("kind") or "http"
    host = str(env.get("host") or "")
    port = int(env.get("port") or 0)
    ident = {"id": env.get("id"), "name": env.get("name")}
    if kind == "offline":
        # offline 环境无运行时依赖可探测：预检为"不适用"（ok=None），绝不做任何连接尝试
        return {"entity": "environment", **ident, "ok": None,
                "checks": [_check("连通性", None, "offline 环境：离线评估，无需连通性检查")],
                "missing": [], "next_steps": []}

    checks = [_check("类型受控", kind in ENV_KINDS, f"kind={kind}")]
    checks.append(_check("地址已配置", bool(host and port),
                         f"{host}:{port}" if host and port else "缺 host/port"))
    if host and port:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                checks.append(_check("TCP 连通性", True, f"{host}:{port} 可连接（仅探测，未发业务报文）"))
        except Exception as e:  # noqa: BLE001
            checks.append(_check("TCP 连通性", False, f"{host}:{port} 不可达：{type(e).__name__}"))
    st = secret_status(env.get("secret_ref") or "", env.get("secret_kind") or "")
    if st["configured"]:
        checks.append(_check("secret 可用性", st["available"], st["detail"]))

    missing = [c["name"] for c in checks if c["ok"] is False]
    steps = []
    if "TCP 连通性" in missing:
        steps.append("确认目标服务已启动、地址/端口正确；真实环境须由用户录入后再启用。")
    if "secret 可用性" in missing:
        steps.append("设置对应环境变量或配置外部 secret 引用（平台不保存明文密钥）。")
    return _result("environment", ident, checks, missing, steps)


def _run_py(py: str, args: list[str], timeout: float = 6.0) -> tuple[int, str]:
    try:
        p = subprocess.run([py, *args], capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}"


def precheck_executor(executor: dict, timeout: float = 6.0) -> dict:
    """校验解释器存在、版本可读、所选 Runner 的关键依赖可用。"""
    ident = {"id": executor.get("id"), "name": executor.get("name")}
    py = resolve_executor_python(executor)
    ref = executor.get("python_ref") or ""
    checks = [_check("解释器引用受控", bool(ref) or bool(py), f"ref={ref or '(legacy 路径)'}")]
    if not py:
        checks.append(_check("解释器存在", False, "未解析到可用解释器（引用不可用或文件不存在）"))
        return _result("executor", ident, checks, ["解释器存在"],
                       ["在 Executor 中选择一个可用的受控解释器引用（如 anaconda / locust-venv / matcheval）。"])
    checks.append(_check("解释器存在", os.path.exists(py), f"{mask_path(py)} 存在"))
    rc, ver = _run_py(py, ["--version"], timeout)
    checks.append(_check("版本可读", rc == 0, ver or "无法读取版本"))

    runners = _json_list(executor.get("allowed_runners")) or list(ENGINES)
    for eng in runners:
        mods = _ENGINE_DEPS.get(eng) or []
        if not mods:
            continue
        rc2, out = _run_py(py, ["-c", f"import {', '.join(mods)}"], timeout)
        checks.append(_check(f"{eng} 依赖可用", rc2 == 0,
                             "、".join(mods) + (" 可导入" if rc2 == 0 else f" 不可用：{out[:120]}")))

    missing = [c["name"] for c in checks if c["ok"] is False]
    steps = []
    if "版本可读" in missing:
        steps.append("解释器无法执行，请检查该引用指向的 Python 是否可用。")
    if any("依赖可用" in m for m in missing):
        steps.append("为对应 Runner 安装缺失依赖（如 locust / opencv-python + numpy），或改用已装好依赖的解释器引用。")
    return _result("executor", ident, checks, missing, steps)


def precheck_asset_source(src: dict) -> dict:
    """校验引用目录/文件存在、基础结构完整，并生成非敏感指纹。"""
    ident = {"id": src.get("id"), "name": src.get("name")}
    kind = src.get("kind") or "matcheval"
    checks = [_check("类型受控", kind in ASSET_KINDS, f"kind={kind}")]
    if kind == "locust":
        ref = src.get("scenario_ref") or ""
        p = resolve_asset_ref("locust_scenario", ref)
        checks.append(_check("场景引用受控", ref in LOCUST_SCENARIOS, f"scenario_ref={ref or '(空)'}"))
        checks.append(_check("场景文件存在", bool(p) and os.path.isfile(p),
                             f"{mask_path(p)} " + ("存在" if p and os.path.isfile(p) else "缺失")))
        missing = [c["name"] for c in checks if c["ok"] is False]
        steps = ["选择受控 Locust 场景引用；真实场景文件须由用户录入后再启用。"] if missing else []
        out = _result("asset_source", ident, checks, missing, steps)
        out["fingerprint"] = asset_fingerprint("locust", src)
        return out

    ds_ref = src.get("dataset_ref") or ""
    if ds_ref:
        ds = resolve_dataset(ds_ref)
        tpl, scenes = ds.get("tpl_dir", ""), ds.get("scenes_dir", "")
        checks.append(_check("dataset 引用受控", ds_ref in DATASET_REFS, f"dataset_ref={ds_ref}"))
        checks.append(_check("真值约定可用", bool(ds.get("ground_truth")),
                             f"ground_truth={ds.get('ground_truth') or '(缺)'}"))
    else:
        tpl_ref = src.get("tpl_ref") or ""
        sc_ref = src.get("scenes_ref") or ""
        tpl = resolve_asset_ref("tpl_dir", tpl_ref)
        scenes = resolve_asset_ref("scenes_dir", sc_ref)
        checks.append(_check("模板引用受控", tpl_ref in TPL_DIRS, f"tpl_ref={tpl_ref or '(空)'}"))
        checks.append(_check("场景引用受控", sc_ref in SCENES_DIRS, f"scenes_ref={sc_ref or '(空)'}"))

    checks.append(_check("模板目录存在", bool(tpl) and os.path.isdir(tpl),
                         f"{mask_path(tpl)} " + ("存在" if tpl and os.path.isdir(tpl) else "缺失")))
    checks.append(_check("场景目录存在", bool(scenes) and os.path.isdir(scenes),
                         f"{mask_path(scenes)} " + ("存在" if scenes and os.path.isdir(scenes) else "缺失")))
    tpl_fp = dir_fingerprint(tpl)
    sc_fp = dir_fingerprint(scenes)
    checks.append(_check("基础结构完整", bool(tpl_fp) and bool(sc_fp),
                         f"模板指纹={tpl_fp or '-'} 场景指纹={sc_fp or '-'}（非敏感）"))
    air = src.get("airtest_ref") or ""
    if air:
        ap = resolve_asset_ref("airtest_dir", air)
        checks.append(_check("Airtest 目录存在", bool(ap) and os.path.isdir(ap),
                             f"{mask_path(ap)} " + ("存在" if ap and os.path.isdir(ap) else "缺失（需用户录入）")))

    missing = [c["name"] for c in checks if c["ok"] is False]
    steps = []
    if any("引用受控" in m for m in missing):
        steps.append("改为选择受控的 dataset/模板/场景/真值引用 key（不接受任意素材目录）。")
    if any("目录存在" in m or "结构完整" in m for m in missing):
        steps.append("确认资产目录已就位且非空；真实识别资产须由用户录入后再启用。")
    out = _result("asset_source", ident, checks, missing, steps)
    out["fingerprint"] = asset_fingerprint("matcheval", src)
    out["dir_fingerprint"] = _h(tpl_fp, sc_fp)
    return out


# ---------- 引擎就绪度：可运行 / 缺少什么配置（不实际运行） ----------
def engine_readiness(db, db_path: str) -> list[dict]:
    """对三引擎分别给出"可运行/缺少什么配置"，供前端明确下一步；不发起任何执行。"""
    executors = [e for e in db.list_executors(db_path) if int(e.get("enabled", 1))]
    assets = [a for a in db.list_asset_sources(db_path) if int(a.get("enabled", 1))]
    out = []
    for eng in ENGINES:
        missing, steps, usable = [], [], []
        for ex in executors:
            allowed = _json_list(ex.get("allowed_runners")) or list(ENGINES)
            if eng not in allowed:
                continue
            py = resolve_executor_python(ex)
            if not py:
                continue
            mods = _ENGINE_DEPS.get(eng) or []
            rc, _out = _run_py(py, ["-c", f"import {', '.join(mods)}"], 6.0) if mods else (0, "")
            if rc == 0:
                usable.append(ex["name"])
        if not usable:
            missing.append("可用执行机")
            steps.append(f"在 Executor 中启用一台允许 {eng} 且依赖齐备的执行机（解释器引用需可用）。")
        if eng == "locust":
            if not any(a.get("kind") == "locust" for a in assets):
                missing.append("Locust 场景资产源")
                steps.append("新增并启用一个 kind=locust 的 AssetSource，选择受控场景引用。")
        if eng == "matcheval":
            ok_assets = [a for a in assets if a.get("kind") == "matcheval"
                         and precheck_asset_source(a)["ok"]]
            if not ok_assets:
                missing.append("MatchEval 资产源")
                steps.append("新增并启用一个 kind=matcheval 的 AssetSource，选择受控 dataset/模板/场景引用。")
        out.append({
            "engine": eng, "runnable": not missing,
            "usable_executors": usable, "missing": missing, "next_steps": steps,
        })
    return out


# ---------- Job 配置快照（脱敏、不可变；仅受控 key + 指纹） ----------
def build_config_snapshot(env: dict, executor: dict, asset_source: Optional[dict] = None) -> dict:
    """构建 Job 配置快照：只含受控 key 与脱敏指纹，绝不落明文路径/密钥。
    快照在 Job 创建时固化，后续编辑配置实体不影响历史 Job / report.json / 比较结论。"""
    env = env or {}
    executor = executor or {}
    snap: dict[str, Any] = {
        "environment": {
            "id": env.get("id"), "name": env.get("name"),
            "kind": env.get("kind") or "http",
            "protocol": env.get("protocol") or "http",
            "host": env.get("host"), "port": env.get("port"),
            "target": f"{env.get('host', '-')}:{env.get('port', '-')}",
            "secret_ref": env.get("secret_ref") or "",
            "secret_kind": env.get("secret_kind") or "",
            "fingerprint": environment_fingerprint(env),
        },
        "executor": {
            "id": executor.get("id"), "name": executor.get("name"),
            "python_ref": executor.get("python_ref") or "",
            "python_masked": mask_path(resolve_executor_python(executor)),
            "cwd_root": executor.get("cwd_root") or "",
            "cwd_masked": mask_path(resolve_workdir(executor.get("cwd_root") or "")
                                    or executor.get("cwd") or ""),
            "allowed_runners": _json_list(executor.get("allowed_runners")) or list(ENGINES),
            "max_concurrency": executor.get("max_concurrency") or 1,
            "fingerprint": executor_fingerprint(executor),
        },
    }
    if asset_source:
        snap["asset_source"] = {
            "id": asset_source.get("id"), "name": asset_source.get("name"),
            "kind": asset_source.get("kind"),
            "refs": {k: asset_source.get(k) or "" for k in _ASSET_REF_KEYS},
            "fingerprint": asset_fingerprint(asset_source.get("kind") or "", asset_source),
        }
    else:
        snap["asset_source"] = None
    snap["snapshot_fingerprint"] = _h(
        snap["environment"]["fingerprint"], snap["executor"]["fingerprint"],
        (snap["asset_source"] or {}).get("fingerprint"))
    return snap


def snapshot_env_view(snapshot) -> Optional[dict]:
    """从快照还原"用于比较元数据的"环境视图，保证历史报告不因配置编辑而漂移。"""
    if not snapshot:
        return None
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:  # noqa: BLE001
            return None
    e = (snapshot or {}).get("environment") or {}
    if not e.get("name"):
        return None
    return {"id": e.get("id"), "name": e.get("name"), "host": e.get("host"),
            "port": e.get("port"), "kind": e.get("kind"), "enabled": 1}


def snapshot_asset_key(snapshot, engine: str) -> str:
    """从快照取与引擎匹配的受控资产 key（matcheval: dataset_ref；locust: scenario_ref）。"""
    if not snapshot:
        return ""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:  # noqa: BLE001
            return ""
    a = (snapshot or {}).get("asset_source") or {}
    refs = a.get("refs") or {}
    if engine == "matcheval":
        return refs.get("dataset_ref") or ""
    if engine == "locust":
        return refs.get("scenario_ref") or ""
    return ""


# ---------- Plan 快照（执行时固化、不可变、脱敏；只含受控 ID/key 与步骤参数） ----------
def build_plan_snapshot(plan: dict) -> dict:
    """把"执行时的计划定义"固化为不可变快照：名称/说明/版本/引用 ID/步骤/fail_fast。

    快照只含受控 ID/key 与白名单步骤参数，绝不含明文密钥、绝对路径或命令；
    与 Job 的 config_snapshot（环境/执行机/资产的脱敏指纹）职责互补、互不重复泄露敏感信息。
    后续编辑 Plan 不会改变已创建 PlanRun 的本快照。"""
    steps = []
    for i, s in enumerate(plan.get("steps") or []):
        item = {
            "index": i,
            "engine": s.get("engine"),
            "name": s.get("name") or s.get("engine"),
            "requires_exclusive": bool(s.get("requires_exclusive", False)),
            "params": dict(s.get("params") or {}),
        }
        # v0.2：仅当步骤引用了 TestCase 时才附加溯源键。
        # legacy 内联步骤**不加任何键** → 其 steps JSON 与 v0.1 逐字节一致 → 旧计划快照指纹不变。
        if s.get("case_ref"):
            item["case_ref"] = dict(s["case_ref"])
            item["override_params"] = dict(s.get("override_params") or {})
        steps.append(item)
    snap = {
        "plan_id": plan.get("id"),
        "name": plan.get("name"),
        "description": plan.get("description") or "",
        "revision": int(plan.get("revision") or 1),
        "fail_fast": bool(plan.get("fail_fast", 1)),
        "owner": plan.get("owner") or "local",
        "environment_id": plan.get("environment_id"),
        "executor_id": plan.get("executor_id"),
        "asset_source_id": plan.get("asset_source_id"),
        "steps": steps,
    }
    snap["snapshot_fingerprint"] = _h(
        "plan", snap["plan_id"], snap["revision"],
        json.dumps(steps, ensure_ascii=False, sort_keys=True),
        snap["environment_id"], snap["executor_id"], snap["asset_source_id"],
        int(snap["fail_fast"]))
    return snap


def plan_snapshot_name(snapshot) -> str:
    """从 PlanRun 的计划快照取计划名，保证历史报告不因后续改名而漂移。"""
    if not snapshot:
        return ""
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except Exception:  # noqa: BLE001
            return ""
    return (snapshot or {}).get("name") or ""


# ---------- Plan 步骤参数白名单（只接受受控 key；拒绝任意命令/路径/解释器） ----------
_STEP_PARAM_KEYS: dict[str, set] = {
    "pytest": {"args", "cwd", "cwd_root", "timeout_sec", "asset_source_id"},
    "locust": {"locustfile", "users", "spawn_rate", "run_time", "csv_full_history",
               "timeout_sec", "asset_source_id"},
    "matcheval": {"dataset", "algorithm", "threshold", "output_level",
                  "timeout_sec", "asset_source_id"},
}
_ABS_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/)")
# pytest 参数只允许相对测试选择器/开关；拒绝 shell 元字符、反斜杠、驱动盘符与可执行文件。
# 注意：正斜杠 '/' 是相对选择器的正常组成（如 demo/test_x.py），故不在此拒绝。
_ARG_META_RE = re.compile(r"[;|&$`<>\\]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_EXE_RE = re.compile(r"\.exe\b", re.I)


def validate_step_params(engine: str, params: dict) -> list[str]:
    """校验 Plan 步骤参数：仅白名单键；拒绝任意命令、绝对路径、解释器与素材目录。"""
    errors: list[str] = []
    if engine not in ENGINES:
        return [f"未知引擎：{engine}"]
    allowed = _STEP_PARAM_KEYS.get(engine, set())
    for k, v in (params or {}).items():
        if k not in allowed:
            errors.append(f"[{engine}] 不接受的参数键：{k!r}（只允许 {sorted(allowed)}）")
            continue
        if k == "args":
            if not isinstance(v, list):
                errors.append("[pytest] args 必须是字符串数组")
                continue
            for a in v:
                s = str(a)
                if (not s.strip() or _ABS_PATH_RE.match(s) or _DRIVE_RE.match(s)
                        or ".." in s or _ARG_META_RE.search(s) or _EXE_RE.search(s)):
                    errors.append(f"[pytest] args 仅允许相对测试选择器/开关：{s!r}")
        elif k == "threshold":
            try:
                t = float(v)
                if not (0.0 <= t <= 1.0):
                    errors.append("threshold 必须在 0..1 之间")
            except (TypeError, ValueError):
                errors.append("threshold 必须是数字")
        elif k in ("cwd", "cwd_root"):
            s = str(v or "")
            if s and s not in WORKDIR_ROOTS and not infer_workdir_root(s):
                errors.append(f"cwd/cwd_root 必须是受控工作目录根 {list(WORKDIR_ROOTS)} 之一")
        elif k == "locustfile":
            if str(v) not in LOCUST_SCENARIOS:
                errors.append(f"locustfile 必须是受控场景引用 {list(LOCUST_SCENARIOS)} 之一")
        elif k == "dataset":
            if str(v) not in DATASET_REFS:
                errors.append(f"dataset 必须是受控 dataset 引用 {list(DATASET_REFS)} 之一")
        elif k == "algorithm":
            algs = v if isinstance(v, list) else str(v).split(",")
            if any(str(a).strip() not in ("tpl", "mstpl", "kaze", "brisk", "akaze", "orb")
                   for a in algs):
                errors.append("algorithm 含非白名单算法")
        elif k == "output_level":
            if str(v) not in ("full", "summary"):
                errors.append("output_level 只能是 full / summary")
        elif k in ("users", "spawn_rate", "timeout_sec", "asset_source_id"):
            try:
                int(v)
            except (TypeError, ValueError):
                errors.append(f"{k} 必须是整数")
    return errors


# ---------- Plan 引用校验（仅启用的配置可被引用；只认 ID） ----------
def validate_plan_refs(db, db_path: str, environment_id, executor_id,
                       asset_source_id=None) -> list[str]:
    """校验 Plan 引用的配置存在且启用。返回错误列表（空=通过）。"""
    errors: list[str] = []
    env = db.get_environment(db_path, int(environment_id)) if environment_id else None
    if not env:
        errors.append(f"引用的 Environment 不存在：{environment_id}")
    elif not int(env.get("enabled", 1)):
        errors.append(f"引用的 Environment 已停用：{env.get('name')}")
    ex = db.get_executor(db_path, int(executor_id)) if executor_id else None
    if not ex:
        errors.append(f"引用的 Executor 不存在：{executor_id}")
    elif not int(ex.get("enabled", 1)):
        errors.append(f"引用的 Executor 已停用：{ex.get('name')}")
    if asset_source_id:
        a = db.get_asset_source(db_path, int(asset_source_id))
        if not a:
            errors.append(f"引用的 AssetSource 不存在：{asset_source_id}")
        elif not int(a.get("enabled", 1)):
            errors.append(f"引用的 AssetSource 已停用：{a.get('name')}")
    return errors


# ---------- Plan 预检 / 执行预览（复用执行校验；不产生 PlanRun/Job，不跑测试） ----------
def _executor_engine_state(exec_pc: Optional[dict], engine: str) -> tuple[bool, bool]:
    """从执行机预检结果提取 (执行机是否允许该引擎, 依赖是否就绪)。

    预检只对 allowed_runners 中的引擎产出"<engine> 依赖可用"检查项，故：
    检查项存在 => 允许该引擎；其 ok=True => 依赖齐备。"""
    if not exec_pc:
        return False, False
    by_name = {c.get("name"): c for c in exec_pc.get("checks") or []}
    dep = by_name.get(f"{engine} 依赖可用")
    return dep is not None, bool(dep and dep.get("ok") is True)


def precheck_plan(db, db_path: str, plan: dict) -> dict:
    """计划预检 / 执行预览：调用与实际执行相同的后端校验逻辑，返回
    步骤顺序、解析后的引擎、配置就绪度、缺失依赖、锁/并发提示、预期 fail_fast 行为。

    安全：只做低风险探测（引用可用性、白名单、解释器/依赖导入探测、TCP 连通性），
    绝不创建 PlanRun/Job、绝不发起压测或完整测试。"""
    ident = {"id": plan.get("id"), "name": plan.get("name")}
    revision = int(plan.get("revision") or 1)
    steps_in = plan.get("steps") or []

    ref_errors = validate_plan_refs(db, db_path, plan.get("environment_id"),
                                    plan.get("executor_id"), plan.get("asset_source_id"))
    env = db.get_environment(db_path, int(plan["environment_id"])) if plan.get("environment_id") else None
    exec_ = db.get_executor(db_path, int(plan["executor_id"])) if plan.get("executor_id") else None
    asset = db.get_asset_source(db_path, int(plan["asset_source_id"])) if plan.get("asset_source_id") else None

    env_pc = precheck_environment(env) if env else None
    exec_pc = precheck_executor(exec_) if exec_ else None
    asset_pc = precheck_asset_source(asset) if asset else None

    step_reports, missing, next_steps = [], [], []
    for i, s in enumerate(steps_in):
        eng = s.get("engine")
        params = s.get("params") or {}
        registered = eng in ENGINES
        param_errors = validate_step_params(eng, params) if registered else [f"未注册引擎：{eng!r}"]
        allowed, deps_ok = _executor_engine_state(exec_pc, eng) if registered else (False, False)
        step_reports.append({
            "index": i, "engine": eng, "name": s.get("name") or eng,
            "requires_exclusive": bool(s.get("requires_exclusive", False)),
            "params": dict(params),
            "engine_registered": registered,
            "params_ok": not param_errors,
            "params_errors": param_errors,
            "executor_allows": allowed,
            "deps_ready": deps_ok,
            "ready": bool(registered and not param_errors and allowed and deps_ok),
        })
        if not registered:
            missing.append(f"步骤{i} 引擎未注册：{eng!r}")
            next_steps.append("步骤引擎只能选择已注册的 pytest / locust / matcheval。")
        elif param_errors:
            missing.append(f"步骤{i} 参数非法")
            next_steps.append(f"修正步骤{i} 的参数（只允许受控白名单字段，不接受任意命令/路径）。")
        elif not allowed:
            missing.append(f"步骤{i} 执行机不允许 {eng}")
            next_steps.append(f"在 Executor 的 allowed_runners 中加入 {eng}，或改用允许该引擎的执行机。")
        elif not deps_ok:
            missing.append(f"步骤{i} 执行机缺少 {eng} 依赖")
            next_steps.append(f"为执行机解释器安装 {eng} 依赖，或改用依赖齐备的执行机。")

    if not steps_in:
        missing.append("计划没有执行单元")
        next_steps.append("至少添加一个执行步骤。")

    missing += ref_errors
    if env_pc and env_pc.get("ok") is False:
        missing.append("被测环境不可达")
        next_steps += env_pc.get("next_steps") or []
    if exec_pc and exec_pc.get("ok") is False:
        missing.append("执行机不可用")
        next_steps += exec_pc.get("next_steps") or []
    if asset_pc and asset_pc.get("ok") is False:
        missing.append("资产源不完整")
        next_steps += asset_pc.get("next_steps") or []

    exec_id = exec_.get("id") if exec_ else None
    lock = db.get_active_executor_lock(db_path, exec_id) if exec_id else None
    concurrency = {
        "executor_id": exec_id,
        "exclusive_lock_held": bool(lock),
        "holder_job_id": (lock or {}).get("holder_job_id"),
        "run_in_progress": bool(db.any_non_terminal_run(db_path)),
        "note": "平台当前为全局串行执行：计划触发后 Job 依次执行；UI/Unity 独占步骤会等待执行机锁释放。",
    }

    fail_fast = bool(plan.get("fail_fast", 1))
    ff = {
        "enabled": fail_fast,
        "behavior": ("首个步骤失败后，其后尚未执行的步骤将置为 skipped（不覆盖历史报告）"
                     if fail_fast else "任一失败不阻断后续步骤，继续串行执行"),
    }

    seen, missing_u = set(), []
    for m in missing:
        if m not in seen:
            seen.add(m)
            missing_u.append(m)

    steps_ready = bool(step_reports) and all(x["ready"] for x in step_reports)
    config_bad = any((pc or {}).get("ok") is False for pc in (env_pc, exec_pc, asset_pc))
    return {
        "entity": "plan", **ident,
        "revision": revision,
        "ok": bool((not ref_errors) and steps_ready and not config_bad),
        "steps": step_reports,
        "config": {"environment": env_pc, "executor": exec_pc, "asset_source": asset_pc},
        "missing": missing_u,
        "next_steps": next_steps,
        "concurrency": concurrency,
        "fail_fast": ff,
        "fingerprint": build_plan_snapshot(plan)["snapshot_fingerprint"],
        "no_side_effects": True,
    }