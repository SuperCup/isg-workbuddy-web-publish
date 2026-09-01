#!/usr/bin/env python3
"""
ismartgo Token Manager — 参考 PMS MCP 的会话管理模式。

核心思路（与 PMS MCP 一致）：
1. Playwright 打开 Chromium → 访问受保护接口 → 自动跳转 SSO 登录页
2. 轮询 context.cookies() 检测会话 Cookie 出现 + 页面已离开登录页
3. 保存完整的 Cookie 集合到本地会话文件
4. 后续调用从会话文件加载 Cookie 使用
5. 有效期内无需重复登录（keepLogin 一周）
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

# playwright 采用惰性导入（仅手动/半隐式浏览器登录需要，纯HTTP模式不依赖）

# ─── 常量 ──────────────────────────────────────────────
BASE_URL = "https://agent.ismartgo.com"
PROBE_PATH = "/api/web/me/upload-token"  # 受保护接口，未登录会跳转 SSO
PROBE_URL = f"{BASE_URL}{PROBE_PATH}"
ADMIN_URL = "https://agent.ismartgo.com/admin"
LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # 5 分钟

HOME_DIR = Path.home() / ".workbuddy"
SESSION_FILE = HOME_DIR / "ismartgo_session.json"
TOKEN_CACHE_FILE = HOME_DIR / "ismartgo_token.json"
# 登录偏好 + 凭据（WorkBuddy 本地，600 权限；绝不放入专家包目录，避免分享泄露）
CONFIG_FILE = HOME_DIR / "ismartgo_config.json"

# SSO 统一登录（op.ismartgo.cn/portalsso，appkey=aisites 对应 agent.ismartgo.com）
SSO_OAUTH_TMPL = (
    "https://op.ismartgo.cn/portalsso/oauth?p-appkey=aisites&p-redirect={redirect}"
)
# 纯 HTTP 登录接口（对应 Agent SSO 鉴权手册 portal_auth.py 的实现）
SSO_BASE = "https://op.ismartgo.cn"
SSO_SUBMIT_URL = f"{SSO_BASE}/portalsso/web/login/submit"   # 账号密码登录
SSO_CHECK2_URL = f"{SSO_BASE}/portalsso/web/login/check2"    # 二次验证码
SSO_RESEND_URL = f"{SSO_BASE}/portalsso/web/login/resend"    # 验证码重发(可选)
SSO_LOGIN_REFERER = f"{SSO_BASE}/portalsso/h5/login.html"    # submit 需带 Referer
CAPTCHA_FILE = HOME_DIR / "ismartgo_captcha.txt"   # 验证码轮询文件
LOGIN_LOG_FILE = HOME_DIR / "ismartgo_login.log"   # 登录脚本实时日志
CAPTCHA_WAIT_S = 180   # 等待用户/AI 写入验证码（用户需去邮箱/企微获取）
LOGIN_FINAL_S = 90     # 提交验证码后等待回跳的超时
# 分步登录中间状态（submit 通过但需验证码时保存，避免主对话长阻塞）
PENDING_LOGIN_FILE = HOME_DIR / "ismartgo_pending_login.json"
PENDING_LOGIN_TTL = 600  # 10 分钟（验证码通常 5-10 分钟有效；超时需重新 submit）

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ─── 工具函数 ──────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_json(path: Path, data: dict, chmod: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except Exception:
        pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    if os.name == "nt":
        try:
            os.chmod(path, chmod)
        except Exception:
            pass


def _is_login_page(url: str) -> bool:
    """判断当前 URL 是否还是登录/SSO 页面"""
    u = url.lower()
    return any(kw in u for kw in ("login", "sso", "passport", "oauth", "auth"))


# ─── 会话管理 ──────────────────────────────────────────

class SessionStore:
    def __init__(self, path: Path = SESSION_FILE):
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict | None:
        if not self.exists():
            return None
        try:
            data = load_json(self.path)
            if data and data.get("cookies"):
                return data
            return None
        except Exception:
            return None

    def save(self, data: dict) -> dict:
        payload = {
            **data,
            "savedAt": data.get("savedAt") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        save_json(self.path, payload)
        if os.name == "nt":
            try:
                os.chmod(str(HOME_DIR), 0o700)
            except Exception:
                pass
        return payload

    def clear(self):
        if self.exists():
            self.path.unlink()

    def status(self) -> dict:
        data = self.load()
        if not data:
            return {"ok": False, "message": "未登录，需要完成 SSO 登录。"}
        return {
            "ok": True,
            "savedAt": data.get("savedAt"),
            "cookieCount": len(data.get("cookies", [])),
            "message": "已有有效会话，可在有效期内复用。",
        }


# ─── 登录流程 ──────────────────────────────────────────

def interactive_login() -> dict:
    """
    打开 Chromium 让用户完成 SSO 登录。
    
    流程（与 PMS MCP 一致）：
    1. 启动 Playwright Chromium（headed）
    2. 访问 probe URL → 自动跳转 SSO 登录页
    3. 轮询 cookies，检测会话 Cookie 出现
    4. Cookie 出现后验证页面已离开登录页
    5. 调 probe API 二次确认会话有效
    6. 保存全部 Cookie 到会话文件
    
    返回: {"ok": True, "message": "...", "session": {...}} 或 {"ok": False, "message": "..."}
    """
    store = SessionStore()

    print("\n" + "=" * 55)
    print("  正在启动浏览器...")
    print("  👆 请在弹出的窗口中完成 SSO 登录")
    print("  （含账号密码 + 二次验证码如需要）")
    print("  登录成功后窗口会自动关闭")
    print("=" * 55 + "\n")

    # 尝试启动 Chromium
    try:
        from playwright.sync_api import sync_playwright
        browser = sync_playwright().start().chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
    except ImportError:
        return {
            "ok": False,
            "message": "未安装 playwright。手动登录需要浏览器支持，请执行: "
                       "pip install playwright && playwright install chromium；"
                       "或改用纯HTTP登录: login-smart --method http",
        }
    except Exception as e:
        return {
            "ok": False,
            "message": f"无法启动 Chromium（{e}）。"
                       f"请先执行: npx playwright install chromium",
        }

    try:
        context = browser.new_context(
            viewport={"width": 1100, "height": 800},
            locale="zh-CN",
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.bring_to_front()

        # 导航到 probe URL → 未登录会跳转 SSO
        page.goto(PROBE_URL, wait_until="domcontentloaded", timeout=60000)

        deadline = time.time() + LOGIN_TIMEOUT_MS / 1000
        sid_cookie = None
        all_cookies = []

        # 轮询等待 Cookie
        print("等待登录完成...")
        while time.time() < deadline:
            all_cookies = await_cookies(context)
            
            # 检查是否有 JSESSIONID（agent.ismartgo.com 的会话 Cookie）
            for c in all_cookies:
                if c["name"] == "JSESSIONID" and c.get("value"):
                    sid_cookie = c["value"]
                    break

            if sid_cookie:
                # Cookie 出现了，但还需要确认页面已离开登录页
                current_url = page.url
                if not _is_login_page(current_url):
                    break
                # Cookie 存在但还在登录页 → 可能还在等待验证码

            await_sleep(1)

        if not sid_cookie:
            browser.close()
            return {
                "ok": False,
                "message": f"登录超时（{LOGIN_TIMEOUT_MS // 60000}分钟）："
                           f"未检测到会话 Cookie。请确认已完成所有登录步骤。",
            }

        # 登录成功，最后一次刷新 Cookie
        all_cookies = await_cookies(context)

        # 二次验证：用 Cookie 调 probe API
        import requests as req
        cookie_header = "; ".join(
            f"{c['name']}={c['value']}" for c in all_cookies
        )
        verify_resp = req.get(
            PROBE_URL,
            headers={
                "Cookie": cookie_header,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            allow_redirects=False,
            timeout=15,
        )

        if 300 <= verify_resp.status_code < 400:
            browser.close()
            return {
                "ok": False,
                "message": "Cookie 已出现但 API 仍被重定向，"
                           "可能登录未完全完成，请重试。",
            }

        if verify_resp.status_code != 200:
            browser.close()
            return {
                "ok": False,
                "message": f"会话验证失败 (HTTP {verify_resp.status_code})，请重试。",
            }

        # 提取并缓存 Token（首次调用返回完整 Token）
        # 注意: verify 响应中的 token 不是上传 token，勿缓存为 upload token
        # （upload token 以 /api/web/me/upload-token 接口 GET/PUT 为准，见 get_token_from_session）
        try:
            verify_data = verify_resp.json()
            inner = verify_data.get("result", verify_data)
            _ = inner.get("token") or verify_data.get("token")
        except Exception:
            pass

        # 保存会话
        session = store.save({
            "cookies": [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                }
                for c in all_cookies
            ],
            "baseUrl": BASE_URL,
        })

        browser.close()
        # 登录成功 → 抓取并保存用户信息(userid/部门), 供知识采集器使用
        try:
            _fetch_and_save_user(cookie_header)
        except Exception:
            pass

        print("会话已保存。\n")
        return {
            "ok": True,
            "session": session,
            "message": f"登录成功！会话已保存到 {SESSION_FILE}。"
                       f"有效期内可直接调用接口，无需重复登录。",
        }

    except Exception as e:
        try:
            browser.close()
        except Exception:
            pass
        return {"ok": False, "message": f"登录过程出错: {e}"}


# ─── 登录偏好与凭据（存 WorkBuddy 本地，不进入专家包）──────

def _load_config() -> dict:
    """读取 ~/.workbuddy/ismartgo_config.json（登录方式偏好 + 凭据）"""
    data = load_json(CONFIG_FILE)
    if not isinstance(data, dict):
        return {}
    return data


def _save_config(data: dict):
    save_json(CONFIG_FILE, data)
    _log(f"已记录登录偏好到 {CONFIG_FILE}（仅本机可读写）")


def _mask(username: str) -> str:
    """账号脱敏显示：首字符 + *** + 尾字符（短账号只显示首字符）"""
    if not username:
        return ""
    if len(username) <= 2:
        return username[0] + "***"
    return username[0] + "***" + username[-1]


def save_credentials(username: str, password: str, method: str | None = None):
    """保存登录凭据与方式偏好到本机 config 文件（600 权限，不落日志）"""
    cfg = _load_config()
    cfg["username"] = username
    cfg["password"] = password
    if method:
        cfg["method"] = method
    cfg["savedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_json(CONFIG_FILE, cfg)
    _log(f"凭据已保存（账号 {_mask(username)}，方式 {cfg.get('method', 'auto')}）")


def _preferred_method() -> str | None:
    """返回用户此前选定的登录方式：http / auto / manual / None（未选择）"""
    cfg = _load_config()
    m = cfg.get("method")
    return m if m in ("http", "auto", "manual") else None


def _auto_credentials() -> tuple | None:
    """返回已保存的 (username, password)，未保存返回 None"""
    cfg = _load_config()
    u, p = cfg.get("username", ""), cfg.get("password", "")
    if u and p:
        return u, p
    return None


def print_login_choices():
    """打印登录方式选择提示（会话失效时展示）"""
    print("\n⚠️ 本地无有效 SSO 会话，请选择登录方式：")
    print("  方式 1（纯HTTP） : 无浏览器接口直登。推荐；带信任设备(device)可免验证码")
    print("                   执行: token_manager.py login-smart --method http")
    print("  方式 2（半隐式）: 自动填账号密码，用户只需提供邮箱/企微收到的 4 位验证码")
    print("                   执行: token_manager.py login-smart --method auto")
    print("  方式 3（手动）   : 弹出浏览器窗口，由用户本人输入账号密码+验证码")
    print("                   执行: token_manager.py login")
    print("  三种方式均需先保存凭据: token_manager.py save-credentials -u 账号 -p 密码")
    print("选择后会自动记录偏好。\n", flush=True)


# ─── 半隐式登录（账号密码 + 验证码文件轮询）─────────────
# 参考 SmartGo SSO 实测逻辑（登录鉴权处理逻辑参考 v1.0）：
# - 登录页 op.ismartgo.cn/portalsso/oauth?p-appkey=aisites&p-redirect=...
# - 表单 #loginname + #pwd + #valid_code（触发式出现，4 位数字发邮箱/企微）
# - 铁律：登录全程不刷新、不导航，一次走完；验证码通过文件轮询传递

def _log(msg: str):
    """实时日志：stdout flush + 追加写文件（避免后台运行无输出）"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOGIN_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
    except Exception:
        pass


def _click_button(page, names: list, selectors: list) -> bool:
    """按选择器/按钮文本点击，找不到返回 False"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                return True
        except Exception:
            continue
    for n in names:
        try:
            page.get_by_role("button", name=re.compile(n)).first.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _wait_captcha_file(captcha_file: Path, timeout_s: int) -> str | None:
    """轮询验证码文件，直到读到 4 位数字；返回验证码或 None（超时）"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if captcha_file.exists():
            try:
                code = captcha_file.read_text(encoding="utf-8").strip()
                if re.fullmatch(r"\d{4}", code):
                    return code
            except Exception:
                pass
        time.sleep(1)
    return None


def _page_has_error(page, keywords: list) -> bool:
    """检测登录页是否出现错误提示文本"""
    try:
        body = page.inner_text("body", timeout=3000)
        return any(k in body for k in keywords)
    except Exception:
        return False


def auto_login(
    username: str,
    password: str,
    captcha_file: Path = CAPTCHA_FILE,
    headed: bool = False,
) -> dict:
    """
    半隐式 SSO 登录：自动填账号密码 → 等验证码框 → 轮询验证码文件
    （用户/AI 写入 4 位数字）→ 自动提交 → 轮询 Cookie → 保存会话。

    全程不刷新、不导航；验证码每次点登录/重新发送都会更新，以文件内容为准。
    """
    store = SessionStore()

    # 清掉旧验证码文件，避免误用旧码
    if captcha_file.exists():
        try:
            captcha_file.unlink()
        except Exception:
            pass

    oauth_url = SSO_OAUTH_TMPL.format(redirect=quote(PROBE_URL, safe=""))
    _log(f"SSO 登录页: {oauth_url[:80]}...")

    try:
        from playwright.sync_api import sync_playwright
        browser = sync_playwright().start().chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
    except ImportError:
        return {"ok": False, "message": "未安装 playwright。半隐式登录需要浏览器支持，请执行: pip install playwright && playwright install chromium；或改用纯HTTP登录: login-smart --method http"}
    except Exception as e:
        return {"ok": False, "message": f"无法启动 Chromium（{e}）。请先执行: npx playwright install chromium"}

    try:
        context = browser.new_context(
            viewport={"width": 1100, "height": 800},
            locale="zh-CN",
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        page.goto(oauth_url, wait_until="domcontentloaded", timeout=60000)

        # 若已有会话会直接回跳（不显示登录表单）→ 直接走保存流程
        time.sleep(2)
        login_input = page.locator("#loginname")
        if login_input.count() == 0:
            _log("检测到已登录（直接回跳），跳过表单填写。")
            return _finish_login(context, store, browser)

        # 1. 填账号密码
        login_input.first.fill(username)
        page.fill("#pwd", password)

        # 2. 勾选「一周内自动登录」（H5 页为 .auto-login div 切换，存在则点）
        try:
            auto_login_el = page.locator(".auto-login").first
            if auto_login_el.count() > 0 and auto_login_el.is_visible():
                auto_login_el.click()
        except Exception:
            pass
        try:
            cb = page.locator("#rememberMe, #remember, #autoLogin, input[type=checkbox]").first
            if cb.count() > 0 and not cb.is_checked():
                cb.check()
        except Exception:
            pass

        # 3. 点「立即登录」（H5 页为 div#login）→ 触发验证码框
        if not _click_button(
            page,
            ["登\\s*录", "立即登录"],
            ["#login", "#btn-login", "button[type=submit]", "input[type=submit]"],
        ):
            return {"ok": False, "message": "未找到登录按钮，登录页结构可能变化，请改用 login 手动登录。"}

        # 4. 等验证码输入框出现（触发式）
        try:
            page.wait_for_selector("#valid_code", timeout=30000)
        except Exception:
            err = "点击登录后未出现验证码框（可能已直接登录或页面结构变化）"
            if _page_has_error(page, ["密码", "错误", "不正确"]):
                err = "账号或密码错误，请检查后重试。"
            return {"ok": False, "message": err}

        _log(f"[READY_FOR_CAPTCHA] 验证码框已出现，等待写入文件: {captcha_file}")
        _log("提示：验证码为 4 位数字，已发送到邮箱和企业微信。")

        # 5. 轮询验证码文件（最多 3 次：失败可点「重新发送」取新码）
        for attempt in range(1, 4):
            code = _wait_captcha_file(captcha_file, CAPTCHA_WAIT_S)
            if not code:
                browser.close()
                return {"ok": False, "message": f"等待验证码超时（{CAPTCHA_WAIT_S}s），请重新执行 login-auto。"}

            _log(f"收到验证码（第 {attempt} 次提交），正在提交...")
            page.fill("#valid_code", code)
            _click_button(
                page,
                ["确\\s*定", "确认", "提\\s*交", "验\\s*证"],
                ["#btn-valid", "#valid-btn", "#valid_submit", "#submit", "button[type=submit]", "input[type=submit]"],
            )

            # 6. 轮询 Cookie + 页面离开登录页
            deadline = time.time() + LOGIN_FINAL_S
            sid = None
            while time.time() < deadline:
                all_cookies = await_cookies(context)
                for c in all_cookies:
                    if c["name"] == "JSESSIONID" and c.get("value"):
                        sid = c["value"]
                        break
                if sid and not _is_login_page(page.url):
                    break
                time.sleep(1)

            if sid and not _is_login_page(page.url):
                break  # 登录成功

            # 提交失败：检测错误提示，尝试点「重新发送」取新码
            if _page_has_error(page, ["校验码", "验证码", "不正确", "错误"]):
                _log(f"[CAPTCHA_ERROR] 第 {attempt} 次验证码被拒，点击「重新发送」等待新码...")
                if captcha_file.exists():
                    try:
                        captcha_file.unlink()
                    except Exception:
                        pass
                clicked = _click_button(
                    page,
                    ["重新发送", "重发", "获取验证码", "发\\s*送"],
                    ["#btn-resend", "#resend", "#send_valid_code", "#valid_resend"],
                )
                if not clicked:
                    browser.close()
                    return {"ok": False, "message": "验证码错误且找不到「重新发送」按钮，请重跑 login-auto。"}
                continue
            browser.close()
            return {"ok": False, "message": "提交验证码后未检测到登录成功（页面无错误提示），请重试。"}

        if not sid or _is_login_page(page.url):
            browser.close()
            return {"ok": False, "message": "登录超时：未检测到会话 Cookie。"}

        return _finish_login(context, store, browser)

    except Exception as e:
        try:
            browser.close()
        except Exception:
            pass
        return {"ok": False, "message": f"登录过程出错: {e}"}


# ─── 纯 HTTP 登录（无浏览器，替代 Playwright 半隐式）─────────────
# 对应 Agent SSO 鉴权操作手册(portal_auth.py)：
#   submit(账号密码+device) → needcheck? check2(validcode) → oauth(p-appkey=aisites)
#   → probe 验证 → 保存 cookies + device(信任设备, 下次免验证码)

def _sso_headers(extra: dict | None = None) -> dict:
    """SSO 登录接口通用请求头"""
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": SSO_LOGIN_REFERER,
    }
    if extra:
        h.update(extra)
    return h


def _persist_device(device: str):
    """持久化信任设备号(存 config)，下次 submit 带回可免验证码"""
    if not device:
        return
    cfg = _load_config()
    if cfg.get("device") != device:
        cfg["device"] = device
        _save_config(cfg)
    _log(f"已持久化信任设备 {device}，下次登录将免验证码。")


def _get_device() -> str:
    """读取已持久化的信任设备号"""
    return _load_config().get("device", "") or ""


def _save_pending_login(s, device: str, username: str):
    """保存分步登录的中间状态（cookies + device + username），供 submit_captcha 续接。
    设计目的：避免主对话在 needcheck 时长阻塞 180s × 3。"""
    payload = {
        "cookies": _session_cookies_to_list(s),
        "device": device,
        "username": username,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expiresAt": int(time.time()) + PENDING_LOGIN_TTL,
    }
    save_json(PENDING_LOGIN_FILE, payload)


def _load_pending_login() -> dict | None:
    """读取待续接登录状态；过期或缺失返回 None（并清理过期文件）"""
    data = load_json(PENDING_LOGIN_FILE)
    if not data or not data.get("cookies"):
        return None
    if data.get("expiresAt", 0) < int(time.time()):
        _clear_pending_login()
        return None
    return data


def _clear_pending_login():
    try:
        PENDING_LOGIN_FILE.unlink()
    except Exception:
        pass


def _complete_login(s, device: str) -> dict:
    """OAuth 建立应用会话 + probe 验证 + 保存会话/设备/userinfo（共享给 http_login 与 submit_captcha）"""
    try:
        # OAuth 建立 aisites 应用会话
        oauth_url = SSO_OAUTH_TMPL.format(redirect=quote(PROBE_URL, safe=""))
        _log("OAuth 建立应用会话(p-appkey=aisites)...")
        r3 = s.get(oauth_url, allow_redirects=False, timeout=30)
        loc = r3.headers.get("location")
        if loc:
            s.get(loc, allow_redirects=True, timeout=30)
        # probe 验证
        r4 = s.get(PROBE_URL, allow_redirects=True, timeout=20)
        if r4.status_code != 200:
            return {"ok": False, "message": f"会话验证失败 (HTTP {r4.status_code})，可能 OAuth 未完成。"}
        ctype = r4.headers.get("Content-Type", "")
        text = r4.text.lstrip()
        if "json" not in ctype and not text.startswith(("{", "[")):
            return {"ok": False, "message": "会话验证返回了登录页（未真正登录成功）。"}
        try:
            verify_data = r4.json()
            inner = verify_data.get("result", verify_data)
            # 勿缓存 verify 响应中的 token（非 upload token，见 get_token_from_session）
            _ = inner.get("token") or verify_data.get("token")
        except Exception:
            pass
        # 保存会话
        cookies = _session_cookies_to_list(s)
        session = SessionStore().save({"cookies": cookies, "baseUrl": BASE_URL})
        if device:
            _persist_device(device)
        try:
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            info = _fetch_and_save_user(cookie_header)
            if not (info or {}).get("userid"):
                _log("本地 cookie 未解析出 userid，改用 deliverysv getUserInfo 兜底...")
                _fetch_user_via_deliverysv(s)
        except Exception:
            pass
        _log("登录成功！会话已保存。")
        return {"ok": True, "session": session, "message": "登录成功，会话已保存。有效期内可直接调用接口。"}
    except Exception as e:
        return {"ok": False, "message": f"完成登录步骤出错: {e}"}


def submit_captcha(code: str) -> dict:
    """续接待登录：读取 pending → POST check2(code) → 成功完成 OAuth+probe+保存。
    验证码错：自动 resend 并保留 pending（请用户提供新码后再次调用）。
    """
    import requests as req
    code = (code or "").strip()
    if not re.fullmatch(r"\d{4}", code):
        return {"ok": False, "need_captcha": True, "message": f"验证码格式错误: '{code}'（应为 4 位数字），请提供新的 4 位验证码"}

    pending = _load_pending_login()
    if not pending:
        return {"ok": False, "message": "没有待续接的登录流程（请先执行 login-smart --method http --username <账号> --password <密码>）"}

    s = req.Session()
    s.headers.update(_sso_headers())
    for c in pending["cookies"]:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    device = pending.get("device", "")

    _log("提交验证码(待续接)...")
    r = s.post(SSO_CHECK2_URL, data={"validcode": code}, allow_redirects=False, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {}
    if body.get("errcode") != 0:
        _log(f"[CAPTCHA_ERROR] 验证码被拒: {body.get('errmsg') or body}，自动 resend...")
        try:
            s.post(SSO_RESEND_URL, data={}, allow_redirects=False, timeout=15)
        except Exception:
            pass
        return {"ok": False, "need_captcha": True, "message": f"验证码错误，已自动重发新码，请提供新的 4 位数字"}
    _log("验证码通过。")
    _clear_pending_login()
    return _complete_login(s, device)


def http_login(
    username: str,
    password: str,
    captcha_file: Path = CAPTCHA_FILE,
    device: str = "",
    code: str = "",
) -> dict:
    """
    纯 HTTP SSO 登录（无浏览器）。

    两种调用模式（分步优先，避免主对话长阻塞）：
    1. **分步模式**（推荐）：仅传 username/password。submit 后若 needcheck=true，
       立即保存中间状态并返回 need_captcha=True（不阻塞），主对话可继续。
       用户提供验证码后再调用 submit_captcha(code) 完成登录。
    2. **一次走完**（脚本/Agent 已收到验证码）：传 --code <验证码>。submit → check2(code)
       → OAuth → probe → 保存。会话一气呵成。

    返回:
      {"ok": True, "session": ..., "message": ...}                            登录成功
      {"ok": False, "need_captcha": True, "message": ...}                    需验证码（分步模式）
      {"ok": False, "message": ...}                                          其它错误
    """
    import requests as req

    code = (code or "").strip()
    use_code_first = bool(re.fullmatch(r"\d{4}", code))

    # 清掉旧验证码文件，避免误用旧码
    if captcha_file.exists():
        try:
            captcha_file.unlink()
        except Exception:
            pass

    s = req.Session()
    s.headers.update(_sso_headers())

    try:
        # 1. 账号密码登录
        _log(f"纯HTTP登录: POST submit (device={'有:' + device if device else '无,首次可能需验证码'})")
        r = s.post(
            SSO_SUBMIT_URL,
            data={
                "loginname": username,
                "pwd": password,
                "appkey": "",
                "authcode": "",
                "keeplogin": "0",
                "device": device,
            },
            allow_redirects=False,
            timeout=30,
        )
        try:
            body = r.json()
        except Exception:
            return {"ok": False, "message": f"登录接口返回非 JSON (HTTP {r.status_code})，可能被风控拦截或接口变更: {r.text[:200]}"}
        if body.get("errcode") != 0:
            return {"ok": False, "message": f"登录失败: {body.get('errmsg') or body} (errcode={body.get('errcode')})"}
        result = body.get("result") or {}
        device = result.get("device") or device

        # 2. 二次验证
        if result.get("needcheck"):
            if use_code_first:
                _log("已预传验证码,提交 check2...")
                r2 = s.post(SSO_CHECK2_URL, data={"validcode": code}, allow_redirects=False, timeout=30)
                try:
                    body2 = r2.json()
                except Exception:
                    body2 = {}
                if body2.get("errcode") != 0:
                    _log(f"[CAPTCHA_ERROR] 预传验证码被拒: {body2.get('errmsg') or body2}")
                    return {"ok": False, "need_captcha": True, "message": f"预传验证码错误（{body2.get('errmsg') or 'errcode=' + str(body2.get('errcode'))}），请提供新码后重试"}
                _log("验证码通过。")
                # 继续走 OAuth+probe+保存
            else:
                # 未提供验证码 → 保存中间状态，立即返回（避免主对话长阻塞）
                _save_pending_login(s, device, username)
                _log(f"[NEED_CAPTCHA] 已保存中间登录状态（有效期 {PENDING_LOGIN_TTL//60} 分钟）;验证码已发送到邮箱/企微")
                return {
                    "ok": False,
                    "need_captcha": True,
                    "message": "需要二次验证,4 位验证码已发送到邮箱/企业微信。\n"
                               "请将验证码回复给小包（无需写入文件），小包将自动调用 login-captcha 完成登录，"
                               "避免会话等待验证码超时。",
                }

        # 3-7. OAuth + probe + 保存会话（共享 _complete_login）
        return _complete_login(s, device)

    except Exception as e:
        return {"ok": False, "message": f"登录过程出错: {e}"}


def _fetch_user_via_deliverysv(session) -> dict | None:
    """纯HTTP登录兜底：用同一 SSO 会话对 deliverysv 应用再 OAuth 一次，
    调 getUserInfo 获取 userid/姓名，再经 usertree 关联部门。
    参考 Agent SSO 鉴权手册：同一 SSO 会话可服务多个应用，无需重复输密码。
    失败静默（不阻塞登录）。"""
    try:
        from urllib.parse import unquote
        oauth = (
            f"{SSO_BASE}/portalsso/oauth?p-appkey=deliverysv&p-state="
            f"&p-redirect={quote('https://op.ismartgo.cn/deliverysv/web/index.html', safe='')}"
        )
        r = session.get(oauth, allow_redirects=False, timeout=20)
        loc = r.headers.get("location")
        if loc:
            session.get(loc, allow_redirects=True, timeout=20)
        resp = session.get(f"{SSO_BASE}/deliverysv/api/portal/getUserInfo", timeout=20)
        data = resp.json()
        result = data.get("result") or {}
        userid = result.get("userid")
        if not userid:
            return None
        # 部门关联：usertree 按 userid 匹配
        name, dept = result.get("username", ""), ""
        try:
            tree = session.get(f"{SSO_BASE}/deliverysv/api/portal/usertree", timeout=20).json()
            nodes = tree.get("result", []) if isinstance(tree, dict) else tree

            def _walk(ns):
                nonlocal name, dept
                for n in ns or []:
                    for u in (n.get("users") or []):
                        if str(u.get("id")) == str(userid):
                            name = u.get("name") or name
                            dept = n.get("name", "")
                            return True
                    if _walk(n.get("children") or []):
                        return True
                return False
            _walk(nodes if isinstance(nodes, list) else [])
        except Exception:
            pass
        cfg = _load_config()
        info = {
            "account": cfg.get("username", ""),
            "userid": str(userid),
            "name": name,
            "dept": dept,
            "org": "",
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        user_file = HOME_DIR / "ismartgo_user.json"
        save_json(user_file, info)
        _log(f"已保存登录用户信息(getUserInfo: 账号 {_mask(str(info.get('account','')))}, userid={userid}, 部门={dept or '未知'})")
        return info
    except Exception as e:
        _log(f"getUserInfo 兜底获取用户信息失败(不影响登录): {e}")
        return None


def _fetch_and_save_user(cookie_header: str):
    """登录成功后, 获取当前用户信息并保存到 ~/.workbuddy/ismartgo_user.json,
    供知识采集器作为 member_id(userid) 与部门判定。

    方案(已实测可用):
      1. 从 SSO cookie portalsv_sso_user 解析 userid(格式: userid;过期时间;hash, url编码)
      2. 调组织架构树 usertree, 按 userid 关联出 姓名/部门
    失败不阻塞登录(静默)。"""
    import requests as req
    from urllib.parse import unquote
    try:
        # 1) 从 cookie 解析 userid
        userid = ""
        for seg in cookie_header.split(';'):
            seg = seg.strip()
            if seg.startswith('portalsv_sso_user='):
                raw = seg.split('=', 1)[1].strip()
                try:
                    userid = unquote(raw).split(';')[0].strip()
                except Exception:
                    userid = ""
                break
        # 2) 拉组织架构树, 关联 userid -> (name, dept)
        name, dept = "", ""
        resp = req.get(
            "https://op.ismartgo.cn/deliverysv/api/portal/usertree",
            headers={"Cookie": cookie_header, "User-Agent": USER_AGENT,
                     "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
                     "Referer": "https://op.ismartgo.cn/"},
            timeout=20,
        )
        if resp.status_code == 200:
            try:
                tree = resp.json()
            except Exception:
                tree = {}
            nodes = tree.get("result", []) if isinstance(tree, dict) else tree
            def _walk(ns):
                nonlocal name, dept
                for n in ns or []:
                    for u in (n.get("users") or []):
                        if userid and str(u.get("id")) == str(userid):
                            name = u.get("name", "")
                            dept = n.get("name", "")
                            return True
                    if _walk(n.get("children") or []):
                        return True
                return False
            _walk(nodes if isinstance(nodes, list) else [])
        cfg = _load_config()
        info = {
            "account": cfg.get("username", ""),
            "userid": userid,
            "name": name,
            "dept": dept,
            "org": "",
            "savedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        user_file = HOME_DIR / "ismartgo_user.json"
        save_json(user_file, info)
        _log(f"已保存登录用户信息(账号 {_mask(str(info.get('account','')))}, userid={userid}, 部门={dept or '未知'})")
        return info
    except Exception as e:
        _log(f"获取用户信息失败(不影响登录): {e}")
        return None


def _finish_login(context, store: SessionStore, browser) -> dict:
    """登录成功后的公共收尾：刷新 Cookie → 验证 API → 缓存 Token → 保存会话"""
    all_cookies = await_cookies(context)

    import requests as req
    cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)
    try:
        verify_resp = req.get(
            PROBE_URL,
            headers={"Cookie": cookie_header, "User-Agent": USER_AGENT, "Accept": "application/json"},
            allow_redirects=False,
            timeout=15,
        )
    except Exception as e:
        browser.close()
        return {"ok": False, "message": f"会话验证请求失败: {e}"}

    if 300 <= verify_resp.status_code < 400:
        browser.close()
        return {"ok": False, "message": "Cookie 已出现但 API 仍被重定向，可能登录未完全完成，请重试。"}
    if verify_resp.status_code != 200:
        browser.close()
        return {"ok": False, "message": f"会话验证失败 (HTTP {verify_resp.status_code})，请重试。"}
    # 防假阳性：200 也必须是 JSON（登录页 HTML 会 200）
    ctype = verify_resp.headers.get("Content-Type", "")
    body = verify_resp.text.lstrip()
    if "json" not in ctype and not body.startswith(("{", "[")):
        browser.close()
        return {"ok": False, "message": "会话验证返回了登录页（未真正登录成功），请重试。"}

    try:
        verify_data = verify_resp.json()
        inner = verify_data.get("result", verify_data)
        # 勿缓存 verify 响应中的 token（非 upload token，见 get_token_from_session）
        _ = inner.get("token") or verify_data.get("token")
    except Exception:
        pass

    session = store.save({
        "cookies": [
            {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
            for c in all_cookies
        ],
        "baseUrl": BASE_URL,
    })

    browser.close()
    # 登录成功 → 抓取并保存用户信息(userid/部门), 供知识采集器使用
    try:
        _fetch_and_save_user(cookie_header)
    except Exception:
        pass
    _log(f"登录成功！会话已保存到 {SESSION_FILE}")
    return {"ok": True, "session": session, "message": f"登录成功！会话已保存到 {SESSION_FILE}。有效期内可直接调用接口，无需重复登录。"}


# ─── 智能登录入口（兼容手动 / 半隐式两种方式）───────────

def login_smart(
    method: str | None = None,
    username: str | None = None,
    password: str | None = None,
    captcha_file: Path = CAPTCHA_FILE,
    headed: bool = False,
    code: str = "",
) -> dict:
    """
    智能登录：先判定本地会话；失效则按用户偏好的方式登录。

    - method=None：读偏好（http → 纯HTTP接口直登；auto → 半隐式浏览器；manual → 手动浏览器；
      无偏好 → 默认 http）
    - method=http/auto/manual：记录偏好后按该方式执行
    - http/auto 模式下：显式传入或已保存的凭据均可；仅凭据不存在时提示提供
    - http 模式会自动带信任设备(device)，有则免验证码
    - http 模式下传 code=<4位> 时：与 submit 一次性走完（避免分步时验证码过期）
    """
    store = SessionStore()

    # 1. 会话已有效 → 无需登录
    data = store.load()
    if data:
        ok, _ = _api_verify_session(data.get("cookies", []))
        if ok:
            _log("本地 SSO 会话有效，无需重新登录。")
            return {"ok": True, "message": "会话有效，无需登录。"}

    # 2. 确定登录方式：显式指定 > 历史偏好 > 默认 http
    if method is None:
        method = _preferred_method() or "http"
    if method not in ("http", "auto", "manual"):
        return {"ok": False, "message": f"未知登录方式: {method}（应为 http/auto/manual）"}

    # 记录偏好
    _save_config({**_load_config(), "method": method})

    if method == "manual":
        _log("已记录登录偏好：手动浏览器登录。")
        return interactive_login()

    # http / auto 均需凭据
    if not username or not password:
        creds = _auto_credentials()
        if creds:
            username, password = creds
            _log(f"使用已保存凭据（账号 {_mask(username)}）登录...")
        else:
            return {
                "ok": False,
                "message": f"已选择{'纯HTTP' if method == 'http' else '半隐式'}登录，但本机未保存账号密码。"
                           "请提供账号密码（--username/--password），"
                           "或先执行 save-credentials -u 账号 -p 密码。",
            }

    if method == "http":
        _log("已记录登录偏好：纯HTTP接口直登。")
        result = http_login(username, password, captcha_file=captcha_file, device=_get_device(), code=code)
    else:  # auto：半隐式浏览器
        _log("已记录登录偏好：半隐式浏览器登录。")
        result = auto_login(username, password, captcha_file=captcha_file, headed=headed)

    # 登录成功后持久化凭据（仅当 config 中尚无凭据，且本次为显式传入）
    if result.get("ok") and not _auto_credentials():
        save_credentials(username, password, method=method)
    return result


# ─── Token 获取 ──────────────────────────────────────────

def _build_session(cookies: list):
    """从 cookie 列表构建 requests.Session，支持 SSO 自动续期。

    关键：用 Session 而非裸 Cookie header，这样 allow_redirects=True 时，
    SSO 服务端下发的 Set-Cookie（新 JSESSIONID）会被自动写入 jar，实现续期。
    """
    import requests as req
    s = req.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    for c in cookies:
        s.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ""),
            path=c.get("path", "/"),
        )
    return s


def _session_cookies_to_list(session) -> list:
    """从 requests.Session 提取 cookie 列表（用于持久化）"""
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain or "",
            "path": c.path or "/",
        }
        for c in session.cookies
    ]


def _persist_renewed_cookies(cookies: list) -> bool:
    """若 cookie 有更新（如 JSESSIONID 续期），保存回 session 文件。返回是否有变化。"""
    store = SessionStore()
    old = store.load()
    if not old:
        return False
    old_sig = {f"{c['name']}@{c.get('domain','')}": c["value"] for c in old.get("cookies", [])}
    new_sig = {f"{c['name']}@{c.get('domain','')}": c["value"] for c in cookies}
    if old_sig != new_sig:
        store.save({**old, "cookies": cookies})
        return True
    return False


def _api_verify_session(cookies: list) -> tuple:
    """验证会话是否有效，并利用 SSO cookie 自动续期 JSESSIONID。

    核心改进：用 requests.Session + allow_redirects=True。
    - 旧逻辑：allow_redirects=False，JSESSIONID 过期即返回 302 → 判定失效 → 弹浏览器
    - 新逻辑：allow_redirects=True，JSESSIONID 过期时 SSO cookie（SGSSOSESSION）
      自动走重定向签发新 JSESSIONID → 续期成功 → 无需弹浏览器
    - 防假阳性（SSO 实测教训）：仅 status 200 不够，必须排除
      「302 后 200 的登录页」（final_url 含 portalsso/login/oauth），
      且响应必须是 JSON（登录页 HTML 也可能返回 200）。

    返回: (是否有效, 续期后的 cookie 列表)
    """
    try:
        s = _build_session(cookies)
        resp = s.get(PROBE_URL, allow_redirects=True, timeout=20)
        renewed = _session_cookies_to_list(s)
        if resp.status_code == 200:
            final = resp.url.lower()
            if any(k in final for k in ("portalsso", "/login", "oauth", "passport")):
                return False, cookies  # 200 的登录页 → 假阳性
            ctype = resp.headers.get("Content-Type", "")
            body = resp.text.lstrip()
            if "json" in ctype or body.startswith(("{", "[")):
                return True, renewed
        return False, cookies
    except Exception:
        return False, cookies


def get_token_from_session() -> str | None:
    """
    使用已保存会话获取 Token（支持 SSO 自动续期）。

    流程：
    1. 用 requests.Session 验证会话（allow_redirects=True）
       → JSESSIONID 过期时 SSO cookie 自动续期，无需弹浏览器
    2. 续期后的 cookie 持久化回 session 文件
    3. 优先复用缓存的完整 Token
    4. 无缓存则调 API 获取；脱敏则 PUT 重新生成

    返回: Token 字符串，或 None
    """
    store = SessionStore()
    data = store.load()
    if not data:
        return None

    cookies = data.get("cookies", [])
    ok, renewed_cookies = _api_verify_session(cookies)
    if not ok:
        return None

    # 续期后的 cookie（可能含新 JSESSIONID）持久化
    if _persist_renewed_cookies(renewed_cookies):
        print("会话已自动续期。")

    # 优先复用缓存 Token
    cache = load_json(TOKEN_CACHE_FILE)
    cached_token = cache.get("token") if cache else None
    if cached_token:
        return cached_token

    # 用续期后的 session 获取 Token
    s = _build_session(renewed_cookies)
    try:
        resp = s.get(PROBE_URL, allow_redirects=True, timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            inner = result.get("result", result)
            token = inner.get("token") or result.get("token")
            if token and not token.startswith("***"):
                _cache_token(token)
                return token

            # Token 已脱敏 → PUT 重新生成完整 Token
            masked = inner.get("tokenMasked", "")
            if masked:
                print(f"\n当前 Token（脱敏）: {masked}")
                print("尝试重新生成 Token...")
                try:
                    regen_resp = s.put(PROBE_URL, json={}, timeout=15)
                    if regen_resp.status_code == 200:
                        regen_data = regen_resp.json()
                        regen_inner = regen_data.get("result", regen_data)
                        new_token = regen_inner.get("token")
                        if new_token and not new_token.startswith("***"):
                            _cache_token(new_token)
                            print("Token 已重新生成。")
                            return new_token
                except Exception:
                    pass
                # PUT 也失败 → 需要用户手动提供
                print("自动重新生成失败，请手动提供完整 Token：")
                full_token = input("Token: ").strip()
                if full_token:
                    _cache_token(full_token)
                    return full_token

        return None
    except Exception:
        return None


def _cache_token(token: str):
    save_json(TOKEN_CACHE_FILE, {
        "token": token,
        "cachedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expiresAt": int(time.time() + 7 * 86400),  # 7 天（token 是独立上传凭证，与 session 解耦）
    })


# ─── 主入口 ────────────────────────────────────────────

def get_or_refresh_token() -> str:
    """
    获取有效的上传 Token。优先级：
    1. 缓存 Token（24h 内有效）
    2. 已保存会话 → 调接口获取
    3. 打开浏览器登录
    """
    # Step 1: 缓存
    cache = load_json(TOKEN_CACHE_FILE)
    if cache:
        token = cache.get("token")
        expires = cache.get("expiresAt", 0)
        if token and time.time() < expires - 300:
            return token

    # Step 2: 会话 Cookie
    print("Token 已过期，尝试用已有会话获取...")
    token = get_token_from_session()
    if token:
        print("Token 已刷新。")
        return token

    # Step 3: 智能登录（按偏好：auto 半隐式 / manual 手动浏览器 / 未选择则提示）
    print("会话已过期，需要重新登录。")
    result = login_smart()
    if not result["ok"]:
        print(f"ERROR: {result['message']}", file=sys.stderr)
        sys.exit(1)

    token = get_token_from_session()
    if not token:
        print("ERROR: 登录成功但获取 Token 失败", file=sys.stderr)
        sys.exit(1)

    return token


def clear_cache():
    for p in [TOKEN_CACHE_FILE, SESSION_FILE, CONFIG_FILE, CAPTCHA_FILE]:
        if p.exists():
            p.unlink()
    print("已清除所有缓存（含登录凭据与偏好）。")


def list_spaces() -> dict:
    """
    获取当前用户可访问的 workspace 及 package 列表。

    流程：
    1. 加载会话 Cookie
    2. 用 requests.Session 验证会话（SSO 自动续期 JSESSIONID）
    3. 调用 /api/web/spaces 获取列表
    4. 解析并输出 workspace + package 结构

    返回: {"ok": True, "spaces": [...]} 或 {"ok": False, "message": "..."}
    """
    store = SessionStore()
    data = store.load()
    if not data:
        return {"ok": False, "message": "未登录（无会话文件），请先执行 login 完成 SSO 登录。"}

    cookies = data.get("cookies", [])
    ok, renewed = _api_verify_session(cookies)
    if not ok:
        return {
            "ok": False,
            "message": "会话已失效（SSO cookie 过期），请先执行 login 完成一次浏览器登录。",
        }
    _persist_renewed_cookies(renewed)

    s = _build_session(renewed)
    s.headers.update({
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        resp = s.get(f"{BASE_URL}/api/web/spaces", allow_redirects=True, timeout=30)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "message": f"获取空间列表失败: {e}"}

    if data.get("errcode") == 106:
        return {
            "ok": False,
            "message": "请求登录：空间列表接口需要有效会话，请先执行 login 完成浏览器登录后重试。",
        }

    # 兼容多种返回结构：errcode=0 + result / data / 直接列表
    payload = data.get("result", data)
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], list):
        payload = payload["data"]
    if isinstance(payload, dict) and "spaces" in payload:
        payload = payload["spaces"]
    if isinstance(payload, dict) and "list" in payload:
        payload = payload["list"]
    if not isinstance(payload, list):
        return {
            "ok": True,
            "message": "接口返回结构未识别，请参考原始 JSON：",
            "raw": json.dumps(data, ensure_ascii=False)[:3000],
        }

    spaces = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        # workspace 编码与名称字段兼容
        code = item.get("code") or item.get("workspace") or item.get("name") or item.get("key") or ""
        name = item.get("name") or item.get("title") or item.get("displayName") or code
        # package 列表字段兼容
        packages_raw = item.get("packages") or item.get("packageList") or item.get("apps") or []
        packages = []
        if isinstance(packages_raw, list):
            for pkg in packages_raw:
                if isinstance(pkg, dict):
                    packages.append(
                        pkg.get("code") or pkg.get("package") or pkg.get("name") or pkg.get("key") or ""
                    )
                else:
                    packages.append(str(pkg))
        spaces.append({"workspace": str(code), "name": str(name), "packages": packages})

    if not spaces:
        return {"ok": True, "message": "当前账号下没有可用 workspace。", "spaces": []}
    return {"ok": True, "spaces": spaces}


def set_access_type(workspace: str, package: str, access_type: str,
                    access_token: str = "", title: str = "", description: str = "",
                    token_expire_at: str = "") -> dict:
    """
    设置 workspace 下 package 的访问类型(公开/Token访问/禁用)。

    access_type: PUBLIC(公开,默认) | TOKEN(Token访问) | DISABLED(禁用)
    - TOKEN 模式: 需提供 access_token(访问口令), 外部链接需拼接 ?t=<token> 才能访问
    - DISABLED 模式: 除创建者外其他人均无法访问

    接口: PUT/POST {BASE_URL}/api/web/spaces/<workspace>/packages/<package>
    body: {code, title, description, accessType, accessToken, tokenExpireAt}

    返回: {"ok": True, "message": "..."} 或 {"ok": False, "message": "..."}
    """
    access_type = (access_type or "PUBLIC").upper()
    if access_type not in ("PUBLIC", "TOKEN", "DISABLED"):
        return {"ok": False, "message": f"access_type 非法: {access_type}(应为 PUBLIC/TOKEN/DISABLED)"}
    if access_type == "TOKEN" and not access_token:
        return {"ok": False, "message": "TOKEN 访问模式必须提供 access_token(访问口令)"}

    store = SessionStore()
    data = store.load()
    if not data:
        return {"ok": False, "message": "未登录（无会话文件），请先执行 login 完成 SSO 登录。"}

    cookies = data.get("cookies", [])
    ok, renewed = _api_verify_session(cookies)
    if not ok:
        return {"ok": False, "message": "会话已失效（SSO cookie 过期），请先执行 login 完成一次浏览器登录。"}
    _persist_renewed_cookies(renewed)

    s = _build_session(renewed)
    s.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    body = {
        "code": package,
        "title": title,
        "description": description,
        "accessType": access_type,
        "accessToken": access_token,
        "tokenExpireAt": token_expire_at,
    }
    url = f"{BASE_URL}/api/web/spaces/{workspace}/packages/{package}"
    try:
        # 接口仅支持 PUT(实测 POST 返回 errcode 101 not supported), 先 PUT; PUT 失败再尝试 POST
        resp = s.put(url, json=body, allow_redirects=True, timeout=30)
        if resp.status_code in (405, 404):
            resp = s.post(url, json=body, allow_redirects=True, timeout=30)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "message": f"设置访问类型失败: {e}"}

    if data.get("errcode") == 106:
        return {"ok": False, "message": "请求登录：接口需要有效会话，请先执行 login 完成浏览器登录后重试。"}
    if data.get("errcode") == 101:
        # POST not supported → 尝试 PUT 是正确路径; 若 PUT 也返回 101, 说明接口不接受
        return {"ok": False, "message": f"接口不支持该请求方式: {json.dumps(data, ensure_ascii=False)[:300]}"}
    if data.get("errcode") not in (None, 0) and data.get("errcode") != 200:
        return {"ok": False, "message": f"设置失败: {json.dumps(data, ensure_ascii=False)[:500]}"}
    return {"ok": True, "message": f"访问类型已设置为 {access_type} (workspace={workspace}, package={package})",
            "response": data}


def create_workspace(code: str, name: str, description: str = "") -> dict:
    """
    创建 workspace（空间）。

    接口: POST {BASE_URL}/api/web/spaces
    请求头: x-admin-token: sso + 登录 Cookie(JSESSIONID) + Content-Type: application/json
    body: {code, name, description}

    返回: {"ok": True, "message": "..."} 或 {"ok": False, "message": "..."}
    """
    store = SessionStore()
    data = store.load()
    if not data:
        return {"ok": False, "message": "未登录（无会话文件），请先执行 login 完成 SSO 登录。"}

    cookies = data.get("cookies", [])
    ok, renewed = _api_verify_session(cookies)
    if not ok:
        return {"ok": False, "message": "会话已失效（SSO cookie 过期），请先执行 login 完成一次浏览器登录。"}
    _persist_renewed_cookies(renewed)

    s = _build_session(renewed)
    s.headers.update({
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        # 创建空间需管理员头（x-admin-token: sso）
        "x-admin-token": "sso",
    })
    body = {
        "code": code,
        "name": name,
        "description": description or "",
    }
    try:
        resp = s.post(f"{BASE_URL}/api/web/spaces", json=body, allow_redirects=True, timeout=30)
        data = resp.json()
    except Exception as e:
        return {"ok": False, "message": f"创建 workspace 失败: {e}"}

    if data.get("errcode") == 106:
        return {"ok": False, "message": "请求登录：接口需要有效会话，请先执行 login 完成浏览器登录后重试。"}
    if data.get("errcode") not in (None, 0) and data.get("errcode") != 200:
        # 重名/编码重复等业务错误原样返回，便于上层识别
        return {"ok": False, "message": f"创建失败: {json.dumps(data, ensure_ascii=False)[:500]}"}
    return {"ok": True, "message": f"workspace 已创建: {code} ({name})", "response": data}


# ─── Playwright 辅助 ───────────────────────────────────

def await_cookies(context) -> list:
    """获取当前所有 Cookie（同步包装）"""
    return context.cookies()


def await_sleep(seconds: float):
    time.sleep(seconds)


# ─── CLI ───────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ismartgo Token Manager")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("get-token", help="获取上传 Token")
    sub.add_parser("login", help="打开浏览器登录")
    parser_login_auto = sub.add_parser(
        "login-auto",
        help="半隐式登录：自动填账号密码，验证码从文件轮询读取",
    )
    parser_login_auto.add_argument("--username", required=True, help="SSO 账号")
    parser_login_auto.add_argument("--password", required=True, help="SSO 密码")
    parser_login_auto.add_argument(
        "--captcha-file",
        default=str(CAPTCHA_FILE),
        help=f"验证码轮询文件（默认 {CAPTCHA_FILE}）",
    )
    parser_login_auto.add_argument(
        "--headed", action="store_true", help="显示浏览器窗口（默认 headless）"
    )
    parser_login_smart = sub.add_parser(
        "login-smart",
        help="智能登录：判定会话 → 按已记录偏好登录；默认纯HTTP接口直登",
    )
    parser_login_smart.add_argument(
        "--method", choices=["http", "auto", "manual"],
        help="指定登录方式并记录偏好(http=纯HTTP接口直登/auto=半隐式浏览器/manual=手动浏览器)"
    )
    parser_login_smart.add_argument("--username", help="SSO 账号（半隐式）")
    parser_login_smart.add_argument("--password", help="SSO 密码（半隐式）")
    parser_login_smart.add_argument(
        "--code", default="",
        help="4 位验证码(http 模式下传此参数可与 submit 一次性走完，避免分步时验证码过期)"
    )
    parser_login_smart.add_argument(
        "--captcha-file", default=str(CAPTCHA_FILE), help="验证码轮询文件"
    )
    parser_login_smart.add_argument(
        "--headed", action="store_true", help="显示浏览器窗口（默认 headless）"
    )
    parser_login_captcha = sub.add_parser(
        "login-captcha",
        help="分步登录第二步: 提交 4 位验证码续接登录（用于 login-smart 返回 need_captcha=True 时）",
    )
    parser_login_captcha.add_argument(
        "--code", required=True, help="邮箱/企微收到的 4 位数字验证码"
    )
    parser_save_creds = sub.add_parser(
        "save-credentials", help="保存 SSO 凭据到本机（600 权限，不进专家包）"
    )
    parser_save_creds.add_argument("-u", "--username", required=True, help="SSO 账号")
    parser_save_creds.add_argument("-p", "--password", required=True, help="SSO 密码")
    parser_save_creds.add_argument(
        "--method", choices=["http", "auto", "manual"], default="http",
        help="登录方式偏好（默认 http=纯HTTP接口直登）"
    )
    sub.add_parser("pref", help="查看登录偏好与凭据状态（脱敏）")
    sub.add_parser("clear", help="清除所有缓存")
    sub.add_parser("status", help="查看状态")
    sub.add_parser("list-spaces", help="列出当前账号可访问的 workspace 及 package")
    parser_space = sub.add_parser(
        "create-workspace", help="创建 workspace（请求头含 x-admin-token: sso）"
    )
    parser_space.add_argument("--code", required=True, help="workspace 编码(如 AI-Create-Test)")
    parser_space.add_argument("--name", required=True, help="workspace 名称")
    parser_space.add_argument("--description", default="", help="描述(可选)")
    parser_access = sub.add_parser(
        "set-access-type", help="设置 package 访问类型(公开PUBLIC/Token访问TOKEN/禁用DISABLED)"
    )
    parser_access.add_argument("--workspace", required=True, help="workspace 编码(如 qingpi)")
    parser_access.add_argument("--package", required=True, help="package 编码(如 weekly)")
    parser_access.add_argument(
        "--access-type", choices=["PUBLIC", "TOKEN", "DISABLED"], default="PUBLIC",
        help="访问类型(默认 PUBLIC)"
    )
    parser_access.add_argument("--access-token", default="", help="Token 访问模式的访问口令(如 123456)")
    parser_access.add_argument("--title", default="", help="页面标题(可选)")
    parser_access.add_argument("--description", default="", help="页面描述(可选)")
    parser_access.add_argument("--token-expire-at", default="", help="Token 过期时间(可选, ISO 格式)")

    args = parser.parse_args()

    if args.cmd == "get-token":
        token = get_or_refresh_token()
        print(f"TOKEN: {token}")

    elif args.cmd == "login":
        result = interactive_login()
        if result["ok"]:
            token = get_token_from_session()
            if token:
                print(f"TOKEN: {token}")
            else:
                print("登录成功但 Token 获取失败")
        else:
            print(f"ERROR: {result['message']}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "login-auto":
        result = auto_login(
            username=args.username,
            password=args.password,
            captcha_file=Path(args.captcha_file),
            headed=args.headed,
        )
        if result["ok"]:
            token = get_token_from_session()
            if token:
                print(f"TOKEN: {token}")
            else:
                print("登录成功但 Token 获取失败")
        else:
            print(f"ERROR: {result['message']}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "login-smart":
        result = login_smart(
            method=args.method,
            username=args.username,
            password=args.password,
            captcha_file=Path(args.captcha_file),
            headed=args.headed,
            code=args.code,
        )
        if result.get("need_captcha"):
            # 分步模式：need_captcha=True → 不退出,让上层继续(主对话不卡)
            # 打印到 stderr 让 Agent 易于识别;非零退出以触发脚本结果识别
            print(f"NEED_CAPTCHA: {result['message']}", file=sys.stderr)
            sys.exit(2)
        if result["ok"]:
            token = get_token_from_session()
            if token:
                print(f"TOKEN: {token}")
            else:
                print("登录成功但 Token 获取失败")
        else:
            print(f"ERROR: {result['message']}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "login-captcha":
        result = submit_captcha(args.code)
        if result.get("need_captcha"):
            # 验证码错并已 resend → 需要新码,同样不阻塞主对话
            print(f"NEED_CAPTCHA: {result['message']}", file=sys.stderr)
            sys.exit(2)
        if result["ok"]:
            token = get_token_from_session()
            if token:
                print(f"TOKEN: {token}")
            else:
                print("登录成功但 Token 获取失败")
        else:
            print(f"ERROR: {result['message']}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "save-credentials":
        save_credentials(args.username, args.password, method=args.method)
        print(f"凭据已保存。后续执行 login-smart 将自动使用 {args.method} 方式登录。")

    elif args.cmd == "pref":
        cfg = _load_config()
        print("=== 登录偏好状态 ===")
        method = _preferred_method()
        print(f"登录方式偏好: {method if method else '未选择'}")
        if cfg.get("username"):
            print(f"已保存凭据账号: {_mask(cfg['username'])}")
        else:
            print("已保存凭据账号: 无")
        print(f"配置文件: {CONFIG_FILE}")

    elif args.cmd == "clear":
        clear_cache()

    elif args.cmd == "list-spaces":
        result = list_spaces()
        if not result.get("ok"):
            print(f"ERROR: {result['message']}", file=sys.stderr)
            # 会话失效/未登录 → 提示选择登录方式
            if "登录" in result["message"] or "会话" in result["message"]:
                print_login_choices()
            sys.exit(1)
        spaces = result.get("spaces")
        if spaces is None:
            print(result.get("message", ""))
            if result.get("raw"):
                print(result["raw"])
            sys.exit(0)
        print("=== 可访问的 workspace 及 package ===")
        for sp in spaces:
            if sp["packages"]:
                print(f"workspace: {sp['workspace']} ({sp['name']})")
                for pkg in sp["packages"]:
                    print(f"  package: {pkg}")
            else:
                print(f"workspace: {sp['workspace']} ({sp['name']}) [无 package]")
        if not spaces:
            print(result.get("message", "当前账号下没有可用 workspace。"))

    elif args.cmd == "create-workspace":
        result = create_workspace(
            code=args.code,
            name=args.name,
            description=args.description,
        )
        if not result.get("ok"):
            print(f"ERROR: {result['message']}", file=sys.stderr)
            if "登录" in result["message"] or "会话" in result["message"]:
                print_login_choices()
            sys.exit(1)
        print(f"WORKSPACE_CREATED: {args.code}")
        print(result["message"])

    elif args.cmd == "set-access-type":
        result = set_access_type(
            workspace=args.workspace,
            package=args.package,
            access_type=args.access_type,
            access_token=args.access_token,
            title=args.title,
            description=args.description,
            token_expire_at=args.token_expire_at,
        )
        if not result.get("ok"):
            print(f"ERROR: {result['message']}", file=sys.stderr)
            if "登录" in result["message"] or "会话" in result["message"]:
                print_login_choices()
            sys.exit(1)
        print(result["message"])

    elif args.cmd == "status":
        store = SessionStore()
        print("=== ismartgo 会话状态 ===")

        # 真实验证会话（含 SSO 自动续期），而非仅查文件是否存在
        data = store.load()
        if not data:
            print("会话: 未登录（无会话文件）")
        else:
            cookies = data.get("cookies", [])
            ok, renewed = _api_verify_session(cookies)
            if ok:
                renewed_flag = _persist_renewed_cookies(renewed)
                if renewed_flag:
                    print("会话: 有效（已自动续期，无需重新登录）")
                else:
                    print("会话: 有效")
                print(f"  保存时间: {data.get('savedAt', '未知')}")
                print(f"  Cookie 数: {len(cookies)}")
            else:
                print("会话: 失效（SSO cookie 也已过期，需重新登录）")
                print(f"  上次保存: {data.get('savedAt', '未知')}")

        cache = load_json(TOKEN_CACHE_FILE)
        if cache:
            remaining = max(0, cache.get("expiresAt", 0) - int(time.time()))
            if remaining > 0:
                print(f"Token 缓存: 有效（剩余 {remaining/3600:.1f}h）")
            else:
                print("Token 缓存: 已过期")
        else:
            print("Token 缓存: 无")

    else:
        parser.print_help()
