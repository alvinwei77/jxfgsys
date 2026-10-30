# v2 引擎与费率/关系模型 —— 架构与进度记忆文档

> 这份文档是给"未来某次session"（不管是我压缩过后、还是全新开的）快速恢复上下文用的，
> 尽量做到"读这一份就够了，不用再翻聊天记录/重新推导业务逻辑"。每完成一个阶段就更新一次。
> 最后更新：2026-09-18（Phase 1~4 + 费率/关系模型通用化 + 任务#30全面切换引擎，全部完成
> 并验证通过；只剩 任务#31 一次性部署到设备端 这一项，见第7节状态checklist和第13节）。

## 1. 业务链条一句话总结

供货厂/基地 → (调货员，可能无) → 票源方(自带票/我们配票/反向自然人) → 我方户头 → 国企(垫资方) → 钢厂。
核心矛盾：我们自己厂的进项发票额度不够，需要对外找票来配；同时我们自己也需要进项票抵扣销项，
所以又去外部找票证明进项。这就是为什么"票"要单独建模成 `invoice_sources`，且分 `direct`(票源方
直接对国企开票，我方不经手)和`pass_through`(先开给我方，我方再转开给国企，中间留存"票差利润")
两种结算形态。反向自然人开票是这套"进项票源头"的另一种形式，不是独立子系统。

## 2. 现役 vs v2引擎，谁在管什么

**2026-09-18任务#30全面切换后，以下已过时，仅作历史记录保留——现状见第13节。**

- ~~现役路径（app.py/dashboard.js/trips.js 实际在用）：`trips.mode_code`(M1-M5) →
  `calc.py::compute_trip_finance()`，硬编码 if/else~~ →
  **现在**：`app.py::_compute_finance()` 唯一入口，优先 `engine.py::compute_trip_finance_v2_flat()`
  (`trips.template_id` → `flow_templates`+`template_nodes` 通用节点链 + `rate_links` 费率)，
  `calc.py` 只作为template_id补不上时的兜底，正常不会被调用。
- 两者通过 `compare_v2_engine.py`(净利润+留存利润) 和 `compare_v2_flat_fields.py`(全部18个
  FIN_FIELDS字段+展示字段) 逐笔对比校验，**截至2026-09-18，全部87笔车次全字段100%完全一致，
  0差异**。用户明确说过：即使以后出现差异也不代表新引擎错了（可能是老数据本身
  不准），不做硬性断言阻断，摆数据即可，后续他自己跟财务核对。

## 3. v2引擎数据模型（schema.sql 已落地，migrate_v2_templates.py 是幂等迁移脚本）

```
flow_templates      结算流程模板（split_company/split_reverse/service_fee_direct/
                     service_fee_account/service_fee_dispatcher，对应旧M1/M5/M2/M3/M4）
template_nodes      模板节点，有序(seq)，node_key在模板内唯一，靠 calc_rule_type+calc_rule_config
                     算出一个金额，节点间可以互相引用(node:xxx)，也可以引用车次原始字段(trip:xxx)
trip_node_values    车次逐节点计算结果快照(预留，目前引擎是实时算不是从这里读，表已建但还没接入)
rate_links          费率/关系档案，见下节，取代了当天建了又废弃的旧版 rate_schedules
trips.template_id   新增列，与 mode_code 并存过渡；MODE_TO_TEMPLATE_CODE 定义1:1映射
```

`calc_rule_type` 10种（固定枚举，用户明确要下拉选不要表达式引擎），详细语义见 `engine.py`
顶部文档字符串，不在这里重复。

## 4. 费率/关系档案 rate_links（2026-09-18，本轮改造的核心）

**设计动机**：一开始做的是单实体+5个固定scope的 `rate_schedules`（垫资费率/结算点位/调货员
两种提成/服务费单价，各自只能挂一个实体id）。用户当场指出三个问题并促成了这次通用化改造：
1. 服务费40元/吨不是全局固定的，是要按**票源方**区分的；
2. 调货员提成除了分时间段，还要能按**票源**细分（同一调货员配不同票源提成可能不同）；
3. 更根本的，他想要"更自由化的连接关系"——不希望被5个预设scope锁死。

**方案**：`rate_links` 表把费率建模成"两个实体之间一条带生效时间段的连接"：
`relation_type`(费率/关系类型代码) + `primary_entity_type/id` + `secondary_entity_type/id`(可空)
+ `rate_value` + `rate_unit`(per_ton/pct) + `effective_from/to`。单实体属性(如户头垫资费率)
secondary留空；两实体关系(如调货员+票源的提成)两个都填。

**取数三级兜底**（`engine.get_effective_rate`）：
1. relation_type + primary + secondary 精确匹配该日期生效的记录
2. 找不到→退化成 relation_type + primary，不管secondary（"该实体不分维度的通用费率"）
3. 找不到→退化成 relation_type 本身，primary/secondary都不限定（"该费率类型的全局兜底值"，
   比如历史上服务费统一40元/吨、不挂具体票源时落在这一级）
4. 还找不到→0（与迁移前"没配置就是0"的行为一致）

新增一条时用 `engine.upsert_rate()`，自动把同 relation_type+primary+secondary 的旧"至今生效"
记录截止到新记录生效日前一天，用户只需要填"新费率+从哪天开始"。

**当前 `RELATION_TYPE_CATALOG`**（`engine.py`里，前端表单靠这个目录动态生成primary/secondary选择器）：

| relation_type | 中文名 | primary | secondary | 单位 | 当前种子值 |
|---|---|---|---|---|---|
| account_advance_fee | 户头垫资费率 | account | - | 元/吨 | 23(华西-欧冶户头，开放生效) |
| account_external_settlement | 户头对外结算点位 | account | - | % | 14(同上) |
| dispatcher_commission_peiPiao | 调货员配票提成(M1/M5) | dispatcher | invoice_source(可选) | 元/吨 | 林凯: 5→0(2026-09-05起)，不分票源的通用档 |
| dispatcher_commission_daiPiao | 调货员带票提成(M2/M3/M4) | dispatcher | invoice_source(可选) | 元/吨 | 林凯: 15，不分票源的通用档 |
| service_fee_gross | 服务费单价(按票源方) | invoice_source | - | 元/吨 | 40，全局兜底档(primary_entity_id留空) |

目前"按票源细分"的具体数字用户还没给，都还是种在"不分secondary的通用档"这一级，等业务上有
具体数字了，去「费率与关系」页面新增一条挂到具体票源上即可，会自动优先于通用档/兜底档生效。

**实体类型枚举**(`engine.ENTITY_TABLES`)：account/dispatcher/invoice_source/processing_base/steel_mill，
一一对应 accounts/dispatchers/invoice_sources/processing_bases/steel_mills 五张档案表。

## 5. 「费率与关系」页面(2026-09-18新增，/rate_links)

- 上：业务关系图，纯手写SVG(不依赖外部库，离线本地运行的一贯约定，参考app.js::renderBarChart)，
  按实体类型分5栏(供货厂→调货员→票源→户头→钢厂)，只画"当前生效"的连接，rate_links的二元关系
  画成两节点间的边，一元属性画成节点下方的灰字标签，全局兜底档单独一行文字列在图下面。
  数据源：`GET /api/rate_links/graph`（同时也把 accounts.mill_id / invoice_sources.bound_base_id
  / bound_mill_id 这些本来就有的静态归属关系一起画出来，不止rate_links）。
- 下：列表增删改（用户明确要求"修改和编辑还是list"，不要拖拽/图形化编辑）。"新增后续费率"按钮
  预填同一对象，调 `POST /api/rate_links/upsert`(自动截止旧记录)；"编辑"直接改这条记录本身
  (走通用 `PUT /api/rate_links/<id>`，不触发自动截止，用于改错别字这种)。
- API: 基础CRUD复用 `app.py::ARCHIVE_TABLES["rate_links"]` 走通用 archive_collection/archive_item；
  另加 `GET /api/rate_links/relation_types`(返回目录) / `POST /api/rate_links/upsert` / `GET /api/rate_links/graph`。

## 6. 已知的历史小怪癖（v2引擎忠实保留，没有"顺手修复"）

- `ticket_cost`(买票成本) 字段有记录/展示，但新旧两个引擎的 net_profit 计算里都**没有真正扣减它**。
  这是旧引擎本来就有的行为，用户说了"你看着来，后期跟财务一一核对"，所以v2引擎照原样保留，
  没有自作主张"修复"。以后真要改，需要用户明确确认。
- M4(service_fee_dispatcher) 历史上从未真正使用过(0笔)，公式跟M2/M3完全相同，只是业务归类不同。

## 7. 阶段状态

- [x] Phase 1 核心引擎：flow_templates/template_nodes通用节点引擎，87笔100%对比通过
- [x] 费率/关系模型通用化：rate_links + 关系图页面（比Phase 1原计划的"费率档案"做得更通用）
- [x] Phase 2 两票三款通用化（2026-09-18完成）：见第9节详细说明
- [x] Phase 3 车次进度节点化（2026-09-18完成）：见第11节详细说明
- [x] Phase 4 统计可视化重做（2026-09-18完成）：见第12节详细说明
- [x] 全面切换 mode_code→template_id，下线 calc.py 调用（2026-09-18完成）：见第13节详细说明
- [x] 一次性部署全部改动到设备端（2026-09-18完成，见第10节部署方法论）
- [x] 首页总利润INNER JOIN漏未固化车次的bug修复（2026-09-19完成）：见第14节详细说明
- [x] 费率与关系图跨列连线bug修复 + 链条规则(可编辑多图+票/财务路径)新页面（2026-09-19完成）：
      见第15节详细说明

## 9. Phase 2：两票三款通用化（2026-09-18完成，细节记录）

**背景**：原来是5个写死字段(trips.invoice_out_no/amount, invoice_in_no/amount, invoice_in_stock)
+ payments.pay_type(3个固定值)，只服务pass_through票源(益周)这一种场景。

**新增的3个"单据追踪"节点**（split_company/split_reverse两个模板都有，用户已确认对应关系）：
- `invoice_out_to_soe`(销项发票，我们→国企) / `payment_in_from_soe`(销项款，国企→我们)：
  calc_rule_type='ref_value'（新增的第11种规则类型，直接照抄引用值不做变换），
  金额基准 = `node:give_customer`（用户确认：就是结算金额按户头对外点位扣完那个数）。
- `downstream_payment`(下游款，前端→实际供货客人)：calc_rule_type='manual'，trip_fields=[]恒为0，
  纯追踪节点，没有可计算基准，用户确认这样设计。
- 加上Phase1已有的 `front_invoice_amount`(前端开票金额，doc_types=invoice) 和
  `front_actual_paid`(前端实付，doc_types=payment)，一共5个doc_types非空的节点，
  跟原来UI的"两票三款"(销项发票/销项款/进项发票/进项款/下游款)严格一一对应。

**存储**：`trip_node_values`表新增 doc_no/actual_amount/doc_status 三列（发票号/实际发票金额/
状态如'in_stock'）；`payments`表新增 node_key 列（取代原 pay_type 的3个固定值，任意
doc_types含payment的节点都能用，不再局限于pass_through）。旧列(invoice_out_no等)和pay_type
列本身没删，只是新逻辑不再读写，符合本项目一贯"不做破坏性删除"的原则。

**显示条件**：用户明确要求继续绑定"票源方settlement_type=pass_through"（不是模板固定），
所以前端 `isPassThrough` 判断逻辑没变，变的只是"显示什么内容"——从写死5个字段改成读
`GET /api/trips/<id>` 返回的 `doc_panels` 数组（后端 `app.py::_compute_doc_panels()` 按
trip.template_id查template_nodes里doc_types非空的节点，实时调engine.compute_trip_finance_v2
拿基准值，返回给前端动态渲染）。

**过渡期兼容**：由于全面切换引擎(任务#30)还没做，现网建/改车次时trips.template_id可能是空的。
为了让Phase2在切换引擎之前就能用，`app.py::_sync_template_id()` 在 create_trip/update_trip 里
只要payload带了mode_code就顺带同步一次template_id(通过engine.MODE_TO_TEMPLATE_CODE)，
这不影响净利润计算（仍是mode_code+calc.py在算），只是让doc_panels能正常工作。

**API**：`PUT /api/trips/<id>/node_docs/<node_key>` upsert单据字段；`POST /api/trips/<id>/payments`
新增node_key参数。前端 `static/js/trips.js` 的两票三款卡片、付款记录表、付款类型下拉，
全部从硬编码字段改成遍历 `doc_panels`（class=docno-input/docamt-input/docstatus-input + data-node
属性统一绑定）。已用Playwright对真实益周车次(#45)做过端到端验证：录入发票号/金额/库存状态、
记一笔打款，刷新后核对表格差额正确归零，数据正确持久化。

## 11. Phase 3：车次进度节点化（2026-09-18完成，细节记录）

**设计取舍**：没有新建状态机/没有改动现有6态 `trips.status`（那个仍是list筛选/dashboard统计
用的主状态，改动面太大且用户没要求废弃它）。而是把"到了什么阶段"做成一份**纯只读、从已有数据
实时算出来**的检查点清单——不需要用户手动勾选，也不需要新的"进度"字段本身。

**检查点组成**（`app.py::_compute_trip_progress()`）：
1. 已报备：车次存在即true
2. 已到厂：`status` 在 STATUS_LIST 里的顺序 >= 'arrived'
3. 过磅单：`trip_attachments` 里有 category='weighing_slip' 的记录
4. 结算单：`trips.settlement_doc_no` 非空
5. 每个 doc_types含"invoice"的节点一条：该节点在 `trip_node_values.doc_no` 是否已填
6. 每个 doc_types含"payment"的节点一条：`payments` 里该 node_key 的打款合计 vs 该节点的
   baseline(应收应付基准)——done(全额)/partial(打了一部分，黄色半勾)/未打
7. 已结案：`status == 'closed'`

**详情页**：状态pipeline下方新增一行只读徽标(`.progress-checklist` / `.progress-item`)，
绿色✓=完成，黄色½=部分完成(仅打款类)，灰色·=未完成，鼠标悬停在打款类徽标上显示"应付X已打Y"。

**列表页**：新增"进度"列，用紧凑徽标显示"磅单✓/结算单✓/发票2/2/打款1/3"，其中发票/打款
徽标只有split家族(M1/M5，有doc_types节点的模板)车次才会出现，M2/M3/M4车次没有(符合原设计，
两票三款本来就只服务split家族)。为了不逐行查询template_nodes拖慢列表(现500行上限)，
`list_trips()` 的SQL用了几个相关子查询(EXISTS + COUNT)一次性批量算出，没有N+1查询。

**已验证**：Playwright截图确认列表列渲染正常、详情页9个检查点(该车次为split_company+pass_through)
全部按真实数据状态正确显示(已报备/已到厂/过磅单/结算单为绿勾，5个发票/打款节点按实际录入
情况显示)。

## 12. Phase 4：统计报表可视化重做（2026-09-18完成，细节记录）

**背景**：原「统计报表」页(/reports)只有4张stat卡片+3张纯表格(按钢厂/供货厂/调货人)，
没有图表，也没有"利润构成"这种能看出钱花在哪的展开明细——跟首页看板(dashboard.html)
早就有的"利润构成明细"(A+B+C=结果式公式展开)不是一套东西，领导要看结构性的钱去哪了，
纯净利润一个数字看不出来。这就是任务清单里"给领导看的可视化"要补的部分。

**改动范围**：

1. **口径统一 + 通用化**(`app.py`)：把原来 `_period_trip_stats()`(只服务首页"按钢厂周期"
   这一个场景)里"逐车次拆解+累加"的逻辑拆成三个通用函数：
   - `_empty_stats_bucket(name)`：零值聚合桶，字段跟 `_trip_breakdown()` 的产出(节点化
     拆解：service_fee_income/dispatcher_commission_cost/ticket_cost/other_cost/
     reward/advance_fee等)一一对应；
   - `_accum_bucket(bk, t, b)`：把单笔车次的 `_trip_breakdown()` 结果累加进桶；
   - `_finalize_bucket(bk)`：收尾算 reward_avg_price。
   `_trips_stats(conn, trips)` = 任意一组车次的完整聚合(新增，给"总计"这种单一聚合用)；
   `_period_trip_stats()` 现在只是"查某钢厂某周期的车次 + 调 `_trips_stats`"的薄封装，
   行为完全不变(首页看板没有任何改动)。**这三个函数现在是"任意维度的利润构成汇总"的
   唯一实现，首页周期卡片和统计报表页共用同一份口径**，不会出现"两处算法不一致"。

2. **`GET /api/reports/summary` 扩了两个维度、每个维度都从4字段汇总升级成完整利润构成**：
   - 原有 `by_mill`/`by_base`/`by_dispatcher` 保留，但现在每条记录不再只有
     trip_count/tonnage/settlement_amount/profit 4个字段，而是带完整的
     `_empty_stats_bucket` 字段集(可以直接喂给前端 `profitFormulaHtml()` 展开)；
   - 新增 `by_invoice_source`：按票源方(invoice_sources)汇总——这是Phase2/rate_links
     改造后票源方成为一等维度(服务费/留存利润都挂在票源方上)，之前的报表完全没有这个
     切面，属于本次"基于新节点化数据模型"补的核心维度；
   - 新增 `by_path`：固定两桶(`PATH_BUCKET_DEFS`)，"路径1/3配票反向(差价模型,M1/M5)"
     vs "路径2带票带货(服务费模型,M2/M3/M4)"，即使某条路径在当前筛选范围0车次也展示，
     方便直接对比两种结算模式的结构差异；
   - `totals` 字段也从4个扩到完整利润构成集合。
   实现上是单次遍历 `trips`，每笔算一次 `_trip_breakdown(conn, t)` 后累加进5个维度的桶，
   不是分别查5次，性能上跟原来的实现量级一致。

3. **前端**(`static/js/app.js` / `dashboard.js` / `reports.js` / `templates/reports.html`)：
   - `profitFormulaHtml(m, prefix)` 从 `dashboard.js` 挪到 `app.js`(每页面都加载)，
     成为跨页面共享函数——`reports.js` 直接复用，不重复实现，也保证公式/口径不会跑偏；
   - 报表页新增：总计的"利润构成明细"折叠面板(复用 `profitFormulaHtml`)；按钢厂/
     按调货人利润的SVG柱状图(复用 `app.js::renderBarChart`，跟首页"近14天送货吨位趋势"
     图是同一份手写SVG实现，不引入任何外部图表库)；新增"按票源方汇总"表格；新增
     "按结算路径汇总"两张对比卡片(各自带完整利润构成明细展开)。
   - 原有"按钢厂/按供货厂/按调货人"三张表格渲染逻辑(`renderBreakdown`)不用改，因为
     后端返回对象是"超集"(多了字段，没删字段)，`renderBreakdown`只读它用到的5个字段。

**已验证**：Playwright对 `/reports` 和首页 `/`(确认没有因为挪 `profitFormulaHtml` 到
app.js而破坏首页) 两个页面截图+控制台错误检查均通过(0条console error)；`/api/reports/summary`
接口直接调用核对过字段结构和分桶求和是否等于总计(by_mill/by_invoice_source/by_path三个维度
分别求和trip_count都等于总计的87笔，由目前唯一一家钢厂87笔真实数据验证)；操作全程在
`/tmp/` 临时DB副本上进行，之后核对真实dev库 `trip_node_values`/`payments.node_key` 行数
仍为0(未被测试污染)，并重跑 `compare_v2_engine.py` 确认87/87依然100%一致。

## 13. 全面切换引擎（2026-09-18完成，任务#30，细节记录）

**这是v2改造的最后一次"算钱逻辑"变更**：切换前，net_profit等所有财务数字仍然是
`mode_code` + `calc.py`(硬编码if/else)在算，v2引擎只是并行验证、不影响现网；切换后，
`engine.py`(`template_id` + 节点链 + rate_links)是唯一实际生效的计算路径，`calc.py`
保留在代码里但只作为"template_id怎么都补不上"这种极端脏数据兜底，正常情况下不会被调用。

**engine.py 新增的核心函数**：
- `_run_template(conn, trip, overrides)`：从原 `compute_trip_finance_v2` 里抽出来的"跑一遍
  节点链"通用逻辑，两个对外函数共用，避免重复实现。
- `compute_trip_finance_v2_flat(conn, trip, overrides)`：**app.py现在唯一调用的计算入口**。
  在通用节点值(`node_values`，字典，key是node_key)基础上，按模板的 `calc_family`
  ('split'/'service_fee')把值重新整理成跟 `calc.compute_trip_finance()` 返回字典**字段名/
  字段含义完全一致**的"扁平视图"(`give_customer`/`advance_fee_cost`/`service_fee_income`/
  `dispatcher_commission_cost`/`reward_profit`/`other_cost`/`net_profit`/`rate_out`/
  `rate_in`/`ticket_cost`/`service_fee_type`/`service_fee_value`等，即`calc.FIN_FIELDS`
  全部18个 + 若干仅展示用字段)，这样 app.py/dashboard.js/trips.js/导出台账完全不用改
  读取字段的代码，只是数字的产生路径换了。
- 修了一个"节点通用循环"发现不了的点：`dispatcher_commission`节点是前端UI唯一暴露"手工
  改这一车费率/金额"入口的节点(trips.js"调货人提成(元/吨)"输入框，对应override key
  `dispatcher_commission_rate`/`dispatcher_commission_cost`)，`_run_template`的节点循环
  对`node_key=="dispatcher_commission"`单独拦一道支持这两个override(rate优先于cost，跟
  calc.py语义完全对齐)，其余节点不需要这种特殊处理。

**app.py 改动**：新增 `_ensure_template_id(conn, trip)`(算钱前兜底：万一某条车次的
template_id因历史原因没同步上，临时按mode_code补一次)和 `_compute_finance(conn, trip,
overrides)`(唯一对外入口：优先engine.py，补不上template_id才退回calc.py)。原来6处直接调
`calc.compute_trip_finance(...)`的地方(`_fetch_trip_full`/`preview_trip_finance`/
`save_finance`/`_trip_profit`/`_trip_reward`/`_trip_finance_full`)全部改成调
`_compute_finance(...)`，`calc.save_trip_finance()`(写`trip_finance`表)不用改——它只认
`calc.FIN_FIELDS`这份字段清单，不关心字典是哪个引擎产出的。

**顺手修的一个既有小bug**(跟切换本身无关，是全字段对比时发现的)：`trip_finance`表从来
没有`calc_type`列(纯展示字段，不落库)，`_fetch_trip_full`对**已固化**车次直接
`dict(finance_row)`时一直缺这个字段——导致所有已固化的路径1/3(配票/反向)车次，车次详情
的"利润分项"表格会因为`fin.calc_type !== 'split'`误判走成路径2(带票带货)的展示模板(表格
内容跟实际计算路径对不上)。这个bug在切换引擎之前就存在，不是这次改造引入的，顺手在
`_fetch_trip_full`里按`SPLIT_MODE_CODES`补上了`finance["calc_type"]`。

**验证方法**(比之前几个阶段更严格，因为这次直接改了"钱怎么算"这条路径)：
1. `compare_v2_flat_fields.py`(新增脚本)：对真实87笔车次，逐字段(FIN_FIELDS全部18个 +
   `calc_type`/`give_customer`/`front_actual_paid`等展示字段，一共26个字段)对比
   `calc.compute_trip_finance()` vs `engine.compute_trip_finance_v2_flat()`，**87笔全字段
   100%一致**，不只是net_profit一个数字；
2. 额外写了一版override矩阵测试(4种mode_code代表车次 × 9种override组合，包括
   `dispatcher_commission_rate`/`dispatcher_commission_cost`/`ticket_cost`/
   `service_fee_type`+`service_fee_value`/`reward_unit_price`/空字符串override等边界情况)，
   **全部一致**(初次跑出24处差异，全部是`dispatcher_commission`override没接上，加上面
   那段特殊处理后清零)；
3. Playwright端到端：`/tmp/`临时DB副本上，打开一个已固化M1车次详情确认"利润分项"表格显示
   正确(且calc_type显示split，验证了上面那个bug fix)；打开一个未固化M1车次(#73)，把
   "调货人提成(元/吨)"改成8点"重新试算"，净利润从831.45变556.09(跟Python直接调用
   `engine.compute_trip_finance_v2_flat`算出来的完全一致)；点"固化快照"锁定，核对
   `trip_finance`表持久化的`dispatcher_commission_rate=8.0`/`net_profit=556.09`等字段正确；
   首页/报表/费率关系/报价单/基础档案5个页面全部截图+控制台错误检查，0条console error；
4. 全程在`/tmp/`临时DB副本上做写操作(锁定测试)，之后核对真实dev库`trip_finance`/
   `trip_node_values`/`payments.node_key`均未被污染，并用Flask test_client对真实dev库
   做了一轮只读smoke test(GET不涉及写操作)，确认现网真实87笔车次数字连一分钱都没变
   (net_profit数值逐笔跟切换前的历史记录比对一致)。

**calc.py 现状**：保留全部代码未删除(项目一贯的"不做破坏性删除"原则)，`import calc`还在
app.py/engine.py里，但正常调用路径下不会再被执行——只有理论上"车次mode_code为空或不在
M1-M5映射表里"这种不应该出现的脏数据才会触发`_compute_finance`里的兜底分支。

## 10. 部署状态提醒（已完成，2026-09-18）

**任务#31(一次性部署到设备端)已完成**，按用户"后面的阶段一起做了吧，完整后一次性部署"的
要求，Phase 2/3/4 + 费率关系模型通用化 + 任务#30全面切换引擎，全部在云端验证通过后一次性
部署，没有中途零散部署。部署过程：

1. **部署前逐文件diff审计**：先把设备端当前的app.py/calc.py/schema.sql/db.py/全部
   templates+static文件都拉回云端，跟云端版本逐行diff，确认"设备独有、云端没有"的每一行
   代码，都能在这份文档的Phase记录里找到对应解释(即纯粹是"云端后来的版本取代了这段旧代码"，
   不是设备端独立做过、云端没同步回来的热修复)——calc.py两端完全一致(0差异)，其余文件的
   差异全部是本轮v2改造本身，没有发现任何会被覆盖丢失的设备独有改动。
2. **对设备真实数据库做隔离dry-run**：把设备`data/scrapmetal.db`(87笔车次，mode_code分布
   跟云端dev库完全一致)拉一份到云端临时目录(不是设备本体)，在这份拷贝上跑
   `migrate_v2_templates.py` + `compare_v2_engine.py` + `compare_v2_flat_fields.py`，
   确认对**设备的真实数据**也是87笔全字段100%一致，才碰设备本体。
3. **checksum校验部署代码文件**：18个文件(app.py/schema.sql/db.py/engine.py/
   migrate_v2_templates.py/两个compare脚本/本文档/4个模板/6个静态资源)通过
   `device_commit_files`写入设备，写入后在设备端用`md5sum`逐个核对，18个文件checksum
   跟云端源文件完全一致。
4. **在设备本体上执行迁移**：`migrate_v2_templates.py`自带的备份步骤生成了
   `data/scrapmetal.db.bak_before_v2_templates_20260918_172638`，迁移后设备DB的
   template_id分布跟云端dev库完全一致(`{'service_fee_account': 8, 'service_fee_direct': 6,
   'split_company': 68, 'split_reverse': 5}`)。
5. **设备本体二次验证 + 只读冒烟测试**：`compare_v2_engine.py`/`compare_v2_flat_fields.py`
   直接在设备真实DB上重跑，87笔全字段100%一致；用Flask test_client对设备真实app.py+真实DB
   做了一轮只读GET请求(首页/车次列表/报表/费率关系/报价单/基础档案6个页面 + 车次详情API +
   报表汇总API)，`/api/reports/summary`返回的总利润`153265.48920999985`跟云端dev库验证时
   完全一致的数字，确认设备端现在跑的就是新引擎、且数字没有任何变化。

**用户下次使用前需要重新启动一次系统**（双击"启动系统.command"，或如果程序还在运行先退出
再重新打开）才能加载这次更新的代码——这次部署只更新了磁盘上的文件，没有、也无法远程重启
用户Mac上实际运行的Flask进程。

## 14. 部署后发现的问题（2026-09-19，用户报告，已修复并重新部署）

用户提问"结算单显示未出/首页总利润对不对"，排查后发现根源都是同一批数据：
**车次#73~87**（`import_20260915_0917_batch.py` 2026-09-18批量导入的15笔9/15~9/17新车次），
该脚本只调用了 `POST /api/trips/<id>/status` 把状态设成"已结算"，**从未**：
1. 填写 `settlement_doc_no`(结算单号)——源数据表(`TRIPS`常量)里本来就没有这个字段，不是
   导入脚本漏映射，是当时录入时真的没给单号。这15笔车次的"结算单"列显示"未出"是**真实的
   数据缺口**，不是bug：`_compute_trip_progress`/`trips.js`都是老老实实读`settlement_doc_no`
   这一个字段，没读到就是没读到。**需要用户后续把实际单号补录到这15笔车次里**（车次详情页
   "结算单号"输入框），不是我能替用户编的。
2. 从未调用 `POST /api/trips/<id>/finance` 固化/保存过一次利润快照——导致这15笔车次在
   `trip_finance`表里完全没有行。

第2点牵出一个**真实的既有bug**（跟这次v2改造本身无关，是首页`/api/dashboard`路由自己的问题，
`app.py`原915-920行）：首页"总利润(不含留存)"是`SELECT SUM(net_profit) ... FROM trip_finance
f JOIN trips t`，**INNER JOIN**导致任何"没固化过快照"的车次直接从总和里消失，不会像
`_trip_profit`/`_trip_breakdown`(reports.html/mill_periods用的那套)一样对未固化车次实时
现算——结果首页总利润显示119,621.39，实际(87笔全部现算)应该是153,265.49，**少算了
33,644.10**(正好是这15笔车次的净利润总和)。

**修复**：把这处SQL改成调用 `_trips_stats(conn, 全部非作废车次)`(Phase 4引入的同一份聚合
函数，报表页/周期卡片都在用，未固化车次会走`_trip_finance_full`→`_compute_finance`实时算，
不会漏)，口径统一。已验证云端dev库和Mac设备端修复后都正确返回153265.49，并部署到设备端
(只改了app.py一个文件，checksum已核对)。

**关于"结算单未出"的后续更正（2026-09-19，用户明确业务口径，已改代码）**：第1点当时的结论
（"这15笔要用户补录单号，显示未出是真实数据缺口"）**判断错了**。用户明确："车次管理中，
状态已结算都是已出结算单的"——业务上正是**拿到钢厂结算单才会把状态点成"已结算"**，所以
status本身就是"结算单已出"的证据；`settlement_doc_no`只是"有没有把单号录进系统"，没录
不等于单没出。据此把判定口径从"只看单号"改成"状态≥已结算 或 已录单号"，改动3处：
- `app.py::_compute_trip_progress` 的 `settlement_doc` 检查点
- `static/js/trips.js` 列表"结算单"列 → 抽成 `settlementDocPill(r)`
- `static/js/trips.js::progressBadges` 的"结算单✓/-"徽标 → 统一走 `settlementDocIssued(r)`
效果：87笔非作废车次里由15笔显示"未出"变成0笔，跟用户描述的实际业务状态一致。单号确实
没录的那些，pill仍是"已出"但鼠标悬停提示"系统里还没录结算单号"，想补录时还能找得出来，
不再显示成"未出"误导人。

**遗留待办**（不是代码能解决的，需要用户操作）：车次#73~87这15笔至今没有一次"固化快照"
记录，"利润分项"目前都是实时估算值，建议用户找时间逐笔打开详情页点一次"固化快照"锁定，
避免以后档案参数(费率等)变动导致这15笔的历史利润跟着变。（结算单号是否补录已不影响显示，
属于可选的资料完整性工作。）

## 15. "费率与关系"图看不懂 + 链条规则重做（2026-09-19，用户报告并确认方案，已完成部署）

### 15.1 用户反馈的问题

"闫东朋和林凯都是调货员，平级的，为什么图上看着像有关系？"以及"业务关系图我需要多个，
按链条规则(票商→俊兴→欧冶→华西特钢这种从头到尾)组织，可编辑，还要能分票路径/财务路径看"。

### 15.2 根因排查（`/api/rate_links/graph` + `rate_links.js::renderGraph`）

排查发现数据库里闫东朋/林凯之间**没有任何直接关系记录**，图上"看着相关"是两个独立原因叠加：

1. **数据层面**：`processing_bases`表里有5条记录(id 2/3/5/4/9，林凯/李涛/陈杰/闫东朋/邓晶焱)
   跟`dispatchers`表里的调货员**同名**——2026-09-16历史台账导入脚本把"货主"字段自动建成了
   同名"供货厂/基地"记录（备注"信息不完整待补全"），实际这些车次(M1模式)根本没有独立供货厂，
   就是调货员本人直接带货。查证：这5条base_id对应的车次，dispatcher_id 100%就是同名那个
   调货员（比如base_id=2"林凯"下全部42笔车次的dispatcher_id都是1"林凯"），零例外、零冲突。
2. **渲染层面**：旧版`renderGraph`按5固定列画直线，任何跨列的边(比如"票源→基地"跳过了
   "调货员"这一列)会原样画一条直线穿过中间那一列，视觉上像是连到了中间列的节点上。

### 15.3 修复

- `processing_bases`新增`is_dispatcher_alias`列(迁移脚本`migrate_dispatcher_alias_bases.py`，
  按"该base的车次dispatcher_id是否100%等于同名调货员"的规则自动识别打标，命中5条，
  不改`trips.base_id`、不删数据、不影响任何计算——`engine.py`/`calc.py`取`base.service_fee_type`
  时base为空和这类无追加费率的占位base结果完全一致)。`rate_links.js`渲染时给这类节点加
  "(≈调货员本人)"提示。
- `rate_links.js::renderGraph`：跨列(colDiff>1)的边改画成绕到最上方(`bypassY`)的三次贝塞尔
  曲线，不再穿过中间列节点；相邻列的边不变仍画直线。

### 15.4 链条规则（新概念，`/chain_rules`页面）

用户要的"票商→俊兴→欧冶→华西特钢"这种从头到尾的链条，本质上就是v2引擎已经在用、但用户在
页面上看不见也不能编辑的`flow_templates`/`template_nodes`(M1-M5模板)。这次新增：

- **`template_flow_edges`表**(新增，纯展示性，不参与计算)：一条链条规则(=一个flow_template)
  按seq顺序的业务流程边，字段`from_role_type/from_label/to_role_type/to_label/hop_type
  (invoice开票/payment打款/goods发货物流)/node_key_ref(可选，关联到有实际计算值的
  template_nodes.node_key)/notes`。种子数据：template 1(split_company/M1)按用户口述的真实
  例子原样落地(票商-[开票,打款]-俊兴-[开票,打款]-欧冶-发货->华西特钢，+林凯直接发货到钢厂
  的物流支线，+票商直接打款到钢厂账户的旁支)；template 2-5按各自`template_nodes`的
  role_type/doc_types顺序自动推导初版，都标注"自动推导初版，请在链条规则页面核实"，等用户
  在页面上核对完善（这批种子数据由`migrate_flow_edges.py`一次性灌入）。
- **API**：`flow_templates`(GET/POST/PUT，只开放name/description/is_active可改，code/
  calc_family创建后不可改)、`template_nodes`(GET列表 + PUT，只开放label/role_type/doc_types
  三个纯展示字段可改，calc_rule_type/calc_rule_config/seq不开放——改这些要走"改代码+跑
  compare_v2_flat_fields.py全字段回归"这条老路，避免打乱节点求值顺序把利润算错)、
  `template_flow_edges`(GET/POST/PUT/DELETE/reorder，全部可编辑，纯展示不影响计算)、
  `GET /api/flow_templates/<id>/graph?path=all|invoice|payment`(模板层抽象图)、
  `GET /api/trips/<id>/flow_graph?path=...`(车次层实际图：party名字换成这笔车次的真实供货厂/
  调货员/户头/票源方/钢厂，`node_key_ref`命中的边带上这笔车次通过`engine.compute_trip_finance_v2`
  算出的真实数值，保证关系图和结算/利润用的是同一份计算结果)。
  **踩坑记录**：reorder接口最初直接按新序号挨个UPDATE，被`UNIQUE(template_id,seq)`约束
  卡住(两条记录互换seq，改第一条时跟还没改的第二条暂时重复)——改成先统一挪到负数区间占位
  再写回正式序号，两阶段更新避开中间态冲突。
- **前端渲染** (`renderChainGraph`，挪到`app.js`供`chain_rules.js`和`trips.js`共用)：按边的
  seq顺序算一个"最长路径层级(rank)"做左右分层("第一次到达就定层"，不因后续路径更长而反过来
  更新，否则同一角色在流程里出现多次时布局会被越推越乱)，rank差>1的边画成绕到上方的曲线
  (跟15.3同一个技巧)。
- **车次级"同名归并"**：车次的供货厂如果命中`is_dispatcher_alias`且跟调货员同名，画图时直接
  把supplier角色归并进dispatcher节点(不画成两个同名不同角色的节点)——这是本节问题在"车次
  详情页个例"这个粒度上的同一类修复，验证用车次#1(base=占位"林凯")渲染确认只出现一个"林凯"
  节点。
- **车次详情页**(`trips.js`)新增"这笔车次的链条规则关系图"卡片，带全部/票路径/财务路径切换，
  用真实数据渲染，紧跟在"利润分项"下方，直接回应用户"关系图和结算需要对得上"的诉求。

### 15.5 验证

`compare_v2_flat_fields.py`云端dev库和Mac设备端迁移后均87/87全字段一致、0差异；Playwright
截图确认`/chain_rules`页面5条规则各自的图正确渲染(template 1的三视图跟用户口述例子完全对应)、
编辑区(流程边表格+计算节点口径表格)展开正常、新增/编辑/删除/reorder链条边接口手工回归通过；
`/trips`车次#1的关系图确认真实数据替换和supplier/dispatcher同名归并生效。

### 15.5b 交互返工（2026-09-19，用户反馈"编辑时页面老是上下挪动/一点就重新渲染"）

初版每个动作(保存一行环节、切换路径视图、勾选启用、改名称说明)都调 `renderTemplateCard()`
把整张卡片 `innerHTML` 重建一遍，实际用起来有三个问题：①展开的"流程环节列表"被收起，
用户正编着的表格没了②卡片高度先塌陷再撑开，页面跟着上下跳③同一张表里其它行还没保存的
修改被一起抹掉。车次详情页的路径切换更粗暴，直接 `openTrip()` 整个弹窗重来，正在填的
输入框全部重建。

改成"谁变了只重画谁"：

| 动作 | 改后行为 |
|---|---|
| 切全部/票路径/财务路径 | 只换按钮高亮(`data-path`+`classList.toggle`) + 重画那一张图 |
| 勾选启用 | 只切"已停用"标签的 `display`，卡片其它部分不动 |
| 改名称/说明 | 就地改 `#tpl-name-<id>` / `#tpl-desc-<id>` 两行文字 |
| 保存某一行环节 | 表格一行都不动，只重画图，按钮上闪 1.2 秒"✓已保存" |
| 新增/删除/上下移 | 只重画表格+图，并且 `captureEdgeEdits()` 先把其它行未保存的编辑收起来、重画后叠回去，事后 `restoreScroll()` 还原滚动位置 |
| 保存计算节点口径 | 不再弹 `alert` 打断，同样按钮闪一下 |

另外两个顺带修的坑：①`refreshGraph()` 拉数据期间先把图容器 `minHeight` 锁成当前高度、
画完再放开，否则容器会先塌成0高度导致页面跳一下再跳回来；②表格里的名称/备注是用户自由
输入的文本，直接塞进 `value="..."` 时没转义，一个双引号就能把整行结构撑坏，加了 `attr()`
转义。验证：Playwright 实测保存某行/切视图/勾启用后 `window.scrollY` 恒为 366 不变、
`details.open` 保持 true、另一行未保存的输入内容原样保留，新增删除后滚动位置也能还原。

### 15.5c 环节类型扩展 + 平行边重叠修复（2026-09-19，用户反馈）

用户把 template 1 (自营/外部配票-差价模型) 按真实业务重排后提出两个问题。

**问题1：类型不够。** 真实链条是
`钢厂出结算单 → 我方户头据结算单做台账 → 票源方(益周)据台账开进项票 → 俊兴开票给欧冶(国企垫资) → 再逐级往回打款`，
前两步既不是开票也不是打款，用户只能被迫选"开票"，图上语义是错的。
改法：把原先写死在表 CHECK 约束里的 invoice/payment/goods 三种，扩成由
`engine.HOP_TYPE_CATALOG` 统一定义的六种，并给每种标注所属路径分组：

| code | 名称 | path_group | 颜色 |
|---|---|---|---|
| goods | 发货/物流 | logistics | 灰 |
| weighing | 过磅单 | ticket | 绿 |
| settlement | 出结算单 | ticket | 青 |
| ledger | 做台账 | ticket | 紫 |
| invoice | 开票 | ticket | 橙 |
| payment | 打款 | finance | 蓝 |

配套改动：①`migrate_hop_types.py` 重建 `template_flow_edges` 去掉 CHECK 约束（SQLite 不能
ALTER 掉 CHECK），**以后再加类型只改 engine.py 一行，不用再动表结构**，校验统一在 app.py
按目录做；②新增 `GET /api/flow_edge_hop_types`，前端下拉框和关系图配色都拉它，不再各存一份；
③路径视图从"只看 hop_type='invoice'"改成"按 path_group 过滤"——票据路径现在会同时画出
结算单/台账/发票/过磅单，按钮文案也改成"票据路径(结算单/台账/发票)"；④迁移脚本顺带把
那两条被迫选成"开票"、备注里写明是结算单/台账的边改成正确类型（逐条打印出来核对；
用户若已自行改过则条件不命中，不动）。

**问题2：可视化图重叠，"打款和开票一个来回被覆盖了"。** 同一对节点之间常有两条反向边
（我们开票给欧冶、欧冶再打款回来），原来端点相同、画出来完全重合，只看得见一条。改法：
按"无向节点对"给平行边分组，组内沿垂直方向均匀错开（跳层的曲线则用不同高度错开），
并给每条边画箭头标明方向、线端裁到节点框边缘。

这里踩了一个不显眼的坑：错开方向如果按"这条边自己的方向"算垂直向量，一来一回两条边的
垂直方向正好相反，`-13` 和 `+13` 会同时落到同一侧，等于白错开——必须按**排序后的规范
端点**算垂直基准，组内才真的分得开。（改完实测两条边 y 相差 26px，之前是 0。）

顺带把图的排版放宽了：列宽 190→250、节点框固定 150 宽，于是相邻两框之间留出 100px 空档
专门放边标签（之前只有 30px，"出结算单"这种标签直接盖到节点框上）；行高 60→74 给错开的
平行边和标签留竖向空间；边标签改成两行（第一行类型、第二行金额/单据号），否则车次详情页
那种带金额的标签在窄空档里会被截断，偏偏金额才是要看的；SVG 从 `width="100%"` 等比缩放
改成按自然尺寸画、外面套横向滚动容器——链条一长，缩放会把字压得看不清，宁可横向拖一下。

### 15.6 已知局限 / 后续可做

- template_nodes的计算公式(calc_rule_type/calc_rule_config)本次仍不支持页面编辑，新建一条
  完全自定义的链条规则目前只能建`flow_templates`元信息和`template_flow_edges`展示边，
  真正要让新规则跑通实际计算还需要开发在`engine.py`/`migrate_v2_templates.py`里补节点——
  页面新建表单里已经提示了这一点。
- template 2-5的`template_flow_edges`是自动推导的初版，业务上是否准确需要用户在`/chain_rules`
  页面核对调整(role_type/label/node_key_ref/是否要增删环节)。
- 用户提到的"生效区间从2026年8月19号开始"，核对云端dev库和Mac设备端`rate_links`表全部6条
  记录，均无2026-08-19相关日期(现有生效起始日只有2020-01-01和2026-09-05)，未能定位到具体
  是哪一条/哪个位置——已请用户确认具体是在哪张表/哪个字段看到的，待用户进一步指出后再排查。

## 16. 车/船分开统计（2026-09-19，用户要求"车船统计分开"）

**为什么必须分开**：实测一条船平均 575.6 吨、一辆车平均 58.7 吨，差一个数量级。
现有 87 笔车次里船只有 6 笔（占车次数 7%），却占了总吨位的 42%（3,453.79 / 8,207.17 吨）
和净利润的 45%（68,452 / 153,265 元）。混在一起看"车次数"、以及任何按车次平均的数字
都会被严重带偏。

**做法**：不另起一套统计，而是在共用的聚合桶里加一份车/船分开的计数
（`_empty_stats_bucket` 加 `truck_*`/`ship_*` 四对字段，`_accum_bucket` 里按
`transport_type` 累加，`_finalize_bucket` 再算出各自的平均吨位）。这样**任何维度**
（按钢厂/按供货厂/按调货人/按票源方/按期/首页总计）都自动带上了车船拆分，口径跟主数字同源，
不可能对不上（已加断言校验：车+船的车次数/吨位/结算金额/利润必须精确等于总计）。

**各处呈现**：
- 统计报表页：新增「按运输方式汇总（车/船分开）」卡片区（固定两张，0 车次也占位），
  以及顶部总计下的车船明细行；查询条件加了 `车+船(全部) / 只看汽运 / 只看船运` 下拉，
  选中后所有维度都只统计该类（后端 `transport_type` 参数）。
- 首页看板：「送货吨位（本期）」卡片下补车船明细行（口径是本期，跟卡片主数字一致——
  一开始误用了历史总计，跟"本期"标题对不上，已改）。
- 各钢厂结算周期卡片 + 全部钢厂总计卡片：同样补车船明细行。
- 一键导出台账「统计汇总」sheet：新增「按运输方式汇总（车/船分开）」表，
  复用已有的 COUNTIF/SUMIF 模式，按 Sheet1 的 E 列（运输方式：汽运/船运）统计。

**顺手做的重构**：`/api/dashboard/mill_periods` 原来把聚合桶的每个字段手写两遍
（本期 + prev_ 上期，共 40 多行），聚合桶每加一个字段就得记得在这里补两行、漏了前端就拿不到
（这次加车船字段就正好踩到：桶里有了、接口没返回）。改成直接铺开整个桶、上期统一加 `prev_`
前缀，以后加字段这里不用再动。已验证重构前后原有字段一个没少。

前端新增共用函数 `transportSplitHtml(bucket, prefix)`（在 app.js，各页共用），
某一类 0 车次时不渲染它，避免满屏"船 0次 / 0吨"这种没信息量的占位。

### 16.1 补充：各维度明细 + 首页利润也分车船（2026-09-19，用户追加要求）

第16节把车船拆分做进了共用聚合桶，所以**数据层面每个维度本来就已经有了**
（`by_mill`/`by_base`/`by_dispatcher`/`by_invoice_source` 每一行都带 `truck_*`/`ship_*`），
这次只是补上呈现：

- **统计报表四张明细表**（按钢厂/供货厂/调货人/票源方）各加两列「汽运(次/吨)」「船运(次/吨)」，
  某一类没有就显示灰色"-"，竖着扫一眼"船运"列就知道谁在跑船。原"车次数/吨位"表头
  改成"总车次/总吨位"——否则"车次数"既像"trip 数量"又像"汽运数量"，有歧义。
  实测确实有区分价值：林凯 54 次里有 2 条船（1,333 吨 / 利润 ¥27,623，占他利润的四成）；
  俊兴作为供货厂 15 次里有 4 条船（2,121 吨，占其吨位的八成）。
  想看"只有船的那部分各维度利润"，用页面上方的"只看船运"筛选即可，所有维度都会只统计船。
- **首页"本期利润估算"卡片**补车船拆分：本期 车 71次 ¥80,658 / 船 6次 ¥68,452——
  6 条船的利润跟 71 辆车接近，这个对比混在一起是看不出来的。
- **钢厂周期卡片/总计卡片**：原本吨位、利润分两行写，两行都以"车 71次"开头又没标注哪行是哪个，
  反而更难读；改成 `transportSplitHtml(..., 'both')`，每类各占一行、吨位和利润写在同一行。

校验：逐行断言四个维度每一行的"车+船"都精确等于该行合计（次数/吨位/利润），全部通过。

## 17. 链条规则录入简化（2026-09-19，用户反馈"无论录入编辑还是计算匹配都有点复杂"）

### 17.1 复杂度到底卡在哪（先量化，再动手）

查了库才敢下结论，四个结构性问题：

1. **同一个东西两套名字两个入口**：车次页选「M1 自营带票」，链条规则页叫「自营/外部配票-差价
   模型(企业票源)」。库里 `mode_code` 和 `template_id` 一一对应——68 笔 M1 全是模板1，
   没有一笔例外，纯冗余，却逼用户每次在脑子里翻译一遍。
2. **画的图和算的账是两套数据**，靠手打内部标识(`front_invoice_amount` 这种)关联。
   所以"改了图钱没变"是必然的——用户说的"计算匹配"困惑就来自这儿。
3. **名称是自由文本，而且在车次图里会被悄悄替换**：29 条边 13 个名称只有 3 个跟档案对得上；
   用户打的「欧冶(垫资户头)」档案里其实叫「华西-欧冶户头」，车次图按角色替换后显示成后者，
   等于打的字白打。每加一个环节要填 7 个字段。
4. **维护面 80% 是噪音**：只有 M1 被真正维护过（68车次/9条边全部人工核过）；
   M2/M3/M5 共 20 条边全是"自动推导初版"没人碰过；M4 是 0 车次的死规则。

### 17.2 关于"图能不能驱动计算"的结论（用户问，已答复并采纳分层方案）

技术上不难——引擎本来就是数据驱动的（节点+JSON配置），做个配置编辑器即可。
**难的是安全性**，三条真实风险：①现在 87 笔全字段零差异是**有对照组**才验证得出来的，
公式一旦可自由编辑就没有对照组了，改错不会有任何东西报警；②节点间有求值顺序依赖
（A 引用 B 就必须 B 在前），拖一下顺序就可能算出 0；③87 笔里 15 笔从没固化过快照，
公式一改历史利润就跟着变。

**采纳的分层方案**：「这条边的钱从哪来」→ 界面上从已有计算节点里**选**（零风险，本次已做）；
「新增一种算法」→ 仍走改代码 + 87 笔回归验证；将来真要自助配公式，从单价×吨位这种
**不引用其它节点**的最安全一类开始。

### 17.3 本次改动

- **端点从"选角色 + 手打名称"两步合并成一个下拉**，选项本身说清楚这一端是哪种形态：
  `role:` 角色占位（车次图里换成该车次的真实主体）/ `entity:` 指定具体对象（绑定档案实体，
  以后改名图上自动跟着变）/ `custom:` 自定义文字（台账、最终客户这类不在档案里的概念）。
  `template_flow_edges` 为此加了 `from/to_entity_type`、`from/to_entity_id` 四列。
- **链式录入**：新增环节时起点自动承接上一条的终点，常规情况只需选「终点」和「类型」，
  7 个字段降到 2 个。
- **关联计算节点改下拉**，显示节点中文名而不是手打 `front_invoice_amount`，首项是
  「（不挂·仅流程说明）」。
- **图上明确区分**：挂了计算节点=实线彩色+金额；没挂=细虚线灰色+标注「仅说明」。
  M1 这条链 9 个环节里 8 个是纯说明——这下一眼就能看明白为什么改图不影响钱。
- **点击任一环节弹详情**：这一步是什么、第几步、金额口径来自哪个计算节点、
  （车次图里）这笔车次的真实金额/发票号/单据状态。
- **一键"按计算节点重新生成一版"**（`POST .../flow_edges/auto_generate`）：把原来只在
  迁移脚本里的推导逻辑搬到运行时，生成的边一律用**角色占位**而不是手打泛称，标 `is_auto=1`；
  用户改过任意一条，那条就自动转成已确认。整张图都还没核实时卡片顶部给黄色横幅提示。
  ——这就是用户要的"自动 + 还能二次编辑"。
- **合并两套名字**：车次详情页「结算模式」改成选「链条规则」，界面上不再出现 M1/M2 代号。
  `mode_code` 仍由 `_sync_mode_code_from_template()` 反推着写回去（calc.py 兜底路径和
  车次列表/导出台账还在用它），两个字段始终一致。
- **去噪**：0 车次的 M4 自动停用；车次页的链条规则下拉只列启用中的。

### 17.4 刻意没做的事（附原因）

- **没有自动把名称绑定到档案实体**。第一版脚本顺手把能对上名字的（俊兴/益周/华西特钢）
  绑死了，跑完才想明白是错的：链条规则一条覆盖很多笔车次，而这些车次的票源方各不相同
  （M1 的 68 笔里票源方有俊兴、益周、反向好几种），把「益周」绑死等于宣称这条规则的票源方
  永远是益周，票源方是俊兴的那些车次图就画错了。用户当初打「益周」本意仍是占位。
  **是不是固定的只有用户知道，系统不该替他猜**——所以一律保持角色占位，由用户显式改成
  「指定具体对象」。已回滚该步骤，只保留统计提示。

### 17.5 踩的坑

端点单元格按「前缀-字段」生成 id（`ee-12-f`），读取却按「字段-前缀」找（`ee-f-12`），
正好错开，一进页面就 5 个 `Cannot set properties of null`。统一成「前缀-字段」一条规则贯穿。

## 18. 调货员/供货厂 详情下钻（2026-09-20）

统计报表的四张明细表一行只给一个汇总数，回答不了"这个调货员本期比上期怎么样、提成拿了多少、
其中多少是带票多少是我们配票"。新增 `GET /api/entity_stats?kind=dispatcher|base|mill|invoice_source&id=N`，
返回 **本期 / 上期 / 总计** 三份完整聚合桶 + 最近12笔车次。前端在报表页把这四张表的名称做成
可点链接，点开弹三栏对照面板。

**本期/上期的口径**：沿用全系统唯一那套——各钢厂按自己的结算周期分别取该实体的车次再加总
（`_entity_period_stats` 内部循环钢厂，复用 `calc.get_mill_period_status`/`get_period_bounds`），
跟首页卡片、钢厂周期卡片完全一致。**没有**另造一套按自然月或系统默认周期的算法，否则同一个
调货员在两个页面会看到两个不一样的"本期"。只有一家钢厂时标签显示具体区间，多家时显示
"各钢厂周期（最早~最晚）"。

**聚合桶补字段**：原来只有 `split_trip_count`/`service_trip_count` 和两条路径各自的垫资成本，
吨位/金额/利润/提成都没按路径分开记。这次补齐 `split_tonnage/settlement_amount/profit/commission`
和 `service_*` 同名四项（`_accum_bucket` 里把原来的 if/else 改成按前缀累加，少一半重复代码）。
因为这是共用桶，所以**所有维度**（按钢厂/供货厂/调货人/票源方/按期/首页总计）都自动带上了
配票 vs 带票的拆分，不用另起统计。已加断言：四个维度逐行"配票+带票"必须精确等于该行合计
（车次/吨位/利润/提成四项全测），全部通过。

`reports_summary` 的各维度明细里顺带加了 `entity_id`（桶的 key 本来就是实体id，finalize 时塞进去），
前端才能从表格一行下钻到对应实体。`entity_id` 为空的那行（比如没填调货人的车次）不给链接。

## 19. A阶段：录入提速 + 状态日期联动（2026-09-21）

用户需求补充后确认的顺序：A 录入提速 → B 三条线(货物/开票/回款)+批次模型 → C 钢厂结算单导入。本节是A。

**业务口径补充（用户2026-09-21原话要点，B阶段要用）**
- 供货厂哪怕自己有票额，也可以走"不带票、由我们配票"路径 → 票路径按**车次**决定，不按供货厂属性定死。
- 调货人自己车队的车（如苏DBB772=林凯车队）：历史上供货厂记成了占位基地"林凯"(is_dispatcher_alias=1)。
  这类车**供货厂先留空，不强制录入**。
- 客户账户：不一定是老板本人的卡，但都由老板管理（银行限额等原因会临时指定别的卡，反向的会到个人账户）。
  → B阶段需要"供货厂名下多个收款账户"。
- 结算单、开票、打款基本都是**一批一批**做的 → B阶段需要批次对象。
- 俊兴 = 我们自己的加工厂（票源方"俊兴"也是我们）。
- 计算引擎的业务口径用户还要和财务/台账核对，可能再改；允许"备份后按新规则改数据"。

**改动**
- 迁移 `migrate_phase_a.py`（幂等，自动备份）：trips 加 `arrived_date/invoiced_date/closed_date`；
  新表 `app_settings(key,value)`，`default_departure_place=永康`。
- `STATUS_DATE_FIELD`：reported→order_date, arrived→arrived_date, settled→settlement_date,
  invoiced→invoiced_date, closed→closed_date。推进时目标状态日期为空才填（弹框选的日期或今天），
  **回退不清空、已有不覆盖**、跳过的中间状态不补（不编日期）。
- `_entry_defaults`：只有一个钢厂→默认；户头=绑定该钢厂的唯一户头；接单日期今天；发货地取设置。
  `create_trip` 对没传的字段用它补（所以"再来一车"等所有新建入口都生效）。`/api/entry_defaults`、`/api/app_settings`。
  前端改钢厂时户头同规则跟随。
- 车牌联想：`/api/plates`（历史车牌按次数）、`/api/plate_profile`。某字段在该车牌历史中同一取值≥80%才带出，
  否则列为 varies 让人手选；占位基地不参与带出；占位基地占该车牌≥50%且调货人稳定 → 判定为"某调货人车队的车"。
  只填当前为空的字段。
- `_apply_status` 单车/批量共用；`/api/trips/batch_status`（common: status_date/settlement_doc_no；per_trip 覆盖）。
  推到"已结算"时 `_quick_settle`：**只对没有磅重明细的车**写一行不指定货型的明细（单价=金额/吨位，标混料），
  已有明细的不动。
- 前端：列表勾选+批量推进条、每行"→下一状态"按钮（到已结算且无明细时弹框）、推进弹框（整批日期/结算单号，
  逐车吨位金额，支持从Excel粘贴"吨位\t金额"多行）、"再来一车"（车牌司机清空、日期今天、光标进车牌框）、
  详情页加到厂/开票/结案日期、"录入默认值"按钮、toast 提示。
- 回归：87笔历史车次 0 差异；#88（用户今天新建的空车次，无票源）rate_out 14 vs 0 的差异在改动前就存在，与本次无关。

## 20. B阶段：三条线 + 批次 + 全方位统计补充（2026-09-21）

用户确认：统计全部补充；**历史数据先不改**（要先和财务、台账核对），按当前数据展示。

**数据结构**（`migrate_phase_b.py`，只加不改，自动备份，TRUNCATE日志模式）
- `trips.invoice_path`：self 供货厂自带票 / source 票商·反向→供货厂→欧冶 / ours 我们配票(经俊兴)。
  空=自动判断（`lanes.default_invoice_path`：split_company→ours；split_reverse→source；
  服务费模型无票源→self，票源是俊兴或 pass_through→ours，否则 source）。车次详情可手改。
- `base_accounts`：供货厂收款账户（户名/开户行/卡号后四位/类型：老板本人·老板指定的卡·反向个人户）。
- `batches` + `batch_trips`：开票(in/out)、打款(p_out/p_in/p_client)、提成(commission)批次，一批多车，逐车金额。

**三条线**（`lanes.py`，Blueprint）
- 货物线：起点（真实供货厂 / "某某车队" / 未填）→ 调货人 → 钢厂；检查点 到厂、结算单。
- 开票线：[in 票源→开票主体] → out 开票主体→欧冶。开票主体：ours=俊兴，其余=供货厂/车队。
- 回款线：p_out 欧冶→开票主体 →[p_in →票源]→[ours 才有 p_client →客户账户]。
- 每个环节的应登记金额 = 票额(估) = 结算金额 − 吨位×垫资费率（M5不扣），跟首页票源额度水位同口径，
  **待和财务核对**，改 `lanes.ticket_amount` 一处即可。
- 状态：done / partial / todo / blocked(前一环节没完成) / legacy。legacy：状态已开票或已结案但没登记开票批次 →
  开票环节视为已开；已结案 → 回款也视为完成。其余历史回款显示为未登记（这就是首页"欧冶回款 61车 1346万"的来源）。
- 登记 out 批次时，仍在"已结算"的车次自动推进到"已开票"，开票日期=批次日期（已有不覆盖）。
- API：`/api/lanes/meta`、`/api/trips/<id>/lanes`、`/api/trips_lanes_brief`、`/api/tracking`（待办+账龄汇总）、
  `/api/batches` GET/POST、`/api/batches/<id>` GET/DELETE。
- 页面：新页 `/tracking` 回款追踪（资金占用与账龄卡片、按环节待办、勾选登记批次、提成应付已付、批次记录）；
  车次详情"三条线"取代原进度清单 + "开票路径"下拉；列表进度徽标改为 票x/y 款x/y + 下一步。

**统计补充**
1. 资金占用/账龄：`/api/tracking` summary（15天内/16-30/31-60/60天以上），首页新卡片 + 回款追踪页。
2. 票额度用量、3. 钢厂量补进度：之前已有（首页"票源额度水位"、"各钢厂结算周期"距下一档）。
4. 单吨利润：`_finalize_bucket` 加 profit_per_ton（总/车/船/配票/带票），报表四张表+统计卡+下钻都显示。
5. 趋势：`/api/analytics/trend?granularity=day|week|month|cycle`，报表页三张图（车船吨位堆叠、利润、单吨利润）+数据表。
   图表函数 `renderStackedBars`/`renderSignedBars`（app.js），车/船配色已跑色盲校验。
6. 质量：`/api/analytics/quality` 各供货厂扣杂率/均价，各货型吨位/均价/按月均价（历史车次净重=结算吨位+扣杂还原）。
7. 调货人提成：`/api/analytics/commission` 应付(逐车拆解)/已付(提成批次)/未付，回款追踪页登记付款。
8. 数据完整度：`/api/analytics/completeness`，报表页底部，点车号跳 `/trips?open=<id>`。
- 报表"按供货厂"里占位基地显示为"某某车队"（key 仍是 base_id，下钻不受影响）。`_report_trips` 抽成公共筛选。

**顺手修的**：已固化车次的利润分项表，吨位/周期/中间量一直显示"-"/"undefined"（快照没存这些说明字段）。
现在先实时算补齐说明字段、再用快照覆盖，快照里的钱数不变。

回归：87笔历史车次 0 差异；总利润 ¥153,265.49 不变。

## 21. 反向垫资费改为按时段配置（2026-09-21）

用户说明：**垫资费一直都有**；只是这个月反向的垫资费已经算进了反向票点成本（反向票源是欧冶介绍的渠道），
所以本期对反向不单独扣；下个月不一定。原先"反向不扣垫资费"写死在4处（split_reverse 模板
advance_fee=per_ton_fixed 0、calc.py 兜底、首页额度水位 SQL、lanes 票额估算），规则一变就得改代码。

- `migrate_reverse_advance_fee.py`：给 户头×反向票源 加 rate_links `account_advance_fee=0`（2020-01-01起不设截止，
  覆盖目前所有反向车次，数字不变）；split_reverse.advance_fee 节点改为 per_ton_rate_schedule，按户头+票源查
  （查不到反向专属档就落到户头通用档 23）。
- `engine.RELATION_TYPE_CATALOG.account_advance_fee.secondary_type = invoice_source`，费率页可以按票源配。
- `engine.advance_fee_unit_for_trip(conn, trip)`：按车次模板的 advance_fee 节点配置查单价；
  首页额度水位、lanes 票额估算都改用它，和利润计算同一条规则。
- 只影响反向差价模型(M5)。带票路径里用了反向票源的车次（1笔M3）照旧按户头通用档23，未改。
- calc.py 的"M5不扣"只在没有 template_id 的兜底路径里，现网不走，保留未动。
- **以后反向票点不含垫资费时**：费率与关系页 → 新增 户头垫资费率，户头=华西-欧冶户头，票源=反向，23元/吨，
  生效日期=那天；旧的0档会自动截止到前一天。已测：本期车次0、之后日期23。
- 回归：87笔0差异，总利润 ¥153,265.49、额度水位、回款追踪金额全部不变。

## 22. 排版整理（2026-09-21，用户："车次管理表格横向超出…其他子页面看着排版优化"）

- 车次表 18列→12列：去掉户头；钢厂只有一家时隐藏(多家自动显示)；"类型"并进车号(船带紫色小标)；
  结算单列去掉(进度里已有"结算单✓"，悬停看单号)；调货人+供货厂合并为"调货人：供货厂"
  (只有供货厂就只显示供货厂；调货人车队/占位基地显示"某某车队")；付款只显示已付/未付(悬停看金额)；
  "下一步"和"编辑"并成一列。工具栏按钮改短，1280宽不再换行。
- 统计报表四张维度表：汽运/船运两列合并成"车 · 船"(只写次数，悬停看吨位)，不再横向溢出。
- 回款追踪：历史导入的长结算单号缩成"历史·未录单号"(悬停看全文)。
- 费率与关系：备注两行截断(悬停看全文)，操作列不换行。
- 首页：钢厂周期卡片指标改为自适应多列(卡片矮了一半)；利润卡标题缩短；14天趋势图加高。
- 链条规则：反向模型说明文字更新(不再写"垫资费恒为0")。

## 23. 链条规则简写（2026-09-21）

用户：车次列表"模式"列还是 M1/M2，不方便读。`flow_templates.short_name`（`migrate_template_short_name.py`）：
配票 / 反向 / 直签带票 / 户头通道 / 调货对接。链条规则页"编辑名称/说明"可改简写、新建时可填。
车次列表该列改名"链条"显示简写(悬停看全名)，车次详情顶部、明细抽屉、调货人/供货厂下钻"路径"列都用简写。
mode_code 仍在底层保留（calc.py 兜底、导出台账），界面上不再显示。

## 24. 本车费率可改（2026-09-21）

用户：车次详情"利润分项"下面的框改不了（已固化的车全部disabled），要能改；默认走费率与关系，但可以改。
另外发现原来那几个框大多是摆设：货差单价恒为0、买票成本不参与净利润、服务费类型/数值在v2引擎里根本不用
（服务费走 rate_links service_fee_gross），真正生效的只有调货人提成。已整体替换。

- `trips.finance_overrides`（JSON，`migrate_finance_overrides.py`）：`{"rate:<节点key>": 数值}`，只存这一车改过的。
- engine `_run_template`：凡是"按费率算"的节点(per_ton_fixed / per_ton_rate_schedule / per_ton_reward_tier /
  rate_deduct_*)，先算默认费率(`_default_rate`：费率与关系/模板固定值/量补阶梯/票源点位)，有 `rate:<node_key>` 覆盖就用覆盖值；
  结果里带 `rate_items`[{label, unit, default, value, overridden, source}]。旧键 dispatcher_commission_rate / reward_unit_price 仍兼容。
  split 家族 rate_out/rate_in 取自 rate_items（默认值与原查法一致）。
- `app._compute_finance` 统一合并已存覆盖 → 详情/首页/报表/导出/回款追踪口径一致；`engine.advance_fee_unit_for_trip` 也认 `rate:advance_fee`。
- `PUT /api/trips/<id>/finance_overrides`：设值或传 null 恢复默认；已固化的车按新费率重算并**更新固化快照(仍固化)**，
  未固化但有利润记录的更新记录。注意：重固化是整车重算，量补单价按当前周期累计吨位取档。
- 前端：利润分项下"本车费率"表（项目/默认/本车输入框/已改·恢复默认），清空即恢复默认；改完立即刷新利润表和三条线。
- 回归：87笔 0 差异（未改任何覆盖时数字不变）。

## 25. 统计/汇总内链（2026-09-21）

用户：汇总、统计和数据页面的各数据做好内链，点击跳转看详情。

- 统一落点 = 车次管理 `/trips?...`（顶部蓝条显示"来自哪里 + 附加条件 + 共N车·吨位·金额 + 清除"）。
  `list_trips` 新增参数：status_in / exclude_voided / dispatcher_id / invoice_source_id / template_id（none=未指定桶）/
  family(split|service_fee) / ids / date_field(settlement_date|order_date)。能放进顶部筛选框的放进去，其余记在 EXTRA_FILTER。
- app.js：`tripsUrl(params, from)`、`drill(text, params, from)`、`tripLink(id, text)`；`renderBarChart` 数据项带 href 可点；
  趋势图 `opts.href(r)` 点柱子跳该区间。
- 首页：今日/本期/上期/总计接单、待结算、本期/上期已结算·吨位·利润(用 mill_periods 返回的 trip_ids)、
  钢厂卡片名称和本期/上期车次、排名表、票源额度名称(额度周期内占用的车)、14天吨位柱子；资金卡片跳 `/tracking?hop=`。
- 统计报表：车次数卡片、四张维度表的车次数(带当前钢厂/车船/日期筛选，未指定桶=none)、两张利润柱状图、
  路径/车船卡片、趋势三张图和数据表(按柱子区间)、质量两张表(trip_ids)、调货人/供货厂下钻弹窗(本期/上期/总计车次、最近车次车号、查看全部)。
- 回款追踪：待办车号、提成调货人与车次数、未付明细车号、批次明细车号、批次车次数；支持 `?hop=` 定位环节。
- 链条规则卡片"在用N车"；基础档案(钢厂/供货厂/调货人/票源方/车辆)每行"车次 ›"；车次明细抽屉里供货厂/调货人可点。
- 已验证：所有入口跳转后的车次数与来源数字一致（Playwright 逐个点过）。

## 26. 一键导出台账重做（2026-09-21）

用户：主表重要数据靠前；每车利润过程参数细化；可按带票/不带票等过滤；子表加各类统计分析含票款。
生成逻辑搬到 `ledger_export.py`（app.export_ledger 只取数+校验）。6 个 sheet：
1. **车次台账**：列顺序 ID/结算日期/车号/车船/路径(配票·反向|带票带货)/链条/调货人/供货厂·车队/票源/状态/吨位/金额 →
   净利润(公式)/单吨利润(公式)/留存利润 → 过程参数(服务费单价+毛服务费、量补单价+金额、对外点位+给客户付、垫资费单价+垫资费、
   前端开票金额、票源点位+前端实付、垫资成本、提成单价+提成、其他成本、本车改过的费率) → 票款(开票路径、票额、5个环节状态/已登记金额、提成已付)
   → 其余(接单日期/结算月份/扣杂/结算单号/钢厂/户头/司机/备注)。表头按分组着色，自动筛选+冻结，底部 SUBTOTAL 合计随筛选变化。
   净利润 = 毛服务费 + 量补 − 垫资成本 − 提成 − 其他成本（配票路径垫资成本=个人垫资），导出时逐车与系统值校验，不一致会在 A1 标红。
2. **统计汇总**（全是引用主表的公式）：总体+利润构成、按路径/车船/链条/调货人/供货厂/票源/状态/结算月份、票款环节(引用票款追踪)、
   调货人提成应付已付未付、各货型吨位均价扣杂(引用磅重明细)。
3. **磅重明细**（加路径/链条/调货人/供货厂列，可筛选）；4. **票款追踪**（每车每环节一行：应登记/已登记/待登记/状态/账龄/批次ID）；
5. **批次记录**（批次+分摊明细）；6. **票源额度**（额度/预警/已用/占比/剩余/状态）。
- 数字口径与系统一致（已固化取快照、本车费率覆盖已合并）；LibreOffice 重算 0 错误；合计与系统总利润一致。
- `_quota_levels(conn)` 从首页接口抽出来复用。

## 27. 计算公式"读得懂版"（2026-09-21）

用户：计算节点口径不知道怎么编辑，要阅读性高、清晰简单地做关联。链条规则卡片里原"计算节点口径"表（node_key/calc_rule_type 原码）
换成"计算公式"视图，原表降为"高级：角色/单据类型"。
- `GET /api/flow_templates/<tid>/formula`：`_describe_template` 把每个节点翻译成中文公式 token（结算金额/吨位字段、①②③节点引用、运算说明），
  标费率来源（费率与关系某类费率+按什么查 / 固定值 / 量补阶梯 / 票源方点位，带跳转链接），并用该链条最近一车代入真实数值；返回使用车次数、已固化数。
- 只开放两类修改（`_validate_rule_change`）：费率节点改"费率从哪来"（per_ton_fixed↔per_ton_rate_schedule、rate_deduct_fixed↔rate_deduct_rate_schedule，
  单位不能混，可勾"按票源细分"=加 secondary）；净利润汇总节点每项 加/减/不计入（只能引用它前面的节点）。其余节点只读。
- `POST /api/template_nodes/<id>/rule`：confirm=false 只预览（同连接内改→全链条车次实时重算→rollback），返回影响车数/已固化数/净利润合计前后/变化最大20车；
  confirm=true 保存并写 audit_log(旧/新口径)。`GET .../rule_history` + 前端"撤销上次修改"。已固化车次统计仍取快照不受影响。
- 前端：公式行里点 ①② 小标签跳到对应步骤高亮；✎ 改名称；/archives?tab= 支持直达某档案页签；app.js 加全局 toast。
- 顺手：split_reverse 的"垫资费(反向不扣)"节点名改为"垫资费"（口径早已改为按费率档，名字过时）。

## 28. v2.0 第一批（2026-09-22）：数据安全 + 结算单导入 + 首页预警

用户确认建议并说"开始吧，数据完善后续再补充"。界面品牌改为 v2.0，导航新增「系统」页。

**数据安全（system_ops.py，Blueprint）**
- 自动备份：`app.py __main__` 调 `system_ops.start_background()`：把 data/ 下迁移脚本留下的 `*.bak_*`、`_devcopy.db` 等挪到
  `data/backups/旧迁移备份/`(只挪不删) → 启动备份一份 → 每小时检查当天没有自动备份就补 → 清理超30天回收站。
  备份用 SQLite 在线 backup 接口；自动备份保留最近30份，手动不删。`/api/system/backups` GET/POST、`/<name>/restore`(恢复前先另存当前数据)。
- 操作留痕：`audit()/audit_row_diff()` 写 audit_log。已接入：车次新建/字段修改/状态/磅重明细(吨位金额前后)/本车费率/固化快照/删除/恢复、
  档案新建/修改/删除、费率新增续期、链条规则名称说明、批次登记/删除、链条口径(原有)。`/api/system/audit`（中文表名字段名）。
  车次详情底部「修改记录」。v2 之前的改动没有记录。
- 回收站：`recycle_bin` 表（运行时 CREATE IF NOT EXISTS）。删车次(连同磅重/利润/单据/打款/批次分摊)、删批次(连分摊)、删档案 → 打包进回收站，
  30天内「系统→回收站」恢复(原编号)。车次详情新增「删除」按钮（作废照旧）。

**钢厂结算单导入**（trips 页「导入结算单」，static/js/import_settlement.js + `/api/import/settlement/preview|apply`）
- 粘贴 Excel 表格 → 按表头关键字/内容自动认列（日期/车号/车船/货型/扣杂/吨位/单价/金额/结算单号/票源或供货厂/调货人），可手改 →
  "/" 或车号空的行并入上一车(一车多料) → 按 车号+日期±3天 匹配：已结算且同金额=跳过(防重复)，否则更新最近的未结算车次，找不到=新建。
- "票源或供货厂"列自动分辨：先精确匹配票源/供货厂；写简称(如"反向")按票源绑定的供货厂/调货人挑(反向+尹鹏飞→反向-尹鹏飞)，并提示核对；
  "调货人"列写的是供货厂名(船的货主)时自动当供货厂。新建车次按车牌历史带出调货人，链条默认：反向票源→反向、其他票源→配票、无票源→直签带票(可改)。
- 应用前自动手动备份；逐车走现有接口(明细/字段/状态)，都推进到"已结算"，结算日期=表中日期。完成后跳到这批车次列表核对。

**首页「待办与预警」**（`/api/alerts`）：票源额度≥90%/超预警线/超额度、报备超3天未结算、结算超7天未开销项票、欧冶回款超30天未登记、
调货人提成未付、量补差25%以内升档、数据缺项(不含未录结算单号)、超过2天没有自动备份。每条可点到对应车次/页面。

回归：101笔0差异；删除→恢复、备份→恢复、导入(重复跳过/新建/简称票源)均实测。

## 29. v2.0.1 内部整理（2026-09-22）

用户："系统子页点了url出错啊？还有 内部整理那些也更新了吧"。

**URL 出错的原因**：设备上文件是新的，data/backups 里也有 10:02 的启动备份(说明新版 start_background 跑过)，但很可能旧服务还占着
5000 端口——新进程先做完备份，再绑端口失败退出，浏览器连的仍是旧服务，/system 在旧代码里不存在 → Flask 英文 "Not Found / The requested URL
was not found"。云端用设备数据库副本全链接实测无错。处理：
- `/api/version`(system_ops)：磁盘上 *.py/VERSION 的修改时间晚于本次启动 → `code_changed`；app.js 每页打开+每2分钟检查，顶部黄条"系统程序已更新…现在重启"。
- `/api/system/restart`：只允许本机；0.8秒后 `os.execv` 原地重启。坑：werkzeug 把监听端口设为可继承并写 `WERKZEUG_SERVER_FD`，
  直接 execv 会"端口被占用"——重启前 pop 掉环境变量并 `os.closerange(3,4096)`。
- 404/500 友好页(templates/error.html；/api/ 下返回中文 JSON)。代码已更新但没重启时 404 页直接给"现在重启"按钮。
- 启动系统.command：启动前先结束还占着 5000 端口的旧 app.py。
- VERSION 文件(2.0.1)，品牌角标显示运行中的版本。

**拆分主程序**：tools 脚本按 AST 机械拆分——带 `@app.route` 的函数按原行号分进 routes_pages/archives/rules/trips/quotes/reports.py，
其它(导入/常量/工具函数/钩子)按原顺序进 core.py(末尾 `__all__` 含下划线名字)；路由模块 `from core import *`。app.py 只剩入口，
仍 `from core import *`，所以 `import app as A; A.app / A._compute_finance` 照旧可用。拆分前的原文件：legacy/app_before_split.py。
验证：tools_golden_master.py 对 420 个 GET 接口(含所有车次/模板/档案的带参地址)拍快照，拆分前后 JSON 一字不差。

**下线旧计算引擎**：calc.py 删掉 compute_trip_finance / compute_dispatcher_commission / DISPATCHER_COMMISSION_RULES，只留引擎共用工具。
完整旧文件 legacy/calc_v1.py；compare_v2_*.py 挪到 legacy/(改为 import calc_v1，仍可跑：101 笔全字段一致)。
`core._compute_finance` 不再兜底旧引擎：没有链条规则时返回全0 + `engine_error`。

**利润自动核对**(regression.py)：`profit_baseline(trip_id, sig, net_profit, reserved_profit, checked_at)`。
sig = 车次整行(去 updated_at) + 固化状态/时间 + 本期累计吨位。sig 变 → 静默更新基准；sig 不变利润变 → 列入「系统→利润核对」
(附同期 audit_log 里档案/费率/规则改了几处)，可逐车或全部「以当前为准」。链条规则页确认改口径后自动对该规则车次 accept。
首页预警第8项；启动时打印结果；命令行 `python3 regression_check.py [db] [--accept]`(有差异退出码1)。

## 30. v2.1 手机录入（2026-09-22）

用户确认：只有自己用、先不拍照、手机上能看利润。
- ~~服务改为监听 0.0.0.0:5000~~ → v2.1.1 改为：电脑仍 127.0.0.1:5000；手机另起局域网端口 0.0.0.0:5080（`MOBILE_PORT`，后台线程 make_server）。
  原因(用户报"升级重启 5000 端口被占")：macOS 隔空播放接收器(AirPlay Receiver)占着 *:5000，绑 0.0.0.0:5000 必冲突，
  绑 127.0.0.1:5000 不冲突(以前一直没事)。5080 起不来只提示、电脑端照常；werkzeug 端口冲突是 sys.exit 不是异常，要接 SystemExit。
  `SCRAPMETAL_HOST=127.0.0.1` 不开手机端口。
- mobile.py `before_request` 把关：本机(127.0.0.1/::1)不受影响；局域网设备必须有口令 cookie(`sm_mobile`，180天，
  = HMAC(salt, pin_hash)，换口令全部失效)。没设口令时手机只看到"请先在电脑上开启"。输错5次锁5分钟(按IP，内存)。
  手机可 GET 全部(除 /api/app_settings、/api/mobile/access、/api/system/backups)；写操作白名单：POST /api/trips、
  PUT /api/trips/<id>、POST /api/trips/<id>/status、POST /api/trips/batch_status，其余 403。
  手机直接打开 "/" → /m；从系统里点过来(Referer 同 host)仍显示电脑版首页。
- audit() 在 g.via_mobile 时备注"手机"。app_settings 接口不再返回/接受 mobile_* 键。
- /m（templates/mobile.html + static/js/mobile.js + static/css/mobile.css）：底部4个标签 报车/在途/查车/概况。
  报车：车号联想+车牌画像带出；链条按"画像→票源推荐(反向个人→反向、其它票源→配票、无→直签带票)"，手点后不再自动改；
  选反向票源带出绑定供货厂；同车号有在途记录会提醒。在途：到厂一键、结算弹层(日期/吨位/金额/单号，已有磅重明细则按明细)。
  详情弹层显示利润并可推进到下一状态。
- 系统页新增「手机访问」：设/换/关口令、局域网地址、二维码(qr.py 自写 QR 编码器：字节模式 M 级 v1~10，已用 OpenCV 解码验证)。
- style.css 加 ≤760px 适配：导航横滑、网格变两列/一列、导航里出现"手机录入"。

## 31. 财务对账同步 + 经营报告一键导出（2026-09-22，v2.2.0）
- 按财务「发货记录表6」同步：sync_20260922_finance_v6.py（字段/路径/票源、切割费客户承担=其他成本节点去掉 cutting_fee_total、王荣胜 13.8%、
  7/26~8/25 量补 20 写进本车费率）；sync_20260922_attr_v6.py（调货人/供货厂按财务分表统一，新增调货人尹鹏飞、自有货源高栾凤/王荣胜）；
  add_20260921_yandongpeng.py（补录 9/21 车，车号待补）。同步后 102 车四项全部一致，利润差只剩财务 INT 取整。
- app_settings.last_recon = 最近一次对账结论（JSON：date/ledger/rows/fin_total/note），报告“和财务台账的核对”一段读它。
- report_export.py：/report（templates/report_print.html，手机宽 120mm，静态 SVG，数据由 build_data() 注入）；
  /api/report/pdf 找本机 Chrome/Edge（mac/win 常见路径、PATH、REPORT_BROWSER 环境变量）无界面 --print-to-pdf，另存 data/reports/；
  找不到浏览器 → 返回 fallback=/report?autoprint=1（打开后自动弹打印框，选“存储为 PDF”）。以 root 运行时加 --no-sandbox。
  坑：Chrome 打印时媒体查询宽度不可靠，模板里直接写死窄屏布局（KPI 两列、路径卡一列）。
- 首页看板、统计报表右上角「导出经营报告 PDF」「在线预览」。
- tools/recon（对账：导出/配对/分步对账 Excel）、tools/report（离线生成报告）见 tools/README.md。

## 32. v2.3 结构重构 + 附件真上传（2026-09-30）

项目首次接入 git 版本管理（baseline commit 43b9262）。每步独立 commit + golden_master 验证，可精确回退。

### 32.1 core.py 机械拆分（第一步，commit 3e6eaf4）
- 1100行 → 65行(Flask 实例 + `globals().update` 重导出)，按职责拆 7 子模块：
  `archives_config`(165) 档案/状态/链条规则/统计分桶常量；`flow_graph`(221) 链条规则展示(公式翻译/流程图)；
  `trip_compute`(229) 车次组装/计算/同步；`stats`(247) 统计分桶(含 32.2 重构)；
  `entry`(108) 录入辅助；`entity_stats`(92) 实体下钻/报表筛选；`import_helpers`(24) 结算单导入辅助。
- routes_*.py 零改动：core.py 末尾用 `globals().update` 把子模块名字搬进 core 命名空间
  （因工具函数都是 `_` 开头，`from xxx import *` 默认不导出它们，故用 `globals().update`）。
- 原文件存档 `legacy/core_before_split.py`。
- 验证：golden_master 480 接口，476 个 md5 字字不差；4 个差异(`/api/version`/`/report`/`/api/report/pdf`/`/api/export/ledger`)
  经"同代码连跑两次也差"对照，证明是进程时间戳(started_at/code_changed mtime)/xlsx 元数据(openpyxl 创建时间)导致，与拆分无关。

### 32.2 统计桶 DIMENSIONS 驱动（第二步，commit 47efac3）
- `_empty_stats_bucket`/`_accum_bucket`/`_finalize_bucket` 从"split_*/truck_* 平铺硬编码"改成"基础桶 + 维度装饰器"。
- `DIMENSIONS` 定义 path(split/service 配票·反向 vs 带票) + transport(truck/ship) 两维度，
  每维度含 `keys`/`key_of`(车次归入哪个key)/`fields`(子桶累加来源)/`derivatives`(派生)。
- `_empty`：基础桶 + `_dims` 子桶挂载；`_accum`：累加基础 + 遍历维度累加子桶；
  `_finalize`：基础派生 + 子桶平铺回 `split_*`/`truck_*` + 维度派生平铺。
- 对外 JSON 形状一字不差(routes 零改动)；以后加新维度只需在 `DIMENSIONS` 加一项，
  不再改 _empty/_accum/_finalize/接口铺开 4 处（22 节踩过的坑）。
- 验证：118 车次对照(全量/按钢厂/按路径/按运输) md5 全一致；golden_master before vs after2 真实差异 0 处；
  compare_v2_flat_fields 113 笔全字段一致（5 笔 other_cost 口径差异是 v2 引擎 vs calc 旧引擎的历史遗留，与桶重构无关）。

### 32.3 附件真上传接口 + 手机端拍照直传（①，commit 23adcc9）
- 录入最大缺口补齐：过磅单/结算单/付款截图不再手工填路径。
- 新增 `POST /api/trips/<id>/attachments/upload`（multipart 真上传）：
  扩展名白名单 jpg/png/webp/heic/pdf，大小限 10MB；文件落 `uploads/trip_<id>_<时间戳>_<随机>.<ext>`，
  DB 存 `/uploads/<filename>`（前端可直接 `<img src>`/`<a href>`）；audit 记录上传动作。
- 新增 `DELETE /api/trips/<id>/attachments/<att_id>`（电脑端删）：物理文件移 `uploads/.trash/` 不破坏性删除；
  手机端不在 mobile 白名单，只能电脑端删（外勤误删可回电脑端找回）。
- 保留旧 JSON 接口 `POST /api/trips/<id>/attachments`（只填路径）兼容。
- mobile.py 写白名单加 `POST /api/trips/<id>/attachments/upload`，外勤现场拍过磅单直传。
- 前端：trips.js 附件区改 file 上传 UI（选文件+category+上传按钮），附件列表行图片缩略图/pdf 链接/删除按钮，手工填路径折叠为兜底；
  mobile.js openTrip sheet 加附件区 + 拍照上传（`input capture=environment`）。
- 验证：golden_master 480 接口非 200 仅 4 个(零 500 回归)；端到端测试 上传落盘入库/错误场景 4 种拦截/删除移 .trash/手机白名单放行上传拦截删除 全通过。
- 不碰算钱逻辑、不改 schema、不改现有接口行为。

### 32.4 备份保留标记 + 健康校验 + audit_log 归档（③，commit c5f7f2b）
- 数据安全增强，不碰算钱逻辑。
- **备份保留标记**：`pin_backup(name, pin)` 创建/删除 `<name>.pin` 标记文件；`prune_backups` 跳过有 `.pin` 的，月结后可手动锁定关键备份不被自动清理。API `POST /api/system/backups/<name>/pin {pin}`。
- **备份健康校验**：`_backup_health(path)` 只读（`mode=ro`）打开备份 db 查 trips 行数，损坏返回 `(None, False)`；`list_backups` 每份加 `trips_count`/`healthy`/`pinned` 字段，前端显示"X 车"或"损坏"。
- **audit_log 归档**：`archive_audit_log(conn, days=365)` 超期记录导出到 `data/audit_archive_*.json` 后删除；`start_background` 启动时自动归档；API `POST /api/system/audit/archive {days}`（<30 拒绝）。audit_log 无限增长会拖慢，默认 365 天归档一次。
- 前端 system.js/system.html：备份列表加"车次"列 + 📌保留标记 + 保留/取消按钮；备份卡片加"归档超期记录(>365天)"按钮；b-hint 文案更新说明保留标记机制。
- **不做（经深读评估）**：`regression.startup_check` 改增量——会漏"指纹没变但利润变了"（费率改导致老车次利润变）的核心报警场景，必须保持全量。性能优化（批量预取 trip_finance）留待车次上千时再做。
- 验证：golden_master 480 接口零 500 回归；端到端测试 list_backups 新字段/pin 标记+取消/损坏文件检测/prune 跳过 pinned(31=30+1)/archive 归档+文件校验/API pin+archive+天数拒绝 全通过。
- `/api/system/backups` 返回增强（加字段，向后兼容）；不改 schema。

### 32.5 直签带票(M2) 车次票源方补录 + 三方核账（数据校准，非代码改动）
- 起因：27 台直签带票(M2) 车次的 `trips.invoice_source_id` 一直是空的（录入时不确定，留空）。
  引擎按 `rate_links` 的 `service_fee_gross` 三级兜底落到「服务费单价全局兜底档 40 元/吨」（rate_links.id=6，
  `primary_entity_id=NULL`）。金额虽然正确，但报表「按票源方」维度看不见这批业务、额度水位也无法归集。
- 依据（两份外部账交叉核对，逐车吨位吻合）：
  1) 会计《2026华西发货明细8月(2).xlsx》汇总表 J 列「开票方」——带票批次的开票主体
     = 常州君盛 / 溧阳市力拓 / 无锡鑫景通 / 无锡兴德；
  2) 财务《发货记录表(8).xlsx》带票分表 4 张：林凯-君盛 / 林凯-力拓 / 林凯-鑫景通 / 林凯-兴德，
     逐车吨位与 (1) 完全一致，且与系统 `trips.base_id`（供货厂）一致。
- 动作：`fix_20260930_m2_invoice_sources.py`（幂等，含自检断言）
  1) 新建 4 条 `invoice_sources`（id 6~9）：source_type=front_company、settlement_type=direct、
     `rate_point=NULL`（不带点位）、`bound_base_id` 指向对应 `processing_bases`；
  2) 27 台 M2 车次 `invoice_source_id` 指向对应票源方（#14/68/69/74/79/87/91/92/98/99→君盛，
     #63/64/65/70/83/86/106→力拓，#66/78→鑫景通，#97/100/101/102/105/108/113/114→兴德）；
  3) `audit_log` 留痕 27 条。
- **不新增任何 rate_links**，服务费仍取「全局兜底档 40 元/吨」→ 算钱结果零变化。
- 验证：改动前/后逐车 `engine.compute_trip_finance_v2_flat()` **全字段差异 0 条**（119 台车次）；
  关键口径合计零变化（净利 201,394.43 / 量补 252,806.95 / 毛服务费 62,312.80 / 垫资 75,880.37 /
  提成 37,844.95 / 票差留存 30,638.99）；`tools_golden_master.py` 480 接口回归，差异只落在 27 台车次详情
  与 `/api/invoice_sources`、`/api/reports/summary`（票源方维度）、`/api/tracking`、`/api/dashboard/quota_levels`、
  `/api/system/audit` 等票源方相关处；抽检 `/api/trips/<id>` 全字段仅 4 处变化（invoice_source_id /
  invoice_source_name / invoice_source_settlement_type / updated_at），无金额字段。
- 备份：`backups/scrapmetal_20260930_151844.db`（改动前）+ `/tmp/scrapmetal_test.db` 副本先行试跑。
- **遗留待人工核对（未静默处理）**：车次 #86（2026-09-17｜29.79 吨｜苏DBE075｜含税 68,576.58）
  —— 系统 + 财务表《林凯-力拓》都挂 力拓，会计最终结算表挂 君盛，影响 ±506.43 元（29.79×17），两家一多一少、
  合计不变。本次按系统/财务表口径写「溧阳市力拓」以保持内部一致。
- 附带发现（财务表侧，供后续维护）：`林凯` sheet 实际 54 行（r2~r55），但 `林凯!L3` 抽成公式只取
  `SUM(B2:B46)`（到 09-01 为止），r47~r55 共 1,691.56 吨落在区间外；因配票提成 2026-09-05 起已改为 0 元/吨，
  对金额无影响，但公式区间不随数据延伸，建议改成整列引用以免以后漏算。

## 33. 两期封账写入（2026-09-30，v2.3.2）

**做了什么**：8月期+9月期（结算日 2026-07-26~09-25，109 台）按会计最终结果封账——
21 台 settled→closed（合计 109 台全 closed）；104 台填 `invoiced_date`+`invoice_out_amount`
（6 个欧冶销项开票批次，批→汇总行段→车全精确匹配，合计 23,017,355.85 分毫一致；
#104/106/107/108/109 会计上未开票，留空）；109 台 `is_paid=1`+`paid_amount=INT(引擎应付)`；
结案日期=批次付款时间（6 台补填）；两期剩余提成 8,241.90（林凯 11 车）登记「已付」批次。

**依据**：会计《2026华西发货明细8月(2).xlsx》（用户确认最贴近准确）。关键匹配发现：
汇总行可把一台船拆两行（r58+r59=#104 江淮货88888）；船次会计发货日(报单日)≠系统结算日
（#11/57/61）；6 开票批合计+未开票残行=两期含税总额 24,835,929.33 分毫吻合。

**不改**：引擎/费率/票源方/结算金额等算钱数据零改动（109 台引擎全字段回归零差异）。

**验证**：写入前整库备份 manual_20260930_163404.db；全部走系统接口（audit_log 留痕）；
/tmp 测试副本先行试跑；写后 regression.check 通过。

**不做什么**：进项票 invoice_in_amount 不逐车填（会计进票批次与车结算吨位混批+磅差，
且该字段语义仅益周使用）；10月期 9 台（#110~118）完全不碰；会计「未付」标注行
（r28/30/36/39/43/48/54，涉及 #57/62/80/98/99/100/101/102）属供应商内部结算列，
无法可靠映射，入报告待人工核对。

脚本 `fix_20260930_close_two_periods.py`（只读分析 `scripts/analyze_close_plan.py` 在
工作区）。核对报告《两期封账_写入核对_20260930.xlsx》。

## 34. 2026-10期新规则落库（2026-10-04，v2.3.3）

用户 2026-10-04 指令（结算日 2026-09-26 起生效）：
1. 票额俊兴 1000 万（可配票）
2. 邓晶焱带货带票，单期 1000 吨以上 5 元/吨提成
3. 除林凯外，其他报价统一 14 个点减 5 元；林凯正常 14 个点
   其他（量补、林凯带票 15 元）不变。

**落库内容（纯配置数据，不动引擎代码、不动任何车次金额）**：

- A. `invoice_sources#2 俊兴`：`quota_amount` 5,000,000 → **10,000,000**。
- B. `rate_links` 新增一条 `dispatcher_commission_daiPiao` / 调货人#5(邓晶焱) = **5.0 元/吨**，
  `effective_from=2026-09-26`。⚠「1000 吨以上」是期级门槛，费率档只表达单价；即时影响 0
  （本期尚无邓晶焱带票车次），期末按累计吨位判断是否计提。
- C. `rate_links` 新增 7 条 `account_external_settlement`（户头#1 + 调货人#2..#8 非林凯）
  = **13.8 %**，`effective_from=2026-09-26`。林凯(#1)不建行 → 自动落到户头通用档 14.0%。
  **13.8% = 14 个点让 5 元/吨**（用户在 #104 已确认 5 元/吨 ≈ 0.2 个点；系统里反向个人的
  13.8% 即同一口径）。若原意是「在 14 个点上再扣 5 元/吨」（≈14.2%），只改这 7 行的 rate_value。
- D. `template_nodes`：`split_company`(配票 M1) 的 `give_customer` 节点增加
  `secondary_type=dispatcher` / `secondary_ref=trip:dispatcher_id`，使「对外结算点位」可按调货人分档。
  **只改 split_company**；`split_reverse`(反向 M5) 原样不动 → 反向「对外 14% / 票源 13.8%」口径保持。
  引擎无需改动：`engine._rate_schedule_lookup` 早已支持 secondary_ref。

**验证**：写入前整库备份 `data/backup/manual_20261004_092746.db`；先在 /tmp 副本试跑；
逐车引擎全字段回归——**两期已封账车次（结算日 ≤2026-09-25）差异 0 台**，两期关键合计
（给客户付 18,400,301.95 / 净利 204,236.03 / 量补 252,806.95 / 垫资 36,922.45 / 提成 35,696.50）
分毫不变；仅新期 3 台非林凯配票车按新报价调整：
#115 给客户付 76,176.39→76,353.55、#116 61,799.77→61,943.49、#118 73,773.72→73,945.29
（各 含税×0.2%）。林凯 4 台仍为 14.0%，M2/M5 全部不受影响。

脚本 `fix_20261004_period_rules.py`（幂等 + 自检）。⚠ 写入该库需绕过沙箱
（沙箱会让 SQLite 报 `disk I/O error`，普通文件写入正常，非库损坏）。

**§34-补（2026-10-04 终态复验 + 热 journal 处置规程）**

- 复验脚本 `verify_20261004_final.py`（**只读生产库**）：在 `/tmp` 副本上删掉
  `effective_from >= '2026-09-26'` 的 8 条 rate_links 再重算，作为「旧规则」对照组。
  结果：**已封账期（结算日 ≤2026-09-25，109 台）差异 0 条**；
  新期 9 台中仅 `#115(调货人#6)`、`#116(调货人#5)`、`#118(调货人#5)` 三台 split_company
  车的 give_customer 各按 含税×0.2% 上浮（+177.15 / +143.72 / +171.57），
  `#110/#111/#112/#117`（林凯）与 `#113/#114`（service_fee_direct 带票）**全 0 变化**。
  分月期复算与经营报告基线逐项一致：
  08期 11 台 1,770,711.92 / 净利 5,334.01；09期 98 台 23,065,217.41 / 净利 198,902.02；
  10期 9 台 1,348,299.00 / 净利 −3,334.04（量补 0，期内累计 548.17 吨未达 3,000 吨档）。
- ⚠ **热 journal 处置规程（沙箱环境必备）**：若一次写库在 `commit()` 处被沙箱拦下，
  会在 `data/` 留下 `scrapmetal.db-journal`。此后**任何**连接（含只读）都会先尝试回滚该
  journal，回滚需要写权限 → 沙箱下直接抛 `disk I/O error`，看起来像「库坏了」，其实不是。
  正确处置：
  1. `cp scrapmetal.db /tmp/rec/x.db && cp scrapmetal.db-journal /tmp/rec/x.db-journal`
  2. 在 `/tmp`（可写）打开 `x.db` → SQLite 自动完成回滚 → `PRAGMA integrity_check` 应为 `ok`
  3. 用 sha256 比对副本与「该次写库前的备份」——一致即证明**生产库内容从未被改动**
  4. `cp /tmp/rec/x.db` 回写生产库 + `rm -f scrapmetal.db-journal`
  5. 复验：只读 `mode=ro` 打开无报错、`integrity_check=ok`、关键行数/金额对得上
  **不要**在沙箱内反复重试写库，也不要直接 `rm` journal 而不比对哈希。

**§34-补2（2026-10-04 邓晶焱「1000 吨」门槛语义明确化）**

- 问题：`rate_links` 只有「单价」，没有门槛字段；`dispatcher_commission_daiPiao` 按单价
  **无条件**计提。用户原话「1000吨以上一顿5元提成」有两种读法：
  (a) 当期带票累计 **≥1000 吨 → 当期带票吨位整体**按 5 元/吨；
  (b) 仅**超出** 1000 吨的部分按 5 元/吨。
- 判定依据：与「量补」档位同构——`mill_reward_tiers` 的「≥5,000 吨 → 25 元/吨」是**整期吨位**
  （9 月期 9,505.07×25 已验证），用户对同一句式「X吨以上，一吨N元」的既有理解即**整期口径**。
  故采用 **(a)**。
- 落库：`fix_20261004_gate_semantics.py` 只改 `rate_links#9` 的 `notes`，把两种读法、采用口径、
  系统承载方式、切换点写清楚；**rate_value 仍为 5.0**（回归复核：已封账期 0 差异、新期仍仅
  #115/#116/#118 三台变化，与本备注无关）。
- 系统承载：门槛是**期级条件**，无字段承载 → 期末按 `trip` 累计带票吨位复核；未达门槛时把该车
  `dispatcher_commission_cost` 覆盖为 0。切换口径只改本行 notes + 期末覆盖规则，不动 rate_value。
- 已同步到两个计算器：Excel `⑩ 配比计算器` 的 `B13` 改为
  `=IF($B$10="林凯",15,IF(AND($B$10="邓晶焱",$B$24>=1000),5,0))`，并把门槛提示并入 A38 警示链；
  HTML 版 `WHO.djy.gateTon=1000`。**修掉了一处假阳性**：此前邓晶焱带票不足 1000 吨时仍计提 5 元/吨。
  实测两版一致：带票 1200 吨 → 提成 5（净 14,400）；带票 180 吨 → 提成 0（净 3,060）+ 提示。

## §35 报价口径修正：「14个点减5元」= 14%点位 + 吨减5元（2026-10-04）

**起因**：§34 落库时把「除林凯外报价统一14个点减5元」折算成 13.8% 点位（推断"5元/吨≈0.2个点"）。
用户出示价格表样张 `2026-09-29（14-5）.png`（江阴华西特钢收购类型表，23 行）纠正：
实际口径为 **不含税价 = 含税价 × 0.86 − 5 元/吨，尾数截断**（23 行全部吻合：
2690→2308、2580→2213、2520→2162、2500→2145、2250→1930）。两者在 2,500 元/吨时
相差 10 元/吨且方向相反（13.8% 反而多付），必须修正。

**修正内容（`fix_20261004_quote_14minus5.py`，全配置无引擎改动）**：
1. `rate_links` #10..#16（account_external_settlement 非林凯 7 行）：13.8 → **14.0** pct。
2. 新增关系 `account_external_settlement_ton_adj`（per_ton）：
   (account#1×dispatcher#2..#8)=5.0（eff 2026-09-26）；(account#1,NULL)=0.0（eff 2020-01-01，
   林凯兜底+封账期兜底）。get_effective_rate 查不到返 0，未列组合天然安全。
3. 模板 1/2 节点重排：`give_customer`(rate_deduct) 改名 `give_customer_pct`(seq1) →
   新增 `give_customer_adj`(per_ton_rate_schedule, seq2) → `give_customer` 变
   `subtract_nodes`(pct − adj, seq3)；advance_fee seq 2→4。下游 `node:give_customer` 引用不变。
4. **配套数据迁移**（`fix_20261004_override_rekey.py`）：5 台封账车（#16/#19/#95/#96/#104）
   的手工点位覆盖键 `rate:give_customer` → `rate:give_customer_pct`
   （引擎按 `overrides[f"rate:{node_key}"]` 匹配，节点改名后旧键失配，#19 13.8% 等议价点位会丢）。

**回归**：已封账期 109 台逐车全字段 **0 差异**（08 期净利 5,334.01 / 09 期 198,902.02 复原）；
新期仅 3 台非林凯配票车变化：#115 给客户付 76,176.39→75,982.99（净利 +193.40）、
#116 →61,642.87（+156.90）、#118 →73,586.42（+187.30）；10 期净利 −3,334.04 → **−2,304.00**。

**两处教训**：
- **sqlite3 连接不 commit 就 close = 全部回滚**：脚本内自检读的是同连接未提交数据（看似成功），
  `close()` 后 /tmp 副本仍是原样，回写等于没写。修复脚本必须 `commit()` 后再 `close()`，
  且**回写后用独立只读连接复核关键行**（本例靠 verify 发现 13.8 残留=7 才暴露）。
- 节点改名必须同步检索 `finance_overrides` 里的 `rate:<node_key>` 键并迁移，
  新增节点键时先 `grep -rn node_key` 确认无代码/数据硬编码。

**工具侧同步**：计算器两版（Excel `⑩`、HTML）给客户付公式改 `含税×0.86−5×吨`，
派生参数新增「对外吨减 B19」，工具二速查改四列（配票·林凯 0.005P−19.895 / 配票·其他
0.005P−14.895 / 反向·林凯 0.002P / 反向·其他 0.002P+5）；新增「票额需求合计 +
现有额度最多可消化总货量倒算」（Excel 用 `IF(…>0,…,999999999)` 防除零，不用 IFERROR）。
默认算例（5000 吨/反向20%/带票30%/2500/其他）合计净收益 **148,694（29.74 元/吨）**，
可消化 7,000 吨、余 2,000 吨。

## §36 规则逐期保留与变更留痕（2026-10-04）

**起因**：用户问「费率与关系，上一期的规则是不是删除掉了？每一期的都要保留」。
核查结论：**规则行未删除**——`rate_links` 24 行全在，历史上靠「多行并存 + effective_to 到期」
保留（范例：林凯配票提成 #3=5 元/吨 生效至 2026-09-04 与 #4=0 元/吨 自 09-05 起并存，
09 期期内 9/5 切换由此可还原）。但发现 **3 处「就地改值」把旧值覆盖了**（均为本日修复动作）：

| 对象 | 旧值 | 新值 | 历史值现状 |
|---|---|---|---|
| `rate_links`#10~#16 非林凯对外点位 | 13.8%（误落） | 14.0% | 仅存于 audit_log（已回填） |
| `invoice_sources`#2 俊兴票额 | 5,000,000 | 10,000,000 | 仅存于 audit_log（已回填） |
| `template_nodes` give_customer | 单节点 rate_deduct | 拆为 pct−adj | 旧配置记入 audit_log |

**处置（`fix_20261004_rule_audit.py`，幂等，13 条）**：
1. 回填 `audit_log`：本轮 fix 脚本直改 SQL 绕过了系统审计（`rate_links`/`invoice_sources`
   此前 0 条审计），现将今日 9 类改动（俊兴额度、邓晶焱提成新增、13.8% 误落、口径修正、
   吨减新增、模板节点拆分、5 台车覆盖键迁移、门槛语义、规约本身）全部补写，并保留原始时间戳。
2. **立规约（已写入 audit_log (规约) 行）**：改费率/额度一律
   ① 关闭旧行（`effective_to` = 新规则生效前一日）② 新增一行承载新值 ③ 写 `audit_log`；
   **禁止就地改值**。系统已内置 `engine.upsert_rate()` 自动完成 ①+②，改费率应走该入口。
3. **新增 `rule_period_snapshot` 表**（period_key, item, value, source, captured_at；
   主键 period_key+item，`INSERT OR IGNORE`）：生成规则台账时把**各期实际生效值首次观测即冻结**，
   覆盖 `invoice_sources` 这类无生效区间字段的逐期留存（俊兴票额 08/09 期=500 万、10 期=1000 万
   已冻结）。今后任何一期一经观测，其规则值不再随库变动而漂移。

**工具**：`gen_rule_ledger.py` v2 → `规则台账.xlsx` 三页：
① 规则台账（当前生效 + 每类下「往期（已失效）」**折叠组**，Excel outline 一级、默认收起，共 6 行折叠）；
② 分期规则对照（14 项 × 各期，用 `engine.get_effective_rate` 以期首/期末双点解析，
期内切换显式标注，如 09 期配票提成「5 → 0（期内切换）」）；
③ 变更留痕（audit_log 规则类记录，倒序）。

**待升级（已知局限，未改）**：`invoice_sources.rate_point / quota_amount` 无生效区间字段，
逐期值只能靠 audit_log + 快照表；`mill_reward_tiers` 同样无生效区间。若需库内自洽的历史，
应把这两张表升级为带 effective_from/to 的版本（涉及 App 读写改造，另行评估）。

## §37 票源额度水位 A/B 拆分 + 规则台账按期分区重构（2026-10-04）

**起因**：用户要求 ①「票源额度水位分两部分：配票+反向的水位；自带票（不关心额度，只要知道本期多少）」；
②「规则台账不看失效项，按期区分，往期用抽屉折叠；常驻/按年分开标记；合并同类项——户头对外结算点位
一行即可，下面选择对哪些调货人生效」。

### 一、经营报告「票源额度水位」页 → A / B 两区（`gen_ops_report_any.py::sheet_quota`）

- **A 区｜配票 + 反向（占额度）**：列＝票源方 / 链条 / 水位口径 / 额度(元) / 开票·各期 / 周期内已用 /
  水位 / 剩余额度 / 状态。按链条分组（配票在前、反向在后，组内按用量降序）。
  - 额度口径：配票公司＝**月度·本期**；反向个人＝**年度·累计**（自然年累计前端开票额）。
  - 状态机：`○ 本期无用量` / `✓ 正常` / `⚠ 已过预警线`(warn_threshold) / `⚠ 水位偏高`(≥0.8) / `⚠ 超额度`。
  - 水位列挂 `DataBarRule`（0~1）。
- **B 区｜自带票（直签带票 M2，不占额度）**：按开票公司 × 期列**票面额**（= 含税结算额）+ 合计票面额 + 合计吨位。
- **归属判定**：`reverse_individual` → 反向；`front_company` 且有 `quota_amount` → 配票；
  其余 `front_company` → B 区自带票。**不得**把无额度的带票公司混进 A 表。

### 二、规则台账①页 v3 结构（`gen_rule_ledger.py`）

版面 9 列（A..I）：编号｜类别｜规则内容/明细｜当前值｜生效区间｜系统落点｜**属性**｜本期沿用?(您填)｜说明。

分区与属性（属性列 + 行底色同色）：

| 分区 | 属性 | 底色 | 内容 |
|---|---|---|---|
| 一、常驻规则 | 常驻 | 绿 | 对外点位(A1)/吨减兜底(A2)/带票提成(B1)/其他0(B4)/票源点位与档案(C1,C3~C7)/量补(D1,D2)/服务费垫资(E1~E3) |
| 二、按年规则 | 按年 | 紫 | 反向·闫东朋/尹鹏飞 年额 350 万（预警 180 万） |
| 三、计算口径结构 | 结构 | 蓝 | F1 报价口径 / F2 前端链条 / F3 反向净收益 / F4 期划分 |
| 四、本期(最新期) | 本期 | 黄 | 仅**逐期变动项**（A3/B2/B3/C2），★标本期新增或变动 |
| 往期抽屉 | 往期 | 灰 | 每个以往期一个折叠组（outline L1 + hidden），列该期上述 4 项实际生效值 |

- **合并同类项**：户头对外结算点位＝A1 单行 + 下方「3 行 × 8 人」勾选块：姓名行 /「生效」下拉
  （`DataValidation` `✅生效,⬜不适用`）/「吨减」逐人值（林凯 0、其余 5）。杜绝每人一行。
- **变动判定**：`changed_at(no, pk)` = `eff(期起始日) != eff(起始日前一日)`。
  这样 2026-09-05 发生的期内切换不会被误报为 10 期变动（B2 在 10 期显示「本期沿用上期，未变动」）。
- ②分期规则对照（14 项 × 各期，期首/期末双点解析）与 ③变更留痕（audit_log 倒序）保持；
  `rule_period_snapshot` 冻结仍为幂等 `INSERT OR IGNORE`。

**回归基线**：08 期 5,334.01 / 09 期 198,902.02 / 10 期 −2,304.00 分毫一致。

## §38 「费率与关系」页重构：按期分区 + 往期抽屉 + 同类合并 + 多选生效对象（2026-10-04）

**起因（用户原文）**：「费率与关系，我不需要看哪些失效的，我需要按照每一期来区分，往期的规则用抽屉
折叠起来。常驻的、按年的，这些都分开标记，我感觉这一页有点乱，一些是可以统一折叠的，例如户头对外
结算点位，下面可以选择对那些调货人生效，而不是每一个都列出来。」

**目标页面**：应用内「费率与关系」页（导航 `/rate_links`，`templates/rate_links.html` +
`static/js/rate_links.js`）。改造前该表是一张按 `relation_type` 排序的平铺大表：过期记录与在用记录
混排，且「户头对外结算点位」「户头对外结算吨减」各按调货人拆成 7 条独立行。

### 一、前端列表重构（`static/js/rate_links.js::renderTable`）
1. **按期分区**：新增 `periodKeyOf()`（26 日 ~ 次月 25 日，与引擎 `period` 同口径）与 `attrOf()`
   判定属性，把 `rate_links` 分成 4 个分区：
   - **常驻**（`effective_to` 为空、现在生效）—— 默认展开；
   - **当期**（`effective_to ≥ 今天`）—— 默认展开；
   - **未生效**（`effective_from > 今天`）—— 默认展开；
   - **往期**（`effective_to < 今天`）—— **按失效期分桶，整组默认折叠**，点分区行展开（抽屉）。
   分区行整行可点（`onclick="toggleSection(key)"`），展开状态存 `SEC_OPEN`。
2. **同类合并**：同一 `relation_type + primary + effective_from + effective_to` 的一批记录合并为 1 行，
   「细分维度」列显示「调货员 N 项（分档）」，「费率」列各界不同时显示「分档（N 档）」；
   行内「展开明细」按钮（`toggleGroup`）在下方列出逐项子行（可各自编辑/新增后续/删除）。
   → 户头对外结算点位从 7 行变 1 行，明细折叠在下面。
3. **属性徽章**：列头「状态」改为「属性」，徽章 = 常驻 / 当期 / 未生效 / 往期
   （`.tag-green` / `.tag-blue` / `.tag-gray`）。新增 `.rate-section-row` / `.rate-merged-row` /
   `.rate-subrow` / `.rate-checklist` 样式（`static/css/style.css`）。

### 二、新增/续期表单支持「选择对哪些调货人生效」
- 当费率类型的细分维度存在且为**新建**时，细分维度渲染为**多选勾选列表**（`entityChecklist`，
  新增 `.rf-multi-cb`）——勾选多个 = 逐个调用 `/api/rate_links/upsert` 各建/续接一条；
  全不勾 = 落一条不区分维度的通用档。编辑既有记录时仍为单选（避免批量误改）。
- **修掉一个静默数据风险**：原先编辑记录时，表单若未渲染细分维度控件，`saveForm` 仍会把
  `secondary_entity_id: null` 提交给 `PUT /api/rate_links/<id>`（该接口只更新提交的列），
  等于**默默清掉该记录的调货人维度**。现改为：表单未渲染该控件时**不提交**这两个字段。

### 三、费率目录补项（`engine.py::RELATION_TYPE_CATALOG`）
- 新增 `account_external_settlement_ton_adj`（「户头对外结算吨减」，primary=account，
  secondary=dispatcher，per_ton）。此前该关系只存在于数据库、目录里没有 → 页面显示原始代码，
  且**编辑时下拉无对应项会把关系类型改错**。
- `account_external_settlement` 的 `secondary_type` 由 `None` 改为 `dispatcher`
  （10 期起「对外点位」确已按调货人分档），使链条规则页可正常启用「按调货人细分」。

### 四、验证
- **逐车零影响**：在内存里把目录还原为旧版，对全部 118 台车逐字段比对
  `compute_trip_finance_v2_flat` → **差异字段 0 个**（目录是纯元数据，不参与取数）。
- **接口回归**（481 个 GET 快照）：差异 11 个 = 4 个已知时间敏感
  （`/api/version`、`/report`、`/api/report/pdf`、`/api/export/ledger`）
  + 7 个目录标签类（`/api/rate_links/relation_types`、`/api/rate_links/graph`、
  `/api/flow_templates/1~5/formula`）。公式接口内的示例金额与改造前一致（由上面的逐车零影响佐证）。
- **渲染实跑**：Node + DOM 桩加载真实接口数据执行 `renderTable()`，确认分区/折叠/合并输出
  （常驻区 23 条；户头对外结算点位 1 条合并行 + 7 项明细；2026-09 期往期抽屉已折叠）。
- `node --check static/js/rate_links.js` 通过。

## §39 首页看板「票源额度水位」拆 A/B 两区 + 月度额度窗口与结算期对齐（2026-10-04）

**起因（用户原文）**：「票源额度水位，分两部分，一部分是**配票+反向的水位统计**，一部分是**自带票**的
（我们不关心额度，但需要知道**这个月**多少了）」→「页面首页呢？我见UI没变」。

**澄清**：这条要求指的是**应用首页看板**（`/`，`templates/dashboard.html` 的「票源额度水位」面板
+ `static/js/dashboard.js::loadQuotaLevels`），不是 Excel《经营报告》里那一页（那一页此前已按同一要求
改成 A/B，本轮首页补齐，两边口径对齐）。

### 一、首页面板拆两区（`static/js/dashboard.js`）

| 分区 | 成员 | 展示 |
|---|---|---|
| **A · 配票 + 反向（占额度）** | `quota_amount` 有值的票源方 | 保留原「轨道+填充」水位条：占比%、预警线刻度、已用/额度、剩余或**已超额** |
| **B · 自带票（直签带票，不占额度）** | `front_company` 且 `quota_amount` 为空 | 车次 / 吨位 / **票面额**，末尾给合计；不画水位条 |

- 未设额度的**反向个人**（`reverse_individual` 且无额度）单列一行提示，提醒去基础档案补额度。
- 新增 CSS：`.quota-sec-head/.quota-sec-title/.quota-sec-note/.quota-sec-total/.tag-carrying/.quota-carry-amt/.quota-over`。
- B 区「票面额」取 `gross_amount`（含税结算额，带票公司自行开票口径），**不是** `used_amount`
  （前端开票额 = 含税 − 垫资费）：兴德本期 362,654.00（含税）而非 359,359.71。与《经营报告》B 区一致。

### 二、额度窗口与结算期对齐（`stats.py::_quota_levels`）

**问题**：月度额度原按「自然月 1 日至今」取数，与 26 日 ~ 次月 25 日的结算期错位——每月 1 日水位归零，
而 26~31 日已开的票又落进上一个月。实测 2026-10-04：10 期（9/26 起）俊兴已开 976,331.38，
首页却因为「10 月 1 日至今无结算日」显示 **0 车 / 0%**，B 区四家自带票也全是 0，面板整体失去意义。

**改法**：月度额度窗口 → `calc.get_period_bounds(today)`（默认 26 日 ~ 次月 25 日，与「各钢厂结算周期」
卡片、经营报告同口径）；年度额度窗口 → 自然年（`01-01 ~ 12-31`），额度按年给，与月期无关。

**接口新增字段**（纯追加，向后兼容）：`period_end`、`period_label`（本期/全年）、`period_span`
（如 `09-26 ~ 10-25`）、`trip_count`、`tonnage`、`gross_amount`。

**连带标签修正**：`routes_reports.py` 额度预警文案 `本月/年度` → 取 `period_label`（本期/全年）；
`templates/report_print.html` 与 `tools/report/report_pdf_tpl.html` 的额度文案同理改为
`period_label || (annual ? '全年' : '本期')`，避免「期口径的数字配自然月的标签」。
（`对账/华西废钢经营报告_20260922.html` 是历史归档件，不动。）

### 三、验证

- **改造后首页数据**（10-04）：俊兴 本期 7 车 404.94 吨 976,331.38/1000 万 = **9.8% ✓**；
  益周、煌烽 本期 0；反向-尹鹏飞 全年 2 车 1,090.63 吨 2,633,150.80/350 万 = **75.2% ⚠**；
  反向-闫东朋 31.7% ✓；自带票 B 区 兴德 本期 2 车 143.23 吨 票面 **¥362,654.00**，其余三家 0，
  合计 2 车 / 143.23 吨 / ¥362,654.00。
- **接口回归**（481 个 GET 快照，同库前后对照）差异 6 个：`/api/dashboard/quota_levels`（本改动，
  新增字段 + 窗口修正）、`/api/alerts`、`/report`、`/api/report/pdf`（额度数值与标签随之更新）、
  `/api/version`、`/api/export/ledger`（已知时间敏感）。
- **`/api/alerts` 实证零功能变化**：同进程内把 `_quota_levels` 换回旧口径做 A/B，
  逐行 diff 仅 `已用 75.2%，剩 ¥866,849（年度）` → `（全年）` 一处标签；预警条数 4 → 4，
  无新增/消失预警（俊兴 `warn_threshold` 为空且 <90%，不触发）。
- **前端渲染实跑**：Node + DOM 桩 + 真实接口数据执行 `loadQuotaLevels()`，确认 A/B 两区、
  分区说明、水位条行、B 区合计输出正确；`node --check static/js/dashboard.js` 通过。
- **顺手修掉一个静默口径缺陷**：`routes_reports.py::alerts` 与报告模板里硬编码的 `本月`，
  在窗口改为结算期后会变成错误标签（数字是期的、标签写月），一并改为读 `period_label`。

## §40 票源方「分期额度 / 接单状态」——把按月票额与中途停接单做成可配置（2026-10-04）

**起因（用户原文）**：「前端公司，票额，也有时限（按月）。例如煌烽、益周 10 月期已经没票额了，不计入了。
另外，AB 区票额水位，A 区的话配票和反向也分个小 ab，分开好一些。此外，闫东朋反向，目前是不接了，
仅仅 9 月份接他的单，10 月份不给他配票接单了。此外，整个系统你也捋一下，把这种业务自由度，时效性
之类的，都升级一下，而不是这样写死。」

**问题**：`invoice_sources.quota_amount` / `warn_threshold` 是**单值**列，表达不了「这家这期有票额、
下期没了」，也表达不了「某家从某期起不再接单」。于是水位页只能一直按老额度画。

### 一、新表 `invoice_source_terms`（票源方 × 生效区间 → 额度/预警线/是否接单）

```sql
CREATE TABLE invoice_source_terms (
  id, invoice_source_id REFERENCES invoice_sources(id) ON DELETE CASCADE,
  effective_from TEXT NOT NULL,      -- 生效起（含）
  effective_to   TEXT,               -- 生效止（含）；NULL = 长期/至今
  quota_amount   REAL,               -- 该时段额度；NULL = 本期无票额
  warn_threshold REAL,               -- 该时段预警线；NULL = 沿用档案
  accepting      INTEGER DEFAULT 1,  -- 0 = 已停止接单
  notes, created_at);
```

- **语义**：`accepting=0` → 已停止接单；`accepting=1` 且 `quota_amount IS NULL` → 本期没有票额。
  **两者都不计入水位**，但仍原样返回给前端，由前端用一行灰字说明「本期不计入：某某（原因）」，信息不丢。
- **向后兼容**：水位计算按「今天」找命中行，**没有命中行就回退 `invoice_sources` 的档案默认值**
  （俊兴/尹鹏飞等未做分期的票源行为完全不变）。
- 落库脚本 `fix_20261004_source_quota_terms.py`（幂等 + 备份 + audit_log 留痕），已写入 3 家 6 条：
  益周/煌烽 `2026-09-26 起 quota 置空`（本期无票额）、反向-闫东朋 `2026-09-26 起 accepting=0`（停止接单），
  并各自留一条 `2026-07-01 ~ 2026-09-25` 的历史行（追溯当时额度）。

### 二、`stats.py::_quota_levels` 改造

- **角色不再靠「有没有额度」猜**：改为按车次结算模式判定 `QUOTA_ROLE_MODES`
  （`peipiao`←M1 / `reverse`←M5 / `carry`←M2~M4），没有车次的新档案才回退档案列。
  否则「某家本期没票额」会被误判成自带票、掉进 B 区。新增字段 `role` / `role_label`。
- 新增 `_source_term_row(conn, source_id, on_date)` 与 `_has_table()`（老库没建表时自动退化）。
- 新增返回字段 `excluded` / `exclude_reason` / `term_notes`；被剔除项 `quota=None`、不参与预警。

### 三、首页看板「票源额度水位」→ A 区再拆 A1/A2

- **A1 配票（前端公司 · 额度按月）** / **A2 反向（自然人 · 额度按年）** 两个小分区，各带小标题。
- 剔除项与「未设额度上限」的票源方合并成一行灰字提示（`quota-skip`）。
- 新增 CSS：`.quota-sub-head` / `.quota-skip`；角色徽章改用后端 `role_label`。

### 四、基础档案页新增「票源额度分期」tab（可自助维护）

- `archives_config.ARCHIVE_TABLES` 加表 → 通用 CRUD（`/api/invoice_source_terms`）自动生效；
  删除走回收站、修改走 `audit_row_diff` 留痕。
- `archives.js`：新增 tab、字段配置（含 `checkbox` 接单中、`computed` 计算列「涵盖期」
  —— 由生效区间反推期号，如 `2026-01期 ~ 2026-09期`）、顶部说明条 `TAB_NOTES`。
- `system_ops.TABLE_CN` 补「票源额度分期」（否则变更留痕里显示原始表名）。

### 五、经营报告 Excel（`gen_ops_report_any.py::sheet_quota`）同步

- 判定基准日 = **所报最新一期起始日**（报告可复现，不随生成日漂移）。
- A 区同样拆 A1 配票 / A2 反向；本期无票额/已停接单的在表下以一行「本期不计入」列出。

### 六、验证

- 接口：`/api/invoice_source_terms`（新增，含 3 条详情路由）返回 6 行；`/api/dashboard/quota_levels`
  实测 闫东朋 `excluded=已停止接单`、益周/煌烽 `excluded=本期无票额`；俊兴 9.8%、
  反向-尹鹏飞 75.2%⚠；`/api/alerts` 4 条、无新增预警。
- 首页看板：Node + DOM 桩实跑 `loadQuotaLevels()` → A1/A2 小分区 + 剔除提示 + B 区合计均正确。
- 档案页：DOM 桩实跑 `renderTable()` → 6 行、「涵盖期」计算正确、说明条渲染正确。
- **481 接口 A/B**（`git archive HEAD` 导出改造前代码，同库前后对照）：新增 4 个接口，
  差异 8 处 = `/api/dashboard/quota_levels`(本改动) + `/archives`(加说明条 div) + `/report`(额度随之更新)
  + `/api/system/audit`、`/api/system/verify`(仅 `TABLE_CN` 由原始表名变「票源额度分期」)
  + 3 个已知时间敏感(`/api/version`、`/api/report/pdf`、`/api/export/ledger`)。
- **逐车利润零影响**：对全部 118 台车逐字段比对 `compute_trip_finance_v2_flat` → **差异 0 字段**
  （本改动不碰 `engine.py`/`calc.py`）。三期净利 08=5,334.01 / 09=198,902.02 / 10=−2,304.00 分毫未变。

## §41 「经营报告（分期与专题）」Excel 一键导出（2026-10-07）

**起因**：经营报告 Excel（总览/各期明细/调货人工资/量补汇总/票源额度水位）一直靠工作区脚本
`gen_ops_report_any.py` 手工生成，用户要求：先出一份最新数据的，再在网站里加按钮一键导出。

### 一、移植 `ops_report_export.py`（工作区脚本 → 系统模块，逻辑不变）

- `build_workbook(periods_param)`：缺省自动探测库中全部期（26~25 结算期），也接受
  `"2026-08,2026-10"`；返回 `(wb, periods, scope, sel)`。库路径走 `_db_path()`
  （SCRAPMETAL_DB 可覆盖），只读连接 + engine 实时逐车口径，与页面一致。
- CLI 保留：`python3 ops_report_export.py [--periods …] [--out 文件名]`，缺省写 `data/reports/`。

### 二、接口与按钮

- `GET /api/export/ops_report[?periods=2026-10]`（routes_reports.py）：BytesIO + send_file，
  文件名 `经营报告_<期串>_<日期>.xlsx`；期号不合法返回 400 + 中文错误。
- 统计报表页（/reports）头部新增「导出经营报告 Excel」按钮（在「导出经营报告 PDF」左侧）；
  `reports.js::exportOpsReportXlsx` 用 fetch 下载（完成/失败都恢复按钮，报错弹中文提示）。

### 三、验证

- CLI 与工作区脚本输出逐数一致（08 期 11 台 / 09 期 98 台 / 10 期 12 台 净利 -4,267.49，
  含 10-05 录入的 #120-122 三车）。
- 接口实测：全部期 200（7 个 sheet）、单期 200（5 个 sheet）、非法期 400；票源额度水位页
  俊兴 10 期 1,533,546.98 / 1000 万=15.3%，与首页一致。
- 481 接口 A/B（git archive HEAD 同库对照，501 端点）：新增仅 `/api/export/ops_report`，
  共同接口差异仅 `/reports`（按钮）+ `/api/version`、`/api/report/pdf`（已知时间敏感）。

## §42 功能自检修复：批量录入「认列」纠错 + 车次删除入口下沉（2026-10-09）

**起因（用户原文）**：「1 功能逻辑能不能自检一遍，很多功能是不完善的，例如新建车次点错无法删除，
此外，录入车次我记得之前有批量录入自动识别列的功能，现在怎么没了。2 功能复杂性，是否可以适应性的降低一些？」

自检结论：**断链 0 条**（模板 onclick → JS 函数、JS 里的 /api 调用 → 后端路由全部闭合）、
运行时代码无 TODO/FIXME；用户感觉"功能没了/不完善"的根因是**入口命名不清 + 认列算法有 bug**，不是功能缺失。

### 一、「批量录入（粘贴·自动认列）」= 原来的「导入结算单」

功能一直在车次管理页工具栏（`import_settlement.js`：粘贴整块表格 → 自动认列 → 匹配车次 → 推送已结算）。
本次做了三件事：

1. **按钮改名**：`导入结算单` → `批量录入（粘贴·自动认列）`（`templates/trips.html`），
   title 里把能认的列一次列全，并说明"认错可用下拉改"。
2. **修认列 bug**（`import_settlement.js::impGuess`）：旧算法用「非空单元格」算覆盖率，
   于是**某一两行才有值的稀疏列（如只在一车出现的切割费 300）也会被当成业务数字列**，
   抢走吨位/扣杂的位置——用户实测「吨位 24.668 被认成扣杂、300 被认成吨位」。
   改法：① 数字列要求**整列非空覆盖率 ≥80%**；② 补上注释里一直承诺、却漏实现的
   **「吨位 × 单价 ≈ 金额（±2%）」一致性校验**来定吨位；③ 扣杂取剩余列里均值最小的。
3. **新增「切割费」列角色**：`IMP_ROLES`/`IMP_HEADER_KEYS` 加 `cutting`（表头匹配 切割|加工费），
   分组与 apply 一并透传；`routes_reports.py::import_settlement_apply` 把 `cutting_fee` 写进
   `/api/trips/<id>/details`（该接口本来就支持，之前导入链路没接）。

### 二、新建车次点错删不掉 → 删除入口下沉（`static/js/trips.js`）

原状：列表点行只开「明细抽屉」，抽屉里只有「详情 →」；删除藏在二级的详情弹窗里，不知道的人找不到。
改法：`voidTripById(id)` / `deleteTripById(id)` 抽成按 id 调用，**明细抽屉底部直接放「作废 / 删除」**；
抽屉里若是「无车号 + 无明细」的空壳车，额外给一行灰字说明（这是新建没填完的空车，点删除即可）。

### 三、顺带修一个隐性 bug

`ops_report_export.py::_db_path()` 之前只认 `SCRAPMETAL_DB`，用 `SCRAPMETAL_TEST_DB` 跑副本时
会静默去读仓库自带的 `data/scrapmetal.db`（副本里没这个文件就直接 500）。
已改为 `SCRAPMETAL_DB → SCRAPMETAL_TEST_DB → 仓库库` 三级回退（与 `db.py` 同源）。

### 四、验证

- 认列单测（Node 直接调 `impGuess`）：带表头 / 无表头 / 钢厂单三种样例，**修复后无表头样例的
  「0.28=扣杂、24.668=吨位、稀疏列=忽略」全部正确**（修复前为 0.28=忽略、24.668=扣杂、300=吨位）。
- 导入端到端（副本库）：preview 200 → apply 200 → 明细落库 `cutting_fee=300.0`、车次推到已结算。
- 抽屉渲染（Node + DOM 桩）：空壳车与正常车都渲染出「作废/删除」，空车提示只对空壳车出现。
- **519 接口 A/B**（`git archive HEAD` 同库对照）：差异仅 `/trips`、`/trips?open=5`（按钮文案，
  4130→4354 字符）+ 3 个已知时间敏感（`/api/version`、`/api/export/ledger`、`/api/report/pdf`、
  `/api/export/ops_report`，仅 md5 随时间变、status/len 一致）。

## §43 降复杂度三档：治乱（A）→ 瘦身（B）→ 收口（C）（2026-10-09，v2.4.0）

**起因**：紧接 §42 的自检，用户对同一个诉求（「功能复杂性，是否可以适应性的降低一些？
感觉现在设定规则和读取数据信息，阅读性很差，要消化理解半天」）选定「ABC 按顺序来」——
即三档全做，先治乱、再重整页面、最后收口参数。**本次改动一行算钱逻辑都没碰**：
改的全是信息呈现层级、术语解释、入口位置、以及把写死的显示阈值变成可配置。

### A 档 · 先治乱（看得见、找得到）

1. **车次详情 21 个字段分三组折叠**（`static/js/trips.js::renderModal`）：
   - 组1「核心信息」7 项（接单日期 / 车号 / 链条规则 / 供货厂 / 钢厂 / 调货人 / 票源方）→ **默认展开**
   - 组2「单据与日期」9 项（类型 / 户头 / 开票路径 / 结算单号 / 到厂 / 结算 / 开票 / 结案 / 混料）→ 收起
   - 组3「司机 · 物流 · 备注」5 项 → 收起
   一个按钮 `toggleInfoGroups()` 在「展开全部字段 / 只留核心字段」间切换，且**按钮文字跟着状态变**。
   字段 id 全部保持不变 → `bindInfoAutoSave()` 的自动保存、利润试算、三条线全部照旧（DOM 里的
   input 即使折起来也在，绑定不受影响）。
2. **全站术语悬停**：`app.js` 新增 `TERMS`（20 个词）+ `term(text,key)` / `termTip(key)` 助手，
   渲染成 `<span class="term" title="…">`（虚线下划线 = 可悬停）。已铺到首页 KPI 与面板标题、
   报表页 KPI/表头/趋势标题、车次详情（链条规则/票源方/户头/混料）、首页水位区（配票/反向/自带票）。
3. **首页三个主行动按钮**（`templates/dashboard.html` 的 `.quick-actions`）：
   「新建车次」→ `/trips?new=1`、「批量录入」→ `/trips?import=1`、「回款追踪」→ `/tracking`。
   `/trips` 侧新增两个一次性参数：`?new=1` 直接建一车并聚焦车牌框、`?import=1` 直接开批量录入框；
   **参数用完立刻 `history.replaceState` 抹掉**，避免一刷新又建一车 / 又弹框。

### B 档 · 重整页面（一屏能读懂）

1. **统计报表瘦身**（`templates/reports.html`）：核心只留 3 块——KPI + 利润构成、趋势、四张维度汇总表；
   其余（按钢厂/调货人利润柱状图、按运输方式、按结算路径、质量两张表、数据完整度）整体收进
   `<details class="more-analysis">`「更多分析」，默认收起。**DOM 全部保留** → `reports.js` 的
   `renderChart/loadQuality/loadCompleteness` 一个字没改，仍照常写入这些容器。
2. **车次列表列可隐藏**（`templates/trips.html` + `trips.js`）：工具栏加「列设置」，
   10 个数据列（日期…进度）可勾选显隐，选择存 `localStorage['scrapmetal.trips.hiddenCols']`，
   跨会话记住；「全部显示」一键复位。实现是给 `th`/`td` 打 `data-col`、靠 `applyColumnPrefs()`
   挂 `.hidden-col` 类，**不重排 DOM**，所以导出 CSV 的列序、批量选择、内联推进按钮都不受影响。
3. **链条规则只读「一眼看懂」视图**（`templates/chain_rules.html` + `chain_rules.js`）：
   顶部切「一眼看懂 / 编辑」，**默认落在只读视图**。只读视图每条规则一张卡：名称+简写+模型
   （差价/服务费）+在用 N 车 + ①流程线（按 `seq` 排出主体 chip，配色按角色）②"赚哪一段"一句话
   ③"参与算钱的口子 N 个"（列出挂了计算节点的环节）④说明。数据来自 `/api/flow_templates/<id>/graph`，
   只读、不写库。切到「编辑」才 `renderTemplateList()` 构建可编辑卡片（**懒构建**——以前一进页面就
   为每条规则拉图渲染，现在只读视图不付这个代价；`CARDS_BUILT` 标记保证只建一次）。

### C 档 · 收口（把写死的数字拿出来）

1. **新增 `settings.py`**：一张 `SETTINGS_CATALOG` 声明 13 个参数（类型/默认值/单位/取值范围/一句人话说明/分组），
   值仍存在既有的 `app_settings(key,value)` 表（不新建表）。读取统一 `settings.get(conn,key)`——
   **读不到 / 不是数字 / 越界，一律回退默认值**，配置填坏不能影响算钱。边界很明确：
   **只收「提醒阈值 / 显示比例 / 维护参数」，费率/点位/额度/量补阶梯仍留在各自页面**
   （那些改了要按生效时间留痕，不能放进随手可改的输入框）。
   收口的 13 项：`quota_warn_pct`(90) `quota_high_water_pct`(80) `report_stale_days`(3)
   `invoice_overdue_days`(7) `payment_overdue_days`(30) `plate_stable_ratio`(0.8) `fleet_alias_ratio`(0.5)
   `aging_days_1/2/3`(15/30/60) `keep_auto_backups`(30) `recycle_days`(30) `mobile_cookie_days`(180)。
2. **新接口** `GET/PUT /api/system/settings`（`system_ops.py`）：GET 返回清单+当前值；PUT 逐项校验
   （不在目录里的键忽略、非数字 400、越界 400）并写 `audit_log`。手机端加入 `MOBILE_READ_BLOCKED`。
3. **新页面「系统 → 参数设置」**（`templates/system.html` + `system.js`）：按分组渲染可编辑行，
   保存后 toast 提示改了几项。
4. **各消费点改为读参数（默认值 = 原来的写死值）**：`routes_reports.py`（额度预警线、报备超期、
   开票/回款超期，连提醒文案里的天数也一起跟着变）、`lanes.py`（账龄分桶，标签格式与旧版一字不差；
   三档填得不递增则整体回退 15/30/60）、`entry.py`（车牌带出稳定门槛、车队判定占比）、
   `ops_report_export.py`（水位偏高线）、`system_ops.py`（备份保留份数、回收站天数）、
   `mobile.py`（登录有效期）、`stats.py`（把全局预警线作为 `default_warn_pct` 附在额度水位每一行上，
   前端不再写死 90）。
5. **新增页面「术语速查」**（`/glossary` + `templates/glossary.html`，导航新增一项）：
   四张表——①常见事情怎么做 ②说法对照（你可能怎么说 ↔ 系统叫什么 ↔ 在哪个页面）
   ③术语一句话解释 ④批量录入的列是什么意思。带搜索框，跨四张表过滤。
   导航是 `base.html` 的公共片段 → **所有页面 HTML 都会变**（见验证）。

### 验证（全部通过，无未归因差异）

- **JS**：`node --check` 全绿；`renderModal` 用 Node + DOM 桩实跑：3 组、默认只开第 1 组、
  7 个核心字段、21 个字段齐全、`toggleInfoGroups` 两次点击状态与按钮文字都对；
  `plainCard/plainSteps` 用 DOM 桩实跑：流程线 4 个主体 chip、假金额环节带"不参与算钱"提示、空图容错。
- **后端冒烟**（`/tmp` 副本库 + Flask test_client）：参数清单 13 项且全为默认值；改
  `report_stale_days`/`invoice_overdue_days`/`quota_warn_pct`/`quota_high_water_pct` 后
  对应提醒与染色**确实跟着变**；越界 400、非数字 400、未知键忽略；账龄三档填不自增 → 整体回退默认；
  `/glossary`、`/`、`/trips`、`/reports`、`/chain_rules`、`/system?tab=settings` 全 200。
- **518 接口 A/B**（`gm_before` 用改前代码 + 同库）：新增 2 个接口（`/glossary`、`/api/system/settings`），
  消失 0 个；其余 18 个差异**逐条归因完毕**：
  ① 12 个页面 = `base.html` 导航新增「术语速查」+ 本页新增控件（逐行 diff 确认只有这些）；
  ② `/api/dashboard/quota_levels` = 只新增 `default_warn_pct` 字段，其余字段**逐字段比对完全一致**；
  ③ 4 个时间/压缩戳敏感接口（`/api/version`、`/report`、`/api/report/pdf`、`/api/export/ledger`、
  `/api/export/ops_report`）——同代码连跑两次同样会差，已单列证明。
  **关键：所有金额/统计接口（`/api/trips*`、`/api/reports/*`、`/api/dashboard*`（除上面那一项）、
  `/api/analytics/*`、`/api/entity_stats*`）全部一字不差。**
- **经营报告 Excel**：新旧代码各生成一份，**7 个 sheet 逐格比对完全一致**（`/api/export/ops_report`
  的 md5 差异纯属 zip 时间戳）。
- **算钱全字段回归**：`legacy/compare_v2_flat_fields.py` 在新旧代码下**输出逐字节相同**
  （128 笔中 11 笔差异是 calc 旧引擎 vs v2 引擎的历史遗留基线，与本次改动无关）。

### 不做什么

- 不动 `engine.py` / `calc.py` / 任何算钱公式、不动 `trip_finance` 固化数据。
- 不把费率/点位/额度/量补阶梯搬进「参数设置」（会破坏"按生效时间留痕"的规矩）。
- 不做用户界面主题改版、不引入前端框架、不动 `trips.js` 的既有函数命名（只在其上叠加）。


---

## §44 死代码清理：3 个孤儿 JS 函数（2026-10-09，v2.4.1）

上一轮全仓自检标记的 3 个**无任何调用点**的 JS 函数，本次一并清掉：

| 文件 | 函数 | 原用途 | 为何已成死代码 |
|---|---|---|---|
| `static/js/archives.js` | `fk(table, label)` | 生成「外键下拉」字段配置的简写 | `CONFIGS` 里的外键列早已全部手写完整对象（还要带 `required`/`source`），无一处调用 |
| `static/js/reports.js` | `transportCell(count, tonnage, cls)` | 车、船各占一列的旧渲染 | 已改为 `carShipCell(r)` 合成一列（只写次数、吨位放悬停），旧函数没删 |
| `static/js/trips.js` | `settlementDocPill(r)` | 列表「结算单」列的「已出/未出」徽标 | 列表进度列统一走 `progressBadges(r)`，该列不再单独渲染 |

**注意**：`settlementDocIssued(r)` **保留**（`progressBadges` 仍在用）。本文档
2026-09-19 那节「列表『结算单』列 → 抽成 `settlementDocPill(r)`」是**当时的历史记录**，
其设计已被 `progressBadges` 取代，该函数本次删除；历史节保留原样不改，以免抹掉沿革。

### 验证

- `node --check` 全部前端 JS 通过（`archives` / `reports` / `trips` / `app` / `dashboard` / `system` / `chain_rules`）。
- 全仓检索 `fk(` / `transportCell` / `settlementDocPill`：除本文档的历史叙述外**无任何引用**。
- 纯前端死代码删除：未动模板、未增删路由、不碰算钱代码，故接口集合与响应体**零变化**，
  无需重跑 518 接口 A/B（前端死代码不参与任何服务端渲染）。

---

## §45 票点成本「按期可配」收口 + 运行库迁移（2026-10-09，v2.4.2）

### 为什么做

用户 2026-10-04 的原话：「每一期都有新的结算规则和**票点成本**…整个系统你也捋一下，把这种业务自由度、
时效性之类的都升级一下，而不是这样写死。」§43/§44 已经把 13 个写死阈值搬进了「参数设置」，
**票点成本是最后一块写死的业务自由度**：`invoice_sources.rate_point / outbound_rate_point` 是单值列，
改一次就把 8、9 月期的历史口径一起改了，且没有留痕。

### 做了什么

代码 2026-10-04 就已写完（`engine._effective_invoice_source` + `schema.sql` 两列 + 幂等迁移脚本），
但**一直在工作区没提交、迁移也从没跑过**——表里没有这两列，功能等于空转。本次收口补齐三处缺口：

1. **迁移真正落到运行库**（此前从未执行）：`invoice_source_terms` 加 `rate_point` / `outbound_rate_point`
   两个可空列，6 条现有分期记录一律留空 = 沿用档案列。
2. **补齐两个入口**（此前即使迁了表也填不进去）：
   - `archives_config.py::ARCHIVE_TABLES["invoice_source_terms"]` 加这两列（后端读写白名单）；
   - `static/js/archives.js` 的「票源额度分期」表单加两个数字输入 + 顶部说明改口径。
3. `engine.py` 三处调用点全部改走 `_effective_invoice_source(conn, src, as_of)`：
   `rate_deduct_invoice_source`（默认值 + 节点）、`rate_spread_invoice_source`、
   以及 `compute_trip_finance_v2_flat` 里反向自然人的 `front_actual_paid` 默认点位。

**口径**：`as_of = settlement_date or order_date`（与 `get_effective_rate` 的既有约定一致）；
命中分期记录且该字段非空 → 用分期值；没命中 / 字段留空 / 老库没这张表 → **原样回退档案列**。

### 迁移执行过程（重要，下次会再遇到）

第一次直接对运行库跑迁移脚本时，SQLite 抛 `disk I/O error`，并**在主库目录留下热日志
`data/scrapmetal.db-journal`**；热日志一旦留下，后续连读都失败（`PRAGMA integrity_check` 都报 I/O 错误），
删掉日志则会把"已写进主文件但未提交"的 DDL 固化下来 —— 本次就是这样留下了「加了列、没有留痕」的半迁移状态。

- 现象定位：普通文件字节读写（`open('r+b')` + `write` + `fsync`）**正常**；新建小库也正常；
  只有对**已有的 471KB 实时库**做 SQLite 写事务时随机失败（同一操作重试有时成功）。
  沙箱开与关都复现过，且 `data/` 下还留着上一会话（2026-10-08）的 `_probe_io.db-journal` 等同类残骸
  （本次已一并清掉）。
- 可靠做法（本次采用）：**在 `/tmp` 副本上迁移，再整文件换回**。
  1. `cp data/scrapmetal.db /tmp/x.db`（先记 md5）；
  2. `SCRAPMETAL_DB=/tmp/x.db python3 fix_20261004_source_terms_points.py`（脚本自带备份）；
  3. **全表逐行核对**（见下）确认迁移影响面；
  4. 再次 md5 运行库，与第 1 步一致（说明期间无写入）→ `cp /tmp/x.db data/scrapmetal.db`；
  5. 删残留 `-journal`，用 `vfs=unix-none` 只读复核（`PRAGMA integrity_check` + 列表 + 计数）。
- **另一个坑**：失败留下的热日志不要直接删。本次删掉后状态被固化，只能从 `data/backup/` 的
  迁移前备份重做一遍才拿回完整留痕。若必须清，先确认主文件里已无未提交的 DDL 影响。
- 恢复用的干净备份：`data/backup/manual_20261009_102910_source_terms_points.db`（迁移前，audit 止于 #848）。

### 验证

- **迁移影响面（全表逐行 md5 比对，34 张表）**：迁移前快照 vs 运行库 → 仅 `invoice_source_terms`
  多两列、**无一行内容变化**（同时证明 10:29:10 之后没有业务写入）；迁移后 vs 迁移前 → 仅
  `audit_log` +1 条留痕（id 849）+ 上述两列，其余 33 张表逐行相同。
- **按期选择**（`_effective_invoice_source`）：as_of 06-01 → 回退档案 12.8；07-01、09-25 → 命中 term#1；
  09-26、10-05 → 落在 term#2 但其票点留空 → 回退档案。全部符合。
- **零影响**：所有分期票点留空时，「改后引擎」vs「改造前行为（恒等回退）」对 128 笔车次
  × 11 个金额字段 **逐字节相同**。
- **真生效 + 定量**：
  - 益周（pass_through）term#1 填 10.0/9.5 → 恰好 15 笔变化，全部落在 07-01~09-25 且全为益周；
    `front_actual_paid` 变化率恒为 4.6243% = (1−9.5%)/(1−13.5%)；
  - 反向-闫东朋（direct，走个人垫资）term 填 11.8 → 恰好 8 笔变化，Δ = **票面额 × 2%** 逐笔误差 0.00；
  - 俊兴（配票）09-26 起填 11.0 → 恰好区间内 16 笔变化，区间外 47 笔不动；
    Δ = 票面额 × 2.5% 逐笔精确，且 `票面额 = 结算额 − 吨位×23`（配票垫资费）逐笔吻合。
  - 三组都验证「把覆盖值清空 → 立刻回到基线」。
- **HTTP 写入路径**（test_client + 副本）：PUT 带 `rate_point` → 200 且落库；算钱按预期变化（15 笔）；
  复位为 null → 回基线；提交目录外字段 → 400 `empty payload`，行内容不变。
- **算钱全字段回归**：`legacy/compare_v2_flat_fields.py` 在迁移后副本上仍是 117 笔全一致 /
  11 笔历史基线差异（calc 旧引擎 vs v2 引擎，与本次无关）。
- **运行库实测**：重启后 `/api/version` = 2.4.1→2.4.2；`/api/invoice_source_terms` 返回 11 个键
  （含两个新字段且为 null）；`/`、`/archives`、`/glossary`、`/trips`、`/system?tab=settings` 全 200。

### 不做什么

- 不给现有 6 条分期记录预填任何值（预填等于替用户改口径）。
- 不改 `calc._invoice_cost_rate_pct` 的既有语义（pass_through 取 outbound、其余取 rate_point）。
- 不动 `rate_links` 那套生效区间机制：票点走票源方分期表，不并进 rate_links。

---

## §46 2026-10 期切割费「不含税扣」（给客户付口径，一次性）（2026-10-09，v2.4.3）

### 为什么做

用户 2026-10-09：「10月期切割费计算，不含税扣，先按照吨位和单价计算钢厂总含税价格，再计算税后，
再减去」。经两轮确认：落点是**「给客户付」（应付供货方货款）**这一项，税后系数沿用报价口径的
**14 个点（×0.86）**，范围**只 2026-10 期一次性**。

### 口径（改动前 → 改动后）

切割费以前**先扣在含税侧**再整体 ×(1−点位)，于是被稀释成只承担 86%：

| | 公式 | #131 苏DBE961（林凯，吨 73.58、单价 2330、切割费 300） |
|---|---|---|
| 车次结算金额（含税） | 吨位×单价 − 切割费 | 171,441.40 − 300 = 171,141.40（**不变**） |
| 给客户付（改前） | 结算金额 × (1−14%) − 吨位×5(非林凯) | 171,141.40 × 0.86 = **147,181.60** |
| 给客户付（改后） | 吨位×单价 × (1−14%) − 吨位×5 − 切割费 | 171,441.40 × 0.86 − 300 = **147,139.60** |

即切割费改在**税后（不含税）层全额扣**：客户承担切割费全额（改前只承担 86%，余下 14% 留在我们这边，
净利润同步 +14%×切割费）。两式相减恒为 **−对外点位% × 切割费**（#131 = −42.00），
这也是本次全部差异的唯一来源。

### 实现

只动 `engine.py` 两处：

1. 文件上方新增 `CUTTING_FEE_AFTER_TAX_WINDOW = ("2026-09-26", "2026-10-25")` + `cutting_fee_after_tax(as_of)`。
   **清空元组 = 整体回退历史口径**（以后哪期要恢复，把日期写进来即可）。
2. `_run_template` 节点循环里，`give_customer` 求值后对命中的车次做
   `node_values["give_customer"] -= 切割费 × 对外点位% / 100`（点位取该车实际生效值，含逐车 override）。

**为什么改节点结果而不是模板结构**：模板是数据驱动、对所有期次生效；这是"只这一期"的口径，
改节点结果能做到窗口外零影响，也不必为一次性口径跑模板迁移。

**未动**：`settlement_amount_total`（钢厂结算单原样，仍是含税 − 切割费）、票源点位/垫资/量补/提成各节点。

### 影响面（实测）

- **逐车全字段（128 台 × `compute_trip_finance_v2_flat` 全部字段逐字节比对）**：只有 **#131** 变化，
  5 个字段：`give_customer` −42、`personal_advance`/`advance_fee_cost` −42、`net_profit` +42、
  `advance_fee_rate` 随之；其余 127 台**逐字节相同**。
- **518 接口 A/B**：15 个差异，全部归因——
  - 10 个是 #131 那 42 元的传播（`/api/trips/131`、`/api/dashboard`、`/api/dashboard/mill_periods`、
    `/api/reports/summary`×2、`/api/analytics/trend`×2、`/api/flow_templates/1/formula` 的示例算例、
    `/api/system/verify` 与 `/api/alerts` 的利润核对提示），逐字段 diff 证明差异**恒为 ±42.00**；
  - 5 个是时间/内容压缩类（`/api/version`、`/report`、`/api/report/pdf`、`/api/export/ledger`、
    `/api/export/ops_report`）：`/api/version`、`/api/report/pdf` 同代码两次跑也差；`/report` 正文 diff
    只有内嵌 `const D` 一行（`period.profit` 与 `trips[131]`）；两个 xlsx 在快照里只记字节长度，
    而同代码两次生成字节**完全一致** → 大小差来自内容压缩而非时间戳。
- **经营报告 Excel 逐格比对（7 sheet）**：差异 27 格，全部 ±42（① 总览 14 格、④ 2026-10期 13 格）；
  ② 2026-08期、③ 2026-09期、调货人工资、量补汇总、票源额度水位 **0 格差异**。
- **算钱回归**：`legacy/compare_v2_flat_fields.py` 仍是 117 笔全一致 / 11 笔历史基线差异
  （#131 本就在这 11 笔内；本次让它多出一项 `give_customer` 差异——calc 旧引擎是历史对照，不随新口径走）。

### 文案同步

- `ops_report_export.py` 总览页「应付·供货方货款（给客户付）」备注加「2026-10 期切割费另在税后全额扣」。
- `templates/report_print.html` 口径说明的「配票」条、「切割费由客户承担」条同步标明 2026-10 期改口径。

### 不做什么

- 不改 `settlement_amount_total`（钢厂结算单口径不动，报表「含税结算额」含义不变）。
- 不为一次性口径动模板节点结构（不跑模板迁移）。
- 不自动「以当前为准」：本次利润变动会触发一条利润核对提示，由用户核对后自行确认。
- 不影响 8、9 月已封账期次与窗口外的任何车次。

## §47 2026-10 期反向票点成本 14.5%（10-01 起，一次性）（2026-10-11/12，v2.4.4）

### 需求与口径确认

用户指令：「**10月期，10.1 开始的反向路径按照 14.5 票点成本计算**」。
AskUserQuestion 两轮确认（因该改动会让 #135 净利翻负，必须先拍方向）：

1. **期限**：只 2026-10 期 → 分期生效区间 `2026-10-01 ~ 2026-10-25`，11 期起自动回退档案 13.8（同 §46 切割费的"一次性期次"思路）。
2. **对象**：两个反向自然人都配（反向-闫东朋 id 1、反向-尹鹏飞 id 5）——用户说明"目前这两个都是尹鹏飞找的反向自然人"。
3. **方向**：反向利润 = 票面额 ×（对外点位 14% − 收票票点）+ 吨减 → **收票票点越高越亏**。
   13.8 → 14.5 使 #135 由 +4,902.29 翻为 **−3,522.52**（倒挂 0.5 个点）。用户明确答复：
   **「确认，成本就是涨到 14.5」** → 该亏损是预期结果，不是符号 bug。

### 实现（零代码改动，纯配置）

§45 已把票点做成"按期可配"，本次只需往 `invoice_source_terms` 插两行：

| id | 票源 | 生效区间 | rate_point | 额度/预警 | accepting |
|---|---|---|---|---|---|
| 7 | 反向-尹鹏飞 (5) | 2026-10-01 ~ 10-25 | **14.5** | 350万 / 180万（沿用档案） | 1 |
| 8 | 反向-闫东朋 (1) | 2026-10-01 ~ 10-25 | **14.5** | 空（沿用停接单语义） | 0 |

- **额度/预警/接单状态必须复制当前生效值**，否则新行会把票源从"停接单/有额度"变成默认态，
  连带改动首页水位面板（`stats._source_term_row` 取的是同一张表的最新命中行）。
- 写入路径：运行中服务的通用档案接口 `POST /api/invoice_source_terms`（自动 `audit_log` 留痕，
  避开沙箱写库坑）。审计行 #918/#919。
- 取数：`engine._effective_invoice_source(as_of=settlement_date)` → #135 结算日 10-08 命中新行 →
  `rate_in 13.8 → 14.5`；窗口外无命中行 → 回退档案 13.8。**不动档案列**（历史口径保留）。
- 配套幂等重建脚本：`fix_20261011_reverse_point_145.py`（同 (source, effective_from) 已存在即跳过）。

### 影响量（唯一变化车次 #135 苏淮货15666）

| 字段 | 改前 | 改后 |
|---|---|---|
| rate_in（票点） | 13.8 | 14.5 |
| front_actual_paid | 1,037,454.54 | 1,029,029.74 |
| net_profit | +4,902.29 | **−3,522.52** |

恒等式（校验用）：**Δ = −票面额 × 0.7% = −8,424.80485**（票面额 1,203,543.55；反向垫资费 0 → 票面额=含税结算额）。

### 验证链

1. **逐车全字段 A/B（132 台，改前/改后）**：仅 #135 的 7 个字段变化
   （`rate_in / front_actual_paid / net_profit / personal_advance / advance_fee_cost / advance_fee_rate / rate_items`）。
2. **`legacy/compare_v2_flat_fields.py`**：120 笔全一致 / 12 笔差异 = 原 11 笔历史基线 + #135
   （calc 旧引擎是冻结对照，不随分期走，属预期归属）。
3. **518 接口 golden master A/B（逆补丁构造改前态：删 id 7/8 + 两条审计行）**：19 处差异全归因——
   - #135 传播 6 处：`/api/trips/135`、`/api/trips/135/flow_graph`、`/api/dashboard`、`/api/dashboard/mill_periods`、
     `/api/analytics/trend`、`/api/flow_templates/2/formula`（示例算例）；
   - `/api/reports/summary`：35 个金额格**全部恰好 ±8,424.80485**；21 个 `*_per_ton` 派生值逐条自洽
     （266 个 per_ton 单元格复算 0 异常，Δper_ton×吨位 = ±8,424.80）；
   - `/api/dashboard/quota_levels`：**数值零变化**，只有 `term_notes` 文案变（水位 109.6% 不变）；
   - 列表/留痕 3 处：`/api/invoice_source_terms`（6→8 行）、`/api/system/audit`、`/api/system/verify`；
   - 时间/压缩类 5 处：`/api/version`、`/report`、`/api/report/pdf`、`/api/export/ledger`、`/api/export/ops_report`。
4. **利润核对闭环**：改后 `/api/system/verify` 多一行 #135（固化快照 4,902.29 vs 实时 −3,522.52，差 −8,424.81），
   经用户确认方向后执行 `POST /api/system/verify/accept {"ids":[135]}` → `accepted:1`，自检归零、预警回到 3 条。
   （注意：accept 不传 `ids` 或 `all` 时 `accepted:0` 却仍会写一条审计，属接口现状。）

### 不做什么

- 不改 `invoice_sources.rate_point` 档案列（13.8 作为历史默认值保留，回退口径唯一）。
- 不把 14.5 延伸到 11 期（窗口边界即期界；要延续须新增分期行）。
- 不动模板节点结构、不动 calc 旧引擎。
- 不处理反向-尹鹏飞年度额度爆仓（109.6%，超 ¥336,694）——待用户定分流方案。

### 待落地的用户新约定（2026-10-11 口述，尚未实现）

> 「目前这两个都是尹鹏飞找的反向自然人，后续都按照**反向调货人[月-编号]**这样来显示吧，
> 超过的自动按照新的编号和自然人来增加。」

即：反向自然人以"调货人 + 月-编号"编号体系建档（额度超限即新增编号 + 新自然人分流），
现状仍是 `反向-姓名`（`反向-闫东朋`/`反向-尹鹏飞`）。等第二个身份证到手建新档时一并落地命名与自动编号规则。

## §48 反向路径与「−5元/吨」吨减解耦；反向-王荣胜建档；10.10/10.11 批次录入（2026-10-13，v2.4.5）

### 需求（用户 2026-10-13 明确口径）

1. 「**反向每吨不-5元，只有配票的（例如俊兴）非林凯才-5元**」→ 反向模板不得再吃
   `account_external_settlement_ton_adj`（该关系原为模板1/2 共用，非林凯 5 元/吨，2026-09-26 起）。
2. 两条反向船（中伟机8288、新海集货588，调货人王荣胜）**新建「反向-王荣胜」票源**，独立年度额度。
3. 带票 鲁DC6106（邓晶焱）走**正常带票费率**（不套 #136/#137 的「服务费0只赚量补」override）。
4. 双价行（同一车 50/50 两拼料）按**同一台车、两行各按自身吨位拆行**录入。

### A. 反向吨减解耦（唯一受影响车次 #135）

| 项 | 内容 |
|---|---|
| 关系目录 | `engine.py` RELATION_TYPE_CATALOG 新增 `account_external_settlement_ton_adj_reverse`（反向专用；默认不配置=0） |
| 模板节点 | `template_nodes` id 47（template_id=2, node_key=`give_customer_adj`）改挂新关系（audit #922） |
| 幂等脚本 | `fix_20261013_reverse_no_tonadj.py`（副本验证 0 新增） |
| 结果 | #135 给客户付 1,032,552.25 → **1,035,047.45**；净利 **−3,522.52 → −6,017.72**（Δ = −5×499.04 = **−2,495.20**） |

验证链：逐车全字段 A/B（132 台，**仅 #135** 的 `give_customer / personal_advance / net_profit / advance_fee_cost / advance_fee_rate / rate_items` 变化）；
`legacy/compare_v2_flat_fields.py` 120 一致 / 12 差异（原 11 条历史基线 + #135）；518 接口快照 A/B（逆补丁还原节点配置 + 删 audit #922）**18 处差异全归因**——
#135 传播 8 处、配置/留痕 2 处（`/flow_templates/2/nodes`、`/system/audit`）、报表 `/api/reports/summary` 35 个金额格**全部恰好 ±2,495.20** 且 21 个 `*_per_ton` 派生值逐条自洽（Δper_ton×分组吨位 = ±2,495.20，21/21）、时间/压缩类 5 处；`quota_levels` 数值零变化；
随后 `POST /api/system/verify/accept {"ids":[135]}` → 自检归零。

### B. 「反向-王荣胜」建档（invoice_source id 11）

- 档案：`source_type=reverse_individual`、`settlement_type=direct`、`quota_period=annual`、
  **350 万年度额度 / 180 万预警**、`rate_point=13.8`（回退值）、`bound_mill_id=1`、`bound_base_id=13`（王荣胜基地）。
- 10 期票点分期：`invoice_source_terms` id 9（2026-10-01~10-25，**14.5**，额度/预警/accepting 照抄当前生效值）。
- **必须补的隐藏档位**：`account_advance_fee` 增 invoice_source=11 的 **0 元/吨**行（id 25）——反向票源的垫资费已含在票点里，
  否则新票源会回退户头通用档 23 元/吨（本次先踩到：#140/#144 初见 23×吨位，补档后修正）。
- 水位：反向-王荣胜 74.18%（两条船 259.6 万 / 350 万）；反向-尹鹏飞仍 109.6% 不变。

### C. 10.10/10.11 批次 7 台车（#138–#144）

录入规程（**重要**）：`POST /api/trips` 的白名单 `TRIP_FIELDS` **不含** `net_weight_total / deduction_total /
settlement_tonnage_total / settlement_amount_total`（结算字段走状态推进），必须再用
`PUT /api/trips/<id>/details`（多行明细，品名落 `trip_scrap_details.scrap_type_id`）或
`POST /api/trips/batch_status` 的 `per_trip` 补结算数。反向船明细镜像 #135：不挂品名、`unit_price=0`、金额直给。

| # | 路径 | 日期 | 车牌 | 明细 | 吨位 | 结算额 | 净利 |
|---|---|---|---|---|---|---|---|
| 138 | 配票(林凯) | 10.10 | 苏DBB772 | 边角料压块1@2530 | 81.10 | 205,183.00 | 1,034.43 |
| 139 | 带票(邓晶焱) | 10.10 | 鲁DC6106 | 中废2@2350 23.995t + 小废@2280 23.995t | 47.99 | 111,096.85 | 1,535.68 |
| 140 | 反向(王荣胜) | 10.10 | 中伟机8288 | — | 502.58 | 1,234,774.20 | 3,877.73 |
| 141 | 配票(林凯) | 10.11 | 苏DBE075 | 边角料压块1@2530 | 81.68 | 206,650.40 | 1,041.83 |
| 142 | 配票(邓晶焱) | 10.11 | 苏BTQ938 | 废钢压块3@2280 | 32.08 | 73,142.40 | 529.48 |
| 143 | 配票(林凯) | 10.11 | 苏DBB772 | 废钢压块3@2280 37.13t + 废钢压块2@2320 37.13t | 74.26 | 170,798.00 | 861.79 |
| 144 | 反向(王荣胜) | 10.11 | 新海集货588 | — | 546.90 | 1,361,683.50 | 4,129.58 |

第 5 列（0.6/0.325/4.42…）经 #135 实证为**扣杂吨**（毛重−扣杂=净吨，如 504.58−5.54=499.04）；本批金额列全部 = 净吨×单价，逐笔勾稽通过。

### D. 量补跨档（本批录入的连锁效应，非 bug）

本批 1,366.59 吨入账后 10 期累计 = **3,204.12 吨 ≥ 3,000 吨档** → 引擎按**期累计**给**全期**车次记
**20 元/吨**量补（回溯生效，含此前已核对过的 #110–#135）。10 期净利合计：
**−10,964.60（改前 23 台）→ +36,301.32（现 30 台）**，差额可完全拆解：
新录 7 台 +13,010.52、量补回溯 +36,750.60、反向去吨减 −2,495.20。

利润核对：`check()` 以「费率指纹 sig」判定——口径/费率变化会**自动重设基线不报**，只有指纹未变而利润漂移才报；
本次 30 台指纹全变（税点/吨减/量补档），故 0 待核对，属正常。

### 待办 / 未落地

- **俊兴票点 13.5% 无需修改**（2026-10-13 用历史台账核实）：`8月期9月期_经营统计_20260930.xlsx` ③票点位差表
  两期均记「俊兴 13.5% direct、点差 0.5%」，\|系统口径明细\| 亦同；§45 里出现过的 11.0 只是当期验证用例。
  即配票本身点差薄（0.5%×结算额 − 19.895 元/吨垫资），单台小负、靠量补到档转正，**属既有口径不是缺陷**。
- 反向自然人命名约定「反向调货人[月-编号]，超额自动新编号+新自然人」仍未落代码（现状 `反向-王荣胜`）。

## §49 杭州禄国建档；4 台车票商更正；非俊兴垫资费 25（10-12 起）（2026-10-13，v2.4.6）

### 需求（用户 2026-10-13 口径）

1. 10.10/10.11 四台配票车（#138/#141/#142/#143）**票商写错**：不走俊兴，实为
   **杭州禄国环境科技有限公司**（新票商）——无金额额度、**累计 1000 吨上限**（手工监控）、
   **票点成本 13.2**、我方有票差利润。
2. **反向开票链路**（信息性约定）：所有反向票均由**舞阳县浩洋再生资源有限公司**开出 → 再走到
   反向个人，个人信息由浩洋自行提供 → 已追加到三个反向票源（id 1/5/11）的档案备注。
3. **凡是非俊兴的票，垫资费 2026-10-12 起一律 25 元/吨**（本期生效，下期默认承接 = 不设截止）。
4. 用户澄清：表格第 5 列扣杂对应的**金额已经是扣杂后的结算金额**（与既有录入口径一致，无需改数）。

### A. 杭州禄国建档（invoice_sources id 12）

`front_company / direct / rate_point=13.2 / quota_period=monthly / quota_amount 空`（镜像盐城宏峰的
「monthly 但无额度」形态）；备注写明全称、1000 吨累计上限与四台车归属。
⚠ 无额度票源在水位面板按「无额度」行显示（只示已用），**不占俊兴额度**。

### B. 垫资费档位重构（rate_links，全部走 upsert 留审计）

| id | 键 | 值 | 生效区间 | 说明 |
|---|---|---|---|---|
| 1 | 户头通用 | 23 | 2020-01-01 ~ **2026-10-11** | 被 upsert 自动截止 |
| **27** | 户头通用 | **25** | **2026-10-12 ~ 至今** | 非俊兴新价，下期默认承接 |
| **26** | 户头×俊兴(2) | 23 | 2020-01-01 ~ 至今 | **专项行防误伤**（10-12 后俊兴仍 23） |
| 7/8/25 | 户头×反向(1/5/11) | 0 | 2020-01-01 ~ 至今 | 反向专项，专项行优先于通用行，不受影响 |

取数链：`engine.get_effective_rate` 三级兜底（票源专项 → 户头通用 → 全局），**按结算日期命中生效区间**。

### C. 四台车更正（PUT /api/trips/<id>，审计 #946~#953）

| # | 票源 13.5→13.2 后前端实付 | 净利变化 |
|---|---|---|
| 138 | 175,869.81 → 176,479.76 | 1,034.43 → **1,644.38** |
| 141 | 177,127.57 → 177,741.89 | 1,041.83 → **1,656.14** |
| 142 | 62,629.94 → 62,847.16 | 529.48 → **746.69** |
| 143 | 146,262.87 → 146,770.14 | 861.79 → **1,369.06** |

恒等式：**Δ前端实付 = Δ净利 = 票面额 × 0.3%**（609.95 / 614.32 / 217.21 / 507.27，合计 **+1,948.75**）；
4 台结算日均在 10-12 前 → 垫资费仍按旧通用档 23（若需这 4 台也按 25，把 id 27 的 effective_from 提前即可）。
10 期净利合计 **36,301.32 → 38,250.07**（配票 19,742.70 / 反向 11,970.39 / 带票 6,536.98）。

### 验证链

1. **逐车全字段 A/B**：139 台中仅 4 台变化（7 字段：rate_in / net_profit / rate_items / advance_fee_cost /
   front_actual_paid / personal_advance / advance_fee_rate），其中 advance_fee_cost=给客户付−前端实付
   的差额口径同步翻转（禄国 13.2 点差 0.8% 已盖住 23 元/吨摊薄 → 点差转正）。
2. **compare_v2**：123 全一致 / 16 差异 = 11 历史基线 + #135 + 本批 4 台（预期归属）。
3. **518+ 接口 GM A/B**（改前库直接 cp 留存，无需逆补丁）：37 处差异全归因——
   配置类 5（`/api/invoice_sources`、`/api/rate_links`、`/api/rate_links/1`、`/api/rate_links/graph`、
   `/api/system/audit`）、4 台传播 24（trips 详情/列表/lanes/flow_graph/tracking/dashboard/mill_periods/
   trend×2/reports/summary×2/formula 示例/endpoint_options）、**reports/summary 金额格 64 格全部归因**
   （40 格 = 四台常数的子集和精确命中，24 格 per_ton 派生 Δ×组吨位=子集和自洽）、
   水位 1（俊兴已用 −649,584.04、禄国新行已用 649,584.04=开票额口径）、时间/压缩类 5、
   `by_invoice_source` 12→13 行（新票商，预期）。
4. **自检 0 待核对**：费率指纹变化 → `check()` 自动重设基线不报（§48 已确认的设计行为）。

### 监控与待办

- **禄国 1000 吨累计上限**为系统外口径：当前累计 **269.12 吨**（#138 81.10 + #141 81.68 + #142 32.08 + #143 74.26），
  无自动预警，录车时人工盯；若要做成系统内档位需扩展票源档案（暂不做）。
- 反向自然人「反向调货人[月-编号]」命名约定仍未落代码（等正式启用）。

## §50 财务对账闭环；禄国垫资费专项 25；advance_fee 节点补票源维度（2026-10-13，v2.4.7）

### 用户三项拍板（对财务送货明细表的 4 个差异点）

1. **禄国垫资费以 25 为准**（4 台 10.10/10.11 财务已按 25 扣）；
2. **我方反向票点成本 = 14.5% 且已含垫资费** → 付浩洋 14.5% 口径正确、无需改；
   财务右列个人付款（潘巧玲/王荣昌）按 14% 属浩洋付其下家的内部口径，浩洋赚 0.5% 通道费 ≈12,163.59；
3. **中伟机8288 结算吨位以系统 502.58 为准**（财务 498.40 系笔误）、**户名盐城宏峰正确**（财务「宏鑫」笔误）。

### A. 禄国专项垫资费 25（rate_links id 28）

- `POST /api/rate_links/upsert`：户头×禄国(12) **25 元/吨自 2026-10-10**（audit #955）。
- **通用档 10-12 不动** → #139 盐城宏峰（10.10，模板 3）仍按 23，不被误伤。
- 影响仅 4 台：Δ开票额 = −2×吨位（−162.20/−163.36/−64.16/−148.52），
  Δ净利 = −2×吨位×0.868（−140.79/−141.80/−55.69/−128.92，合计 **−467.19**）；
  改后 #138 1,503.59 / #141 1,514.35 / #142 691.00 / #143 1,240.14。

### B. ⚠ 顺带发现并修复的结构缺陷：模板 1/3/4/5 的 advance_fee 节点缺 secondary_ref

- **发现路径**：落 id 28 后首跑 A/B 竟 0 变化 → `advance_fee_unit_for_trip` 走模板节点配置，
  而模板 1/3/4/5 的 `advance_fee` 节点 `calc_rule_config` **没配** `secondary_ref: trip:invoice_source_id`
  （只有模板 2 反向配了）→ `get_effective_rate` 只查「不区分票源」一级，
  **票源专项行（俊兴 23 id26、禄国 25 id28、反向 0 id7/8/25）对模板 1/3/4/5 车次从未生效**；
  尤其 **10-12 后俊兴配票车会被通用档 25 误伤**（用户口径：非俊兴才是 25）。
  §49 加俊兴专项行时 A/B 恰好无变化（4 台禄国车票点变化掩盖），该缺陷当时未暴露。
- **修复**：`fix_20261013_advance_fee_secondary.py`（幂等，副本验证二跑 0 新增）给模板 1/3/4/5
  节点追加 `secondary_type/secondary_ref`（audit #956~#959），/tmp 副本规程换回运行库。
- **安全性**：`get_effective_rate` 三级兜底——票源无专项行回退通用档；M2 空票源车
  （secondary=None）只匹配 `secondary_entity_id IS NULL` 的通用行，行为不变。

### 验证链

1. **逐车全字段 A/B**：139 台仅 4 台禄国车变化（8 字段，恒等式逐条吻合）。
2. **compare_v2**：120 一致 / 19 差异 = 11 条历史基线（5 笔 other_cost + #115/116/118/125/127/131
   双引擎口径差）+ #135/#140/#144（calc 不读 14.5 分期与反向 0 垫资专项，同 §47 模式）
   + #139（M2 提成差，同 #125 模式）+ **本次 4 台**。
3. **551 接口 GM A/B**（逆补丁还原 4 节点配置+删 4 条审计）：36 处差异全归因——
   配置类 8（flow_templates 1/3/4/5 的 formula+nodes，formula 示例算例同步变 25）；
   4 台传播 12；聚合 11（`reports/summary` 45 个金额格全部为四台常数子集和精确命中、
   27 个 per_ton 派生格 Δ×组吨位自洽，**零未归因**）；时间/导出 5。
4. **利润核对闭环**：verify 新增 4 行（diff −140.79/−141.80/−55.69/−128.92）→
   `POST /api/system/verify/accept {"ids":[138,141,142,143]}` → accepted:4，自检归零、预警回 4 条。

### C. 财务对账结论（三路径逐车，交付 `财务对账_三条路径逐车对比_20261013.xlsx`）

- **配票（禄国 4 台）**：含税/给客户付逐车 Δ=0（563,805.06 vs 财务 563,805.07 舍入）；付款口径
  655,773.80−25×269.12=649,045.80 ✓；财务行 1 入库 81.11 系笔误（钱按 81.10）。
- **带票（宏峰 #139）**：付款 109,177.25=111,096.85−40×47.99 ✓；系统净利 1,535.68。
- **反向（浩洋 2 船）**：票面合计 2,438,317.75 vs 财务 2,438,318.47（0.72 舍入）；付浩洋 ×0.855 ✓；
  个人 14% 为浩洋内部口径；#144/#136/#137 不在此表。
- 10 期净利合计 38,250.07 → **37,782.87**（−467.19）。

### 交付物

- `财务对账_三条路径逐车对比_20261013.xlsx`（5 sheet：总览/配票/带票/反向/差异与落实）。
