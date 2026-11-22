# 辅具租借归还采录台

离线采录借用人适配意见、辅具检修消杀状态、租期交付与归还押金责任。三个账本有自己的持久化实体、决定和 CLI 入口。迟到的适配复核或故障检验会改变可交付资格；归还后单元回到隔离状态，旧租借的押金结论保留在历史中。

适配意见的 `therapist` 字段只是原始文本标签，不参与人员认定。真实签署身份来自仓内 mock 资质公示页（多页 HTML/JSON）与中心值班簿：采集器保留页面原始字节、来源 URL、分页游标、抓取时刻，以及公示的证书编号、人员编号、可签型号范围、生效/注销时刻与公示版本。姓名相同不合并；证书号相同但人员号不同的绑定被隔离，只有与公示页面版本对应的签署更正才能解除。晚抓到的旧缓存页不会替代已生效的吊销公告——归属按公示版本而非抓取顺序解析。签署人认定有两条等效制度路线：原签署设备的双人见证日志，或当班签字原件的双人核验；二者面对同一批页面与含糊旧值班簿可能得出不同归属，系统如实记录证据路线，绝不按姓名、首条检索结果或最近抓取页自动选人。`fit_decision` 只消费经归属且签署时资质有效的意见；报告主结果是每份意见的真实签署人、当时的有效执业范围、待核冲突与引用页面，而非一个 "fit approved" 字段。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m assistlease demo --db demo.sqlite
python -m assistlease audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：

```powershell
python -m assistlease credential-crawl --db state.sqlite --dir fixtures/credential_site
python -m assistlease credential-import --db state.sqlite --file fixtures/credential_roster.json
python -m assistlease credential-import --db state.sqlite --file fixtures/credential_attest_device.json
python -m assistlease fit-import --db state.sqlite --file fixtures/fit_ok.json
python -m assistlease asset-import --db state.sqlite --file fixtures/asset_ok.json
python -m assistlease rental-import --db state.sqlite --file fixtures/rental_ok.json
python -m assistlease credential-decide --db state.sqlite
python -m assistlease report --db state.sqlite
python -m assistlease audit --db state.sqlite
```

对应 `fit_bad.json` 会将承重意见变为不合格，`asset_bad.json` 将检修放行改为故障，`rental_bad.json` 使逾期和损坏合计超过已签押金。`credential_attest_roster.json` 走原件核验路线把 opinion-1 认定给只有训练资质的 T-2，该意见进入待重评清单；`credential_correction.json` 按页面版本解除 CERT-FIT-005 的隔离。原始文件留在 `file_journal`（抓取束含页面原始字节），每事件保留 SHA；重放幂等，异内容事件 ID 拒绝覆盖。`audit` 在空库重放原始页面与原件记录并重建归属、比较决策。报告区分每份意见的归属与资质结论、租借批准、辅具可交付与押金退款。

首版的治疗师、检修员和签收身份只做离线证据标签；不连接真实医疗、库存或支付系统。
