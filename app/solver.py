# -*- coding: utf-8 -*-
"""再生料分配求解器。

业务约束（全部重量均为非负整数公斤）：
  1. 每炉再生料进出相等：
       外购再生料 + 转入余料再生料 = 各出货批再生料 + 转出余料再生料
  2. 每笔出货的再生料重量以该批实称重量为上限；
     有效签单要求该批再生料重量 >= 签发的最低再生料重量（下界）。
  3. 每笔转料的再生料重量以转料实称重量为上限。

模型：带下界的整数网络流。
  S -> 炉              容量=该炉外购再生料
  炉 -> 出货批         容量=批实称重量，下界=该批有效签单下限
  炉 -> 炉(转料)       容量=转料实称重量
  出货批 -> T          容量=批实称重量
再叠加一条 T -> S 的无穷容量边，转成无源汇带下界可行流，用超级源/超级汇
+ Dinic 最大流判定可行性；可行后拆除辅助边再跑一次 S->T 最大流，使各出货批
承载的再生料重量尽量大，得到一套完整的核定重量。
容量与流量均为整数，Dinic 保证得到整数解。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence


class SolveError(Exception):
    """无解或输入数据本身不合法。"""


@dataclass
class FurnaceInput:
    code: str
    primary_kg: int
    recycled_kg: int


@dataclass
class TransferInput:
    tid: int                 # 数据库行号，用于回传每条转料的核定值
    src: str
    dst: str
    weight_kg: int


@dataclass
class CertRequirement:
    batch: str
    min_recycled_kg: int


@dataclass
class SolveResult:
    ok: bool
    reason: Optional[str] = None
    # 出货批 -> 核定再生料重量（整数公斤）
    batch_recycled: Dict[str, int] = field(default_factory=dict)
    # 转料行号 -> 核定再生料重量
    transfer_recycled: Dict[int, int] = field(default_factory=dict)
    # 每炉再生料进料/出料核对表（用于页面展示）
    furnace_balance: Dict[str, Dict[str, int]] = field(default_factory=dict)


class _Dinic:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[int]] = [[] for _ in range(n)]
        self.head: List[int] = [0] * n
        self.level: List[int] = [-1] * n
        # 边以 (to, rev, cap) 三元组就地存储
        self._to: List[int] = []
        self._rev: List[int] = []
        self._cap: List[int] = []

    def add_edge(self, u: int, v: int, cap: int) -> int:
        idx = len(self._to)
        self._to.append(v)
        self._rev.append(idx + 1)
        self._cap.append(cap)
        self.g[u].append(idx)
        # 反向边
        self._to.append(u)
        self._rev.append(idx)
        self._cap.append(0)
        self.g[v].append(idx + 1)
        return idx

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * self.n
        self.level[s] = 0
        q = [s]
        head = 0
        while head < len(q):
            u = q[head]
            head += 1
            for e in self.g[u]:
                if self._cap[e] > 0 and self.level[self._to[e]] < 0:
                    self.level[self._to[e]] = self.level[u] + 1
                    q.append(self._to[e])
        return self.level[t] >= 0

    def _dfs(self, u: int, t: int, f: int) -> int:
        if u == t:
            return f
        while self.head[u] < len(self.g[u]):
            e = self.g[u][self.head[u]]
            v = self._to[e]
            if self._cap[e] > 0 and self.level[v] == self.level[u] + 1:
                pushed = self._dfs(v, t, min(f, self._cap[e]))
                if pushed:
                    self._cap[e] -= pushed
                    self._cap[self._rev[e]] += pushed
                    return pushed
            self.head[u] += 1
        return 0

    def max_flow(self, s: int, t: int, limit: Optional[int] = None) -> int:
        flow = 0
        INF = 10**30
        limit = INF if limit is None else limit
        while flow < limit and self._bfs(s, t):
            self.head = [0] * self.n
            while flow < limit:
                pushed = self._dfs(s, t, limit - flow)
                if not pushed:
                    break
                flow += pushed
        return flow

    def reachable_from(self, s: int) -> List[bool]:
        seen = [False] * self.n
        seen[s] = True
        stack = [s]
        while stack:
            u = stack.pop()
            for e in self.g[u]:
                if self._cap[e] > 0 and not seen[self._to[e]]:
                    seen[self._to[e]] = True
                    stack.append(self._to[e])
        return seen



# 出货批归属信息：每个出货批由唯一一座炉产出，实称重量为该批称量值。
class FurnaceBatchLink:
    __slots__ = ("furnace", "batch", "weight_kg")

    def __init__(self, furnace: str, batch: str, weight_kg: int):
        self.furnace = furnace
        self.batch = batch
        self.weight_kg = weight_kg


def solve_allocation(
    furnaces: Sequence[FurnaceInput],
    batch_links: Sequence[FurnaceBatchLink],
    transfers: Sequence[TransferInput],
    requirements: Sequence[CertRequirement],
) -> SolveResult:
    """完整求解。batch_links 给出每个出货批的归属炉与实称重量。"""
    # ---------- 基础校验 ----------
    furnace_codes = [f.code for f in furnaces]
    batches = [(bl.batch, bl.weight_kg) for bl in batch_links]
    if len(set(furnace_codes)) != len(furnace_codes):
        raise SolveError("炉号存在重复，请检查账套数据。")
    if len(set(b for b, _ in batches)) != len(batches):
        raise SolveError("出货批号存在重复，请检查账套数据。")
    fmap = {f.code: f for f in furnaces}
    for bl in batch_links:
        if bl.furnace not in fmap:
            raise SolveError(f"出货批 {bl.batch} 引用了不存在的炉号 {bl.furnace}。")
        if bl.weight_kg < 0:
            raise SolveError(f"批 {bl.batch} 的实称重量不能为负。")
    for t in transfers:
        if t.src not in fmap or t.dst not in fmap:
            raise SolveError(f"转料记录引用了不存在的炉号（{t.src}->{t.dst}）。")
        if t.src == t.dst:
            raise SolveError(f"炉 {t.src} 存在转入自身的转料记录。")
        if t.weight_kg < 0:
            raise SolveError("转料重量不能为负。")
    for f in furnaces:
        if f.primary_kg < 0 or f.recycled_kg < 0:
            raise SolveError(f"炉 {f.code} 的进料重量不能为负。")

    bmap = {bl.batch: bl for bl in batch_links}
    req_by_batch: Dict[str, int] = {}
    for r in requirements:
        if r.batch not in bmap:
            raise SolveError(
                f"签单引用的出货批 {r.batch} 不在当前账套中，无法重新核定。"
            )
        if r.min_recycled_kg <= 0:
            raise SolveError(f"批 {r.batch} 的签单最低再生料重量必须为正整数。")
        if r.min_recycled_kg > bmap[r.batch].weight_kg:
            return SolveResult(
                ok=False,
                reason=(
                    f"出货批 {r.batch} 实称仅 {bmap[r.batch].weight_kg} kg，"
                    f"低于已签最低再生料 {r.min_recycled_kg} kg，单批即不可能满足。"
                ),
            )
        req_by_batch[r.batch] = r.min_recycled_kg

    total_cert = sum(req_by_batch.values())
    total_recycled_in = sum(f.recycled_kg for f in furnaces)
    if total_cert > total_recycled_in:
        return SolveResult(
            ok=False,
            reason=(
                f"全部有效签单要求再生料合计 {total_cert} kg，"
                f"超过各炉外购再生料合计 {total_recycled_in} kg，总量不足。"
            ),
        )

    # ---------- 建网络 ----------
    S, T = 0, 1
    node_f = {f.code: 2 + i for i, f in enumerate(furnaces)}
    node_b = {bl.batch: 2 + len(furnaces) + i for i, bl in enumerate(batch_links)}
    ss = 2 + len(furnaces) + len(batch_links)
    tt = ss + 1
    n = tt + 1
    d = _Dinic(n)
    INF = 10**18
    demand = [0] * n  # 正=需要净流入，负=需要净流出

    def add_lb_edge(u: int, v: int, lower: int, upper: int) -> int:
        """带下界边 [lower, upper]，返回边 id（正向）。"""
        if upper < lower:
            raise SolveError("内部错误：边容量小于下界。")
        eid = d.add_edge(u, v, upper - lower)
        demand[u] -= lower
        demand[v] += lower
        return eid

    # S -> 炉（下界 0）
    edge_supply: Dict[str, int] = {}
    for f in furnaces:
        edge_supply[f.code] = d.add_edge(S, node_f[f.code], f.recycled_kg)

    # 炉 -> 批（下界=签单下限，容量=实称重量）
    edge_batch: Dict[str, int] = {}
    for bl in batch_links:
        lb = req_by_batch.get(bl.batch, 0)
        edge_batch[bl.batch] = add_lb_edge(
            node_f[bl.furnace], node_b[bl.batch], lb, bl.weight_kg
        )

    # 批 -> T（容量=实称重量；与炉->批同容量，保证批流量不超实重）
    for bl in batch_links:
        d.add_edge(node_b[bl.batch], T, bl.weight_kg)

    # 炉 -> 炉 转料（容量=转料实重）
    edge_transfer: Dict[int, int] = {}
    for t in transfers:
        edge_transfer[t.tid] = d.add_edge(
            node_f[t.src], node_f[t.dst], t.weight_kg
        )

    # T -> S 无穷边，使可行流可循环
    edge_ts = d.add_edge(T, S, INF)

    # 超级源/汇折算
    need_total = 0
    for i in range(n):
        if demand[i] > 0:
            d.add_edge(ss, i, demand[i])
            need_total += demand[i]
        elif demand[i] < 0:
            d.add_edge(i, tt, -demand[i])

    got = d.max_flow(ss, tt)
    if got != need_total:
        reason = _explain_infeasible(d, ss, furnaces, batch_links, transfers,
                                     req_by_batch, node_f, node_b, INF)
        return SolveResult(ok=False, reason=reason)

    # ---------- 可行流已找到，拆除辅助边后最大化 S->T 流量 ----------
    # 中和 T->S 边及其反向边（残余容量清零），避免继续循环。
    rev_ts = d._rev[edge_ts]
    d._cap[edge_ts] = 0
    d._cap[rev_ts] = 0
    # 超级源/汇上的边此时均已饱和或残余对主网络无影响，保持即可。
    d.max_flow(S, T)

    # ---------- 回读流量 ----------
    batch_recycled: Dict[str, int] = {}
    for bl in batch_links:
        eid = edge_batch[bl.batch]
        lb = req_by_batch.get(bl.batch, 0)
        # 原容量 upper-lower；已用流量 = 残余减少量
        used = (bl.weight_kg - lb) - d._cap[eid]
        batch_recycled[bl.batch] = lb + used

    transfer_recycled: Dict[int, int] = {}
    for t in transfers:
        eid = edge_transfer[t.tid]
        transfer_recycled[t.tid] = t.weight_kg - d._cap[eid]

    # ---------- 每炉再生料进出核对 ----------
    balance_tbl: Dict[str, Dict[str, int]] = {}
    for f in furnaces:
        balance_tbl[f.code] = {
            "purchased": f.recycled_kg,
            "transfer_in": 0,
            "transfer_out": 0,
            "to_batches": 0,
        }
    for t in transfers:
        balance_tbl[t.src]["transfer_out"] += transfer_recycled[t.tid]
        balance_tbl[t.dst]["transfer_in"] += transfer_recycled[t.tid]
    for bl in batch_links:
        balance_tbl[bl.furnace]["to_batches"] += batch_recycled[bl.batch]
    for code, row in balance_tbl.items():
        row["in_total"] = row["purchased"] + row["transfer_in"]
        row["out_total"] = row["to_batches"] + row["transfer_out"]

    return SolveResult(
        ok=True,
        batch_recycled=batch_recycled,
        transfer_recycled=transfer_recycled,
        furnace_balance=balance_tbl,
    )


def _explain_infeasible(
    d: "_Dinic",
    ss: int,
    furnaces: Sequence[FurnaceInput],
    batch_links: Sequence["FurnaceBatchLink"],
    transfers: Sequence[TransferInput],
    req_by_batch: Dict[str, int],
    node_f: Dict[str, int],
    node_b: Dict[str, int],
    inf: int,
) -> str:
    """用残量网络最小割定位瓶颈，生成中文原因说明。"""
    seen = d.reachable_from(ss)
    bottlenecks: List[str] = []
    batch_cert_in: List[str] = []

    # 炉->批 饱和边（下界+残余容量均耗尽）
    cap_by_furnace: Dict[str, int] = {}
    for bl in batch_links:
        u, v = node_f[bl.furnace], node_b[bl.batch]
        if seen[u] and not seen[v]:
            cap_by_furnace[bl.furnace] = (
                cap_by_furnace.get(bl.furnace, 0) + bl.weight_kg
            )
            if bl.batch in req_by_batch:
                batch_cert_in.append(
                    f"{bl.batch}(签单≥{req_by_batch[bl.batch]})"
                )
    for fc, cap in sorted(cap_by_furnace.items()):
        detail = ("，涉及签单批 " + "、".join(batch_cert_in)) if batch_cert_in else ""
        bottlenecks.append(
            f"炉 {fc} 可下达的出货再生料通道（各批实重合计 {cap} kg）已全部占满，"
            f"再生料无法再经出货批排出{detail}"
        )

    # 转料饱和边
    sat_transfer = [
        t for t in transfers
        if seen[node_f[t.src]] and not seen[node_f[t.dst]]
    ]
    if sat_transfer:
        parts = [
            f"{t.src}->{t.dst}（实称上限 {t.weight_kg} kg）" for t in sat_transfer
        ]
        bottlenecks.append("回炉转料通道饱和：" + "、".join(parts))

    # S->炉 供应饱和：割集在 S 侧的炉
    starved = []
    for f in furnaces:
        u = node_f[f.code]
        # 炉在割的需求侧且有签单批挂在它下面
        if not seen[u] and any(
            bl.furnace == f.code and bl.batch in req_by_batch
            for bl in batch_links
        ):
            starved.append(
                f"{f.code}(外购再生料 {f.recycled_kg} kg)"
            )
    if starved:
        bottlenecks.append(
            "以下炉自身外购再生料已耗尽且得不到足够转入：" + "、".join(starved)
        )

    if not bottlenecks:
        # 兜底：总量口径
        total_cert = sum(req_by_batch.values())
        total_in = sum(f.recycled_kg for f in furnaces)
        return (
            f"账套与全部有效签单无可行分配：签单需求合计 {total_cert} kg，"
            f"再生料供应合计 {total_in} kg，且受各批实重/转料通道限制无法调配。"
        )

    head = "新账/签单无可行解，瓶颈如下："
    return head + "；".join(bottlenecks) + "。"
