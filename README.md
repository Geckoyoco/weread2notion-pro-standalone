# WeRead2Notion Pro Standalone

将微信读书的书架、进度、阅读时长、笔记与划线同步到个人 Notion 阅读系统，并生成可切换 2020–2026 年真实数据的年度热力图。

## 当前结构

- `WeRead Pro sync`：同步书籍、阅读状态、进度、笔记与划线。
- `WeRead Pro statistics`：同步日、周、月、年统计，生成热力图并刷新 Notion 嵌入链接。
- `Publish heatmap dashboard`：发布 GitHub Pages 热力图网页。
- `Initialize Notion Pro`：仅在重建或校验后台数据库时手动运行。
- `Tests`：每次代码更新自动执行完整测试。

Notion 主页面只保存展示模块；书籍主库和后台数据库放在独立的“系统数据”页面。热力图只更新已有嵌入块，找不到时会停止更新，不会在主页面自动追加重复内容。

## 自动运行时间

以下时间均为北京时间：

- 书籍、笔记与划线：每天 `02:17 / 08:17 / 14:17 / 20:17`
- 阅读统计与热力图：每天 `02:43 / 08:43 / 14:43 / 20:43`

也可以在 GitHub Actions 中随时手动运行。仅需补某一年热力图时，在 `WeRead Pro statistics` 中填写年份即可，不再使用单独的历史回填任务。

## 必需配置

### Secrets

| 名称 | 用途 |
| --- | --- |
| `WEREAD_API_KEY` | 腾讯微信读书官方 Gateway API Key |
| `NOTION_TOKEN` | Notion Internal Integration Token |
| `NOTION_PAGE` | Reading 主页面链接或 ID |

### Repository Variables

| 名称 | 用途 |
| --- | --- |
| `NOTION_SYSTEM_PAGE` | “系统数据”页面链接或 ID |
| `HEATMAP_DASHBOARD_URL` | GitHub Pages 的 `title-inline/` 地址 |

可选变量：`BOOK_DATABASE_NAME`、`CHAPTER_DATABASE_NAME`、`READ_DATABASE_NAME`、`ALLOW_REMOTE_DELETIONS`、`HEATMAP_BACKGROUND`、`HEATMAP_EMPTY`、`HEATMAP_LOW`、`HEATMAP_MEDIUM`、`HEATMAP_HIGH`、`HEATMAP_TEXT`。

## 使用与维护

1. 日常只需等待两个定时同步任务，无需重新全量同步。
2. 新书或状态偶尔未刷新时，手动运行 `WeRead Pro sync`；只有明确需要重扫全部数据时才勾选 `full_sync`。
3. 修改热力图样式后，GitHub Pages 会自动发布；统计任务会给 Notion 嵌入链接加入版本号，避免缓存旧页面。
4. 不要删除或改名“系统数据”中的关联数据库，也不要把后台数据库移动回 Reading 主页面。

## 本地验证

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

本仓库为独立运行版本，不依赖原作者仓库、CookieCloud 或第三方热力图服务；接口实现参考腾讯官方 [WeChatReading Skills](https://github.com/Tencent/WeChatReading)。
