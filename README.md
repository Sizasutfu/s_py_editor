# s-py-editor

A terminal-based Python code editor with syntax highlighting, auto-completion,
line numbers, a file sidebar, and a built-in runner. Run a selection, a loop,
or a whole file without leaving the editor. It also works well on phones
(Termux).

## Install

```bash
pipx install s-py-editor
```

Or with pip:

```bash
pip install s-py-editor
```

For smarter, context-aware completions, install [Jedi](https://github.com/davidhalter/jedi) too (optional):

```bash
pip install jedi
```

## Usage

```bash
spy                  # start with an empty buffer
spy myfile.py        # open a file
```

The sidebar always lists the directory you launched `spy` from.

## Key bindings

| Key | Action |
|-----|--------|
| `F5` / `Ctrl+R` | Run selection → enclosing loop → whole file |
| `Ctrl+S` | Save (asks for a filename if the buffer is untitled) |
| `Ctrl+A` | Select all |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo |
| `Tab` / `Shift+Tab` | Indent / dedent (`Tab` also accepts a completion) |
| `Ctrl+B` | Show / hide the sidebar |
| `Ctrl+E` | Show / hide the output panel |
| `Ctrl+X` | Stop running code |
| `Ctrl+T` | Toggle mouse support |
| `Ctrl+Q` / `Ctrl+C` | Quit |

If the buffer has unsaved changes, quitting or opening another file asks you to
repeat the action to confirm.

## Running code

`F5` picks what to run, in this order:

1. **Selection**, if you have text selected
2. **Enclosing loop**, if the cursor is inside a `for` / `while`
3. **Whole file**

Code runs in a separate Python process, so:

- `Ctrl+X` stops it for real, even during `time.sleep()` or a tight loop
- `sys.exit()`, crashes and `input()` can't affect the editor (`input()` gets EOF)
- Every run starts fresh, so variables from earlier runs don't carry over
- Tracebacks show your real file name and the correct line numbers, even when
  running a selection or loop

## Output panel

Output appears in a panel docked above the status bar. It uses the full terminal
width, wraps long lines, and resizes with the terminal (for example when a
soft keyboard opens). It keeps the last 100,000 characters.

| Key | Action |
|-----|--------|
| `Alt+↑` / `Alt+↓` | Scroll one line (`Ctrl+↑` / `Ctrl+↓` also work) |
| `Alt+PgUp` / `Alt+PgDn` | Scroll one page |
| `Alt+Home` | Jump to the oldest output |
| `Alt+End` | Jump to the newest output and follow it |
| Swipe / mouse wheel over the panel | Scroll 3 lines (mouse mode must be on) |

While you're scrolled up, the header shows your position, and the view holds
still even if the script keeps printing. Each new run jumps back to the newest
output.

## Sidebar

When the sidebar is focused (`Ctrl+B`):

- `↑` / `↓`: move selection
- `→` / `←`: expand / collapse folders
- `Enter`: open file or toggle folder
- `Ctrl+R`: refresh directory listing
- `Tab`: focus the editor

## Saving

- Saves are atomic (a temp file is swapped in), so a crash mid-save can't wipe your file
- `.py` is only added when the filename has no extension (`notes.txt` stays `notes.txt`)
- Saving over an existing file asks you to press `Enter` a second time to confirm
- On a phone, the filename prompt appears as a bar on the bottom line, right above the keyboard

## Termux / Android

- **Mouse support starts off on Termux**, because mouse reporting stops taps from
  reopening the soft keyboard. Press `Ctrl+T` to turn it on (needed for swipe
  scrolling) and off again.
- For `Alt` shortcuts, tap **ALT** in the extra-keys row, then the key.
- Add `CTRL`, `ESC`, `ALT` and a keyboard toggle to your extra keys in
  `~/.termux/termux.properties`, then run `termux-reload-settings`:

```
extra-keys = [['ESC','TAB','CTRL','ALT','KEYBOARD','UP','DOWN','LEFT','RIGHT']]
```

## Requirements

- Python 3.9+
- `prompt_toolkit>=3.0`
- `pygments>=2.0`
- `jedi>=0.18` (optional, for better completions)

## License

MIT — see [LICENSE](https://github.com/Sizasutfu/s_py_editor/blob/main/LICENSE) for details.