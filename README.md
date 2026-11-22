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

## 跨扫描来源的合同身份

线下扫描服务可以各自把第一份合同编号为 `contract-1`。合同事件可带以下来源字段：

```json
{"source":"scanner-a","source_event_id":"contract-1","paper_sha":"paper-real",
 "item":"camera-1","version":2,"owner":"seller-a","serial":"serial-a",
 "valid_from":"2026-11-01","valid_to":"2026-11-20",
 "reserve_price":8000,"commission_pct":10,"signed_by":"seller-a"}
```

来源内身份写作 `src:["scanner-a","contract-1"]`，与全库 `event_id`、规范化合同 ID 分开保存。旧文件不带 `source` 时仍按 `event_id` 全库唯一，单来源同号异内容照旧整批回滚。

同一来源内号码出现在不同来源时，先保持 `pending`，不批准、不挂牌。后到的对照单放在同一 `consign-import` 文件的 `reconciliations` 中：

```json
{"reconciliations":[{"rec_id":"recon-merge","item":"camera-1","decision":"merge",
 "refs":[{"source":"scanner-a","source_event_id":"contract-1"},
         {"source":"scanner-b","source_event_id":"contract-1"}],
 "adopted_ref":{"source":"scanner-b","source_event_id":"contract-1"},
 "adopted_version":2,"paper_sha":"paper-real",
 "signed_by":["scanner-a","scanner-b","seller-a"],"signed_at":"2026-11-04"}]}
```

- `merge`：纸张指纹必须一致，且必须指明采用哪一来源的哪一字段版本；保留价为 9000 的更正提取被核定后，有效报价与委托期可恢复 `title approved` / `listing listed`。
- `separate`：两份原件必须有不同纸张指纹；同版本的两份合同仍触发并行条款冲突，`title frozen`、`listing blocked`。
- 只凭裸 `event_id` 或相同照片摘要不能归并；对照单须由两个扫描来源与货主共同签认。
- 撤销对照单：同一 `rec_id` 再导入 `{"status":"revoked","revoked_at":...}`，已归并来源重新展开并重裁；撤销前在有效授权期内已完成的银行付款与 serial 交割保持 `settled`，未交付交易按新挂牌资格处理。
- 一个来源事件号被复用到不同纸张、对照单引用不存在的来源事件、两份对照单互相矛盾时，相关来源标为 `quarantined`，不影响无关 item。
- `report` 的 `contract_identities` 列出每个来源身份、纸张指纹、`identity_state`（`active`/`pending`/`merged`/`separate`/`quarantined`）、原 `event_id` 与 `identity_reason`（归并/排除原因）；`contract_clusters` 给出规范化合同 ID 与成员，`reconciliations` 给出对照单的引用、采用版本与裁决。
- `audit` 从 `file_journal` 重放同一批文件，要求当前来源簇、对照裁决与业务裁定完全一致；导入顺序不同不影响结果。

