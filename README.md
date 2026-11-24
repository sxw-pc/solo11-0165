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

## 清分账

`rental_decision` 的 `refund` 只是应退责任，不代表银行已付。`settlement-import` 采录退款账户授权（`refund_accounts`）、跨租借抵扣授权（`offset_auths`）、付款批次明细（`payments`，route 为 `refund` 或 `offset`）、银行成功/退票/冲正回单（`receipts`，按外部 `flow_id` 去重，同内容幂等、异内容拒绝）与下次租借取消（`cancellations`）。清分按 `request_id` 给出应退、已付、待付、退票恢复、冻结余额、抵扣余额与追回责任，并按银行流水给出归属：

- 回单只归属其引用的付款批次，按批次明细顺序清分；先于指令到达的回单暂挂（`suspense`），不按同额押金任意匹配。
- 每笔成功回单的归属总额不超过其实际入账金额；同一外部流水跨导入批次不重复计入。
- 同一责任在一版结算中只能采用退款或抵扣一条路线，冲突即 `blocked`。
- 迟到的租借争议冻结未发送款项；已完成的银行付款保留为事实，差额转为追回责任（`recover`）。
- 下次租借取消时抵扣余额重新成为待退，不产生第二次已付。

```powershell
python -m assistlease settlement-import --db state.sqlite --file fixtures/settlement_auth.json
python -m assistlease settlement-import --db state.sqlite --file fixtures/settlement_pay_refund.json
python -m assistlease settlement-import --db state.sqlite --file fixtures/settlement_receipt_success.json
python -m assistlease settlement-import --db state.sqlite --file fixtures/settlement_receipt_return.json
python -m assistlease report --db state.sqlite
```

`settlement_*.json` 合成第二笔借还（应退 300）、抵扣授权、9700 汇总成功回单、300 退票、补付批次与迟到争议；`demo` 走通汇总回单加退票再补付的退款路线，`audit` 重放后清分报告一致。

首版的治疗师、检修员和签收身份只做离线证据标签；不连接真实医疗、库存或支付系统。
