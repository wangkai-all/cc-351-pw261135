# 多语言资料目录审计

从零实现的一套本地可运行系统，用**虚构来源站**演示同一份资料的多语言版本在
“撤回译文 → 发布替代版本 → 其他语言未同步更新”这类真实场景下的采集、关系核对
与对外目录发布。

三个服务彼此独立运行、各自持有数据库连接：

| 组件 | 入口 | 职责 |
| --- | --- | --- |
| 采集端 | `python3 -m corpus_audit collect` / `scripts/worker.py` | 发现目录、抓取每种语言版本、保存原始快照 |
| 关系核对器 | `python3 -m corpus_audit relations` | 独立复核“译文↔原文”“替代/被替代”声明 |
| 对外目录 | `python3 -m corpus_audit serve` | 只读网页：原始来源、语言对应、撤回依据、关系证据 |

网页没有任何写接口，只读取已提交的发布记录；写操作全部在独立 CLI 中完成。

## 快速开始

无第三方依赖，仅需 Python 3.11+。

```bash
# 一条命令启动虚构来源站 + 目录网站，并跑完采集/核对/发布
python3 -m corpus_audit demo --once          # 打印各步骤结果后退出

# 或者分别启动三个进程
python3 -m corpus_audit source --port 8100           # 虚构来源站
python3 -m corpus_audit serve  --port 8200           # 只读目录

python3 scripts/worker.py --source http://127.0.0.1:8100 --once
```

打开 <http://127.0.0.1:8200/>。示例数据包含：

* **港湾通行章程**：中/英/法三语，全部正常。
* **信号灯维护手册**：法语旧译文被来源撤回（附撤回理由与依据快照），新的法语
  版本声明替代旧版；英语译文仍基于原文第 4 版（原文已到第 5 版）——这是**译文
  滞后**，不会被当成原文更新。
* **潮汐表说明**、**灯塔登记册**：用于循环、策略冲突与删除等测试场景。

## 关键规则

**译文更新不等于原文更新。** 每个快照区分 `role`（原文/译文）。译文行单独记录
`translationSourceVersion`；摘要与目录中分别标注“依据原文 vN”和“原文当前 vN”。
译文版本上升不会改变原文行的快照、版本或发布状态。

**不用来源声明直接当结论。** 采集端只记录观察结果，关系核对器独立重新抓取
目标页面并比对 `itemId`、语言、角色、版本、声明方向、自身替代、同语言等条件，
把每条关系记为 `verified / rejected / pending`，证据（URL、HTTP 状态、SHA-256、
原始响应存档）一并落库。

**旧任务不能恢复已撤回链接。** 每个关系任务绑定来源快照 id。若文档已有更新的
快照，旧任务直接标记 `superseded`，不抓取、不生效；分析层接受核对结果时还要求
来源与目标快照都仍是当前快照。即使有落后的核对记录被写进库，也不会被用于展示。
（测试 `test_old_relation_task_cannot_revive_withdrawn_link` 覆盖。）

**不沿替代关系成环。** 分析层对全部生效的替代边做环检测。发现环时整组进入冲突
状态，页面列出环上的边和证据。

**冲突策略说明取舍。** `policy` 有两种：

| 策略 | 可用性 | 完整性 |
| --- | --- | --- |
| `quarantine`（默认） | 冲突整组隔离，目录不提供任何语言版本 | 绝不展示可能错误的关系 |
| `independent` | 保留各语言独立记录，资料继续可用 | 只展示已通过核对的关系，冲突关系标注争议 |

页面顶部与资料详情都会明确写出该策略对可用性和完整性的影响。

**修正后只推进受影响资料。** 采集、核对或策略变化只把相关资料的 `dirty_seq`
推进；发布时仅处理 `dirty_seq > published_seq` 的资料，其余保持原修订号。
（测试 `test_fixing_one_item_only_republishes_that_item` 覆盖。）

**失败不是删除。** 抓取失败（5xx/网络）把文档标记为错误但不改成已下架，也不落
新快照；404/410 或页面元数据损坏会让该版本进入“无法确认”，旧快照只作为审计
材料，不当作当前版本。关系目标暂时不可用会让任务进入 `pending` 并重试（上限 3
次），耗尽后状态页可见，需要修正来源后重新采集排队。

## 页面能看到什么

* 首页：可用资料 / 隔离区，语言与角色（原文/译文）徽标，当前策略说明。
* 资料页：策略影响、问题与证据（含可展开的 JSON 证据）、语言对应表、
  替代与撤回链、**撤回依据**（理由、日期、依据快照、HTTP 状态、SHA-256）、
  全部来源快照（来源链接 + 本地抓取原文 + 下载）、最近审计记录。
* `/source/<doc>/<snapshot>`：转义后的抓取原文与完整元数据。
* `/raw/<doc>/<snapshot>`：原始归档字节，以 `text/plain`、`sandbox` CSP 提供，
  不会被当作本站页面执行。
* `/status`：策略、待推进资料、采集问题、排队或耗尽的关系任务、最近运行记录。

## 命令行

```
python3 -m corpus_audit init                     # 初始化数据库
python3 -m corpus_audit collect --source URL     # 采集一轮
python3 -m corpus_audit relations --source URL   # 核对排队任务
python3 -m corpus_audit publish [--policy ...] [--item ID] [--force]
python3 -m corpus_audit policy --set independent
python3 -m corpus_audit inspect ITEM_ID          # 只读输出某资料的分析 JSON
python3 -m corpus_audit serve --port 8200        # 只读目录网站
python3 -m corpus_audit source --port 8100       # 虚构来源站
python3 -m corpus_audit demo                     # 全流程演示
```

`--item` 可重复，用于“只重新发布受影响资料”。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

25 个用例全部使用可操控的本地站点与虚构文件，覆盖：元数据解析与损坏、撤回与
替代并存、译文滞后、译文指向译文、迟到任务、删除页面、临时故障与重试耗尽、
替代环、两种冲突策略、单资料推进、只读并发读、网页证据与安全响应头。

## 目录结构

```
corpus_audit/
  source.py     虚构来源站（JSON-LD 元数据、故障注入 /__admin）
  fetch.py      同源、限长、限时的 HTTP 客户端
  parse.py      HTML/JSON-LD 解析与严格校验
  collector.py  目录发现、快照、关系任务排队
  relations.py  独立关系核对器
  analyze.py    只读分析：语言对应、替代链、环、冲突与影响
  publish.py    核对 + 只读发布（策略在此生效）
  web.py        只读目录网页
  db.py         SQLite 表结构与并发写约定（WAL + BEGIN IMMEDIATE）
scripts/worker.py  长驻 worker：采集/核对/发布分步独立连接
tests/             端到端测试
```

数据默认写入 `data/`（已在 `.gitignore` 中忽略），HTML 原文存档在
`data/raw/` 下按文档与快照编号保存。
