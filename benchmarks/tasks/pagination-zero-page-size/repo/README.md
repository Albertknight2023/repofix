# Pagination Sample Repository

这是 RepoFix 的第一个缺陷样本。

## 问题描述

`paginate` 在 `page_size=0` 时会抛出 `ZeroDivisionError`。接口要求 `page_size <= 0` 时抛出消息明确的 `ValueError`，并保持其他分页行为不变。

## 运行公开测试

从本目录运行：

```powershell
$env:PYTHONPATH = "src"
python -m pytest tests -q
```

初始缺陷版本应有测试失败。
