"""半开时间区间 [start, end) 的并集与重叠计算。

同一实例内并行任务的处理区间在墙钟上重叠，历时分解必须基于区间并集，
而不是把各任务时长简单相加。所有度量以秒为单位，区间端点为带时区的
datetime；相邻区间（一端等于下一端点）不算重叠。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

Interval = tuple[datetime, datetime]


def normalize(intervals: Iterable[Interval]) -> list[Interval]:
    """合并重叠或相邻的区间，返回按起点排序的不相交并集。"""
    ordered = sorted((s, e) for s, e in intervals if s is not None and e is not None and e > s)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def union_seconds(intervals: Iterable[Interval]) -> float:
    """区间并集的总秒数（并行重叠只计一次）。"""
    return float(sum((e - s).total_seconds() for s, e in normalize(intervals)))


def overlap_seconds(intervals: Iterable[Interval]) -> float:
    """被并行重复覆盖的秒数 = 各区间长度之和 - 并集长度。"""
    items = [(s, e) for s, e in intervals if s is not None and e is not None and e > s]
    raw = sum((e - s).total_seconds() for s, e in items)
    return float(raw - union_seconds(items))


def cover_seconds(intervals: Iterable[Interval], window: Interval) -> float:
    """区间集合与给定窗口相交的并集秒数（用于截尾观察量）。"""
    w_start, w_end = window
    clipped = [(max(s, w_start), min(e, w_end)) for s, e in normalize(intervals)]
    return float(sum((e - s).total_seconds() for s, e in clipped if e > s))


def subtract(intervals: Iterable[Interval], blockers: Iterable[Interval]) -> list[Interval]:
    """从 intervals 的并集中挖掉 blockers 并集，返回剩余不相交区间。"""
    base = normalize(intervals)
    cuts = normalize(blockers)
    result: list[Interval] = []
    for start, end in base:
        cursor = start
        for cut_start, cut_end in cuts:
            if cut_end <= cursor:
                continue
            if cut_start >= end:
                break
            if cut_start > cursor:
                result.append((cursor, min(cut_start, end)))
            cursor = max(cursor, cut_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return normalize(result)


def partition(window: Interval, layers: Sequence[Iterable[Interval]]) -> list[list[Interval]]:
    """把窗口按给定层级顺序切分，每层挖掉前面各层后取剩余部分。

    用于把一个实例的墙钟窗口守恒地分解为：处理、外部等待、排队、
    无法分解。返回的各层区间两两不相交，并集与窗口完全一致（最后一层
    兜底所有剩余时间），因此各层秒数之和恒等于窗口秒数。
    """
    covered: list[Interval] = []
    pieces: list[list[Interval]] = []
    for index, layer in enumerate(layers):
        if index == len(layers) - 1:
            part = subtract([window], covered)
        else:
            part = subtract(layer, covered)
            covered = normalize(covered + part)
        pieces.append(part)
    return pieces


def hours(seconds: float) -> float:
    return round(seconds / 3600.0, 6)
