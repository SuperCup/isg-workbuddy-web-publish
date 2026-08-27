#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WEB部署Agent(小包)专家包 — 自动更新脚本
================================================
功能:非作者用户使用专家包时,检查 GitHub 生产分支(main)是否有更新,
有则自动下载最新专家包并覆盖本地安装目录。

硬性规则(写死,不可配置):
- AUTHOR_USER_ID:作者(包维护者)的 WorkBuddy userId,作者本人不触发更新
- BRANCH:非作者用户仅可获取生产环境 main,不接受任何分支参数,不可切换

机制(纯 zip,不依赖 GitHub API,无匿名限流问题):
1. 下载 https://github.com/<REPO>/archive/refs/heads/main.zip(公开仓库免登录)
2. 解压到临时目录,读取新包根目录 .update-version.json 的 version
3. 与本地已安装的 .update-version.json 的 version 比较,不同则覆盖更新

用法:
    python3 auto_update.py check   # 检查更新,输出状态
    python3 auto_update.py update  # 检查并执行更新

输出格式(供 AI/用户解析):
    AUTHOR_MODE            : 作者本人,不更新(本地即源码仓库)
    UP_TO_DATE             : 已是最新
    UPDATE_AVAILABLE       : 有新版本,执行 update 可更新
    UPDATED                : 更新完成(重新进入专家对话即生效,无需重启)
    NO_USER_ID             : 无法识别当前用户,保守不更新
    NOT_AUTHOR_NO_BRANCH   : 非作者尝试切换分支被拒绝(理论上不会发生)
    ERROR:<原因>           : 出错(网络/解析等)
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# ─── 写死的规则 ──────────────────────────────────────────
AUTHOR_USER_ID = "e266ae24-3f86-4af8-9ca6-b9218cd6845f"  # 作者 WorkBuddy userId
BRANCH = "main"  # 非作者仅可获取生产环境,写死,不接受分支参数
REPO = "SuperCup/isg-workbuddy-web-publish"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

# 本地版本记录文件名(放在专家包根目录,随包分发/覆盖)
VERSION_FILE = ".update-version.json"
# 覆盖时排除(用户本地数据与运行产物,不触碰)
EXCLUDE_NAMES = {".git", "__pycache__", ".git-credentials"}


def log(msg: str):
    print(msg, flush=True)


# 专家包根标记文件:位于专家包根目录,更新前必须校验存在,
# 防止脚本被放置/解析到异常路径时误删用户文件(安全防呆)
ROOT_MARKER = ".expert-root-marker"


def get_expert_root(require_marker: bool = False) -> Path:
    """定位专家包根目录(本脚本位于 <root>/skills/auto-update/scripts/)

    require_marker=True 时校验根目录存在 .expert-root-marker,
    不存在则抛异常拒绝执行(防误删)。
    """
    root = Path(__file__).resolve().parents[3]
    if require_marker and not (root / ROOT_MARKER).exists():
        raise RuntimeError(
            f"专家包根目录校验失败(缺少 {ROOT_MARKER}): {root} — 已拒绝执行更新,请检查安装完整性"
        )
    return root


def get_current_user_id() -> str | None:
    """多来源获取当前 WorkBuddy 用户 userId"""
    # 1. 环境变量(调试/注入)
    for k in ("WORKBUDDY_USER_ID", "CODEBUDDY_USER_ID"):
        v = os.environ.get(k)
        if v:
            return v
    # 2. ~/.workbuddy/workbuddy.db sessions 表最近活跃会话
    db = Path.home() / ".workbuddy" / "workbuddy.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = conn.execute(
                "SELECT user_id FROM sessions "
                "WHERE deleted_at IS NULL ORDER BY last_activity_at DESC LIMIT 1"
            ).fetchone()
            conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    # 3. ~/.workbuddy/connectors/ 下的用户目录名
    cp = Path.home() / ".workbuddy" / "connectors"
    if cp.is_dir():
        dirs = [d.name for d in cp.iterdir() if d.is_dir() and len(d.name) >= 16]
        if len(dirs) == 1:
            return dirs[0]
    return None


def get_local_version() -> dict:
    """读取本地已安装版本记录"""
    vf = get_expert_root() / VERSION_FILE
    if vf.exists():
        try:
            return json.loads(vf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def download_zip(dest: Path) -> Path:
    """下载 main 分支 zip 到临时目录,返回 zip 路径(带重试)"""
    zip_path = dest / "update.zip"
    last_err = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "web-packaging-assistant-auto-update"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(zip_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
            return zip_path
        except Exception as e:
            last_err = e
            if attempt < 3:
                time.sleep(3 * attempt)
    raise last_err


def fetch_remote_package() -> tuple:
    """
    下载并解压远程 main 分支包,返回 (src_dir, remote_version_dict)
    src_dir:临时目录中的新包根(调用方负责清理临时目录)
    """
    tmp = Path(tempfile.mkdtemp(prefix="wp-update-"))
    try:
        zip_path = download_zip(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        src_dir = next(p for p in tmp.iterdir() if p.is_dir())
        ver = {}
        vf = src_dir / VERSION_FILE
        if vf.exists():
            try:
                ver = json.loads(vf.read_text(encoding="utf-8"))
            except Exception:
                pass
        return src_dir, ver
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def apply_update(src_dir: Path) -> int:
    """用新包内容覆盖专家包。

    核心:逐文件复制覆盖(失败才计入致命失败);
    清理:尽力而为(删除旧文件/空目录失败不影响更新成功,最多残留无用文件)。
    """
    root = get_expert_root(require_marker=True)
    fail_count = 0

    def is_excluded(rel: Path) -> bool:
        return any(part in EXCLUDE_NAMES for part in rel.parts)

    # 1. 核心:覆盖/新增 src 中的文件
    for rel in [p.relative_to(src_dir) for p in src_dir.rglob("*") if p.is_file()]:
        if is_excluded(rel):
            continue
        s, d = src_dir / rel, root / rel
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        except Exception:
            fail_count += 1

    # 2. 清理(尽力而为,不阻断):删除 src 已不存在的多余文件
    for p in list(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if is_excluded(rel):
            continue
        if not (src_dir / rel).exists():
            try:
                p.unlink()
            except Exception:
                pass

    # 3. 清理(尽力而为,不阻断):删除多余空目录
    for d in sorted([p for p in root.rglob("*") if p.is_dir()], reverse=True):
        try:
            rel = d.relative_to(root)
        except ValueError:
            continue
        if is_excluded(rel):
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass
    return fail_count


def cmd_check() -> int:
    uid = get_current_user_id()
    if not uid:
        log("NO_USER_ID")
        return 0
    if uid == AUTHOR_USER_ID:
        log("AUTHOR_MODE")
        return 0
    # 非作者:仅生产环境 main(URL 写死,无分支参数)
    if len(sys.argv) > 2:
        log("NOT_AUTHOR_NO_BRANCH")
        return 0
    try:
        _, remote_ver = fetch_remote_package()
    except Exception as e:
        log(f"ERROR:无法访问 GitHub({e})")
        return 1
    local_ver = get_local_version()
    if local_ver.get("version") == remote_ver.get("version"):
        log("UP_TO_DATE")
    else:
        log("UPDATE_AVAILABLE")
    return 0


def cmd_update() -> int:
    uid = get_current_user_id()
    if not uid:
        log("NO_USER_ID")
        return 0
    if uid == AUTHOR_USER_ID:
        log("AUTHOR_MODE")
        return 0
    # 写死:非作者仅 main,无分支参数可传
    if len(sys.argv) > 2:
        log("NOT_AUTHOR_NO_BRANCH")
        return 0
    # 安全防呆:更新前先校验专家包根(防止误判路径导致误删)
    try:
        get_expert_root(require_marker=True)
    except Exception as e:
        log(f"ERROR:{e}")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="wp-dl-"))
    try:
        src_dir, remote_ver = fetch_remote_package()
        local_ver = get_local_version()
        if local_ver.get("version") == remote_ver.get("version"):
            log("UP_TO_DATE")
            return 0
        fail = apply_update(src_dir)
    except Exception as e:
        log(f"ERROR:更新失败({e})")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fail:
        log(f"ERROR:部分文件被占用({fail} 个),请关闭其他 WorkBuddy 窗口后重试更新")
        return 1
    log("UPDATED")
    return 0


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "check"
    if action == "check":
        sys.exit(cmd_check())
    elif action == "update":
        sys.exit(cmd_update())
    else:
        log("用法: python3 auto_update.py [check|update]")
        sys.exit(2)
