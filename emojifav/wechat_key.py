"""macOS 上自动获取微信 4.x emoticon.db 密钥。

只操作 ~/Library/Caches/emojifav 下的微信副本，不修改 /Applications/WeChat.app。
副本通过 DYLD 注入捕获它自己交给 SQLCipher 的密钥，随后立即退出。
"""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from .common import HOME
from .sqlcipher import KeyError_, resolve_key

WECHAT_APP = Path("/Applications/WeChat.app")
CACHE_ROOT = HOME / "Library/Caches/emojifav"
NATIVE_DIR = Path(__file__).with_name("_native")


def _db(account: Path) -> Path:
    return account / "db_storage" / "emoticon" / "emoticon.db"


def _cache_file(account: Path, cache_root: Path = CACHE_ROOT) -> Path:
    return cache_root / "wechat_keys" / f"{account.name}.key"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _cached_keys(accounts: list[Path],
                 cache_root: Path = CACHE_ROOT) -> dict[str, str]:
    found: dict[str, str] = {}
    for account in accounts:
        path = _cache_file(account, cache_root)
        try:
            candidate = path.read_text().strip()
            found[account.name] = resolve_key(_db(account), candidate)
        except (OSError, KeyError_):
            path.unlink(missing_ok=True)
    return found


def _parse_candidates(path: Path) -> list[tuple[str, str]]:
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    result: list[tuple[str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and len(fields[1]) == 64:
            result.append((fields[0], fields[1].lower()))
    return result


def _running(app: Path = WECHAT_APP) -> bool:
    try:
        output = subprocess.run(
            ["/bin/ps", "-A", "-o", "args="],
            check=True, capture_output=True, text=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    needle = f"{app.name}/Contents/"
    return any(needle in line for line in output.splitlines())


def _wait_until_quit(app: Path = WECHAT_APP) -> None:
    while _running(app):
        message = (
            "检测到微信正在运行。首次自动导出需要先完全退出微信；"
            "退出后按回车继续（输入 q 取消）："
        )
        if not sys.stdin.isatty():
            raise KeyError_("首次自动导出前请完全退出微信，然后重新运行命令")
        if input(message).strip().lower() == "q":
            raise KeyError_("用户取消了微信密钥获取")


def _bundle_version(app: Path) -> str:
    try:
        with (app / "Contents/Info.plist").open("rb") as file:
            info = plistlib.load(file)
        return str(info.get("CFBundleVersion", "unknown"))
    except (OSError, plistlib.InvalidFileException):
        return "unknown"


def _prepare_app_copy(app: Path = WECHAT_APP,
                      cache_root: Path = CACHE_ROOT) -> Path:
    if not (app / "Contents/MacOS/WeChat").is_file():
        raise KeyError_(f"未找到微信应用：{app}")

    copy = cache_root / "WeChat.app"
    marker = cache_root / "wechat-version"
    version = _bundle_version(app)
    current = marker.read_text().strip() if marker.is_file() else ""
    if current != version or not (copy / "Contents/MacOS/WeChat").is_file():
        shutil.rmtree(copy, ignore_errors=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["/usr/bin/ditto", str(app), str(copy)], check=True)
            subprocess.run(
                ["/usr/bin/xattr", "-cr", str(copy)],
                check=False, capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            raise KeyError_(f"准备微信临时副本失败：{e}") from e
        marker.write_text(version)

    try:
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(copy)],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise KeyError_(f"微信临时副本签名失败：{e}") from e
    return copy


def _build_dylib(workdir: Path) -> Path:
    compiler = shutil.which("xcrun")
    if not compiler:
        raise KeyError_("自动获取微信密钥需要 Xcode Command Line Tools（xcode-select --install）")
    arch = {"arm64": "arm64", "x86_64": "x86_64"}.get(platform.machine())
    if not arch:
        raise KeyError_(f"暂不支持当前 Mac 架构：{platform.machine()}")

    output = workdir / "wechat_key_dumper.dylib"
    command = [
        compiler, "--sdk", "macosx", "clang", "-dynamiclib", "-O2",
        "-fvisibility=hidden", "-mmacosx-version-min=11.0",
        "-arch", arch, "-o", str(output),
        str(NATIVE_DIR / "wechat_key_dumper.c"),
        str(NATIVE_DIR / "fishhook.c"),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"")
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise KeyError_(f"编译微信密钥读取组件失败：{detail or e}") from e
    return output


def acquire_keys(accounts: list[Path], workdir: Path, timeout: int = 180,
                 app: Path = WECHAT_APP,
                 cache_root: Path = CACHE_ROOT) -> tuple[str, list[str]]:
    """返回可解开本机账号数据库的密钥文本和用户提示。"""
    if not accounts:
        raise KeyError_("未找到微信 4.x 账号数据")

    keys = _cached_keys(accounts, cache_root)
    if len(keys) == len(accounts):
        return "\n".join(keys.values()), ["已使用本机缓存的微信数据库密钥"]
    initial_key_count = len(keys)

    _wait_until_quit(app)
    dylib = _build_dylib(workdir)
    app_copy = _prepare_app_copy(app, cache_root)
    output = workdir / "wechat-key-candidates.txt"
    output.unlink(missing_ok=True)

    env = os.environ.copy()
    env.update({
        "DYLD_INSERT_LIBRARIES": str(dylib),
        "EMOJIFAV_KEY_OUT": str(output),
        "EMOJIFAV_WECHAT_ROOT": str(accounts[0].parent),
    })
    binary = app_copy / "Contents/MacOS/WeChat"
    try:
        child = subprocess.Popen(
            [str(binary)], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise KeyError_(f"启动微信临时副本失败：{e}") from e

    started = time.monotonic()
    saw_valid_candidate = False
    try:
        while time.monotonic() - started < timeout:
            for hinted_account, candidate in _parse_candidates(output):
                targets = [
                    account for account in accounts
                    if hinted_account == "*" or account.name == hinted_account
                ]
                for account in targets:
                    try:
                        raw_key = resolve_key(_db(account), candidate)
                    except KeyError_:
                        continue
                    saw_valid_candidate = True
                    keys[account.name] = raw_key
                    _write_private(_cache_file(account, cache_root), raw_key + "\n")
            if len(keys) > initial_key_count or saw_valid_candidate:
                break
            if child.poll() is not None:
                break
            time.sleep(0.5)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    if not keys:
        raise KeyError_(
            f"{timeout} 秒内未获取到微信数据库密钥；请登录弹出的微信并打开一次表情面板"
        )
    notes = ["已自动获取并验证微信数据库密钥"]
    if len(keys) < len(accounts):
        notes.append("部分历史微信账号没有可用密钥，已跳过")
    return "\n".join(keys.values()), notes
