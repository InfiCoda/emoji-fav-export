"""导入辅助：QQ / 微信 / 企业微信都支持在“收藏表情”面板里点“+”直接选本地图片添加，
所以导出的就是普通 gif/png/jpg 文件，不需要私有格式。

本模块不去“模拟点击”各客户端的界面（需要辅助功能权限、极易随版本失效、还可能误操作），
而是把导入前最容易踩坑的事情做掉：
  1. 体检：只放行 gif/png/jpg，标出过大/过小/webp 等可能被客户端拒绝的文件；
  2. 分批：把文件复制到 import_ready/<app>/batch_NN/，一批一批在文件选择框里 ⌘A 全选即可；
  3. 在 Finder 里打开目录，并打印该应用里“+”按钮的位置。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .common import human, sniff_ext

IMPORT_STEPS = {
    "qq": "QQ：聊天窗口 → 表情按钮 → 底部「♡ 我的收藏」→ 左上角虚线框「+」→ 在文件选择框里 ⌘A 全选一批 → 打开",
    "wechat": "微信：聊天窗口 → 表情按钮 → 「♡ 收藏」→ 「+」(添加的单个表情) → 在文件选择框里选一批 → 打开",
    "wecom": "企业微信：聊天窗口 → 表情按钮 → 「收藏」页 → 「+」(添加表情) → 在文件选择框里选一批 → 打开",
}

# 经验值，客户端版本不同可能有差异，可用 --max-mb 覆盖；None 表示不提醒
DEFAULT_MAX_MB = {"qq": None, "wechat": 1.0, "wecom": None}


def prepare(app: str, src: Path, dest_root: Path, batch: int = 50,
            max_mb: float | None = -1, open_finder: bool = True) -> dict:
    app = app.lower()
    if app not in IMPORT_STEPS:
        raise SystemExit(f"未知应用 {app}，可选：{', '.join(IMPORT_STEPS)}")
    if max_mb == -1:
        max_mb = DEFAULT_MAX_MB[app]
    files = sorted(p for p in src.iterdir() if p.is_file() and not p.name.startswith("."))
    ok, warn, bad = [], [], []
    for f in files:
        ext = sniff_ext(f.read_bytes()[:16])
        if ext not in ("gif", "png", "jpg"):
            bad.append((f, f"格式 {ext or '未知'} 通常不被添加表情接受"))
            continue
        if f.suffix.lower().lstrip(".").replace("jpeg", "jpg") != ext:
            bad.append((f, f"扩展名与内容不符(内容是 {ext})，请用导出工具生成的文件"))
            continue
        size = f.stat().st_size
        if max_mb and size > max_mb * 1024 * 1024:
            warn.append((f, f"{human(size)} 超过 {max_mb}MB，{app} 可能拒收"))
        ok.append(f)

    out = dest_root / app
    if out.exists():
        shutil.rmtree(out)
    batches = [ok[i:i + batch] for i in range(0, len(ok), batch)]
    for i, group in enumerate(batches, 1):
        d = out / f"batch_{i:02d}"
        d.mkdir(parents=True)
        for f in group:
            try:
                os.link(f, d / f.name)       # 同盘硬链接，不占额外空间
            except OSError:
                shutil.copy2(f, d / f.name)
    if open_finder and sys.platform == "darwin" and batches:
        subprocess.run(["open", str(out)], check=False)
    return {"ok": len(ok), "warn": warn, "bad": bad, "batches": len(batches),
            "dir": str(out), "steps": IMPORT_STEPS[app]}
