# WEB Deployment Agent（小包）

WorkBuddy 专家插件：把前端项目打成符合 ismartgo 静态托管规范的 ZIP，自动处理相对路径与上传 Token，并通过 **对话收集** 配置微信/企微链接预览卡片（Open Graph）。

## 类型

Agent 型（单个 AI 专家）

## 功能

- 检查并修复 Vite / Webpack / CRA / Vue CLI 的 `base` / `publicPath`，保证子目录可访问
- 对话收集分享标题、描述、小图（300×300、≤10KB），注入极简 4 个 OG 标签（无 wecom-cli）
- 上传后 curl 校验 HTML/图片可达性，交付带 `?v=` 的分享链以规避微信缓存
- 通过 `ismartgo-token` Skill 自动获取上传 Token

## 使用示例

- 帮我打包这个前端项目并上传到平台
- 帮我配置微信分享卡片
- 页面已部署但微信仍只显示链接，帮我排查

## 头像

头像在 `avatars/`。自定义要求：PNG/JPG，建议 512×512，单张不超过 500KB。

## 安装

将本目录放到专家目录，例如：

```
~/.workbuddy/plugins/marketplaces/my-experts/plugins/web-packaging-assistant/
```

然后按 WorkBuddy 文档执行专家注册（若环境提供 `register_expert.py`）。

## 打包分享

```bash
cd /path/to/Web-Publish
zip -r web-deploy-agent-expert.zip web-packaging-assistant \
  -x "*.DS_Store" -x "**/.git/**" -x "**/__pycache__/**" -x "*.pyc"
```

## 版本

- `1.1.0`：去掉 wecom-cli/Sheet；对话收集分享信息；统一小图规格；强化「已部署仍不出卡」排查；补充公众号 JS-SDK 可选说明
