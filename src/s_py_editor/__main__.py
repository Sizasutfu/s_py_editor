import sys
import os
import ast
import codecs
import subprocess
import tempfile
import textwrap
import threading
import time
import keyword
import builtins
from collections import deque

try:
    import jedi
except ImportError:
    jedi = None

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import (
    WordCompleter, Completer, Completion,
    FuzzyCompleter, ThreadedCompleter
)
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    HSplit, VSplit, Window, FloatContainer, Float, ConditionalContainer
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.margins import NumberedMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.filters import Condition, has_focus, has_selection
from prompt_toolkit.selection import SelectionState
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea, Frame
from pygments.lexers.python import PythonLexer


# ---------- Config ----------

FILE_PATH = sys.argv[1] if len(sys.argv) > 1 else None
ROOT_DIR = os.getcwd()
SIDEBAR_WIDTH = 32
OUTPUT_WIDTH = 60
OUTPUT_HEIGHT = 15
INDENT = '    '

# Auto-generate all keywords and built-in functions
PYTHON_WORDS = set(keyword.kwlist + dir(builtins))

PYTHON_WORDS.update([
    'print()', 'print("")', 'print(f"")',
    'if __name__ == "__main__":',
    'def main():',
    '__init__', '__str__', '__repr__', '__dict__',
    'import os', 'import sys', 'import math', 'import time',
    'import json', 'import re', 'from typing import',
])

PYTHON_WORDS = sorted(list(PYTHON_WORDS))


# ---------- Smart Completer (Jedi) ----------

_jedi_error_reported = {'v': False}


class JediCompleter(Completer):
    def get_completions(self, document, complete_event):
        if jedi is None:
            return
        try:
            # Jedi 0.18+: line/column go on .complete(), not on Script()
            script = jedi.Script(document.text)
            completions = script.complete(
                document.cursor_position_row + 1,
                document.cursor_position_col,
            )
            for c in completions:
                yield Completion(
                    c.name,
                    start_position=-c.get_completion_prefix_length(),
                )
        except Exception as e:
            # Report once instead of failing silently forever
            if not _jedi_error_reported['v']:
                _jedi_error_reported['v'] = True
                status['message'] = f'Jedi error: {type(e).__name__}: {e}'


class DedupedCompleter(Completer):
    """Jedi first, then the word list, skipping anything already offered."""

    def __init__(self, completers):
        self.completers = completers

    def get_completions(self, document, complete_event):
        seen = set()
        for completer in self.completers:
            for c in completer.get_completions(document, complete_event):
                if c.text in seen:
                    continue
                seen.add(c.text)
                yield c


base_word_completer = FuzzyCompleter(WordCompleter(PYTHON_WORDS, ignore_case=True))
smart_completer = DedupedCompleter([JediCompleter(), base_word_completer])
threaded_completer = ThreadedCompleter(smart_completer)


# ---------- State ----------

status = {'message': 'Ready'}
dirty = {'v': False}
pending = {'target': None, 'overwrite': None}
dialog_state = {'active': False}


def invalidate():
    try:
        app.invalidate()
    except Exception:
        pass


class OutputBuffer:
    MAX_CHARS = 100_000
    REDRAW_INTERVAL = 0.05  # seconds; keeps print-heavy loops from redrawing per write

    def __init__(self):
        self._chunks = deque()
        self._size = 0
        self._lock = threading.Lock()
        self._last_redraw = 0.0
        self._timer_pending = False

    def clear(self):
        with self._lock:
            self._chunks.clear()
            self._size = 0

    def write(self, s):
        if not s:
            return 0
        with self._lock:
            self._chunks.append(s)
            self._size += len(s)
            while self._size > self.MAX_CHARS and len(self._chunks) > 1:
                self._size -= len(self._chunks.popleft())
        self._request_redraw()
        return len(s)

    def _request_redraw(self):
        with self._lock:
            now = time.monotonic()
            wait = self.REDRAW_INTERVAL - (now - self._last_redraw)
            if wait <= 0:
                self._last_redraw = now
                immediate = True
            else:
                immediate = False
                if self._timer_pending:
                    return
                self._timer_pending = True
        if immediate:
            invalidate()
        else:
            t = threading.Timer(wait, self._flush_redraw)
            t.daemon = True
            t.start()

    def _flush_redraw(self):
        with self._lock:
            self._timer_pending = False
            self._last_redraw = time.monotonic()
        invalidate()

    def flush(self):
        pass

    def getvalue(self):
        with self._lock:
            return ''.join(self._chunks)


exec_state = {
    'sink': OutputBuffer(),
    'label': '',
    'running': False,
    'proc': None,
    'stopped': False,
    'show_output': False,
}

sidebar_state = {'visible': False}

tree_state = {
    'root': ROOT_DIR,
    'expanded': set(),
    'selected': 0,
    'items': [],
}


# ---------- File helpers ----------

def load_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    buffer.reset(Document(text, 0))
    dirty['v'] = False
    pending['target'] = None
    pending['overwrite'] = None


def confirm_discard(target):
    if not dirty['v'] or pending['target'] == target:
        pending['target'] = None
        return True
    pending['target'] = target
    status['message'] = 'Unsaved changes! Repeat to discard (Ctrl+S to save)'
    return False


# ---------- Buffer ----------

buffer = Buffer(
    completer=threaded_completer,
    complete_while_typing=True,
    name='main',
)

if FILE_PATH and os.path.exists(FILE_PATH):
    try:
        load_file(FILE_PATH)
    except Exception as e:
        status['message'] = f'Open failed: {e}'


def _on_text_changed(_buf):
    dirty['v'] = True
    pending['target'] = None

buffer.on_text_changed += _on_text_changed


def rebuild_tree():
    items = []

    def walk(path, depth):
        try:
            entries = os.listdir(path)
        except OSError:
            return
        entries = [e for e in entries if not e.startswith('.')]
        entries.sort(key=lambda x: (not os.path.isdir(os.path.join(path, x)), x.lower()))
        for name in entries:
            full = os.path.join(path, name)
            is_dir = os.path.isdir(full)
            is_exp = full in tree_state['expanded']
            items.append((full, depth, is_dir, is_exp))
            if is_dir and is_exp:
                walk(full, depth + 1)

    walk(tree_state['root'], 0)
    tree_state['items'] = items
    if tree_state['selected'] >= len(items):
        tree_state['selected'] = max(0, len(items) - 1)

rebuild_tree()


# ---------- Rendering ----------

def get_status():
    path = FILE_PATH or '<untitled>'
    mod = '*' if dirty['v'] else ''
    run = ' [RUNNING]' if exec_state['running'] else ''
    return (
        f' {path}{mod}   |   {status["message"]}{run}   |   '
        f'Ctrl+B sidebar  Ctrl+A sel-all  Ctrl+Z undo  F5 run  Ctrl+S save  '
        f'Ctrl+X stop  Ctrl+E output  Ctrl+Q quit '
    )


def get_sidebar_text():
    result = []
    for i, (path, depth, is_dir, is_exp) in enumerate(tree_state['items']):
        name = os.path.basename(path) or path
        indent = '  ' * depth
        if is_dir:
            arrow = '▼ ' if is_exp else '▶ '
            line = f'{indent}{arrow}{name}/'
        else:
            line = f'{indent}  {name}'
        style = 'class:sidebar.selected' if i == tree_state['selected'] else 'class:sidebar'
        padded = line[:SIDEBAR_WIDTH].ljust(SIDEBAR_WIDTH)
        result.append((style, padded + '\n'))
    if not result:
        result.append(('class:sidebar', '(empty)'))
    return result


def get_sidebar_cursor():
    return Point(0, tree_state['selected'])


def get_output_text():
    if not exec_state['label']:
        return '(no output yet — press F5 to run)'
    state = 'running (Ctrl+X stops)' if exec_state['running'] else 'done'
    header = f'-- {exec_state["label"]}: {state} --'

    text = exec_state['sink'].getvalue()
    rows = []
    for line in text.split('\n'):
        line = line.rstrip('\r').expandtabs(4)
        rows.extend(line[i:i + OUTPUT_WIDTH] for i in range(0, max(len(line), 1), OUTPUT_WIDTH))
    if rows and rows[-1] == '' and text.endswith('\n'):
        rows.pop()
    tail = rows[-(OUTPUT_HEIGHT - 1):]
    return header + '\n' + '\n'.join(tail)


# ---------- Loop detection ----------

def enclosing_loop(text, cursor_index):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    lines = text.split('\n')
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)):
            start = starts[node.lineno - 1]
            end = starts[node.end_lineno]
            if start <= cursor_index < end:
                best = node

    if best is None:
        return None
    segment = '\n'.join(lines[best.lineno - 1:best.end_lineno])
    return textwrap.dedent(segment), best.lineno


# ---------- Code execution (subprocess) ----------

# Tiny launcher executed by the child interpreter. It compiles the code under the
# real file name (so tracebacks look right), sets __file__, and strips its own
# frame from tracebacks.
LAUNCHER = r'''
import sys, linecache, traceback
path, display, filevar = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, 'r', encoding='utf-8') as f:
    src = f.read()
linecache.cache[display] = (len(src), None, src.splitlines(True), display)
g = {'__name__': '__main__'}
if filevar:
    g['__file__'] = filevar
sys.argv = [filevar or display]
try:
    exec(compile(src, display, 'exec'), g)
except SystemExit:
    raise
except BaseException:
    et, ev, tb = sys.exc_info()
    traceback.print_exception(et, ev, tb.tb_next)
    sys.exit(1)
'''


def run_code(code, label, first_line=1):
    if exec_state['running']:
        status['message'] = 'Already running — Ctrl+X to stop'
        return

    sink = exec_state['sink']
    sink.clear()
    exec_state['label'] = label
    exec_state['stopped'] = False
    exec_state['show_output'] = True

    # Pad with blank lines so traceback line numbers match the editor
    source = '\n' * (first_line - 1) + code

    if FILE_PATH:
        display = os.path.abspath(FILE_PATH)
        filevar = display
        cwd = os.path.dirname(display) or ROOT_DIR
    else:
        display = '<untitled>'
        filevar = ''
        cwd = ROOT_DIR

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', suffix='.py', delete=False
        )
        with tmp:
            tmp.write(source)
        tmp_path = tmp.name

        env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
        proc = subprocess.Popen(
            [sys.executable, '-u', '-c', LAUNCHER, tmp_path, display, filevar],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            bufsize=0,
        )
    except Exception as e:
        status['message'] = f'Could not start: {e}'
        return

    exec_state['proc'] = proc
    exec_state['running'] = True
    status['message'] = f'Running {label}...'

    def worker():
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                sink.write(decoder.decode(chunk))
            tail = decoder.decode(b'', final=True)
            if tail:
                sink.write(tail)
        except Exception as e:
            sink.write(f'\n[output error: {e}]\n')
        finally:
            rc = proc.wait()
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            if exec_state['stopped']:
                sink.write('\n[stopped]\n')
                outcome = 'stopped'
            elif rc == 0:
                outcome = 'finished'
            else:
                outcome = f'failed (exit {rc})'
            exec_state['running'] = False
            exec_state['proc'] = None
            status['message'] = f'{label} {outcome}'
            invalidate()

    threading.Thread(target=worker, daemon=True).start()


def request_stop():
    proc = exec_state['proc']
    if exec_state['running'] and proc is not None and proc.poll() is None:
        exec_state['stopped'] = True
        try:
            proc.terminate()
        except OSError:
            pass

        def _force_kill():
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

        t = threading.Timer(2.0, _force_kill)
        t.daemon = True
        t.start()
        status['message'] = 'Stopping...'
    else:
        status['message'] = 'Nothing running'


# ---------- Controls ----------

buffer_control = BufferControl(buffer=buffer, lexer=PygmentsLexer(PythonLexer))
editor_window = Window(
    buffer_control,
    wrap_lines=False,
    left_margins=[NumberedMargin()],
)

output_window = Window(
    FormattedTextControl(get_output_text),
    style='class:output',
    wrap_lines=False,
)


# ---------- Save Dialog ----------

def _actual_save():
    global FILE_PATH
    if not FILE_PATH:
        return
    tmp_path = FILE_PATH + '.tmp~'
    try:
        # Atomic save: write a temp file, then swap it in
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(buffer.text)
        os.replace(tmp_path, FILE_PATH)
        dirty['v'] = False
        pending['target'] = None
        status['message'] = f'Saved to {FILE_PATH}'
        rebuild_tree()
    except Exception as e:
        status['message'] = f'Save failed: {e}'
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def do_save_with_name(buf):
    global FILE_PATH
    name = buf.text.strip()
    if not name:
        name = 'untitled.py'
    # Only add .py when there's no extension at all (so notes.txt stays notes.txt)
    if not os.path.splitext(name)[1]:
        name += '.py'

    # Don't silently overwrite an existing file
    if os.path.exists(name) and pending['overwrite'] != name:
        pending['overwrite'] = name
        status['message'] = f'{name} exists — press Enter again to overwrite, Esc to cancel'
        invalidate()
        return True  # keep the dialog open and the text in place

    pending['overwrite'] = None
    FILE_PATH = name
    dialog_state['active'] = False
    _actual_save()
    app.layout.focus(buffer_control)
    invalidate()
    return True


filename_input = TextArea(
    multiline=False,
    accept_handler=do_save_with_name,
    height=1,
    width=40,
    focusable=True,
)

save_dialog = Frame(
    filename_input,
    title="Save As",
)


# ---------- Sidebar key bindings ----------

sidebar_kb = KeyBindings()

@sidebar_kb.add('up')
def _sb_up(event):
    if tree_state['selected'] > 0:
        tree_state['selected'] -= 1
    event.app.invalidate()

@sidebar_kb.add('down')
def _sb_down(event):
    if tree_state['selected'] < len(tree_state['items']) - 1:
        tree_state['selected'] += 1
    event.app.invalidate()

@sidebar_kb.add('enter')
def _sb_enter(event):
    global FILE_PATH
    if not tree_state['items']:
        return
    path, depth, is_dir, is_exp = tree_state['items'][tree_state['selected']]
    if is_dir:
        if is_exp:
            tree_state['expanded'].discard(path)
        else:
            tree_state['expanded'].add(path)
        rebuild_tree()
    else:
        if not confirm_discard(path):
            event.app.invalidate()
            return
        try:
            load_file(path)
            FILE_PATH = path
            status['message'] = f'Opened {os.path.basename(path)}'
            event.app.layout.focus(buffer_control)
        except Exception as e:
            status['message'] = f'Open failed: {e}'
    event.app.invalidate()

@sidebar_kb.add('right')
def _sb_right(event):
    if not tree_state['items']:
        return
    path, depth, is_dir, is_exp = tree_state['items'][tree_state['selected']]
    if is_dir and not is_exp:
        tree_state['expanded'].add(path)
        rebuild_tree()
    elif is_dir and is_exp:
        if tree_state['selected'] + 1 < len(tree_state['items']):
            nxt = tree_state['items'][tree_state['selected'] + 1]
            if nxt[1] == depth + 1:
                tree_state['selected'] += 1
    event.app.invalidate()

@sidebar_kb.add('left')
def _sb_left(event):
    if not tree_state['items']:
        return
    path, depth, is_dir, is_exp = tree_state['items'][tree_state['selected']]
    if is_dir and is_exp:
        tree_state['expanded'].discard(path)
        rebuild_tree()
    elif depth > 0:
        parent = os.path.dirname(path)
        for i, (p, _, _, _) in enumerate(tree_state['items']):
            if p == parent:
                tree_state['selected'] = i
                break
    event.app.invalidate()

@sidebar_kb.add('tab')
def _sb_tab(event):
    event.app.layout.focus(buffer_control)
    status['message'] = 'Editor focused'
    event.app.invalidate()

@sidebar_kb.add('c-r')
def _sb_refresh(event):
    rebuild_tree()
    status['message'] = 'Sidebar refreshed'
    event.app.invalidate()


sidebar_control = FormattedTextControl(
    get_sidebar_text,
    focusable=True,
    show_cursor=False,
    get_cursor_position=get_sidebar_cursor,
    key_bindings=sidebar_kb,
)
sidebar_window = Window(
    sidebar_control,
    width=SIDEBAR_WIDTH,
    style='class:sidebar',
    wrap_lines=False,
)


# ---------- Layout ----------

body = VSplit([
    ConditionalContainer(
        sidebar_window,
        filter=Condition(lambda: sidebar_state['visible']),
    ),
    HSplit([
        editor_window,
        Window(FormattedTextControl(get_status), height=1, style='reverse'),
    ]),
])

root_container = FloatContainer(
    content=body,
    floats=[
        Float(
            xcursor=True,
            ycursor=True,
            content=CompletionsMenu(max_height=15, scroll_offset=1),
        ),
        Float(
            content=ConditionalContainer(
                output_window,
                filter=Condition(lambda: exec_state['show_output']),
            ),
            top=2, right=2, width=OUTPUT_WIDTH, height=OUTPUT_HEIGHT,
        ),
        Float(
            content=ConditionalContainer(
                save_dialog,
                filter=Condition(lambda: dialog_state['active']),
            ),
            top=5, left=5,
        ),
    ],
)


# ---------- Global key bindings ----------

kb = KeyBindings()
not_sidebar = ~has_focus(sidebar_control)
in_editor = has_focus(buffer_control)

# Filter to prevent global shortcuts from firing while the dialog is open
dialog_inactive = ~Condition(lambda: dialog_state['active'])

@kb.add('c-b', filter=dialog_inactive)
def toggle_sidebar(event):
    if sidebar_state['visible']:
        event.app.layout.focus(buffer_control)
        sidebar_state['visible'] = False
        status['message'] = 'Sidebar hidden (Ctrl+B to show)'
    else:
        sidebar_state['visible'] = True
        event.app.layout.focus(sidebar_control)
        status['message'] = 'Sidebar — arrows/Enter, Tab to edit, Ctrl+B hide'
    event.app.invalidate()

@kb.add('c-a', filter=has_focus(buffer_control))
def select_all(event):
    if buffer.text:
        buffer.selection_state = SelectionState(original_cursor_position=0)
        buffer.cursor_position = len(buffer.text)
        status['message'] = 'Selected all — Backspace to clear, F5 to run'
    else:
        status['message'] = 'Buffer is empty'
    event.app.invalidate()

@kb.add('backspace', filter=in_editor & has_selection)
@kb.add('delete', filter=in_editor & has_selection)
def delete_selection(event):
    event.current_buffer.cut_selection()

@kb.add('tab', filter=in_editor)
def indent_or_complete(event):
    b = event.current_buffer
    if b.complete_state:
        current = b.complete_state.current_completion
        if current:
            b.apply_completion(current)
            if current.text and current.text[-1] in (')', '"', "'"):
                b.cursor_left()
        else:
            b.complete_next()
    else:
        b.insert_text(INDENT)

@kb.add('s-tab', filter=in_editor)
def dedent_line(event):
    b = event.current_buffer
    doc = b.document
    line = doc.current_line
    n = min(len(INDENT), len(line) - len(line.lstrip(' ')))
    if n:
        pos = b.cursor_position
        start = pos - doc.cursor_position_col
        b.save_to_undo_stack()
        b.text = b.text[:start] + b.text[start + n:]
        b.cursor_position = max(start, pos - n)

@kb.add('enter', filter=in_editor)
def auto_indent(event):
    b = event.current_buffer
    before = b.document.current_line_before_cursor
    b.newline(copy_margin=True)
    if before.rstrip().endswith(':'):
        b.insert_text(INDENT)

@kb.add('c-z', filter=in_editor)
def undo(event):
    event.current_buffer.undo()
    status['message'] = 'Undo'

@kb.add('c-y', filter=in_editor)
def redo(event):
    event.current_buffer.redo()
    status['message'] = 'Redo'


@kb.add('escape', filter=Condition(lambda: dialog_state['active']), eager=True)
def cancel_save_dialog(event):
    dialog_state['active'] = False
    pending['overwrite'] = None
    event.app.layout.focus(buffer_control)
    status['message'] = 'Save cancelled'
    event.app.invalidate()


@kb.add('c-s', filter=dialog_inactive)
def save(event):
    global FILE_PATH
    if FILE_PATH is None:
        # Show the dialog and focus the input immediately (no deferred focus).
        dialog_state['active'] = True
        pending['overwrite'] = None
        default_name = 'untitled.py'
        filename_input.buffer.reset(Document(default_name, len(default_name)))
        event.app.layout.focus(filename_input.window)
        status['message'] = 'Enter filename (Enter to save, Esc to cancel)'
    else:
        _actual_save()
    event.app.invalidate()


@kb.add('f5', filter=dialog_inactive)
def _do_run(event):
    doc = buffer.document
    sel = doc.selection_range()
    first_line = 1
    if doc.selection and sel[0] != sel[1]:
        start, end = sel
        row, col = doc.translate_index_to_position(start)
        start -= col
        code = textwrap.dedent(doc.text[start:end])
        first_line = row + 1
        label = 'selection'
    else:
        loop = enclosing_loop(doc.text, doc.cursor_position)
        if loop:
            code, first_line = loop
            label = 'enclosing loop'
        else:
            code = doc.text
            label = 'file'
    run_code(code, label, first_line)
    event.app.invalidate()

@kb.add('c-r', filter=not_sidebar & dialog_inactive)
def _do_run_shortcut(event):
    _do_run(event)


@kb.add('c-e', filter=dialog_inactive)
def toggle_output(event):
    exec_state['show_output'] = not exec_state['show_output']
    event.app.invalidate()


@kb.add('c-x', filter=dialog_inactive)
def stop_run(event):
    request_stop()
    event.app.invalidate()


@kb.add('c-q', filter=dialog_inactive)
@kb.add('c-c', filter=dialog_inactive)
def quit_(event):
    if confirm_discard('<quit>'):
        request_stop()  # don't leave a child process running after we exit
        event.app.exit()
    else:
        event.app.invalidate()


# ---------- Style ----------

style = Style.from_dict({
    'sidebar': 'bg:#1e1e2e #c0c0d0',
    'sidebar.selected': 'bg:#4444aa #ffffff bold',
    'output': 'bg:#1a1a2a #dddddd',
})


# ---------- Application ----------

layout = Layout(root_container, focused_element=buffer_control)
app = Application(
    layout=layout,
    key_bindings=kb,
    full_screen=True,
    style=style,
    mouse_support=True,
)


def main():
    app.run()


if __name__ == '__main__':
    main()