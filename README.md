# 辅具租借归还采录台

离线采录借用人适配意见、辅具检修消杀状态、租期交付与归还押金责任。三个账本有自己的持久化实体、决定和 CLI 入口。迟到的适配复核或故障检验会改变可交付资格；归还后单元回到隔离状态，旧租借的押金结论保留在历史中。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m assistlease demo --db demo.sqlite
python -m assistlease audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：

```powershell
python -m assistlease fit-import --db state.sqlite --file fixtures/fit_ok.json
python -m assistlease asset-import --db state.sqlite --file fixtures/asset_ok.json
python -m assistlease rental-import --db state.sqlite --file fixtures/rental_ok.json
python -m assistlease report --db state.sqlite
```

对应 `fit_bad.json` 会将承重意见变为不合格，`asset_bad.json` 将检修放行改为故障，`rental_bad.json` 使逾期和损坏合计超过已签押金。原始文件留在 `file_journal`，每事件保留 SHA；重放幂等，异内容事件 ID 拒绝覆盖。`audit` 在空库重放原始文件并比较决策。报告区分租借批准、辅具可交付与押金退款。

首版的治疗师、检修员和签收身份只做离线证据标签；不连接真实医疗、库存或支付系统。

本隔离增强副本增加 `gait-import`，只采集一段助行器握把力数列并报告算术均值，`fixtures/gait_ok.json` 可在原有三个入口后导入。现阶段不划分步相、不关联左右受力或步态与个体扶持设置。

## 步态扶持设置（gait-support-import）

`assistlease/gait_support.py` 在 `gait-import` 之上增加同组试走与逐步相重建。它只读取 `fit.py` 的已批准型号和 `rental.py` 的实际交付/归还窗作为约束，只写扶持路线决定，不改写租借、押金或单元可交付状态。

采录内容：同组试走标识、本人授权、治疗师签认与个体安全硬界、握套类型、左右握把压力、鞋底触地、躯干倾角，以及设备启动周期、序号、设备时间/网关时间和校准证据。

- 按设备启动周期 + 序号排序，用网关时间锚定，重建左/右支撑、摆动、双支撑与转身相；异启动周期的包按实际单元/人/时窗隔离，永不拼进当前人的步相。
- 每相计算峰值握力、左右不对称、前倾与稳定步速；转身横摆从转身相取得。
- 硬界（左支撑峰值 ≤ 120 N、前倾 ≤ 15°、转身横摆 ≤ 8 cm）逐相判定，总体均值不参与安全结论；转身横摆 ≤ 5 cm 才能声明独立完成 12 米，否则须旁站。
- 缺包不被前一相均值补齐，缺相即“不可判定”；漂移校准保留原始读数与采用的换算版本。
- 两条合法路线读取同一组三次试走，产生不同实际参数：88 cm 优先减轻腕痛→`contact_guard`（可完成但转身须旁站）；84 cm 配左手减压握套→`supervised_independent`（可独立完成 12 米）。握套复测缺失时不得仅凭 84 cm 原始均值宣称独立。

```powershell
python -m assistlease fit-import --db state.sqlite --file fixtures/fit_ok.json
python -m assistlease asset-import --db state.sqlite --file fixtures/asset_ok.json
python -m assistlease rental-import --db state.sqlite --file fixtures/rental_ok.json
python -m assistlease gait-support-import --db state.sqlite --file fixtures/gait_support_ok.json
python -m assistlease report --db state.sqlite
python -m assistlease audit --db state.sqlite
```

验收 fixture：`gait_support_ok.json`（两种安全设定）、`gait_support_badpeak.json`（均值合格但左峰值 142 N 触发重设）、`gait_support_no_cover.json`（缺握套复测不可独立）、`gait_support_gap.json`（缺包不判定）、`gait_support_drift.json`（漂移校准不可判定）、`gait_support_crosslease.json`（归还前另一借用人旧包被隔离）。`report` 输出逐相证据、参数、陪同级别与不可判定原因；`audit` 从原始文件重放并逐字比较扶持路线与分析结果。
