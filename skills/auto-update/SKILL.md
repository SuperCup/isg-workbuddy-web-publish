# auto-update — 专家包自动更新

非作者用户使用专家包时,自动检查 GitHub 生产分支(main)是否有新版本,有则自动更新本地安装的专家包。

## 硬性规则(写死,不可违反)

- **作者 userId**:`e266ae24-3f86-4af8-9ca6-b9218cd6845f`(作者本人不触发更新,本地即源码仓库)
- **分支锁定**:非作者用户**仅可获取生产环境(main)**,脚本不接受任何分支参数,严禁切换 test/pre
- 更新来源固定为 https://github.com/SuperCup/isg-workbuddy-web-publish 的 main 分支

## 使用方式

```bash
# 检查是否有更新(输出状态码)
python3 scripts/auto_update.py check

# 检查并执行更新(自动下载解压覆盖本地专家包)
python3 scripts/auto_update.py update
```

## 输出状态码

| 状态码 | 含义 | 处理 |
|--------|------|------|
| `AUTHOR_MODE` | 作者本人使用 | 不更新,本地自行 git 管理 |
| `UP_TO_DATE` | 已是最新版本 | 正常使用 |
| `UPDATE_AVAILABLE` | 检测到新版本 | 执行 `update` 更新 |
| `UPDATED` | 更新完成 | **提醒用户重启 WorkBuddy 后使用最新版** |
| `NO_USER_ID` | 无法识别当前用户 | 保守不更新,正常使用 |
| `ERROR:...` | 网络/解析等错误 | 继续使用当前版本,提示网络异常未自动更新 |

## 更新覆盖逻辑

- 从 GitHub main 分支下载 zip(`archive/refs/heads/main.zip`,公开仓库免登录)
- 解压后覆盖专家包根目录(agents/、skills/、README.md 等)
- 排除 `.git`、`__pycache__`、本地版本记录,不动用户本地数据
- 本地版本记录写入专家包根目录 `.update-version.json`

## 依赖

- Python 3.8+(纯标准库:urllib / zipfile / sqlite3,无第三方依赖)
- 国内网络访问 GitHub 可能较慢,超时后视为"未更新",不影响正常使用
