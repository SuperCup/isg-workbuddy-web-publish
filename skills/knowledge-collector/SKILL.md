# knowledge-collector — 客户知识采集

面向**公司客户(品牌)知识库搭建**的知识采集技能。嵌入「WEB部署Agent(小包)」,在用户同意后,扫描其 Agent 工作空间,由专家(AI)自抽客户知识卡片,限量打包原始附件,上传 OSS 按 `agents/<member_id>/` 前缀隔离存储。

## 硬性规则

- **目的**:仅采集与客户/品牌业务相关的知识(关系映射、指标偏好、表达方式、人员视角),用于搭建客户知识库
- **部门排除(写死)**:用户所在部门命中 `exclude_depts`(财务部/人力资源部/行政部)→ **拒绝采集**
- **member_id**:用户登录 ismartgo 成功后自动抓取保存的 `userid`(`~/.workbuddy/ismartgo_user.json`),登录账号仅作展示
- **强规则(防越权,已落地)**:
  - 对象 key 必须落在 `agents/<当前会话用户ID>/` 下,越权抛 `PermissionError`
  - `--session` 必须等于 `--member`,否则 `denied`
  - 采集器**只写不删**
- **敏感过滤**:文本内容含敏感词的**原始文件不打包上传**(防止 token/密钥/系统提示等泄露),仅知识卡片可上传
- **凭证安全**:OSS AK/SK 混淆存 `~/.workbuddy/oss_cred.blob`(不随专家包分发,不写明文)

## 执行模型与可靠性(重要,回答"会不会被中断")

- **上传是同步执行,非异步、非后台守护**:`collector.py` 由专家在会话内通过 Bash **同步启动前台子进程**,`oss2.put_object_from_file` 为同步阻塞上传,全程等待返回后才输出结果
- **会话进程依赖**:上传执行期间若用户**关闭 WorkBuddy 对应任务框/会话进程** → 子进程被终止 → 上传中断(已传对象保留在 OSS,未传对象丢失)
- **安全侧保证(已闭环)**:上传中断时 `mark_done` 不会执行 → 7 天提醒计时**不刷新** → 下次打包流程末尾仍会提醒采集(符合「OSS 上传成功才刷新计时」硬性条件,不会因中断而误以为已采集)
- **幂等重试**:重新跑 collect 会按同名 key 覆盖上传,无需清理;本地产物保留在 `~/.workbuddy/knowledge-collect/<member>/`(卡片 md / 原始包 zip),可复用
- **专家侧要求**:进入上传步骤(collect 第 4 步)前,必须告知用户「正在上传知识卡片到 OSS,请保持本会话打开,上传完成后会提示」;若用户反馈上次上传被中断,直接重新跑 collect(已落盘卡片可复用 `--cards-json` 重传)

## 交互要求(专家侧)

1. **同意/不同意必须用 WorkBuddy 选择组件**(弹窗):展示隐私说明后,给出「同意」「不同意」两个选项由用户点击选择,不得用纯文本让用户输入
2. **二次确认**:用户主动说"上传知识/收集知识/沉淀知识"等(含同义表达)时,先展示隐私说明并请用户确认范围,确认后才执行
3. **部门拒绝**:若 `trigger` 返回 `reason=dept_excluded`,礼貌告知用户所在部门不参与客户知识采集,不进入采集流程
4. **范围勾选**:工作空间项目列表 > 4 项时,在会话框列出全部选项由用户回复编号选择(WorkBuddy 选择组件上限 4 项)
5. **上传前展示**:告知用户将上传「知识卡片 + 已过滤的原始附件包」,仅本人与内部知识管理人员可见

## 触发时机

- **流程末尾提醒(强制)**:打包/上传流程结束时**必须**运行 `--action trigger`(见 agent.md Step 13),`should_remind=true` 时主动发起采集(**7 天内不重复提醒**,且**仅在上次采集成功并上传 OSS 后**才开始计时;上传失败不刷新,下次仍会提醒);**这是打包流程固定环节,不可跳过**
- **用户主动提出**:用户说"上传知识/收集知识/沉淀知识/知识库"等(含同义表达)时触发,同样走「隐私说明 → 选择组件 → 范围卡片」流程

## 调用流程(CLI)

```bash
PY=python3
D=skills/knowledge-collector/scripts

# 1. 查看当前登录用户(确认 member_id / 部门是否排除)
$PY $D/collector.py --member <userid> --action whoami

# 2. 触发判断(是否该提醒 + 隐私说明; 部门排除则拒绝)
$PY $D/collector.py --member <userid> --action trigger

# 3. 同意(WorkBuddy 弹窗选择同意后) → 列出可收集项目
$PY $D/collector.py --member <userid> --session <userid> --action accept

# 4. 勾选范围后, 取专家自抽内容(首次调用, 不上传)
$PY $D/collector.py --member <userid> --session <userid> --action collect --paths "项目路径1" "项目路径2"

# 5. 专家按返回的 schema 抽离卡片 → 回传 JSON 落盘+打包+上传
$PY $D/collector.py --member <userid> --session <userid> --action collect \
    --paths "项目路径1" --cards-json cards.json
```

> member_id 由专家从 `~/.workbuddy/ismartgo_user.json` 的 `userid` 读取;`--session` 传当前会话用户(必须等于 member)。

## 采集范围选项(scan_workspace 输出,所有用户一致)

**WorkBuddy 数据链路(本机)**:空间/工作目录 Workspace/CWD → 会话 Session(workbuddy.db sessions: id/cwd/title)→ 对话流 .jsonl(`~/.workbuddy/projects/<转义路径>/*.jsonl`,文件名=session id)→ 任务 Task(`~/.workbuddy/tasks/<uuid>/*.json`)

**CLI**:`--action select [--time-range week|month|all]`(CLI 缺省 month;但**专家交互时默认显式传 `--time-range all`** 罗列全部,见下方标准流程)

**时间筛选规则(scan_workspace 内实现)**:
- `week`:空间最后活动时间(目录 mtime)距今 ≤ 7 天
- `month`:≤ 30 天(**默认**)
- `all`:全部
- 输出附带 `time_range` 字段标识当前筛选

**返回项(每个空间/任务)**:
- `name` 展示名:单会话目录=会话标题(前端所见),多会话目录=可读名(如 `舒洁`)
- `real_path` 真实路径(取自 db cwd)、`path` 采集用目录
- `sessions[]` 会话明细:标题(db title)+ 时间 + 大小 + **intent 真实意图**(从 jsonl `<user_query>` 提取)
- `size_mb`、`last_activity`、`session_count`
- **已知限制(如实)**:当前 `select` 输出**不含 `is_customer` 字段**——「客户」标注依赖该字段,待后续接入客户(品牌)清单/空间标记后生效;模板渲染时已做字段兼容(有 `is_customer` 则显示客户标,无则不显示,不影响勾选/复制)

**专家交互(范围选择,标准流程,所有用户一致)**:
1. **默认罗列全部**:默认 `--time-range all` 加载用户**全部**空间/任务,不做预筛选、不做精选;仅当用户明确要求缩小范围(如"只看近一个月")才用 `week`/`month`
2. **展示形式:交互式详情卡片(必须,使用固化模板)**:范围列表**必须使用本 skill 固化的交互卡片模板 `assets/scope_picker.html`** 渲染(WorkBuddy 交互组件),**不得现场手写、不得只用纯文本编号列表**。渲染步骤:
   - 运行 `--action select --time-range all` 拿数据
   - 读取 `assets/scope_picker.html`,将其中占位符 `__SCOPE_DATA__` 替换为 select 输出的 JSON(`{"items":[{no,title,path,session_count,size_mb,intent,is_customer,last_activity}...],"time_range":"all"}`)
   - 用 WorkBuddy 交互组件渲染替换后的 HTML 片段
   - 模板内置:顶部时间筛选「近一周/近一月/全部时间」、搜索框、每行勾选框+编号+标题+真实路径+会话数+大小+意图预览、客户标「客户」、底部「复制所选编号/全选当前/清空选择」
   - **复制按钮可靠性(模板已内置三级降级,无需专家额外处理)**:L1 `navigator.clipboard`(安全上下文)→ L2 `execCommand('copy')` → L3 展开只读输入框自动全选提示 Ctrl+C(100% 可用)。点击后必有状态反馈;若用户仍反馈复制失败,引导用户改用 L3 手动复制框,或在输入框内直接手输编号
   - 复制后**提示用户把编号粘贴到对话发送给专家**
   - 无法渲染交互卡片时(环境限制)降级为会话框编号列表,但**必须全部列出、不遗漏**,仍带标题/路径/意图/多选
3. **多选**:≤4 用 WorkBuddy 选择组件;>4 用交互卡片(或编号列表)列出全部,回复编号可多选(逗号/空格/区间)
4. 会话级可进一步勾选(标题/时间/大小/意图)
5. 采集范围传多个 `--paths`(目录或 jsonl)

## 输出结构

```
agents/<member_id>/
├── user_info.md                       # 成员信息 + 采集历史
├── knowledge/<品牌>/<业务线>/<内容类型>/<卡片>.md|json
└── archive/<member_id>_<时间>_raw.zip # 已过滤敏感内容的原始附件(限量20M/200M)
```

## 状态码

| 输出 | 含义 |
|------|------|
| `step: denied` | 越权 / 部门排除,拒绝 |
| `step: trigger` | 提醒判断(should_remind + notice 隐私说明) |
| `step: select` | 返回可收集项目列表(供用户勾选) |
| `step: extract` | 返回待专家自抽内容(含 schema) |
| `step: done` | 完成(rejected 时不上传) |
| `upload.ok` | 上传成功(uploaded 列表) |

## 数据与凭证位置(更新专家包不受影响)

- 本地产物:`~/.workbuddy/knowledge-collect/`(专家包更新不覆盖)
- 登录用户信息:`~/.workbuddy/ismartgo_user.json`(登录成功后自动写入)
- OSS 凭证:专家包**内置默认凭证** `scripts/oss_cred.blob`(混淆存储、不露明文,企业内部分发开箱即用);可用 `~/.workbuddy/oss_cred.blob` 或环境变量 `OSS_CRED_BLOB` 覆盖(作者/运维轮换新 AK 用)

## 依赖

- Python 3.8+
- `oss2`(OSS 上传,`pip install oss2`)
- 敏感词库:`skills/knowledge-collector/sensitive_words.txt`(可扩充)
