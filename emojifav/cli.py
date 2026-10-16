from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import importer, qq, wechat, wechat_key, wecom
from .common import (ExportReport, export_candidates, human, write_manifest,
                     write_preview)
from .sqlcipher import KeyError_

ALL = ("qq", "wechat", "wecom")
ROOT = Path(__file__).resolve().parent.parent


def default_export_dir() -> Path:
    """默认写到本仓库 exports/<时间戳>/，不依赖当前工作目录。"""
    return ROOT / "exports" / time.strftime("%Y%m%d_%H%M%S")


def highlight(text: str) -> str:
    return f"\033[1;36m{text}\033[0m" if sys.stdout.isatty() else text


def cmd_list(args) -> int:
    print("== 本机检测 ==")
    for acc in qq.accounts():
        ori = acc / "nt_data/Emoji/personal_emoji/Ori"
        n = sum(1 for f in ori.iterdir() if f.is_file()) if ori.is_dir() else 0
        print(f"QQ      {acc.name}  收藏原图文件 {n} 个  ({ori})")
    if not qq.accounts():
        print("QQ      未找到 NT 版 QQ 数据目录")
    ps = wecom.profiles()
    for p in ps:
        n = sum(1 for b in (p / 'Caches/Emotions').iterdir()
                if b.is_dir() and wecom.FAV_BUCKET.match(b.name)
                for f in b.rglob('*') if f.is_file())
        print(f"WeCom   {p.name}  收藏表情文件 {n} 个")
    if not ps:
        print("WeCom   未找到企业微信数据目录")
    for a in wechat.accounts():
        print(f"WeChat  {a.name}  已找到收藏数据库（导出时自动获取密钥）")
    if not wechat.accounts():
        print("WeChat  未找到微信 4.x 数据目录")
    return 0


def cmd_export(args) -> int:
    apps = [a.strip().lower() for a in args.app.split(",")] if args.app != "all" else list(ALL)
    if "wechat" in apps and importlib.util.find_spec("cryptography") is None:
        print("缺少微信导出依赖 cryptography。请先执行：")
        print("  .venv/bin/pip install -e .")
        print("然后使用项目环境运行：")
        print("  .venv/bin/python -m emojifav export")
        return 1
    out = Path(args.out).expanduser() if args.out else default_export_dir()
    out.mkdir(parents=True, exist_ok=True)
    reports: list[ExportReport] = []
    tmp: Path | None = None

    for app in apps:
        if app == "qq":
            cands, sources = qq.collect()
            rep = export_candidates("QQ", cands, out, sources, args.keep_incomplete)
        elif app == "wecom":
            cands, sources = wecom.collect()
            rep = export_candidates("WeCom", cands, out, sources, args.keep_incomplete)
        elif app == "wechat":
            key_text = args.wechat_key or ""
            if args.wechat_key_file:
                key_text += "\n" + Path(args.wechat_key_file).expanduser().read_text(errors="ignore")
            tmp = Path(tempfile.mkdtemp(prefix="emojifav_wx_"))
            key_notes: list[str] = []
            if not key_text.strip():
                print("[WeChat] 正在自动获取数据库密钥...")
                try:
                    key_text, key_notes = wechat_key.acquire_keys(
                        wechat.accounts(), tmp, timeout=args.wechat_key_timeout
                    )
                except KeyError_ as e:
                    print(f"[WeChat] 失败：{e}")
                    continue
            try:
                cands, sources, notes = wechat.collect(key_text, tmp, offline=args.offline)
            except KeyError_ as e:
                print(f"[WeChat] 失败：{e}")
                continue
            notes = key_notes + notes
            for n in notes:
                print("[WeChat]", n)
            rep = export_candidates("WeChat", cands, out, sources, args.keep_incomplete)
            rep.warnings = notes + rep.warnings
        else:
            print(f"未知应用: {app}")
            continue
        reports.append(rep)
        print(f"[{rep.app}] 候选 {rep.found} → 导出 {rep.exported}"
              f"（重复 {rep.duplicates}，非图片 {rep.skipped_not_image}，"
              f"残缺 {rep.skipped_incomplete}，空文件 {rep.skipped_empty}）")
        for w in rep.warnings[:5]:
            print("   !", w)
        if len(rep.warnings) > 5:
            print(f"   ... 还有 {len(rep.warnings) - 5} 条提示，见 manifest.json")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    if not reports:
        print("没有导出任何内容")
        return 1
    m = write_manifest(out, reports)
    p = write_preview(out, reports)
    total = sum(i["bytes"] for r in reports for i in r.items)
    print(f"\n完成：{sum(r.exported for r in reports)} 张，{human(total)}")
    print(f"目录    {out}\n清单    {m}\n预览    {p}")
    print(highlight(f"查看预览：open \"{p}\"  ← 复制并执行此命令"))
    return 0


def cmd_import(args) -> int:
    src = Path(args.src).expanduser()
    if not src.is_dir():
        print(f"目录不存在: {src}")
        return 1
    dest = Path(args.dest).expanduser() if args.dest else src.parent / "import_ready"
    mb = -1 if args.max_mb is None else args.max_mb
    res = importer.prepare(args.app, src, dest, batch=args.batch, max_mb=mb,
                           open_finder=not args.no_open)
    print(f"可导入 {res['ok']} 张，分 {res['batches']} 批 → {res['dir']}")
    for f, why in res["warn"][:10]:
        print(f"  提醒 {f.name}: {why}")
    for f, why in res["bad"][:10]:
        print(f"  剔除 {f.name}: {why}")
    print("\n操作：" + res["steps"])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emojifav", description="导出 QQ/微信/企业微信 中用户自己收藏的表情")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="检测本机各客户端的收藏表情").set_defaults(fn=cmd_list)

    e = sub.add_parser("export", help="导出收藏表情")
    e.add_argument("--app", default="all", help="qq,wechat,wecom 或 all（默认 all）")
    e.add_argument("-o", "--out", help="输出目录（默认 本项目/exports/时间戳）")
    e.add_argument("--wechat-key", help="微信 emoticon.db 的 64 位十六进制密钥")
    e.add_argument("--wechat-key-file", help="含密钥的文本/JSON 文件（自动挑选能解开的）")
    e.add_argument("--wechat-key-timeout", type=int, default=180,
                   help="自动获取微信密钥的等待秒数（默认 180）")
    e.add_argument("--offline", action="store_true", help="微信：不联网，只用本地 Persist 文件")
    e.add_argument("--keep-incomplete", action="store_true", help="保留疑似残缺的图片")
    e.set_defaults(fn=cmd_export)

    i = sub.add_parser("import", help="导入前体检并分批，打开 Finder（客户端里点“+”选择即可）")
    i.add_argument("--app", required=True, choices=ALL)
    i.add_argument("--src", required=True, help="导出目录里的某个应用子目录，如 exports/<时间戳>/QQ")
    i.add_argument("--dest", help="分批输出根目录（默认 <src>/../import_ready）")
    i.add_argument("--batch", type=int, default=50, help="每批张数（默认 50）")
    i.add_argument("--max-mb", type=float, help="单张大小提醒阈值（默认：微信 1MB，其余不提醒）")
    i.add_argument("--no-open", action="store_true", help="不自动打开 Finder")
    i.set_defaults(fn=cmd_import)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
