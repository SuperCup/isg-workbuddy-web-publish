# WEB Deployment Agent（小包）

WorkBuddy 专家插件：把前端项目打成符合 ismartgo 静态托管规范的 ZIP，自动处理相对路径与上传 Token，并在构建前确认访问类型（公开 / Token / 禁用）、上传后自动设置。

## 类型

Agent 型（单个 AI 专家）

## 功能

- 检查并修复 Vite / Webpack / CRA / Vue CLI 的 `base` / `publicPath`，保证子目录可访问
- 构建前确认访问类型（公开 PUBLIC / Token 访问 TOKEN / 禁用 DISABLED），上传后自动设置
- 上传后 curl 校验线上可达性
- 通过 `ismartgo-token` Skill 自动获取上传 Token
- 无可用 workspace 时可直接创建（`create-workspace` 命令，自动携带 `x-admin-token: sso` 头）
- **自动更新**：非作者用户使用时，自动从 GitHub 生产分支(main)检查并拉取最新版本，更新后重新进入专家对话即生效（无需重启 WorkBuddy）

## 自动更新（v1.3.0 起）

- **作者 userId（写死）**：`e266ae24-3f86-4af8-9ca6-b9218cd6845f`，作者本人不触发更新
- 非作者用户：仅可获取**生产环境(main)**，不可切换分支；每次使用前自动检查 `.update-version.json` 版本，有更新则下载覆盖本地专家包
- 更新来源：`https://github.com/SuperCup/isg-workbuddy-web-publish` 的 main 分支（zip 下载，免登录、无 API 限流）
- **生效方式**：更新完成后**重新进入本专家对话（新开会话）即生效，无需重启 WorkBuddy**（专家包文件在会话启动时读取）

### 作者发布流程（每次发布必须执行）

1. 修改代码后,更新专家包根目录 `.update-version.json` 的 `version`（如 `1.4.0`）与 `updatedAt`
2. 同步更新 `plugin.json` 的 `version`
3. 提交推送到 `main`（生产）分支,可选同步 `test`（开发/体验分支）
4. 打新 tag：`git tag v1.4.0 && git push origin main --tags`

## 使用示例

- 帮我打包这个前端项目并上传到平台

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

- `1.5.2`：新增 `create-workspace` 命令——创建 workspace(自动携带 `x-admin-token: sso` 头+登录Cookie+JSON body),成功输出 `WORKSPACE_CREATED`/重复编码等业务错误走 ERROR 分支;已实测通过;Step 3 无可用 workspace 时改走脚本创建(原引导管理后台)
- `1.5.1`：修复 upload token 误缓存(invalid upload token)——登录时不再把 verify 响应 token 当上传 token,以 `/api/web/me/upload-token` 接口 GET/PUT 为准;方案B指令热加载(update 后输出 INSTRUCTION_UPDATED 变更摘要,当前会话可按最新指令继续执行)
- `1.5.0`：删除微信分享卡片配置相关内容(4 大章节+Step 5a+FAQ 等);访问类型确认改为**构建前必做**(Step 5c);保留 `?v=` 通用缓存规避
- `1.4.8`：分支策略调整——**删除 pre 预发布分支,仅保留 main(生产)/test(开发)两分支**;体验通道从 `--channel pre` 改为 `--channel test`(白名单用户直接从 test 开发分支获取体验版);发布流程去除 pre 同步步骤;OSS 主AK 内置专家包(混淆存储,企业内部分发开箱即用,凭证优先级:环境变量 > ~/.workbuddy/oss_cred.blob > 包内置)
- `1.4.6`：访问类型询问时机修复——①从上传后(Step 11)提前到**分享配置后立即询问**(新增 Step 5d,修复"分享配置后无访问类型提醒") ②Token 访问新增来源选择:**自动生成(专家生成并经用户确认)/ 用户自己设置(用户提供)**,禁止专家擅自决定
- `1.4.5`：①采集提醒频率优化——拒绝采集后**每日提醒一次**(原仅首次),直至成功上传刷新状态;成功上传后 7 天间隔 ②上传后**设置访问类型**(公开PUBLIC默认/Token访问TOKEN/禁用DISABLED),新增 `set-access-type` 命令(实测接口仅支持 PUT)
- `1.4.4`：采集流程修复——①打包流程末尾新增强制 Step 12 知识采集提醒(trigger,不可跳过,修复"上传后无采集通知") ②范围列表默认 `--time-range all` 罗列全部空间(修复"只列近一月") ③展示形式固化「交互式详情卡片」(时间筛选/搜索/多选/复制编号,修复"非卡片形式")
- `1.4.3`：采集范围体验升级——①选项改为「工作目录→会话」两级(贴合 WorkBuddy 数据链路) ②展示名用会话标题+真实意图预览(不再是日期) ③支持多选(逗号/空格/区间) ④全量加载+时间筛选(近一周/近一月/全部,默认近一月) ⑤卡片文件名哈希加入内容摘要,杜绝同批次同类型互相覆盖
- `1.4.2`：防降级保护——本地版本高于远程(疑似通道混用)时拒绝更新,避免体验通道/生产通道误覆盖;update 必须与 check 使用相同 --channel
- `1.4.1`：作者工作流调整(不主动询问发布,发布前二次确认);新增体验通道(--channel pre,白名单用户可从预发布分支更新最新体验版)
- `1.4.0`：新增客户知识采集(knowledge-collector)——面向公司客户(品牌)知识库;登录 ismartgo 自动记录 userid/账号;部门排除(财务/人力/行政);OSS 混淆凭证本机存储;敏感文件打包剔除
- `1.3.3`：更新生效方式优化——无需重启 WorkBuddy,重新进入专家对话即生效;选择交互明确为 ≤4 个用弹窗、>4 个才用会话框列表
- `1.3.2`：新增作者工作流——作者修改专家包默认在 test 分支,推送生产环境(main)前必须经作者确认
- `1.3.1`：workspace/package 选择交互改为数量自适应——超过 4 个时在会话框列出全部选项(编号列表)供用户选择(WorkBuddy 选择弹窗上限 4 项)
- `1.3.0`：新增 auto-update 自动更新机制(非作者用户从 main 分支自动更新);打包命令排除 __pycache__;加入 Git 版本管理
- `1.1.0`：去掉 wecom-cli/Sheet；对话收集分享信息；统一小图规格；强化「已部署仍不出卡」排查；补充公众号 JS-SDK 可选说明
