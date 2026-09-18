# 安装与使用

## 默认模式

新安装只启用普通模式，接受 Bilibili 与抖音的 HTTPS 链接。首次启动创建空目录和本机 `config.json`，不抓取全站、不自动下载视频、不继承已有 Google/Drive/Gemini 授权，也不会修改系统服务。

普通模式基于 yt-dlp 解析平台链接。支持范围受当前解析器、链接类型和平台权限限制：抖音主要使用单视频链接；Bilibili 视频及解析器支持的合集可以按条数导入。不保证抖音个人主页、搜索结果或任意合集均可导入。

## Windows

1. 安装 Python 3.10 或更新版本（推荐 3.12）以及 Node.js 22.12+（推荐 24 LTS），将两者添加到 PATH。
2. 完整解压发行包到自己的目录，双击 `start-windows.bat`。
3. 首次启动会创建项目虚拟环境，从 PyPI 和 npm 安装依赖，并在本机构建网页。看到地址后，打开 `http://127.0.0.1:8787`。
4. 关闭服务时在终端按 Ctrl+C。

也可在 PowerShell 执行：

```powershell
.\start-windows.bat setup
.\start-windows.bat web
```

批处理仅为这次脚本进程设置 PowerShell 执行策略，不修改系统全局策略。不需要管理员权限运行项目；安装 Python 等依赖是否需要管理员权限取决于安装方式。

## Ubuntu

先安装 Node.js 22.12+（推荐 24 LTS），并确认 `node --version`、`npm --version` 可用；Ubuntu 系统仓库中的 Node.js 版本可能过旧。然后执行：

```bash
sudo apt update
sudo apt install python3 python3-venv ffmpeg rclone
tar -xzf avtool-1.0.0-source.tar.gz
cd avtool-1.0.0-source
./start-ubuntu.sh
```

如果使用 ZIP 解压后脚本没有执行权限，可用 `bash start-ubuntu.sh`。服务仅在前台运行，Ctrl+C 停止，不会自动安装开机启动任务。

Git 仓库及源码包不含网页构建产物。首次启动需要 Node.js 22.12+（推荐 24 LTS）执行 `npm ci --ignore-scripts` 和 `npm run build`。本机生成的 `web/out` 被 Git 忽略；不要上传它或 `node_modules`。

## 导入与播放

在网页点“导入链接”，粘贴自己有权使用的 Bilibili 或抖音链接。也可以修改自动生成的 `config.json`，将链接填进 `sources` 数组，然后运行：

```bash
./start-ubuntu.sh crawler
```

Windows 对应 `start-windows.bat crawler`。每次导入结束后刷新视频库；导入只保存目录信息。命令行单次导入可使用 `run.py import HTTPS_LINK`，其中 `HTTPS_LINK` 替换为自己的实际链接。

`max_items_per_source` 默认 50，可设为 1–200；最多配置 20 个来源。它限制每个来源每轮导入的条数，不是全站抓取。重复导入按站点与视频编号更新，不产生重复目录。若要定时刷新明确配置的来源，可单独运行 `crawler --watch`；间隔由 `crawl_interval_seconds` 控制，最低 300 秒，默认 1800 秒。不要同时运行多个爬虫。

导入任务与源地址解析分别有并发限制和 120 秒超时；Ubuntu 解析子进程另有 512 MiB 地址空间上限。大合集可能因超时仅能导入较少条目，应减小条数或换用单视频链接。热门榜覆盖已成功导入且有播放量的目录；平台不提供播放量时，视频仍在普通片库中。

点击视频先尝试源站播放。Bilibili 的分离音视频会经 FFmpeg 在内存管道中合并为 MP4，不转码、不保存本地原片；实时合并流不支持任意时间点跳转，保存到云盘后可按字节范围播放。没有浏览器兼容格式时会明确报错，请回源站观看。

## FFmpeg 与云存储

直接播放兼容的单文件视频不一定需要 FFmpeg；分离音视频、HLS 合并及相关缓存操作需要 `ffmpeg` 和 `ffprobe`。合并前先检测音频编码；AAC 使用 `aac_adtstoasc` 转换封装所需的头信息，其他编码不会套用 AAC 过滤器，全程保持音视频流复制。Windows 用户安装 FFmpeg 后，把其 `bin` 目录加入 PATH。

云盘缓存是可选功能。默认未启用，在未配置时点击缓存会提示配置，不会使用系统的既有授权。

1. 安装 rclone 并确保 `rclone` 在 PATH。
2. 在项目目录使用自己新建的独立配置授权：

   ```bash
   rclone --config .secrets/rclone.conf config
   ```

3. 用自己在 rclone 中创建的远程名和子目录填入 `config.json`，例如：

   ```json
   {
     "cache_remote": "myremote:videos",
     "rclone_config": ".secrets/rclone.conf"
   }
   ```

   上面仅为需要修改的字段，保留原配置中的其他字段。`myremote` 是示例名字，项目不会替你创建它。需要 OAuth 时，应使用自己的客户端和账号。

4. 重启网页服务。此后缓存按钮、关键词批量缓存和数量选择保存到这份配置指定的云存储。

传输采用源站 → 有界内存管道 → rclone → 云端临时对象 → 完成后发布的流程。默认 1 个缓存 worker，可设为 1–2；媒体不先落到本机，也不要求挂载云盘。目录、SQLite 索引和任务状态保留在本机。完成的视频持续保留，直到用户手动删除；取消或移除任务与删除已完成的视频是不同操作。

## Cookie、特殊模式和诊断

平台要求登录时，将自己合法取得的 Netscape 格式 Cookie 文件放在 `.secrets` 内，并设置 `cookies_file`；默认不会自动读取浏览器 Cookie。不要将文件、授权回调、令牌或完整错误页面提交到 Git。

完整的 agent 提示词、手动加站步骤和适配器字段约定见 [README：特殊模式](README.md#3-特殊模式部署后手动增加站点)。公开包没有成人站点或成人爬虫。

只有明确要求特殊模式时，agent 才应安装用户指定并审查过的本地适配器。可参考 `special_plugin.example.py` 的接口，把实现放到忽略的 `private/provider.py`，再设置：

```json
{
  "mode": "special",
  "special_plugin": "private/provider.py"
}
```

这也是局部配置示例。没有适配器时启动会报错，不会自动下载未知“特殊版本”。扩展会执行本地 Python 代码，需要自行审查；公开包不附带成人站点地址、目录或授权。既有私人环境不应被普通版解压覆盖，应在独立目录验证后自行迁移适配器。

执行 `start-windows.bat doctor` 或 `./start-ubuntu.sh doctor` 检查版本、FFmpeg 和 rclone。诊断不连接云盘，也不输出账号和令牌。

本服务没有面向公网的用户隔离、权限和运维方案。遵守 [使用政策](USAGE_POLICY.md)，禁止公网部署；不提供改变监听地址的参数。若需要私人远程访问，可自行建立仅绑定本机的 SSH 转发，不得创建公众可访问入口。

## 重建与验证

```bash
npm ci --ignore-scripts
npm run build
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/popularity.test.mjs
python audit_release.py
python pack.py
```

测试包含需要本机 FFmpeg、ffprobe 和 rclone 的集成用例，全部使用临时目录及独立空授权配置，不连接真实云盘。`pack.py` 仅收录 `source-manifest.json` 中审核过的源码文件，排除所有构建产物、依赖与运行数据，并输出源码 ZIP、tar.gz、文件校验清单与压缩包 SHA-256。不需要先构建网页才能打源码包。
