# Librarian

A filename librarian. The map is a markdown file. The model may only put
things on shelves that exist in that file.

This is **not** Browser Management System (Helium tabs / RAM).

## The index

Edit `~/Library/Application Support/Librarian/library.md` (copied from
`library.md` in this repo on first run).

```markdown
Scan: ~/Downloads, ~/Desktop
Leave: ~/Pictures/Screenshots

## resumes
Path: ~/Desktop/resumes
Names: resume cv

## video
Path: ~/Downloads/video
Files: .mp4 .mov
```

- **Files:** extensions → rule, no model
- **Names:** substring in the filename → rule, no model
- Anything else: Qwen 3.5 0.8B picks **one of the `##` headings**, then unloads

`other` is the inbox for “I don’t know.” The model cannot invent a new heading.

## Usage

```bash
python3 librarian.py              # dry run
python3 librarian.py --apply      # move files
python3 librarian.py --undo       # reverse last apply
python3 librarian.py --print-index
python3 librarian.py --no-model   # rules only
```

Needs [Ollama](https://ollama.com) and `qwen3.5:0.8b` only when leftovers exist.

## License

MIT
