# -*- coding: utf-8 -*-
"""CSV 账套解析与校验。

CSV 格式（UTF-8，首行为表头，所有重量为非负整数公斤）：

record_type,furnace,ref,primary_kg,recycled_kg,weight_kg
furnace,F1,,,100,400
batch,F1,B1,,,300
transfer,F1,F2,,,50

- furnace：furnace=炉号，primary_kg=外购原生料，recycled_kg=外购再生料
  （原生料/再生料列留空按 0 处理）
- batch：  furnace=归属炉，ref=出货批号，weight_kg=该批实称重量
- transfer：furnace=源炉，ref=目标炉，weight_kg=余料转料实称重量

校验：字段完整、整数非负、炉号/批号唯一、引用存在，且每炉
总进料（原生+再生+转入）== 总出料（出货批+转出）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

REQUIRED_COLUMNS = [
    "record_type", "furnace", "ref",
    "primary_kg", "recycled_kg", "weight_kg",
]
VALID_TYPES = {"furnace", "batch", "transfer"}


class CsvError(ValueError):
    """CSV 结构或账套平衡校验失败。"""


@dataclass
class ParsedLedger:
    furnaces: List[dict] = field(default_factory=list)        # code, primary, recycled
    batches: List[dict] = field(default_factory=list)         # furnace, code, weight
    transfers: List[dict] = field(default_factory=list)       # src, dst, weight


def _parse_int(raw: str, field_name: str, line_no: int,
               allow_blank: bool = False, positive: bool = False) -> int:
    raw = (raw or "").strip()
    if raw == "":
        if allow_blank:
            return 0
        raise CsvError(f"第 {line_no} 行：字段 {field_name} 不能为空。")
    try:
        v = int(raw)
    except ValueError:
        raise CsvError(f"第 {line_no} 行：字段 {field_name} 不是整数（收到 {raw!r}）。")
    if v < 0:
        raise CsvError(f"第 {line_no} 行：字段 {field_name} 不能为负数（{v}）。")
    if positive and v <= 0:
        raise CsvError(f"第 {line_no} 行：字段 {field_name} 必须为正整数（{v}）。")
    return v


def parse_ledger_csv(content: bytes) -> ParsedLedger:
    if not content or not content.strip():
        raise CsvError("上传文件为空。")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CsvError("文件不是 UTF-8 编码，请另存为 UTF-8 CSV 后重试。")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        raise CsvError("CSV 只有表头或无数据行。")
    header = [h.strip() for h in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise CsvError("表头缺少必需列：" + "、".join(missing)
                       + f"。完整列：{', '.join(REQUIRED_COLUMNS)}")
    idx = {c: header.index(c) for c in REQUIRED_COLUMNS}

    parsed = ParsedLedger()
    seen_furnaces: Dict[str, int] = {}
    seen_batches: Dict[str, int] = {}

    for line_no, row in enumerate(rows[1:], start=2):
        if not row or all((c or "").strip() == "" for c in row):
            continue
        if len(row) < len(header):
            row = row + [""] * (len(header) - len(row))
        rtype = row[idx["record_type"]].strip().lower()
        if rtype not in VALID_TYPES:
            raise CsvError(
                f"第 {line_no} 行：record_type 必须是 furnace/batch/transfer，"
                f"收到 {rtype!r}。"
            )
        furnace = row[idx["furnace"]].strip()
        ref = row[idx["ref"]].strip()
        if not furnace:
            raise CsvError(f"第 {line_no} 行：furnace（炉号）不能为空。")

        if rtype == "furnace":
            primary = _parse_int(row[idx["primary_kg"]], "primary_kg", line_no,
                                 allow_blank=True)
            recycled = _parse_int(row[idx["recycled_kg"]], "recycled_kg", line_no,
                                  allow_blank=True)
            if furnace in seen_furnaces:
                raise CsvError(f"第 {line_no} 行：炉号 {furnace} 重复定义"
                               f"（首次见第 {seen_furnaces[furnace]} 行）。")
            seen_furnaces[furnace] = line_no
            parsed.furnaces.append({
                "code": furnace, "primary_kg": primary, "recycled_kg": recycled,
            })

        elif rtype == "batch":
            if not ref:
                raise CsvError(f"第 {line_no} 行：batch 记录的 ref（出货批号）不能为空。")
            weight = _parse_int(row[idx["weight_kg"]], "weight_kg", line_no)
            if ref in seen_batches:
                raise CsvError(f"第 {line_no} 行：出货批号 {ref} 重复定义"
                               f"（首次见第 {seen_batches[ref]} 行）。")
            seen_batches[ref] = line_no
            parsed.batches.append({
                "furnace": furnace, "code": ref, "weight_kg": weight,
            })

        else:  # transfer
            if not ref:
                raise CsvError(f"第 {line_no} 行：transfer 记录的 ref（目标炉号）不能为空。")
            if furnace == ref:
                raise CsvError(f"第 {line_no} 行：余料转料的源炉与目标炉不能相同（{furnace}）。")
            weight = _parse_int(row[idx["weight_kg"]], "weight_kg", line_no)
            parsed.transfers.append({
                "src": furnace, "dst": ref, "weight_kg": weight,
            })

    if not parsed.furnaces:
        raise CsvError("账套中没有任何 furnace 记录。")

    # ---------- 引用完整性 ----------
    fcodes = {f["code"] for f in parsed.furnaces}
    for b in parsed.batches:
        if b["furnace"] not in fcodes:
            raise CsvError(f"出货批 {b['code']} 的归属炉 {b['furnace']} 未在 furnace 行定义。")
    for t in parsed.transfers:
        if t["src"] not in fcodes:
            raise CsvError(f"转料 {t['src']}->{t['dst']} 的源炉 {t['src']} 未定义。")
        if t["dst"] not in fcodes:
            raise CsvError(f"转料 {t['src']}->{t['dst']} 的目标炉 {t['dst']} 未定义。")

    # ---------- 每炉总进料 == 总出料 ----------
    balance: Dict[str, Dict[str, int]] = {
        c: {"in": 0, "out": 0} for c in fcodes
    }
    for f in parsed.furnaces:
        balance[f["code"]]["in"] += f["primary_kg"] + f["recycled_kg"]
    for t in parsed.transfers:
        balance[t["src"]]["out"] += t["weight_kg"]
        balance[t["dst"]]["in"] += t["weight_kg"]
    for b in parsed.batches:
        balance[b["furnace"]]["out"] += b["weight_kg"]

    problems = []
    for code in sorted(fcodes):
        tin, tout = balance[code]["in"], balance[code]["out"]
        if tin != tout:
            problems.append(f"炉 {code}：总进料 {tin} kg ≠ 总出料 {tout} kg（差 {tin - tout:+d} kg）")
    if problems:
        raise CsvError("炉次物料平衡不满足：" + "；".join(problems) + "。")

    return parsed


def ledger_summary(parsed: ParsedLedger) -> Tuple[int, int, int]:
    return len(parsed.furnaces), len(parsed.batches), len(parsed.transfers)
