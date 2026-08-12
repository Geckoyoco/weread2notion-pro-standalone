# WeRead2Notion Pro

把微信读书的书架、阅读进度、划线、想法和阅读统计同步到 Notion Pro 模板。

这是 `ryocobb/weread2notion-pro` 的现代化版本。它保留旧 Pro 的多数据库和统计能力，但底层已改为：

- 腾讯微信读书官方 Agent Gateway + `WEREAD_API_KEY`
- Notion API `2026-03-11` Data Source 模型
- 仓库内原生生成 SVG 阅读热力图

不再依赖微信读书 Cookie、CookieCloud、`heatmap.malinkang.com` 或其他原作者私有服务。

## 功能

- 同步书架、作者、分类、封面、简介和豆瓣链接
- 同步阅读状态、进度、累计时长、最近阅读时间和个人评分
- 按章节同步划线、想法和整本书点评
- 在 Notion 中维护日、周、月、年关联统计
- 生成年度阅读热力图并由 GitHub Actions 自动更新
- 支持定时同步和手动完整同步

## 配置

### 1. 准备 Notion

在自己的 Notion 工作区新建一个空白页面（例如“微信读书 Pro”）。创建一个拥有读取、插入和更新内容权限的 Notion Internal Integration，并在该空白页面右上角通过 **连接 / Connections** 把页面共享给它。

原作者的公开 Pro 模板已经失效，本仓库不再依赖它。后台数据库只允许创建在 `NOTION_SYSTEM_PAGE` 指定的独立“系统数据”页面，主页面仅放面向使用者的关联视图。配置后运行 **Actions → Initialize Notion Pro → Run workflow**；重复运行只验证现有结构，不会在主页面新增数据库。

已有旧 Pro 模板仍可继续使用，不会被自动重建。

### 2. 获取微信读书 API Key

前往 [微信读书 Skills](https://weread.qq.com/r/weread-skills) 获取 API Key。

### 3. 配置 GitHub Actions Secrets

在本仓库的 **Settings → Secrets and variables → Actions** 中添加：

| Secret | 必需 | 说明 |
| --- | --- | --- |
| `WEREAD_API_KEY` | 是 | 微信读书 API Key |
| `NOTION_TOKEN` | 是 | Notion Integration Token |
| `NOTION_PAGE` | 是 | 已共享给 Integration 的空白首页（或旧 Pro 首页）链接/ID |
| `HEATMAP_BLOCK_ID` | 否 | 无法自动识别热力图占位块时填写 |

请勿把这些值写入代码、Issue、Actions 日志或聊天记录。

旧配置 `WEREAD_COOKIE`、`CC_URL`、`CC_ID`、`CC_PASSWORD` 已不再使用，可以删除。

### 4. 初始化并首次运行

先打开 **Actions → Initialize Notion Pro → Run workflow**。看到绿色成功标记、并确认空白 Notion 页面出现数据库后，再继续同步。

打开 **Actions → WeRead Pro sync → Run workflow**，勾选 `full_sync` 后运行。完成后再运行 **WeRead Pro statistics**。

后续默认每 6 小时增量同步一次。

## 可选变量

以下 Repository Variables 可覆盖模板名称或热力图颜色：

- `BOOK_DATABASE_NAME`、`CHAPTER_DATABASE_NAME`、`READ_DATABASE_NAME`
- `YEAR`：指定热力图年份
- `ALLOW_REMOTE_DELETIONS`：设为 `true` 时，删除微信读书端已不存在的 Notion 划线/想法；默认 `false` 以避免接口异常导致误删
- `HEATMAP_BACKGROUND`、`HEATMAP_EMPTY`、`HEATMAP_LOW`
- `HEATMAP_MEDIUM`、`HEATMAP_HIGH`、`HEATMAP_TEXT`

## 与旧 Pro 的差异

- 认证从易过期的 Cookie 改为官方 API Key。
- Notion 查询与写入使用 Data Source API，兼容新版 Notion。
- 阅读热力图在本仓库内生成，不经过第三方热力图服务。
- 官方 Gateway 提供按日总阅读时长；旧接口若曾返回“每本书每天的阅读明细”，新版不保证提供。因此总览统计和热力图可恢复，单本书的历史日明细只在接口实际返回时写入。
- 官方接口目前没有稳定提供旧 Pro 的 `beginReadingDate`、`totalReadDay` 和单本 `readDetail.data`。已有页面的旧值会保留；新书的开始阅读时间、单本阅读天数和逐书日明细可能为空。

### 已兼容的字段变化

| 当前 Gateway 字段 | 旧 Pro 兼容字段 | 写入 Notion |
| --- | --- | --- |
| `book.recordReadingTime`（兼容 `readingTime`） | `readingTime`，单位秒 | `阅读时长` |
| `book.progress`，范围 0–100 | `readingProgress` | `阅读进度`，转换为 0–1 |
| `book.updateTime` | `lastReadingDate` / `activityDate` | `最后阅读时间` / `时间` |
| `book.finishTime` | `finishedDate` | `阅读状态` 和 `时间` |
| `book.category` 或 `categories[]` | `categories[]` | `分类` 关系 |
| `newRating`，范围 0–100 | `newRating` | `评分`，转换为 0–10 |
| 整本书评 `star`，范围 1–5 | 个人评分 | `我的评分` |

书籍详情同时兼容字段位于响应顶层或 `book` 对象内；笔记本书籍信息同时兼容顶层和 `books[].book`。首次升级建议手动勾选 `full_sync`，让以前因字段变化漏掉的累计阅读时长和进度重新写入。

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

本项目由 [malinkang/weread2notion-pro](https://github.com/malinkang/weread2notion-pro) 的旧版代码演进而来，并参考腾讯官方 [WeChatReading Skills](https://github.com/Tencent/WeChatReading) 的接口规范。
