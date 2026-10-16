"""微信 4.x（macOS）“添加的单个表情 = 我的收藏”提取。

【为什么微信比 QQ/企业微信麻烦】
  微信把收藏表情的清单放在加密库 emoticon.db（SQLCipher 4），表情文件
  business/emoticon/Persist/<md5前2位>/<md5> 也是加密的，且目录里还混着
  cache/YYYY-MM/Emoticon（聊天缓存）。没有数据库密钥就**无法区分“收藏”与“缓存”**，
  所以本工具不会去猜、不会把缓存导出来充数。

【密钥】
  默认复制一份微信到 ~/Library/Caches/emojifav，对副本做 ad-hoc 签名并注入
  本地动态库，读取副本自己交给 SQLCipher 的密钥；原版微信不会被修改。
  自动获取失败时仍可手动提供密钥：
      --wechat-key  <64位hex>          直接给密钥
      --wechat-key-file <文件>          任意文本/JSON，里面凡是 64 位 hex 都会逐个试，
                                        用页 HMAC 自动挑出能解开 emoticon.db 的那个

【有密钥之后】
  1. 解密 emoticon.db（含 WAL）；
  2. 优先按收藏顺序表过滤 kNonStoreEmoticonTable；没有顺序表时回退读取全表；
  3. 对每条记录：先试本地 Persist 文件解密 → 不行再按 cdn_url / encrypt_url 下载；
     以 md5 校验或魔数校验，通过才收。
  字段名因微信版本可能有差异，所以按列名关键字自动匹配，并把识别结果打印出来。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import urllib.request
from pathlib import Path
from typing import Optional

from .common import HOME, Candidate, is_complete, sniff_ext
from .sqlcipher import KeyError_, decrypt_db, resolve_key

WECHAT_ROOT = HOME / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
HEX64 = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
MD5 = re.compile(r"^[0-9a-fA-F]{32}$")


def accounts(root: Path = WECHAT_ROOT) -> list[Path]:
    """返回含 db_storage/emoticon/emoticon.db 的账号目录。"""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if (p / "db_storage" / "emoticon" / "emoticon.db").is_file())


def keys_from_text(text: str) -> list[str]:
    seen, out = set(), []
    for m in HEX64.finditer(text):
        k = m.group(0).lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _open_plain(account: Path, keys: list[str]) -> sqlite3.Connection:
    db = account / "db_storage" / "emoticon" / "emoticon.db"
    wal = db.with_name(db.name + "-wal")
    last_err: Optional[Exception] = None
    for k in keys:
        try:
            raw_key = resolve_key(db, k)
            plain = bytearray(decrypt_db(db, raw_key, wal))
        except KeyError_ as e:
            last_err = e
            continue
        # 微信库以 WAL 模式关闭；反序列化到无文件的内存连接前改为 rollback
        # 模式，否则 SQLite 仍会尝试打开一个不存在的 -wal 文件。
        plain[18] = plain[19] = 1
        conn = sqlite3.connect(":memory:")
        conn.deserialize(bytes(plain))
        conn.row_factory = sqlite3.Row
        return conn
    raise KeyError_(f"提供的 {len(keys)} 个候选密钥都无法解开 emoticon.db：{last_err}")


def _pick(cols: list[str], *patterns: str) -> Optional[str]:
    for pat in patterns:
        for c in cols:
            if re.search(pat, c, re.I):
                return c
    return None


def _try_decode(blob: bytes, md5: str, aes_key: Optional[bytes]) -> Optional[bytes]:
    """尝试把 blob 还原成图片：明文 / AES-CBC(iv=key) / AES-ECB / AES-CBC(iv=0)。"""
    def ok(b: bytes) -> bool:
        if hashlib.md5(b).hexdigest() == md5.lower():
            return True
        e = sniff_ext(b)
        return bool(e and is_complete(b, e))

    if ok(blob):
        return blob
    if aes_key and len(aes_key) in (16, 24, 32) and len(blob) % 16 == 0:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        for mode in (modes.CBC(aes_key[:16]), modes.ECB(), modes.CBC(b"\x00" * 16)):
            try:
                d = Cipher(algorithms.AES(aes_key), mode).decryptor()
                out = d.update(blob) + d.finalize()
                pad = out[-1]
                if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
                    out = out[:-pad]
                if ok(out):
                    return out
            except Exception:
                continue
    return None


def _as_key(v) -> Optional[bytes]:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v) if len(v) in (16, 24, 32) else (_as_key(bytes(v).decode("latin1")) if v else None)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"[0-9a-fA-F]{32}", s):
            return bytes.fromhex(s)
        if len(s) in (16, 24, 32):
            return s.encode()
    return None


def _download(url: str, timeout: int = 20) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def collect(key_text: str, workdir: Path, root: Path = WECHAT_ROOT,
            offline: bool = False) -> tuple[list[Candidate], list[str], list[str]]:
    """返回 (候选, 来源, 提示信息)。候选文件写在 workdir 下（用完由调用方清理）。"""
    notes: list[str] = []
    cands: list[Candidate] = []
    sources: list[str] = []
    keys = keys_from_text(key_text)
    if not keys:
        raise KeyError_("没有在提供的内容里找到 64 位十六进制密钥")

    opened = 0
    for acc in accounts(root):
        try:
            conn = _open_plain(acc, keys)
        except KeyError_:
            notes.append(f"[{acc.name}] 当前密钥不匹配，已跳过该账号")
            continue
        opened += 1
        tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
        fav = [t for t in tables if re.search(r"NonStoreEmoticon", t, re.I)]
        notes.append(f"[{acc.name}] 解密成功，表：{', '.join(tables)}")
        if not fav:
            notes.append(f"[{acc.name}] 没找到 NonStoreEmoticon 表，无法确定“收藏”清单，已放弃该账号（不会拿缓存充数）")
            conn.close()
            continue
        order: list[str] = []
        for wanted in ("kFavEmoticonOrderTable", "kCustomEmoticonOrderTable"):
            order_table = next((t for t in tables if t.lower() == wanted.lower()), None)
            if not order_table:
                continue
            order_cols = [
                row[1] for row in conn.execute(f'pragma table_info("{order_table}")')
            ]
            order_md5 = _pick(order_cols, r"^md5$", r"md5")
            if order_md5:
                order = [
                    str(row[0]).lower()
                    for row in conn.execute(
                        f'select "{order_md5}" from "{order_table}" order by rowid'
                    )
                    if row[0] and MD5.match(str(row[0]))
                ]
            if order:
                notes.append(f"[{order_table}] 按收藏列表筛选并排序 {len(order)} 条")
                break
        persist = acc / "business" / "emoticon" / "Persist"
        for t in fav:
            cur = conn.execute(f'select * from "{t}"')
            cols = [d[0] for d in cur.description]
            c_md5 = _pick(cols, r"^md5$", r"md5")
            c_key = _pick(cols, r"aes.?key", r"^key$")
            c_urls = [c for c in cols if re.search(r"url", c, re.I)]
            c_del = _pick(cols, r"is.?del", r"^del")
            notes.append(f"[{t}] 识别列 md5={c_md5} aeskey={c_key} urls={c_urls} del={c_del}")
            if not c_md5:
                notes.append(f"[{t}] 找不到 md5 列，已跳过")
                continue
            sources.append(f"{acc.name}:{t}")
            ok = miss = 0
            rows = cur.fetchall()
            if order:
                by_md5 = {str(row[c_md5] or "").lower(): row for row in rows}
                rows = [by_md5[md5] for md5 in order if md5 in by_md5]
            for n, row in enumerate(rows):
                md5 = str(row[c_md5] or "")
                if not MD5.match(md5) or (c_del and row[c_del]):
                    continue
                aes = _as_key(row[c_key]) if c_key else None
                data: Optional[bytes] = None
                local = persist / md5[:2].lower() / md5.lower()
                if local.is_file():
                    data = _try_decode(local.read_bytes(), md5, aes)
                if data is None and not offline:
                    for c in c_urls:
                        u = str(row[c] or "")
                        if u.startswith("http"):
                            blob = _download(u)
                            if blob:
                                data = _try_decode(blob, md5, aes)
                                if data:
                                    break
                if data is None:
                    miss += 1
                    continue
                f = workdir / f"{n:05d}_{md5}"
                f.write_bytes(data)
                cands.append(Candidate("WeChat", f, note=f"{acc.name}:{t}:{md5}", sort_key=-n))
                ok += 1
            notes.append(f"[{t}] 还原 {ok} 张，失败 {miss} 张（本地无文件且{'离线' if offline else '下载/解密失败'}）")
        conn.close()
    if not opened:
        raise KeyError_("提供的候选密钥无法解开任何 emoticon.db")
    return cands, sources, notes
