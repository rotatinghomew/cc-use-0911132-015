# 返工与并行任务下的处置流程瓶颈归因

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `response_feedback_ledger.ledger.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/feedback_cycles -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q response_feedback_ledger tests`。

业务调用：`python3 -m response_feedback_ledger.ledger.cli fixtures/demo.json --database /tmp/response_feedback_ledger-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：parallel_tasks、unfinished_cases、rework_link。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `insight_id`，其余输出包括 `variants, duration_accounting, bottlenecks, comparisons, unresolved`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

## 流程瓶颈分析入口

```
python3 -m response_feedback_ledger.process_insights \
  --input fixtures/analysis/input.json \
  --database <既有处置账本 sqlite3> \
  --output <瓶颈分析 json>
```

该入口为只读分析，不改变指令下发流程，也不产出个人或部门绩效排名。同一输入重复运行得到同一 `insight_id`（由输入指纹决定），输出字节级一致，可与库内既有 replay 链与 closure 证据交叉对账。方法测试：`python3 -m unittest discover -s tests -p test_process_insights.py -v`。

### 口径与守恒关系

- 发生时刻 `occurred_at` 决定流程顺序，可知时刻 `available_at` 决定某次复盘能采用的证据；晚到记录与撤回通过 `visibility_snapshots` 的历史 cutoff 快照解释，主分析按发生时刻重建，不静默丢弃证据。
- 并行：同一实例内任务处理区间按**并集**计时，并行重叠秒数在 `overlap_hours` 单独披露，墙钟历时中只计一次；`work_content = processing + overlap`，即各任务毛处理时长之和。
- 返工：`rework_of` 标识同一步骤的再次尝试，保留回路证据并形成 `*-rework` 变体，不计为新业务实例。
- 历时分层互斥且守恒：`wall_clock = processing + external_wait + queue + undecomposed`，每个实例的 `residual_seconds` 必须为 0；排队细分为受理、环节交接与闭环登记，外部等待保留暂停原因，证据不足或右截尾在办尾巴进入 `undecomposed` 并注明原因，空值不转零。
- 未闭环实例一律保留，以 `as_of` 右截尾，不参与闭环效率统计但保留已观察部分，不允许删除后宣称效率提升。
- 对比仅在闭环且无证据冲突的实例上、按任务复杂度与协作结构分层；单元格样本不足 3 时 `attribution_safe=false`，只描述结构、不做效率归因。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
