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

## 电池召回追溯

厂家公告（含后续勘误版本）、入库电池标签扫描、双人复扫、厂家签署的更换证明和触达回执均为离线合成证据，各自保留原始来源、签发时间、版本与 event_id。追溯按实物电池批次而非整机型号判定：

```powershell
python -m consignproof recall-import --db state.sqlite --file fixtures/recall_bulletins.json
python -m consignproof recall-trace --db state.sqlite
python -m consignproof report --db state.sqlite
python -m consignproof audit --db state.sqlite
```

- 标签局部磨损时仅能匹配型号，不得直接判受影响：进入 `review`；双人复扫确认批次落在公告危险范围才召回（在店 `quarantined` 隔离停售，已签收向实际买家 `buyer_notice_pending`/`buyer_noticed` 触达），或由厂家签署的售前更换证明判 `excluded` 保留可售资格。单名扫码员的猜测与维修店报价不能替代实物批次证据。
- 迟到的厂家勘误按新版本重算当前受影响集合；旧版已发通知不可删除，只追加解除说明（`recall_notice_release`），已完成更换动作保留。更换证明晚于买家签收时间的，不能倒签消除此前风险，历史已送达通知保留。
- 电池按挂载/拆卸时序在整机间转移时，风险跟随实物批次；同一标签在两台相机上同时挂载会阻断两台机的判定（`blocked`）。无 buyer 或 serial 不匹配的 delivery 不生成“已触达”；通知不等于维修，`sale_decision` 的历史结算不被修改。
- 同一文件重放不重复通知或隔离；event_id 异内容冲突整体回滚；`audit` 从文件证据复算当时与当前的追溯集合。

权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。
