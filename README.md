# emojifav

从 QQ、微信和企业微信中导出自己收藏的表情，并生成可浏览、可定位原文件的本地预览页。

> **仅支持 macOS，暂不支持 Windows。**
>
> 全程只读本机数据，不修改客户端文件、不上传数据，也不会导出聊天缓存、商城表情或系统表情。

## 功能

- 一条命令检测并导出 QQ、微信、企业微信收藏表情
- 自动获取并验证微信 4.x 数据库密钥
- 生成 `preview.html`，集中预览并点击打开本地表情文件
- 生成 `manifest.json`，记录文件来源、尺寸及动图信息
- 导入前检查格式并按批次整理文件

## 快速开始

需要 Python 3.11 或更高版本。

```bash
cd emoji-fav-export
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

emojifav list
emojifav export
```

- `emojifav list`：查看本机检测到的客户端、账号及收藏数据
- `emojifav export`：一次导出 QQ、微信和企业微信
- `emojifav export --app qq`：只导出指定客户端，可选 `qq`、`wechat`、`wecom`
- `emojifav export -o ~/Downloads/emoji`：导出到指定目录

默认输出到项目内的 `exports/YYYYMMDD_HHMMSS/`。

### 使用 AI 工具

也可以使用 Codex、Cursor、WorkBuddy 等 AI 编程工具打开本项目，然后直接告诉 AI：

> 请使用这个项目检测并导出我本机的收藏表情，完成后告诉我导出目录和预览文件的位置。

首次导出微信时，根据提示完全退出微信，工具会自动获取数据库密钥。

## 查看与查找导出结果

每次导出会生成：

```text
exports/YYYYMMDD_HHMMSS/
├── QQ/
├── WeChat/
├── WeCom/
├── manifest.json
└── preview.html
```

导出完成后，终端会高亮显示打开预览页的命令，复制执行即可：

```bash
open "/实际路径/preview.html"
```

预览页会展示全部已导出的表情。点击任一表情可打开对应的本地文件；缩略图下方的文件名与 `QQ`、`WeChat`、`WeCom` 子目录中的文件一一对应，便于快速找到需要转移的表情。

## 各客户端表情存放位置

- **QQ**：`~/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/nt_qq_*/nt_data/Emoji/personal_emoji/Ori/`
- **企业微信**：`~/Library/Containers/com.tencent.WeWorkMac/Data/Documents/Profiles/<账号>/Caches/Emotions/1970-*/`
- **微信 4.x 收藏清单**：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/db_storage/emoticon/emoticon.db`
- **微信 4.x 本地文件**：`~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/business/emoticon/Persist/`

`~/Library` 默认在 Finder 中隐藏。按 `⌘⇧G` 可直接粘贴路径访问，按 `⌘⇧.` 可切换隐藏文件显示状态。

## 微信自动导出

微信 4.x 的收藏清单和表情文件均已加密。执行 `emojifav export` 时，工具会自动获取并验证数据库密钥：

1. 如果微信正在运行，根据提示将其完全退出。
2. 工具在 `~/Library/Caches/emojifav/` 创建并临时启动微信副本，不会修改 `/Applications/WeChat.app`。
3. 如有提示，在副本中登录并打开一次表情面板。密钥获取成功后副本会自动退出，密钥将以仅当前用户可读的权限缓存。

自动获取密钥需要 Xcode Command Line Tools。缺少时可执行：

```bash
xcode-select --install
```

如果 macOS 请求访问其他 App 的数据，请允许当前终端或 AI 工具访问；也可在“系统设置 → 隐私与安全性 → 完全磁盘访问权限”中调整。

<details>
<summary>手动提供微信密钥与高级选项</summary>

```bash
emojifav export --app wechat --wechat-key "YOUR_64_HEX_KEY"
emojifav export --app wechat --wechat-key-file all_keys.json
emojifav export --app wechat --offline
```

- `--wechat-key-file` 支持包含多个密钥的文本或 JSON 文件，工具会自动选择可用密钥。
- `--offline` 禁止联网，仅使用本地 `Persist/` 文件。
- 解密后的数据库只在内存中使用，不会写入磁盘。

</details>

## 导入收藏表情

QQ、微信和企业微信都支持从本地选择 GIF、PNG 或 JPG 文件添加收藏。本项目不操作客户端界面，避免申请辅助功能权限或发生误操作。

`import` 命令会检查文件格式和后缀，使用硬链接按每批 50 张整理文件，并打开 Finder：

```bash
emojifav import --app qq --src exports/YYYYMMDD_HHMMSS/QQ
```

随后在对应客户端中进入“收藏 → +”，选择整理后的文件即可。

## 开发与验证

```bash
python3 -m unittest discover -s tests
```

## 实现参考

微信自动取密钥的实现思路参考了 [export-wechat-emoji](https://github.com/liusheng22/export-wechat-emoji)；符号重绑定使用 BSD 许可的 [fishhook](https://github.com/facebook/fishhook)。[pic2meme](https://github.com/nulastudio/pic2meme) 主要用于图片格式转换，不涉及微信收藏数据库导出。
