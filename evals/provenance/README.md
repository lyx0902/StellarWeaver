# 评分规则档案

`catalog-14a8d31d6ef372cb27e12943f0f00e8bd2cc90edead76a9c0e60d56e751ce6ef.json` 是根据当前清单去除后加的 `required_sources` 字段，并恢复原 JSON 缩进、UTF-8 和 CRLF 后重建的旧清单。其 SHA-256 与本地归档、未改写的 `docs/validation/m5_live_report.json` 中记录完全一致；这是字节级校验，不以语义相似替代。

原 M5 评分 Python 当时没有快照。新增收据使用当前评分器加此旧 catalog 复现原规则，明确保存当前实现来源；这不等于找回旧版 Python。当前修正规则仍以 Python 包内 `stellarweaver/evals/catalog.json` 为准。原报告和旧重评分回执保持不变。

后续运行和 `score --output` 均保存规则与评分源码 hash、版本及按内容命名的源码快照。快照可被人工修改，哈希核验会发现不同字节；它不是访问控制或签名。

原始报告、工作区与重评分收据仅作本地归档；GitHub 中的[公开验证摘要](../../docs/validation/README.md)不能代替这些运行数据。此目录公开规则字节，供新运行选择可信历史规则。
