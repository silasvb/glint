"""Evaluator for GitLab CI/CD expressions (``rules:if``, ``only:variables``).

Supports ``$VAR``, string literals, ``/regex/flags``, ``null``, ``==``, ``!=``,
``=~``, ``!~``, ``&&``, ``||`` and parentheses, with GitLab's semantics:

* a bare ``$VAR`` is true when the variable is defined and non-empty;
* an undefined variable compares equal to ``null`` only;
* ``=~`` is an unanchored search; the right-hand side may be a variable that
  holds a ``/regex/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

Lookup = Callable[[str], str | None]

_TOKEN_RE = re.compile(
    r"""\s*(?:
      (?P<var>\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)
    | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
    | (?P<regex>/(?:[^/\\]|\\.)*/[a-z]*)
    | (?P<null>null\b)
    | (?P<op>==|!=|=~|!~|&&|\|\||\(|\))
    )""",
    re.VERBOSE,
)


class ExprError(ValueError):
    pass


@dataclass
class _Tok:
    kind: str
    text: str


@dataclass
class Result:
    value: bool
    explanation: str  # the expression with variable values substituted
    variables: list[str]  # variables referenced


class _Regex:
    def __init__(self, source: str):
        self.source = source
        body, _, flags = source[1:].rpartition("/")
        f = 0
        if "i" in flags:
            f |= re.IGNORECASE
        if "m" in flags:  # Ruby's /m is Python's DOTALL
            f |= re.DOTALL
        if "x" in flags:
            f |= re.VERBOSE
        body = body.replace(r"\z", r"\Z").replace(r"\/", "/")
        try:
            self.rx = re.compile(body, f)
        except re.error as e:
            raise ExprError(f"invalid regex {source}: {e}") from e


def _tokenize(expr: str) -> list[_Tok]:
    toks, i = [], 0
    expr = expr.strip()
    while i < len(expr):
        m = _TOKEN_RE.match(expr, i)
        if not m or m.end() == i:
            if expr[i:].strip() == "":
                break
            raise ExprError(f"unexpected input at: {expr[i:]!r}")
        kind = m.lastgroup
        toks.append(_Tok(kind, m.group(kind)))
        i = m.end()
    return toks


def _var_name(text: str) -> str:
    return text.strip("${}")


class _Parser:
    def __init__(self, toks: list[_Tok], lookup: Lookup):
        self.toks = toks
        self.pos = 0
        self.lookup = lookup
        self.vars: list[str] = []
        self.parts: list[str] = []

    def peek(self) -> _Tok | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def take(self) -> _Tok:
        tok = self.peek()
        if tok is None:
            raise ExprError("unexpected end of expression")
        self.pos += 1
        return tok

    # or := and ('||' and)*
    def parse_or(self):
        left = self.parse_and()
        while (t := self.peek()) and t.text == "||":
            self.take()
            right = self.parse_and()
            left = ("or", left, right)
        return left

    def parse_and(self):
        left = self.parse_cmp()
        while (t := self.peek()) and t.text == "&&":
            self.take()
            right = self.parse_cmp()
            left = ("and", left, right)
        return left

    def parse_cmp(self):
        left = self.parse_primary()
        t = self.peek()
        if t and t.text in ("==", "!=", "=~", "!~"):
            self.take()
            right = self.parse_primary()
            return ("cmp", t.text, left, right)
        return left

    def parse_primary(self):
        t = self.take()
        if t.text == "(":
            node = self.parse_or()
            if self.take().text != ")":
                raise ExprError("expected )")
            return ("group", node)
        if t.kind == "var":
            name = _var_name(t.text)
            self.vars.append(name)
            return ("var", name)
        if t.kind == "str":
            return ("str", t.text[1:-1].replace('\\"', '"').replace("\\'", "'"))
        if t.kind == "regex":
            return ("regex", _Regex(t.text))
        if t.kind == "null":
            return ("null",)
        raise ExprError(f"unexpected token {t.text!r}")


def _operand(node, lookup: Lookup):
    kind = node[0]
    if kind == "var":
        return lookup(node[1])
    if kind == "str":
        return node[1]
    if kind == "regex":
        return node[1]
    if kind == "null":
        return None
    raise ExprError(f"not an operand: {kind}")


def _show(node, lookup: Lookup) -> str:
    kind = node[0]
    if kind == "var":
        v = lookup(node[1])
        return f"${node[1]}(unset)" if v is None else f'${node[1]}(="{v}")'
    if kind == "str":
        return f'"{node[1]}"'
    if kind == "regex":
        return node[1].source
    if kind == "null":
        return "null"
    if kind == "group":
        return "(" + _show(node[1], lookup) + ")"
    if kind == "cmp":
        return f"{_show(node[2], lookup)} {node[1]} {_show(node[3], lookup)}"
    if kind in ("and", "or"):
        op = "&&" if kind == "and" else "||"
        return f"{_show(node[1], lookup)} {op} {_show(node[2], lookup)}"
    return "?"


def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, _Regex):
        return True
    return v != ""


def _as_regex(v) -> _Regex | None:
    if isinstance(v, _Regex):
        return v
    if isinstance(v, str):
        m = re.fullmatch(r"/(.*)/([a-z]*)", v, re.DOTALL)
        if m:
            return _Regex(v)
        try:
            return _Regex(f"/{v}/")
        except ExprError:
            return None
    return None


def _eval(node, lookup: Lookup):
    kind = node[0]
    if kind == "group":
        return _eval(node[1], lookup)
    if kind == "and":
        return _truthy(_eval(node[1], lookup)) and _truthy(_eval(node[2], lookup))
    if kind == "or":
        return _truthy(_eval(node[1], lookup)) or _truthy(_eval(node[2], lookup))
    if kind == "cmp":
        op, left, right = node[1], _operand(node[2], lookup), _operand(node[3], lookup)
        if op in ("==", "!="):
            if isinstance(left, _Regex):
                left = left.source
            if isinstance(right, _Regex):
                right = right.source
            eq = left == right
            return eq if op == "==" else not eq
        rx = _as_regex(right)
        if rx is None or not isinstance(left, str):
            matched = False
        else:
            matched = rx.rx.search(left) is not None
        return matched if op == "=~" else not matched
    return _operand(node, lookup)


def evaluate(expr: str, lookup: Lookup) -> Result:
    """Evaluate ``expr``; raises ExprError on syntax errors."""
    p = _Parser(_tokenize(str(expr)), lookup)
    tree = p.parse_or()
    if p.peek() is not None:
        raise ExprError(f"unexpected token {p.peek().text!r}")
    return Result(_truthy(_eval(tree, lookup)), _show(tree, lookup), p.vars)


def referenced_variables(expr: str) -> list[str]:
    try:
        return [_var_name(t.text) for t in _tokenize(str(expr)) if t.kind == "var"]
    except ExprError:
        return re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", str(expr))
