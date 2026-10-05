"""Lightweight POSIX-shell scanner.

This is not a full shell parser. It understands quoting, comments, parameter
expansion (``$X``, ``${X:-default}``, ...), command substitution, arithmetic
and heredocs well enough to answer two questions about a script:

* which variables does it *read*, and how (plain, defaulted, guarded, tested)?
* which variables does it *set* (assignment, export, read, for, ...)?
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\[[^\]]*\])?\+?=")

# ${X:-word} etc. never fail on an unset/blank X.
DEFAULTING = {":-", "-", ":=", "=", ":+", "+"}
# ${X:?msg} aborts the script when X is unset (or blank, with the colon).
GUARDING = {":?", "?"}

_LEADING_KEYWORDS = {
    "!",
    "then",
    "do",
    "else",
    "elif",
    "if",
    "while",
    "until",
    "time",
    "{",
    "}",
}
_DECLARERS = {"export", "declare", "typeset", "local", "readonly"}


@dataclass
class Use:
    name: str
    start: int
    end: int
    modifier: str | None = (
        None  # ":-", ":?", "#" (length), "!" (indirect), "op" (other), None (plain)
    )
    tested: bool = False  # operand of a [ -n ... ] / [ -z ... ] test
    guard_test: bool = (
        False  # ... and that test is followed by `exit` (a hand-written guard)
    )


@dataclass
class Def:
    name: str
    pos: int  # offset after which the variable counts as defined
    kind: str


@dataclass
class Scan:
    uses: list[Use] = field(default_factory=list)
    defs: list[Def] = field(default_factory=list)
    sources: list[tuple[str, int]] = field(
        default_factory=list
    )  # (path, pos) of `source x` / `. x`
    evals: list[int] = field(
        default_factory=list
    )  # positions of `eval` we couldn't see through
    set_u: bool = False  # script enables `set -u` / `set -o nounset`


@dataclass
class _Word:
    text: str
    start: int
    uses: list[int]


def scan(text: str) -> Scan:
    """Scan a shell script and return the variables it reads and writes."""
    s = _Scanner(text, 0)
    s.run()
    return s.result


def _unquote(word: str) -> str:
    return word.replace('"', "").replace("'", "")


class _Scanner:
    def __init__(self, text: str, base: int, expansions_only: bool = False):
        self.t = text
        self.n = len(text)
        self.base = base
        self.expansions_only = expansions_only
        self.result = Scan()
        # (words, end offset, separator that ended it: "&&", "||", ";", "|", "&", "\n", ...)
        self.statements: list[tuple[list[_Word], int, str]] = []
        self.pending_heredocs: list[
            tuple[str, bool, bool]
        ] = []  # (delimiter, expands, strip_tabs)

    # -- helpers ---------------------------------------------------------

    def _add_use(self, use: Use, cur) -> None:
        self.result.uses.append(use)
        if cur is not None:
            cur[2].append(len(self.result.uses) - 1)

    def _sub(self, text: str, offset: int, expansions_only: bool = False) -> Scan:
        """Scan a nested fragment (command substitution, ${X:-word}, heredoc body)."""
        sub = _Scanner(text, self.base + offset, expansions_only)
        sub.run()
        return sub.result

    def _merge_uses(self, sub: Scan, cur) -> None:
        for u in sub.uses:
            self._add_use(u, cur)

    def _find_close(self, i: int, open_ch: str, close_ch: str, depth: int = 1) -> int:
        """Index of the bracket closing a group whose body starts at ``i``."""
        t, n = self.t, self.n
        while i < n:
            c = t[i]
            if c == "\\":
                i += 2
                continue
            if c == "'":
                j = t.find("'", i + 1)
                i = n if j == -1 else j + 1
                continue
            if c == '"':
                i += 1
                while i < n and t[i] != '"':
                    i += 2 if t[i] == "\\" else 1
                i += 1
                continue
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return n - 1 if n else 0

    # -- main loop -------------------------------------------------------

    def run(self) -> None:
        if self.expansions_only:
            self._run_expansions_only()
            return
        t, n = self.t, self.n
        words: list[_Word] = []
        cur = None  # [chars, start, use_indices]

        def end_word():
            nonlocal cur
            if cur is not None:
                words.append(_Word("".join(cur[0]), self.base + cur[1], cur[2]))
                cur = None

        def end_stmt(pos):
            end_word()
            if words:
                two = t[pos : pos + 2]
                sep = two if two in ("&&", "||") else (t[pos] if pos < n else "\n")
                self.statements.append((list(words), self.base + pos, sep))
                words.clear()

        i = 0
        while i < n:
            c = t[i]
            if c == "\n":
                end_stmt(i)
                i += 1
                if self.pending_heredocs:
                    i = self._consume_heredocs(i)
                continue
            if c in " \t\r":
                end_word()
                i += 1
                continue
            if c == "#" and cur is None:
                j = t.find("\n", i)
                i = n if j == -1 else j
                continue
            if t.startswith("<<", i) and not t.startswith("<<<", i):
                end_word()
                i = self._heredoc_op(i + 2)
                continue
            if c in "<>":
                end_word()
                j = i
                while j < n and t[j] in "<>&|":
                    j += 1
                words.append(_Word(t[i:j], self.base + i, []))
                i = j
                continue
            if c in ";|&()":
                end_stmt(i)
                i += 1
                continue
            if c in "{}" and cur is None and (i + 1 >= n or t[i + 1] in " \t\r\n;"):
                end_stmt(i)
                i += 1
                continue
            if cur is None:
                cur = [[], i, []]
            if c == "\\":
                cur[0].append(t[i : i + 2])
                i += 2
            elif c == "'":
                j = t.find("'", i + 1)
                j = n - 1 if j == -1 else j
                cur[0].append(t[i : j + 1])
                i = j + 1
            elif c == '"':
                i = self._dquote(i, cur)
            elif c == "$":
                i = self._dollar(i, cur, quoted=False)
            elif c == "`":
                i = self._backtick(i, cur)
            else:
                cur[0].append(c)
                i += 1
        end_stmt(n)
        self._analyse_statements()

    def _run_expansions_only(self) -> None:
        """Heredoc bodies / default words: only $, ` and backslash are special."""
        t, n = self.t, self.n
        i = 0
        while i < n:
            c = t[i]
            if c == "\\":
                i += 2
            elif c == "$":
                i = self._dollar(i, None, quoted=True)
            elif c == "`":
                i = self._backtick(i, None)
            else:
                i += 1

    def _dquote(self, i: int, cur) -> int:
        t, n = self.t, self.n
        cur[0].append('"')
        j = i + 1
        while j < n:
            c = t[j]
            if c == "\\" and j + 1 < n:
                cur[0].append(t[j : j + 2])
                j += 2
            elif c == '"':
                cur[0].append('"')
                return j + 1
            elif c == "$":
                j = self._dollar(j, cur, quoted=True)
            elif c == "`":
                j = self._backtick(j, cur)
            else:
                cur[0].append(c)
                j += 1
        return n

    def _append(self, cur, text: str) -> None:
        if cur is not None:
            cur[0].append(text)

    def _dollar(self, i: int, cur, quoted: bool) -> int:
        t, n = self.t, self.n
        nxt = t[i + 1] if i + 1 < n else ""
        if t.startswith("$((", i):
            close = self._find_close(i + 3, "(", ")", depth=2)
            self._merge_uses(
                self._sub(t[i + 3 : close - 1], i + 3, expansions_only=True), cur
            )
            self._append(cur, t[i : close + 1])
            return close + 1
        if nxt == "(":
            close = self._find_close(i + 2, "(", ")")
            # A subshell: its uses count, its assignments don't leak out.
            self._merge_uses(self._sub(t[i + 2 : close], i + 2), cur)
            self._append(cur, t[i : close + 1])
            return close + 1
        if nxt == "{":
            close = self._find_close(i + 2, "{", "}")
            self._brace(t[i + 2 : close], i, close + 1, cur)
            self._append(cur, t[i : close + 1])
            return close + 1
        if nxt and (nxt.isalpha() or nxt == "_"):
            m = NAME_RE.match(t, i + 1)
            self._add_use(Use(m.group(), self.base + i, self.base + m.end()), cur)
            self._append(cur, t[i : m.end()])
            return m.end()
        if nxt == "'" and not quoted:  # $'ANSI-C string' - no expansion
            j = i + 2
            while j < n and t[j] != "'":
                j += 2 if t[j] == "\\" else 1
            self._append(cur, t[i : j + 1])
            return j + 1
        if nxt and (nxt in "@*#?$!-" or nxt.isdigit()):
            self._append(cur, t[i : i + 2])
            return i + 2
        self._append(cur, "$")
        return i + 1

    def _brace(self, inner: str, start: int, end: int, cur) -> None:
        abs_start, abs_end = self.base + start, self.base + end
        if inner.startswith("#") and NAME_RE.fullmatch(inner[1:]):
            self._add_use(Use(inner[1:], abs_start, abs_end, "#"), cur)
            return
        if inner.startswith("!"):
            m = NAME_RE.match(inner, 1)
            if m:
                self._add_use(Use(m.group(), abs_start, abs_end, "!"), cur)
            return
        m = NAME_RE.match(inner)
        if not m:
            return  # ${1}, ${@}, ...
        name, rest = m.group(), inner[m.end() :]
        mod, word, word_off = None, "", 0
        if rest:
            for op in (":-", ":=", ":?", ":+", "-", "=", "?", "+"):
                if rest.startswith(op):
                    mod, word, word_off = op, rest[len(op) :], m.end() + len(op)
                    break
            else:
                mod, word, word_off = "op", rest, m.end()
        self._add_use(Use(name, abs_start, abs_end, mod), cur)
        if word:
            self._merge_uses(
                self._sub(word, start + 2 + word_off, expansions_only=True), cur
            )
        if mod in (":=", "="):
            self.result.defs.append(Def(name, abs_end, "default-assign"))

    def _backtick(self, i: int, cur) -> int:
        t, n = self.t, self.n
        j = i + 1
        while j < n and t[j] != "`":
            j += 2 if t[j] == "\\" else 1
        self._merge_uses(self._sub(t[i + 1 : j], i + 1), cur)
        self._append(cur, t[i : j + 1])
        return j + 1

    def _heredoc_op(self, i: int) -> int:
        t, n = self.t, self.n
        strip_tabs = i < n and t[i] == "-"
        if strip_tabs:
            i += 1
        while i < n and t[i] in " \t":
            i += 1
        j = i
        while j < n and t[j] not in " \t\r\n;|&<>()":
            j += 1
        raw = t[i:j]
        delim = _unquote(raw).replace("\\", "")
        if delim:
            self.pending_heredocs.append((delim, raw == delim, strip_tabs))
        return j

    def _consume_heredocs(self, i: int) -> int:
        t, n = self.t, self.n
        for delim, expands, strip_tabs in self.pending_heredocs:
            body_start = i
            while i < n:
                eol = t.find("\n", i)
                eol = n if eol == -1 else eol
                line = t[i:eol].rstrip("\r")
                if (line.lstrip("\t") if strip_tabs else line) == delim:
                    if expands:
                        self._merge_uses(
                            self._sub(
                                t[body_start:i], body_start, expansions_only=True
                            ),
                            None,
                        )
                    i = eol + 1
                    break
                i = eol + 1
            else:
                if expands:
                    self._merge_uses(
                        self._sub(t[body_start:n], body_start, expansions_only=True),
                        None,
                    )
        self.pending_heredocs = []
        return min(i, n)

    # -- statement analysis ---------------------------------------------

    def _analyse_statements(self) -> None:
        r = self.result
        stmts = self.statements
        for si, (words, end, sep) in enumerate(stmts):
            texts = [_unquote(w.text) for w in words]
            k = 0
            while k < len(texts) and texts[k] in _LEADING_KEYWORDS:
                k += 1
            assigns = []
            while k < len(texts) and ASSIGN_RE.match(texts[k]):
                assigns.append(ASSIGN_RE.match(texts[k]).group(1))
                k += 1
            if k == len(texts):
                r.defs.extend(Def(a, end, "assign") for a in assigns)
                continue
            cmd, args = texts[k], texts[k + 1 :]
            if cmd in _DECLARERS:
                for a in args:
                    if a.startswith("-"):
                        continue
                    name = a.split("=", 1)[0]
                    if NAME_RE.fullmatch(name):
                        r.defs.append(Def(name, end, cmd))
            elif cmd == "read":
                skip = False
                for a in args:
                    if skip:
                        skip = False
                    elif a in ("-p", "-d", "-n", "-N", "-t", "-u"):
                        skip = True
                    elif (
                        a.startswith("-")
                        or a in ("<", "<<<")
                        or not NAME_RE.fullmatch(a)
                    ):
                        if a in ("<", "<<<"):
                            break
                    else:
                        r.defs.append(Def(a, end, "read"))
            elif cmd == "for" and args and NAME_RE.fullmatch(args[0]):
                r.defs.append(Def(args[0], end, "for"))
            elif cmd in ("mapfile", "readarray"):
                names = [a for a in args if NAME_RE.fullmatch(a)]
                if names:
                    r.defs.append(Def(names[-1], end, cmd))
            elif cmd == "getopts" and len(args) >= 2:
                r.defs.append(Def(args[1], end, "getopts"))
            elif cmd in ("source", ".") and args:
                r.sources.append((args[0], end))
            elif cmd == "eval":
                found = False
                for a in args:
                    m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", a)
                    if m:
                        r.defs.append(Def(m.group(1), end, "eval"))
                        found = True
                if not found:
                    r.evals.append(end)
            elif cmd == "set" and (
                any(
                    a.startswith("-") and not a.startswith("--") and "u" in a
                    for a in args
                )
                or ("nounset" in args)
            ):
                r.set_u = True

            if cmd in ("[", "[[", "test"):
                tested: dict[int, str] = {}  # use index -> "-n" / "-z"
                for wi in range(k + 2, len(words)):
                    if texts[wi - 1] in ("-n", "-z"):
                        for ui in words[wi].uses:
                            r.uses[ui].tested = True
                            tested[ui] = texts[wi - 1]
                if tested and _exits_soon(stmts[si + 1 : si + 4]):
                    in_if = any(t in ("if", "elif") for t in texts[:k])
                    for ui, op in tested.items():
                        # A guard exits when the variable is *missing*:
                        #   [ -z "$X" ] && exit 1  ·  [ -n "$X" ] || exit 1  ·  if [ -z "$X" ]; then exit 1; fi
                        # whereas `[ -n "$SKIP" ] && exit 0` exits when it's present - not a guard.
                        if (
                            (in_if or sep == "&&")
                            and op == "-z"
                            or sep == "||"
                            and not in_if
                            and op == "-n"
                        ):
                            r.uses[ui].guard_test = True


def _exits_soon(following) -> bool:
    """Is there an `exit` in the next few statements, before the if/else branch ends?"""
    for words, _, _ in following:
        if words and _unquote(words[0].text) in ("else", "elif", "fi"):
            return False
        if _first_command(words) == "exit":
            return True
    return False


def _first_command(words: list[_Word]) -> str | None:
    for w in words:
        text = _unquote(w.text)
        if text not in _LEADING_KEYWORDS:
            return text
    return None


# --- GitLab variable-value expansion (variables: FOO: "$BAR/x") ----------

GITLAB_REF_RE = re.compile(
    r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_]*)%"
)


def gitlab_refs(value: str) -> list[str]:
    """Variable names referenced in a GitLab ``variables:`` value."""
    out = []
    for m in GITLAB_REF_RE.finditer(value):
        name = m.group(1) or m.group(2) or m.group(3)
        if name:
            out.append(name)
    return out
