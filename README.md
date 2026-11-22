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

## 报废处置账（retirement）

归还后判废的单元走第四条账，不是把 `asset_decision` 改成普通故障：一经技术复核批准报废，单元进入独立的 `retired` 状态，不再被 reservation 锁定。同一台 unit-1 允许两条路线：

- **整机路线**（`retire_whole.json`）：整机交合规回收商，按整机重量出具单一凭证，部件随整机落账 `disposed`。
- **拆件路线**（`retire_partout.json`）：中心先见证拆出合格附件——部件获得独立身份（`unit:part`）、自有检修/消杀资格（不继承整机旧有效期）和去向——再把承力架与危险材料交回收商。逐部件核对重量与接收人，原单元总重量守恒；重复接收或重量不符判 `disputed` 且同一部件只计一次回收量，缺失交割则停在 `pending_handover`。

迟到的技术复核（`retire_late_review.json`）把扶手由可复用改为隐患件时：拆件路线撤回该部件可用资格并转入待回收，危险材料交割量随实际交接更新，但已完成的实物交接不被改写；整机路线的单一凭证按真实收货事实保留。归还损伤责任仍在 `rental_decision` 争议时，实物保持隔离、处置价款冻结在 `retirement_decision.frozen`，报废收入不抵消旧押金裁定。

```powershell
python -m assistlease retire-import --db state.sqlite --file fixtures/retire_partout.json
python -m assistlease report --db state.sqlite
python -m assistlease audit --db state.sqlite
```

单元与部件状态：待复核 `pending_review`、可拆复用 `reusable`、不可复用 `non_reusable`、待交割 `pending_handover`、已处置 `disposed`、争议 `disputed`。所有来源文件进入 `file_journal`，同事件重放幂等、异内容拒绝、任意导入顺序裁决一致，`audit` 从来源重建处置报告。
