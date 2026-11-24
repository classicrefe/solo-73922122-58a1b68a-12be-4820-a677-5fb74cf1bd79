# -*- coding: utf-8 -*-
"""CSV 解析与物料平衡校验测试。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.csvutil import CsvError, parse_ledger_csv  # noqa: E402

HEADER = "record_type,furnace,ref,primary_kg,recycled_kg,weight_kg\n"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def expect_error(name, text, needle):
    try:
        parse_ledger_csv(text.encode("utf-8"))
    except CsvError as exc:
        check(name, needle in str(exc))
        return
    check(name, False)


# 1. 正常解析（含留空按 0）。平衡：F1 进 400=350 批+50 转出；F2 进 50+50+50=150=B2
ok = HEADER + (
    "furnace,F1,,0,400,\n"
    "furnace,F2,,50,50,\n"
    "batch,F1,B1,,,350\n"
    "batch,F2,B2,,,150\n"
    "transfer,F1,F2,,,50\n"
)
p = parse_ledger_csv(ok.encode("utf-8"))
check("parse_counts", len(p.furnaces) == 2 and len(p.batches) == 2 and len(p.transfers) == 1)
check("parse_values", p.furnaces[0]["recycled_kg"] == 400 and p.batches[0]["weight_kg"] == 350)

# 2. 空文件
expect_error("empty_file", "", "为空")

# 3. 缺列
expect_error("missing_column",
             "record_type,furnace,ref\nfurnace,F1,,\n", "缺少必需列")

# 4. 非整数
expect_error("non_integer", HEADER + "furnace,F1,,0,abc,\n", "不是整数")

# 5. 负数
expect_error("negative", HEADER + "furnace,F1,,0,-5,\n", "不能为负")

# 6. 未知 record_type
expect_error("bad_type", HEADER + "widget,F1,,0,1,\n", "record_type")

# 7. 炉号重复
expect_error("dup_furnace",
             HEADER + "furnace,F1,,0,1,\nfurnace,F1,,0,2,\n", "重复")

# 8. 批号重复
expect_error("dup_batch",
             HEADER + "furnace,F1,,0,10,\nbatch,F1,B1,,,5\nbatch,F1,B1,,,5\n",
             "出货批号 B1 重复")

# 9. 自转炉
expect_error("self_transfer",
             HEADER + "furnace,F1,,0,10,\ntransfer,F1,F1,,,5\n", "源炉与目标炉不能相同")

# 10. 引用不存在的炉
expect_error("unknown_furnace_ref",
             HEADER + "furnace,F1,,0,10,\nbatch,F9,B1,,,10\n", "未在 furnace 行定义")

# 11. 物料不平衡
expect_error("unbalanced",
             HEADER + "furnace,F1,,0,100,\nbatch,F1,B1,,,90\n", "物料平衡")

# 12. BOM 头与 CRLF 均可解析
p2 = parse_ledger_csv(("﻿" + ok).replace("\n", "\r\n").encode("utf-8"))
check("bom_and_crlf", len(p2.furnaces) == 2)

# 13. 平衡账套通过（含转料双向找平）
balanced = HEADER + (
    "furnace,F1,,0,400,\n"
    "furnace,F2,,50,50,\n"
    "batch,F1,B1,,,350\n"
    "batch,F2,B2,,,150\n"
    "transfer,F1,F2,,,50\n"
)
p3 = parse_ledger_csv(balanced.encode("utf-8"))
check("balanced_accepted", len(p3.transfers) == 1)

failed = [n for n, ok_ in results if not ok_]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
