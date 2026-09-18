#!/usr/bin/env python3
"""Check reviewed source files, the Git index and every reachable Git blob."""
import json
from pathlib import Path
import subprocess

import pack


def git(*args):
    return subprocess.check_output(['git', '-C', str(pack.ROOT), *args], stderr=subprocess.PIPE)


def audit():
    source = pack.source_files()
    expected = {name for name, _path in source}
    for name, path in source:
        pack.scan(name, path.read_bytes())
    report = {'source_files': len(source), 'index_files': 0, 'history_blobs': 0, 'commits': 0,
              'build_artifacts_included': False}
    if not (pack.ROOT / '.git').exists():
        report['git'] = 'not initialized; source-only check'
        return report
    indexed = set()
    for entry in git('ls-files', '--stage', '-z').split(b'\0'):
        if not entry: continue
        metadata, raw_name = entry.split(b'\t', 1)
        mode, object_id, stage = metadata.decode().split()
        name = raw_name.decode('utf-8')
        if stage != '0' or mode not in ('100644', '100755') or name not in expected:
            raise ValueError(f'Unreviewed Git index entry: {name}')
        pack.scan(name, git('cat-file', 'blob', object_id))
        indexed.add(name)
    if indexed != expected:
        raise ValueError('Git index differs from the reviewed source manifest')
    report['index_files'] = len(indexed)
    for row in git('rev-list', '--objects', '--all').decode().splitlines():
        object_id, _, name = row.partition(' ')
        kind = git('cat-file', '-t', object_id).strip()
        if kind == b'blob':
            if name not in expected:
                raise ValueError(f'Unreviewed file in Git history: {name}')
            pack.scan(name, git('cat-file', 'blob', object_id))
            report['history_blobs'] += 1
        elif kind == b'commit':
            pack.scan('Git commit metadata', git('cat-file', 'commit', object_id))
            report['commits'] += 1
    report['git'] = 'index and all reachable history checked'
    return report


if __name__ == '__main__':
    try:
        print(json.dumps(audit(), ensure_ascii=False, indent=2))
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc))
