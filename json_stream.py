#!/usr/bin/env python3
"""Small streaming helpers for large top-level JSON arrays."""

import json
import os
from pathlib import Path


READ_SIZE = 1024 * 1024


def iter_json_array(path):
    """Yield values without loading the whole top-level JSON array."""
    decoder = json.JSONDecoder()
    with Path(path).open("r", encoding="utf-8") as source:
        buffer = ""
        position = 0
        eof = False
        started = False

        while True:
            if position:
                buffer = buffer[position:]
                position = 0
            if not eof:
                chunk = source.read(READ_SIZE)
                if chunk:
                    buffer += chunk
                else:
                    eof = True

            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if not started:
                if position >= len(buffer):
                    if eof:
                        raise ValueError(f"Empty JSON document: {path}")
                    continue
                if buffer[position] != "[":
                    raise ValueError(f"Expected a JSON array: {path}")
                position += 1
                started = True

            while True:
                while position < len(buffer) and (
                    buffer[position].isspace() or buffer[position] == ","
                ):
                    position += 1
                if position < len(buffer) and buffer[position] == "]":
                    return
                if position >= len(buffer):
                    break
                try:
                    value, end = decoder.raw_decode(buffer, position)
                except json.JSONDecodeError:
                    if eof:
                        raise
                    break
                yield value
                position = end

            if eof:
                raise ValueError(f"Unterminated JSON array: {path}")


def write_json_array_atomic(path, values):
    """Stream an iterable to a compact JSON array, then atomically replace it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            output.write("[")
            first = True
            for value in values:
                if not first:
                    output.write(",")
                json.dump(value, output, ensure_ascii=False, separators=(",", ":"))
                first = False
            output.write("]")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class DiskCatalog:
    """Temporary, bounded-cache catalog for merges requiring a global sort."""

    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        import sqlite3
        import tempfile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="catalog-merge-", dir=self.path.parent)
        try:
            self.db = sqlite3.connect(str(Path(self.directory.name) / "catalog.db"))
            self.db.execute("PRAGMA cache_size=-4096")
            self.db.execute("PRAGMA temp_store=FILE")
            self.db.execute("CREATE TABLE rows (code TEXT PRIMARY KEY, digits INTEGER, numeric TEXT, date TEXT, value TEXT)")
            if self.path.exists():
                for item in iter_json_array(self.path):
                    if item.get("code"):
                        self[str(item["code"])] = item
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __setitem__(self, code, item):
        code = str(code)
        numeric = str(int(code)) if code.isdigit() else ""
        self.db.execute("INSERT OR REPLACE INTO rows VALUES (?, ?, ?, ?, ?)",
                        (code, len(numeric), numeric, str(item.get("date") or ""),
                         json.dumps(item, ensure_ascii=False, separators=(",", ":"))))

    def get(self, code):
        row = self.db.execute("SELECT value FROM rows WHERE code=?", (str(code),)).fetchone()
        return json.loads(row[0]) if row else None

    def values(self):
        for row in self.db.execute("SELECT value FROM rows ORDER BY digits DESC, numeric DESC, date DESC, code DESC"):
            yield json.loads(row[0])

    def __len__(self):
        return self.db.execute("SELECT count(*) FROM rows").fetchone()[0]

    def __exit__(self, *args):
        if hasattr(self, "db"):
            self.db.close()
        self.directory.cleanup()
