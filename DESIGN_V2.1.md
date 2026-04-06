# paper_reader_v2.1 详细设计方案

## 一、概述

### 1.1 现状问题

当前系统是**单向推送型**：自动抓取 → AI 处理 → 写入 Obsidian → 结束。  
用户反馈无法回流到系统，导致论文越积越多，无法区分**未看**和**不感兴趣**的论文。

### 1.2 改进目标

建立完整的「收集 → 阅读 → 反馈 → 优化 → 清理」闭环：

```
arXiv → 去重 → AI 摘要 → 写入 Obsidian（status: unread）
                                         ↓
                              用户在 Obsidian 中审阅
                              ↓                    ↓
                    标记 interested        标记 rejected
                              ↓                    ↓
                    → deep 深度分析      → cleanup 自动清理
                              ↓                    ↓
                    保留在 vault        删除文件 + 记入 history
                                                   ↓
                                       下次 scan 自动跳过
```

---

## 二、核心改动：三态论文状态

### 2.1 状态定义

| 状态 | frontmatter 值 | 含义 | Obsidian 操作 |
|------|----------------|------|---------------|
| `unread` | `status: unread` | 新论文，尚未审阅 | 默认值 |
| `interested` | `status: interested` | 感兴趣，需要细读 | Properties 下拉选择 |
| `rejected` | `status: rejected` | 不感兴趣 | Properties 下拉选择 |
| `reading` | `status: reading` | 正在阅读中 | Properties 下拉选择 |
| `done` | `status: done` | 已读完 | Properties 下拉选择 |

### 2.2 frontmatter 变更

**修改前：**
```yaml
interested: false
interested_at: ""
```

**修改后：**
```yaml
status: unread         # unread | interested | rejected | reading | done
status_updated: ""     # 状态变更时间（用户手动填或留空）
```

### 2.3 向后兼容

已有论文的迁移策略：
- `interested: true` → `status: interested`
- `interested: false` → `status: unread`
- 提供 `migrate` 命令一键迁移所有旧格式文件

### 2.4 代码改动点

| 文件 | 改动 |
|------|------|
| `obsidian_writer.py` | `_build_markdown()` 中 frontmatter 替换 interested → status |
| `obsidian_writer.py` | `快速操作` 提示文案更新 |
| `deep_reader.py` | `find_interested_papers()` 改为读取 `status: interested` |
| `main.py` | `_list_all_papers()` 读取 status 字段 |
| `main.py` | `cmd_list()` 按状态分组显示 |
| `main.py` | `cmd_cleanup()` 改为清理 `status: rejected` 的论文 |
| `main.py` | 新增 `cmd_migrate()` 迁移旧格式 |
| `post_check.py` | frontmatter 检查加入 status 字段 |

---

## 三、cleanup 命令重新设计

### 3.1 新行为

```bash
# 预览将要清理的论文（status: rejected）
python -m paper_reader_v2.main cleanup

# 确认删除
python -m paper_reader_v2.main cleanup --confirm

# 也清理超过 N 天还是 unread 的论文（不关心的论文自然过期）
python -m paper_reader_v2.main cleanup --expire-days 14 --confirm
```

### 3.2 清理逻辑

```python
def should_clean(paper, expire_days=None):
    status = paper.get("status", "unread")  # 兼容旧格式
    
    # 明确标记 rejected 的一定清理
    if status == "rejected":
        return True
    
    # 超过 N 天仍为 unread 的视为过期
    if expire_days and status == "unread":
        created = parse_date(paper["created"])
        if (now - created).days > expire_days:
            return True
    
    return False
```

### 3.3 清理流程

1. 扫描 vault 中所有论文
2. 筛选 `status: rejected` + 过期 `unread` 论文
3. 预览列表（按分类分组）
4. `--confirm` 时执行：
   - 删除 .md 文件和对应 PDF
   - 将 arxiv_id 记入 history.json
   - 清理空目录

---

## 四、AI 容灾 - Provider Fallback 链

### 4.1 配置格式

```yaml
ai:
  # 主 provider
  provider: "codex"
  model: "gpt-5.4"
  
  # fallback 链：主 provider 失败后依次尝试
  fallback:
    - provider: "raw"
      model: "gpt-4o"
      base_url: "https://models.inference.ai.azure.com"
      api_key: "env:GITHUB_TOKEN"  # 支持环境变量引用
    - provider: "litellm"
      model: "github/gpt-4o"
```

### 4.2 实现方式

```python
def chat_completion_with_fallback(messages, config):
    """带容灾的统一调用"""
    ai_cfg = config["ai"]
    
    # 尝试主 provider
    result = chat_completion(messages, provider=ai_cfg["provider"], ...)
    if result:
        return result
    
    # 主 provider 失败，尝试 fallback 链
    for fb in ai_cfg.get("fallback", []):
        print(f"  [切换] 尝试 fallback: {fb['provider']}/{fb['model']}")
        result = chat_completion(messages, provider=fb["provider"], ...)
        if result:
            return result
    
    return None  # 所有 provider 都失败
```

### 4.3 代码改动

| 文件 | 改动 |
|------|------|
| `llm_client.py` | 新增 `chat_completion_with_fallback()` |
| `summarizer.py` | `summarize_paper()` 改用 fallback 版本 |
| `deep_reader.py` | `ai_deep_analysis()` 改用 fallback 版本 |
| `config.yaml` | 添加 fallback 配置段 |

---

## 五、断点续传 & 批量处理优化

### 5.1 regenerate 内置为子命令

将 `regenerate_summaries.py` 的功能整合进 main.py：

```bash
# 修复所有降级摘要
python -m paper_reader_v2.main fix

# 只修复最近 N 篇
python -m paper_reader_v2.main fix --recent 10

# 修复指定论文
python -m paper_reader_v2.main fix --paper 2604.01234
```

### 5.2 断点续传

通过检测文件内容判断是否需要重新生成（已有 `"未经 AI 总结"` 检测），不需要额外的进度文件。

### 5.3 scan 命令增量安全

```python
# scan 中每成功处理一篇就立即 mark_processed
# 当前已经是这样做的（第 131 行），无需改动
for paper in papers:
    path = write_paper_note(paper, ...)
    if path:
        mark_processed([paper])  # ✅ 已有断点保护
```

---

## 六、Obsidian 交互优化

### 6.1 论文笔记新模板

```markdown
---
title: "Paper Title"
arxiv_id: "2604.01234"
status: unread
status_updated: ""
# ... 其他字段不变
---
# 📄 Paper Title

> [!info] 论文状态
> 在 Properties 面板中修改 `status` 字段：
> - **unread** — 尚未审阅（默认）
> - **interested** — 感兴趣，需要细读
> - **rejected** — 不感兴趣，将被自动清理
> - **reading** — 正在阅读中
> - **done** — 已读完

> **领域**: 📄 通用
> ...
```

### 6.2 去掉无效提示

删除 `Ctrl+Shift+I` 快捷键提示（不存在）。改用 Properties 面板下拉菜单操作说明。

---

## 七、分类系统可配置化

### 7.1 配置格式

```yaml
# config.yaml
categories:
  - name: "量化"
    icon: "🗜️"
    keywords: ["quantization", "int4", "int8", "gptq", "awq"]
  - name: "剪枝"
    icon: "✂️"
    keywords: ["pruning", "sparsity", "sparse"]
  - name: "推测解码"
    icon: "🎯"
    keywords: ["speculative decoding", "draft model"]
  # 用户可自行添加...
```

### 7.2 代码改动

| 文件 | 改动 |
|------|------|
| `obsidian_writer.py` | `classify_paper()` 从 config 读取分类规则 |
| `filters.py` | `core_keywords` 从 config 读取 |
| `config.yaml` | 新增 `categories` 配置段 |

---

## 八、history.json 增强

### 8.1 增加写入锁

```python
import fcntl  # Unix; Windows 用 msvcrt

def save_history(processed_ids):
    with open(HISTORY_FILE, "w") as f:
        # 文件锁防止并发写入
        fcntl.flock(f, fcntl.LOCK_EX)
        json.dump({"processed_ids": sorted(processed_ids)}, f, indent=2)
        fcntl.flock(f, fcntl.LOCK_UN)
```

### 8.2 自动备份

每次写入前自动备份为 `history.json.bak`：
```python
def save_history(processed_ids):
    if HISTORY_FILE.exists():
        shutil.copy2(HISTORY_FILE, HISTORY_FILE.with_suffix(".json.bak"))
    # ... 写入
```

---

## 九、实施计划

### Phase 1：核心闭环 ✅

1. ✅ **三态状态系统** — frontmatter 改 status 字段（5 态：unread/interested/rejected/reading/done）
2. ✅ **migrate 命令** — 迁移旧格式（144 篇全部迁移成功）
3. ✅ **cleanup 重设计** — 基于 status: rejected
4. ✅ **快速操作提示更新** — Meta Bind 插件 `INPUT[inlineSelect(...):status]` 替代无效快捷键

### Phase 2：健壮性提升 ✅

5. ✅ **AI 容灾 fallback 链** — litellm(copilot) → codex，`chat_completion_with_fallback()`
6. ✅ **fix 命令** — regenerate 整合进 CLI，支持 `--limit`
7. ✅ **断点续传** — fix 命令跳过已修复

### Phase 3：体验优化 ✅

8. ✅ **分类可配置** — categories 移入 config.yaml（10 类 + 用户自定义）
9. ✅ **history.json 增强** — 原子写入（temp + os.replace）+ 自动备份 `.json.bak`
10. 🔲 ~~定时运行脚本~~ — 暂不实施

### Phase 4：进阶能力 ✅

11. ✅ **阅读统计面板** — `stats` 命令（状态分布/分类分布/月度趋势/核心指标/兴趣偏好）
12. ✅ **基于反馈的推荐优化** — `build_feedback_profile()` + `score_paper_relevance()` 反馈驱动排序
13. ✅ **Obsidian 统计仪表板** — `dashboard` 命令生成 `_dashboard.md`（Mermaid 饼图/柱状图 + 双链跳转）

### Phase 5：智能反馈闭环 ✅

14. ✅ **动态正反关键词画像** — `build_keyword_profile()` 从感兴趣/淘汰论文提取 bigram/trigram 短语，**差分计算净倾向**（按论文数量归一化，消除正反重叠词），持久化到 `keyword_profile.json`
15. ✅ **细粒度活动日志** — `log_activity()` 在 scan/fix/cleanup/update-keywords/dashboard 完成时自动记录，保留 90 天，存储到 `activity_log.json`
16. ✅ **仪表板全量聚合** — dashboard 聚合所有 Phase 4 统计维度 + Phase 5 新增 4 个区块：
    - 🤖 AI 推荐优先阅读（基于 `score_paper_relevance` 对 unread 打分排序）
    - 🔑 研究兴趣词云（差分净正/净负关键词可视化）
    - 📋 最近活动日志（14 天操作时间线表格）
    - ⚙️ 快捷操作（dataviewjs 交互按钮：刷新仪表板 / 更新关键词 / 扫描新论文）
17. ✅ **update-keywords 命令** — CLI 一键更新正反关键词画像并展示 net 分数分析
18. ✅ **Dataview 动态索引页** — `_unread.md` / `_interested.md` / `_reading.md` / `_done.md` / `_rejected.md` 使用 `dataviewjs` 实时查询，按分类分组展示，在 Obsidian 中打开即自动刷新
19. ✅ **关键词自动回写 config.yaml** — `update-keywords` 命令将 top 5 净偏正短语自动追加到 `config.yaml` 搜索词列表，以 `# --- 自动发现（来自兴趣画像） ---` 标记区域，每次运行替换（幂等），保留原有注释
20. ✅ **仪表板关键词词云实时刷新** — dashboard 每次运行时调用 `build_keyword_profile()` 重新构建画像（而非读缓存），确保数据与论文标注状态同步
21. ✅ **定时自动扫描** — `scheduled_run.ps1` 脚本封装 scan → update-keywords → dashboard 完整流程，注册为 Windows 任务计划程序（`PaperReader_DailyScan`，每天 09:00），自动清理 30 天前日志

---

## 十、文件变更汇总

| 文件 | Phase | 变更类型 | 说明 |
|------|-------|---------|------|
| `obsidian_writer.py` | P1 | 修改 | frontmatter status + Meta Bind 提示 + DEFAULT_CATEGORIES |
| `main.py` | P1-P5 | 修改 | migrate/cleanup/list/stats/dashboard/update-keywords 命令 + 反馈排序 + AI 推荐区 + Dataview 动态索引页 + 关键词回写 config.yaml |
| `deep_reader.py` | P1 | 修改 | find_interested_papers 读 status |
| `post_check.py` | P1 | 修改 | frontmatter 检查 status |
| `config.yaml` | P2-P3 | 修改 | 添加 fallback + categories + litellm 配置 |
| `llm_client.py` | P2 | 修改 | 新增 fallback 调用 + LiteLLM 后端 + drop_params |
| `summarizer.py` | P2 | 修改 | 使用 fallback 调用 |
| `filters.py` | P2-P4 | 修改 | history 增强；分类可配置；反馈驱动排序 |
| `interest_tracker.py` | P4-P5 | 修改 | 新增 build_feedback_profile / score_paper_relevance / build_keyword_profile / load_keyword_profile |
| `filters.py` | P2-P5 | 修改 | history 增强；分类可配置；反馈驱动排序；活动日志系统 |
| `regenerate_summaries.py` | P2 | 删除 | 功能整合到 fix 命令 |
| `scheduled_run.ps1` | P5 | 新增 | 定时自动任务脚本（scan → update-keywords → dashboard），注册为 Windows 任务计划程序 |
