# s-py-editor

A terminal-based Python code editor with syntax highlighting, auto-completion,
a file sidebar, and the ability to run code (selections, loops, or whole files)
without leaving the editor.

## Install

```bash
pipx install s-py-editor
```

Or with pip:

```bash
pip install s-py-editor
```

## Usage

```bash
spy                  # start with empty buffer
spy myfile.py        # open a file
spy .                # start in a directory (sidebar shows it)
```

## Key bindings

| Key | Action |
|-----|--------|
| `F5` / `Ctrl+R` | Run selection → enclosing loop → whole file |
| `Ctrl+S` | Save the buffer |
| `Ctrl+A` | Select all |
| `Ctrl+B` | Toggle sidebar focus |
| `Ctrl+E` | Toggle output popup |
| `Ctrl+X` | Stop running code |
| `Ctrl+Q` / `Ctrl+C` | Quit |

## Sidebar

When the sidebar is focused (`Ctrl+B`):

- `↑` / `↓` — move selection
- `→` / `←` — expand / collapse folders
- `Enter` — open file or toggle folder
- `Ctrl+R` — refresh directory listing
- `Tab` — focus the editor

## Requirements

- Python 3.9+
- `prompt_toolkit>=3.0`
- `pygments>=2.0`

## License

MIT (or your choice — update this line)