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

## 耐用度事件归因

`reliability_fault.json` 的两条 `state=fault` 检出行只是原始检出次数，不含轮组身份、维修交接或故障延续关系；单凭这两行无法裁决 150 次故障／千借用日的型号承诺，报表给出 `indeterminate`。维修补证经独立入口 `service-import` 采录，与原始检查分别留存，后到的维修单不会覆盖先前故障：

```powershell
python -m assistlease service-import --db case.sqlite --file fixtures/reliability_service.json
python -m assistlease report --db case.sqlite
python -m assistlease audit --db case.sqlite
```

维修单采录开始/结束时间、声称动作（仅紧固 `tighten` 或换件 `replace`）、中心与厂家交接签名、厂家回执、取下/装上部件在中心与厂家两侧的实物序列号扫描，以及现场复检。归因只认真实扫描：同一轮组（取下与装上序列号相同）在维修窗口内的复检与原故障构成一次连续事件；实物轮组已换且新轮组再次失效才另开新事件。声称换件但装上扫描仍是旧序列号、中心与厂家交接链序列号冲突、或同一维修的两组新增证据效力未裁明时，结论 `frozen`，不按维修单号机械切段。

借用暴露按已见证的实物交付到实际归还计算（独立中心签收），按单元对日期取并集，同日重叠交付不重复计时；未归还区间在取数点右删失。暴露不依赖 `rental_decision` 状态，追溯的适配资格重裁不会抹除已经发生的九天使用，法律资格在报表中以 `qualification`（`valid`/`review_required`）另列；恢复有效资格后同一区间也不会再次累加。

# Reliability analysis exercise seed

This isolated exercise source adds a simple model-level count of inspection faults and completed-loan exposure days. The exercise fixtures are synthetic. Source text uses LF line endings so quoted evidence matches the checked-out files across platforms.

