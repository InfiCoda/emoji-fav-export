"""最小化的 SQLCipher 4 解密器（只读）：用于微信 4.x 的 emoticon.db。

微信(WCDB)参数：AES-256-CBC + HMAC-SHA512，页大小 4096，保留区 80 字节(IV16 + HMAC64)。
这里使用“原始密钥(raw key)”，即进程内存中缓存的 32 字节派生密钥 enc_key，不再做 PBKDF2。

依赖：cryptography（仅微信功能需要）。
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from pathlib import Path

PAGE = 4096
RESERVE = 80
IV = 16
MAC = 64
SQLITE_MAGIC = b"SQLite format 3\x00"


class KeyError_(Exception):
    pass


def _aes_cbc(key: bytes, iv: bytes, data: bytes, decrypt: bool) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # pragma: no cover
        raise SystemExit("微信功能需要 cryptography：pip install cryptography") from e
    c = Cipher(algorithms.AES(key), modes.CBC(iv))
    ctx = c.decryptor() if decrypt else c.encryptor()
    return ctx.update(data) + ctx.finalize()


def _mac_key(enc_key: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", enc_key, bytes(b ^ 0x3A for b in salt), 2, 32)


def _page_hmac(mac_key: bytes, page: bytes, pgno: int) -> bytes:
    off = 16 if pgno == 1 else 0
    body = page[off:PAGE - RESERVE + IV]
    return hmac.new(mac_key, body + struct.pack("<I", pgno), hashlib.sha512).digest()


def resolve_key(db_path: Path, key_hex: str) -> str:
    """校验候选密钥并返回数据库实际使用的 32 字节原始密钥。"""
    normalized = key_hex.strip().lower().removeprefix("x'").removesuffix("'")
    try:
        candidate = bytes.fromhex(normalized)
    except ValueError as e:
        raise KeyError_("密钥应为 64 位十六进制（32 字节）") from e
    if len(candidate) != 32:
        raise KeyError_("密钥应为 64 位十六进制（32 字节）")

    with db_path.open("rb") as file:
        page = file.read(PAGE)
    if len(page) != PAGE:
        raise KeyError_("emoticon.db 首页不完整")
    salt = page[:16]
    for key in (candidate, hashlib.pbkdf2_hmac(
            "sha512", candidate, salt, 256_000, 32)):
        mac_key = _mac_key(key, salt)
        if hmac.compare_digest(page[PAGE - MAC:PAGE],
                               _page_hmac(mac_key, page, 1)):
            return key.hex()
    raise KeyError_("数据库密钥不匹配")


def decrypt_page(enc_key: bytes, mac_key: bytes, page: bytes, pgno: int,
                 salt: bytes, verify: bool = True) -> bytes:
    off = 16 if pgno == 1 else 0
    if verify:
        want = page[PAGE - MAC:PAGE]
        if not hmac.compare_digest(want, _page_hmac(mac_key, page, pgno)):
            raise KeyError_(f"第 {pgno} 页 HMAC 校验失败（密钥不对或数据损坏）")
    iv = page[PAGE - RESERVE:PAGE - RESERVE + IV]
    plain = _aes_cbc(enc_key, iv, page[off:PAGE - RESERVE], decrypt=True)
    head = SQLITE_MAGIC if pgno == 1 else b""
    return head + plain + b"\x00" * RESERVE


def encrypt_page(enc_key: bytes, mac_key: bytes, plain_body: bytes, pgno: int,
                 salt: bytes, iv: bytes) -> bytes:
    """仅用于单元测试：构造与真实格式一致的加密页。plain_body 为未含保留区的明文。"""
    off = 16 if pgno == 1 else 0
    ct = _aes_cbc(enc_key, iv, plain_body, decrypt=False)
    page = (salt if pgno == 1 else b"") + ct + iv
    page = page + b"\x00" * (PAGE - len(page))
    return page[:PAGE - MAC] + _page_hmac(mac_key, page, pgno)


def decrypt_db(db_path: Path, key_hex: str, wal_path: Path | None = None) -> bytes:
    """解密整个数据库（并回放 WAL 中已提交的帧），返回标准 SQLite 文件字节。"""
    key_hex = key_hex.strip().lower().removeprefix("x'").removesuffix("'")
    enc_key = bytes.fromhex(key_hex[:64])
    if len(enc_key) != 32:
        raise KeyError_("密钥应为 64 位十六进制（32 字节 enc_key）")
    raw = db_path.read_bytes()
    if len(raw) < PAGE or len(raw) % PAGE:
        raise KeyError_("数据库文件大小不是 4096 的整数倍，可能不是 SQLCipher 4 / WCDB 格式")
    salt = raw[:16]
    mac_key = _mac_key(enc_key, salt)

    pages: dict[int, bytes] = {}
    for pgno in range(1, len(raw) // PAGE + 1):
        pages[pgno] = decrypt_page(enc_key, mac_key, raw[(pgno - 1) * PAGE:pgno * PAGE], pgno, salt)

    if wal_path and wal_path.is_file():
        for pgno, plain in _wal_pages(wal_path, enc_key, mac_key, salt):
            pages[pgno] = plain

    last = max(pages)
    return b"".join(pages.get(i, b"\x00" * PAGE) for i in range(1, last + 1))


def _wal_pages(wal_path: Path, enc_key: bytes, mac_key: bytes, salt: bytes):
    """回放 WAL：只采用 salt 与 WAL 头一致、且截止到最后一个 commit 帧的页。"""
    wal = wal_path.read_bytes()
    if len(wal) < 32:
        return
    s1, s2 = struct.unpack(">II", wal[16:24])
    frame = 24 + PAGE
    staged: list[tuple[int, bytes]] = []
    committed: list[tuple[int, bytes]] = []
    pos = 32
    while pos + frame <= len(wal):
        pgno, ncommit, fs1, fs2 = struct.unpack(">IIII", wal[pos:pos + 16])
        if (fs1, fs2) != (s1, s2) or pgno == 0:
            break
        page = wal[pos + 24:pos + frame]
        try:
            staged.append((pgno, decrypt_page(enc_key, mac_key, page, pgno, salt)))
        except KeyError_:
            break
        if ncommit:
            committed.extend(staged)
            staged = []
        pos += frame
    yield from committed
