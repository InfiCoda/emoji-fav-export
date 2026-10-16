import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emojifav import common, importer, qq, sqlcipher, wechat, wechat_key, wecom  # noqa: E402

GIF = (b"GIF89a\x02\x00\x02\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
       b"\x21\xf9\x04\x00\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x02\x00\x02\x00\x00\x02\x02\x44\x01\x00\x3b")
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x03\x00\x00\x00\x04\x08\x06\x00\x00\x00"
       b"\x00\x00\x00\x00\x00\x00\x00\x00IEND\xaeB`\x82")
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" \
      b"\xff\xc0\x00\x0b\x08\x00\x07\x00\x09\x01\x01\x11\x00" + b"\x00" * 20 + b"\xff\xd9"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="emojifav_t_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSniff(unittest.TestCase):
    def test_sniff_and_size(self):
        self.assertEqual(common.sniff_ext(GIF), "gif")
        self.assertEqual(common.sniff_ext(PNG), "png")
        self.assertEqual(common.sniff_ext(JPG), "jpg")
        self.assertIsNone(common.sniff_ext(b"hello world!!"))
        self.assertEqual(common.image_size(GIF, "gif"), (2, 2))
        self.assertEqual(common.image_size(PNG, "png"), (3, 4))
        self.assertEqual(common.image_size(JPG, "jpg"), (9, 7))

    def test_gif_trailing_data_is_complete_but_truncated_is_not(self):
        self.assertTrue(common.is_complete(GIF + b"QQprivate-trailer" * 3, "gif"))
        self.assertFalse(common.is_complete(GIF[:-8], "gif"))
        self.assertEqual(common.gif_end(GIF + b"xx"), len(GIF))


class TestQQ(Base):
    def test_only_personal_emoji_ori(self):
        acc = self.tmp / "nt_qq_abc" / "nt_data" / "Emoji"
        (acc / "personal_emoji" / "Ori").mkdir(parents=True)
        (acc / "personal_emoji" / "Thumb").mkdir(parents=True)
        (acc / "emoji-recv" / "2026-10" / "Ori").mkdir(parents=True)
        (acc / "marketface").mkdir(parents=True)
        # 收藏：一个内容是 JPEG 却叫 .gif 的文件 + 一个 png
        (acc / "personal_emoji" / "Ori" / "a.gif").write_bytes(JPG)
        (acc / "personal_emoji" / "Ori" / "b.jpg").write_bytes(PNG)
        # 干扰项：缩略图 / 聊天缓存 / 商城
        (acc / "personal_emoji" / "Thumb" / "a.png").write_bytes(PNG + b"1")
        (acc / "emoji-recv" / "2026-10" / "Ori" / "x.gif").write_bytes(GIF)
        (acc / "marketface" / "m.png").write_bytes(PNG + b"2")

        cands, sources = qq.collect(self.tmp)
        self.assertEqual({c.path.name for c in cands}, {"a.gif", "b.jpg"})
        rep = common.export_candidates("QQ", cands, self.tmp / "out", sources)
        self.assertEqual(rep.exported, 2)
        exts = sorted(i["ext"] for i in rep.items)
        self.assertEqual(exts, ["jpg", "png"])  # 扩展名以真实内容为准


class TestWeCom(Base):
    def test_only_1970_bucket(self):
        emo = self.tmp / "PROF" / "Caches" / "Emotions"
        for bucket, name, data in [("1970-1", "fav1", GIF), ("2026-08", "chat1", PNG), ("2026-09", "chat2", JPG)]:
            d = emo / bucket / name
            d.mkdir(parents=True)
            (d / f"{name}.gif").write_bytes(data)
        cands, _ = wecom.collect(self.tmp)
        self.assertEqual([c.path.name for c in cands], ["fav1.gif"])


class TestDedupeAndFilter(Base):
    def test_dedupe_trim_incomplete(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "1.gif").write_bytes(GIF)
        (src / "2.gif").write_bytes(GIF + b"tracking-trailer")  # 裁掉尾巴后与 1 相同 → 去重
        (src / "3.gif").write_bytes(GIF[:-10])                  # 残缺
        (src / "4.gif").write_bytes(b"not an image at all")
        (src / "5.gif").write_bytes(b"")
        cands = [common.Candidate("T", p) for p in src.iterdir()]
        rep = common.export_candidates("T", cands, self.tmp / "o", [])
        self.assertEqual((rep.exported, rep.duplicates, rep.skipped_incomplete,
                          rep.skipped_not_image, rep.skipped_empty), (1, 1, 1, 1, 1))
        out = (self.tmp / "o" / "T").iterdir().__next__().read_bytes()
        self.assertEqual(out, GIF)


class TestPreview(Base):
    def test_click_opens_encoded_local_file_in_new_tab(self):
        rep = common.ExportReport(
            "QQ", exported=1, items=[{"file": "QQ/表情 & one.gif"}]
        )
        preview = common.write_preview(self.tmp, [rep]).read_text()

        self.assertIn("点击表情可在新标签页打开对应的本地文件", preview)
        self.assertIn("href='QQ/%E8%A1%A8%E6%83%85%20%26%20one.gif'", preview)
        self.assertIn("target='_blank'", preview)
        self.assertIn("表情 &amp; one.gif", preview)


class TestImporter(Base):
    def test_prepare_batches_and_rejects_mismatch(self):
        src = self.tmp / "QQ"
        src.mkdir()
        for i in range(5):
            (src / f"{i}.png").write_bytes(PNG + bytes([i]))
        (src / "bad.gif").write_bytes(PNG)   # 内容是 png 但后缀 gif
        res = importer.prepare("qq", src, self.tmp / "ready", batch=2, open_finder=False)
        self.assertEqual((res["ok"], res["batches"], len(res["bad"])), (5, 3, 1))
        self.assertTrue((self.tmp / "ready" / "qq" / "batch_01").is_dir())


class TestSQLCipher(Base):
    def _encrypt_db(self, plain_pages, enc_key, salt):
        mac = sqlcipher._mac_key(enc_key, salt)
        out = b""
        for i, body in enumerate(plain_pages, 1):
            iv = hashlib.md5(bytes([i])).digest()
            out += sqlcipher.encrypt_page(enc_key, mac, body, i, salt, iv)
        return out

    def test_roundtrip_and_wrong_key(self):
        enc_key = bytes(range(32))
        salt = bytes(range(100, 116))
        bodies = [os.urandom(4096 - 16 - 80), os.urandom(4096 - 80)]
        raw = self._encrypt_db(bodies, enc_key, salt)
        p = self.tmp / "x.db"
        p.write_bytes(raw)
        plain = sqlcipher.decrypt_db(p, enc_key.hex())
        self.assertEqual(plain[:16], sqlcipher.SQLITE_MAGIC)
        self.assertEqual(plain[16:4096 - 80], bodies[0])
        self.assertEqual(plain[4096:8192 - 80], bodies[1])
        with self.assertRaises(sqlcipher.KeyError_):
            sqlcipher.decrypt_db(p, ("ff" * 32))

    def test_resolve_raw_key_or_passphrase(self):
        passphrase = bytes(range(32))
        salt = bytes(range(100, 116))
        enc_key = hashlib.pbkdf2_hmac(
            "sha512", passphrase, salt, 256_000, 32
        )
        raw = self._encrypt_db([os.urandom(4096 - 96)], enc_key, salt)
        db = self.tmp / "passphrase.db"
        db.write_bytes(raw)

        self.assertEqual(sqlcipher.resolve_key(db, enc_key.hex()), enc_key.hex())
        self.assertEqual(sqlcipher.resolve_key(db, passphrase.hex()), enc_key.hex())

    def test_wal_replay_only_committed(self):
        enc_key, salt = bytes(range(32)), bytes(range(16))
        mac = sqlcipher._mac_key(enc_key, salt)
        raw = self._encrypt_db([os.urandom(4096 - 96), os.urandom(4096 - 80)], enc_key, salt)
        db = self.tmp / "y.db"
        db.write_bytes(raw)
        new2 = os.urandom(4096 - 80)
        stale = os.urandom(4096 - 80)

        def frame(pgno, commit, s1, s2, body):
            enc = sqlcipher.encrypt_page(enc_key, mac, body, pgno, salt, b"\x07" * 16)
            return struct.pack(">IIIIII", pgno, commit, s1, s2, 0, 0) + enc

        wal = struct.pack(">IIIIIIII", 0x377F0682, 3007000, 4096, 0, 11, 22, 0, 0)
        wal += frame(2, 2, 11, 22, new2)        # 已提交
        wal += frame(2, 0, 11, 22, stale)       # 未提交的尾巴
        wal += frame(2, 2, 99, 99, stale)       # 旧周期 salt，应忽略
        (self.tmp / "y.db-wal").write_bytes(wal)
        plain = sqlcipher.decrypt_db(db, enc_key.hex(), self.tmp / "y.db-wal")
        self.assertEqual(plain[4096:8192 - 80], new2)


class TestWeChatAutoKey(Base):
    def test_parse_candidates_ignores_invalid_lines(self):
        path = self.tmp / "keys"
        path.write_text(
            "wxid_a " + "ab" * 32 + "\n"
            "not-valid\n"
            "* " + "CD" * 32 + "\n"
        )
        self.assertEqual(wechat_key._parse_candidates(path), [
            ("wxid_a", "ab" * 32),
            ("*", "cd" * 32),
        ])

    def test_valid_cached_key_avoids_launching_wechat(self):
        account = self.tmp / "xwechat_files" / "wxid_test"
        db = account / "db_storage" / "emoticon" / "emoticon.db"
        db.parent.mkdir(parents=True)
        key, salt = bytes(range(32)), bytes(range(100, 116))
        body = os.urandom(4096 - 96)
        db.write_bytes(sqlcipher.encrypt_page(
            key, sqlcipher._mac_key(key, salt), body, 1, salt, b"\x01" * 16
        ))
        cache = self.tmp / "cache"
        key_file = cache / "wechat_keys" / "wxid_test.key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text(key.hex())

        text, notes = wechat_key.acquire_keys(
            [account], self.tmp / "work", app=self.tmp / "missing.app",
            cache_root=cache,
        )
        self.assertEqual(text, key.hex())
        self.assertIn("缓存", notes[0])

    @unittest.skipUnless(
        sys.platform == "darwin" and shutil.which("xcrun"),
        "自动密钥组件只在装有 Xcode Command Line Tools 的 macOS 构建",
    )
    def test_native_key_helper_builds(self):
        dylib = wechat_key._build_dylib(self.tmp)
        self.assertTrue(dylib.is_file())
        self.assertGreater(dylib.stat().st_size, 0)
        root = self.tmp / "xwechat_files"
        db = root / "wxid_test" / "db_storage" / "emoticon" / "emoticon.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(bytes(range(16)))
        source = self.tmp / "pbkdf_host.c"
        source.write_text(
            "#include <CommonCrypto/CommonKeyDerivation.h>\n"
            "int main(void) { unsigned char p[32], s[16], out[32];\n"
            "for(int i=0;i<32;i++)p[i]=i; for(int i=0;i<16;i++)s[i]=i;\n"
            "return CCKeyDerivationPBKDF(kCCPBKDF2,(char*)p,32,s,16,"
            "kCCPRFHmacAlgSHA512,256000,out,32); }\n"
        )
        host = self.tmp / "pbkdf_host"
        subprocess.run([
            "xcrun", "--sdk", "macosx", "clang", str(source), "-o", str(host)
        ], check=True, capture_output=True)
        output = self.tmp / "candidates"
        env = os.environ | {
            "DYLD_INSERT_LIBRARIES": str(dylib),
            "EMOJIFAV_KEY_OUT": str(output),
            "EMOJIFAV_WECHAT_ROOT": str(root),
        }
        subprocess.run([str(host)], check=True, env=env)
        expected = hashlib.pbkdf2_hmac(
            "sha512", bytes(range(32)), bytes(range(16)), 256_000, 32
        ).hex()
        self.assertIn(("wxid_test", expected),
                      wechat_key._parse_candidates(output))


@unittest.skipUnless(shutil.which("sqlite3"), "需要 sqlite3 命令行生成带保留区的样本库")
class TestWeChatPipeline(Base):
    def test_end_to_end_with_local_persist(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        root = self.tmp / "xwechat_files"
        acc = root / "wxid_test"
        (acc / "db_storage" / "emoticon").mkdir(parents=True)
        persist = acc / "business" / "emoticon" / "Persist"

        aes = bytes(range(16))
        entries = []
        for i, img in enumerate((GIF, PNG)):
            md5 = hashlib.md5(img).hexdigest()
            pad = 16 - len(img) % 16
            e = Cipher(algorithms.AES(aes), modes.CBC(aes)).encryptor()
            blob = e.update(img + bytes([pad]) * pad) + e.finalize()
            (persist / md5[:2]).mkdir(parents=True, exist_ok=True)
            (persist / md5[:2] / md5).write_bytes(blob)
            entries.append((md5, aes.hex(), 0))
        favorite_md5 = [m for m, _, _ in entries]
        # NonStore 可能残留已移出收藏列表、但仍可正常解密的历史表情。
        history = JPG
        history_md5 = hashlib.md5(history).hexdigest()
        pad = 16 - len(history) % 16
        encryptor = Cipher(algorithms.AES(aes), modes.CBC(aes)).encryptor()
        history_blob = encryptor.update(
            history + bytes([pad]) * pad
        ) + encryptor.finalize()
        (persist / history_md5[:2]).mkdir(parents=True)
        (persist / history_md5[:2] / history_md5).write_bytes(history_blob)
        entries.append((history_md5, aes.hex(), 0))
        # 一条已删除 + 一条聊天缓存里的非收藏文件，都必须不出现。
        entries.append((hashlib.md5(b"del").hexdigest(), aes.hex(), 1))
        stray = acc / "cache" / "2026-10" / "Emoticon" / "zz"
        stray.mkdir(parents=True)
        (stray / "stray.gif").write_bytes(GIF + b"\x00")

        plain = self.tmp / "plain.db"
        sql = ("pragma page_size=4096;\n"
               "create table kNonStoreEmoticonTable(md5 text, cdn_url text, aes_key text, is_delete int);\n"
               + "".join(f"insert into kNonStoreEmoticonTable values('{m}','','{k}',{d});\n" for m, k, d in entries)
               + "create table kFavEmoticonOrderTable(md5 text);\n"
               + "".join(f"insert into kFavEmoticonOrderTable values('{m}');\n" for m in favorite_md5)
               + "create table kStoreEmoticonTable(md5 text);\ninsert into kStoreEmoticonTable values('"
               + hashlib.md5(b"store").hexdigest() + "');\n.filectrl reserve_bytes 80\nvacuum;\n")
        subprocess.run(["sqlite3", str(plain)], input=sql.encode(), check=True)
        raw = plain.read_bytes()
        self.assertEqual(raw[20], 80)

        enc_key = bytes(range(32, 64))
        salt = bytes(range(16))
        mac = sqlcipher._mac_key(enc_key, salt)
        out = b""
        for pg in range(len(raw) // 4096):
            page = raw[pg * 4096:(pg + 1) * 4096]
            body = page[16:4096 - 80] if pg == 0 else page[:4096 - 80]
            out += sqlcipher.encrypt_page(enc_key, mac, body, pg + 1, salt, os.urandom(16))
        (acc / "db_storage" / "emoticon" / "emoticon.db").write_bytes(out)

        work = self.tmp / "work"
        work.mkdir()
        junk = "some json {\"k\": \"" + "ab" * 32 + "\", \"real\": \"" + enc_key.hex() + "\"}"
        cands, sources, notes = wechat.collect(junk, work, root=root, offline=True)
        self.assertEqual(len(cands), 2, notes)
        rep = common.export_candidates("WeChat", cands, self.tmp / "o", sources)
        self.assertEqual(sorted(i["ext"] for i in rep.items), ["gif", "png"])
        self.assertTrue(all("stray" not in i["source"] for i in rep.items))


if __name__ == "__main__":
    unittest.main()
