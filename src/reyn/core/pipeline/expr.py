"""Total expression evaluator for the Pipeline control plane (R1).

Implements the expression grammar pinned in
``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md`` R1: a small,
purpose-built tree-walking interpreter for ``transform.value`` / ``until`` /
``verify.condition`` / ``fold.init``. It is deliberately **not** a general
scripting language and **not** the CodeAct safe-AST gate
(``op_runtime`` / ``_validate_safe_ast``) — that gate restricts a
Turing-complete Python surface for `agent`-invoked tool code, a different
trust context. This module is total by construction instead:

- No recursion and no user-defined functions: the grammar has no call syntax
  beyond a fixed, closed set of combinators (``map``/``filter``/``all``/
  ``any``/``count``/``sum``/``find``/``join``/``get``/``parse_json``), and a
  ``Lambda`` node
  can only be produced as the direct argument of one of those combinators —
  it is never a value, so it can't be stored, named, returned, or invoked
  more than once per element.
- No unbounded loops: every combinator iterates a single already-materialized
  Python list exactly once.
- No IO, no imports of runtime modules, no ``eval``/``exec``: evaluation is a
  pure recursive function over an immutable AST and a ``Mapping`` context;
  the only Python builtins touched are arithmetic/comparison operators and
  container literals.

Because every construct is structural and finite, every well-formed program
provably terminates and the parser can statically enumerate every context
path an expression reads (feeds the future data-flow analyzer).
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExprError(Exception):
    """Base class for expression-language errors."""


class ExprParseError(ExprError):
    """Raised when source text does not conform to the R1 grammar."""


class ExprEvalError(ExprError):
    """Raised for a runtime failure while evaluating an AST against a context.

    Covers: a bare ``Path`` resolving to an absent field, and type errors
    (e.g. ``count`` on a non-list, ``+`` on incompatible operand types).
    Callers (the pipeline executor) map this to a step failure.
    """


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    """A bool / number / string / null literal."""

    value: Any


@dataclass(frozen=True)
class Path:
    """Dotted static context access, e.g. ``ctx.review.passed``."""

    parts: tuple[str, ...]


@dataclass(frozen=True)
class ListLit:
    """A list literal ``[expr, expr, ...]``."""

    items: tuple["ExprNode", ...]


@dataclass(frozen=True)
class ObjectLit:
    """An object literal ``{key: expr, ...}``."""

    fields: tuple[tuple[str, "ExprNode"], ...]


@dataclass(frozen=True)
class Unary:
    """``not expr`` or ``-expr``."""

    op: str  # "not" | "-"
    operand: "ExprNode"


@dataclass(frozen=True)
class Binary:
    """A binary operator application."""

    op: str  # "==" "!=" "<" ">" "<=" ">=" "and" "or" "+" "-" "*" "/"
    left: "ExprNode"
    right: "ExprNode"


@dataclass(frozen=True)
class Lambda:
    """``param -> body``. Only ever appears as a combinator argument node —

    never returned as a standalone evaluation result, never storable in a
    context value, never itself an argument of another combinator except as
    that combinator's own direct lambda slot.
    """

    param: str
    body: "ExprNode"


@dataclass(frozen=True)
class Combinator:
    """One of the fixed R1 combinators applied to its (already-parsed) args."""

    name: str
    args: tuple["ExprNode", ...]


ExprNode = Union[Literal, Path, ListLit, ObjectLit, Unary, Binary, Lambda, Combinator]

COMBINATOR_NAMES = frozenset(
    {"map", "filter", "all", "any", "count", "sum", "find", "join", "get", "parse_json"}
)
_LAMBDA_COMBINATORS = frozenset({"map", "filter", "all", "any", "find"})

# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    pos: int


_TOKEN_SPEC = [
    ("SKIP", r"[ \t\r\n]+"),
    ("NUMBER", r"\d+\.\d+|\d+"),
    ("STRING", r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""),
    ("ARROW", r"->"),
    ("DOT", r"\."),
    ("EQ", r"=="),
    ("NE", r"!="),
    ("LE", r"<="),
    ("GE", r">="),
    ("LT", r"<"),
    ("GT", r">"),
    ("LPAREN", r"\("),
    ("RPAREN", r"\)"),
    ("LBRACE", r"\{"),
    ("RBRACE", r"\}"),
    ("LBRACKET", r"\["),
    ("RBRACKET", r"\]"),
    ("COMMA", r","),
    ("COLON", r":"),
    ("PLUS", r"\+"),
    ("MINUS", r"-"),
    ("STAR", r"\*"),
    ("SLASH", r"/"),
    ("IDENT", r"[A-Za-z_][A-Za-z0-9_]*"),
]
_TOKEN_RE = re.compile("|".join(f"(?P<{name}>{pat})" for name, pat in _TOKEN_SPEC))

_KEYWORDS = {"true", "false", "null", "not", "and", "or"}


def _tokenize(src: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    n = len(src)
    while pos < n:
        m = _TOKEN_RE.match(src, pos)
        if not m:
            raise ExprParseError(
                f"unexpected character {src[pos]!r} at position {pos} in {src!r}"
            )
        kind = m.lastgroup
        text = m.group()
        if kind != "SKIP":
            tokens.append(_Token(kind, text, pos))
        pos = m.end()
    tokens.append(_Token("EOF", "", n))
    return tokens


def _unescape_string(raw: str) -> str:
    # raw includes the surrounding quotes.
    quote = raw[0]
    body = raw[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            mapping = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    _ = quote
    return "".join(out)


# ---------------------------------------------------------------------------
# Parser (recursive descent)
# ---------------------------------------------------------------------------


class _Parser:
    def __init__(self, tokens: list[_Token], src: str) -> None:
        self._tokens = tokens
        self._src = src
        self._i = 0

    def _peek(self) -> _Token:
        return self._tokens[self._i]

    def _advance(self) -> _Token:
        tok = self._tokens[self._i]
        self._i += 1
        return tok

    def _expect(self, kind: str) -> _Token:
        tok = self._peek()
        if tok.kind != kind:
            raise ExprParseError(
                f"expected {kind} but found {tok.kind} ({tok.text!r}) at position "
                f"{tok.pos} in {self._src!r}"
            )
        return self._advance()

    def parse_program(self) -> ExprNode:
        node = self._parse_or()
        self._expect("EOF")
        return node

    # or -> and ("or" and)*
    def _parse_or(self) -> ExprNode:
        left = self._parse_and()
        while self._peek().kind == "IDENT" and self._peek().text == "or":
            self._advance()
            right = self._parse_and()
            left = Binary("or", left, right)
        return left

    # and -> not_expr ("and" not_expr)*
    def _parse_and(self) -> ExprNode:
        left = self._parse_not()
        while self._peek().kind == "IDENT" and self._peek().text == "and":
            self._advance()
            right = self._parse_not()
            left = Binary("and", left, right)
        return left

    # not_expr -> "not" not_expr | comparison
    def _parse_not(self) -> ExprNode:
        if self._peek().kind == "IDENT" and self._peek().text == "not":
            self._advance()
            operand = self._parse_not()
            return Unary("not", operand)
        return self._parse_comparison()

    _CMP_OPS = {"EQ": "==", "NE": "!=", "LT": "<", "GT": ">", "LE": "<=", "GE": ">="}

    # comparison -> additive (cmp_op additive)?
    def _parse_comparison(self) -> ExprNode:
        left = self._parse_additive()
        if self._peek().kind in self._CMP_OPS:
            op_tok = self._advance()
            right = self._parse_additive()
            left = Binary(self._CMP_OPS[op_tok.kind], left, right)
        return left

    # additive -> multiplicative (("+"|"-") multiplicative)*
    def _parse_additive(self) -> ExprNode:
        left = self._parse_multiplicative()
        while self._peek().kind in ("PLUS", "MINUS"):
            op_tok = self._advance()
            right = self._parse_multiplicative()
            left = Binary(op_tok.text, left, right)
        return left

    # multiplicative -> unary (("*"|"/") unary)*
    def _parse_multiplicative(self) -> ExprNode:
        left = self._parse_unary()
        while self._peek().kind in ("STAR", "SLASH"):
            op_tok = self._advance()
            right = self._parse_unary()
            left = Binary(op_tok.text, left, right)
        return left

    # unary -> "-" unary | primary
    def _parse_unary(self) -> ExprNode:
        if self._peek().kind == "MINUS":
            self._advance()
            operand = self._parse_unary()
            return Unary("-", operand)
        return self._parse_primary()

    def _parse_primary(self) -> ExprNode:
        tok = self._peek()
        if tok.kind == "NUMBER":
            self._advance()
            if "." in tok.text:
                return Literal(float(tok.text))
            return Literal(int(tok.text))
        if tok.kind == "STRING":
            self._advance()
            return Literal(_unescape_string(tok.text))
        if tok.kind == "LPAREN":
            self._advance()
            node = self._parse_or()
            self._expect("RPAREN")
            return node
        if tok.kind == "LBRACKET":
            return self._parse_list()
        if tok.kind == "LBRACE":
            return self._parse_object()
        if tok.kind == "IDENT":
            return self._parse_ident_led()
        raise ExprParseError(
            f"unexpected token {tok.kind} ({tok.text!r}) at position {tok.pos} "
            f"in {self._src!r}"
        )

    def _parse_list(self) -> ExprNode:
        self._expect("LBRACKET")
        items: list[ExprNode] = []
        if self._peek().kind != "RBRACKET":
            items.append(self._parse_or())
            while self._peek().kind == "COMMA":
                self._advance()
                items.append(self._parse_or())
        self._expect("RBRACKET")
        return ListLit(tuple(items))

    def _parse_object(self) -> ExprNode:
        self._expect("LBRACE")
        fields: list[tuple[str, ExprNode]] = []
        if self._peek().kind != "RBRACE":
            fields.append(self._parse_object_field())
            while self._peek().kind == "COMMA":
                self._advance()
                fields.append(self._parse_object_field())
        self._expect("RBRACE")
        return ObjectLit(tuple(fields))

    def _parse_object_field(self) -> tuple[str, ExprNode]:
        key_tok = self._expect("IDENT")
        self._expect("COLON")
        value = self._parse_or()
        return key_tok.text, value

    def _parse_ident_led(self) -> ExprNode:
        tok = self._advance()
        name = tok.text
        if name == "true":
            return Literal(True)
        if name == "false":
            return Literal(False)
        if name == "null":
            return Literal(None)
        if name in ("and", "or", "not"):
            raise ExprParseError(
                f"keyword {name!r} used as an expression at position {tok.pos} "
                f"in {self._src!r}"
            )
        if self._peek().kind == "LPAREN":
            if name not in COMBINATOR_NAMES:
                raise ExprParseError(
                    f"{name!r} is not a known combinator (arbitrary function "
                    f"calls are not part of the R1 grammar) at position {tok.pos} "
                    f"in {self._src!r}"
                )
            return self._parse_combinator(name)
        # dotted path
        parts = [name]
        while self._peek().kind == "DOT":
            self._advance()  # consume '.'
            part_tok = self._expect("IDENT")
            parts.append(part_tok.text)
        return Path(tuple(parts))

    def _parse_combinator(self, name: str) -> ExprNode:
        self._expect("LPAREN")
        args: list[ExprNode] = []
        if name in _LAMBDA_COMBINATORS:
            list_expr = self._parse_or()
            self._expect("COMMA")
            lam = self._parse_lambda()
            args = [list_expr, lam]
        elif name in ("count", "sum", "parse_json"):
            args = [self._parse_or()]
        elif name == "join":
            list_expr = self._parse_or()
            self._expect("COMMA")
            sep_expr = self._parse_or()
            args = [list_expr, sep_expr]
        elif name == "get":
            base_expr = self._parse_or()
            self._expect("COMMA")
            path_tok = self._expect("STRING")
            args = [base_expr, Literal(_unescape_string(path_tok.text))]
            if self._peek().kind == "COMMA":
                self._advance()
                default_expr = self._parse_or()
                args.append(default_expr)
        else:  # pragma: no cover - COMBINATOR_NAMES is exhaustive above
            raise ExprParseError(f"unknown combinator {name!r}")
        self._expect("RPAREN")
        return Combinator(name, tuple(args))

    def _parse_lambda(self) -> ExprNode:
        param_tok = self._expect("IDENT")
        if param_tok.text in _KEYWORDS:
            raise ExprParseError(
                f"lambda parameter cannot be keyword {param_tok.text!r} at "
                f"position {param_tok.pos} in {self._src!r}"
            )
        self._expect("ARROW")
        body = self._parse_or()
        return Lambda(param_tok.text, body)


def parse(src: str) -> ExprNode:
    """Parse ``src`` into an :class:`ExprNode` AST. Raises :class:`ExprParseError`."""
    if not isinstance(src, str) or not src.strip():
        raise ExprParseError("expression source must be a non-empty string")
    tokens = _tokenize(src)
    parser = _Parser(tokens, src)
    return parser.parse_program()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


class _ChildContext(Mapping):
    """A context overlaying a single lambda binding on top of a base mapping."""

    __slots__ = ("_base", "_name", "_value")

    def __init__(self, base: Mapping[str, Any], name: str, value: Any) -> None:
        self._base = base
        self._name = name
        self._value = value

    def __getitem__(self, key: str) -> Any:
        if key == self._name:
            return self._value
        return self._base[key]

    def __iter__(self):
        seen = {self._name}
        yield self._name
        for k in self._base:
            if k not in seen:
                yield k

    def __len__(self) -> int:
        return len({self._name, *self._base.keys()})

    def __contains__(self, key: object) -> bool:
        return key == self._name or key in self._base


def evaluate(node: ExprNode, context: Mapping[str, Any]) -> Any:
    """Evaluate ``node`` (from :func:`parse`) against ``context``. Pure; total.

    Raises :class:`ExprEvalError` on an absent bare-``Path`` field or a type
    error (e.g. arithmetic/``count``/``sum``/``join`` on the wrong shape).
    """
    if isinstance(node, Literal):
        return node.value
    if isinstance(node, Path):
        return _resolve_path(node.parts, context)
    if isinstance(node, ListLit):
        return [evaluate(item, context) for item in node.items]
    if isinstance(node, ObjectLit):
        return {key: evaluate(value, context) for key, value in node.fields}
    if isinstance(node, Unary):
        return _eval_unary(node, context)
    if isinstance(node, Binary):
        return _eval_binary(node, context)
    if isinstance(node, Combinator):
        return _eval_combinator(node, context)
    if isinstance(node, Lambda):  # pragma: no cover - defensive; never reached
        raise ExprEvalError("a lambda cannot be evaluated outside a combinator")
    raise ExprEvalError(f"unknown AST node: {node!r}")  # pragma: no cover


def evaluate_expr(src: str, context: Mapping[str, Any]) -> Any:
    """Convenience: ``evaluate(parse(src), context)``."""
    return evaluate(parse(src), context)


def _missing_structured_hint(part: str, current: Mapping[str, Any]) -> str:
    """A decision-enabling addendum for the single most common absent-path shape a pipeline
    author hits: reading ``.structured`` off a ``ctx.<step_name>`` reduced-fields dict
    (:func:`reyn.core.offload.canonical.canonical_to_ctx_fields`'s ``{"text", "structured"?,
    "meta"?}`` shape) whose producer emitted no ``structured`` attachment.

    Without this, the failure is an opaque ``path '...structured' is absent: no field
    '...structured' in context`` with no hint that (a) the field is ABSENT-WHEN-EMPTY BY
    DESIGN (not a bug in the pipeline), and (b) the fix lives in the PRODUCER's canonical
    mapper, not in the pipeline expression. #2955/#2972: this is exactly the shape a
    ``for_each: over: ctx.<name>.structured`` over a text-only producer (e.g. glob_files
    before its canonical mapper was fixed to emit a ``structured`` attachment) used to hit
    with zero guidance toward the actual cause."""
    if part != "structured" or "text" not in current:
        return ""
    return (
        " -- this producer's canonical result carries no `structured` attachment (text-only "
        "or a legit-empty result), and `structured` is ABSENT-WHEN-EMPTY by design (see "
        "canonical_to_ctx_fields), not a pipeline bug. If this producer SHOULD support "
        "list-style iteration (e.g. `for_each`), its canonical mapper "
        "(reyn.core.offload.canonical) needs to emit a `structured` attachment for the data "
        "(see `web_search_to_canonical` / `file_to_canonical`'s `glob` branch for the "
        "pattern); if it genuinely has no list data, read `.text` instead"
    )


def _resolve_path(parts: Sequence[str], context: Mapping[str, Any]) -> Any:
    current: Any = context
    consumed: list[str] = []
    for part in parts:
        consumed.append(part)
        if isinstance(current, Mapping):
            if part not in current:
                raise ExprEvalError(
                    f"path {'.'.join(parts)!r} is absent: no field "
                    f"{'.'.join(consumed)!r} in context"
                    f"{_missing_structured_hint(part, current)}"
                )
            current = current[part]
        else:
            raise ExprEvalError(
                f"path {'.'.join(parts)!r} is absent: {'.'.join(consumed[:-1]) or '<root>'} "
                f"is not an object, cannot access {part!r}"
            )
    return current


def _resolve_path_safe(parts: Sequence[str], base: Any, default: Any) -> Any:
    current = base
    for part in parts:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return default
    return current


def _eval_unary(node: Unary, context: Mapping[str, Any]) -> Any:
    value = evaluate(node.operand, context)
    if node.op == "not":
        return not _truthy(value)
    if node.op == "-":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ExprEvalError(f"unary '-' requires a number, got {type(value).__name__}")
        return -value
    raise ExprEvalError(f"unknown unary operator {node.op!r}")  # pragma: no cover


def _truthy(value: Any) -> bool:
    return bool(value)


_NUMERIC_OPS = {"+", "-", "*", "/"}
_CMP_SYMBOLS = {"==", "!=", "<", ">", "<=", ">="}


def _eval_binary(node: Binary, context: Mapping[str, Any]) -> Any:
    op = node.op
    if op == "and":
        left = evaluate(node.left, context)
        if not _truthy(left):
            return left
        return evaluate(node.right, context)
    if op == "or":
        left = evaluate(node.left, context)
        if _truthy(left):
            return left
        return evaluate(node.right, context)

    left = evaluate(node.left, context)
    right = evaluate(node.right, context)

    if op in _CMP_SYMBOLS:
        return _eval_comparison(op, left, right)
    if op in _NUMERIC_OPS:
        return _eval_arithmetic(op, left, right)
    raise ExprEvalError(f"unknown binary operator {op!r}")  # pragma: no cover


def _eval_comparison(op: str, left: Any, right: Any) -> bool:
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    ordering_ok = isinstance(left, (int, float)) and isinstance(right, (int, float))
    ordering_ok = ordering_ok or (isinstance(left, str) and isinstance(right, str))
    if not ordering_ok:
        raise ExprEvalError(
            f"comparison {op!r} requires two numbers or two strings, got "
            f"{type(left).__name__} and {type(right).__name__}"
        )
    if op == "<":
        return left < right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == ">=":
        return left >= right
    raise ExprEvalError(f"unknown comparison operator {op!r}")  # pragma: no cover


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _eval_arithmetic(op: str, left: Any, right: Any) -> Any:
    if op == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right
    if op == "+" and isinstance(left, list) and isinstance(right, list):
        return left + right
    if not (_is_number(left) and _is_number(right)):
        raise ExprEvalError(
            f"arithmetic {op!r} requires two numbers (or '+' on two strings/lists), "
            f"got {type(left).__name__} and {type(right).__name__}"
        )
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise ExprEvalError("division by zero")
        result = left / right
        if isinstance(left, int) and isinstance(right, int) and left % right == 0:
            return left // right
        return result
    raise ExprEvalError(f"unknown arithmetic operator {op!r}")  # pragma: no cover


def _require_list(value: Any, combinator: str) -> list:
    if not isinstance(value, list):
        raise ExprEvalError(f"{combinator}() requires a list, got {type(value).__name__}")
    return value


def _eval_combinator(node: Combinator, context: Mapping[str, Any]) -> Any:
    name = node.name
    if name in _LAMBDA_COMBINATORS:
        list_node, lam = node.args
        items = _require_list(evaluate(list_node, context), name)
        assert isinstance(lam, Lambda)
        if name == "map":
            return [evaluate(lam.body, _ChildContext(context, lam.param, item)) for item in items]
        if name == "filter":
            return [
                item
                for item in items
                if _truthy(evaluate(lam.body, _ChildContext(context, lam.param, item)))
            ]
        if name == "all":
            return all(
                _truthy(evaluate(lam.body, _ChildContext(context, lam.param, item)))
                for item in items
            )
        if name == "any":
            return any(
                _truthy(evaluate(lam.body, _ChildContext(context, lam.param, item)))
                for item in items
            )
        if name == "find":
            for item in items:
                if _truthy(evaluate(lam.body, _ChildContext(context, lam.param, item))):
                    return item
            return None
        raise ExprEvalError(f"unhandled lambda combinator {name!r}")  # pragma: no cover

    if name == "count":
        items = _require_list(evaluate(node.args[0], context), name)
        return len(items)

    if name == "sum":
        items = _require_list(evaluate(node.args[0], context), name)
        total = 0
        for item in items:
            if not _is_number(item):
                raise ExprEvalError(f"sum() requires a list of numbers, found {type(item).__name__}")
            total += item
        return total

    if name == "join":
        list_node, sep_node = node.args
        items = _require_list(evaluate(list_node, context), name)
        sep = evaluate(sep_node, context)
        if not isinstance(sep, str):
            raise ExprEvalError(f"join() separator must be a string, got {type(sep).__name__}")
        for item in items:
            if not isinstance(item, str):
                raise ExprEvalError(
                    f"join() requires a list of strings, found {type(item).__name__}"
                )
        return sep.join(items)

    if name == "get":
        base = evaluate(node.args[0], context)
        path_literal = node.args[1]
        assert isinstance(path_literal, Literal) and isinstance(path_literal.value, str)
        parts = path_literal.value.split(".")
        default = evaluate(node.args[2], context) if len(node.args) > 2 else None
        return _resolve_path_safe(parts, base, default)

    if name == "parse_json":
        value = evaluate(node.args[0], context)
        if not isinstance(value, str):
            raise ExprEvalError(f"parse_json() requires a string, got {type(value).__name__}")
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ExprEvalError(f"parse_json() failed to decode: {exc}") from exc

    raise ExprEvalError(f"unknown combinator {name!r}")  # pragma: no cover
