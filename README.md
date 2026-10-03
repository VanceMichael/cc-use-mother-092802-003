# 集体草场生态作业与收益分配

协调集体草场生态时序、农机作业、牧草批次和成员收益，把地块边界、休牧禁牧期、
成员股份、农机班组、收割任务、牧草批次、销售成本和公益支出保存为**可追溯记录**。

## 参与方与事实

主要参与方包括嘎查管理人员、合作社成员、农机班组、生态监测人员、财务审核人员。领域资料记录以下已经确认的事实：

- 嘎查统一经营3.1万亩集体草场并推迟收割时间
- 15台现代化收割机开展规模化打草
- 集体收益用于股份分红、优惠牧草供应和代缴医疗保险

## 模块结构

```
src/grassland/
├── ledger.py    # 追加式哈希链账本，JSONL 持久化，重放重建状态
├── errors.py    # 面向录入人员的业务异常
├── service.py   # 全部业务规则与命令
└── explain.py   # 分红/优惠牧草/务工收入的证据链解释
```

核心设计：**只增事件、不改正文**。所有写操作向账本追加一条带哈希链的事件；
`GrasslandService` 每次启动重放整条链重建全部状态，因此离线补录、农机故障、
作业延期、成员资格变化和重启后的未完任务都能完整恢复。

### 资源与决定的覆盖范围

| 你要记录的事 | 入口 |
| --- | --- |
| 地块边界、休牧/禁牧期 | `register_plot` / `impose_restriction` / `lift_restriction` |
| 草籽成熟与生态观察 | `confirm_ecology` |
| 农机、班组 | `register_machine` / `report_machine_down` / `repair_machine` / `register_crew` |
| 成员股份版本、资格变化 | `register_member` / `change_shares` / `member_left` / `member_rejoined` |
| 任务与作业调度 | `open_task` / `delay_task` / `schedule_job` / `start_job` / `interrupt_job` / `resume_job` / `reschedule_job` / `complete_job` |
| 牧草数量守恒 | `weigh_forage` / `record_loss` / `store_forage` / `dispatch_subsidy` / `sell_forage` |
| 成本、公益支出、务工 | `record_expense` / `confirm_expense` / `record_wage` / `confirm_wage` |
| 年度分配 | `draft_plan` / `correct_plan` / `publish_plan` / `adjust_plan` |
| 未完事项恢复 | `unfinished()` |
| 给牧民的解释 | `explain_dividend` / `explain_subsidy` / `explain_wage` |

## 关键业务约束

- **生态优先**：未确认草籽成熟与生态观察、作业早于成熟日、与休牧/禁牧期重叠，均不能排产。
- **资源互斥**：同一地块同一时段只有一个有效作业；同机同组时间重叠同样拒绝（已完工/取消不占位）。
- **数量守恒**：`入库 = 净重 − 损耗`，`结存 = 入库 − 优惠供应 − 对外销售 ≥ 0`。
- **权益版本化**：股份按生效日版本化；退出归零、回迁追加新版本；分配取基准日有效股份。
- **双岗确认与分离**：支出/务工录入人不得确认自己的单据；称重人不能发布含本人分红的方案。
- **方案冻结与调整**：发布前可纠错；发布后只能补发或追缴，原方案行不变。
- **可解释**：每笔分红、优惠草、务工钱都能列出依据了哪些事件（带账本序号）。

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/grassland_context.py` 负责读取和校验这些资料。`docs/domain-rules.md` 详列领域规则。

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件（测试在临时目录读写账本），不需要连接外部业务系统。
