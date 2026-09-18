#!/usr/bin/env python3
"""Reproducible source-only archives from an explicit reviewed file list."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '1.0.0'
PREFIX = f'avtool-{VERSION}-source'
EPOCH = 315532800  # 1980-01-01: supported by ZIP; contains no host timestamps.


def source_files():
    entries = json.loads((ROOT / 'source-manifest.json').read_text())
    if not isinstance(entries, list) or len(entries) != len(set(entries)):
        raise ValueError('Invalid source manifest')
    result = []
    for name in sorted(entries):
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or '\\' in name or path.as_posix() != name:
            raise ValueError('Invalid manifest path')
        target = ROOT / name
        if any(part.is_symlink() for part in [target, *target.parents] if part != ROOT.parent):
            raise ValueError(f'Symlink cannot be released: {name}')
        if not target.is_file() or not target.resolve().is_relative_to(ROOT):
            raise ValueError(f'Missing source: {name}')
        if set(path.parts) & {'.git', '.venv', 'node_modules', 'private', '.secrets', 'data', 'var', 'backups', 'downloads', 'library', 'out', 'dist', 'build', '__pycache__'}:
            raise ValueError(f'Runtime path cannot be released: {name}')
        if path.name == 'config.json' or re.search(r'(?i)(client_secret|credentials|cookies?\.|rclone\.conf|\.sqlite|\.log$|\.pem$|\.key$|\.env$|\.pyc$|\.map$|\.zip$|\.tar(?:\.gz)?$)', name):
            raise ValueError(f'Private file cannot be released: {name}')
        result.append((name, target))
    return result


def scan(name, data):
    text = data.decode('utf-8')
    patterns = [r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                r'GOCSPX-[A-Za-z0-9_-]{10,}', r'AIza[A-Za-z0-9_-]{25,}',
                r'gh[pousr]_[A-Za-z0-9]{25,}', r'github_pat_[A-Za-z0-9_]{20,}',
                r'ya29\.[A-Za-z0-9_-]{20,}', r'\d{10,}-[a-z0-9]+\.apps\.googleusercontent\.com',
                r'AKIA[0-9A-Z]{16}', r'sk-[A-Za-z0-9_-]{24,}',
                r'(?i)"(?:access_token|refresh_token|client_secret|password)"\s*:\s*"[^"\s]{8,}"',
                r'(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\s*[=:]\s*[\x27\x22][A-Za-z0-9_./+-]{16,}[\x27\x22]',
                r'/(?:root|mnt|home|opt|srv)/[A-Za-z0-9_.-]+/',
                r'[A-Z]:\\Users\\[^\\\s]+\\']
    for pattern in patterns:
        if re.search(pattern, text):
            raise ValueError(f'Potential private data in {name}; inspect locally')
    allowed = {'bilibili.com', 'douyin.com', 'b23.tv', 'github.com', 'registry.npmjs.org',
               'opensource.org', 'spdx.org', 'opencollective.com', 'tidelift.com',
               'react.dev', 'reactjs.org', 'w3.org', 'apache.org', 'hlsjs.video-dev.org',
               'ffmpeg.org', 'rclone.org', 'python.org', 'nodejs.org', 'rolldown.rs',
               'localhost', '127.0.0.1', 'example.com', 'example.org', 'example.invalid'}
    # Preserve required upstream copyright/attribution links in license texts.
    if name.startswith('licenses/'):
        allowed |= {'dailymotion.com', 'paulmillr.com', 'sindresorhus.com', 'jonathantneal.com',
                    'jquery.org', 'jquery.com', 'mozilla.org', '2ality.com', 'purl.org',
                    'adobe.com', 'google.com', 'dojofoundation.org', 'mootools.net',
                    'beneb.info', 'creativecommons.org', 'lukeed.com', 'mathiasbynens.be', 'underscorejs.org'}
    for address in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])', text):
        if address not in {'127.0.0.1', '0.0.0.0'}:
            raise ValueError(f'Non-loopback IP address in {name}; inspect locally')
    for url in re.findall(r'https?://[^\s<>"\x27`\\]+', text):
        try:
            host = urlsplit(url.rstrip(').,;')).hostname or ''
        except ValueError:
            raise ValueError(f'Invalid URL in {name}')
        if host in {'…', '...', '{host}', '{self.server.server_port}', '{httpd.server_port}'}:
            continue
        if host.endswith(('.example', '.test', '.invalid')):
            continue
        if not any(host == domain or host.endswith('.' + domain) for domain in allowed):
            raise ValueError(f'Unreviewed URL host in {name}; inspect locally')


def release_files():
    entries = source_files()
    payload = []
    for name, path in entries:
        data = path.read_bytes()
        scan(name, data)
        payload.append((name, data))
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payload}
    payload.append(('FILE-SHA256.json', (json.dumps(hashes, indent=2, sort_keys=True) + '\n').encode()))
    return payload


def build(output):
    payload = release_files()  # Finish validation before creating any artifact.
    output.mkdir(parents=True, exist_ok=True)
    zip_path, tar_path = output / f'{PREFIX}.zip', output / f'{PREFIX}.tar.gz'
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in payload:
            info = zipfile.ZipInfo(f'{PREFIX}/{name}', (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    with tar_path.open('wb') as raw:
        with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=EPOCH) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                for name, data in payload:
                    info = tarfile.TarInfo(f'{PREFIX}/{name}')
                    info.size, info.mtime = len(data), EPOCH
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    info.mode = 0o755 if name.endswith('.sh') else 0o644
                    archive.addfile(info, io.BytesIO(data))
    checksum = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in [zip_path, tar_path])
    (output / 'SHA256SUMS.txt').write_text(checksum)
    print(f'Validated {len(payload)} files; produced {zip_path.name} and {tar_path.name}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build(args.output)
