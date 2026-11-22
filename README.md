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

`audit` 还会逐来源事件核验 `evidence.body` 的规范化摘要、`kind`、`event_id` 与 `file_journal` 原记录，并检查原始文件自身字节摘要是否仍与导入时一致；仅比较报告行数或重算当前决定不足以防住本地改写的合同行。

```powershell
# 复现：demo 导入后把 contract-1 的 evidence.body 改成 {}，旧式审计仍 matched
python -m consignproof demo --db out.sqlite
python -c "import sqlite3;c=sqlite3.connect('out.sqlite');c.execute(\"UPDATE evidence SET body='{}' WHERE event_id='contract-1'\");c.commit()"
python -m consignproof audit --db out.sqlite   # status=compromised，列出 fault/diff/依赖决定

# 选择一：原件字节完好且两名独立保管员确认，从原件重建
python -m consignproof audit --db out.sqlite --mode rebuild --event contract-1 --confirm keeper-a --confirm keeper-b
# 选择二：保留现场不覆盖被改写行，隔离为来源待核
python -m consignproof audit --db out.sqlite --mode isolate --event contract-1 --confirm keeper-a --confirm keeper-b
```

`file_journal.raw` 自身被改写或只剩异路径复制件时，重建请求返回 `refused`，只能隔离受污染子图并等待第二份独立原件；恢复动作写入不可改写的 `audit_action`（含前后摘要），中断会回滚到审计仍为 `compromised` 的状态。已由银行付款和 serial 交接完成的 `sale_decision` 作为历史保留。


权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。
