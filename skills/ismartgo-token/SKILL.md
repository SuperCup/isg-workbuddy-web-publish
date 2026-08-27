# ismartgo Token Manager

管理 ismartgo 静态托管平台的上传 Token，采用与 PMS MCP 相同的会话管理模式。

## 核心理念

**Cookie 驱动检测 + SSO 自动续期** —— 不靠 URL 模式判断登录状态，而是轮询检测关键会话 Cookie（JSESSIONID）的出现。验证会话时用 `requests.Session + allow_redirects=True`，当 JSESSIONID 过期，SSO cookie（SGSSOSESSION）会自动走重定向签发新 JSESSIONID，**无需弹浏览器重新登录**。

## 工作流程

```
get-token 调用
  ├── Token 缓存有效（7 天内）→ 直接返回，零网络请求
  ├── Token 过期，已有会话 → 用 Session 验证（allow_redirects=True）
  │     ├── JSESSIONID 有效 → 直接用
  │     └── JSESSIONID 过期 → SSO cookie 自动续期签发新 JSESSIONID → 持久化
  │     → 调 upload-token API；脱敏则 PUT 重新生成完整 Token
  └── SSO cookie 也过期 → 打开 Chromium → 用户登录 → 保存 Cookie → 获取 Token
```

**关键改进（2026-07-20）：**
- Token 缓存从 24h 延长到 7 天（token 是独立上传凭证，与 session 解耦）
- 会话验证改用 `allow_redirects=True`，利用 SSO cookie 自动续期 JSESSIONID
- 续期后的 cookie 自动持久化回 session 文件
- `status` 命令改为真实 API 验证（不再误报"有效"）

## 使用方式

```bash
# 获取 Token（每次上传前调用）
python3 scripts/token_manager.py get-token

# 列出 workspace 及 package（选择上传目标前调用，主动展示给用户）
python3 scripts/token_manager.py list-spaces

# 设置 package 访问类型（上传成功后必做：公开PUBLIC/Token访问TOKEN/禁用DISABLED）
python3 scripts/token_manager.py set-access-type --workspace <ws> --package <pkg> \
    --access-type PUBLIC|TOKEN|DISABLED [--access-token <token>] [--title <标题>] [--description <描述>]

# 手动登录（打开浏览器，人工完成）
python3 scripts/token_manager.py login

# 半隐式登录（自动填账号密码，验证码走文件轮询；见下方说明）
python3 scripts/token_manager.py login-auto --username <账号> --password <密码>
python3 scripts/token_manager.py login-auto --username <账号> --password <密码> --headed

# 查看状态（真实验证会话有效性）
python3 scripts/token_manager.py status

# 清除缓存
python3 scripts/token_manager.py clear
```

**重要：`list-spaces` 是主动拉取 workspace/package 列表的唯一入口。** 不要先询问用户有哪些 workspace/package，直接执行该命令获取列表并展示给用户选择。若返回"请求登录"，先执行 `login` 或 `login-auto` 完成登录，再重新执行 `list-spaces`。

**访问类型说明（set-access-type）**：
- `PUBLIC` 公开访问（默认）：任何人可通过链接访问
- `TOKEN` Token 访问：需创建者设置 `--access-token`，外部访问链接必须拼接 `?t=<token>` 才能访问页面（如 `https://agent.ismartgo.com/qingpi/weekly?t=123456`）
- `DISABLED` 禁用：除创建者外其他人都不能访问该链接
- 接口：`PUT {BASE_URL}/api/web/spaces/<workspace>/packages/<package>`，body 含 `code/title/description/accessType/accessToken/tokenExpireAt`（**实测接口仅支持 PUT，POST 返回 errcode 101**）

## 半隐式登录（login-auto）

参考 SmartGo SSO 实测逻辑（登录鉴权处理逻辑参考 v1.0）：`agent.ismartgo.com` 与 PMS/PMP 共用 `op.ismartgo.cn/portalsso`，appkey=`aisites`。登录页为 H5 版（`portalsso/h5/login.html`），表单：

- `#loginname`（账号）+ `#pwd`（密码）+ `#valid_code`（验证码框，**触发式出现**）
- 登录按钮：`div#login`（文本「立即登录」）；自动登录：`.auto-login` div（一周内自动登录）
- 验证码：**4 位数字，发送到邮箱和企业微信**（非短信）；每次点登录/重新发送都会产生新验证码

**执行流程（铁律：登录全程不刷新、不导航，一次走完）：**

1. 启动脚本（headless 默认，`--headed` 可开窗调试）
2. 脚本自动：填账号密码 → 勾一周自动登录 → 点「立即登录」
3. 日志输出 `[READY_FOR_CAPTCHA]`，等待写入验证码文件 `~/.workbuddy/ismartgo_captcha.txt`
4. **Agent 向用户索要验证码（用户查看邮箱/企业微信的最新 4 位数字）→ 写入文件 → 脚本轮询读到后自动提交**
5. 提交后轮询 Cookie（JSESSIONID 出现 + 页面离开登录页）→ 保存完整 Cookie（含 SSO 根凭证）→ 二次验证 API → 缓存 Token

**注意事项：**
- 验证码文件读取后会被消费，失败重试会先清空文件再等新码
- 账号密码**只用于本次登录，不落盘、不写入日志**（命令行参数形式传入，注意本机进程可见性）
- 若验证码被拒（`[CAPTCHA_ERROR]`），脚本会自动点「重新发送」等待新码，最多 3 次
- 登录页结构变化时改用 `login` 手动模式兜底
- 登录日志实时写入 `~/.workbuddy/ismartgo_login.log`

## 存储文件

| 文件 | 用途 | 权限 |
|------|------|------|
| `~/.workbuddy/ismartgo_session.json` | 完整 Cookie 集合（含 SSO cookie） | 600 |
| `~/.workbuddy/ismartgo_token.json` | Token 缓存（7 天有效） | 600 |

## 依赖

- Python 3.8+
- playwright + Chromium
- requests
