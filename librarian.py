#!/usr/bin/env python3
"""Filename librarian driven by library.md.

Rules in the index file first. Qwen 3.5 0.8B only chooses among those
shelf names for leftovers, then unloads. Nothing moves without --apply.
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,78}[a-z0-9]$")


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


def unique_dest(dest: Path, src: Path | None = None) -> Path:
    if src is not None and dest.exists() and dest.resolve() == src.resolve():
        return dest
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
        if not cand.exists():
            return cand
        if src is not None and cand.resolve() == src.resolve():
            return cand
        n += 1


JUNK_BITS = (
    "riverside_",
    "riverside ",
    "copy_of_",
    "copy of ",
    "copy_of ",
    "gokul_bala's studio",
    "gokul bala's studio",
    "gokul_bala",
    "backup-video",
    "copy-of-",
    "copy-of ",
)
COPY_MARK = re.compile(r"\s*\(\d+\)\s*")
HEXISH = re.compile(r"^[0-9a-f]{8,}$", re.I)
DIGITS = re.compile(r"^\d{8,}$")


def slugify(stem: str) -> str:
    s = stem.lower()
    for bit in JUNK_BITS:
        s = s.replace(bit, " ")
    s = COPY_MARK.sub(" ", s)
    s = s.replace("—", " ").replace("–", " ").replace("_", " ").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:70]


def is_messy(name: str) -> bool:
    stem = Path(name).stem
    low = name.lower()
    if any(b.strip() in low for b in ("riverside", "copy of", "copy_of", "studio")):
        return True
    if COPY_MARK.search(name):
        return True
    if HEXISH.match(stem) or DIGITS.match(stem):
        return True
    if " " in name or name != name.strip():
        return True
    if len(stem) > 60:
        return True
    return False


def resume_name(name: str) -> str | None:
    ext = Path(name).suffix.lower() or ".pdf"
    s = slugify(Path(name).stem)
    for drop in ("gokul", "bala", "resume", "cv"):
        s = re.sub(rf"(^|-){drop}(-|$)", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if s:
        return f"gokul-bala-resume-{s}{ext}"
    return f"gokul-bala-resume{ext}"


def rule_rename(name: str, shelf: str) -> str | None:
    ext = Path(name).suffix.lower()
    if not ext:
        return None
    if shelf == "resumes":
        return resume_name(name)
    stem = slugify(Path(name).stem)
    if not stem:
        return None
    if shelf == "video" and stem.startswith("whatsapp-video"):
        stem = stem.replace("whatsapp-video", "whatsapp")
    if stem.isdigit():
        stem = f"{shelf}-{stem}"
    proposed = f"{stem}{ext}"
    if proposed.lower() == name.lower() and not is_messy(name):
        return None
    if not SAFE_NAME.match(stem):
        return None
    return proposed


def model_rename(name: str, shelf: str) -> str | None:
    ext = Path(name).suffix.lower()
    prompt = (
        f"Shelf: {shelf}\n"
        f"Current filename: {name}\n"
        "Give a short lowercase filename. Keep the same extension. "
        "Use hyphens not spaces. No folders. JSON only: {\"name\":\"example.pdf\"}"
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
    blob = raw[start : end + 1] if start >= 0 and end > start else ""
    try:
        proposed = str(json.loads(blob).get("name") or "").strip()
    except json.JSONDecodeError:
        return None
    p = Path(proposed)
    if p.suffix.lower() != ext:
        proposed = p.stem + ext
        p = Path(proposed)
    stem = slugify(p.stem)
    if not stem or not SAFE_NAME.match(stem):
        return None
    return f"{stem}{ext}"


def attach_new_name(row: dict, use_model: bool) -> None:
    name = row["name"]
    shelf = row["shelf"]
    dest = Path(row["dest"])
    new = rule_rename(name, shelf)
    if not new and use_model and is_messy(name):
        new = model_rename(name, shelf)
        if new:
            row["rename_via"] = "model"
    elif new:
        row["rename_via"] = "rule"
    if not new or new.lower() == name.lower():
        return
    row["new_name"] = new
    row["dest"] = str(unique_dest(dest.with_name(new)))


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

    model_on = False
    if leftovers and use_model:
        ensure_ollama()
        model_on = True
        slugs = lib.slugs()
        for i, row in enumerate(leftovers, 1):
            slug = classify(row["name"], slugs)
            shelf = lib.by_slug(slug) or lib.by_slug("other")
            dest = unique_dest(shelf.path / Path(row["name"]).name)
            row.update(shelf=shelf.slug, dest=str(dest), via="model")
            rows.append(row)
            print(f"[{i}/{len(leftovers)}] {shelf.slug:12} {row['name']}", file=sys.stderr)
    else:
        other = lib.by_slug("other")
        for row in leftovers:
            dest = unique_dest(other.path / Path(row["name"]).name)
            row.update(shelf="other", dest=str(dest), via="other")
            rows.append(row)
    return rows, model_on


def existing_shelf_rows(lib: Library) -> list[dict]:
    rows = []
    for shelf in lib.shelves:
        if not shelf.path.is_dir():
            continue
        for path in iter_files(shelf.path):
            if shelf.slug != "resumes" and not is_messy(path.name):
                continue
            rows.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "inbox": str(shelf.path),
                    "shelf": shelf.slug,
                    "dest": str(path),
                    "via": "rename-existing",
                }
            )
    return rows


def attach_renames(rows: list[dict], use_model: bool, model_already_on: bool) -> bool:
    started = model_already_on
    try:
        for row in rows:
            if not row.get("shelf"):
                continue
            new = rule_rename(row["name"], row["shelf"])
            if not new and use_model and is_messy(row["name"]):
                if not started:
                    ensure_ollama()
                    started = True
                new = model_rename(row["name"], row["shelf"])
                if new:
                    row["rename_via"] = "model"
            elif new:
                row["rename_via"] = "rule"
            if not new or new.lower() == row["name"].lower():
                continue
            dest_dir = Path(row["dest"]).parent
            row["new_name"] = new
            row["dest"] = str(unique_dest(dest_dir / new, src=Path(row["path"])))
    finally:
        if started:
            stop_model()
    return started


def apply(rows: list[dict]) -> int:
    n = 0
    log = []
    for row in rows:
        src, dest = Path(row["path"]), Path(row["dest"])
        if src.resolve() == dest.resolve() or not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = unique_dest(dest, src=src)
        if src.resolve() == dest.resolve() and src.name == dest.name:
            continue
        if src.name.lower() == dest.name.lower() and src.parent == dest.parent:
            tmp = unique_dest(src.with_name(src.stem + "-case" + src.suffix))
            src.rename(tmp)
            src = tmp
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
    rows, model_on = plan(lib, use_model=not args.no_model)
    rows.extend(existing_shelf_rows(lib))
    attach_renames(rows, use_model=not args.no_model, model_already_on=model_on)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["shelf"]] = counts.get(r["shelf"], 0) + 1
    moved = apply(rows) if args.apply else 0
    renamed = sum(1 for r in rows if r.get("new_name"))
    print(
        json.dumps(
            {
                "index": str(index),
                "apply": bool(args.apply),
                "moved": moved,
                "renamed": renamed,
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
