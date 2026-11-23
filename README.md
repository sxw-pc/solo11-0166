# 二手寄售交割采证台

离线采录委托货品的权属与期限、成色与挂牌价格、付款及实物交割。三账分别裁定挂牌授权、报价许可和委托款/佣金分配；撤销委托使未来挂牌失效，但保留撤销前已完成的交割。全部数据为合成 JSON，不访问真实交易平台或付款机构。

盘点台账（stocktake）采录离线扫码枪会话（货架区域、原始条码/serial、设备时钟校准、区域封签、双人复扫）、移库单与承运交接回执，为每个应盘实物裁定最后可信位置、当前保管责任人、能否承诺店内自取以及待补证据。保管裁决只读取委托、挂牌、交割三账，不改写它们：已有效交付给买家的 serial 改归 `delivered` 而非失踪库存；货主撤销后店内仍可追溯实物，但停止自取承诺。

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
python -m consignproof stocktake-import --db state.sqlite --file fixtures/stocktake_sessions.json
python -m consignproof custody-decide --db state.sqlite [--item lens-2]
python -m consignproof report --db state.sqlite
```

盘点缺失（如仍在挂牌但未出现在货架扫描中的 lens-2）有且仅有两条合规恢复路径：

1. **漏扫路径**——第二台设备的独立复扫、匹配的区域封签号与时钟校准同时成立，证明首次漏扫，实物仍在后仓，责任在店内，经出库复核后可向柜台买家承诺自取（`in_store` / `store_pickup=true`）。
2. **承运路径**——承运人与店员双方签收清单、匹配 serial 的封签号与装车扫描同时成立，实物在途，责任转给承运人，店内禁止自取（`in_transit` / `store_pickup=false`）；之后的店内误扫只形成争议记录，不覆盖已确认的交接。

两条路径从同一缺失输入出发，结论却不同。单台设备的重复扫码或单方出库单不能替代它们，缺失物保持 `pending_review` 并列出待补证据。跨设备乱序、重复条码、时钟漂移按校准后的会话闭合时刻与交接边界裁定，不简单取最大时间戳。整批文件的事件 ID 冲突整批回滚；重复导入、不同文件顺序与 audit 重放得到同一当前保管决定与历史（custody 裁决由证据表确定性重建）。原始扫码事件进入留证链，可回溯来源设备、会话与文件 SHA。

对应三个 `*_bad.json` 分别触发委托撤销、功能故障和退货索赔。权属证明需序列号及持有人双匹配；价格不得低于签署保留价；银行付款和正确序列号交割才能分款；退货索赔冻结尾款与佣金。事件 ID 异内容重放拒绝覆盖，原始来源文件 SHA 与历史裁定保留，`audit` 从空库重放文件核对报告。

权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。
