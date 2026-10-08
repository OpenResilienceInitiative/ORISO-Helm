"""Every render contract must run when CI executes its file directly (ORISO-Helm#360).

validate-helm-chart.yml runs each tests/render_*_test.py as `python <file>`. A file
that only defines test_* functions exits 0 without calling any of them, so the
contract counts as passed although nothing was checked. This guard names every
test_* function that the file's `if __name__ == "__main__":` block does not call.
"""

from __future__ import annotations

import ast
import pathlib
import unittest


TESTS = pathlib.Path(__file__).resolve().parent


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    left, comparators = node.test.left, node.test.comparators
    names = {getattr(left, "id", None)} | {getattr(c, "id", None) for c in comparators}
    values = {getattr(left, "value", None)} | {getattr(c, "value", None) for c in comparators}
    return "__name__" in names and "__main__" in values


def _called_names(node: ast.AST) -> set[str]:
    return {
        inner.func.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
    }


def _delegated_call(function: ast.FunctionDef) -> str | None:
    """The callee of a test whose whole body is one call, e.g. `def test_x(): main()`."""
    body = function.body
    if (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Call)
        and isinstance(body[0].value.func, ast.Name)
    ):
        return body[0].value.func.id
    return None


def uncalled_tests(source: str) -> list[str]:
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    # Everything the __main__ block reaches, following module-level functions
    # such as `main()` that call the cases one by one.
    pending = [name for node in tree.body if _is_main_guard(node) for name in _called_names(node)]
    reached: set[str] = set()
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        if name in functions:
            pending.extend(_called_names(functions[name]))
    # A test that only delegates to a reached function (a pytest alias for
    # `main()`) checks nothing the direct run does not.
    return [
        name
        for name, function in functions.items()
        if name.startswith("test_")
        and name not in reached
        and _delegated_call(function) not in reached
    ]


class RenderContractEntrypointTest(unittest.TestCase):
    def test_every_render_contract_runs_its_tests_when_executed_directly(self) -> None:
        missing = {
            path.name: names
            for path in sorted(TESTS.glob("render_*_test.py"))
            if (names := uncalled_tests(path.read_text()))
        }
        self.assertEqual(
            missing,
            {},
            "These render contracts define tests their __main__ block never calls, "
            "so CI reports them as passed without running them",
        )

    def test_guard_detects_a_contract_without_entrypoint(self) -> None:
        self.assertEqual(uncalled_tests("def test_a():\n    assert False\n"), ["test_a"])
        self.assertEqual(
            uncalled_tests(
                "def test_a():\n    pass\n\ndef test_b():\n    pass\n\n"
                'if __name__ == "__main__":\n    test_a()\n'
            ),
            ["test_b"],
        )
        self.assertEqual(
            uncalled_tests('def test_a():\n    pass\n\nif __name__ == "__main__":\n    test_a()\n'),
            [],
        )
        through_main = (
            "def test_a():\n    pass\n\ndef test_b():\n    pass\n\n"
            "def main():\n    test_a()\n\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        self.assertEqual(uncalled_tests(through_main), ["test_b"])
        alias = (
            "def main():\n    assert True\n\ndef test_all():\n    main()\n\n"
            'if __name__ == "__main__":\n    main()\n'
        )
        self.assertEqual(uncalled_tests(alias), [])


if __name__ == "__main__":
    unittest.main()
