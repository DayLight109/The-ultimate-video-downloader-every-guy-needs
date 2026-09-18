"""Catalog compatibility helpers; no embedded sites, accounts or video catalog."""
import math
import re
from json_stream import write_json_array_atomic


def parse_source_views(value):
    if isinstance(value, bool) or value is None:
        return 0
    text = str(value).strip().lower().replace(',', '')
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kmb万亿]?)', text)
    if not match:
        return 0
    number = float(match[1]) * {'': 1, 'k': 1000, 'm': 1000000, 'b': 1000000000,
                               '万': 10000, '亿': 100000000}[match[2]]
    return min(9223372036854775807, int(number)) if math.isfinite(number) else 0


def catalog_title_usable(item):
    return isinstance(item, dict) and bool(str(item.get('title') or '').strip())


def write_json_atomic(path, value):
    import json
    import os
    import tempfile
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def download_by_code(*_args, **_kwargs):
    raise RuntimeError('Use the cloud cache queue; local media download is disabled')
