# WEB Deployment Agent（小包）

WorkBuddy 专家插件：把前端项目打成符合 ismartgo 静态托管规范的 ZIP，自动处理相对路径与上传 Token，并通过 **对话收集** 配置微信/企微链接预览卡片（Open Graph）。

## 类型

Agent 型（单个 AI 专家）

## 功能

- 检查并修复 Vite / Webpack / CRA / Vue CLI 的 `base` / `publicPath`，保证子目录可访问
- 对话收集分享标题、描述、小图（300×300、≤10KB），注入极简 4 个 OG 标签（无 wecom-cli）
- 上传后 curl 校验 HTML/图片可达性，交付带 `?v=` 的分享链以规避微信缓存
- 通过 `ismartgo-token` Skill 自动获取上传 Token
- **自动更新**：非作者用户使用时，自动从 GitHub 生产分支(main)检查并拉取最新版本，更新后重新进入专家对话即生效（无需重启 WorkBuddy）

## 自动更新（v1.3.0 起）

- **作者 userId（写死）**：`e266ae24-3f86-4af8-9ca6-b9218cd6845f`，作者本人不触发更新
- 非作者用户：仅可获取**生产环境(main)**，不可切换分支；每次使用前自动检查 `.update-version.json` 版本，有更新则下载覆盖本地专家包
- 更新来源：`https://github.com/SuperCup/isg-workbuddy-web-publish` 的 main 分支（zip 下载，免登录、无 API 限流）
- **生效方式**：更新完成后**重新进入本专家对话（新开会话）即生效，无需重启 WorkBuddy**（专家包文件在会话启动时读取）

### 作者发布流程（每次发布必须执行）

1. 修改代码后,更新专家包根目录 `.update-version.json` 的 `version`（如 `1.4.0`）与 `updatedAt`
2. 同步更新 `plugin.json` 的 `version`
3. 提交推送到 `main`（生产）分支,可选同步 `pre`/`test`
4. 打新 tag：`git tag v1.4.0 && git push origin main --tags`

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

- `1.4.2`：防降级保护——本地版本高于远程(疑似通道混用)时拒绝更新,避免体验通道/生产通道误覆盖;update 必须与 check 使用相同 --channel
- `1.4.1`：作者工作流调整(不主动询问发布,发布前二次确认);新增体验通道(--channel pre,白名单用户可从预发布分支更新最新体验版)
- `1.4.0`：新增客户知识采集(knowledge-collector)——面向公司客户(品牌)知识库;登录 ismartgo 自动记录 userid/账号;部门排除(财务/人力/行政);OSS 混淆凭证本机存储;敏感文件打包剔除
- `1.3.3`：更新生效方式优化——无需重启 WorkBuddy,重新进入专家对话即生效;选择交互明确为 ≤4 个用弹窗、>4 个才用会话框列表
- `1.3.2`：新增作者工作流——作者修改专家包默认在 test 分支,推送生产环境(main)前必须经作者确认
- `1.3.1`：workspace/package 选择交互改为数量自适应——超过 4 个时在会话框列出全部选项(编号列表)供用户选择(WorkBuddy 选择弹窗上限 4 项)
- `1.3.0`：新增 auto-update 自动更新机制(非作者用户从 main 分支自动更新);打包命令排除 __pycache__;加入 Git 版本管理
- `1.1.0`：去掉 wecom-cli/Sheet；对话收集分享信息；统一小图规格；强化「已部署仍不出卡」排查；补充公众号 JS-SDK 可选说明
