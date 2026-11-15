import sys
import os
import io
import ast
import threading
import traceback

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    HSplit, VSplit, Window, FloatContainer, Float, ConditionalContainer
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.lexers import PygmentsLexer
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.selection import SelectionState
from prompt_toolkit.styles import Style
from pygments.lexers.python import PythonLexer


# ---------- Config ----------

FILE_PATH = sys.argv[1] if len(sys.argv) > 1 else None
ROOT_DIR = os.getcwd()
SIDEBAR_WIDTH = 32

PYTHON_WORDS = [
    'print', 'import', 'from', 'def', 'class', 'return',
    'for', 'while', 'if', 'elif', 'else', 'try', 'except',
    'finally', 'with', 'as', 'lambda', 'yield', 'pass',
    'break', 'continue', 'raise', 'assert', 'del', 'global',
    'nonlocal', 'True', 'False', 'None', 'and', 'or', 'not',
    'in', 'is', 'self', 'len', 'range', 'str', 'int', 'float',
    'list', 'dict', 'set', 'tuple', 'open', 'enumerate', 'zip',
]


# ---------- Buffer ----------

buffer = Buffer(
    completer=WordCompleter(PYTHON_WORDS, ignore_case=True),
    complete_while_typing=True,
    name='main',
)

if FILE_PATH and os.path.exists(FILE_PATH):
    with open(FILE_PATH, 'r') as f:
        buffer.text = f.read()
    buffer.cursor_position = 0


# ---------- State ----------

status = {'message': 'Ready'}

exec_state = {
    'namespace': {'__name__': '__main__'},
    'output': '',
    'running': False,
    'thread': None,
    'stop_event': threading.Event(),
    'show_output': False,
}

tree_state = {
    'root': ROOT_DIR,
    'expanded': set(),
    'selected': 0,
    'items': [],
}


def rebuild_tree():
    items = []

    def walk(path, depth):
        try:
            entries = os.listdir(path)
        except PermissionError:
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
    run = ' [RUNNING]' if exec_state['running'] else ''
    return (
        f' {path}   |   {status["message"]}{run}   |   '
        f'Ctrl+B sidebar  Ctrl+A sel-all  F5 run  Ctrl+S save  '
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


# ---------- Loop detection ----------

def enclosing_loop_source(text, cursor_index):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    lines = text.splitlines(keepends=True)

    def offset_of_line(n):
        return sum(len(l) for l in lines[:n - 1])

    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            start_off = offset_of_line(node.lineno)
            end_line = getattr(node, 'end_lineno', node.lineno) or node.lineno
            end_off = offset_of_line(end_line + 1)
            if start_off <= cursor_index <= end_off:
                target = node
    if target is None:
        return None
    try:
        return ast.get_source_segment(text, target)
    except Exception:
        return None


# ---------- Code execution ----------

def run_code(code, label):
    if exec_state['running']:
        status['message'] = 'Already running — Ctrl+X to stop'
        return
    exec_state['running'] = True
    exec_state['output'] = ''
    exec_state['show_output'] = True
    exec_state['stop_event'].clear()
    status['message'] = f'Running {label}...'

    def worker():
        buf = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = buf
        try:
            compiled = compile(code, f'<{label}>', 'exec')
            exec(compiled, exec_state['namespace'])
        except SystemExit:
            pass
        except BaseException:
            traceback.print_exc(file=buf)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            raw = buf.getvalue()
            exec_state['output'] = (
                f"=== ran ({label}) ===\n{code}\n"
                f"=== stdout / stderr ===\n"
                f"{raw if raw else '(no print output)'}"
            )
            exec_state['running'] = False
            status['message'] = f'{label} finished'
            try:
                app.invalidate()
            except Exception:
                pass

    t = threading.Thread(target=worker, daemon=True)
    exec_state['thread'] = t
    t.start()


def get_output_text():
    if exec_state['running']:
        return 'Running... (Ctrl+X to request stop)'
    return exec_state['output'] or '(no output yet — press F5 to run)'


# ---------- Controls ----------

buffer_control = BufferControl(buffer=buffer, lexer=PygmentsLexer(PythonLexer))
editor_window = Window(buffer_control, wrap_lines=False)

output_window = Window(
    FormattedTextControl(get_output_text),
    style='class:output',
    wrap_lines=True,
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
        try:
            with open(path, 'r') as f:
                buffer.text = f.read()
            buffer.cursor_position = 0
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
    sidebar_window,
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
            content=CompletionsMenu(max_height=10, scroll_offset=1),
        ),
        Float(
            content=ConditionalContainer(
                output_window,
                filter=Condition(lambda: exec_state['show_output']),
            ),
            top=2, right=2, width=60, height=15,
        ),
    ],
)


# ---------- Global key bindings ----------

kb = KeyBindings()
not_sidebar = ~has_focus(sidebar_control)


@kb.add('c-b')
def toggle_sidebar(event):
    try:
        current = event.app.layout.current_control
    except Exception:
        current = None
    if current is sidebar_control:
        event.app.layout.focus(buffer_control)
        status['message'] = 'Editor focused'
    else:
        event.app.layout.focus(sidebar_control)
        status['message'] = 'Sidebar — arrows/Enter, Tab to edit, Ctrl+R refresh'
    event.app.invalidate()


@kb.add('c-a', filter=not_sidebar)
def select_all(event):
    """Ctrl+A: select the entire buffer."""
    if buffer.text:
        # Anchor at offset 0, move cursor to the end → full selection
        buffer.selection_state = SelectionState(original_cursor_position=0)
        buffer.cursor_position = len(buffer.text)
        status['message'] = 'Selected all — Backspace to clear, F5 to run'
    else:
        status['message'] = 'Buffer is empty'
    event.app.invalidate()


@kb.add('c-s')
def save(event):
    global FILE_PATH
    if FILE_PATH is None:
        FILE_PATH = 'untitled.py'
    try:
        with open(FILE_PATH, 'w') as f:
            f.write(buffer.text)
        status['message'] = f'Saved to {FILE_PATH}'
    except Exception as e:
        status['message'] = f'Save failed: {e}'
    event.app.invalidate()


def _do_run(event):
    doc = buffer.document
    sel = doc.selection_range()
    if sel and sel[0] != sel[1]:
        start, end = sel
        code = doc.text[start:end]
        label = 'selection'
    else:
        loop_src = enclosing_loop_source(doc.text, doc.cursor_position)
        if loop_src:
            code = loop_src
            label = 'enclosing loop'
        else:
            code = doc.text
            label = 'file'
    status['message'] = f'Running {label}'
    run_code(code, label)
    event.app.invalidate()


kb.add('f5')(_do_run)
kb.add('c-r', filter=not_sidebar)(_do_run)


@kb.add('c-e')
def toggle_output(event):
    exec_state['show_output'] = not exec_state['show_output']
    event.app.invalidate()


@kb.add('c-x')
def stop_run(event):
    if exec_state['running']:
        exec_state['stop_event'].set()
        status['message'] = 'Stop requested'
    else:
        status['message'] = 'Nothing running'
    event.app.invalidate()


@kb.add('c-q')
def quit_(event):
    event.app.exit()


@kb.add('c-c')
def quit_c(event):
    event.app.exit()


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
)


def main():
    app.run()


if __name__ == '__main__':
    main()