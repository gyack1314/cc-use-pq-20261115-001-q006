秋窗客流分析账本
运行环境：Python 3.11，已按 requirements.txt 预装依赖。
启动：python -m autumn_ledger serve --database autumn-ledger.sqlite3
接口说明：服务启动后访问 /docs。
合成业务演练：python -B -m autumn_ledger demo
测试：python -B -m unittest discover -s tests -v
构建：python -B -m autumn_ledger.check_source
数据完整性：python -B -m autumn_ledger check --database autumn-ledger.sqlite3
金额单位为人民币分；订单修订为全量快照，退款为累计值。
出游日期是供应商给出的目的地本地日期；事件时间必须含时区。
取消不计出游人数，未退收入仍计入净额。缺报不等于零需求。
本地演练使用虚构数据，分析仅描述所选来源样本。
跨渠道同一销售只凭提交的关联证据建组（/reconciliation/evidence），不按金额或日期相近自动合并。
一个销售组每个来源最多一笔订单；重复提交同一证据不会重复建组，冲突证据留待处理（settle）。
金额、人数或取消状态不一致时由核对人员采信某一来源的一份完整修订（adopt），不拼接字段。
被采信的修订之后发生变化，销售组自动回到待核对；撤销采信（revoke）或拆开（dissolve）后重算即恢复。
分析报告同时给出来源口径原账与去重销售口径及差异解释；未解决的核对关系只能预览，不能发布。
