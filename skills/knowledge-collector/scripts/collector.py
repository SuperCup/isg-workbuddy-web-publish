#!/usr/bin/env python3
"""
知识卡片收集器 - 流程编排(嵌入 workbuddy 等 Agent 工具专家)
====================================================
流程(已与大明确认):
  ① 隐私说明
  ② 用户同意/不同意
     ├─ 同意 → 存同意记录 → 进③
     └─ 不同意 → 仅本地状态标记(不上传)→ 结束
  ③ 扫描 Agent 工作空间, 列出用户名下任务/项目 → 用户勾选范围
  ④ 收集选中任务/项目的附件:
     - 本地过滤敏感/隐私 → 生成知识卡片(通用prompt+schema校验)
     - 原始附件打压缩包(单≤20M/总≤200M, 超限让用户挑)
     - 更新用户信息文件(md)
  ⑤ 上传 OSS(STS 临时凭证, agents/<member_id>/, 仅本人+管理员可读)

设计原则:
  - 与具体 Agent 工具解耦: 工作空间根路径可配置
  - 与具体模型解耦: 抽离走通用 Prompt 模板 + JSON Schema 校验
  - 敏感过滤在本地, 独立成词库
  - 限量/权限/共享规则按确认执行
"""

import os
import sys
import json
import time
import shutil
import zipfile
import hashlib
import datetime
import subprocess
from pathlib import Path
from typing import List, Dict, Optional

# ============================================================
# 配置(可从环境变量 / config.json 覆盖)
# ============================================================
def _auto_workspace_root() -> str:
    """自动探测本机 WorkBuddy 工作空间根(优先 ~/.workbuddy, 不存在则用环境变量)"""
    home_root = Path.home() / ".workbuddy"
    if home_root.exists():
        return str(home_root)
    return str(Path.home() / ".workbuddy")


def _auto_local_work_dir() -> str:
    """本地中间产物目录: ~/.workbuddy/knowledge-collect(自动创建, 独立于专家包, 更新不覆盖)"""
    return str(Path.home() / ".workbuddy" / "knowledge-collect")


CONFIG_DEFAULT = {
    # Agent 工具工作空间根(自动探测 ~/.workbuddy, 从这扫描 projects/tasks/workspace)
    "agent_workspace_root": _auto_workspace_root(),
    # 输出/工作目录(本地中间产物; 位于 ~/.workbuddy/knowledge-collect, 专家包更新不会覆盖)
    "local_work_dir": _auto_local_work_dir(),
    # 上传相关
    "oss_bucket": "instantretailagent",
    "oss_endpoint": "oss-cn-shenzhen.aliyuncs.com",
    "sts_token_endpoint": "http://127.0.0.1:9100/v1/sts-token",  # 可选代签服务
    "upload_mode": "direct",         # 'direct'(默认) | 'sts'
    "oss_ak_id": "",                 # direct 模式用(通常走混淆 blob, 无需填写)
    "oss_ak_secret": "",
    # 限量
    "single_file_max_mb": 20,
    "total_max_mb": 200,
    # 共享: 仅管理员可跨用户读
    "admin_member_ids": [],   # 由运维/管理员填
    # 业务线
    "biz_lines": ["即时零售(到家)", "到店营销", "物码"],
    # 提醒频率控制: 每次执行后, 经历这个天数才再次提醒(默认14天)
    "reminder_interval_days": 14,
    # 不参与采集的部门(匹配用户组织后, 命中即拒绝采集)
    "exclude_depts": ["财务部", "人力资源部", "行政部"],
}

def load_config() -> Dict:
    cfg = dict(CONFIG_DEFAULT)
    # 环境变量覆盖
    env_map = {
        "AGENT_WORKSPACE_ROOT": "agent_workspace_root",
        "LOCAL_WORK_DIR": "local_work_dir",
        "OSS_BUCKET": "oss_bucket",
        "OSS_ENDPOINT": "oss_endpoint",
        "STS_TOKEN_ENDPOINT": "sts_token_endpoint",
        "OSS_UPLOAD_MODE": "upload_mode",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    # 本地 config.json 覆盖(若有)
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


CONFIG = load_config()

# ============================================================
# 常量
# ============================================================
PRIVACY_NOTICE = """关于客户知识采集的说明

我们正在搭建公司级的【客户（品牌）知识库】——把大家在与客户协作中沉淀的宝贵经验（客户偏好、指标口径、表达习惯、关键决策点等）系统化地收集起来，让每一位同事都能站在彼此的肩膀上，为客户提供更专业、更贴心的服务。你贡献的每一份内容，都是团队知识资产的一部分。

我们只收集：与你经手的客户、品牌业务相关的内容，用于沉淀"客户关系、指标偏好、表达方式、人员视角"等知识卡片，让知识库真正帮到你。

我们绝不收集：你的账号密码、私人照片、与工作或客户无关的内容，以及闲聊调侃信息。采集时会主动过滤这些个人隐私。

收集方式：在你使用到相关环节时，会先征得你同意；你可以自行勾选想分享的范围。上传的文件仅限你本人和内部知识管理人员可见，不会向他人公开。

你可以随时线下联系我们撤回或调整。

点击「同意」即表示你知晓并同意上述范围；点击「不同意」则本次不收集任何内容。

【特别说明】本采集仅面向客户知识卡片的整理与收集；若你所在部门为财务部 / 人力资源部 / 行政部，将不会发起采集。"""


class Status:
    """流程状态标记(本地)"""
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SCANNING = "scanning"
    SELECTING = "selecting"
    COLLECTING = "collecting"
    PACKING = "packing"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"


def state_path(member_id: str) -> Path:
    d = Path(CONFIG["local_work_dir"]) / member_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def save_state(member_id: str, state: str, **extra):
    p = state_path(member_id)
    data = {"member_id": member_id, "state": state, "updated_at": datetime.datetime.now().isoformat(), **extra}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# ============================================================
# ① 隐私说明 + ② 同意/不同意
# ============================================================
def show_privacy() -> str:
    return PRIVACY_NOTICE


def load_login_user() -> Optional[Dict]:
    """读取登录 ismartgo 时保存的用户信息(~/.workbuddy/ismartgo_user.json)
    内容: {account, userid, dept, org, savedAt}
    由 ismartgo-token 技能在登录成功后自动写入。
    """
    p = Path.home() / ".workbuddy" / "ismartgo_user.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def user_dept() -> str:
    """返回当前用户所在部门(来自登录用户信息, 未知返回 '')"""
    info = load_login_user()
    if info:
        return info.get("dept", "") or ""
    return ""


def handle_consent(member_id: str, decision: str) -> Dict:
    """decision: 'accept' | 'reject'"""
    decision = decision.lower()
    if decision == "accept":
        return save_state(member_id, Status.ACCEPTED,
                          agreed_at=datetime.datetime.now().isoformat(),
                          privacy_version="v1")
    elif decision == "reject":
        # 不同意: 仅本地标记, 不上传任何内容
        return save_state(member_id, Status.REJECTED,
                          rejected_at=datetime.datetime.now().isoformat(),
                          note="user_rejected")
    else:
        return {"error": "decision 必须是 accept 或 reject"}


# ============================================================
# ③ 扫描 Agent 工作空间, 列出任务/项目
# ============================================================
def scan_workspace(member_id: str) -> List[Dict]:
    """
    扫描 agent_workspace_root 下的 projects/tasks/workspace/sessions,
    返回可收集的任务/项目清单(供用户勾选)。
    只列该用户名下(这里以"是不是本机用户"做初判, 具体归属规则可配置)。
    """
    root = Path(CONFIG["agent_workspace_root"])
    items = []

    def _scan_dir(base: Path, kind: str):
        if not base.exists():
            return
        for entry in sorted(base.iterdir()):
            if entry.is_dir() or entry.is_file():
                # 估算大小
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file()) if entry.is_dir() else entry.stat().st_size
                items.append({
                    "name": entry.name,
                    "path": str(entry),
                    "kind": kind,
                    "size_mb": round(size / 1024 / 1024, 2),
                })

    _scan_dir(root / "projects", "project")
    _scan_dir(root / "tasks", "task")
    _scan_dir(root / "workspace" / "sessions", "session")

    return items


def list_for_selection(member_id: str) -> List[Dict]:
    items = scan_workspace(member_id)
    return items


# ============================================================
# ④ 收集 + 抽离(见 collect_and_upload, 专家自抽模式)
# ============================================================


# ============================================================
# 提醒频率控制(14天): 到流程末尾时判断要不要提醒执行
# ============================================================
def reminder_state(member_id: str) -> Dict:
    """判断当前是否该提醒用户执行采集
    返回: {should_remind: bool, reason: 'first_time|due|within_interval', last_collect_at}
    规则: 从未执行过 → 提醒; 距上次 >= interval → 提醒; 否则不提醒
    """
    p = state_path(member_id)
    last = None
    if p.exists():
        try:
            last = json.loads(p.read_text(encoding="utf-8")).get("last_collect_at")
        except Exception:
            last = None
    interval = int(CONFIG["reminder_interval_days"])
    if not last:
        return {"should_remind": True, "reason": "first_time", "last_collect_at": None}
    # 解析上次执行时间
    try:
        last_dt = datetime.datetime.fromisoformat(last)
        if last_dt.tzinfo is not None:
            last_dt = last_dt.replace(tzinfo=None)  # 去tz, 统一naive比较
    except Exception:
        last_dt = None
    if last_dt is None:
        return {"should_remind": True, "reason": "invalid_state", "last_collect_at": last}
    days = (datetime.datetime.now() - last_dt).days
    if days >= interval:
        return {"should_remind": True, "reason": "due", "last_collect_at": last}
    return {"should_remind": False, "reason": "within_interval", "last_collect_at": last,
            "days_left": interval - days}


def mark_done(member_id: str, collect_result: Dict) -> Dict:
    """执行成功后记录 last_collect_at(供频率控制)"""
    p = state_path(member_id)
    cards = collect_result.get("cards")
    n_cards = len(cards) if isinstance(cards, list) else (cards or 0)
    data = {"member_id": member_id, "state": Status.DONE,
            "last_collect_at": datetime.datetime.now().isoformat(),
            "updated_at": datetime.datetime.now().isoformat(),
            "cards": n_cards,
            "uploaded": collect_result.get("uploaded", [])}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# ============================================================
# 完整执行: 收集 + 打包 + 上传
# ============================================================
def collect_and_upload(member_id: str, selected_paths: List[str],
                       cards: Optional[List[Dict]] = None,
                       current_session_member: str = "") -> Dict:
    """
    完整执行(专家自抽模式):
      - 若专家已回传 cards: 走 ingest 落盘
      - 否则: 返回 prepare_for_agent 的内容(供专家自抽), 由专家回传后再 ingest
    然后打包 + 更新用户信息 + 上传 OSS + 记录执行时间。
    强规则: 当前会话用户必须等于 member_id(防跨用户越权)。
    """
    from knowledge_extractor import (collect_raw_files, ingest_cards,
                                     prepare_for_agent)
    import uploader

    # 强规则: 当前会话用户必须等于操作目标 member_id
    if current_session_member and current_session_member != member_id:
        return {"ok": False, "error":
                f"越权拒绝: 当前会话用户 {current_session_member} 不能操作 {member_id} 的目录"}

    work_dir = Path(CONFIG["local_work_dir"]) / member_id

    # 1) 抽离落盘: 专家自抽
    if cards is None:
        # 第一次调用: 返回给专家看的内容, 让专家自抽
        prep = prepare_for_agent(member_id, selected_paths)
        return {"step": "extract", "prepare": prep}

    # 专家已回传卡片 → 落盘
    collect_result = ingest_cards(member_id, cards, work_dir=work_dir)

    # 2) 打包原始附件
    raw_files = collect_raw_files(selected_paths)
    pack = uploader.pack_raw(member_id, raw_files, work_dir)

    # 3) 用户信息(登录账号仅作展示, member_id 主键为 userid)
    login_info = load_login_user()
    uif = uploader.update_user_info(member_id, work_dir, {
        "user_id": (login_info or {}).get("userid", ""),
        "agent_user_id": member_id,
        "pms_account": (login_info or {}).get("account", ""),
        "agent_tool": "workbuddy",
        "collect_history": [{"time": datetime.datetime.now().isoformat(),
                             "scope": selected_paths, "cards": len(collect_result["cards"]),
                             "files": pack["packed_count"]}],
    })

    # 4) 上传
    up = uploader.upload_result(member_id, collect_result["cards"],
                                pack.get("zip_path"), work_dir, {"path": str(uif)})
    collect_result["pack"] = pack
    collect_result["upload"] = up

    # 5) 记录执行时间(频率控制)
    mark_done(member_id, collect_result)
    return collect_result


def run_flow(member_id: str, decision: str, selected_paths: Optional[List[str]] = None,
             current_session_member: str = "") -> Dict:
    """完整流程入口(供专家调用)
    - 专家流程末尾固定环节: 先调 should_remind 判断是否需要提醒
    - 需要提醒 → 展示隐私说明 → 用户选择(WorkBuddy 插件交互由专家负责) → 收集上传
    - 部门检查: 用户所在部门命中 exclude_depts → 拒绝采集
    强规则: 当前会话用户必须等于 member_id(防跨用户越权)。
    """
    if current_session_member and current_session_member != member_id:
        return {"step": "denied",
                "error": f"当前会话用户 {current_session_member} 不能操作 {member_id} 的目录"}

    # 部门检查: 命中不参与采集部门 → 拒绝
    dept = user_dept()
    if dept in CONFIG.get("exclude_depts", []):
        return {"step": "denied",
                "error": f"当前用户所在部门({dept})不参与客户知识采集, 已跳过",
                "dept": dept}

    # 触发判断: 未同意前, 先看是否该提醒
    if decision is None:
        rm = reminder_state(member_id)
        return {"step": "trigger", "should_remind": rm["should_remind"],
                "reason": rm.get("reason"), "last_collect_at": rm.get("last_collect_at"),
                "notice": show_privacy() if rm["should_remind"] else None}

    st = handle_consent(member_id, decision)
    if st.get("state") == Status.REJECTED:
        return {"step": "done", "result": "user_rejected", "uploaded": False}

    if selected_paths is None:
        return {"step": "select", "items": list_for_selection(member_id)}

    save_state(member_id, Status.COLLECTING)
    return collect_and_upload(member_id, selected_paths,
                              current_session_member=current_session_member)


# ============================================================
# CLI 入口(供 workbuddy 专家一条命令调用)
# ============================================================
def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="知识卡片采集器")
    parser.add_argument("--member", required=True, help="成员ID(userid, 来自登录 ismartgo 时记录的 ismartgo_user.json)")
    parser.add_argument("--action", choices=["trigger", "accept", "reject", "select", "collect", "whoami"],
                        default="trigger", help="流程动作")
    parser.add_argument("--paths", nargs="*", default=[], help="collect 时传入所选项目路径")
    parser.add_argument("--config", default="", help="可选config.json路径")
    parser.add_argument("--cards-json", default="", help="专家抽离后回传的JSON卡片(文件路径或内联JSON)")
    parser.add_argument("--session", default="", help="当前会话用户ID(强规则校验: 必须等于--member, 否则拒绝)")
    args = parser.parse_args(argv)

    if args.config:
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        CONFIG.update(cfg)

    # 强规则: 当前会话用户必须等于 member
    if args.session and args.session != args.member:
        out = {"step": "denied", "error": f"当前会话用户 {args.session} 不能操作 {args.member} 的目录"}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    if args.action == "whoami":
        info = load_login_user()
        out = {"action": "whoami", "user": info or {},
               "dept": user_dept(),
               "excluded": user_dept() in CONFIG.get("exclude_depts", [])}
    elif args.action == "trigger":
        RM = reminder_state(args.member)
        dept = user_dept()
        if dept in CONFIG.get("exclude_depts", []):
            out = {"action": "trigger", "should_remind": False,
                   "reason": "dept_excluded", "dept": dept,
                   "notice": "当前用户所在部门(财务部/人力资源部/行政部)不参与客户知识采集"}
        else:
            out = {"action": "trigger", "should_remind": RM["should_remind"],
                   "reason": RM.get("reason"), "last_collect_at": RM.get("last_collect_at"),
                   "dept": dept,
                   "notice": show_privacy() if RM["should_remind"] else None}
    elif args.action in ("accept", "reject"):
        out = handle_consent(args.member, args.action)
        if args.action == "accept":
            out["items"] = list_for_selection(args.member)
    elif args.action == "select":
        out = {"action": "select", "items": list_for_selection(args.member)}
    elif args.action == "collect":
        cards = None
        if args.cards_json:
            # 专家回传卡片: 文件路径或内联JSON
            cp = Path(args.cards_json)
            if cp.exists():
                cards = json.loads(cp.read_text(encoding="utf-8"))
            else:
                cards = json.loads(args.cards_json)
        out = collect_and_upload(args.member, args.paths, cards=cards,
                                 current_session_member=args.session)
    else:
        out = {"error": "unknown action"}

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    main()
