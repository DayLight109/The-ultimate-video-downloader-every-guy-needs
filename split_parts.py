#!/usr/bin/env python3
"""Split index.json into browser-friendly per-site chunks atomically."""

import os
import json
import time
from pathlib import Path

import av_tui
import popular_index
from json_stream import iter_json_array


ROOT = Path(os.environ.get("AVTOOL_ROOT", str(Path(__file__).resolve().parent)))
DATA = ROOT / "data"
OUTPUT = DATA / "by_site"
CHUNK_SIZE = 500
POPULAR_GLOBAL_LIMIT = 200
POPULAR_SITE_LIMIT = 100


def chunks(items, size):
    for index in range(0, len(items), size):
        yield items[index:index + size]


def browser_item(item):
    return {
        "site": item.get("site", ""),
        "code": item.get("code", ""),
        "title": item.get("title", ""),
        "page_url": item.get("page_url", ""),
        "duration": item.get("duration", ""),
        "date": item.get("date", ""),
        "source_views": av_tui.parse_source_views(item.get("source_views")),
        "tags": item.get("tags") or item.get("tags_cn") or [],
        "has_local": bool(item.get("has_local")),
        "downloadable": bool(item.get("downloadable") or item.get("video_url")),
        "s": item.get("search_text", "") or "",
        "thumb": item.get("thumb", "") or item.get("image", "") or "",
    }


def rebuild():
    states = {}
    popular_all = []
    popular_sites = {}

    def add_popular(item):
        views = av_tui.parse_source_views(item.get("source_views"))
        if views <= 0:
            return
        compact = browser_item(item)
        popular_all.append(compact)
        site = str(compact.get("site") or "")
        popular_sites.setdefault(site, []).append(compact)
        # Keep only the leading candidates between batches; the catalog can
        # contain hundreds of thousands of rows, but the UI needs only top N.
        if len(popular_all) >= POPULAR_GLOBAL_LIMIT + CHUNK_SIZE:
            trim_popular()

    def trim_popular():
        popular_all.sort(key=lambda row: (-av_tui.parse_source_views(row.get("source_views")), row.get("site", ""), row.get("code", "")))
        del popular_all[POPULAR_GLOBAL_LIMIT:]
        for site, rows in popular_sites.items():
            rows.sort(key=lambda row: (-av_tui.parse_source_views(row.get("source_views")), row.get("code", "")))
            del rows[POPULAR_SITE_LIMIT:]

    def write_part(site, state):
        name = f"{site}.p{state['parts']}.json"
        path = OUTPUT / name
        av_tui.write_json_atomic(path, state["buffer"])
        state["expected"].add(name)
        state["size"] += path.stat().st_size
        state["parts"] += 1
        state["buffer"] = []

    OUTPUT.mkdir(parents=True, exist_ok=True)
    for item in iter_json_array(DATA / "index.json"):
        if not isinstance(item, dict):
            continue
        site = item.get("site")
        code = item.get("code")
        if not site or not code or not av_tui.catalog_title_usable(item):
            continue
        add_popular(item)
        state = states.setdefault(site, {
            "buffer": [], "count": 0, "parts": 0, "size": 0, "expected": set()
        })
        if len(state["buffer"]) == CHUNK_SIZE:
            write_part(site, state)
        state["buffer"].append(browser_item(item))
        state["count"] += 1

    expected = set()
    metadata = []
    for site, state in states.items():
        if state["parts"] == 0:
            name = f"{site}.json"
            path = OUTPUT / name
            av_tui.write_json_atomic(path, state["buffer"])
            state["expected"].add(name)
            state["size"] += path.stat().st_size
            state["parts"] = 1
        elif state["buffer"]:
            write_part(site, state)
        expected.update(state["expected"])
        metadata.append(
            {
                "site": site,
                "count": state["count"],
                "size": state["size"],
                "parts": state["parts"],
            }
        )
        print(f"{site}: {state['count']} 条, {state['parts']} 个分片", flush=True)

    for path in OUTPUT.glob("*.json"):
        if path.name not in expected:
            path.unlink()
    metadata.sort(key=lambda item: -item["count"])
    av_tui.write_json_atomic(DATA / "sites.json", metadata)
    trim_popular()
    av_tui.write_json_atomic(
        DATA / "popular.json",
        {
            "updated_at": time.time(),
            "all": popular_all,
            "sites": popular_sites,
        },
    )
    print(f"sites.json: {sum(item['count'] for item in metadata)} 条", flush=True)
    # The small JSON above remains a preview for older clients. The full
    # searchable ranking has no top-N cutoff and is published atomically.
    ranked = popular_index.build(DATA / 'index.json', DATA.parent / 'var/popularity.sqlite3')
    print(f"popular index: {ranked['total']} 条（全量）", flush=True)
    return metadata


if __name__ == "__main__":
    rebuild()
