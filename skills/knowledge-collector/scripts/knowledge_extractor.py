#!/usr/bin/env python3
"""
知识卡片抽离模块
====================================================
核心设计(与任何模型解耦):
  - 本地解析工作空间对话流/文件, 提取"与工作/客户相关的知识"
  - 用【通用 Prompt 模板】让"任何模型"按固定 JSON Schema 输出
  - 本地做 JSON Schema 校验, 不合规自动重抽/标记
  - 敏感/隐私过滤在本地独立词库, 不依赖模型判断

产出:
  knowledge/<品牌>/<业务线>/<内容类型>/<卡片>.md + <卡片>.json
  原始附件包(限量)

数据源: Agent 工作空间的 projects/tasks/workspace-sessions 文件
  (含 .jsonl 对话流, 内含用户指令/附件引用/输出内容)
"""

import os
import sys
import re
import json
import datetime
from pathlib import Path
from typing import List, Dict, Optional

# 业务线与内容类型(与大明确认)
BIZ_LINES = ["即时零售(到家)", "到店营销", "物码"]
CARD_TYPES = ["关系映射表", "指标与数据维度偏好", "表达方式", "人员视角"]

# 敏感过滤词库(本地, 抽离前过滤)
SENSITIVE_WORDS = [
    "password", "密码", "passwd", "token", "access_key", "secret_key",
    "api_key", "私钥", "secret", "账号密码", "身份证", "手机号", "银行",
    "私人照片", "自拍", "私聊", "qq号", "微信", "家庭", "住址", "生日",
    "personal_photo", "private", "闲聊", "逗趣", "吐槽", "段子", "表情包",
]


def load_sensitive_words() -> List[str]:
    """从独立词库文件加载, 便于运维/你扩充"""
    # 优先读取 skill 根目录的 sensitive_words.txt(用户扩充的主词库),
    # scripts/ 同级仅作兜底(不应存在, 由旧版 ensure_sensitive_file 误生成)
    for wf in (Path(__file__).parent.parent / "sensitive_words.txt",
               Path(__file__).parent / "sensitive_words.txt"):
        if wf.exists():
            try:
                return [w.strip() for w in wf.read_text(encoding="utf-8").splitlines() if w.strip()]
            except Exception:
                pass
    return SENSITIVE_WORDS


def is_sensitive(text: str) -> bool:
    """判断文本是否含敏感/隐私内容"""
    for w in load_sensitive_words():
        if w.lower() in text.lower():
            return True
    return False


def filter_sensitive(text: str) -> str:
    """把敏感片段替换为占位符(抽离时剔除)"""
    for w in load_sensitive_words():
        if w and w in text:
            text = text.replace(w, f"[已过滤:{w}]")
    return text


# ============================================================
# JSON Schema(任何模型按此输出; 本地校验)
# ============================================================
CARD_SCHEMA = {
    "type": "object",
    "required": ["brand", "biz_line", "card_type", "content", "source", "tags", "quality"],
    "properties": {
        "brand": {"type": "string", "description": "品牌名"},
        "biz_line": {"type": "string", "enum": BIZ_LINES, "description": "业务线"},
        "card_type": {"type": "string", "enum": CARD_TYPES, "description": "内容类型"},
        "content": {"type": "string", "description": "提炼的知识内容(与工作/客户相关)"},
        "source": {"type": "string", "description": "来源(项目/任务/看板名称)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "标签, 如 #区域 #SKU"},
        "quality": {"type": "string", "enum": ["高", "中", "低"], "description": "置信度"},
    },
}


def validate_card(card: Dict) -> List[str]:
    """本地校验卡片是否符合 schema, 返回错误列表(空=通过)"""
    errors = []
    for field in CARD_SCHEMA["required"]:
        if field not in card or card[field] in (None, "", []):
            errors.append(f"缺失必填: {field}")
    if card.get("biz_line") and card["biz_line"] not in BIZ_LINES:
        errors.append(f"biz_line 非法: {card['biz_line']}")
    if card.get("card_type") and card["card_type"] not in CARD_TYPES:
        errors.append(f"card_type 非法: {card['card_type']}")
    return errors


# ============================================================
# 通用 Prompt 模板(任何模型加载这个即可按规范抽离)
# ============================================================
PROMPT_TEMPLATE = """你是一个知识抽取助手。请从给定内容中, 抽取【与工作或客户相关的知识】, 并按 JSON 输出。

抽取范围(只抽这些):
- 客户关系信息: 子品牌、SKU名称映射、区域映射、零售商映射
- 客户偏好: 关注的指标、数据分析维度
- 表达方式: 分析/建议/结论的措辞习惯
- 人员视角: 客户对接人、部门、角色、职位、个性化关注点

不要抽取(敏感/隐私):
- 账号密码、token、密钥、手机号、身份证、私人照片、家庭住址、生日
- 与工作/客户无关的闲聊、调侃、逗趣、表情包

输出格式(严格 JSON, 不要多余文字):
{json_schema}

内容如下:
<<<
{content}
>>>"""


def build_prompt(content: str) -> str:
    return PROMPT_TEMPLATE.format(json_schema=json.dumps(CARD_SCHEMA, ensure_ascii=False, indent=2),
                                  content=content[:8000])


# ============================================================
# 对话流解析(.jsonl) - 从 Agent 工作空间提取内容
# ============================================================
def extract_text_from_path(path: Path) -> str:
    """从文件提取可读文本(jsonl 对话流 / md / txt / json)"""
    try:
        suffix = path.suffix.lower()
        if suffix in (".jsonl", ".ndjson"):
            texts = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        texts.append(line)
                        continue
                    # 递归提取字符串值
                    def _collect(o):
                        if isinstance(o, str):
                            return o
                        if isinstance(o, dict):
                            return " ".join(_collect(v) for v in o.values())
                        if isinstance(o, list):
                            return " ".join(_collect(v) for v in o)
                        return ""
                    texts.append(_collect(obj))
            return "\n".join(texts)
        elif suffix in (".md", ".txt", ".csv", ".html", ".json"):
            return path.read_text(encoding="utf-8", errors="replace")
        else:
            # 二进制(图片/zip/pdf等) 只记录来源, 不抽内容
            return ""
    except Exception as e:
        return f"[解析失败: {e}]"


def get_attachments_in_project(project_dir: Path) -> List[Path]:
    """列出项目目录下"可抽离/可打包"的附件"""
    files = []
    if project_dir.is_file():
        return [project_dir]
    for p in project_dir.rglob("*"):
        if p.is_file():
            files.append(p)
    return files


# ============================================================
# 主流程(专家自抽模式)
# 说明: 抽离内容由专家(用户在 workbuddy 选定的模型)提供,
#       采集器只做: ① 前置准备给专家看的内容(已敏感过滤)
#                  ② 专家回传 JSON 后, 本地 schema 校验 + 落盘
#       不再使用任何规则兜底。
# ============================================================
def prepare_for_agent(member_id: str, selected_paths: List[str]) -> Dict:
    """
    专家调用: 返回待抽离的"已敏感过滤"内容 + 抽离规范(Prompt提示词)。
    专家据此用自身模型按 JSON Schema 抽离。
    返回: {items: [{source, content, prompt}], schema}
    """
    items = []

    def expand(p: Path):
        if p.is_file():
            return [p]
        if p.is_dir():
            return [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in
                    (".jsonl", ".ndjson", ".md", ".txt", ".csv", ".json", ".html")]
        return []

    for sp in selected_paths:
        p = Path(sp)
        if not p.exists():
            items.append({"source": sp, "error": "路径不存在"})
            continue
        for file in expand(p):
            text = extract_text_from_path(file)
            filtered = filter_sensitive(text)
            if not filtered.strip():
                continue
            items.append({
                "source": str(file),
                "content": filtered,
                "prompt": build_prompt(filtered),
            })
    return {"items": items, "schema": CARD_SCHEMA}


def ingest_cards(member_id: str, cards: List[Dict], work_dir: Path) -> Dict:
    """
    专家: 把抽离的 JSON 卡片列表传回, 本地 schema 校验 + 落盘。
    归类: knowledge/<品牌>/<业务线>/<内容类型>/<fname>.md|json
    返回统计 + 每张卡片的 ok/errors。
    """
    work_dir = Path(work_dir)
    extracted_dir = work_dir / "knowledge"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    result = {"member_id": member_id, "cards": []}
    for card in cards:
        errors = validate_card(card)
        if not errors:
            # 三级归类: 品牌/业务线/内容类型
            brand = sanitize_dir(card.get("brand", "未识别品牌"))
            biz = sanitize_dir(card.get("biz_line", "未分类"))
            ctype = sanitize_dir(card.get("card_type", "其他"))
            card_dir = extracted_dir / brand / biz / ctype
            card_dir.mkdir(parents=True, exist_ok=True)
            source = card.get("source", "unknown")
            fname = make_card_filename(source, card)
            json_path = card_dir / f"{fname}.json"
            md_path = card_dir / f"{fname}.md"
            json_path.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(to_markdown(card), encoding="utf-8")
            card["ok"] = True
            card["json_path"] = str(json_path)
            card["md_path"] = str(md_path)
            card["rel_path"] = f"knowledge/{brand}/{biz}/{ctype}/{fname}.md"
        else:
            card["ok"] = False
            card["errors"] = errors
        result["cards"].append(card)
    return result


def collect_raw_files(selected_paths: List[str]) -> List[str]:
    """返回选中路径下的原始附件(供打包)"""
    raw = []
    for sp in selected_paths:
        p = Path(sp)
        if p.exists():
            for f in get_attachments_in_project(p):
                raw.append(str(f))
    return raw


def sanitize_dir(name: str) -> str:
    """目录名安全化: 去除路径分隔符/非法字符, 避免路径穿越"""
    s = re.sub(r'[\\/:*?"<>|\r\n]+', '_', str(name)).strip()
    return s or "未命名"


def make_card_filename(p, card: Dict) -> str:
    import hashlib
    # p 可能是 Path 或 str
    pname = Path(p).name if not isinstance(p, str) else Path(p).name
    h = hashlib.md5(f"{pname}{card['brand']}{card['card_type']}".encode()).hexdigest()[:6]
    # 加时间戳避免同来源同类型碰撞
    ts = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    return f"{card['brand']}_{card['card_type']}_{ts}_{h}"


def to_markdown(card: Dict) -> str:
    return (f"# {card.get('brand','')} · {card.get('card_type','')}\n\n"
            f"- 业务线: {card.get('biz_line','')}\n"
            f"- 来源: {card.get('source','')}\n"
            f"- 标签: {', '.join(card.get('tags', []))}\n"
            f"- 置信度: {card.get('quality','')}\n\n"
            f"## 内容\n{card.get('content','')}\n")


# ============================================================
# 敏感词库写入(仅当主词库缺失时生成默认文件到 skill 根目录)
# ============================================================
def ensure_sensitive_file():
    wf = Path(__file__).parent.parent / "sensitive_words.txt"
    if not wf.exists():
        try:
            wf.write_text("\n".join(SENSITIVE_WORDS), encoding="utf-8")
        except Exception:
            pass


ensure_sensitive_file()
