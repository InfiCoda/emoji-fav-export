# emojifav：导出 QQ / 微信 / 企业微信里「自己收藏的表情」

只读本机数据，不改客户端任何文件，不上传。只导出“收藏”，不带聊天缓存、商城表情、系统表情。

目前仅支持 macOS，暂不支持 Windows。

## 快速开始

```bash
cd emoji-fav-export
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate
python3 -m emojifav list                         # 看本机检测到什么
python3 -m emojifav export                        # 一次导出 QQ + 微信 + 企业微信
python3 -m emojifav export --app qq               # 只导 QQ
open exports/<时间戳>/preview.html                # 肉眼核对（命令结束时会打印实际路径）
python3 -m emojifav import --app qq --src exports/<时间戳>/QQ   # 导入前体检+分批
python3 -m unittest discover -s tests             # 自测
```

也可以在 Codex、Cursor、WorkBuddy 等 AI 编程工具中打开本项目，然后直接告诉 AI：

> 请使用这个项目检测并导出我本机的收藏表情，完成后告诉我导出目录和预览文件的位置。

AI 会根据项目说明执行检测和导出命令。首次导出微信时，按提示完全退出微信即可自动获取数据库密钥。

默认输出在本项目 `exports/<时间戳>/`。需要别的位置时用 `-o`。

输出：`<out>/QQ|WeCom|WeChat/NNNN_<sha8>.<gif|png|jpg>` + `manifest.json`（每张的来源、尺寸、是否动图）+ `preview.html`。

导出完成后，终端会打印 `preview.html` 的实际路径。执行 `open <该路径>` 即可在浏览器中查看全部表情；点击某个表情会在新标签页打开对应的本地文件，缩略图下方的文件名与导出目录中的文件一一对应，方便在 `QQ`、`WeCom` 或 `WeChat` 子目录中快速找到需要转移的 emoji。

## 各客户端表情存放位置

- QQ：`~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/nt_qq_*/nt_data/Emoji/personal_emoji/Ori/`
- 企业微信：`~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<账号>/Caches/Emotions/1970-*/`
- 微信 4.x：
  - 收藏清单：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage/emoticon/emoticon.db`
  - 本地文件：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/business/emoticon/Persist/`

`~/Library` 默认被 Finder 隐藏，可在 Finder 中按 `⌘⇧G` 后粘贴路径，或按 `⌘⇧.` 显示隐藏文件。

## 导入
三者的“收藏 → +”都能直接选本地 gif/png/jpg，导出文件即可直接添加，所以**不做界面自动化**（要辅助功能权限，易误操作）。
`import` 子命令做的是：格式/后缀体检、分批（默认 50 张/批，硬链接不占空间）、打印该应用的操作路径。

## 微信自动导出

微信 4.x 的收藏清单和表情文件都已加密。默认执行 `export` 时，本工具会自动获取并验证数据库密钥，用户不需要手动查找：

1. 首次使用时，如果微信正在运行，按提示完全退出微信。
2. 工具在 `~/Library/Caches/emojifav/` 创建并临时启动微信副本，通过本地动态库读取副本自身使用的数据库密钥；不会修改 `/Applications/WeChat.app`。
3. 如有需要，在弹出的微信中登录并打开一次表情面板。取得密钥后副本会自动退出，密钥以仅当前用户可读的权限缓存，后续可直接导出。

自动获取功能需要 Xcode Command Line Tools；缺少时可运行 `xcode-select --install` 安装。也可继续手动提供 32 字节密钥：
macOS 若提示访问其他 App 数据，请允许当前使用的终端或 AI 工具访问微信数据；被拒绝时可在“系统设置 → 隐私与安全性 → 完全磁盘访问权限”中调整。

```bash
python3 -m emojifav export --app wechat --wechat-key <64hex> -o out
python3 -m emojifav export --app wechat --wechat-key-file all_keys.json -o out   # 文件里有一堆 key 也行，自动挑能解开的
```

流程：解密库(含 WAL) → 读 `kNonStoreEmoticonTable` → 本地 `Persist/` 解密，不行再按 `cdn_url` 下载 → md5/魔数校验。
解密后的数据库只在内存中使用，不落盘。`--offline` 则不联网。

自动取密钥的实现思路参考了 [export-wechat-emoji](https://github.com/liusheng22/export-wechat-emoji)；符号重绑定使用 BSD 许可的 [fishhook](https://github.com/facebook/fishhook)。[pic2meme](https://github.com/nulastudio/pic2meme) 主要用于图片格式转换，不涉及微信收藏数据库导出。
