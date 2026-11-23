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

## 外部刊登对账（完全离线）

`page-crawl` 只读取 `fixtures/mock_pages/` 下的分页 HTML/JSON mock，按游标遍历，不访问真实网站；原始页面字节、提取字段、来源 URL、抓取批次与时间全部进入可重放证据。`publication-reconcile` 读取签署的转发许可与货主渠道撤回举报，把外部 `listing_id`、店铺账号、serial 与照片来源许可关联到本地委托/检验证据，输出每个外部页面版本的归因与撤稿动作。本地 `title_decision`、`listing_decision`、`sale_decision` 不会被改写来冒充平台结果。

```powershell
python -m consignproof page-crawl --db state.sqlite --root fixtures/mock_pages
python -m consignproof publication-reconcile --db state.sqlite --file fixtures/mock_pages/submissions_1.json
python -m consignproof page-crawl --db state.sqlite --root fixtures/mock_pages --manifest crawl_manifest_r1.json
python -m consignproof page-crawl --db state.sqlite --root fixtures/mock_pages --manifest crawl_manifest_receipts.json
python -m consignproof audit --db state.sqlite
```

归因规则：同一照片摘要不等于授权——合作店持有覆盖 item、账号、渠道、照片与发布时点且原委托仍允许传播的签署转发许可时记为「授权镜像」并保留页面（记录许可版本）；货主提交渠道撤回且平台确认接收的举报单形成「需下架」事件，对该外部 `listing_id` 只发一次撤稿请求。`received` 回执仅受理，只有 `listing_id` 与请求版本双匹配的 `applied` 回执才判已下架；迟到的旧版 `applied` 不会关闭重新发布的新版页面。原委托在页面发布后撤销时，只对撤销后仍在传播的当前镜像生成新撤稿动作；成色复核更换 `photo_sha` 后旧镜像不能继承新照片许可；同一 `listing_id` 跨页面版本指向不同 serial 时整批隔离并记录差异冲突，不产生任何动作。相同批次重放幂等，`audit` 从页面原始字节与回执重建每个外部 `listing_id` 的当前状态。

权属签署、检测照片与银行回执均为离线标签，不连接真实身份、鉴定或支付网络。
