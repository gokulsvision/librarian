#!/usr/bin/env python3
"""Filename librarian driven by library.md.

Rules in the index file first. Qwen 3.5 0.8B only chooses among those
shelf names for leftovers, then unloads. Nothing moves without --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
APP = HOME / "Library/Application Support/Librarian"
INDEX_ICLOUD = HOME / "Library/Mobile Documents/com~apple~CloudDocs/index.md"
INDEX_USER = APP / "library.md"
MODEL = os.environ.get("LIBRARIAN_MODEL", "qwen3.5:0.8b")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
SKIP_EXT = {".crdownload", ".download", ".part", ".tmp", ".ds_store"}


@dataclass
class Shelf:
    slug: str
    path: Path
    extensions: set[str] = field(default_factory=set)
    names: list[str] = field(default_factory=list)


@dataclass
class Library:
    inboxes: list[Path]
    leave: list[Path]
    shelves: list[Shelf]

    def slugs(self) -> list[str]:
        return [s.slug for s in self.shelves]

    def by_slug(self, slug: str) -> Shelf | None:
        for s in self.shelves:
            if s.slug == slug:
                return s
        return None


def expand(p: str) -> Path:
    return Path(p.strip()).expanduser()


def parse_index(text: str) -> Library:
    inboxes: list[Path] = []
    leave: list[Path] = []
    shelves: list[Shelf] = []
    current: Shelf | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            # keep ## headings
            pass
        if line.lower().startswith("scan:"):
            inboxes = [expand(x) for x in line.split(":", 1)[1].split(",") if x.strip()]
            continue
        if line.lower().startswith("leave:"):
            leave = [expand(x) for x in line.split(":", 1)[1].split(",") if x.strip()]
            continue
        if line.startswith("## "):
            slug = line[3:].strip().lower().replace(" ", "-")
            current = Shelf(slug=slug, path=HOME / "Downloads" / slug)
            shelves.append(current)
            continue
        if not current:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "path" and val:
            current.path = expand(val)
        elif key == "files" and val:
            current.extensions = {
                (x if x.startswith(".") else f".{x}").lower()
                for x in val.replace(",", " ").split()
                if x
            }
        elif key == "names" and val:
            current.names = [x.lower() for x in val.replace(",", " ").split() if x]

    if not inboxes:
        inboxes = [HOME / "Downloads", HOME / "Desktop"]
    if not any(s.slug == "other" for s in shelves):
        shelves.append(Shelf(slug="other", path=HOME / "Downloads" / "inbox"))
    return Library(inboxes=inboxes, leave=leave, shelves=shelves)


def load_library(path: Path) -> Library:
    return parse_index(path.read_text())


def ensure_user_index() -> Path:
    APP.mkdir(parents=True, exist_ok=True)
    bundled = Path(__file__).resolve().parent / "index.md"
    if not bundled.exists():
        bundled = Path(__file__).resolve().parent / "library.md"
    if INDEX_ICLOUD.exists():
        return INDEX_ICLOUD
    if not INDEX_USER.exists() and bundled.exists():
        INDEX_USER.write_text(bundled.read_text())
        if not INDEX_ICLOUD.exists():
            try:
                INDEX_ICLOUD.parent.mkdir(parents=True, exist_ok=True)
                INDEX_ICLOUD.write_text(bundled.read_text())
                return INDEX_ICLOUD
            except OSError:
                pass
    return INDEX_USER if INDEX_USER.exists() else bundled


def iter_files(folder: Path):
    if not folder.is_dir():
        return
    for p in sorted(folder.iterdir()):
        if p.name.startswith(".") or p.is_dir():
            continue
        if p.suffix.lower() in SKIP_EXT:
            continue
        if p.name.lower() in {"index.md", "library.md"}:
            continue
        yield p


def match_rule(path: Path, lib: Library) -> Shelf | None:
    name = path.name.lower()
    ext = path.suffix.lower()
    # keyword shelves first (resume beats .pdf → docs)
    for shelf in lib.shelves:
        if shelf.slug == "other":
            continue
        if any(k in name for k in shelf.names):
            return shelf
    for shelf in lib.shelves:
        if shelf.slug == "other":
            continue
        if ext and ext in shelf.extensions:
            return shelf
    return None


def ollama_post(route: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(
        OLLAMA + route,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ensure_ollama():
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2)
    except Exception:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            try:
                urllib.request.urlopen(OLLAMA + "/api/tags", timeout=1)
                return
            except Exception:
                time.sleep(0.25)
        raise SystemExit("Ollama is not running")


def stop_model():
    try:
        ollama_post("/api/generate", {"model": MODEL, "keep_alive": 0, "prompt": ""}, timeout=30)
    except Exception:
        subprocess.run(["ollama", "stop", MODEL], check=False, capture_output=True)


def classify(filename: str, slugs: list[str]) -> str:
    menu = ", ".join(slugs)
    prompt = (
        f"Pick exactly one shelf from: {menu}\n"
        "Use the filename only. Reply with JSON only: {\"shelf\":\"other\"}\n"
        f"filename: {filename}"
    )
    data = ollama_post(
        "/api/chat",
        {
            "model": MODEL,
            "stream": False,
            "keep_alive": "2m",
            "options": {"num_ctx": 256, "temperature": 0},
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    raw = ((data.get("message") or {}).get("content") or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    blob = raw[start : end + 1] if start >= 0 and end > start else raw
    try:
        slug = str(json.loads(blob).get("shelf") or "").strip().lower()
    except json.JSONDecodeError:
        slug = ""
    if slug in slugs:
        return slug
    low = raw.lower()
    for s in slugs:
        if s in low:
            return s
    return "other"


def unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not cand.exists():
            return cand
        n += 1


def plan(lib: Library, use_model: bool) -> list[dict]:
    rows = []
    leftovers = []
    leave_set = {p.resolve() for p in lib.leave if p.exists()}
    for inbox in lib.inboxes:
        if not inbox.exists():
            continue
        if inbox.resolve() in leave_set:
            continue
        for path in iter_files(inbox):
            # already sitting on a shelf folder inside the inbox
            if path.parent.resolve() in {s.path.resolve() for s in lib.shelves}:
                continue
            shelf = match_rule(path, lib)
            row = {
                "path": str(path),
                "name": path.name,
                "inbox": str(inbox),
            }
            if shelf:
                dest = unique_dest(shelf.path / path.name)
                row.update(shelf=shelf.slug, dest=str(dest), via="index")
                rows.append(row)
            else:
                leftovers.append(row)

    if leftovers and use_model:
        ensure_ollama()
        slugs = lib.slugs()
        try:
            for i, row in enumerate(leftovers, 1):
                slug = classify(row["name"], slugs)
                shelf = lib.by_slug(slug) or lib.by_slug("other")
                dest = unique_dest(shelf.path / Path(row["name"]).name)
                row.update(shelf=shelf.slug, dest=str(dest), via="model")
                rows.append(row)
                print(f"[{i}/{len(leftovers)}] {shelf.slug:12} {row['name']}", file=sys.stderr)
        finally:
            stop_model()
    else:
        other = lib.by_slug("other")
        for row in leftovers:
            dest = unique_dest(other.path / Path(row["name"]).name)
            row.update(shelf="other", dest=str(dest), via="other")
            rows.append(row)
    return rows


def apply(rows: list[dict]) -> int:
    n = 0
    log = []
    for row in rows:
        src, dest = Path(row["path"]), Path(row["dest"])
        if src.resolve() == dest.resolve() or not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = unique_dest(dest)
        shutil.move(str(src), str(dest))
        log.append({"from": str(src), "to": str(dest), "shelf": row["shelf"]})
        n += 1
    if log:
        APP.mkdir(parents=True, exist_ok=True)
        (APP / "last-run.json").write_text(json.dumps({"moves": log, "t": int(time.time())}, indent=2))
    return n


def undo() -> int:
    p = APP / "last-run.json"
    if not p.exists():
        print("no last-run.json", file=sys.stderr)
        return 0
    data = json.loads(p.read_text())
    n = 0
    for mv in reversed(data.get("moves") or []):
        src, dest = Path(mv["to"]), Path(mv["from"])
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(unique_dest(dest)))
            n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description="Librarian: file by library.md")
    parser.add_argument("--index", default="", help="Path to library.md (default: user copy)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--undo", action="store_true")
    parser.add_argument("--print-index", action="store_true")
    args = parser.parse_args()

    bundled = Path(__file__).resolve().parent / "library.md"
    index = Path(args.index).expanduser() if args.index else ensure_user_index()
    # keep bundled next to script as default template
    if not bundled.exists():
        bundled.write_text(INDEX_USER.read_text() if INDEX_USER.exists() else "")

    if args.print_index:
        print(index.read_text())
        print(f"\n# index: {index}", file=sys.stderr)
        return

    if args.undo:
        n = undo()
        print(json.dumps({"undone": n}))
        return

    lib = load_library(index)
    rows = plan(lib, use_model=not args.no_model)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["shelf"]] = counts.get(r["shelf"], 0) + 1
    moved = apply(rows) if args.apply else 0
    print(
        json.dumps(
            {
                "index": str(index),
                "apply": bool(args.apply),
                "moved": moved,
                "count": len(rows),
                "by_shelf": counts,
                "items": rows,
            },
            indent=2,
        )
    )
    if not args.apply:
        print(f"Dry run using {index}. Pass --apply to move. --undo reverses last apply.", file=sys.stderr)


if __name__ == "__main__":
    main()
