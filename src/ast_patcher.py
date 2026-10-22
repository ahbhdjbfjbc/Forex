"""Surgical, minimal-diff patching of a single constant assignment inside a
Python source file.

Locates the target via the `ast` module, then replaces only that literal's
exact byte range in the original source text — every other line
(formatting, comments, unrelated code) is left completely untouched. This
is deliberately *not* "parse -> mutate the tree -> `ast.unparse()`", since
unparsing regenerates the entire file and discards comments/formatting;
byte-range replacement on the original text is what makes this a genuinely
minimal patch.

Secondary mechanism to `src.strategy_params`: prefer the JSON config for
anything already exposed as a strategy parameter (that's what
`strategy_evolver.py` uses for its search+backtest loop). Reach for this
only to change a literal constant that hasn't been parameterized yet — e.g.
a module-level threshold in `src/indicators.py`.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

Literal = Union[int, float, str, bool, None]


class PatchTargetNotFoundError(Exception):
    pass


def _find_assignment_node(tree: ast.Module, name: str, class_name: str | None) -> ast.AST:
    """Finds the `ast.Assign`/`ast.AnnAssign` node for `name = <literal>`,
    either at module level (`class_name=None`) or as a class-body attribute
    of `class_name`."""
    if class_name is None:
        scope_body = tree.body
    else:
        class_node = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name), None
        )
        if class_node is None:
            raise PatchTargetNotFoundError(f"class {class_name!r} not found")
        scope_body = class_node.body

    for node in scope_body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return node

    where = f" in class {class_name!r}" if class_name else " at module level"
    raise PatchTargetNotFoundError(f"assignment to {name!r} not found{where}")


def patch_constant(
    file_path: str | Path,
    name: str,
    new_value: Literal,
    class_name: str | None = None,
    dry_run: bool = False,
) -> str:
    """Replaces the literal value assigned to `name` (module-level, or an
    attribute of `class_name`) with `new_value`, touching only that
    assignment's exact source span.

    Returns the patched source text. Writes it back to `file_path` unless
    `dry_run=True`. Raises `PatchTargetNotFoundError` if no matching simple
    literal assignment exists, and re-validates that the patched source
    still parses before writing anything.
    """
    path = Path(file_path)
    source = path.read_text()
    tree = ast.parse(source)

    node = _find_assignment_node(tree, name, class_name)
    value_node = node.value if isinstance(node, ast.Assign) else node.value  # type: ignore[union-attr]

    if value_node is None or not hasattr(value_node, "lineno"):
        raise PatchTargetNotFoundError(f"{name!r} is not a simple literal assignment")

    lines = source.splitlines(keepends=True)
    start_line, start_col = value_node.lineno - 1, value_node.col_offset
    end_line, end_col = value_node.end_lineno - 1, value_node.end_col_offset

    new_literal = repr(new_value)

    if start_line == end_line:
        line = lines[start_line]
        lines[start_line] = line[:start_col] + new_literal + line[end_col:]
    else:
        # Multi-line literal (rare for a simple constant) — replace the full span.
        first = lines[start_line][:start_col]
        last = lines[end_line][end_col:]
        lines[start_line : end_line + 1] = [first + new_literal + last]

    patched_source = "".join(lines)

    ast.parse(patched_source)  # sanity check: must still be valid Python before we write anything

    if not dry_run:
        path.write_text(patched_source)
        target_label = f"{class_name}.{name}" if class_name else name
        logger.info("patched %s: %s = %s", path, target_label, new_literal)

    return patched_source


def read_constant(file_path: str | Path, name: str, class_name: str | None = None) -> Literal:
    """Reads the current literal value of `name` without modifying anything —
    useful to snapshot a value before patching it, for an easy manual revert."""
    source = Path(file_path).read_text()
    tree = ast.parse(source)
    node = _find_assignment_node(tree, name, class_name)
    value_node = node.value  # type: ignore[union-attr]
    if not isinstance(value_node, ast.Constant):
        raise PatchTargetNotFoundError(f"{name!r} is not a simple literal assignment")
    return value_node.value
