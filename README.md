# 二手寄售交割采证台

离线采录委托货品的权属与期限、成色与挂牌价格、付款及实物交割。三账分别裁定挂牌授权、报价许可和委托款/佣金分配；撤销委托使未来挂牌失效，但保留撤销前已完成的交割。全部数据为合成 JSON，不访问真实交易平台或付款机构。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m consignproof demo --db demo.sqlite
python -m consignproof audit --db demo.sqlite
python -m unittest discover -s tests -v
```

独立入口：

```powershell
python -m consignproof consign-import --db state.sqlite --file fixtures/consign_ok.json
python -m consignproof grade-import --db state.sqlite --file fixtures/grade_ok.json
python -m consignproof sale-import --db state.sqlite --file fixtures/sale_ok.json
python -m consignproof report --db state.sqlite
```

对应三个 `*_bad.json` 分别触发委托撤销、功能故障和退货索赔。权属证明需序列号及持有人双匹配；价格不得低于签署保留价；银行付款和正确序列号交割才能分款；退货索赔冻结尾款与佣金。事件 ID 异内容重放拒绝覆盖，原始来源文件 SHA 与历史裁定保留，`audit` 从空库重放文件核对报告。

权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。

## 对外研究版（research-export / research-check）

`fixtures/research_cohorts.json` 是含两个研究期（p1/p2）、11 笔合成寄售交易、逐货主/买家签署的用途许可与撤回的本地 fixture，分 `v1`、`v2` 两个阶段。公开包只收纳有效委托授权下、银行付款且 serial 匹配交付、无 `return_claim` 的 settled 交易；内部 item、owner、buyer、serial 仅以每条 lineage 独立密钥的 HMAC 化名进入内部映射表，`evidence.body` 与 `file_journal.raw` 绝不写入公开包。

两条合法发布路径对同一内部交易输入给出不同可联结性：

- `period-stats`：只有每期分组计数、价位带与成交耗时，公开包不含任何跨期个人关联键；
- `cross-period-link`：额外发布按同一人稳定的版本化队列化名（per-lineage HMAC，非全局身份哈希），单元 id 仍按版本更换；小群体的日期与价位强制合并，稳定分组不能成为重新识别的钥匙。

v1 发布后的许可撤回不召回外部已持有的 v1；v2 排除未授权记录、发布撤回清单，并对 v1→v2 差分会暴露单人的单元做跨版本合并。迟到退货主张把 settled 改为 disputed 时，下一版发布更正说明且历史 delivery 保留不删。每个冻结版本带 SHA-256 摘要，同版重放摘要不变，污染或缺许可的版本拒绝发布。

```powershell
# 阶段一：仅导入 v1 事件后冻结两条路径的 v1
python -m consignproof consign-import --db research.sqlite --file stage1-consign.json
python -m consignproof grade-import  --db research.sqlite --file stage1-grade.json
python -m consignproof sale-import   --db research.sqlite --file stage1-sale.json
python -m consignproof research-import --db research.sqlite --file stage1-research.json
python -m consignproof research-export --db research.sqlite --lineage cohort-link --mode cross-period-link --periods p1 --as-of 2026-04-15 --out link-v1.json
python -m consignproof research-export --db research.sqlite --lineage period-only --mode period-stats    --periods p1 --as-of 2026-04-15 --out stats-v1.json
# 阶段二：导入含撤回与迟到退货的全部事件后发布 v2（同 lineage 续版）
python -m consignproof consign-import --db research.sqlite --file full-consign.json
python -m consignproof grade-import  --db research.sqlite --file full-grade.json
python -m consignproof sale-import   --db research.sqlite --file full-sale.json
python -m consignproof research-import --db research.sqlite --file full-research.json
python -m consignproof research-export --db research.sqlite --lineage cohort-link --mode cross-period-link --periods p1,p2 --as-of 2026-07-15 --out link-v2.json
python -m consignproof research-export --db research.sqlite --lineage period-only --mode period-stats    --periods p1,p2 --as-of 2026-07-15 --out stats-v2.json
# 校验全部冻结版本：摘要、禁用字段、抑制原因披露、聚合口径与无关联键
python -m consignproof research-check --db research.sqlite
python -m consignproof audit --db research.sqlite
```

`research-check` 对每个被抑制单元核对其原因已在公开包 `suppressions` 中披露，并扫描公开包是否混入内部姓名、serial 或原始证据内容；发现摘要被篡改或键白名单违规则返回 `failed`。

