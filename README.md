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
python -m consignproof offer-import --db state.sqlite --file fixtures/offer_cases.json
python -m consignproof offer-decide --db state.sqlite
python -m consignproof report --db state.sqlite
```

对应三个 `*_bad.json` 分别触发委托撤销、功能故障和退货索赔。权属证明需序列号及持有人双匹配；价格不得低于签署保留价；银行付款和正确序列号交割才能分款；退货索赔冻结尾款与佣金。事件 ID 异内容重放拒绝覆盖，原始来源文件 SHA 与历史裁定保留，`audit` 从空库重放文件核对报告。

让价流程（`offer-import` / `offer-decide`）把已接受报价绑定到 `order_id` 与具体序列号，采录买家报价有效期、货主签署的改价（reprice）/保价（protect）授权、店铺补差承诺及独立银行补差流水。同一笔买家银行付款允许两条路线：改价路线须在付款前取得货主对新公开价的签署确认且不低于保留价，按实际成交价计佣，店铺无补差义务；保价路线保持原公开价与货主结算基准，店铺须在付款前从独立资金账户真实补入差额，名义佣金扣除补差后为店铺净得。仅有承诺而无补差银行流水时保价路线停在 pending；店员口头同意（provider 非 bank）不得记为买家付款；同一补差流水号不得支持两个订单。撤销委托或检测复核下架只阻止尚未完成序列号交割的订单，授权期内已交割订单不被改写；部分退款按 order_id 与付款人对齐，仅冻结对应买家来源，补差来源保留。

权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。
