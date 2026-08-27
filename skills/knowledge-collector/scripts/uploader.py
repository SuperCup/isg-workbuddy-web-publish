#!/usr/bin/env python3
"""
打包 + 用户信息文件 + OSS 上传 模块
====================================================
- 打包: 原始附件打 zip(限量: 单≤20M/总≤200M, 超限让用户挑)
- 用户信息文件: md, 记录采集范围/时间/数量; 没有则创建
- OSS 上传: 默认【直接 AK/SK 签名】上传,
  凭证采用【混淆存储】(编码+拆分+运行时缝合), 避免用户在本地直接看到明文 AK。
  另保留 STS 可选模式(代签服务可用时优先生效, 更规范)。
"""

import os
import json
import zipfile
import datetime
import base64
from pathlib import Path
from typing import List, Dict, Optional


# ============================================================
# 凭证混淆存储(仅防"随手打开看到明文", 非真加密)
# ------------------------------------------------------------
# 原则: 不把 AK/SK 明文写进任何 .py/.json/.txt 配置。
# 而是把一段"[编码后的载荷]"存在独立文件, 运行时解码+拆分+缝合。
# 即便用户打开 config 也只能看到乱码片段, 看不到完整 AK。
# 说明: 这是"混淆"(obfuscation), 非强加密。攻击者仍可从内存/逆向还原,
#       但能阻止一般人"打开文件一眼看到 key"。真正的安全靠:
#       ① AK 属最小权限 RAM 子账号(只写 agents/ 前缀)
#       ② AK 已设过期时间(运维配置)
#       ③ 定期轮换
# ============================================================

# 混淆密钥(固定, 仅用于打乱, 不用于加密。可换, 但换后需重新生成载荷)
_MIX_KEY = "ismartgo-oss-sg-2026"
# 载荷文件:优先存 WorkBuddy 本机 ~/.workbuddy/oss_cred.blob(不随专家包分发),
# 避免混淆凭证在专家包分享/自动更新时泄露;可用环境变量 OSS_CRED_BLOB 覆盖
_CRED_FILE = "oss_cred.blob"


def _cred_path() -> Path:
    env = os.environ.get("OSS_CRED_BLOB", "")
    if env:
        return Path(env)
    home_blob = Path.home() / ".workbuddy" / _CRED_FILE
    if home_blob.exists():
        return home_blob
    return Path(__file__).parent / _CRED_FILE


def _xor(data: bytes, key: str) -> bytes:
    """简单异或打乱 + 可逆"""
    k = key.encode()
    return bytes(b ^ k[i % len(k)] for i, b in enumerate(data))


def encode_cred(ak: str, sk: str, blob_path: str = None) -> None:
    """把 AK/SK 混淆写入 blob 文件(运维/主控一次性执行, 不随包下发)"""
    payload = json.dumps({"ak": ak, "sk": sk}).encode()
    obf = base64.b64encode(_xor(payload, _MIX_KEY)).decode()
    (Path(blob_path) if blob_path else _cred_path()).write_text(obf, encoding="utf-8")


def decode_cred(blob_path: str = None) -> Dict:
    """运行时读取 blob, 解码还原 AK/SK"""
    p = Path(blob_path) if blob_path else _cred_path()
    if not p.exists():
        return {"ak": "", "sk": ""}
    obf = p.read_text(encoding="utf-8").strip()
    try:
        payload = _xor(base64.b64decode(obf), _MIX_KEY)
        data = json.loads(payload)
        return {"ak": data.get("ak", ""), "sk": data.get("sk", "")}
    except Exception:
        return {"ak": "", "sk": ""}


# ============================================================
# 配置
# ============================================================
def _default_cfg():
    return {
        # 上传鉴权源: 'direct'(默认, 直接AK签名) | 'sts'(代签服务, 需在线)
        "upload_mode": os.environ.get("OSS_UPLOAD_MODE", "direct"),
        # direct 模式下凭证: 优先读环境变量, 其次读混淆 blob
        "oss_ak_id": os.environ.get("OSS_UPLOAD_AK_ID", ""),
        "oss_ak_secret": os.environ.get("OSS_UPLOAD_AK_SECRET", ""),
        "single_file_max_mb": 20,
        "total_max_mb": 200,
        "oss_bucket": "instantretailagent",
        "oss_endpoint": "oss-cn-shenzhen.aliyuncs.com",
        "sts_token_endpoint": os.environ.get("STS_TOKEN_ENDPOINT", "http://127.0.0.1:9100/v1/sts-token"),
        "admin_member_ids": [],
    }


def _cfg():
    cfg = _default_cfg()
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 环境变量覆盖
    env_map = {
        "SINGLE_FILE_MAX_MB": "single_file_max_mb",
        "TOTAL_MAX_MB": "total_max_mb",
        "OSS_BUCKET": "oss_bucket",
        "OSS_ENDPOINT": "oss_endpoint",
        "STS_TOKEN_ENDPOINT": "sts_token_endpoint",
    }
    for env, k in env_map.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg


def _get_direct_cred(cfg: Dict) -> Dict:
    """拿到 direct 模式用的 AK/SK: 环境变量优先, 其次混淆blob"""
    ak = cfg.get("oss_ak_id", "")
    sk = cfg.get("oss_ak_secret", "")
    if not ak or not sk:
        # 读混淆 blob
        blob = decode_cred()
        ak = blob.get("ak", "")
        sk = blob.get("sk", "")
    if not ak or not sk:
        raise RuntimeError("direct模式缺少AK/SK: 请配置环境变量 OSS_UPLOAD_AK_ID/SECRET 或用 encode_cred 生成混淆凭证")
    return {"ak": ak, "sk": sk}


# ============================================================
# 强规则: 越权防护
# ------------------------------------------------------------
# ① 同一共享AK, 无法靠RAM权限区分用户 → 在代码层强制:
#    任何对象key必须落在 agents/<当前会话用户ID>/ 前缀下,
#    凡请求 member_id 不是当前会话用户ID、或目标key落在他人前缀,
#    一律拒绝(WRITE/READ/DELETE 皆禁止跨用户越权)。
# ② 采集器【不提供删除能力】: 只写不删, 杜绝误删。
#    真正删除只能在管理侧(主控AK)执行。
# ============================================================

def assert_in_own_prefix(member_id: str, target_key: str) -> str:
    """
    校验 target_key 必须属于 member_id 自己的 agents/<member_id>/ 前缀。
    通过则返回目标key; 越权则抛出异常。
    """
    own_prefix = f"agents/{member_id}/"
    if not target_key.startswith(own_prefix):
        raise PermissionError(
            f"越权访问被拒绝: 当前用户 {member_id} 只能操作 {own_prefix}, "
            f"目标 {target_key} 不在其名下")
    return target_key


def assert_member_id(current_session_member: str, requested_member: str):
    """当前会话用户ID 必须与 操作目标member_id 一致, 否则拒绝。
    这是防止 A 借采集器操作 B 目录的最后防线。"""
    if current_session_member != requested_member:
        raise PermissionError(
            f"越权: 当前会话用户 {current_session_member} 试图操作 "
            f"他人的目录 {requested_member}, 已拒绝")


# ============================================================
# 用户信息文件(md)
# ============================================================
def update_user_info(member_id: str, work_dir: Path, extra: Dict) -> Path:
    """创建/更新用户信息文件 agents/<member_id>/user_info.md 的本地映射"""
    d = Path(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    info = {
        "member_id": member_id,
        "pms_account": extra.get("pms_account", ""),
        "user_id": extra.get("user_id", ""),
        "agent_tool": extra.get("agent_tool", ""),
        "agent_user_id": extra.get("agent_user_id", ""),
        "updated_at": datetime.datetime.now().isoformat(),
        "collect_history": extra.get("collect_history", []),
    }
    fp = d / "user_info.md"
    lines = [
        "# 用户采集信息",
        f"- 成员ID: {info['member_id']}",
        f"- PMS账号: {info['pms_account']}",
        f"- UserId: {info['user_id']}",
        f"- Agent工具: {info['agent_tool']}",
        f"- Agent内UserID: {info['agent_user_id']}",
        f"- 更新时间: {info['updated_at']}",
        "",
        "## 采集历史",
    ]
    for h in info["collect_history"]:
        lines.append(f"- {h.get('time','')} | 范围:{h.get('scope','')} | 卡片:{h.get('cards',0)} | 附件:{h.get('files',0)}")
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


# ============================================================
# 打包(限量)
# ============================================================
def pack_raw(member_id: str, raw_files: List[str], work_dir: Path) -> Dict:
    """
    把原始附件打 zip。限量规则:
      - 单文件 ≤ single_file_max_mb
      - 总 ≤ total_max_mb
      - 超限 → 返回 over_limit 列表(让用户挑)
    返回: {zip_path, packed_count, skipped_over_size, over_limit_files}
    """
    cfg = _cfg()
    single_max = cfg["single_file_max_mb"] * 1024 * 1024
    total_max = cfg["total_max_mb"] * 1024 * 1024

    d = Path(work_dir)
    d.mkdir(parents=True, exist_ok=True)
    over_limit = []
    excluded_sensitive = []  # 含敏感内容的文件, 不打包(防止 token/密钥等随附件包上传)
    packable = []
    total_size = 0

    for fp in raw_files:
        p = Path(fp)
        if not p.exists():
            continue
        sz = p.stat().st_size
        if _content_sensitive(p):
            excluded_sensitive.append({"path": fp, "reason": "内容含敏感词, 已剔除不上传"})
            continue
        if sz > single_max:
            over_limit.append({"path": fp, "size_mb": round(sz/1024/1024, 2), "reason": "单个超20M"})
            continue
        if total_size + sz > total_max:
            over_limit.append({"path": fp, "size_mb": round(sz/1024/1024, 2), "reason": "累计超200M"})
            continue
        packable.append((fp, sz))
        total_size += sz

    zip_name = f"{member_id}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_raw.zip"
    zip_path = d / zip_name
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        # zip 内保留相对路径结构(公共父目录), 避免不同目录同名文件互相覆盖
        if packable:
            parents = [str(Path(fp).parent) for fp, _ in packable]
            common = os.path.commonpath(parents) if len(packable) > 1 else parents[0]
            common_root = Path(common)
            for fp, sz in packable:
                try:
                    rel = Path(fp).relative_to(common_root)
                    z.write(fp, str(rel))
                except Exception:
                    try:
                        z.write(fp, os.path.basename(fp))
                    except Exception:
                        pass

    return {
        "zip_path": str(zip_path),
        "packed_count": len(packable),
        "total_size_mb": round(total_size/1024/1024, 2),
        "over_limit_files": over_limit,
        "excluded_sensitive": excluded_sensitive,
    }


def _load_sensitive_words() -> List[str]:
    """读取敏感词库(skill 根目录 sensitive_words.txt 或脚本同级)"""
    for wf in (Path(__file__).parent.parent / "sensitive_words.txt",
               Path(__file__).parent / "sensitive_words.txt"):
        if wf.exists():
            try:
                return [w.strip() for w in wf.read_text(encoding="utf-8").splitlines() if w.strip()]
            except Exception:
                pass
    return []


def _content_sensitive(path: Path) -> bool:
    """检测文本类文件内容是否含敏感词(敏感文件不打包, 防止 token/密钥等随附件上传)"""
    try:
        suffix = path.suffix.lower()
        if suffix in (".jsonl", ".ndjson", ".md", ".txt", ".csv", ".json", ".html", ".py", ".log", ".js"):
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if not text.strip():
                return False
            for w in _load_sensitive_words():
                if w and w.lower() in text:
                    return True
    except Exception:
        pass
    return False


# ============================================================
# OSS 上传(STS 临时凭证, 可选; 默认 direct)
# ============================================================
def get_sts_token(member_id: str) -> Dict:
    """调用代签服务换发临时凭证"""
    import urllib.request
    cfg = _cfg()
    body = json.dumps({"member_id": member_id}).encode()
    req = urllib.request.Request(cfg["sts_token_endpoint"], data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ============================================================
# 鉴权源: upload_mode = 'direct'(默认) | 'sts'
#   direct : 直接用 AK/SK 签名(默认; 凭证混淆存储, AK已设过期)
#   sts    : 调代签服务换临时凭证(若代签服务在线可用, 更规范)
# ============================================================
def _get_auth_bucket(member_id: str):
    """返回 (oss2.Bucket, cred_dict)"""
    import oss2
    cfg = _cfg()
    mode = cfg.get("upload_mode", "direct")

    if mode == "direct":
        cred = _get_direct_cred(cfg)
        ak, sk = cred["ak"], cred["sk"]
        c = {"access_key_id": ak, "access_key_secret": sk,
             "security_token": None, "bucket": cfg["oss_bucket"],
             "endpoint": cfg["oss_endpoint"]}
        auth = oss2.Auth(ak, sk)
        bucket = oss2.Bucket(auth, f"https://{cfg['oss_endpoint']}", cfg["oss_bucket"])
        return bucket, c

    # sts 模式(代签服务在线时)
    sts = get_sts_token(member_id)
    if not sts.get("data"):
        raise RuntimeError(f"STS 换发失败: {sts}")
    cred = sts["data"]
    api = oss2.StsAuth(cred["access_key_id"], cred["access_key_secret"], cred["security_token"])
    bucket = oss2.Bucket(api, f"https://{cred['endpoint']}", cred["bucket"])
    return bucket, cred


def upload_to_oss(local_path: str, object_key: str, member_id: str = "") -> Dict:
    """用当前配置的鉴权源上传(直接用/或用STS)。
    强制校验: object_key 必须落在 member_id 自己的前缀下, 否则拒绝。"""
    if not member_id:
        raise PermissionError("member_id 不能为空")
    # 校验目标key归属当前用户(越权拒绝)
    assert_in_own_prefix(member_id, object_key)
    bucket, cred = _get_auth_bucket(member_id)
    bucket.put_object_from_file(object_key, local_path)
    return {"ok": True, "url": f"https://{cred['bucket']}.{cred['endpoint']}/{object_key}"}


def upload_result(member_id: str, cards: List[Dict],
                  raw_zip: Optional[str], work_dir: Path, user_info: Dict) -> Dict:
    """
    上传: 知识卡片(目录) + 原始包 + 用户信息文件
    放到 agents/<member_id>/knowledge/...
    鉴权源由 config upload_mode 决定(sts/direct)。
    强规则: 所有上传的key必须落在 agents/<member_id>/ 下, 越权拒绝。
    """
    try:
        bucket, cred = _get_auth_bucket(member_id)
    except Exception as e:
        return {"ok": False, "error": f"获取上传鉴权失败: {e}"}
    prefix = f"agents/{member_id}/"

    uploaded = []
    # 1) 知识卡片(按品牌/业务线/内容类型三级归类)
    for c in cards:
        if c.get("ok"):
            local = c.get("md_path")
            rel = c.get("rel_path", "")  # knowledge/<品牌>/<业务线>/<类型>/<fname>.md
            if local and os.path.exists(local):
                key = f"{prefix}{rel}" if rel else f"{prefix}knowledge/{Path(local).name}"
                assert_in_own_prefix(member_id, key)
                bucket.put_object_from_file(key, local)
                uploaded.append(key)
    # 2) 原始包
    if raw_zip and os.path.exists(raw_zip):
        key = f"{prefix}archive/{os.path.basename(raw_zip)}"
        assert_in_own_prefix(member_id, key)
        bucket.put_object_from_file(key, raw_zip)
        uploaded.append(key)
    # 3) 用户信息文件
    uif = user_info.get("path")
    if uif and os.path.exists(uif):
        key = f"{prefix}user_info.md"
        assert_in_own_prefix(member_id, key)
        bucket.put_object_from_file(key, uif)
        uploaded.append(key)

    return {"ok": True, "uploaded": uploaded, "prefix": prefix}


def main():
    cfg = _cfg()
    print("config:", json.dumps(cfg, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
