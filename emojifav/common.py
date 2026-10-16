"""通用工具：图片类型嗅探、尺寸解析、去重导出、manifest 与预览页生成。

设计原则：
  * 对客户端数据目录只读，绝不修改；
  * 以文件内容（魔数）判断真实格式，不信任扩展名
    （例如 QQ 的 personal_emoji 里存在 .gif 后缀但内容是 JPEG 的文件）；
  * 按 sha256 内容去重；
  * 过滤残缺文件（下载到一半的缓存），避免把坏图导进去。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import shutil
import struct
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional

HOME = Path.home()


# --------------------------------------------------------------------------
# 图片类型 / 尺寸 / 完整性
# --------------------------------------------------------------------------
def sniff_ext(data: bytes) -> Optional[str]:
    """按魔数返回 gif/png/jpg/webp，无法识别返回 None。"""
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def image_size(data: bytes, ext: str) -> Optional[tuple[int, int]]:
    try:
        if ext == "gif":
            return struct.unpack("<HH", data[6:10])
        if ext == "png":
            return struct.unpack(">II", data[16:24])
        if ext == "jpg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seglen
        if ext == "webp":
            if data[12:16] == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
    except Exception:
        pass
    return None


def gif_end(data: bytes) -> Optional[int]:
    """按 GIF 块结构走到结尾标记 0x3B，返回结尾偏移(含)；走不到说明文件残缺。

    QQ 会在 GIF 末尾追加几十字节私有数据，因此不能简单看“最后一个字节是不是 ;”。
    """
    try:
        pos = 13
        if data[10] & 0x80:
            pos += 3 * (2 << (data[10] & 7))

        def skip_sub(p: int) -> int:
            while True:
                n = data[p]
                p += 1
                if n == 0:
                    return p
                p += n

        while True:
            b = data[pos]
            if b == 0x3B:
                return pos + 1
            if b == 0x21:
                pos = skip_sub(pos + 2)
            elif b == 0x2C:
                lf = data[pos + 9]
                pos += 10
                if lf & 0x80:
                    pos += 3 * (2 << (lf & 7))
                pos = skip_sub(pos + 1)
            else:
                return None
    except IndexError:
        return None


def gif_end(data: bytes) -> Optional[int]:
    """按 GIF 块结构走到结尾标记 0x3B，返回结尾偏移(含)；走不到说明文件残缺。

    QQ 会在 GIF 末尾追加几十字节私有数据，因此不能简单看“最后一个字节是不是 ;”。
    """
    try:
        pos = 13
        if data[10] & 0x80:
            pos += 3 * (2 << (data[10] & 7))

        def skip_sub(p: int) -> int:
            while True:
                n = data[p]
                p += 1
                if n == 0:
                    return p
                p += n

        while True:
            b = data[pos]
            if b == 0x3B:
                return pos + 1
            if b == 0x21:
                pos = skip_sub(pos + 2)
            elif b == 0x2C:
                lf = data[pos + 9]
                pos += 10
                if lf & 0x80:
                    pos += 3 * (2 << (lf & 7))
                pos = skip_sub(pos + 1)
            else:
                return None
    except IndexError:
        return None


def is_complete(data: bytes, ext: str) -> bool:
    """判断图片是否完整（检查结尾标记），用于排除下载中断的缓存文件。"""
    if ext == "gif":
        return gif_end(data) is not None
    if ext == "png":
        return b"IEND" in data[-64:]
    if ext == "jpg":
        return b"\xff\xd9" in data[-512:]
    if ext == "webp":
        return len(data) >= 12 and int.from_bytes(data[4:8], "little") + 8 <= len(data)
    return False


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    """某个客户端中“用户收藏的表情”的一个候选文件。"""
    app: str
    path: Path
    note: str = ""            # 来源说明，写进 manifest 便于追溯
    sort_key: float = 0.0     # 排序用（越大越靠前）


@dataclass
class ExportReport:
    app: str
    sources: list[str] = field(default_factory=list)
    found: int = 0
    exported: int = 0
    duplicates: int = 0
    skipped_not_image: int = 0
    skipped_incomplete: int = 0
    skipped_empty: int = 0
    warnings: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def export_candidates(app: str, cands: Iterable[Candidate], out_dir: Path,
                      sources: list[str], keep_incomplete: bool = False) -> ExportReport:
    """把候选文件按内容去重后拷贝到 out_dir/<app>/，扩展名以真实格式为准。"""
    rep = ExportReport(app=app, sources=sources)
    dest = out_dir / app
    dest.mkdir(parents=True, exist_ok=True)

    seen: dict[str, str] = {}
    ordered = sorted(cands, key=lambda c: (-c.sort_key, str(c.path)))
    rep.found = len(ordered)
    for c in ordered:
        try:
            data = c.path.read_bytes()
        except OSError as e:
            rep.warnings.append(f"读取失败 {c.path}: {e}")
            continue
        if not data:
            rep.skipped_empty += 1
            continue
        ext = sniff_ext(data)
        if ext is None:
            rep.skipped_not_image += 1
            rep.warnings.append(f"非图片(魔数无法识别)，已跳过: {c.path.name}")
            continue
        if not keep_incomplete and not is_complete(data, ext):
            rep.skipped_incomplete += 1
            rep.warnings.append(f"文件疑似残缺，已跳过: {c.path.name}")
            continue
        trimmed = 0
        if ext == "gif":
            end = gif_end(data)
            if end is not None and end < len(data):
                trimmed = len(data) - end
                data = data[:end]
        trimmed = 0
        if ext == "gif":
            end = gif_end(data)
            if end is not None and end < len(data):
                trimmed = len(data) - end
                data = data[:end]
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            rep.duplicates += 1
            continue
        name = f"{len(seen) + 1:04d}_{digest[:8]}.{ext}"
        seen[digest] = name
        target = dest / name
        target.write_bytes(data)
        try:  # 保留原始修改时间，便于按时间整理
            st = c.path.stat()
            os.utime(target, (st.st_atime, st.st_mtime))
        except OSError:
            pass
        size = image_size(data, ext)
        rep.items.append({
            "file": f"{app}/{name}",
            "ext": ext,
            "bytes": len(data),
            "sha256": digest,
            "size": list(size) if size else None,
            "animated": ext == "gif" and data.count(b"\x21\xf9\x04") > 1,
            "source": c.note or str(c.path),
            **({"trimmed_trailing_bytes": trimmed} if trimmed else {}),
        })
    rep.exported = len(rep.items)
    return rep


def write_manifest(out_dir: Path, reports: list[ExportReport]) -> Path:
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tool": "emojifav",
        "apps": {r.app: {k: v for k, v in asdict(r).items() if k != "items"} for r in reports},
        "items": [dict(i, app=r.app) for r in reports for i in r.items],
    }
    p = out_dir / "manifest.json"
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def write_preview(out_dir: Path, reports: list[ExportReport]) -> Path:
    """生成可点击打开本地表情文件的相对路径预览页。"""
    parts = ["<!doctype html><meta charset='utf-8'><title>表情收藏导出预览</title>",
             "<style>body{font-family:-apple-system,sans-serif;margin:24px;background:#fafafa}"
             ".tip{color:#666;margin-bottom:24px}"
             "h2{margin-top:32px}.g{display:grid;grid-template-columns:repeat(auto-fill,96px);gap:8px}"
             ".g a{display:flex;flex-direction:column;width:96px;color:#555;text-decoration:none}"
             ".pic{width:96px;height:96px;background:#fff;border:1px solid #eee;border-radius:6px;"
             "overflow:hidden;box-sizing:border-box}.g a:hover .pic{border-color:#1677ff;"
             "box-shadow:0 0 0 2px #1677ff22}.g img{width:100%;height:100%;object-fit:contain}"
             ".name{font-size:11px;line-height:18px;overflow:hidden;text-overflow:ellipsis;"
             "white-space:nowrap;text-align:center}</style><h1>表情收藏导出预览</h1>"
             "<p class='tip'>点击表情可在新标签页打开对应的本地文件；文件名显示在缩略图下方。</p>"]
    for r in reports:
        parts.append(f"<h2>{html.escape(r.app)} · {r.exported} 张</h2><div class='g'>")
        for it in r.items:
            path = str(it["file"])
            url = html.escape(urllib.parse.quote(path, safe="/"), quote=True)
            label = html.escape(Path(path).name, quote=True)
            title = html.escape(f"点击打开本地文件：{path}", quote=True)
            parts.append(
                f"<a href='{url}' target='_blank' title='{title}'>"
                f"<span class='pic'><img loading='lazy' src='{url}' alt='{label}'></span>"
                f"<span class='name'>{label}</span></a>"
            )
        parts.append("</div>")
    p = out_dir / "preview.html"
    p.write_text("".join(parts), encoding="utf-8")
    return p


def human(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if x < 1024:
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{x:.1f}TB"


def copy_tree_flat(files: list[Path], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)
