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

## 消杀工艺微生物效果采录

`lab-import` 把实验室批次绑定到同一次 sanitation 事件。一个可采信的结论需要三层证据同时成立：

1. **批次与样本身份**：每个 `lab_batch` 绑定 `process_id`、采样者、原始文件 SHA、条码、材质、几何遮挡与原始培养记录；同一 `sanitation.event_id` 的工艺起止日志在 `process_run`。样本身份事件和 `event_id` 一样去重，导入顺序与重复上传不改变结论。
2. **四类对照必须逐批齐全**：`untreated`（未处理阳性对照，识别原本无菌）、`blank`（工艺空白，识别过程污染）、`recovery`（采样回收加标，识别消毒剂残留造成的假阴性）和 `neutralization`（中和对照）。缺一项就不算通过；回收率必须大于 0 且达到 50%；未处理对照本身阴性时整批判 `invalid`，不得宣称更安全。
3. **物理覆盖与逐表面证据**：阴性拭子只对与其材质/几何/单元匹配、且由有效物理工艺到达的同一表面生效。蒸汽遮挡关节必须有同批 `witness_ref` 指向的内侧见证片；UV 只能覆盖外部可见面，不能拿 UV 图像替代内侧见证。名称相同但几何或单元不同的表面不合并；十台助行器必须逐台逐面验证。

由此得到逐表面去除范围：`lab_decision` 记录有效批次与峰值 CFU，`process_claim` 记录 `scope`（`all-surfaces` / `external-visible` / `surfaces:...` / `none`）、`surfaces_verified`、`surfaces_unverified`、依赖的对照和客户可见用途。`lab_state` 取值 `clear / growth / controls-failed / inconclusive / invalid`；controls 失败或缺失时负样本只记待核，不算杀灭。

单个零拭子导入仍然可用（`fixtures/lab_case.json`），但只会得到 `inconclusive` 且 `scope=none`，不会升级成 `verified/all-surfaces`。

`report` 和 `process-report` 对每个工艺回答五项验收输出：

- `source_process`：工艺、方法、授权路线 A/B 与签署采样协议；
- `sample_identity`：批次、采样者、原始文件 SHA、条码与原始培养记录；
- `controls`：四类对照的逐批结果、回收率和依赖关系；
- `removal_scope`：逐表面有效覆盖与去除范围；
- `customer_declaration`：可对客户披露的用途声明。

两条授权路线分别验证，不互相借用证据：

```powershell
python -m assistlease asset-import --db state.sqlite --file fixtures/lab_processes.json
python -m assistlease lab-import --db state.sqlite --file fixtures/lab_route_a.json   # 蒸汽+内侧见证片 -> 整批接触面
python -m assistlease lab-import --db state.sqlite --file fixtures/lab_route_b.json   # UV 外部可见面 -> 仅非接触演示
python -m assistlease process-report --db state.sqlite --process sanitation-1
python -m assistlease audit --db state.sqlite
```

反例 fixture：`lab_recovery_zero.json`（零回收仍全阴性）、`lab_untreated_sterile.json`（未处理对照阴性→试验无效）、`lab_contaminated.json`（工艺空白污染）、`lab_uv_joint.json`（UV 外表阴性不能扩成关节）。这些声明不参与也不改写 `asset.py` 的单元放行或 `rental.py` 的交付/押金结论。

