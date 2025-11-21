#!/usr/bin/env python3

"""
生成：天河区（广州市） 地铁 + 公交（OSM/Overpass）单文件 GeoJSON 并打包为 zip

说明：
- 先用 Nominatim 找到 "天河区 广州" 的 relation id，再用 Overpass 抓取 area 内的 relations type=route
- 支持 route=bus, tram, subway, light_rail（其中 subway/light_rail 视为地铁）
- 输出 single GeoJSON 包含 LineString (routes) 与 Point (stops)
- 依赖：requests
"""

import requests
import time
import json
import sys
from collections import defaultdict
from zipfile import ZipFile
from pathlib import Path

# 配置
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "tianhe-transport-extractor/1.0 (+https://example.org/)"

OUT_GEOJSON = "tianhe_transport.geojson"
OUT_ZIP = "tianhe_transport.zip"
TIMEOUT = 180  # seconds for Overpass

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

def get_tianhe_relation_id():
    q = "天河区 广州"
    params = {"q": q, "format": "json", "limit": 5, "accept-language": "zh-CN"}
    r = session.get(NOMINATIM_URL, params=params, timeout=30)
    r.raise_for_status()
    results = r.json()
    for item in results:
        if item.get("osm_type") == "relation":
            return int(item["osm_id"])
    if results and results[0].get("osm_type") == "relation":
        return int(results[0]["osm_id"])
    raise RuntimeError("未在 Nominatim 中找到天河区的 relation id，请检查网络或手动提供 relation id。")

def build_overpass_query(area_relation_id):
    area_id = 3600000000 + area_relation_id
    q = f"""
    [out:json][timeout:{TIMEOUT}];
    area({area_id})->.searchArea;
    (
      relation["type"="route"]["route"~"bus|tram|subway|light_rail"](area.searchArea);
    );
    out body;
    >;
    out skel qt;
    """
    return q

def fetch_overpass(q):
    r = session.post(OVERPASS_URL, data={"data": q}, timeout=TIMEOUT+30)
    r.raise_for_status()
    return r.json()

def assemble_features(osm):
    nodes = {}
    ways = {}
    relations = []
    for el in osm.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"], el.get("tags", {}))
        elif el["type"] == "way":
            ways[el["id"]] = {
                "nodes": el.get("nodes", []),
                "tags": el.get("tags", {})
            }
        elif el["type"] == "relation":
            relations.append(el)

    features = []
    stop_features = []
    seen_stop_nodes = {}

    for rel in relations:
        tags = rel.get("tags", {})
        route_type = tags.get("route", "").lower()
        if route_type not in ("bus", "tram", "subway", "light_rail"):
            continue
        mode = "metro" if route_type in ("subway", "light_rail") else "bus"

        coords = []
        member_nodes_sequence = []
        stops_in_rel = []
        for m in rel.get("members", []):
            if m.get("type") == "node" and (m.get("role") in ("stop","stop_exit","platform","") or m.get("role")==''):
                nid = m.get("ref")
                if nid in nodes:
                    lon, lat, _ = nodes[nid]
                    member_nodes_sequence.append((nid, lon, lat))
                    stops_in_rel.append(nid)
            if m.get("type") == "way":
                wid = m.get("ref")
                w = ways.get(wid)
                if w:
                    for nid in w["nodes"]:
                        if nid in nodes:
                            lon, lat, _ = nodes[nid]
                            coords.append([lon, lat])
        if not coords and member_nodes_sequence:
            coords = [[lon, lat] for (_, lon, lat) in member_nodes_sequence]

        dedup_coords = []
        prev = None
        for c in coords:
            if prev != c:
                dedup_coords.append(c)
            prev = c

        route_props = {
            "mode": mode,
            "network": tags.get("network") or tags.get("operator"),
            "route_id": tags.get("ref") or str(rel.get("id")),
            "name": tags.get("name") or tags.get("ref") or ("route/"+str(rel.get("id"))),
            "operator": tags.get("operator"),
            "osm_relation_id": rel.get("id"),
            "route": tags.get("route"),
            "colour": tags.get("colour") or tags.get("line"),
        }
        if dedup_coords:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": dedup_coords
                },
                "properties": route_props
            })

        seq = 1
        for m in rel.get("members", []):
            if m.get("type") == "node" and m.get("role") in ("stop","platform","stop_exit",""):
                nid = m.get("ref")
                if nid in nodes:
                    lon, lat, ntags = nodes[nid]
                    if nid in seen_stop_nodes:
                        seen_stop_nodes[nid]["routes"].append(route_props["route_id"])
                    else:
                        sf = {
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [lon, lat]},
                            "properties": {
                                "mode": mode,
                                "stop_id": ntags.get("ref") or str(nid),
                                "stop_name": ntags.get("name"),
                                "osm_node_id": nid,
                                "sequence": seq,
                                "routes": [route_props["route_id"]],
                                "tags": ntags
                            }
                        }
                        stop_features.append(sf)
                        seen_stop_nodes[nid] = sf["properties"]
                    seq += 1

    all_features = features + stop_features
    return {"type": "FeatureCollection", "features": all_features}

def save_geojson(fc, fn):
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, indent=2)

def make_zip(filepaths, zipname):
    with ZipFile(zipname, "w") as z:
        for p in filepaths:
            z.write(p, Path(p).name)

def main():
    print("1) 获取天河区 relation id（Nominatim）...")
    rid = get_tianhe_relation_id()
    print("   天河区 relation id =", rid)
    print("2) 构造 Overpass 查询并抓取 relations（route）...")
    q = build_overpass_query(rid)
    osm = fetch_overpass(q)
    print("   Overpass 返回元素数量：", len(osm.get("elements", [])))
    print("3) 组装 GeoJSON ...")
    fc = assemble_features(osm)
    print("   routes+stops 特征数：", len(fc["features"]))
    print(f"4) 保存为 {OUT_GEOJSON} ...")
    save_geojson(fc, OUT_GEOJSON)
    print("5) 打包为 zip ...")
    make_zip([OUT_GEOJSON], OUT_ZIP)
    print("完成。生成文件：", OUT_GEOJSON, "和", OUT_ZIP)
    print("建议将 zip 上传到 transfer.sh:  curl --upload-file tianhe_transport.zip https://transfer.sh/tianhe_transport.zip")
    print("或者把文件推到你的 GitHub 仓库。")

if __name__ == "__main__":
    main()
