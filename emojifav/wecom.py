"""企业微信（macOS）“收藏的表情”提取。

数据位置：
  ~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<账号哈希>/Caches/Emotions/<分桶>/<id>/<id>.<ext>

Emotions 下按“消息月份”分桶：
  2026-07 / 2026-08 ...   聊天里收发过的表情缓存（挂在某条消息上，**不是**收藏）
  1970-1                  没有关联消息时间（时间戳=0 → 1970）的表情，也就是
                          “收藏表情”列表同步下来的原图

依据（在本机实测）：1970-1 里的表情有不少同时也出现在后续月份目录
（= 把收藏的表情发到聊天里），反过来月份目录里大量表情不在 1970-1
（= 别人发来的、没收藏）。所以只取 1970-* 桶，月份桶全部忽略。

注意：企业微信的收藏库（Collection.db）是加密的，无法直接核对完整列表；
这里导出的是“已同步到本机的收藏原图”。若收藏里有从没点开过/没下载到本机的表情，
本地没有文件，自然无法导出。
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import HOME, Candidate

WECOM_ROOT = HOME / "Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles"
FAV_BUCKET = re.compile(r"^1970-\d+$")


def profiles(root: Path = WECOM_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "Caches" / "Emotions").is_dir())


def collect(root: Path = WECOM_ROOT) -> tuple[list[Candidate], list[str]]:
    cands: list[Candidate] = []
    sources: list[str] = []
    for prof in profiles(root):
        emo = prof / "Caches" / "Emotions"
        for bucket in sorted(emo.iterdir()):
            if not bucket.is_dir() or not FAV_BUCKET.match(bucket.name):
                continue
            sources.append(str(bucket))
            for f in bucket.rglob("*"):
                if not f.is_file() or f.name.startswith("."):
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                cands.append(Candidate("WeCom", f,
                                       note=f"{prof.name}/Caches/Emotions/{bucket.name}/{f.name}",
                                       sort_key=mtime))
    return cands, sources
