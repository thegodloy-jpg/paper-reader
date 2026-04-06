# 📚 Paper Reader — AI 驱动的论文自动阅读系统

自动从 arXiv、Semantic Scholar 搜索论文，利用 GPT-4.1 等大语言模型生成结构化中文摘要，并写入 Obsidian 笔记库，支持深度分析、关键词画像、阅读状态管理和可视化仪表板。

## ✨ 功能亮点

- **多源搜索**：arXiv + Semantic Scholar + 顶会论文（ICLR/ICML/NeurIPS/AAAI 等 9 个会议）
- **AI 结构化摘要**：使用 GPT-4.1 生成问题-方法-创新点-局限性等结构化中文笔记
- **多模型 Fallback**：主模型失败自动切换（gpt-4.1 → gpt-4o → gpt-5-mini）
- **Obsidian 深度集成**：自动分类、标签、状态管理、词云仪表板、一键操作按钮
- **深度分析**：对感兴趣的论文搜索代码仓库 + AI 生成架构/复现分析
- **关键词画像**：基于阅读行为自动学习兴趣偏好，动态扩展搜索关键词
- **质量检查**：二次检查 AI 生成内容质量，自动修复不合格笔记
- **定时运行**：Windows Task Scheduler 每日 09:00 自动扫描

## 🚀 快速开始

### 安装

```bash
pip install pyyaml litellm
```

### 配置

编辑 `config.yaml`：

```yaml
ai:
  provider: "litellm"
  model: "github_copilot/gpt-4.1"    # 需要 GitHub Copilot 授权

output:
  vault_path: "D:/obsidian-vault"     # 你的 Obsidian 库路径
  folder: "papers"
```

### 运行

```bash
# 扫描新论文并写入 Obsidian
python -m paper_reader_v2.main scan

# 深度分析收藏论文
python -m paper_reader_v2.main deep

# 生成仪表板
python -m paper_reader_v2.main dashboard

# 查看统计
python -m paper_reader_v2.main stats
```

## 📋 全部命令

| 命令 | 说明 | 常用参数 |
|------|------|---------|
| `scan` | 扫描新论文 + AI 摘要 + 写入 Obsidian | `--no-ai` `--dry-run` `--no-check` |
| `deep` | 深度分析收藏论文（代码搜索 + 架构分析） | `--paper ID` `--force` |
| `check` | 检查笔记质量 | `--fix` `--recent N` |
| `fix` | 修复低质量 fallback 摘要 | `--limit N` |
| `dashboard` | 生成 Obsidian 可视化仪表板 | |
| `stats` | 查看阅读统计 | |
| `list` | 查看论文列表 | `--all` |
| `cleanup` | 清理 rejected 论文 | `--confirm` `--days N` |
| `update-keywords` | 更新关键词画像 | |
| `sync-history` | 同步 vault 论文 ID 到 history | |
| `migrate` | 迁移旧版数据格式 | |

## 🏗️ 架构

```
paper_reader_v2/
├── main.py                    # CLI 入口 + 11 个子命令
├── config.yaml                # 全局配置（关键词、AI、分类）
├── llm_client.py              # 统一 LLM 客户端（LiteLLM/Codex/Raw）
├── summarizer.py              # AI 结构化摘要生成
├── deep_reader.py             # 深度分析（代码搜索 + AI 分析）
├── arxiv_fetcher.py           # arXiv API 论文抓取
├── semantic_scholar_fetcher.py # Semantic Scholar + 顶会搜索
├── paperswithcode_fetcher.py  # Papers With Code 抓取（已停用）
├── filters.py                 # 论文过滤与排序
├── interest_tracker.py        # 关键词画像 + 兴趣学习
├── obsidian_writer.py         # Obsidian Markdown 笔记写入
├── post_check.py              # 笔记质量二次检查
├── scheduled_run.ps1          # Windows 定时任务脚本
├── history.json               # 已处理论文 ID 记录
├── keyword_profile.json       # 正反关键词画像数据
└── activity_log.json          # 操作活动日志
```

## 🔍 搜索源

| 源 | 说明 | 状态 |
|----|------|------|
| arXiv | 主要搜索源，50+ 关键词覆盖 LLM 推理优化领域 | ✅ |
| Semantic Scholar | 补充搜索 + 引用数据 | ✅（有速率限制） |
| 会议论文 | 通过 S2 按 venue 检索 ICLR/ICML/NeurIPS 等 | ✅ |
| Papers With Code | API 已迁移至 HuggingFace | ❌ 已停用 |

## 🤖 AI 模型配置

支持通过 [LiteLLM](https://docs.litellm.ai/) 调用多种模型。当前可用的 GitHub Copilot 免费模型：

```
gpt-4.1 / gpt-4o / gpt-4o-mini / gpt-5-mini / gpt-4-o-preview
```

Fallback 链自动降级：主模型连接失败 → Fallback 1 → Fallback 2 → 模板摘要（连续 3 次失败后跳过 AI）

## 📊 Obsidian 仪表板

Dashboard 提供：
- 论文总数、分类统计、状态分布
- 词云可视化（按分类）
- 一键操作按钮（扫描/刷新/更新关键词）
- 扫描按钮带**实时日志输出**（`cp.spawn` 流式显示）
- 状态索引页：未读 / 收藏 / 阅读中 / 完成 / 已拒绝

## ⏰ 定时任务

```powershell
# 注册 Windows 定时任务（每天 09:00）
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-File D:\project\inference\research\paper_reader_v2\scheduled_run.ps1"
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
Register-ScheduledTask -TaskName "PaperReader_DailyScan" `
  -Action $action -Trigger $trigger
```

## 依赖

- Python 3.10+
- `pyyaml >= 6.0`
- `litellm >= 1.0.0`
- Obsidian（可选，用于阅读笔记）
