"""生成 match_eval 的隔离 fixture：合成模板(tpl)+整屏场景(scenes)小图。

NOTE: 仅用于平台集成验证的隔离 fixture，与真实评估资产(真实游戏截屏/真实模板)完全独立。
真实资产需用户放入配置目录后，在 Runner 白名单 key 中登记，不迁移进平台。

ground-truth 约定（写入文件名，供 Runner 解析真值，不改 match_eval 算法）：
    tpl_{id}_{scene}.png   —— 模板 {id} 仅在 {scene} 场景中真实存在（pos），其余场景为 neg。
合成：场景为带纹理色块 UI + 轻微噪声；模板从对应场景精确裁剪其 UI 按钮区。
依赖：numpy + opencv（cv2）。用法：python make_matcheval_fixture.py <输出目录>
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

# 调色区分场景（保证 neg 场景与模板来源场景视觉差异足够大，避免误命中）
PALETTE = {
    "menu":  ((80, 180, 240), (255, 152, 48), (76, 175, 80)),   # 蓝底/橙钮/绿钮
    "store": ((120, 90, 200), (255, 220, 66), (233, 30, 99)),   # 紫底/黄钮/粉钮
}


def _ui_block(dst, x, y, w, h, color, center_rgb):
    """一个带边框与内渐变纹理的 UI 色块，模拟按钮，保留足够特征供匹配。"""
    cv2.rectangle(dst, (x, y), (x + w, y + h), color, -1)
    cv2.rectangle(dst, (x, y), (x + w, y + h), (255, 255, 255), 2)
    # 加一个水平亮条模拟按钮高光，提供稳定匹配特征
    cv2.line(dst, (x, y + h // 2), (x + w, y + h // 2), (255, 255, 255), 3)
    return (x, y, w, h)


def _make_scene(key: str, size=(420, 300)):
    bg, btn_a, btn_b = PALETTE[key]
    scene = np.full((size[1], size[0], 3), bg, np.uint8)
    # 轻微噪声（保证与"精确裁剪模板"形成的差异在匹配容差内）
    noise = np.random.default_rng(len(key)).integers(-12, 13, scene.shape, dtype=np.int16)
    scene = np.clip(scene.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    # 场景专属的若干 UI 块（菜单场景放 A/B 两钮；商店场景放 C/D 两钮，布局镜像防串扰）
    blocks = {}
    if key == "menu":
        blocks["A"] = _ui_block(scene, 40, 60, 120, 44, btn_a, btn_a)
        blocks["B"] = _ui_block(scene, 40, 130, 120, 44, btn_b, btn_b)
        cv2.rectangle(scene, (30, 30), (170, 200), (255, 255, 255), 2)
    else:  # store
        blocks["C"] = _ui_block(scene, 60, 240, 140, 46, btn_a, btn_a)
        blocks["D"] = _ui_block(scene, 250, 40, 140, 46, btn_b, btn_b)
        cv2.rectangle(scene, (230, 300), (410, 110), (255, 255, 255), 2)
    return scene, blocks


def main(out_dir: str) -> None:
    scenes_dir = os.path.join(out_dir, "scenes")
    tpl_dir = os.path.join(out_dir, "tpl")
    os.makedirs(scenes_dir, exist_ok=True)
    os.makedirs(tpl_dir, exist_ok=True)

    scenes = {k: _make_scene(k) for k in PALETTE}
    for k, (img, _) in scenes.items():
        cv2.imwrite(os.path.join(scenes_dir, f"scene_{k}.png"), img)

    # 模板：从各自来源场景精确裁剪 UI 块（真实存在 -> 应对该场景 hit；对其他场景应 neg）
    for k, (img, blocks) in scenes.items():
        for block_id, (x, y, w, h) in blocks.items():
            tpl = img[y:y + h, x:x + w]  # 裁剪自同一图，算法应稳定命中
            name = f"tpl_{block_id}_{k}.png"  # 命名编码真值：{id} 仅在 {k} 场景存在
            cv2.imwrite(os.path.join(tpl_dir, name), tpl)
    print("fixture 生成完成 ->", out_dir)
    for root, _, files in os.walk(out_dir):
        for f in files:
            print("  ", os.path.join(root, f), os.path.getsize(os.path.join(root, f)), "B")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "matcheval_fixture")