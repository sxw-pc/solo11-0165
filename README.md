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
# Reliability analysis exercise seed

This isolated exercise source adds a simple model-level count of inspection faults and completed-loan exposure days. The exercise fixtures are synthetic. Source text uses LF line endings so quoted evidence matches the checked-out files across platforms.

