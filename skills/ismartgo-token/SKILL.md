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
  └── SSO cookie 也过期 → 分步纯HTTP登录(避免主对话长阻塞)
        ├── login-smart --method http --username X --password Y
        │     ├── 退出码 0(成功) → 拿到 Token
        │     └── 退出码 2(NEED_CAPTCHA) → 提示用户提供验证码
        │           └── login-captcha --code XXXX → 拿到 Token
        └── 失败 → 降级 login-smart --method auto(半隐式浏览器) 或 login(手动浏览器)
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

# 纯HTTP登录（默认，无浏览器接口直登；验证码走文件轮询，见下方说明）
python3 scripts/token_manager.py login-smart --method http

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

**Token 来源交互（用户选 TOKEN 时必须询问）**：
- **自动生成**：专家生成随机 Token（建议 6 位数字+字母），生成后**展示给用户确认**，用户认可才使用
- **用户自己设置**：用户提供 Token 值（如 6 位数字），专家回显确认后使用
- 禁止专家擅自决定 Token 值而不经用户确认

# 分步纯HTTP登录（login-smart --method http，默认推荐）

对应精明购 Agent SSO 鉴权操作手册（`backend/app/services/portal_auth.py`），**无浏览器/Playwright，分两步避免主对话长阻塞**。

**为什么分步**：旧方式脚本在验证码步骤同步阻塞 180s × 3 = 9 分钟，导致 WorkBuddy 主对话卡住、验证码过期。新方式：
- 提交账号密码后脚本立即返回（`<3s`），主对话继续；
- 用户提供验证码后调第二步命令（`~3s`），整个交互总耗时 `~6s`。

**第一步：提交账号密码**
```bash
python3 scripts/token_manager.py login-smart --method http --username <账号> --password <密码>
```
- 退出码 0 + 输出 `TOKEN: ...` → 登录成功
- 退出码 2 + `NEED_CAPTCHA: ...` → 进入第二步（脚本已保存中间状态到 `~/.workbuddy/ismartgo_pending_login.json`，有效期 10 分钟）

**第二步：提交验证码（仅 need_captcha 时）**
```bash
python3 scripts/token_manager.py login-captcha --code <4位数字>
```
- 退出码 0 + `TOKEN: ...` → 成功
- 退出码 2 + `NEED_CAPTCHA: ...` → 验证码错误(脚本已自动 resend 新码),请提供**新的** 4 位再试
- 退出码 1 + `ERROR: ...` → 中间状态过期(10分钟),需重新第一步

**底层流程**（第一步内）：
1. `POST op.ismartgo.cn/portalsso/web/login/submit`（表单：`loginname`/`pwd`/`appkey`空/`authcode`空/`keeplogin=0`/`device`）
2. `needcheck=true` → **不阻塞**，保存中间状态（cookies+device）到 `ismartgo_pending_login.json` → 退出码 2 返回 NEED_CAPTCHA
3. `needcheck=false` → 直接进入 OAuth + probe + 保存会话（exit 0）

**device 信任机制（减少验证码）**：登录成功后自动持久化设备号（存 `~/.workbuddy/ismartgo_config.json`），下次登录自动带回 → **大幅降低验证码触发概率**。首次登录无 device 大概率需要验证码，属正常。

**预存凭据**（仅首次需要）：
```bash
python3 scripts/token_manager.py save-credentials -u <账号> -p <密码>    # 默认 --method http
```

**注意事项**：
- 账号密码仅作命令行参数传入，不落盘/不写日志；`save-credentials` 时存到 `ismartgo_config.json`（600 权限）
- `login-smart --method http` 也支持 `--code <4位>`：传 code 时与 submit 一次性走完（适合 Agent 已知验证码时使用，避免分步）

## 半隐式登录（login-auto，浏览器兜底）

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
| `~/.workbuddy/ismartgo_config.json` | 登录偏好 + 凭据 + 信任设备号（device） | 600 |
| `~/.workbuddy/ismartgo_pending_login.json` | 分步登录中间状态(cookies+device)，needcheck 时写入，login-captcha 后清除；TTL 10 分钟 | 600 |

## 依赖

- Python 3.8+
- requests（纯HTTP登录必需）
- playwright + Chromium（仅半隐式/手动浏览器登录时需要，兜底用）
