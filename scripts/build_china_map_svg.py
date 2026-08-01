#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 DataV 中国省级 GeoJSON 转成「紧凑内联 SVG 省界路径」JSON，供首页省际分布地图使用。

输出：static/geo/china_provinces.min.json
  {
    "viewBox": "0 0 1000 H",
    "provinces": { "广东省": "M.. Z", ... },   # 34 个省/直辖市/自治区/特别行政区/台湾
    "labels":    { "广东省": [x, y], ... },     # 省界主多边形质心（SVG 坐标，可放标记/标签）
    "scs": { "box": [x, y, w, h], "paths": ["M.. Z", ...] }  # 南海诸岛＋断续线 角标插图
  }

设计要点：
  · 投影：标准纬线 35°N 的等距圆柱（X=lon·cos35°, Y=-lat），小图下形状自然、识别度高。
  · 政治合规：台湾/港澳作为省级要素同色纳入；南海诸岛（含断续线）以右下角插图绘出。
  · 三沙/南海诸岛细碎岛礁（质心纬度<16°N）从各省主图剔除，只在插图里出现，避免主图被拉长。
  · 用 shapely 做 Douglas–Peucker 简化（容差 0.02°）+ 坐标一位小数，体积可控（~目标 <120KB）。

依赖：shapely（仓库已装）。源 GeoJSON：data/geo/china_provinces_raw.json
  下载自 https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json （阿里 DataV 公开边界）。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys

from shapely.geometry import shape
from shapely.ops import unary_union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "geo", "china_provinces_raw.json")
OUT = os.path.join(ROOT, "static", "geo", "china_provinces.min.json")

LAT0 = 35.0                      # 标准纬线
KX = math.cos(math.radians(LAT0))
SIMPLIFY_TOL = 0.02              # 度
MIN_RING_AREA = 0.004           # 度²，小于此且非该省最大块的碎岛丢弃（省最大块永远保留）
SCS_LAT_CUTOFF = 16.0           # 质心纬度低于此的多边形视为南海岛礁，移出主图
MAP_W = 1000.0                  # 主图目标宽
PAD = 12.0
SCS_ADCODE = "100000_JD"


def _project(lon: float, lat: float) -> tuple[float, float]:
    return lon * KX, -lat


def _ring_to_path(points: list[tuple[float, float]], sx, sy, tol=0.1) -> str:
    """投影后的 (X,Y) 序列 → SVG path，一位小数、去掉相邻重复点。"""
    out = []
    last = None
    for X, Y in points:
        x = round(sx(X), 1)
        y = round(sy(Y), 1)
        if last is not None and abs(x - last[0]) < tol and abs(y - last[1]) < tol:
            continue
        out.append((x, y))
        last = (x, y)
    if len(out) < 3:
        return ""
    d = "M" + " ".join(f"{x} {y}" for x, y in out) + "Z"
    return d


def _poly_rings(poly):
    """取多边形外环坐标（投影前 lon/lat），忽略内环（小图无需镂空）。"""
    return list(poly.exterior.coords)


def main() -> int:
    gj = json.load(io.open(SRC, "r", encoding="utf-8"))
    feats = gj["features"]

    # 第一遍：简化、按规则筛多边形，收集所有保留点用于定标定 bbox（投影空间）。
    province_polys: dict[str, list] = {}
    scs_geom = None
    minX = minY = math.inf
    maxX = maxY = -math.inf

    for f in feats:
        name = (f.get("properties") or {}).get("name") or ""
        adcode = str((f.get("properties") or {}).get("adcode") or "")
        geom = shape(f["geometry"])
        if adcode == SCS_ADCODE:
            scs_geom = geom.simplify(0.03, preserve_topology=True)
            continue
        if not name:
            continue
        geom = geom.simplify(SIMPLIFY_TOL, preserve_topology=True)
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        # 先算每块面积/质心，决定保留集
        scored = []
        for p in polys:
            if p.is_empty or p.area <= 0:
                continue
            c = p.centroid
            scored.append((p.area, c.y, p))
        if not scored:
            continue
        scored.sort(key=lambda t: t[0], reverse=True)
        largest_area = scored[0][0]
        kept = []
        for area, clat, p in scored:
            if clat < SCS_LAT_CUTOFF:          # 南海岛礁：移出主图
                continue
            if area < MIN_RING_AREA and area < largest_area:
                continue
            kept.append(p)
        if not kept:                            # 兜底：保留最大块，保证该省可见
            kept = [scored[0][2]]
        province_polys[name] = kept
        for p in kept:
            for lon, lat in _poly_rings(p):
                X, Y = _project(lon, lat)
                minX = min(minX, X); maxX = max(maxX, X)
                minY = min(minY, Y); maxY = max(maxY, Y)

    # 定标：主图等比缩放到宽 MAP_W
    span_x = maxX - minX
    span_y = maxY - minY
    scale = (MAP_W - 2 * PAD) / span_x
    map_h = span_y * scale + 2 * PAD

    def sx(X):  # 投影 X → SVG x
        return (X - minX) * scale + PAD

    def sy(Y):  # 投影 Y → SVG y
        return (Y - minY) * scale + PAD

    provinces: dict[str, str] = {}
    labels: dict[str, list] = {}
    for name, polys in province_polys.items():
        parts = []
        for p in polys:
            pts = [_project(lon, lat) for lon, lat in _poly_rings(p)]
            d = _ring_to_path(pts, sx, sy)
            if d:
                parts.append(d)
        if not parts:
            continue
        provinces[name] = "".join(parts)
        # 质心标签锚点：取最大块质心
        big = max(polys, key=lambda p: p.area)
        c = big.centroid
        cx, cy = _project(c.x, c.y)
        labels[name] = [round(sx(cx), 1), round(sy(cy), 1)]

    # 南海诸岛插图：自有 bbox 投影到右下角小框
    scs = None
    if scs_geom is not None and not scs_geom.is_empty:
        sgeoms = list(scs_geom.geoms) if scs_geom.geom_type == "MultiPolygon" else [scs_geom]
        sminX = sminY = math.inf
        smaxX = smaxY = -math.inf
        proj_polys = []
        for p in sgeoms:
            if p.is_empty:
                continue
            ring = [_project(lon, lat) for lon, lat in _poly_rings(p)]
            proj_polys.append(ring)
            for X, Y in ring:
                sminX = min(sminX, X); smaxX = max(smaxX, X)
                sminY = min(sminY, Y); smaxY = max(smaxY, Y)
        s_span_x = smaxX - sminX
        s_span_y = smaxY - sminY
        # 插图框：右下角，宽约主图 16%
        box_w = MAP_W * 0.165
        inner_pad = 4.0
        s_scale = (box_w - 2 * inner_pad) / s_span_x
        box_h = s_span_y * s_scale + 2 * inner_pad
        box_x = MAP_W - box_w - PAD
        box_y = map_h - box_h - PAD

        def ssx(X):
            return (X - sminX) * s_scale + box_x + inner_pad

        def ssy(Y):
            return (Y - sminY) * s_scale + box_y + inner_pad

        spaths = []
        for ring in proj_polys:
            d = _ring_to_path(ring, ssx, ssy, tol=0.05)
            if d:
                spaths.append(d)
        scs = {
            "box": [round(box_x, 1), round(box_y, 1), round(box_w, 1), round(box_h, 1)],
            "paths": spaths,
        }

    payload = {
        "viewBox": f"0 0 {round(MAP_W,1)} {round(map_h,1)}",
        "provinceCount": len(provinces),
        "provinces": provinces,
        "labels": labels,
        "scs": scs,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with io.open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT)
    print(f"wrote {OUT}")
    print(f"  provinces={len(provinces)}  viewBox={payload['viewBox']}  size={size/1024:.1f}KB")
    missing = []
    canonical = {(f.get('properties') or {}).get('name') for f in feats if (f.get('properties') or {}).get('name')}
    for nm in canonical:
        if nm and nm not in provinces:
            missing.append(nm)
    if missing:
        print("  WARNING missing provinces:", missing)
    print("  scs:", "yes" if scs else "no", (f"{len(scs['paths'])} paths" if scs else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
