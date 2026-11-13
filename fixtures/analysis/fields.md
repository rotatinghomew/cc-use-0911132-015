# 业务演练数据口径

任务依赖描述前置关系，不等同日志顺序；rework_of 指回做原任务的独立尝试。发生时刻决定流程顺序，可用时刻决定该次复盘能看见的证据。event_revisions.withdraw 撤回被 supersedes 指向的事件；case.closed_at 是业务登记，可能与任务证据冲突，须保留未解决状态。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
