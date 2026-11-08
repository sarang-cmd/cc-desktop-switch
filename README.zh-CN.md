# Junction

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ja.md">日本語</a> |
  <a href="docs/CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://github.com/sarang-cmd/junction/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/sarang-cmd/junction?style=social"></a>
  <a href="LICENSE.txt"><img alt="License" src="https://img.shields.io/github/license/sarang-cmd/junction"></a>
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-blue?logo=python"></a>
  <a href="https://github.com/sarang-cmd/junction/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/sarang-cmd/junction/total?label=downloads"></a>
</p>

产品网站：[claude-junction.vercel.app](http://claude-junction.vercel.app)

Junction 是一个面向 Claude Desktop 官方桌面客户端的轻量配置工具。它可以用桌面界面管理 DeepSeek、Kimi、智谱 GLM、阿里云百炼、小米 MiMo 等第三方 Anthropic 兼容 API 提供商，并一键写入 Claude Desktop 的第三方推理配置。

本项目主要面向 Windows 和 macOS 的 Claude Desktop 用户。它和偏 Claude Code / CLI 的 `farion1231/cc-switch` 不同，目标是让普通桌面用户更容易完成 provider 配置、模型映射、健康检查和本机 gateway 兼容。

v1.0.18 起，Claude Desktop 默认会连接 Junction 的本机 gateway：`127.0.0.1`。使用第三方 provider 时，请保持 Junction 在后台运行。Windows 关闭窗口会缩到托盘；macOS 关闭窗口会隐藏应用并保持后台服务可用。

## 界面预览

<table>
  <tr>
    <td width="50%">
      <img src="docs/promo/screenshots/readme-provider-list.png" alt="Provider 管理页面">
    </td>
    <td width="50%">
      <img src="docs/promo/screenshots/readme-deepseek-config.png" alt="添加 DeepSeek Provider">
    </td>
  </tr>
  <tr>
    <td align="center">Provider 管理和快速切换</td>
    <td align="center">选择预设后自动填入 API 地址和推荐模型</td>
  </tr>
  <tr>
    <td width="50%">
      <img src="docs/promo/screenshots/readme-deepseek-options.png" alt="DeepSeek 选项">
    </td>
    <td width="50%">
      <img src="docs/promo/screenshots/readme-qwen-1m-menu.png" alt="通义千问 1M 模型菜单">
    </td>
  </tr>
  <tr>
    <td align="center">DeepSeek 1M 上下文和 Max 思维选项</td>
    <td align="center">通义千问 1M 上下文写入 Claude Desktop</td>
  </tr>
</table>

## 能做什么

- 管理 DeepSeek、Kimi、智谱 GLM、阿里云百炼、小米 MiMo 和自定义第三方 provider。
- 一键写入 Windows / macOS 上的 Claude Desktop 第三方推理配置。
- 通过本机 gateway 统一处理模型映射、协议兼容、额外请求头和上游 API Key。
- Claude Desktop 模型菜单只显示显式映射的 Claude-safe 路由名。
- 未映射模型会明确报错，不再静默回退到内部 Default。
- 支持导入 Anthropic 兼容的 CC-Switch 配置。
- 支持 provider 连通检测、模型可用性检测、SSE 流式转发和自定义上游 HTTP/SOCKS 代理。
- Windows 下重复点击桌面快捷方式只会唤起已有窗口，不会再启动第二个实例。

## 下载

最新版本在 GitHub Releases：

```text
https://github.com/sarang-cmd/junction/releases/latest
```

当前公开的最新版本为 v1.0.27，下载文件仍使用旧的 `CC-Desktop-Switch` 名称以保持兼容。下一版 Junction 构建会使用 `Junction-v<version>` 文件名前缀。

推荐下载：

- `CC-Desktop-Switch-v1.0.27-Windows-Setup.exe`：Windows 安装版
- `CC-Desktop-Switch-v1.0.27-Windows-Portable.zip`：Windows 便携版
- `CC-Desktop-Switch-v1.0.27-macOS-arm64.pkg`：macOS 安装包
- `CC-Desktop-Switch-v1.0.27-macOS-arm64.dmg`：macOS 拖拽安装包

Windows 版暂时没有 Authenticode 代码签名证书，系统可能提示未知发布者。Release 页面提供 `.sha256`、`.sig` 和公钥用于校验完整性。

## 快速开始

1. 下载并打开 Junction。
2. 选择一个 provider 预设，或添加自定义 provider。
3. 填入自己的 API Key。
4. 按需调整模型映射。
5. 点击“一键应用到 Claude 桌面版”。
6. 完整重启 Claude Desktop。

如果桌面窗口无法打开，可以使用备用本地地址：

```text
http://127.0.0.1:18081
```

默认端口：

- 管理界面：`18081`
- 本机 gateway：`18080`

## 模型映射

Claude Desktop 期望看到 Claude 系列模型名，但很多第三方 provider 使用 `deepseek-v4-pro`、`kimi-k2.6`、`glm-5.1`、`qwen3.6-plus` 这类真实上游模型 ID。

Junction 会把真实模型 ID 保存在本机 gateway 内部，只向 Claude Desktop 暴露 Claude-safe route。v1.0.19 起，Claude Desktop 模型菜单只显示用户显式映射的 Claude 槽位；`Default` 只作为工具内部后备项，不会出现在菜单里。

## 本地开发

```powershell
git clone https://github.com/sarang-cmd/junction.git
cd junction
pip install -r requirements.txt
python main.py
```

浏览器调试模式：

```powershell
python main.py --browser
```

验证命令：

```powershell
python -m compileall -q backend main.py tests
python -m unittest discover -s tests -v
node --check frontend/js/api.js
node --check frontend/js/app.js
node --check frontend/js/i18n.js
```

## 常见问题

### Claude Desktop 仍然使用旧 provider

Claude Desktop 会在启动时读取第三方推理配置。应用 provider 后，请完整退出 Claude Desktop 再重新打开。只关闭聊天窗口通常不够。

### Claude Desktop 连接失败

第三方 provider 默认依赖本机 gateway。请先确认 Junction 仍在后台运行，再检查端口占用：

```powershell
netstat -ano | findstr :18081
netstat -ano | findstr :18080
```

### 卸载后 Claude Desktop 仍显示“由组织管理”

当前 Windows 版本会把 Junction 连接保存在 Claude Desktop 的 `configLibrary` 中，并且只修改桌面配置里的 `deploymentMode`，不会新建注册表策略。应用或清除配置时，只会删除带有 `ccds_managed=true` 标记的 Junction 旧版 HKCU 策略值；未知或未标记的值会保留并报告。Junction 不会清理 HKLM。若 Claude 仍显示“由组织管理”，请按提示检查确切策略路径并联系管理员。较旧版本可能需要先在应用中执行一次“清除桌面版配置”以迁移已标记的旧值。

### DeepSeek Max / 推理参数报错

DeepSeek 官方 Anthropic 兼容端点支持 `output_config.effort=max`。Junction 现在只有在上游 400/422 明确指出 thinking/effort 字段并拒绝它时，才显示 Max 兼容提示；鉴权、URL、模型、上下文长度或其他参数错误会保留原始诊断信息。

### Claude Code attribution header

`CLAUDE_CODE_ATTRIBUTION_HEADER=0` 只用于 Claude Code 的 prompt cache 兼容，不是 Claude Desktop 第三方推理配置项，也不能替代本机 gateway。

### GitHub Copilot 订阅账号

Junction 不直接支持把 GitHub Copilot 订阅账号当作 provider API 使用。当前部分 Copilot 客户端已经提供自己的 BYOK / 自定义 provider 设置，可直接配置 OpenAI 兼容或本地端点；这类能力应在 Copilot 内部配置，而不是把订阅账号绕到 Junction。你仍可把独立的 OpenAI Chat 或 Anthropic 兼容端点作为自定义 provider 测试，但应先确认使用规则、API 格式、流式输出和工具调用兼容性。

## Star History

<a href="https://www.star-history.com/#sarang-cmd/junction&Date">
  <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=sarang-cmd/junction&type=Date">
</a>

## 技术栈

- 后端：Python, FastAPI, httpx, uvicorn
- 前端：HTML, CSS, Vanilla JavaScript, Bootstrap 5.3 CDN
- 存储：`~/.cc-desktop-switch/config.json`
- 打包：PyInstaller, NSIS, macOS pkg/dmg scripts

## 免责声明

本项目与 Anthropic、Claude、CC-Switch 以及任何第三方模型服务商没有从属关系。你的上游 API Key 只保存在本机。
