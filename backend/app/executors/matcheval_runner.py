"""MatchEval Runner：离线调用真实 match_eval.py 做“算法 × 阈值”识别评估，
收集 CSV / 热力图 / 错误样本图产物，派生 TP/FP/FN/precision/recall/F1，构建版本化 report.json。

安全约束：Job 参数仅允许白名单内的受控项（algorithm / threshold / dataset key / 输出级别 +
environment / executor）。平台不行任何任意脚本路径、素材路径或命令行片段；
不重写 match_eval.py 的算法逻辑，只在 Runner 侧收集产物并派生评估指标。
"""
from __future__ import annotations

import csv
import os
import shutil
import subprocess
from typing import Callable, Optional

from .base import (BaseRunner, RunnerResult, child_env as build_child_env,
                   drain_proc, reap_proc, shell_cmd)

# __file__ 位于 platform/backend/app/executors/，上溯 3 级得 backend，再上溯 1 级得 platform，
# 再上溯 1 级得工作区根（match_eval.py 所在目录）。
_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend
_DEMO = os.path.join(_BACKEND, "demo")
_ROOT = os.path.dirname(_BACKEND)                                        # platform
_WORKSPACE = os.path.dirname(_ROOT)                                      # 测试平台

# 已采集并实测验证的真实 PrivaHigh 游戏素材（与 config_center.PRIVAHIGH_ASSETS 同一目录）。
_PRIVAHIGH_ASSETS = os.path.join(_WORKSPACE, "real_assets", "privahigh")

# 受控脚本：只有这一个可被 Runner 固定的 match_eval.py；不允许 Job 传入脚本路径。
MATCH_EVAL_PY = os.environ.get("PLATFORM_MATCHEVAL_SCRIPT") or os.path.join(_WORKSPACE, "match_eval.py")

# 受白名单保护的 algorithm 集合（与 match_eval.py 的 ALGORITHMS 对齐）
_ALGORITHMS = ("tpl", "mstpl", "kaze", "brisk", "akaze", "orb")
# 评估档位（不动算法，由 --thresholds 传原脚本）
_THRESHOLDS = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95)

# 受白名单保护的 dataset：受控 key -> 真实素材目录 + 真值约定。
# Job 只能引用这里的 key，不能填任意路径 / 任意素材目录。
_DATASETS = {
    # 隔离 fixture：真实算法在合成的模板/场景小样本上实跑，仅用于验证平台集成链路，
    # 与真实评估资产（真实游戏截屏 / 真实模板 + 人工标注）完全独立。
    "fixture-small": {
        "tpl_dir": os.path.join(_DEMO, "matcheval_fixture", "tpl"),
        "scenes_dir": os.path.join(_DEMO, "matcheval_fixture", "scenes"),
        "ground_truth": "fixture-filename",
        "note": "隔离合成 fixture：真值编码于模板文件名 tpl_{id}_{scene}.png（{id} 仅在 {scene} 场景真实存在）",
    },
    # 仅用于验证“脚本失败如实归档 failed”：素材目录为空（match_eval 无图即非零退出），
    # 不指向任何真实或合成资产。
    "fixture-empty": {
        "tpl_dir": os.path.join(_DEMO, "matcheval_fixture", "empty", "tpl"),
        "scenes_dir": os.path.join(_DEMO, "matcheval_fixture", "empty", "scenes"),
        "ground_truth": "none",
        "note": "空素材目录：用于验证脚本失败时的诚实归档（match_eval 报缺图而非伪造成功）",
    },
    # 真实游戏素材：从 PrivaHigh（《私立高中校长》）实机画面采集，5 场景 / 23 模板，1920×1080。
    # 真值同样走文件名约定：tpl_{id}_{scene}.png 只在 scene_{scene}.png 中算真存在。
    # 已用未改动的 match_eval.py 实测：阈值 0.8 / 0.9 / 0.95 下 precision=recall=F1=1.00。
    "privahigh-real": {
        "tpl_dir": os.path.join(_PRIVAHIGH_ASSETS, "tpl"),
        "scenes_dir": os.path.join(_PRIVAHIGH_ASSETS, "scenes"),
        "ground_truth": "fixture-filename",
        "note": "真实 PrivaHigh 游戏 UI 素材（5 场景 / 23 模板）：真值编码于模板文件名 "
                "tpl_{id}_{scene}.png（{id} 仅在 {scene} 场景真实存在）",
    },
}

# 真值命名前缀：tpl_{id}_{scene_key}.png；场景文件为 scene_{scene_key}.png。
_TPL_PREFIX = "tpl_"
_TPL_SUFFIX = ".png"


class MatchEvalRunner(BaseRunner):
    engine = "matcheval"

    # ---- 子进程执行 ----
    def run(self, job, env, executor, junit_path, log_sink, is_cancelled, register_proc=None) -> RunnerResult:
        params = job["params"] or {}

        dkey = str(params.get("dataset", "fixture-small"))
        if dkey not in _DATASETS:
            return RunnerResult(status="failed", exit_code=-1,
                                error=f"非白名单 dataset key: {dkey!r}（可选: {list(_DATASETS)}）")
        ds = _DATASETS[dkey]
        tpl_dir, scenes_dir = ds["tpl_dir"], ds["scenes_dir"]
        if not (os.path.isdir(tpl_dir) and os.path.isdir(scenes_dir)):
            return RunnerResult(status="failed", exit_code=-1,
                                error=f"dataset 素材目录缺失: tpl={tpl_dir} scenes={scenes_dir}")

        # algorithm：单字符串或列表，白名单校验
        alg_raw = params.get("algorithm", ["tpl", "mstpl"])
        if isinstance(alg_raw, str):
            alg_raw = [x.strip() for x in alg_raw.split(",") if x.strip()]
        algorithms = [a for a in alg_raw if a in _ALGORITHMS]
        if not algorithms:
            return RunnerResult(status="failed", exit_code=-1,
                                error=f"非白名单 algorithm: {alg_raw!r}（可选: {list(_ALGORITHMS)}）")

        # 焦点阈值（用于派生单值 precision/recall/F1）；不在档位内则取最近档
        try:
            focus_th = float(params.get("threshold", 0.8))
        except (TypeError, ValueError):
            focus_th = 0.8
        focus_th = min(_THRESHOLDS, key=lambda t: abs(t - focus_th))

        # 输出级别：仅影响 engine_data 是否包含逐格样本结果（用于前端展示密度）
        output_level = str(params.get("output_level", "full"))

        rdir = os.path.dirname(junit_path)
        os.makedirs(rdir, exist_ok=True)
        csv_path = os.path.join(rdir, "matcheval.csv")

        cmd = [executor["python_executable"], MATCH_EVAL_PY,
               "--tpl-dir", tpl_dir, "--scenes-dir", scenes_dir,
               "--out", csv_path,
               "--thresholds", ",".join(f"{t:g}" for t in _THRESHOLDS),
               "--algorithms", ",".join(algorithms),
               "--png"]
        log_sink(f"$ {shell_cmd(cmd)}\n$ cwd={rdir}\n")

        # 统一注入 PYTHONIOENCODING=utf-8（否则中文日志会乱码）+
        # 避免向只读的 Airtest 源码目录写 .pyc
        run_env = build_child_env({"PYTHONDONTWRITEBYTECODE": "1"})

        proc = subprocess.Popen(
            cmd, cwd=rdir, env=run_env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        if register_proc:
            register_proc(proc)

        outcome = drain_proc(proc, log_sink, is_cancelled,
                             "\n[cancel] 收到取消请求，终止 match_eval 进程树\n")
        if outcome == "cancelled":
            return RunnerResult(status="cancelled", exit_code=130, error="cancelled by user")
        reap_proc(proc)
        return self._finalize(proc.returncode, rdir, csv_path, tpl_dir, scenes_dir,
                              dkey, ds, algorithms, focus_th, output_level)

    # ---- 结果派生（不改 match_eval 算法，仅在 Runner 侧按真值约定折算指标） ----
    def _finalize(self, exit_code, rdir, csv_path, tpl_dir, scenes_dir,
                  dkey, ds, algorithms, focus_th, output_level) -> RunnerResult:
        rows = self._rows(csv_path)
        heatmaps = sorted(
            os.path.join(rdir, f) for f in os.listdir(rdir)
            if f.startswith("heatmap_") and f.endswith(".png"))

        # 脚本异常 / 无产物 => 诚实 failed
        if exit_code != 0 or not rows:
            error = f"match_eval 执行失败 (exit={exit_code})" if exit_code != 0 else "无任何评估结果(CSV 为空)"
            return RunnerResult(status="failed", exit_code=exit_code,
                                error=error, metrics={"sample_total": len(rows)})

        # ---------- 真值判定（fixture-filename 约定：tpl_{id}_{scene_key}.png） ----------
        def ground_truth(template: str, scene: str) -> bool:
            base = template[:-len(_TPL_SUFFIX)] if template.endswith(_TPL_SUFFIX) else template
            # tpl_{id}_{scene_key}
            parts = base.split("_") if base.startswith(_TPL_PREFIX) else []
            pos_scene = parts[-1] if len(parts) >= 2 else ""
            return scene == f"scene_{pos_scene}.png"

        def is_pos(template: str, scene: str, status: str) -> bool:
            # 预测正类 = 过线(hit)；miss/no_result/exception 均视为预测负类
            return status == "hit"

        # ---------- 主指标：焦点阈值下所有算法合并聚合 ----------
        focus_rows = [r for r in rows if abs(float(r.get("threshold") or 0) - focus_th) < 1e-9]
        tp = fp = fn = tn = 0
        for r in focus_rows:
            gt = ground_truth(r["template"], r["scene"])
            pred = is_pos(r["template"], r["scene"], r["status"])
            if gt and pred:   tp += 1
            elif not gt and pred: fp += 1
            elif gt and not pred: fn += 1
            else:             tn += 1
        sample_total = len(focus_rows)
        precision = round(tp / (tp + fp), 6) if (tp + fp) else 0.0
        recall = round(tp / (tp + fn), 6) if (tp + fn) else 0.0
        f1 = round(2 * precision * recall / (precision + recall), 6) if (precision + recall) else 0.0
        try:
            duration_ms = round(sum(float(r.get("time_ms") or 0) for r in focus_rows), 1)
        except (TypeError, ValueError):
            duration_ms = 0.0

        metrics = {
            "sample_total": sample_total,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1,
            "duration_ms": duration_ms,
        }

        # ---------- 分组指标：每 (algorithm × threshold) ----------
        groups: dict = {}
        for alg in algorithms:
            for th in _THRESHOLDS:
                g = [r for r in rows if r["algorithm"] == alg and abs(float(r["threshold"] or 0) - th) < 1e-9]
                gp = fp_ = fn_ = 0
                for r in g:
                    gt = ground_truth(r["template"], r["scene"])
                    pred = is_pos(r["template"], r["scene"], r["status"])
                    if gt and pred:   gp += 1
                    elif not gt and pred: fp_ += 1
                    elif gt and not pred: fn_ += 1
                denom_p = gp + fp_
                denom_r = gp + fn_
                pp = round(gp / denom_p, 6) if denom_p else 0.0
                rr = round(gp / denom_r, 6) if denom_r else 0.0
                groups.setdefault(alg, []).append({
                    "threshold": th, "tp": gp, "fp": fp_, "fn": fn_,
                    "precision": pp, "recall": rr,
                    "f1": round(2 * pp * rr / (pp + rr), 6) if (pp + rr) else 0.0,
                    "samples": len(g),
                })
        group_metrics = [dict(algorithm=k, cells=groups[k]) for k in algorithms]

        # ---------- 错误样本：miss/exception 行的真实模板与场景缩略图 ----------
        error_arts, error_samples = [], []
        seen = set()
        for r in rows:
            if r["status"] not in ("miss", "exception"):
                continue
            key = (r["template"], r["scene"], r["algorithm"])
            if key in seen:
                continue
            seen.add(key)
            tpl_src = os.path.join(tpl_dir, r["template"])
            dst = os.path.join(rdir, f"error_{r['algorithm']}_{r['template']}")
            if os.path.exists(tpl_src):
                # 作为“错误样本图”产物：真实模板小图（唯一化，避免同名覆盖）
                dst = os.path.join(rdir, f"error_{r['algorithm']}_{r['template']}_{len(error_arts)}.png")
                shutil.copyfile(tpl_src, dst)
                error_arts.append({"name": os.path.basename(dst),
                                   "type": "image/png", "path": os.path.basename(dst),
                                   "size": os.path.getsize(dst)})
            error_samples.append({
                "template": r["template"], "scene": r["scene"], "algorithm": r["algorithm"],
                "threshold": r["threshold"], "status": r["status"], "confidence": r.get("confidence"),
            })

        # ---------- artifacts ----------
        artifacts = []
        artifacts.append({"name": "matcheval.csv", "type": "text/csv",
                          "path": os.path.basename(csv_path),
                          "size": os.path.getsize(csv_path) if os.path.exists(csv_path) else 0})
        for h in heatmaps:
            artifacts.append({"name": os.path.basename(h), "type": "image/png",
                              "path": os.path.basename(h), "size": os.path.getsize(h)})
        artifacts += error_arts

        # ---------- engine_data ----------
        engine_data = {
            "dataset": dkey,
            "dataset_note": ds["note"],
            "ground_truth": ds["ground_truth"],
            "algorithm": algorithms,
            "focus_threshold": focus_th,
            "output_level": output_level,
            "group_metrics": group_metrics,
            "heatmap_index": [{"name": os.path.basename(h), "path": os.path.basename(h)} for h in heatmaps],
            "error_samples": error_samples,
        }
        if output_level == "full":
            engine_data["sample_results"] = [
                {"template": r["template"], "scene": r["scene"], "algorithm": r["algorithm"],
                 "threshold": r["threshold"], "status": r["status"],
                 "confidence": r.get("confidence")}
                for r in focus_rows]

        summary = {
            "dataset": dkey,
            "template_count": len(os.listdir(tpl_dir)),
            "scene_count": len(os.listdir(scenes_dir)),
            "algorithm_count": len(algorithms),
            "threshold_count": len(_THRESHOLDS),
            "focus_threshold": focus_th,
            "sample_rows": len(focus_rows),
            "miss_or_exc": len([r for r in focus_rows if r["status"] in ("miss", "exception")]),
        }
        return RunnerResult(
            status="success", exit_code=exit_code, summary=summary, metrics=metrics,
            artifacts=artifacts, engine_data=engine_data,
            engine_data_schema="matcheval/v1", error=None,
        )

    # ---------- CSV 解析 ----------
    @staticmethod
    def _rows(path: str) -> list[dict]:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:  # noqa: BLE001
            return []
