"""QQ（NT 架构，macOS）“我的收藏”表情提取。

数据位置：
  ~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/
      nt_qq_<账号哈希>/nt_data/Emoji/personal_emoji/Ori/

personal_emoji 是 QQ 的“个人表情 = 我的收藏”目录：
  Ori/    原图（导出的就是这里）
  Thumb/  缩略图（不导出）

以下目录**不是**收藏、一律不碰（这正是上一版半成品混入杂项的来源）：
  emoji-recv/            聊天中收到的表情缓存
  emoji-related/         表情联想/推荐资源
  marketface/            表情商城（已购/已下载表情包）
  BaseEmojiSyastems/     系统黄脸表情资源
  Ori*Temp, Thumb*Temp   下载中的临时文件
"""
from __future__ import annotations

from pathlib import Path

from .common import HOME, Candidate

QQ_ROOT = HOME / "Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ"


def accounts(root: Path = QQ_ROOT) -> list[Path]:
    """返回所有 nt_qq_* 账号目录。"""
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("nt_qq_*") if p.is_dir())


def collect(root: Path = QQ_ROOT) -> tuple[list[Candidate], list[str]]:
    cands: list[Candidate] = []
    sources: list[str] = []
    for acc in accounts(root):
        ori = acc / "nt_data" / "Emoji" / "personal_emoji" / "Ori"
        if not ori.is_dir():
            continue
        sources.append(str(ori))
        for f in ori.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            cands.append(Candidate("QQ", f, note=f"{acc.name}/.../personal_emoji/Ori/{f.name}",
                                   sort_key=mtime))
    return cands, sources
