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

## 跨中心调拨

第四个账本 `transfer-import` 采录调拨申请、双中心放行、承运接收（含起运清单与封签）、到站验收与取消/改道证据。调拨决定区分 `pending_release`、`in_transit`、`arrived_quarantine`、`deliverable`、`disputed`、`revoked`，并落账实际放行中心、签收责任（custody）与可交付时间。单元在途或未由目的中心验收时，后到的重复调拨申请直接撤销。到站验收的型号、序列号或附件与起运清单不符即进入责任争议，责任停在最后一次双方有效交接；后续纠错扫描保留在报告中但不掩盖缺件。对已起运实物的迟到故障报告只撤销尚未完成的交付承诺，重新放行产生新裁决版本。

归还隔离的单元允许两条合法路线：`origin_prep`（起运中心重新检修消杀、封签起运，目的中心核验封签即可交付）与 `destination_prep`（隔离状态移交，到站后重新检修消杀再交付）。系统按预约窗、两地整备完成时刻与承运时刻选出能兑现的路线，而不是机械地取最早检修记录。

```powershell
python -m assistlease fit-import --db state.sqlite --file fixtures/transfer_fit.json
python -m assistlease asset-import --db state.sqlite --file fixtures/transfer_asset.json
python -m assistlease transfer-import --db state.sqlite --file fixtures/transfer_ok.json
python -m assistlease asset-import --db state.sqlite --file fixtures/transfer_prep.json
python -m assistlease report --db state.sqlite
python -m assistlease audit --db state.sqlite
```

`transfer_ok.json` 含两中心两条路线：`transfer-1` 本地整备虽早但消杀效期错过班车，系统改选到站整备路线兑现预约窗；`transfer-2` 到站缺件进入责任争议。`transfer_fault.json` 演示迟到故障撤销未完成的交付承诺，`transfer_bad.json` 演示取消/改道证据。

首版的治疗师、检修员和签收身份只做离线证据标签；不连接真实医疗、库存或支付系统。
