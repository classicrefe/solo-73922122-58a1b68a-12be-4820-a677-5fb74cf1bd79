# -*- coding: utf-8 -*-
"""求解器单元测试：覆盖可行性、下界满足、转料通道、无解原因与整数性。"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.solver import (  # noqa: E402
    CertRequirement, FurnaceBatchLink, FurnaceInput, SolveError, TransferInput,
    solve_allocation,
)


def _basic_furnaces():
    return [
        FurnaceInput("F1", 100, 400),
        FurnaceInput("F2", 200, 100),
        FurnaceInput("F3", 50, 150),
    ]


def _basic_links():
    return [
        FurnaceBatchLink("F1", "B1", 300),
        FurnaceBatchLink("F1", "B2", 150),
        FurnaceBatchLink("F2", "B3", 250),
        FurnaceBatchLink("F3", "B4", 180),
        FurnaceBatchLink("F3", "B5", 120),
    ]


def _basic_transfers():
    return [
        TransferInput(1, "F1", "F2", 50),
        TransferInput(2, "F2", "F3", 100),
    ]


def _verify(result, furnaces, links, transfers, reqs):
    assert result.ok, result.reason
    req_map = {r.batch: r.min_recycled_kg for r in reqs}
    # 1) 批：签单下界 <= 核定 <= 实称，且为整数
    for bl in links:
        v = result.batch_recycled[bl.batch]
        assert isinstance(v, int)
        assert 0 <= v <= bl.weight_kg
        if bl.batch in req_map:
            assert v >= req_map[bl.batch]
    # 2) 转料：0 <= 核定 <= 实称
    for t in transfers:
        v = result.transfer_recycled[t.tid]
        assert isinstance(v, int)
        assert 0 <= v <= t.weight_kg
    # 3) 每炉再生料进出相等
    bal = result.furnace_balance
    for f in furnaces:
        row = bal[f.code]
        assert row["in_total"] == row["out_total"], (f.code, row)
        assert row["in_total"] <= f.recycled_kg + sum(
            t.weight_kg for t in transfers if t.dst == f.code
        ) + 1  # 容差仅防御性，实际应严格
    # 4) 炉->批核定之和与平衡表一致
    for f in furnaces:
        s = sum(result.batch_recycled[bl.batch] for bl in links if bl.furnace == f.code)
        assert s == bal[f.code]["to_batches"]


def test_feasible_no_certs():
    res = solve_allocation(_basic_furnaces(), _basic_links(),
                           _basic_transfers(), [])
    _verify(res, _basic_furnaces(), _basic_links(), _basic_transfers(), [])
    # 最大化后总出货再生料 = 总供应 650（无瓶颈时全部可送出）
    assert sum(res.batch_recycled.values()) == 650


def test_cert_lower_bounds_met():
    # F2 自身再生料 100 + F1->F2 通道 50 = 最多 150，故 B3 下限取 150
    reqs = [
        CertRequirement("B1", 280),
        CertRequirement("B3", 150),
        CertRequirement("B5", 100),
    ]
    res = solve_allocation(_basic_furnaces(), _basic_links(),
                           _basic_transfers(), reqs)
    _verify(res, _basic_furnaces(), _basic_links(), _basic_transfers(), reqs)
    assert res.batch_recycled["B1"] >= 280
    assert res.batch_recycled["B3"] >= 150
    assert res.batch_recycled["B5"] >= 100


def test_infeasible_total_shortage():
    # 需求 700 > 总再生料 650
    reqs = [CertRequirement("B1", 300), CertRequirement("B3", 250),
            CertRequirement("B4", 150)]
    res = solve_allocation(_basic_furnaces(), _basic_links(),
                           _basic_transfers(), reqs)
    assert not res.ok
    assert "总量不足" in res.reason or "瓶颈" in res.reason


def test_infeasible_transfer_bottleneck():
    # F2 自身再生料 100，批 B3 实称 250；唯一外援是 F1->F2 仅 50。
    # 要求 B3 >= 200：F2 最多拿到 100+50=150，转料通道 F2->F3 不相关。
    reqs = [CertRequirement("B3", 200)]
    res = solve_allocation(_basic_furnaces(), _basic_links(),
                           _basic_transfers(), reqs)
    assert not res.ok
    assert res.reason  # 有中文原因
    print("瓶颈原因输出：", res.reason)


def test_cert_above_batch_weight_single_batch():
    reqs = [CertRequirement("B1", 301)]
    res = solve_allocation(_basic_furnaces(), _basic_links(),
                           _basic_transfers(), reqs)
    assert not res.ok
    assert "B1" in res.reason


def test_unknown_batch_cert_raises():
    try:
        solve_allocation(_basic_furnaces(), _basic_links(),
                         _basic_transfers(), [CertRequirement("BX", 10)])
    except SolveError:
        return
    raise AssertionError("应当抛出 SolveError")


def test_self_transfer_rejected():
    try:
        solve_allocation(
            _basic_furnaces(), _basic_links(),
            [TransferInput(9, "F1", "F1", 10)], [],
        )
    except SolveError:
        return
    raise AssertionError("自转炉应被拒绝")


def test_chain_of_furnaces_routes_flow():
    """再生料只能沿转料链流动：F1 的余料经 F2 才能到 F3。"""
    furnaces = [
        FurnaceInput("F1", 0, 100),
        FurnaceInput("F2", 0, 0),
        FurnaceInput("F3", 0, 0),
    ]
    links = [
        FurnaceBatchLink("F1", "B1", 20),
        FurnaceBatchLink("F2", "B2", 100),
        FurnaceBatchLink("F3", "B3", 80),
    ]
    transfers = [
        TransferInput(1, "F1", "F2", 80),
        TransferInput(2, "F2", "F3", 80),
    ]
    reqs = [CertRequirement("B3", 60)]
    res = solve_allocation(furnaces, links, transfers, reqs)
    assert res.ok, res.reason
    assert res.batch_recycled["B3"] >= 60
    # F3 没有任何外购再生料，其批再生料全部来自转料
    assert res.transfer_recycled[2] >= 60
    _verify(res, furnaces, links, transfers, reqs)


def test_chain_broken_by_capacity():
    """F2->F3 通道只有 30，却要求 F3 的批承载 60。"""
    furnaces = [
        FurnaceInput("F1", 0, 100),
        FurnaceInput("F2", 0, 0),
        FurnaceInput("F3", 0, 0),
    ]
    links = [
        FurnaceBatchLink("F1", "B1", 100),
        FurnaceBatchLink("F3", "B3", 100),
    ]
    transfers = [
        TransferInput(1, "F1", "F2", 100),
        TransferInput(2, "F2", "F3", 30),
    ]
    reqs = [CertRequirement("B3", 60)]
    res = solve_allocation(furnaces, links, transfers, reqs)
    assert not res.ok
    assert "F2->F3" in res.reason or "转料" in res.reason


def test_integer_values_under_large_numbers():
    furnaces = [FurnaceInput("F1", 0, 123456)]
    links = [FurnaceBatchLink("F1", "B1", 100000),
             FurnaceBatchLink("F1", "B2", 23456)]
    res = solve_allocation(furnaces, links, [],
                           [CertRequirement("B1", 99999)])
    assert res.ok
    assert all(isinstance(v, int) for v in res.batch_recycled.values())
    assert res.batch_recycled["B1"] >= 99999


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
