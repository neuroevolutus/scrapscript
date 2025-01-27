#!/usr/bin/env python3.10
from __future__ import annotations
import argparse
import base64
import code
import dataclasses
import enum
import functools
import json
import logging
import os
import re
import struct
import sys
import typing
import unittest
import urllib.request
from dataclasses import dataclass
from enum import auto
from types import ModuleType
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Set, Tuple, Union

readline: Optional[ModuleType]
try:
    import readline
except ImportError:
    readline = None


logger = logging.getLogger(__name__)


def is_identifier_char(c: str) -> bool:
    return c.isalnum() or c in ("$", "'", "_")


@dataclass(eq=True, unsafe_hash=True)
class SourceLocation:
    lineno: int = dataclasses.field(default=-1)
    colno: int = dataclasses.field(default=-1)
    byteno: int = dataclasses.field(default=-1)


@dataclass(eq=True, unsafe_hash=True)
class SourceExtent:
    start: SourceLocation = dataclasses.field(default_factory=SourceLocation)
    end: SourceLocation = dataclasses.field(default_factory=SourceLocation)


@dataclass(eq=True)
class Token:
    source_extent: SourceExtent = dataclasses.field(default_factory=SourceExtent, init=False, compare=False)


@dataclass(eq=True)
class IntLit(Token):
    value: int


@dataclass(eq=True)
class FloatLit(Token):
    value: float


@dataclass(eq=True)
class StringLit(Token):
    value: str


@dataclass(eq=True)
class BytesLit(Token):
    value: str
    base: int


@dataclass(eq=True)
class Operator(Token):
    value: str


@dataclass(eq=True)
class Name(Token):
    value: str


@dataclass(eq=True)
class LeftParen(Token):
    # (
    pass


@dataclass(eq=True)
class RightParen(Token):
    # )
    pass


@dataclass(eq=True)
class LeftBrace(Token):
    # {
    pass


@dataclass(eq=True)
class RightBrace(Token):
    # }
    pass


@dataclass(eq=True)
class LeftBracket(Token):
    # [
    pass


@dataclass(eq=True)
class RightBracket(Token):
    # ]
    pass


@dataclass(eq=True)
class Hash(Token):
    # #
    pass


@dataclass(eq=True)
class EOF(Token):
    pass


def num_bytes_as_utf8(s: str) -> int:
    return len(s.encode(encoding="UTF-8"))


class Lexer:
    def __init__(self, text: str):
        self.text: str = text
        self.idx: int = 0
        self._lineno: int = 1
        self._colno: int = 1
        self.line: str = ""
        self._byteno: int = 0
        self.current_token_source_extent: SourceExtent = SourceExtent(
            start=SourceLocation(
                lineno=self._lineno,
                colno=self._colno,
                byteno=self._byteno,
            ),
            end=SourceLocation(
                lineno=self._lineno,
                colno=self._colno,
                byteno=self._byteno,
            ),
        )
        self.token_start_idx: int = self.idx
        self.token_end_idx: int = self.token_start_idx

    @property
    def lineno(self) -> int:
        return self._lineno

    @property
    def colno(self) -> int:
        return self._colno

    @property
    def byteno(self) -> int:
        return self._byteno

    def mark_token_start(self) -> None:
        self.current_token_source_extent.start.lineno = self._lineno
        self.current_token_source_extent.start.colno = self._colno
        self.current_token_source_extent.start.byteno = self._byteno
        self.token_start_idx = self.idx

    def mark_token_end(self) -> None:
        self.current_token_source_extent.end.lineno = self._lineno
        self.current_token_source_extent.end.colno = self._colno
        self.current_token_source_extent.end.byteno = self._byteno
        self.token_end_idx = self.idx

    def has_input(self) -> bool:
        return self.idx < len(self.text)

    def read_char(self) -> str:
        self.mark_token_end()
        c = self.peek_char()
        if c == "\n":
            self._lineno += 1
            self._colno = 1
            self.line = ""
        else:
            self.line += c
            self._colno += 1
        self.idx += 1
        self._byteno += num_bytes_as_utf8(c)
        return c

    def peek_char(self) -> str:
        if not self.has_input():
            raise UnexpectedEOFError("while reading token")
        return self.text[self.idx]

    def make_token(self, cls: type, *args: Any) -> Token:
        result: Token = cls(*args)

        # Set start of token's source extent
        result.source_extent.start.lineno = self.current_token_source_extent.start.lineno
        result.source_extent.start.colno = self.current_token_source_extent.start.colno
        result.source_extent.start.byteno = self.current_token_source_extent.start.byteno

        # Set end of token's source extent
        result.source_extent.end.colno = self.current_token_source_extent.end.colno
        result.source_extent.end.lineno = self.current_token_source_extent.end.lineno
        result.source_extent.end.byteno = self.current_token_source_extent.end.byteno

        return result

    def read_token(self) -> Token:
        # Consume all whitespace
        while self.has_input():
            # Keep updating the token start location until we exhaust all whitespace
            self.mark_token_start()
            c = self.read_char()
            if not c.isspace():
                break
        else:
            return self.make_token(EOF)
        if c == '"':
            return self.read_string()
        if c == "-":
            if self.has_input() and self.peek_char() == "-":
                self.read_comment()
                # Need to start reading a new token
                return self.read_token()
            return self.read_op(c)
        if c == "#":
            return self.make_token(Hash)
        if c == "~":
            if self.has_input() and self.peek_char() == "~":
                self.read_char()
                return self.read_bytes()
            raise ParseError(f"unexpected token {c!r}")
        if c.isdigit():
            return self.read_number(c)
        if c in "()[]{}":
            custom = {
                "(": LeftParen,
                ")": RightParen,
                "{": LeftBrace,
                "}": RightBrace,
                "[": LeftBracket,
                "]": RightBracket,
            }
            return self.make_token(custom[c])
        if c in OPER_CHARS:
            return self.read_op(c)
        if is_identifier_char(c):
            return self.read_var(c)
        raise InvalidTokenError(
            SourceExtent(
                start=SourceLocation(
                    lineno=self.current_token_source_extent.start.lineno,
                    colno=self.current_token_source_extent.start.colno,
                    byteno=self.current_token_source_extent.start.byteno,
                ),
                end=SourceLocation(
                    lineno=self.current_token_source_extent.end.lineno,
                    colno=self.current_token_source_extent.end.colno,
                    byteno=self.current_token_source_extent.end.byteno,
                ),
            )
        )

    def read_string(self) -> Token:
        buf = ""
        while self.has_input():
            if (c := self.read_char()) == '"':
                break
            buf += c
        else:
            raise UnexpectedEOFError("while reading string")
        return self.make_token(StringLit, buf)

    def read_comment(self) -> None:
        while self.has_input() and self.read_char() != "\n":
            pass

    def read_number(self, first_digit: str) -> Token:
        # TODO: Support floating point numbers with no integer part
        buf = first_digit
        has_decimal = False
        while self.has_input():
            c = self.peek_char()
            if c == ".":
                if has_decimal:
                    raise ParseError(f"unexpected token {c!r}")
                has_decimal = True
            elif not c.isdigit():
                break
            self.read_char()
            buf += c

        if has_decimal:
            return self.make_token(FloatLit, float(buf))
        return self.make_token(IntLit, int(buf))

    def _starts_operator(self, buf: str) -> bool:
        # TODO(max): Rewrite using trie
        return any(op.startswith(buf) for op in PS.keys())

    def read_op(self, first_char: str) -> Token:
        buf = first_char
        while self.has_input():
            c = self.peek_char()
            if not self._starts_operator(buf + c):
                break
            self.read_char()
            buf += c
        if buf in PS.keys():
            return self.make_token(Operator, buf)
        raise ParseError(f"unexpected token {buf!r}")

    def read_var(self, first_char: str) -> Token:
        buf = first_char
        while self.has_input() and is_identifier_char(c := self.peek_char()):
            self.read_char()
            buf += c
        return self.make_token(Name, buf)

    def read_bytes(self) -> Token:
        buf = ""
        while self.has_input():
            if self.peek_char().isspace():
                break
            buf += self.read_char()
        base, _, value = buf.rpartition("'")
        return self.make_token(BytesLit, value, int(base) if base else 64)


PEEK_EMPTY = object()


class Peekable:
    def __init__(self, iterator: Iterator[Any]) -> None:
        self.iterator = iterator
        self.cache = PEEK_EMPTY

    def __iter__(self) -> Iterator[Any]:
        return self

    def __next__(self) -> Any:
        if self.cache is not PEEK_EMPTY:
            result = self.cache
            self.cache = PEEK_EMPTY
            return result
        return next(self.iterator)

    def peek(self) -> Any:
        result = self.cache = next(self)
        return result


class PeekableTests(unittest.TestCase):
    def test_can_create_peekable(self) -> None:
        Peekable(iter([1, 2, 3]))

    def test_can_iterate_over_peekable(self) -> None:
        sequence = [1, 2, 3]
        for idx, e in enumerate(Peekable(iter(sequence))):
            self.assertEqual(sequence[idx], e)

    def test_peek_next(self) -> None:
        iterator = Peekable(iter([1, 2, 3]))
        self.assertEqual(iterator.peek(), 1)
        self.assertEqual(next(iterator), 1)
        self.assertEqual(iterator.peek(), 2)
        self.assertEqual(next(iterator), 2)
        self.assertEqual(iterator.peek(), 3)
        self.assertEqual(next(iterator), 3)
        with self.assertRaises(StopIteration):
            iterator.peek()
        with self.assertRaises(StopIteration):
            next(iterator)

    def test_can_peek_peekable(self) -> None:
        sequence = [1, 2, 3]
        p = Peekable(iter(sequence))
        self.assertEqual(p.peek(), 1)
        # Ensure we can peek repeatedly
        self.assertEqual(p.peek(), 1)
        for idx, e in enumerate(p):
            self.assertEqual(sequence[idx], e)

    def test_peek_on_empty_peekable_raises_stop_iteration(self) -> None:
        empty = Peekable(iter([]))
        with self.assertRaises(StopIteration):
            empty.peek()

    def test_next_on_empty_peekable_raises_stop_iteration(self) -> None:
        empty = Peekable(iter([]))
        with self.assertRaises(StopIteration):
            next(empty)


def tokenize(x: str) -> Peekable:
    lexer = Lexer(x)
    tokens = []
    while (token := lexer.read_token()) and not isinstance(token, EOF):
        tokens.append(token)
    return Peekable(iter(tokens))


@dataclass(frozen=True)
class Prec:
    pl: float
    pr: float


def lp(n: float) -> Prec:
    # TODO(max): Rewrite
    return Prec(n, n - 0.1)


def rp(n: float) -> Prec:
    # TODO(max): Rewrite
    return Prec(n, n + 0.1)


def np(n: float) -> Prec:
    # TODO(max): Rewrite
    return Prec(n, n)


def xp(n: float) -> Prec:
    # TODO(max): Rewrite
    return Prec(n, 0)


PS = {
    "::": lp(2000),
    "@": rp(1001),
    "": rp(1000),
    ">>": lp(14),
    "<<": lp(14),
    "^": rp(13),
    "*": rp(12),
    "/": rp(12),
    "//": lp(12),
    "%": lp(12),
    "+": lp(11),
    "-": lp(11),
    ">*": rp(10),
    "++": rp(10),
    ">+": lp(10),
    "+<": rp(10),
    "==": np(9),
    "/=": np(9),
    "<": np(9),
    ">": np(9),
    "<=": np(9),
    ">=": np(9),
    "&&": rp(8),
    "||": rp(7),
    "|>": rp(6),
    "<|": lp(6),
    "#": lp(5.5),
    "->": lp(5),
    "|": rp(4.5),
    ":": lp(4.5),
    "=": rp(4),
    "!": lp(3),
    ".": rp(3),
    "?": rp(3),
    ",": xp(1),
    # TODO: Fix precedence for spread
    "...": xp(0),
}


HIGHEST_PREC: float = max(max(p.pl, p.pr) for p in PS.values())


OPER_CHARS = set("".join(PS.keys()))
assert " " not in OPER_CHARS


class SyntacticError(Exception):
    pass


class ParseError(SyntacticError):
    pass


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class UnexpectedTokenError(ParseError):
    unexpected_token: Token


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class InvalidTokenError(ParseError):
    unexpected_token: SourceExtent = dataclasses.field(default_factory=SourceExtent, compare=False)


# TODO(max): Replace with EOFError?
class UnexpectedEOFError(ParseError):
    pass


def parse_assign(tokens: Peekable, p: float = 0) -> "Assign":
    assign = parse_binary(tokens, p)
    if isinstance(assign, Spread):
        return Assign(Var("..."), assign)
    if not isinstance(assign, Assign):
        raise ParseError("failed to parse variable assignment in record constructor")
    return assign


def gensym() -> str:
    gensym.counter += 1  # type: ignore
    return f"$v{gensym.counter}"  # type: ignore


def gensym_reset() -> None:
    gensym.counter = -1  # type: ignore


gensym_reset()


def parse_unary(tokens: Peekable, p: float) -> "Object":
    token = next(tokens)
    l: Object
    if isinstance(token, IntLit):
        return Int(token.value)
    elif isinstance(token, FloatLit):
        return Float(token.value)
    elif isinstance(token, Name):
        # TODO: Handle kebab case vars
        return Var(token.value)
    elif isinstance(token, Hash):
        if isinstance(variant := next(tokens), Name):
            # It needs to be higher than the precedence of the -> operator so that
            # we can match variants in MatchFunction
            # It needs to be higher than the precedence of the && operator so that
            # we can use #true() and #false() in boolean expressions
            # It needs to be higher than the precedence of juxtaposition so that
            # f #true() #false() is parsed as f(TRUE)(FALSE)
            return Variant(variant.value, parse_binary(tokens, PS[""].pr + 1))
        else:
            raise UnexpectedTokenError(variant)
    elif isinstance(token, BytesLit):
        base = token.base
        if base == 85:
            l = Bytes(base64.b85decode(token.value))
        elif base == 64:
            l = Bytes(base64.b64decode(token.value))
        elif base == 32:
            l = Bytes(base64.b32decode(token.value))
        elif base == 16:
            l = Bytes(base64.b16decode(token.value))
        else:
            raise ParseError(f"unexpected base {base!r} in {token!r}")
        return l
    elif isinstance(token, StringLit):
        return String(token.value)
    elif token == Operator("..."):
        try:
            if isinstance(tokens.peek(), Name):
                return Spread(next(tokens).value)
            else:
                return Spread()
        except StopIteration:
            return Spread()
    elif token == Operator("|"):
        expr = parse_binary(tokens, PS["|"].pr)  # TODO: make this work for larger arities
        if not isinstance(expr, Function):
            raise ParseError(f"expected function in match expression {expr!r}")
        cases = [MatchCase(expr.arg, expr.body)]
        while True:
            try:
                if tokens.peek() != Operator("|"):
                    break
            except StopIteration:
                break
            next(tokens)
            expr = parse_binary(tokens, PS["|"].pr)  # TODO: make this work for larger arities
            if not isinstance(expr, Function):
                raise ParseError(f"expected function in match expression {expr!r}")
            cases.append(MatchCase(expr.arg, expr.body))
        return MatchFunction(cases)
    elif isinstance(token, LeftParen):
        if isinstance(tokens.peek(), RightParen):
            l = Hole()
        else:
            l = parse(tokens)
        next(tokens)
        return l
    elif isinstance(token, LeftBracket):
        l = List([])
        token = tokens.peek()
        if isinstance(token, RightBracket):
            next(tokens)
        else:
            l.items.append(parse_binary(tokens, 2))
            while not isinstance(next(tokens), RightBracket):
                if isinstance(l.items[-1], Spread):
                    raise ParseError("spread must come at end of list match")
                # TODO: Implement .. operator
                l.items.append(parse_binary(tokens, 2))
        return l
    elif isinstance(token, LeftBrace):
        l = Record({})
        token = tokens.peek()
        if isinstance(token, RightBrace):
            next(tokens)
        else:
            assign = parse_assign(tokens, 2)
            l.data[assign.name.name] = assign.value
            while not isinstance(next(tokens), RightBrace):
                if isinstance(assign.value, Spread):
                    raise ParseError("spread must come at end of record match")
                # TODO: Implement .. operator
                assign = parse_assign(tokens, 2)
                l.data[assign.name.name] = assign.value
        return l
    elif token == Operator("-"):
        # Unary minus
        # Precedence was chosen to be higher than binary ops so that -a op
        # b is (-a) op b and not -(a op b).
        # Precedence was chosen to be higher than function application so that
        # -a b is (-a) b and not -(a b).
        r = parse_binary(tokens, HIGHEST_PREC + 1)
        if isinstance(r, Int):
            assert r.value >= 0, "Tokens should never have negative values"
            return Int(-r.value)
        if isinstance(r, Float):
            assert r.value >= 0, "Tokens should never have negative values"
            return Float(-r.value)
        return Binop(BinopKind.SUB, Int(0), r)
    else:
        raise UnexpectedTokenError(token)


def parse_binary(tokens: Peekable, p: float) -> "Object":
    l: Object = parse_unary(tokens, p)
    while True:
        op: Token
        try:
            op = tokens.peek()
        except StopIteration:
            break
        if isinstance(op, (RightParen, RightBracket, RightBrace)):
            break
        if not isinstance(op, Operator):
            prec = PS[""]
            pl, pr = prec.pl, prec.pr
            if pl < p:
                break
            l = Apply(l, parse_binary(tokens, pr))
            continue
        prec = PS[op.value]
        pl, pr = prec.pl, prec.pr
        if pl < p:
            break
        next(tokens)
        if op == Operator("="):
            if not isinstance(l, Var):
                raise ParseError(f"expected variable in assignment {l!r}")
            l = Assign(l, parse_binary(tokens, pr))
        elif op == Operator("->"):
            l = Function(l, parse_binary(tokens, pr))
        elif op == Operator("|>"):
            l = Apply(parse_binary(tokens, pr), l)
        elif op == Operator("<|"):
            l = Apply(l, parse_binary(tokens, pr))
        elif op == Operator(">>"):
            r = parse_binary(tokens, pr)
            varname = gensym()
            l = Function(Var(varname), Apply(r, Apply(l, Var(varname))))
        elif op == Operator("<<"):
            r = parse_binary(tokens, pr)
            varname = gensym()
            l = Function(Var(varname), Apply(l, Apply(r, Var(varname))))
        elif op == Operator("."):
            l = Where(l, parse_binary(tokens, pr))
        elif op == Operator("?"):
            l = Assert(l, parse_binary(tokens, pr))
        elif op == Operator("@"):
            # TODO: revisit whether to use @ or . for field access
            l = Access(l, parse_binary(tokens, pr))
        else:
            assert isinstance(op, Operator)
            l = Binop(BinopKind.from_str(op.value), l, parse_binary(tokens, pr))
    return l


def parse(tokens: Peekable) -> "Object":
    try:
        return parse_binary(tokens, 0)
    except StopIteration:
        raise UnexpectedEOFError("unexpected end of input")


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Object:
    def __str__(self) -> str:
        return pretty(self)


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Int(Object):
    value: int


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Float(Object):
    value: float


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class String(Object):
    value: str


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Bytes(Object):
    value: bytes


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Var(Object):
    name: str


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Hole(Object):
    pass


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Spread(Object):
    name: Optional[str] = None


Env = Mapping[str, Object]


class BinopKind(enum.Enum):
    ADD = auto()
    SUB = auto()
    MUL = auto()
    DIV = auto()
    FLOOR_DIV = auto()
    EXP = auto()
    MOD = auto()
    EQUAL = auto()
    NOT_EQUAL = auto()
    LESS = auto()
    GREATER = auto()
    LESS_EQUAL = auto()
    GREATER_EQUAL = auto()
    BOOL_AND = auto()
    BOOL_OR = auto()
    STRING_CONCAT = auto()
    LIST_CONS = auto()
    LIST_APPEND = auto()
    RIGHT_EVAL = auto()
    HASTYPE = auto()
    PIPE = auto()
    REVERSE_PIPE = auto()

    @classmethod
    def from_str(cls, x: str) -> "BinopKind":
        return {
            "+": cls.ADD,
            "-": cls.SUB,
            "*": cls.MUL,
            "/": cls.DIV,
            "//": cls.FLOOR_DIV,
            "^": cls.EXP,
            "%": cls.MOD,
            "==": cls.EQUAL,
            "/=": cls.NOT_EQUAL,
            "<": cls.LESS,
            ">": cls.GREATER,
            "<=": cls.LESS_EQUAL,
            ">=": cls.GREATER_EQUAL,
            "&&": cls.BOOL_AND,
            "||": cls.BOOL_OR,
            "++": cls.STRING_CONCAT,
            ">+": cls.LIST_CONS,
            "+<": cls.LIST_APPEND,
            "!": cls.RIGHT_EVAL,
            ":": cls.HASTYPE,
            "|>": cls.PIPE,
            "<|": cls.REVERSE_PIPE,
        }[x]

    @classmethod
    def to_str(cls, binop_kind: "BinopKind") -> str:
        return {
            cls.ADD: "+",
            cls.SUB: "-",
            cls.MUL: "*",
            cls.DIV: "/",
            cls.EXP: "^",
            cls.MOD: "%",
            cls.EQUAL: "==",
            cls.NOT_EQUAL: "/=",
            cls.LESS: "<",
            cls.GREATER: ">",
            cls.LESS_EQUAL: "<=",
            cls.GREATER_EQUAL: ">=",
            cls.BOOL_AND: "&&",
            cls.BOOL_OR: "||",
            cls.STRING_CONCAT: "++",
            cls.LIST_CONS: ">+",
            cls.LIST_APPEND: "+<",
            cls.RIGHT_EVAL: "!",
            cls.HASTYPE: ":",
            cls.PIPE: "|>",
            cls.REVERSE_PIPE: "<|",
        }[binop_kind]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Binop(Object):
    op: BinopKind
    left: Object
    right: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class List(Object):
    items: typing.List[Object]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Assign(Object):
    name: Var
    value: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Function(Object):
    arg: Object
    body: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Apply(Object):
    func: Object
    arg: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Where(Object):
    body: Object
    binding: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Assert(Object):
    value: Object
    cond: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class EnvObject(Object):
    env: Env

    def __str__(self) -> str:
        return f"EnvObject(keys={self.env.keys()})"


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class MatchCase(Object):
    pattern: Object
    body: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class MatchFunction(Object):
    cases: typing.List[MatchCase]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Relocation(Object):
    name: str


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class NativeFunctionRelocation(Relocation):
    pass


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class NativeFunction(Object):
    name: str
    func: Callable[[Object], Object]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Closure(Object):
    env: Env
    func: Union[Function, MatchFunction]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Record(Object):
    data: Dict[str, Object]


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Access(Object):
    obj: Object
    at: Object


@dataclass(eq=True, frozen=True, unsafe_hash=True)
class Variant(Object):
    tag: str
    value: Object


tags = [
    TYPE_SHORT := b"i",  # fits in 64 bits
    TYPE_LONG := b"l",  # bignum
    TYPE_FLOAT := b"d",
    TYPE_STRING := b"s",
    TYPE_REF := b"r",
    TYPE_LIST := b"[",
    TYPE_RECORD := b"{",
    TYPE_VARIANT := b"#",
    TYPE_VAR := b"v",
    TYPE_FUNCTION := b"f",
    TYPE_MATCH_FUNCTION := b"m",
    TYPE_CLOSURE := b"c",
    TYPE_BYTES := b"b",
    TYPE_HOLE := b"(",
    TYPE_ASSIGN := b"=",
    TYPE_BINOP := b"+",
    TYPE_APPLY := b" ",
    TYPE_WHERE := b".",
    TYPE_ACCESS := b"@",
    TYPE_SPREAD := b"S",
    TYPE_NAMED_SPREAD := b"R",
]
FLAG_REF = 0x80


BITS_PER_BYTE = 8
BYTES_PER_DIGIT = 8
BITS_PER_DIGIT = BYTES_PER_DIGIT * BITS_PER_BYTE
DIGIT_MASK = (1 << BITS_PER_DIGIT) - 1


def ref(tag: bytes) -> bytes:
    return (tag[0] | FLAG_REF).to_bytes(1, "little")


tags = tags + [ref(v) for v in tags]
assert len(tags) == len(set(tags)), "Duplicate tags"
assert all(len(v) == 1 for v in tags), "Tags must be 1 byte"
assert all(isinstance(v, bytes) for v in tags)


def zigzag_encode(val: int) -> int:
    if val < 0:
        return -2 * val - 1
    return 2 * val


def zigzag_decode(val: int) -> int:
    if val & 1 == 1:
        return -val // 2
    return val // 2


@dataclass
class Serializer:
    refs: typing.List[Object] = dataclasses.field(default_factory=list)
    output: bytearray = dataclasses.field(default_factory=bytearray)

    def ref(self, obj: Object) -> Optional[int]:
        for idx, ref in enumerate(self.refs):
            if ref is obj:
                return idx
        return None

    def add_ref(self, ty: bytes, obj: Object) -> int:
        assert len(ty) == 1
        assert self.ref(obj) is None
        self.emit(ref(ty))
        result = len(self.refs)
        self.refs.append(obj)
        return result

    def emit(self, obj: bytes) -> None:
        self.output.extend(obj)

    def _fits_in_nbits(self, obj: int, nbits: int) -> bool:
        return -(1 << (nbits - 1)) <= obj < (1 << (nbits - 1))

    def _short(self, number: int) -> bytes:
        # From Peter Ruibal, https://github.com/fmoo/python-varint
        number = zigzag_encode(number)
        buf = bytearray()
        while True:
            towrite = number & 0x7F
            number >>= 7
            if number:
                buf.append(towrite | 0x80)
            else:
                buf.append(towrite)
                break
        return bytes(buf)

    def _long(self, number: int) -> bytes:
        digits = []
        number = zigzag_encode(number)
        while number:
            digits.append(number & DIGIT_MASK)
            number >>= BITS_PER_DIGIT
        buf = bytearray(self._short(len(digits)))
        for digit in digits:
            buf.extend(digit.to_bytes(BYTES_PER_DIGIT, "little"))
        return bytes(buf)

    def _string(self, obj: str) -> bytes:
        encoded = obj.encode("utf-8")
        return self._short(len(encoded)) + encoded

    def serialize(self, obj: Object) -> None:
        assert isinstance(obj, Object), type(obj)
        if (ref := self.ref(obj)) is not None:
            return self.emit(TYPE_REF + self._short(ref))
        if isinstance(obj, Int):
            if self._fits_in_nbits(obj.value, 64):
                self.emit(TYPE_SHORT)
                self.emit(self._short(obj.value))
                return
            self.emit(TYPE_LONG)
            self.emit(self._long(obj.value))
            return
        if isinstance(obj, String):
            return self.emit(TYPE_STRING + self._string(obj.value))
        if isinstance(obj, List):
            self.add_ref(TYPE_LIST, obj)
            self.emit(self._short(len(obj.items)))
            for item in obj.items:
                self.serialize(item)
            return
        if isinstance(obj, Variant):
            # TODO(max): Determine if this should be a ref
            self.emit(TYPE_VARIANT)
            # TODO(max): String pool (via refs) for strings longer than some length?
            self.emit(self._string(obj.tag))
            return self.serialize(obj.value)
        if isinstance(obj, Record):
            # TODO(max): Determine if this should be a ref
            self.emit(TYPE_RECORD)
            self.emit(self._short(len(obj.data)))
            for key, value in obj.data.items():
                self.emit(self._string(key))
                self.serialize(value)
            return
        if isinstance(obj, Var):
            return self.emit(TYPE_VAR + self._string(obj.name))
        if isinstance(obj, Function):
            self.emit(TYPE_FUNCTION)
            self.serialize(obj.arg)
            return self.serialize(obj.body)
        if isinstance(obj, MatchFunction):
            self.emit(TYPE_MATCH_FUNCTION)
            self.emit(self._short(len(obj.cases)))
            for case in obj.cases:
                self.serialize(case.pattern)
                self.serialize(case.body)
            return
        if isinstance(obj, Closure):
            self.add_ref(TYPE_CLOSURE, obj)
            self.serialize(obj.func)
            self.emit(self._short(len(obj.env)))
            for key, value in obj.env.items():
                self.emit(self._string(key))
                self.serialize(value)
            return
        if isinstance(obj, Bytes):
            self.emit(TYPE_BYTES)
            self.emit(self._short(len(obj.value)))
            self.emit(obj.value)
            return
        if isinstance(obj, Float):
            self.emit(TYPE_FLOAT)
            self.emit(struct.pack("<d", obj.value))
            return
        if isinstance(obj, Hole):
            self.emit(TYPE_HOLE)
            return
        if isinstance(obj, Assign):
            self.emit(TYPE_ASSIGN)
            self.serialize(obj.name)
            self.serialize(obj.value)
            return
        if isinstance(obj, Binop):
            self.emit(TYPE_BINOP)
            self.emit(self._string(BinopKind.to_str(obj.op)))
            self.serialize(obj.left)
            self.serialize(obj.right)
            return
        if isinstance(obj, Apply):
            self.emit(TYPE_APPLY)
            self.serialize(obj.func)
            self.serialize(obj.arg)
            return
        if isinstance(obj, Where):
            self.emit(TYPE_WHERE)
            self.serialize(obj.body)
            self.serialize(obj.binding)
            return
        if isinstance(obj, Access):
            self.emit(TYPE_ACCESS)
            self.serialize(obj.obj)
            self.serialize(obj.at)
            return
        if isinstance(obj, Spread):
            if obj.name is not None:
                self.emit(TYPE_NAMED_SPREAD)
                self.emit(self._string(obj.name))
                return
            self.emit(TYPE_SPREAD)
            return
        raise NotImplementedError(type(obj))


@dataclass
class Deserializer:
    flat: Union[bytes, memoryview]
    idx: int = 0
    refs: typing.List[Object] = dataclasses.field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.flat, bytes):
            self.flat = memoryview(self.flat)

    def read(self, size: int) -> memoryview:
        result = memoryview(self.flat[self.idx : self.idx + size])
        self.idx += size
        return result

    def read_tag(self) -> Tuple[bytes, bool]:
        tag = self.read(1)[0]
        is_ref = bool(tag & FLAG_REF)
        return (tag & ~FLAG_REF).to_bytes(1, "little"), is_ref

    def _string(self) -> str:
        length = self._short()
        encoded = self.read(length)
        return str(encoded, "utf-8")

    def _short(self) -> int:
        # From Peter Ruibal, https://github.com/fmoo/python-varint
        shift = 0
        result = 0
        while True:
            i = self.read(1)[0]
            result |= (i & 0x7F) << shift
            shift += 7
            if not (i & 0x80):
                break
        return zigzag_decode(result)

    def _long(self) -> int:
        num_digits = self._short()
        digits = []
        for _ in range(num_digits):
            digit = int.from_bytes(self.read(BYTES_PER_DIGIT), "little")
            digits.append(digit)
        result = 0
        for digit in reversed(digits):
            result <<= BITS_PER_DIGIT
            result |= digit
        return zigzag_decode(result)

    def parse(self) -> Object:
        ty, is_ref = self.read_tag()
        if ty == TYPE_REF:
            idx = self._short()
            return self.refs[idx]
        if ty == TYPE_SHORT:
            assert not is_ref
            return Int(self._short())
        if ty == TYPE_LONG:
            assert not is_ref
            return Int(self._long())
        if ty == TYPE_STRING:
            assert not is_ref
            return String(self._string())
        if ty == TYPE_LIST:
            length = self._short()
            result_list = List([])
            assert is_ref
            self.refs.append(result_list)
            for i in range(length):
                result_list.items.append(self.parse())
            return result_list
        if ty == TYPE_RECORD:
            assert not is_ref
            length = self._short()
            result_rec = Record({})
            for i in range(length):
                key = self._string()
                value = self.parse()
                result_rec.data[key] = value
            return result_rec
        if ty == TYPE_VARIANT:
            assert not is_ref
            tag = self._string()
            value = self.parse()
            return Variant(tag, value)
        if ty == TYPE_VAR:
            assert not is_ref
            return Var(self._string())
        if ty == TYPE_FUNCTION:
            assert not is_ref
            arg = self.parse()
            body = self.parse()
            return Function(arg, body)
        if ty == TYPE_MATCH_FUNCTION:
            assert not is_ref
            length = self._short()
            result_matchfun = MatchFunction([])
            for i in range(length):
                pattern = self.parse()
                body = self.parse()
                result_matchfun.cases.append(MatchCase(pattern, body))
            return result_matchfun
        if ty == TYPE_CLOSURE:
            func = self.parse()
            length = self._short()
            assert isinstance(func, (Function, MatchFunction))
            result_closure = Closure({}, func)
            assert is_ref
            self.refs.append(result_closure)
            for i in range(length):
                key = self._string()
                value = self.parse()
                assert isinstance(result_closure.env, dict)  # For mypy
                result_closure.env[key] = value
            return result_closure
        if ty == TYPE_BYTES:
            assert not is_ref
            length = self._short()
            return Bytes(self.read(length))
        if ty == TYPE_FLOAT:
            assert not is_ref
            return Float(struct.unpack("<d", self.read(8))[0])
        if ty == TYPE_HOLE:
            assert not is_ref
            return Hole()
        if ty == TYPE_ASSIGN:
            assert not is_ref
            name = self.parse()
            value = self.parse()
            assert isinstance(name, Var)
            return Assign(name, value)
        if ty == TYPE_BINOP:
            assert not is_ref
            op = BinopKind.from_str(self._string())
            left = self.parse()
            right = self.parse()
            return Binop(op, left, right)
        if ty == TYPE_APPLY:
            assert not is_ref
            func = self.parse()
            arg = self.parse()
            return Apply(func, arg)
        if ty == TYPE_WHERE:
            assert not is_ref
            body = self.parse()
            binding = self.parse()
            return Where(body, binding)
        if ty == TYPE_ACCESS:
            assert not is_ref
            obj = self.parse()
            at = self.parse()
            return Access(obj, at)
        if ty == TYPE_SPREAD:
            return Spread()
        if ty == TYPE_NAMED_SPREAD:
            return Spread(self._string())
        raise NotImplementedError(bytes(ty))


TRUE = Variant("true", Hole())


FALSE = Variant("false", Hole())


def unpack_number(obj: Object) -> Union[int, float]:
    if not isinstance(obj, (Int, Float)):
        raise TypeError(f"expected Int or Float, got {type(obj).__name__}")
    return obj.value


def eval_number(env: Env, exp: Object) -> Union[int, float]:
    result = eval_exp(env, exp)
    return unpack_number(result)


def eval_str(env: Env, exp: Object) -> str:
    result = eval_exp(env, exp)
    if not isinstance(result, String):
        raise TypeError(f"expected String, got {type(result).__name__}")
    return result.value


def eval_bool(env: Env, exp: Object) -> bool:
    result = eval_exp(env, exp)
    if not isinstance(result, Variant):
        raise TypeError(f"expected #true or #false, got {type(result).__name__}")
    if result.tag not in ("true", "false"):
        raise TypeError(f"expected #true or #false, got {type(result).__name__}")
    return result.tag == "true"


def eval_list(env: Env, exp: Object) -> typing.List[Object]:
    result = eval_exp(env, exp)
    if not isinstance(result, List):
        raise TypeError(f"expected List, got {type(result).__name__}")
    return result.items


def make_bool(x: bool) -> Object:
    return TRUE if x else FALSE


def wrap_inferred_number_type(x: Union[int, float]) -> Object:
    # TODO: Since this is intended to be a reference implementation
    # we should avoid relying heavily on Python's implementation of
    # arithmetic operations, type inference, and multiple dispatch.
    # Update this to make the interpreter more language agnostic.
    if isinstance(x, int):
        return Int(x)
    return Float(x)


BINOP_HANDLERS: Dict[BinopKind, Callable[[Env, Object, Object], Object]] = {
    BinopKind.ADD: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) + eval_number(env, y)),
    BinopKind.SUB: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) - eval_number(env, y)),
    BinopKind.MUL: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) * eval_number(env, y)),
    BinopKind.DIV: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) / eval_number(env, y)),
    BinopKind.FLOOR_DIV: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) // eval_number(env, y)),
    BinopKind.EXP: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) ** eval_number(env, y)),
    BinopKind.MOD: lambda env, x, y: wrap_inferred_number_type(eval_number(env, x) % eval_number(env, y)),
    BinopKind.EQUAL: lambda env, x, y: make_bool(eval_exp(env, x) == eval_exp(env, y)),
    BinopKind.NOT_EQUAL: lambda env, x, y: make_bool(eval_exp(env, x) != eval_exp(env, y)),
    BinopKind.LESS: lambda env, x, y: make_bool(eval_number(env, x) < eval_number(env, y)),
    BinopKind.GREATER: lambda env, x, y: make_bool(eval_number(env, x) > eval_number(env, y)),
    BinopKind.LESS_EQUAL: lambda env, x, y: make_bool(eval_number(env, x) <= eval_number(env, y)),
    BinopKind.GREATER_EQUAL: lambda env, x, y: make_bool(eval_number(env, x) >= eval_number(env, y)),
    BinopKind.BOOL_AND: lambda env, x, y: make_bool(eval_bool(env, x) and eval_bool(env, y)),
    BinopKind.BOOL_OR: lambda env, x, y: make_bool(eval_bool(env, x) or eval_bool(env, y)),
    BinopKind.STRING_CONCAT: lambda env, x, y: String(eval_str(env, x) + eval_str(env, y)),
    BinopKind.LIST_CONS: lambda env, x, y: List([eval_exp(env, x)] + eval_list(env, y)),
    BinopKind.LIST_APPEND: lambda env, x, y: List(eval_list(env, x) + [eval_exp(env, y)]),
    BinopKind.RIGHT_EVAL: lambda env, x, y: eval_exp(env, y),
}


class MatchError(Exception):
    pass


def match(obj: Object, pattern: Object) -> Optional[Env]:
    if isinstance(pattern, Hole):
        return {} if isinstance(obj, Hole) else None
    if isinstance(pattern, Int):
        return {} if isinstance(obj, Int) and obj.value == pattern.value else None
    if isinstance(pattern, Float):
        raise MatchError("pattern matching is not supported for Floats")
    if isinstance(pattern, String):
        return {} if isinstance(obj, String) and obj.value == pattern.value else None
    if isinstance(pattern, Var):
        return {pattern.name: obj}
    if isinstance(pattern, Variant):
        if not isinstance(obj, Variant):
            return None
        if obj.tag != pattern.tag:
            return None
        return match(obj.value, pattern.value)
    if isinstance(pattern, Record):
        if not isinstance(obj, Record):
            return None
        result: Env = {}
        use_spread = False
        seen_keys: set[str] = set()
        for key, pattern_item in pattern.data.items():
            if isinstance(pattern_item, Spread):
                use_spread = True
                if pattern_item.name is not None:
                    assert isinstance(result, dict)  # for .update()
                    rest_keys = set(obj.data.keys()) - seen_keys
                    result.update({pattern_item.name: Record({key: obj.data[key] for key in rest_keys})})
                break
            seen_keys.add(key)
            obj_item = obj.data.get(key)
            if obj_item is None:
                return None
            part = match(obj_item, pattern_item)
            if part is None:
                return None
            assert isinstance(result, dict)  # for .update()
            result.update(part)
        if not use_spread and len(pattern.data) != len(obj.data):
            return None
        return result
    if isinstance(pattern, List):
        if not isinstance(obj, List):
            return None
        result: Env = {}  # type: ignore
        use_spread = False
        for i, pattern_item in enumerate(pattern.items):
            if isinstance(pattern_item, Spread):
                use_spread = True
                if pattern_item.name is not None:
                    assert isinstance(result, dict)  # for .update()
                    result.update({pattern_item.name: List(obj.items[i:])})
                break
            if i >= len(obj.items):
                return None
            obj_item = obj.items[i]
            part = match(obj_item, pattern_item)
            if part is None:
                return None
            assert isinstance(result, dict)  # for .update()
            result.update(part)
        if not use_spread and len(pattern.items) != len(obj.items):
            return None
        return result
    raise NotImplementedError(f"match not implemented for {type(pattern).__name__}")


def free_in(exp: Object) -> Set[str]:
    if isinstance(exp, (Int, Float, String, Bytes, Hole, NativeFunction)):
        return set()
    if isinstance(exp, Variant):
        return free_in(exp.value)
    if isinstance(exp, Var):
        return {exp.name}
    if isinstance(exp, Spread):
        if exp.name is not None:
            return {exp.name}
        return set()
    if isinstance(exp, Binop):
        return free_in(exp.left) | free_in(exp.right)
    if isinstance(exp, List):
        if not exp.items:
            return set()
        return set.union(*(free_in(item) for item in exp.items))
    if isinstance(exp, Record):
        if not exp.data:
            return set()
        return set.union(*(free_in(value) for key, value in exp.data.items()))
    if isinstance(exp, Function):
        assert isinstance(exp.arg, Var)
        return free_in(exp.body) - {exp.arg.name}
    if isinstance(exp, MatchFunction):
        if not exp.cases:
            return set()
        return set.union(*(free_in(case) for case in exp.cases))
    if isinstance(exp, MatchCase):
        return free_in(exp.body) - free_in(exp.pattern)
    if isinstance(exp, Apply):
        return free_in(exp.func) | free_in(exp.arg)
    if isinstance(exp, Access):
        # For records, y is not free in x@y; it is a field name.
        # For lists, y *is* free in x@y; it is an index expression (could be a
        # var).
        # For now, we'll assume it might be an expression and mark it as a
        # (possibly extra) freevar.
        return free_in(exp.obj) | free_in(exp.at)
    if isinstance(exp, Where):
        assert isinstance(exp.binding, Assign)
        return (free_in(exp.body) - {exp.binding.name.name}) | free_in(exp.binding)
    if isinstance(exp, Assign):
        return free_in(exp.value)
    if isinstance(exp, Closure):
        # TODO(max): Should this remove the set of keys in the closure env?
        return free_in(exp.func)
    raise NotImplementedError(("free_in", type(exp)))


def improve_closure(closure: Closure) -> Closure:
    freevars = free_in(closure.func)
    env = {boundvar: value for boundvar, value in closure.env.items() if boundvar in freevars}
    return Closure(env, closure.func)


def eval_exp(env: Env, exp: Object) -> Object:
    logger.debug(exp)
    if isinstance(exp, (Int, Float, String, Bytes, Hole, Closure, NativeFunction)):
        return exp
    if isinstance(exp, Variant):
        return Variant(exp.tag, eval_exp(env, exp.value))
    if isinstance(exp, Var):
        value = env.get(exp.name)
        if value is None:
            raise NameError(f"name '{exp.name}' is not defined")
        return value
    if isinstance(exp, Binop):
        handler = BINOP_HANDLERS.get(exp.op)
        if handler is None:
            raise NotImplementedError(f"no handler for {exp.op}")
        return handler(env, exp.left, exp.right)
    if isinstance(exp, List):
        return List([eval_exp(env, item) for item in exp.items])
    if isinstance(exp, Record):
        return Record({k: eval_exp(env, exp.data[k]) for k in exp.data})
    if isinstance(exp, Assign):
        # TODO(max): Rework this. There's something about matching that we need
        # to figure out and implement.
        assert isinstance(exp.name, Var)
        value = eval_exp(env, exp.value)
        if isinstance(value, Closure):
            # We want functions to be able to call themselves without using the
            # Y combinator or similar, so we bind functions (and only
            # functions) using a letrec-like strategy. We augment their
            # captured environment with a binding to themselves.
            assert isinstance(value.env, dict)
            value.env[exp.name.name] = value
            # We still improve_closure here even though we also did it on
            # Closure creation because the Closure might not need a binding for
            # itself (it might not be recursive).
            value = improve_closure(value)
        return EnvObject({**env, exp.name.name: value})
    if isinstance(exp, Where):
        assert isinstance(exp.binding, Assign)
        res_env = eval_exp(env, exp.binding)
        assert isinstance(res_env, EnvObject)
        new_env = {**env, **res_env.env}
        return eval_exp(new_env, exp.body)
    if isinstance(exp, Assert):
        cond = eval_exp(env, exp.cond)
        if cond != TRUE:
            raise AssertionError(f"condition {exp.cond} failed")
        return eval_exp(env, exp.value)
    if isinstance(exp, Function):
        if not isinstance(exp.arg, Var):
            raise RuntimeError(f"expected variable in function definition {exp.arg}")
        value = Closure(env, exp)
        value = improve_closure(value)
        return value
    if isinstance(exp, MatchFunction):
        value = Closure(env, exp)
        value = improve_closure(value)
        return value
    if isinstance(exp, Apply):
        if isinstance(exp.func, Var) and exp.func.name == "$$quote":
            return exp.arg
        callee = eval_exp(env, exp.func)
        arg = eval_exp(env, exp.arg)
        if isinstance(callee, NativeFunction):
            return callee.func(arg)
        if not isinstance(callee, Closure):
            raise TypeError(f"attempted to apply a non-closure of type {type(callee).__name__}")
        if isinstance(callee.func, Function):
            assert isinstance(callee.func.arg, Var)
            new_env = {**callee.env, callee.func.arg.name: arg}
            return eval_exp(new_env, callee.func.body)
        elif isinstance(callee.func, MatchFunction):
            for case in callee.func.cases:
                m = match(arg, case.pattern)
                if m is None:
                    continue
                return eval_exp({**callee.env, **m}, case.body)
            raise MatchError("no matching cases")
        else:
            raise TypeError(f"attempted to apply a non-function of type {type(callee.func).__name__}")
    if isinstance(exp, Access):
        obj = eval_exp(env, exp.obj)
        if isinstance(obj, Record):
            if not isinstance(exp.at, Var):
                raise TypeError(f"cannot access record field using {type(exp.at).__name__}, expected a field name")
            if exp.at.name not in obj.data:
                raise NameError(f"no assignment to {exp.at.name} found in record")
            return obj.data[exp.at.name]
        elif isinstance(obj, List):
            access_at = eval_exp(env, exp.at)
            if not isinstance(access_at, Int):
                raise TypeError(f"cannot index into list using type {type(access_at).__name__}, expected integer")
            if access_at.value < 0 or access_at.value >= len(obj.items):
                raise ValueError(f"index {access_at.value} out of bounds for list")
            return obj.items[access_at.value]
        raise TypeError(f"attempted to access from type {type(obj).__name__}")
    elif isinstance(exp, Spread):
        raise RuntimeError("cannot evaluate a spread")
    raise NotImplementedError(f"eval_exp not implemented for {exp}")


class ScrapMonad:
    def __init__(self, env: Env) -> None:
        assert isinstance(env, dict)  # for .copy()
        self.env: Env = env.copy()

    def bind(self, exp: Object) -> Tuple[Object, "ScrapMonad"]:
        env = self.env
        result = eval_exp(env, exp)
        if isinstance(result, EnvObject):
            return result, ScrapMonad({**env, **result.env})
        return result, ScrapMonad({**env, "_": result})


class TokenizerTests(unittest.TestCase):
    def test_tokenize_digit(self) -> None:
        self.assertEqual(list(tokenize("1")), [IntLit(1)])

    def test_tokenize_multiple_digits(self) -> None:
        self.assertEqual(list(tokenize("123")), [IntLit(123)])

    def test_tokenize_negative_int(self) -> None:
        self.assertEqual(list(tokenize("-123")), [Operator("-"), IntLit(123)])

    def test_tokenize_float(self) -> None:
        self.assertEqual(list(tokenize("3.14")), [FloatLit(3.14)])

    def test_tokenize_negative_float(self) -> None:
        self.assertEqual(list(tokenize("-3.14")), [Operator("-"), FloatLit(3.14)])

    @unittest.skip("TODO: support floats with no integer part")
    def test_tokenize_float_with_no_integer_part(self) -> None:
        self.assertEqual(list(tokenize(".14")), [FloatLit(0.14)])

    def test_tokenize_float_with_no_decimal_part(self) -> None:
        self.assertEqual(list(tokenize("10.")), [FloatLit(10.0)])

    def test_tokenize_float_with_multiple_decimal_points_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("unexpected token '.'")):
            list(tokenize("1.0.1"))

    def test_tokenize_binop(self) -> None:
        self.assertEqual(list(tokenize("1 + 2")), [IntLit(1), Operator("+"), IntLit(2)])

    def test_tokenize_binop_no_spaces(self) -> None:
        self.assertEqual(list(tokenize("1+2")), [IntLit(1), Operator("+"), IntLit(2)])

    def test_tokenize_two_oper_chars_returns_two_ops(self) -> None:
        self.assertEqual(list(tokenize(",:")), [Operator(","), Operator(":")])

    def test_tokenize_binary_sub_no_spaces(self) -> None:
        self.assertEqual(list(tokenize("1-2")), [IntLit(1), Operator("-"), IntLit(2)])

    def test_tokenize_binop_var(self) -> None:
        ops = ["+", "-", "*", "/", "^", "%", "==", "/=", "<", ">", "<=", ">=", "&&", "||", "++", ">+", "+<"]
        for op in ops:
            with self.subTest(op=op):
                self.assertEqual(list(tokenize(f"a {op} b")), [Name("a"), Operator(op), Name("b")])
                self.assertEqual(list(tokenize(f"a{op}b")), [Name("a"), Operator(op), Name("b")])

    def test_tokenize_var(self) -> None:
        self.assertEqual(list(tokenize("abc")), [Name("abc")])

    @unittest.skip("TODO: make this fail to tokenize")
    def test_tokenize_var_with_quote(self) -> None:
        self.assertEqual(list(tokenize("sha1'abc")), [Name("sha1'abc")])

    def test_tokenize_dollar_sha1_var(self) -> None:
        self.assertEqual(list(tokenize("$sha1'foo")), [Name("$sha1'foo")])

    def test_tokenize_dollar_dollar_var(self) -> None:
        self.assertEqual(list(tokenize("$$bills")), [Name("$$bills")])

    def test_tokenize_dot_dot_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("unexpected token '..'")):
            list(tokenize(".."))

    def test_tokenize_spread(self) -> None:
        self.assertEqual(list(tokenize("...")), [Operator("...")])

    def test_ignore_whitespace(self) -> None:
        self.assertEqual(list(tokenize("1\n+\t2")), [IntLit(1), Operator("+"), IntLit(2)])

    def test_ignore_line_comment(self) -> None:
        self.assertEqual(list(tokenize("-- 1\n2")), [IntLit(2)])

    def test_tokenize_string(self) -> None:
        self.assertEqual(list(tokenize('"hello"')), [StringLit("hello")])

    def test_tokenize_string_with_spaces(self) -> None:
        self.assertEqual(list(tokenize('"hello world"')), [StringLit("hello world")])

    def test_tokenize_string_missing_end_quote_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(UnexpectedEOFError, "while reading string"):
            list(tokenize('"hello'))

    def test_tokenize_with_trailing_whitespace(self) -> None:
        self.assertEqual(list(tokenize("- ")), [Operator("-")])
        self.assertEqual(list(tokenize("-- ")), [])
        self.assertEqual(list(tokenize("+ ")), [Operator("+")])
        self.assertEqual(list(tokenize("123 ")), [IntLit(123)])
        self.assertEqual(list(tokenize("abc ")), [Name("abc")])
        self.assertEqual(list(tokenize("[ ")), [LeftBracket()])
        self.assertEqual(list(tokenize("] ")), [RightBracket()])

    def test_tokenize_empty_list(self) -> None:
        self.assertEqual(list(tokenize("[ ]")), [LeftBracket(), RightBracket()])

    def test_tokenize_empty_list_with_spaces(self) -> None:
        self.assertEqual(list(tokenize("[ ]")), [LeftBracket(), RightBracket()])

    def test_tokenize_list_with_items(self) -> None:
        self.assertEqual(
            list(tokenize("[ 1 , 2 ]")), [LeftBracket(), IntLit(1), Operator(","), IntLit(2), RightBracket()]
        )

    def test_tokenize_list_with_no_spaces(self) -> None:
        self.assertEqual(list(tokenize("[1,2]")), [LeftBracket(), IntLit(1), Operator(","), IntLit(2), RightBracket()])

    def test_tokenize_function(self) -> None:
        self.assertEqual(
            list(tokenize("a -> b -> a + b")),
            [Name("a"), Operator("->"), Name("b"), Operator("->"), Name("a"), Operator("+"), Name("b")],
        )

    def test_tokenize_function_with_no_spaces(self) -> None:
        self.assertEqual(
            list(tokenize("a->b->a+b")),
            [Name("a"), Operator("->"), Name("b"), Operator("->"), Name("a"), Operator("+"), Name("b")],
        )

    def test_tokenize_where(self) -> None:
        self.assertEqual(list(tokenize("a . b")), [Name("a"), Operator("."), Name("b")])

    def test_tokenize_assert(self) -> None:
        self.assertEqual(list(tokenize("a ? b")), [Name("a"), Operator("?"), Name("b")])

    def test_tokenize_hastype(self) -> None:
        self.assertEqual(list(tokenize("a : b")), [Name("a"), Operator(":"), Name("b")])

    def test_tokenize_minus_returns_minus(self) -> None:
        self.assertEqual(list(tokenize("-")), [Operator("-")])

    def test_tokenize_tilde_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, "unexpected token '~'"):
            list(tokenize("~"))

    def test_tokenize_tilde_equals_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, "unexpected token '~'"):
            list(tokenize("~="))

    def test_tokenize_tilde_tilde_returns_empty_bytes(self) -> None:
        self.assertEqual(list(tokenize("~~")), [BytesLit("", 64)])

    def test_tokenize_bytes_returns_bytes_base64(self) -> None:
        self.assertEqual(list(tokenize("~~QUJD")), [BytesLit("QUJD", 64)])

    def test_tokenize_bytes_base85(self) -> None:
        self.assertEqual(list(tokenize("~~85'K|(_")), [BytesLit("K|(_", 85)])

    def test_tokenize_bytes_base64(self) -> None:
        self.assertEqual(list(tokenize("~~64'QUJD")), [BytesLit("QUJD", 64)])

    def test_tokenize_bytes_base32(self) -> None:
        self.assertEqual(list(tokenize("~~32'IFBEG===")), [BytesLit("IFBEG===", 32)])

    def test_tokenize_bytes_base16(self) -> None:
        self.assertEqual(list(tokenize("~~16'414243")), [BytesLit("414243", 16)])

    def test_tokenize_hole(self) -> None:
        self.assertEqual(list(tokenize("()")), [LeftParen(), RightParen()])

    def test_tokenize_hole_with_spaces(self) -> None:
        self.assertEqual(list(tokenize("( )")), [LeftParen(), RightParen()])

    def test_tokenize_parenthetical_expression(self) -> None:
        self.assertEqual(list(tokenize("(1+2)")), [LeftParen(), IntLit(1), Operator("+"), IntLit(2), RightParen()])

    def test_tokenize_pipe(self) -> None:
        self.assertEqual(
            list(tokenize("1 |> f . f = a -> a + 1")),
            [
                IntLit(1),
                Operator("|>"),
                Name("f"),
                Operator("."),
                Name("f"),
                Operator("="),
                Name("a"),
                Operator("->"),
                Name("a"),
                Operator("+"),
                IntLit(1),
            ],
        )

    def test_tokenize_reverse_pipe(self) -> None:
        self.assertEqual(
            list(tokenize("f <| 1 . f = a -> a + 1")),
            [
                Name("f"),
                Operator("<|"),
                IntLit(1),
                Operator("."),
                Name("f"),
                Operator("="),
                Name("a"),
                Operator("->"),
                Name("a"),
                Operator("+"),
                IntLit(1),
            ],
        )

    def test_tokenize_record_no_fields(self) -> None:
        self.assertEqual(
            list(tokenize("{ }")),
            [LeftBrace(), RightBrace()],
        )

    def test_tokenize_record_no_fields_no_spaces(self) -> None:
        self.assertEqual(
            list(tokenize("{}")),
            [LeftBrace(), RightBrace()],
        )

    def test_tokenize_record_one_field(self) -> None:
        self.assertEqual(
            list(tokenize("{ a = 4 }")),
            [LeftBrace(), Name("a"), Operator("="), IntLit(4), RightBrace()],
        )

    def test_tokenize_record_multiple_fields(self) -> None:
        self.assertEqual(
            list(tokenize('{ a = 4, b = "z" }')),
            [
                LeftBrace(),
                Name("a"),
                Operator("="),
                IntLit(4),
                Operator(","),
                Name("b"),
                Operator("="),
                StringLit("z"),
                RightBrace(),
            ],
        )

    def test_tokenize_record_access(self) -> None:
        self.assertEqual(
            list(tokenize("r@a")),
            [Name("r"), Operator("@"), Name("a")],
        )

    def test_tokenize_right_eval(self) -> None:
        self.assertEqual(list(tokenize("a!b")), [Name("a"), Operator("!"), Name("b")])

    def test_tokenize_match(self) -> None:
        self.assertEqual(
            list(tokenize("g = | 1 -> 2 | 2 -> 3")),
            [
                Name("g"),
                Operator("="),
                Operator("|"),
                IntLit(1),
                Operator("->"),
                IntLit(2),
                Operator("|"),
                IntLit(2),
                Operator("->"),
                IntLit(3),
            ],
        )

    def test_tokenize_compose(self) -> None:
        self.assertEqual(
            list(tokenize("f >> g")),
            [Name("f"), Operator(">>"), Name("g")],
        )

    def test_tokenize_compose_reverse(self) -> None:
        self.assertEqual(
            list(tokenize("f << g")),
            [Name("f"), Operator("<<"), Name("g")],
        )

    def test_first_lineno_is_one(self) -> None:
        l = Lexer("abc")
        self.assertEqual(l.lineno, 1)

    def test_first_colno_is_one(self) -> None:
        l = Lexer("abc")
        self.assertEqual(l.colno, 1)

    def test_first_line_is_empty(self) -> None:
        l = Lexer("abc")
        self.assertEqual(l.line, "")

    def test_read_char_increments_colno(self) -> None:
        l = Lexer("abc")
        l.read_char()
        self.assertEqual(l.colno, 2)
        self.assertEqual(l.lineno, 1)

    def test_read_newline_increments_lineno(self) -> None:
        l = Lexer("ab\nc")
        l.read_char()
        l.read_char()
        l.read_char()
        self.assertEqual(l.lineno, 2)
        self.assertEqual(l.colno, 1)

    def test_read_char_increments_byteno(self) -> None:
        l = Lexer("abc")
        l.read_char()
        self.assertEqual(l.byteno, 1)
        l.read_char()
        self.assertEqual(l.byteno, 2)
        l.read_char()
        self.assertEqual(l.byteno, 3)

    def test_read_char_appends_to_line(self) -> None:
        l = Lexer("ab\nc")
        l.read_char()
        l.read_char()
        self.assertEqual(l.line, "ab")
        l.read_char()
        self.assertEqual(l.line, "")

    def test_read_token_sets_start_and_end_linenos(self) -> None:
        l = Lexer("a b \n c d")
        a = l.read_token()
        b = l.read_token()
        c = l.read_token()
        d = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)

        self.assertEqual(b.source_extent.start.lineno, 1)
        self.assertEqual(b.source_extent.end.lineno, 1)

        self.assertEqual(c.source_extent.start.lineno, 2)
        self.assertEqual(c.source_extent.end.lineno, 2)

        self.assertEqual(d.source_extent.start.lineno, 2)
        self.assertEqual(d.source_extent.end.lineno, 2)

    def test_read_token_sets_source_extents_for_variables(self) -> None:
        l = Lexer("aa bbbb \n ccccc ddddddd")

        a = l.read_token()
        b = l.read_token()
        c = l.read_token()
        d = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)
        self.assertEqual(a.source_extent.start.colno, 1)
        self.assertEqual(a.source_extent.end.colno, 2)
        self.assertEqual(a.source_extent.start.byteno, 0)
        self.assertEqual(a.source_extent.end.byteno, 1)

        self.assertEqual(b.source_extent.start.lineno, 1)
        self.assertEqual(b.source_extent.end.lineno, 1)
        self.assertEqual(b.source_extent.start.colno, 4)
        self.assertEqual(b.source_extent.end.colno, 7)
        self.assertEqual(b.source_extent.start.byteno, 3)
        self.assertEqual(b.source_extent.end.byteno, 6)

        self.assertEqual(c.source_extent.start.lineno, 2)
        self.assertEqual(c.source_extent.end.lineno, 2)
        self.assertEqual(c.source_extent.start.colno, 2)
        self.assertEqual(c.source_extent.end.colno, 6)
        self.assertEqual(c.source_extent.start.byteno, 10)
        self.assertEqual(c.source_extent.end.byteno, 14)

        self.assertEqual(d.source_extent.start.lineno, 2)
        self.assertEqual(d.source_extent.end.lineno, 2)
        self.assertEqual(d.source_extent.start.colno, 8)
        self.assertEqual(d.source_extent.end.colno, 14)
        self.assertEqual(d.source_extent.start.byteno, 16)
        self.assertEqual(d.source_extent.end.byteno, 22)

    def test_read_token_correctly_sets_source_extents_for_variants(self) -> None:
        l = Lexer("# \n\r\n\t abc")

        a = l.read_token()
        b = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)
        self.assertEqual(a.source_extent.start.colno, 1)
        # TODO(max): Should tabs count as one column?
        self.assertEqual(a.source_extent.end.colno, 1)

        self.assertEqual(b.source_extent.start.lineno, 3)
        self.assertEqual(b.source_extent.end.lineno, 3)
        self.assertEqual(b.source_extent.start.colno, 3)
        self.assertEqual(b.source_extent.end.colno, 5)

    def test_read_token_correctly_sets_source_extents_for_strings(self) -> None:
        l = Lexer('"今日は、Maxさん。"')
        a = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)

        self.assertEqual(a.source_extent.start.colno, 1)
        self.assertEqual(a.source_extent.end.colno, 12)

        self.assertEqual(a.source_extent.start.byteno, 0)
        self.assertEqual(a.source_extent.end.byteno, 25)

    def test_read_token_correctly_sets_source_extents_for_byte_literals(self) -> None:
        l = Lexer("~~QUJD ~~85'K|(_ ~~64'QUJD\n ~~32'IFBEG=== ~~16'414243")
        a = l.read_token()
        b = l.read_token()
        c = l.read_token()
        d = l.read_token()
        e = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)
        self.assertEqual(a.source_extent.start.colno, 1)
        self.assertEqual(a.source_extent.end.colno, 6)
        self.assertEqual(a.source_extent.start.byteno, 0)
        self.assertEqual(a.source_extent.end.byteno, 5)

        self.assertEqual(b.source_extent.start.lineno, 1)
        self.assertEqual(b.source_extent.end.lineno, 1)
        self.assertEqual(b.source_extent.start.colno, 8)
        self.assertEqual(b.source_extent.end.colno, 16)
        self.assertEqual(b.source_extent.start.byteno, 7)
        self.assertEqual(b.source_extent.end.byteno, 15)

        self.assertEqual(c.source_extent.start.lineno, 1)
        self.assertEqual(c.source_extent.end.lineno, 1)
        self.assertEqual(c.source_extent.start.colno, 18)
        self.assertEqual(c.source_extent.end.colno, 26)
        self.assertEqual(c.source_extent.start.byteno, 17)
        self.assertEqual(c.source_extent.end.byteno, 25)

        self.assertEqual(d.source_extent.start.lineno, 2)
        self.assertEqual(d.source_extent.end.lineno, 2)
        self.assertEqual(d.source_extent.start.colno, 2)
        self.assertEqual(d.source_extent.end.colno, 14)
        self.assertEqual(d.source_extent.start.byteno, 28)
        self.assertEqual(d.source_extent.end.byteno, 40)

        self.assertEqual(e.source_extent.start.lineno, 2)
        self.assertEqual(e.source_extent.end.lineno, 2)
        self.assertEqual(e.source_extent.start.colno, 16)
        self.assertEqual(e.source_extent.end.colno, 26)
        self.assertEqual(e.source_extent.start.byteno, 42)
        self.assertEqual(e.source_extent.end.byteno, 52)

    def test_read_token_correctly_sets_source_extents_for_numbers(self) -> None:
        l = Lexer("123 123.456")
        a = l.read_token()
        b = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)
        self.assertEqual(a.source_extent.start.colno, 1)
        self.assertEqual(a.source_extent.end.colno, 3)
        self.assertEqual(a.source_extent.start.byteno, 0)
        self.assertEqual(a.source_extent.end.byteno, 2)

        self.assertEqual(b.source_extent.start.lineno, 1)
        self.assertEqual(b.source_extent.end.lineno, 1)
        self.assertEqual(b.source_extent.start.colno, 5)
        self.assertEqual(b.source_extent.end.colno, 11)
        self.assertEqual(b.source_extent.start.byteno, 4)
        self.assertEqual(b.source_extent.end.byteno, 10)

    def test_read_token_correctly_sets_source_extents_for_operators(self) -> None:
        l = Lexer("> >>")
        a = l.read_token()
        b = l.read_token()

        self.assertEqual(a.source_extent.start.lineno, 1)
        self.assertEqual(a.source_extent.end.lineno, 1)
        self.assertEqual(a.source_extent.start.colno, 1)
        self.assertEqual(a.source_extent.end.colno, 1)
        self.assertEqual(a.source_extent.start.byteno, 0)
        self.assertEqual(a.source_extent.end.byteno, 0)

        self.assertEqual(b.source_extent.start.lineno, 1)
        self.assertEqual(b.source_extent.end.lineno, 1)
        self.assertEqual(b.source_extent.start.colno, 3)
        self.assertEqual(b.source_extent.end.colno, 4)
        self.assertEqual(b.source_extent.start.byteno, 2)
        self.assertEqual(b.source_extent.end.byteno, 3)

    def test_tokenize_list_with_only_spread(self) -> None:
        self.assertEqual(list(tokenize("[ ... ]")), [LeftBracket(), Operator("..."), RightBracket()])

    def test_tokenize_list_with_spread(self) -> None:
        self.assertEqual(
            list(tokenize("[ 1 , ... ]")),
            [
                LeftBracket(),
                IntLit(1),
                Operator(","),
                Operator("..."),
                RightBracket(),
            ],
        )

    def test_tokenize_list_with_spread_no_spaces(self) -> None:
        self.assertEqual(
            list(tokenize("[ 1,... ]")),
            [
                LeftBracket(),
                IntLit(1),
                Operator(","),
                Operator("..."),
                RightBracket(),
            ],
        )

    def test_tokenize_list_with_named_spread(self) -> None:
        self.assertEqual(
            list(tokenize("[1,...rest]")),
            [
                LeftBracket(),
                IntLit(1),
                Operator(","),
                Operator("..."),
                Name("rest"),
                RightBracket(),
            ],
        )

    def test_tokenize_record_with_only_spread(self) -> None:
        self.assertEqual(
            list(tokenize("{ ... }")),
            [
                LeftBrace(),
                Operator("..."),
                RightBrace(),
            ],
        )

    def test_tokenize_record_with_spread(self) -> None:
        self.assertEqual(
            list(tokenize("{ x = 1, ...}")),
            [
                LeftBrace(),
                Name("x"),
                Operator("="),
                IntLit(1),
                Operator(","),
                Operator("..."),
                RightBrace(),
            ],
        )

    def test_tokenize_record_with_spread_no_spaces(self) -> None:
        self.assertEqual(
            list(tokenize("{x=1,...}")),
            [
                LeftBrace(),
                Name("x"),
                Operator("="),
                IntLit(1),
                Operator(","),
                Operator("..."),
                RightBrace(),
            ],
        )

    def test_tokenize_variant_with_whitespace(self) -> None:
        self.assertEqual(list(tokenize("# \n\r\n\t abc")), [Hash(), Name("abc")])

    def test_tokenize_variant_with_no_space(self) -> None:
        self.assertEqual(list(tokenize("#abc")), [Hash(), Name("abc")])


class ParserTests(unittest.TestCase):
    def test_parse_with_empty_tokens_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedEOFError) as ctx:
            parse(Peekable(iter([])))
        self.assertEqual(ctx.exception.args[0], "unexpected end of input")

    def test_parse_digit_returns_int(self) -> None:
        self.assertEqual(parse(Peekable(iter([IntLit(1)]))), Int(1))

    def test_parse_digits_returns_int(self) -> None:
        self.assertEqual(parse(Peekable(iter([IntLit(123)]))), Int(123))

    def test_parse_negative_int_returns_negative_int(self) -> None:
        self.assertEqual(parse(Peekable(iter([Operator("-"), IntLit(123)]))), Int(-123))

    def test_parse_negative_var_returns_binary_sub_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Operator("-"), Name("x")]))), Binop(BinopKind.SUB, Int(0), Var("x")))

    def test_parse_negative_int_binds_tighter_than_plus(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Operator("-"), Name("l"), Operator("+"), Name("r")]))),
            Binop(BinopKind.ADD, Binop(BinopKind.SUB, Int(0), Var("l")), Var("r")),
        )

    def test_parse_negative_int_binds_tighter_than_mul(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Operator("-"), Name("l"), Operator("*"), Name("r")]))),
            Binop(BinopKind.MUL, Binop(BinopKind.SUB, Int(0), Var("l")), Var("r")),
        )

    def test_parse_negative_int_binds_tighter_than_index(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Operator("-"), Name("l"), Operator("@"), Name("r")]))),
            Access(Binop(BinopKind.SUB, Int(0), Var("l")), Var("r")),
        )

    def test_parse_negative_int_binds_tighter_than_apply(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Operator("-"), Name("l"), Name("r")]))),
            Apply(Binop(BinopKind.SUB, Int(0), Var("l")), Var("r")),
        )

    def test_parse_decimal_returns_float(self) -> None:
        self.assertEqual(parse(Peekable(iter([FloatLit(3.14)]))), Float(3.14))

    def test_parse_negative_float_returns_binary_sub_float(self) -> None:
        self.assertEqual(parse(Peekable(iter([Operator("-"), FloatLit(3.14)]))), Float(-3.14))

    def test_parse_var_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("abc_123")]))), Var("abc_123"))

    def test_parse_sha_var_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("$sha1'abc")]))), Var("$sha1'abc"))

    def test_parse_sha_var_without_quote_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("$sha1abc")]))), Var("$sha1abc"))

    def test_parse_dollar_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("$")]))), Var("$"))

    def test_parse_dollar_dollar_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("$$")]))), Var("$$"))

    @unittest.skip("TODO: make this fail to parse")
    def test_parse_sha_var_without_dollar_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, "unexpected token"):
            parse(Peekable(iter([Name("sha1'abc")])))

    def test_parse_dollar_dollar_var_returns_var(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("$$bills")]))), Var("$$bills"))

    def test_parse_bytes_returns_bytes(self) -> None:
        self.assertEqual(parse(Peekable(iter([BytesLit("QUJD", 64)]))), Bytes(b"ABC"))

    def test_parse_binary_add_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("+"), IntLit(2)]))), Binop(BinopKind.ADD, Int(1), Int(2))
        )

    def test_parse_binary_sub_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("-"), IntLit(2)]))), Binop(BinopKind.SUB, Int(1), Int(2))
        )

    def test_parse_binary_add_right_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("+"), IntLit(2), Operator("+"), IntLit(3)]))),
            Binop(BinopKind.ADD, Int(1), Binop(BinopKind.ADD, Int(2), Int(3))),
        )

    def test_mul_binds_tighter_than_add_right(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("+"), IntLit(2), Operator("*"), IntLit(3)]))),
            Binop(BinopKind.ADD, Int(1), Binop(BinopKind.MUL, Int(2), Int(3))),
        )

    def test_mul_binds_tighter_than_add_left(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("*"), IntLit(2), Operator("+"), IntLit(3)]))),
            Binop(BinopKind.ADD, Binop(BinopKind.MUL, Int(1), Int(2)), Int(3)),
        )

    def test_mul_and_div_bind_left_to_right(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("/"), IntLit(3), Operator("*"), IntLit(3)]))),
            Binop(BinopKind.MUL, Binop(BinopKind.DIV, Int(1), Int(3)), Int(3)),
        )

    def test_exp_binds_tighter_than_mul_right(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(5), Operator("*"), IntLit(2), Operator("^"), IntLit(3)]))),
            Binop(BinopKind.MUL, Int(5), Binop(BinopKind.EXP, Int(2), Int(3))),
        )

    def test_list_access_binds_tighter_than_append(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("+<"), Name("ls"), Operator("@"), IntLit(0)]))),
            Binop(BinopKind.LIST_APPEND, Var("a"), Access(Var("ls"), Int(0))),
        )

    def test_parse_binary_str_concat_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([StringLit("abc"), Operator("++"), StringLit("def")]))),
            Binop(BinopKind.STRING_CONCAT, String("abc"), String("def")),
        )

    def test_parse_binary_list_cons_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator(">+"), Name("b")]))),
            Binop(BinopKind.LIST_CONS, Var("a"), Var("b")),
        )

    def test_parse_binary_list_append_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("+<"), Name("b")]))),
            Binop(BinopKind.LIST_APPEND, Var("a"), Var("b")),
        )

    def test_parse_binary_op_returns_binop(self) -> None:
        ops = ["+", "-", "*", "/", "^", "%", "==", "/=", "<", ">", "<=", ">=", "&&", "||", "++", ">+", "+<"]
        for op in ops:
            with self.subTest(op=op):
                kind = BinopKind.from_str(op)
                self.assertEqual(
                    parse(Peekable(iter([Name("a"), Operator(op), Name("b")]))), Binop(kind, Var("a"), Var("b"))
                )

    def test_parse_empty_list(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([LeftBracket(), RightBracket()]))),
            List([]),
        )

    def test_parse_list_of_ints_returns_list(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([LeftBracket(), IntLit(1), Operator(","), IntLit(2), RightBracket()]))),
            List([Int(1), Int(2)]),
        )

    def test_parse_list_with_only_comma_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBracket(), Operator(","), RightBracket()])))

        self.assertEqual(parse_error.exception.unexpected_token, Operator(","))

    def test_parse_list_with_two_commas_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBracket(), Operator(","), Operator(","), RightBracket()])))

        self.assertEqual(parse_error.exception.unexpected_token, Operator(","))

    def test_parse_list_with_trailing_comma_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBracket(), IntLit(1), Operator(","), RightBracket()])))

        self.assertEqual(parse_error.exception.unexpected_token, RightBracket())

    def test_parse_assign(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("="), IntLit(1)]))),
            Assign(Var("a"), Int(1)),
        )

    def test_parse_function_one_arg_returns_function(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("->"), Name("a"), Operator("+"), IntLit(1)]))),
            Function(Var("a"), Binop(BinopKind.ADD, Var("a"), Int(1))),
        )

    def test_parse_function_two_args_returns_functions(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter([Name("a"), Operator("->"), Name("b"), Operator("->"), Name("a"), Operator("+"), Name("b")])
                )
            ),
            Function(Var("a"), Function(Var("b"), Binop(BinopKind.ADD, Var("a"), Var("b")))),
        )

    def test_parse_assign_function(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("id"), Operator("="), Name("x"), Operator("->"), Name("x")]))),
            Assign(Var("id"), Function(Var("x"), Var("x"))),
        )

    def test_parse_function_application_one_arg(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("f"), Name("a")]))), Apply(Var("f"), Var("a")))

    def test_parse_function_application_two_args(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("f"), Name("a"), Name("b")]))), Apply(Apply(Var("f"), Var("a")), Var("b"))
        )

    def test_parse_where(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("a"), Operator("."), Name("b")]))), Where(Var("a"), Var("b")))

    def test_parse_nested_where(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("."), Name("b"), Operator("."), Name("c")]))),
            Where(Where(Var("a"), Var("b")), Var("c")),
        )

    def test_parse_assert(self) -> None:
        self.assertEqual(parse(Peekable(iter([Name("a"), Operator("?"), Name("b")]))), Assert(Var("a"), Var("b")))

    def test_parse_nested_assert(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("?"), Name("b"), Operator("?"), Name("c")]))),
            Assert(Assert(Var("a"), Var("b")), Var("c")),
        )

    def test_parse_mixed_assert_where(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("?"), Name("b"), Operator("."), Name("c")]))),
            Where(Assert(Var("a"), Var("b")), Var("c")),
        )

    def test_parse_hastype(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator(":"), Name("b")]))), Binop(BinopKind.HASTYPE, Var("a"), Var("b"))
        )

    def test_parse_hole(self) -> None:
        self.assertEqual(parse(Peekable(iter([LeftParen(), RightParen()]))), Hole())

    def test_parse_parenthesized_expression(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([LeftParen(), IntLit(1), Operator("+"), IntLit(2), RightParen()]))),
            Binop(BinopKind.ADD, Int(1), Int(2)),
        )

    def test_parse_parenthesized_add_mul(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter([LeftParen(), IntLit(1), Operator("+"), IntLit(2), RightParen(), Operator("*"), IntLit(3)])
                )
            ),
            Binop(BinopKind.MUL, Binop(BinopKind.ADD, Int(1), Int(2)), Int(3)),
        )

    def test_parse_pipe(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("|>"), Name("f")]))),
            Apply(Var("f"), Int(1)),
        )

    def test_parse_nested_pipe(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([IntLit(1), Operator("|>"), Name("f"), Operator("|>"), Name("g")]))),
            Apply(Var("g"), Apply(Var("f"), Int(1))),
        )

    def test_parse_reverse_pipe(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("f"), Operator("<|"), IntLit(1)]))),
            Apply(Var("f"), Int(1)),
        )

    def test_parse_nested_reverse_pipe(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("g"), Operator("<|"), Name("f"), Operator("<|"), IntLit(1)]))),
            Apply(Var("g"), Apply(Var("f"), Int(1))),
        )

    def test_parse_empty_record(self) -> None:
        self.assertEqual(parse(Peekable(iter([LeftBrace(), RightBrace()]))), Record({}))

    def test_parse_record_single_field(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([LeftBrace(), Name("a"), Operator("="), IntLit(4), RightBrace()]))),
            Record({"a": Int(4)}),
        )

    def test_parse_record_with_expression(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter([LeftBrace(), Name("a"), Operator("="), IntLit(1), Operator("+"), IntLit(2), RightBrace()])
                )
            ),
            Record({"a": Binop(BinopKind.ADD, Int(1), Int(2))}),
        )

    def test_parse_record_multiple_fields(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter(
                        [
                            LeftBrace(),
                            Name("a"),
                            Operator("="),
                            IntLit(4),
                            Operator(","),
                            Name("b"),
                            Operator("="),
                            StringLit("z"),
                            RightBrace(),
                        ]
                    )
                )
            ),
            Record({"a": Int(4), "b": String("z")}),
        )

    def test_non_variable_in_assignment_raises_parse_error(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse(Peekable(iter([IntLit(3), Operator("="), IntLit(4)])))
        self.assertEqual(ctx.exception.args[0], "expected variable in assignment Int(value=3)")

    def test_non_assign_in_record_constructor_raises_parse_error(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse(Peekable(iter([LeftBrace(), IntLit(1), Operator(","), IntLit(2), RightBrace()])))
        self.assertEqual(ctx.exception.args[0], "failed to parse variable assignment in record constructor")

    def test_parse_right_eval_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("!"), Name("b")]))),
            Binop(BinopKind.RIGHT_EVAL, Var("a"), Var("b")),
        )

    def test_parse_right_eval_with_defs_returns_binop(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("a"), Operator("!"), Name("b"), Operator("."), Name("c")]))),
            Binop(BinopKind.RIGHT_EVAL, Var("a"), Where(Var("b"), Var("c"))),
        )

    def test_parse_match_no_cases_raises_parse_error(self) -> None:
        with self.assertRaises(ParseError) as ctx:
            parse(Peekable(iter([Operator("|")])))
        self.assertEqual(ctx.exception.args[0], "unexpected end of input")

    def test_parse_match_one_case(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Operator("|"), IntLit(1), Operator("->"), IntLit(2)]))),
            MatchFunction([MatchCase(Int(1), Int(2))]),
        )

    def test_parse_match_two_cases(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter(
                        [
                            Operator("|"),
                            IntLit(1),
                            Operator("->"),
                            IntLit(2),
                            Operator("|"),
                            IntLit(2),
                            Operator("->"),
                            IntLit(3),
                        ]
                    )
                )
            ),
            MatchFunction(
                [
                    MatchCase(Int(1), Int(2)),
                    MatchCase(Int(2), Int(3)),
                ]
            ),
        )

    def test_parse_compose(self) -> None:
        gensym_reset()
        self.assertEqual(
            parse(Peekable(iter([Name("f"), Operator(">>"), Name("g")]))),
            Function(Var("$v0"), Apply(Var("g"), Apply(Var("f"), Var("$v0")))),
        )

    def test_parse_compose_reverse(self) -> None:
        gensym_reset()
        self.assertEqual(
            parse(Peekable(iter([Name("f"), Operator("<<"), Name("g")]))),
            Function(Var("$v0"), Apply(Var("f"), Apply(Var("g"), Var("$v0")))),
        )

    def test_parse_double_compose(self) -> None:
        gensym_reset()
        self.assertEqual(
            parse(Peekable(iter([Name("f"), Operator("<<"), Name("g"), Operator("<<"), Name("h")]))),
            Function(
                Var("$v1"),
                Apply(Var("f"), Apply(Function(Var("$v0"), Apply(Var("g"), Apply(Var("h"), Var("$v0")))), Var("$v1"))),
            ),
        )

    def test_boolean_and_binds_tighter_than_or(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([Name("x"), Operator("||"), Name("y"), Operator("&&"), Name("z")]))),
            Binop(BinopKind.BOOL_OR, Var("x"), Binop(BinopKind.BOOL_AND, Var("y"), Var("z"))),
        )

    def test_parse_list_spread(self) -> None:
        self.assertEqual(
            parse(Peekable(iter([LeftBracket(), IntLit(1), Operator(","), Operator("..."), RightBracket()]))),
            List([Int(1), Spread()]),
        )

    @unittest.skip("TODO(max): Raise if ...x is used with non-name")
    def test_parse_list_with_non_name_expr_after_spread_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("unexpected token IntLit(lineno=-1, value=1)")):
            parse(Peekable(iter([LeftBracket(), IntLit(1), Operator(","), Operator("..."), IntLit(2), RightBracket()])))

    def test_parse_list_with_named_spread(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter(
                        [
                            LeftBracket(),
                            IntLit(1),
                            Operator(","),
                            Operator("..."),
                            Name("rest"),
                            RightBracket(),
                        ]
                    )
                )
            ),
            List([Int(1), Spread("rest")]),
        )

    def test_parse_list_spread_beginning_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of list match")):
            parse(Peekable(iter([LeftBracket(), Operator("..."), Operator(","), IntLit(1), RightBracket()])))

    def test_parse_list_named_spread_beginning_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of list match")):
            parse(
                Peekable(iter([LeftBracket(), Operator("..."), Name("rest"), Operator(","), IntLit(1), RightBracket()]))
            )

    def test_parse_list_spread_middle_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of list match")):
            parse(
                Peekable(
                    iter(
                        [
                            LeftBracket(),
                            IntLit(1),
                            Operator(","),
                            Operator("..."),
                            Operator(","),
                            IntLit(1),
                            RightBracket(),
                        ]
                    )
                )
            )

    def test_parse_list_named_spread_middle_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of list match")):
            parse(
                Peekable(
                    iter(
                        [
                            LeftBracket(),
                            IntLit(1),
                            Operator(","),
                            Operator("..."),
                            Name("rest"),
                            Operator(","),
                            IntLit(1),
                            RightBracket(),
                        ]
                    )
                )
            )

    def test_parse_record_spread(self) -> None:
        self.assertEqual(
            parse(
                Peekable(
                    iter(
                        [LeftBrace(), Name("x"), Operator("="), IntLit(1), Operator(","), Operator("..."), RightBrace()]
                    )
                )
            ),
            Record({"x": Int(1), "...": Spread()}),
        )

    def test_parse_record_spread_beginning_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of record match")):
            parse(
                Peekable(
                    iter(
                        [LeftBrace(), Operator("..."), Operator(","), Name("x"), Operator("="), IntLit(1), RightBrace()]
                    )
                )
            )

    def test_parse_record_spread_middle_raises_parse_error(self) -> None:
        with self.assertRaisesRegex(ParseError, re.escape("spread must come at end of record match")):
            parse(
                Peekable(
                    iter(
                        [
                            LeftBrace(),
                            Name("x"),
                            Operator("="),
                            IntLit(1),
                            Operator(","),
                            Operator("..."),
                            Operator(","),
                            Name("y"),
                            Operator("="),
                            IntLit(2),
                            RightBrace(),
                        ]
                    )
                )
            )

    def test_parse_record_with_only_comma_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBrace(), Operator(","), RightBrace()])))

        self.assertEqual(parse_error.exception.unexpected_token, Operator(","))

    def test_parse_record_with_two_commas_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBrace(), Operator(","), Operator(","), RightBrace()])))

        self.assertEqual(parse_error.exception.unexpected_token, Operator(","))

    def test_parse_record_with_trailing_comma_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([LeftBrace(), Name("x"), Operator("="), IntLit(1), Operator(","), RightBrace()])))

        self.assertEqual(parse_error.exception.unexpected_token, RightBrace())

    def test_parse_variant_returns_variant(self) -> None:
        self.assertEqual(parse(Peekable(iter([Hash(), Name("abc"), IntLit(1)]))), Variant("abc", Int(1)))

    def test_parse_hash_raises_unexpected_eof_error(self) -> None:
        tokens = Peekable(iter([Hash()]))
        with self.assertRaises(UnexpectedEOFError):
            parse(tokens)

    def test_parse_variant_non_name_raises_parse_error(self) -> None:
        with self.assertRaises(UnexpectedTokenError) as parse_error:
            parse(Peekable(iter([Hash(), IntLit(1)])))

        self.assertEqual(parse_error.exception.unexpected_token, IntLit(1))

    def test_parse_variant_eof_raises_unexpected_eof_error(self) -> None:
        with self.assertRaises(UnexpectedEOFError):
            parse(Peekable(iter([Hash()])))

    def test_match_with_variant(self) -> None:
        ast = parse(tokenize("| #true () -> 123"))
        self.assertEqual(ast, MatchFunction([MatchCase(TRUE, Int(123))]))

    def test_binary_and_with_variant_args(self) -> None:
        ast = parse(tokenize("#true() && #false()"))
        self.assertEqual(ast, Binop(BinopKind.BOOL_AND, TRUE, FALSE))

    def test_apply_with_variant_args(self) -> None:
        ast = parse(tokenize("f #true() #false()"))
        self.assertEqual(ast, Apply(Apply(Var("f"), TRUE), FALSE))


class MatchTests(unittest.TestCase):
    def test_match_hole_with_non_hole_returns_none(self) -> None:
        self.assertEqual(match(Int(1), pattern=Hole()), None)

    def test_match_hole_with_hole_returns_empty_dict(self) -> None:
        self.assertEqual(match(Hole(), pattern=Hole()), {})

    def test_match_with_equal_ints_returns_empty_dict(self) -> None:
        self.assertEqual(match(Int(1), pattern=Int(1)), {})

    def test_match_with_inequal_ints_returns_none(self) -> None:
        self.assertEqual(match(Int(2), pattern=Int(1)), None)

    def test_match_int_with_non_int_returns_none(self) -> None:
        self.assertEqual(match(String("abc"), pattern=Int(1)), None)

    def test_match_with_equal_floats_raises_match_error(self) -> None:
        with self.assertRaisesRegex(MatchError, re.escape("pattern matching is not supported for Floats")):
            match(Float(1), pattern=Float(1))

    def test_match_with_inequal_floats_raises_match_error(self) -> None:
        with self.assertRaisesRegex(MatchError, re.escape("pattern matching is not supported for Floats")):
            match(Float(2), pattern=Float(1))

    def test_match_float_with_non_float_raises_match_error(self) -> None:
        with self.assertRaisesRegex(MatchError, re.escape("pattern matching is not supported for Floats")):
            match(String("abc"), pattern=Float(1))

    def test_match_with_equal_strings_returns_empty_dict(self) -> None:
        self.assertEqual(match(String("a"), pattern=String("a")), {})

    def test_match_with_inequal_strings_returns_none(self) -> None:
        self.assertEqual(match(String("b"), pattern=String("a")), None)

    def test_match_string_with_non_string_returns_none(self) -> None:
        self.assertEqual(match(Int(1), pattern=String("abc")), None)

    def test_match_var_returns_dict_with_var_name(self) -> None:
        self.assertEqual(match(String("abc"), pattern=Var("a")), {"a": String("abc")})

    def test_match_record_with_non_record_returns_none(self) -> None:
        self.assertEqual(
            match(
                Int(2),
                pattern=Record({"x": Var("x"), "y": Var("y")}),
            ),
            None,
        )

    def test_match_record_with_more_fields_in_pattern_returns_none(self) -> None:
        self.assertEqual(
            match(
                Record({"x": Int(1), "y": Int(2)}),
                pattern=Record({"x": Var("x"), "y": Var("y"), "z": Var("z")}),
            ),
            None,
        )

    def test_match_record_with_fewer_fields_in_pattern_returns_none(self) -> None:
        self.assertEqual(
            match(
                Record({"x": Int(1), "y": Int(2)}),
                pattern=Record({"x": Var("x")}),
            ),
            None,
        )

    def test_match_record_with_vars_returns_dict_with_keys(self) -> None:
        self.assertEqual(
            match(
                Record({"x": Int(1), "y": Int(2)}),
                pattern=Record({"x": Var("x"), "y": Var("y")}),
            ),
            {"x": Int(1), "y": Int(2)},
        )

    def test_match_record_with_matching_const_returns_dict_with_other_keys(self) -> None:
        # TODO(max): Should this be the case? I feel like we should return all
        # the keys.
        self.assertEqual(
            match(
                Record({"x": Int(1), "y": Int(2)}),
                pattern=Record({"x": Int(1), "y": Var("y")}),
            ),
            {"y": Int(2)},
        )

    def test_match_record_with_non_matching_const_returns_none(self) -> None:
        self.assertEqual(
            match(
                Record({"x": Int(1), "y": Int(2)}),
                pattern=Record({"x": Int(3), "y": Var("y")}),
            ),
            None,
        )

    def test_match_list_with_non_list_returns_none(self) -> None:
        self.assertEqual(
            match(
                Int(2),
                pattern=List([Var("x"), Var("y")]),
            ),
            None,
        )

    def test_match_list_with_more_fields_in_pattern_returns_none(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Var("x"), Var("y"), Var("z")]),
            ),
            None,
        )

    def test_match_list_with_fewer_fields_in_pattern_returns_none(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Var("x")]),
            ),
            None,
        )

    def test_match_list_with_vars_returns_dict_with_keys(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Var("x"), Var("y")]),
            ),
            {"x": Int(1), "y": Int(2)},
        )

    def test_match_list_with_matching_const_returns_dict_with_other_keys(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Int(1), Var("y")]),
            ),
            {"y": Int(2)},
        )

    def test_match_list_with_non_matching_const_returns_none(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Int(3), Var("y")]),
            ),
            None,
        )

    def test_parse_right_pipe(self) -> None:
        text = "3 + 4 |> $$quote"
        ast = parse(tokenize(text))
        self.assertEqual(ast, Apply(Var("$$quote"), Binop(BinopKind.ADD, Int(3), Int(4))))

    def test_parse_left_pipe(self) -> None:
        text = "$$quote <| 3 + 4"
        ast = parse(tokenize(text))
        self.assertEqual(ast, Apply(Var("$$quote"), Binop(BinopKind.ADD, Int(3), Int(4))))

    def test_parse_match_with_left_apply(self) -> None:
        text = """| a -> b <| c
                  | d -> e"""
        tokens = tokenize(text)
        self.assertEqual(
            list(tokens),
            [
                Operator("|"),
                Name("a"),
                Operator("->"),
                Name("b"),
                Operator("<|"),
                Name("c"),
                Operator("|"),
                Name("d"),
                Operator("->"),
                Name("e"),
            ],
        )
        tokens = tokenize(text)
        ast = parse(tokens)
        self.assertEqual(
            ast, MatchFunction([MatchCase(Var("a"), Apply(Var("b"), Var("c"))), MatchCase(Var("d"), Var("e"))])
        )

    def test_parse_match_with_right_apply(self) -> None:
        text = """
| 1 -> 19
| a -> a |> (x -> x + 1)
"""
        tokens = tokenize(text)
        ast = parse(tokens)
        self.assertEqual(
            ast,
            MatchFunction(
                [
                    MatchCase(Int(1), Int(19)),
                    MatchCase(
                        Var("a"),
                        Apply(
                            Function(Var("x"), Binop(BinopKind.ADD, Var("x"), Int(1))),
                            Var("a"),
                        ),
                    ),
                ]
            ),
        )

    def test_match_list_with_spread_returns_empty_dict(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2), Int(3), Int(4), Int(5)]),
                pattern=List([Int(1), Spread()]),
            ),
            {},
        )

    def test_match_list_with_named_spread_returns_name_bound_to_rest(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2), Int(3), Int(4)]),
                pattern=List([Var("a"), Int(2), Spread("rest")]),
            ),
            {"a": Int(1), "rest": List([Int(3), Int(4)])},
        )

    def test_match_list_with_named_spread_returns_name_bound_to_empty_rest(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2)]),
                pattern=List([Var("a"), Int(2), Spread("rest")]),
            ),
            {"a": Int(1), "rest": List([])},
        )

    def test_match_list_with_mismatched_spread_returns_none(self) -> None:
        self.assertEqual(
            match(
                List([Int(1), Int(2), Int(3), Int(4), Int(5)]),
                pattern=List([Int(1), Int(6), Spread()]),
            ),
            None,
        )

    def test_match_record_with_constant_and_spread_returns_empty_dict(self) -> None:
        self.assertEqual(
            match(
                Record({"a": Int(1), "b": Int(2), "c": Int(3)}),
                pattern=Record({"a": Int(1), "...": Spread()}),
            ),
            {},
        )

    def test_match_record_with_var_and_spread_returns_match(self) -> None:
        self.assertEqual(
            match(
                Record({"a": Int(1), "b": Int(2), "c": Int(3)}),
                pattern=Record({"a": Var("x"), "...": Spread()}),
            ),
            {"x": Int(1)},
        )

    def test_match_record_with_mismatched_spread_returns_none(self) -> None:
        self.assertEqual(
            match(
                Record({"a": Int(1), "b": Int(2), "c": Int(3)}),
                pattern=Record({"d": Var("x"), "...": Spread()}),
            ),
            None,
        )

    def test_match_variant_with_equal_tag_returns_empty_dict(self) -> None:
        self.assertEqual(match(Variant("abc", Hole()), pattern=Variant("abc", Hole())), {})

    def test_match_variant_with_inequal_tag_returns_none(self) -> None:
        self.assertEqual(match(Variant("def", Hole()), pattern=Variant("abc", Hole())), None)

    def test_match_variant_matches_value(self) -> None:
        self.assertEqual(match(Variant("abc", Int(123)), pattern=Variant("abc", Hole())), None)
        self.assertEqual(match(Variant("abc", Int(123)), pattern=Variant("abc", Int(123))), {})

    def test_match_variant_with_different_type_returns_none(self) -> None:
        self.assertEqual(match(Int(123), pattern=Variant("abc", Hole())), None)


class EvalTests(unittest.TestCase):
    def test_eval_int_returns_int(self) -> None:
        exp = Int(5)
        self.assertEqual(eval_exp({}, exp), Int(5))

    def test_eval_float_returns_float(self) -> None:
        exp = Float(3.14)
        self.assertEqual(eval_exp({}, exp), Float(3.14))

    def test_eval_str_returns_str(self) -> None:
        exp = String("xyz")
        self.assertEqual(eval_exp({}, exp), String("xyz"))

    def test_eval_bytes_returns_bytes(self) -> None:
        exp = Bytes(b"xyz")
        self.assertEqual(eval_exp({}, exp), Bytes(b"xyz"))

    def test_eval_with_non_existent_var_raises_name_error(self) -> None:
        exp = Var("no")
        with self.assertRaises(NameError) as ctx:
            eval_exp({}, exp)
        self.assertEqual(ctx.exception.args[0], "name 'no' is not defined")

    def test_eval_with_bound_var_returns_value(self) -> None:
        exp = Var("yes")
        env = {"yes": Int(123)}
        self.assertEqual(eval_exp(env, exp), Int(123))

    def test_eval_with_binop_add_returns_sum(self) -> None:
        exp = Binop(BinopKind.ADD, Int(1), Int(2))
        self.assertEqual(eval_exp({}, exp), Int(3))

    def test_eval_with_nested_binop(self) -> None:
        exp = Binop(BinopKind.ADD, Binop(BinopKind.ADD, Int(1), Int(2)), Int(3))
        self.assertEqual(eval_exp({}, exp), Int(6))

    def test_eval_with_binop_add_with_int_string_raises_type_error(self) -> None:
        exp = Binop(BinopKind.ADD, Int(1), String("hello"))
        with self.assertRaises(TypeError) as ctx:
            eval_exp({}, exp)
        self.assertEqual(ctx.exception.args[0], "expected Int or Float, got String")

    def test_eval_with_binop_sub(self) -> None:
        exp = Binop(BinopKind.SUB, Int(1), Int(2))
        self.assertEqual(eval_exp({}, exp), Int(-1))

    def test_eval_with_binop_mul(self) -> None:
        exp = Binop(BinopKind.MUL, Int(2), Int(3))
        self.assertEqual(eval_exp({}, exp), Int(6))

    def test_eval_with_binop_div(self) -> None:
        exp = Binop(BinopKind.DIV, Int(3), Int(10))
        self.assertEqual(eval_exp({}, exp), Float(0.3))

    def test_eval_with_binop_floor_div(self) -> None:
        exp = Binop(BinopKind.FLOOR_DIV, Int(2), Int(3))
        self.assertEqual(eval_exp({}, exp), Int(0))

    def test_eval_with_binop_exp(self) -> None:
        exp = Binop(BinopKind.EXP, Int(2), Int(3))
        self.assertEqual(eval_exp({}, exp), Int(8))

    def test_eval_with_binop_mod(self) -> None:
        exp = Binop(BinopKind.MOD, Int(10), Int(4))
        self.assertEqual(eval_exp({}, exp), Int(2))

    def test_eval_with_binop_equal_with_equal_returns_true(self) -> None:
        exp = Binop(BinopKind.EQUAL, Int(1), Int(1))
        self.assertEqual(eval_exp({}, exp), TRUE)

    def test_eval_with_binop_equal_with_inequal_returns_false(self) -> None:
        exp = Binop(BinopKind.EQUAL, Int(1), Int(2))
        self.assertEqual(eval_exp({}, exp), FALSE)

    def test_eval_with_binop_not_equal_with_equal_returns_false(self) -> None:
        exp = Binop(BinopKind.NOT_EQUAL, Int(1), Int(1))
        self.assertEqual(eval_exp({}, exp), FALSE)

    def test_eval_with_binop_not_equal_with_inequal_returns_true(self) -> None:
        exp = Binop(BinopKind.NOT_EQUAL, Int(1), Int(2))
        self.assertEqual(eval_exp({}, exp), TRUE)

    def test_eval_with_binop_concat_with_strings_returns_string(self) -> None:
        exp = Binop(BinopKind.STRING_CONCAT, String("hello"), String(" world"))
        self.assertEqual(eval_exp({}, exp), String("hello world"))

    def test_eval_with_binop_concat_with_int_string_raises_type_error(self) -> None:
        exp = Binop(BinopKind.STRING_CONCAT, Int(123), String(" world"))
        with self.assertRaises(TypeError) as ctx:
            eval_exp({}, exp)
        self.assertEqual(ctx.exception.args[0], "expected String, got Int")

    def test_eval_with_binop_concat_with_string_int_raises_type_error(self) -> None:
        exp = Binop(BinopKind.STRING_CONCAT, String(" world"), Int(123))
        with self.assertRaises(TypeError) as ctx:
            eval_exp({}, exp)
        self.assertEqual(ctx.exception.args[0], "expected String, got Int")

    def test_eval_with_binop_cons_with_int_list_returns_list(self) -> None:
        exp = Binop(BinopKind.LIST_CONS, Int(1), List([Int(2), Int(3)]))
        self.assertEqual(eval_exp({}, exp), List([Int(1), Int(2), Int(3)]))

    def test_eval_with_binop_cons_with_list_list_returns_nested_list(self) -> None:
        exp = Binop(BinopKind.LIST_CONS, List([]), List([]))
        self.assertEqual(eval_exp({}, exp), List([List([])]))

    def test_eval_with_binop_cons_with_list_int_raises_type_error(self) -> None:
        exp = Binop(BinopKind.LIST_CONS, List([]), Int(123))
        with self.assertRaises(TypeError) as ctx:
            eval_exp({}, exp)
        self.assertEqual(ctx.exception.args[0], "expected List, got Int")

    def test_eval_with_list_append(self) -> None:
        exp = Binop(BinopKind.LIST_APPEND, List([Int(1), Int(2)]), Int(3))
        self.assertEqual(eval_exp({}, exp), List([Int(1), Int(2), Int(3)]))

    def test_eval_with_list_evaluates_elements(self) -> None:
        exp = List(
            [
                Binop(BinopKind.ADD, Int(1), Int(2)),
                Binop(BinopKind.ADD, Int(3), Int(4)),
            ]
        )
        self.assertEqual(eval_exp({}, exp), List([Int(3), Int(7)]))

    def test_eval_with_function_returns_closure_with_improved_env(self) -> None:
        exp = Function(Var("x"), Var("x"))
        self.assertEqual(eval_exp({"a": Int(1), "b": Int(2)}, exp), Closure({}, exp))

    def test_eval_with_match_function_returns_closure_with_improved_env(self) -> None:
        exp = MatchFunction([])
        self.assertEqual(eval_exp({"a": Int(1), "b": Int(2)}, exp), Closure({}, exp))

    def test_eval_assign_returns_env_object(self) -> None:
        exp = Assign(Var("a"), Int(1))
        env: Env = {}
        result = eval_exp(env, exp)
        self.assertEqual(result, EnvObject({"a": Int(1)}))

    def test_eval_assign_function_returns_closure_without_function_in_env(self) -> None:
        exp = Assign(Var("a"), Function(Var("x"), Var("x")))
        result = eval_exp({}, exp)
        assert isinstance(result, EnvObject)
        closure = result.env["a"]
        self.assertIsInstance(closure, Closure)
        self.assertEqual(closure, Closure({}, Function(Var("x"), Var("x"))))

    def test_eval_assign_function_returns_closure_with_function_in_env(self) -> None:
        exp = Assign(Var("a"), Function(Var("x"), Var("a")))
        result = eval_exp({}, exp)
        assert isinstance(result, EnvObject)
        closure = result.env["a"]
        self.assertIsInstance(closure, Closure)
        self.assertEqual(closure, Closure({"a": closure}, Function(Var("x"), Var("a"))))

    def test_eval_assign_does_not_modify_env(self) -> None:
        exp = Assign(Var("a"), Int(1))
        env: Env = {}
        eval_exp(env, exp)
        self.assertEqual(env, {})

    def test_eval_where_evaluates_in_order(self) -> None:
        exp = Where(Binop(BinopKind.ADD, Var("a"), Int(2)), Assign(Var("a"), Int(1)))
        env: Env = {}
        self.assertEqual(eval_exp(env, exp), Int(3))
        self.assertEqual(env, {})

    def test_eval_nested_where(self) -> None:
        exp = Where(
            Where(
                Binop(BinopKind.ADD, Var("a"), Var("b")),
                Assign(Var("a"), Int(1)),
            ),
            Assign(Var("b"), Int(2)),
        )
        env: Env = {}
        self.assertEqual(eval_exp(env, exp), Int(3))
        self.assertEqual(env, {})

    def test_eval_assert_with_truthy_cond_returns_value(self) -> None:
        exp = Assert(Int(123), TRUE)
        self.assertEqual(eval_exp({}, exp), Int(123))

    def test_eval_assert_with_falsey_cond_raises_assertion_error(self) -> None:
        exp = Assert(Int(123), FALSE)
        with self.assertRaisesRegex(AssertionError, re.escape("condition #false () failed")):
            eval_exp({}, exp)

    def test_eval_nested_assert(self) -> None:
        exp = Assert(Assert(Int(123), TRUE), TRUE)
        self.assertEqual(eval_exp({}, exp), Int(123))

    def test_eval_hole(self) -> None:
        exp = Hole()
        self.assertEqual(eval_exp({}, exp), Hole())

    def test_eval_function_application_one_arg(self) -> None:
        exp = Apply(Function(Var("x"), Binop(BinopKind.ADD, Var("x"), Int(1))), Int(2))
        self.assertEqual(eval_exp({}, exp), Int(3))

    def test_eval_function_application_two_args(self) -> None:
        exp = Apply(
            Apply(Function(Var("a"), Function(Var("b"), Binop(BinopKind.ADD, Var("a"), Var("b")))), Int(3)),
            Int(2),
        )
        self.assertEqual(eval_exp({}, exp), Int(5))

    def test_eval_function_returns_closure_with_captured_env(self) -> None:
        exp = Function(Var("x"), Binop(BinopKind.ADD, Var("x"), Var("y")))
        res = eval_exp({"y": Int(5)}, exp)
        self.assertIsInstance(res, Closure)
        assert isinstance(res, Closure)  # for mypy
        self.assertEqual(res.env, {"y": Int(5)})

    def test_eval_function_capture_env(self) -> None:
        exp = Apply(Function(Var("x"), Binop(BinopKind.ADD, Var("x"), Var("y"))), Int(2))
        self.assertEqual(eval_exp({"y": Int(5)}, exp), Int(7))

    def test_eval_non_function_raises_type_error(self) -> None:
        exp = Apply(Int(3), Int(4))
        with self.assertRaisesRegex(TypeError, re.escape("attempted to apply a non-closure of type Int")):
            eval_exp({}, exp)

    def test_eval_access_from_invalid_object_raises_type_error(self) -> None:
        exp = Access(Int(4), String("x"))
        with self.assertRaisesRegex(TypeError, re.escape("attempted to access from type Int")):
            eval_exp({}, exp)

    def test_eval_record_evaluates_value_expressions(self) -> None:
        exp = Record({"a": Binop(BinopKind.ADD, Int(1), Int(2))})
        self.assertEqual(eval_exp({}, exp), Record({"a": Int(3)}))

    def test_eval_record_access_with_invalid_accessor_raises_type_error(self) -> None:
        exp = Access(Record({"a": Int(4)}), Int(0))
        with self.assertRaisesRegex(
            TypeError, re.escape("cannot access record field using Int, expected a field name")
        ):
            eval_exp({}, exp)

    def test_eval_record_access_with_unknown_accessor_raises_name_error(self) -> None:
        exp = Access(Record({"a": Int(4)}), Var("b"))
        with self.assertRaisesRegex(NameError, re.escape("no assignment to b found in record")):
            eval_exp({}, exp)

    def test_eval_record_access(self) -> None:
        exp = Access(Record({"a": Int(4)}), Var("a"))
        self.assertEqual(eval_exp({}, exp), Int(4))

    def test_eval_list_access_with_invalid_accessor_raises_type_error(self) -> None:
        exp = Access(List([Int(4)]), String("hello"))
        with self.assertRaisesRegex(TypeError, re.escape("cannot index into list using type String, expected integer")):
            eval_exp({}, exp)

    def test_eval_list_access_with_out_of_bounds_accessor_raises_value_error(self) -> None:
        exp = Access(List([Int(1), Int(2), Int(3)]), Int(4))
        with self.assertRaisesRegex(ValueError, re.escape("index 4 out of bounds for list")):
            eval_exp({}, exp)

    def test_eval_list_access(self) -> None:
        exp = Access(List([String("a"), String("b"), String("c")]), Int(2))
        self.assertEqual(eval_exp({}, exp), String("c"))

    def test_right_eval_evaluates_right_hand_side(self) -> None:
        exp = Binop(BinopKind.RIGHT_EVAL, Int(1), Int(2))
        self.assertEqual(eval_exp({}, exp), Int(2))

    def test_match_no_cases_raises_match_error(self) -> None:
        exp = Apply(MatchFunction([]), Int(1))
        with self.assertRaisesRegex(MatchError, "no matching cases"):
            eval_exp({}, exp)

    def test_match_int_with_equal_int_matches(self) -> None:
        exp = Apply(MatchFunction([MatchCase(pattern=Int(1), body=Int(2))]), Int(1))
        self.assertEqual(eval_exp({}, exp), Int(2))

    def test_match_int_with_inequal_int_raises_match_error(self) -> None:
        exp = Apply(MatchFunction([MatchCase(pattern=Int(1), body=Int(2))]), Int(3))
        with self.assertRaisesRegex(MatchError, "no matching cases"):
            eval_exp({}, exp)

    def test_match_string_with_equal_string_matches(self) -> None:
        exp = Apply(MatchFunction([MatchCase(pattern=String("a"), body=String("b"))]), String("a"))
        self.assertEqual(eval_exp({}, exp), String("b"))

    def test_match_string_with_inequal_string_raises_match_error(self) -> None:
        exp = Apply(MatchFunction([MatchCase(pattern=String("a"), body=String("b"))]), String("c"))
        with self.assertRaisesRegex(MatchError, "no matching cases"):
            eval_exp({}, exp)

    def test_match_falls_through_to_next(self) -> None:
        exp = Apply(
            MatchFunction([MatchCase(pattern=Int(3), body=Int(4)), MatchCase(pattern=Int(1), body=Int(2))]), Int(1)
        )
        self.assertEqual(eval_exp({}, exp), Int(2))

    def test_eval_compose(self) -> None:
        gensym_reset()
        exp = parse(tokenize("(x -> x + 3) << (x -> x * 2)"))
        env = {"a": Int(1)}
        expected = Closure(
            {},
            Function(
                Var("$v0"),
                Apply(
                    Function(Var("x"), Binop(BinopKind.ADD, Var("x"), Int(3))),
                    Apply(Function(Var("x"), Binop(BinopKind.MUL, Var("x"), Int(2))), Var("$v0")),
                ),
            ),
        )
        self.assertEqual(eval_exp(env, exp), expected)

    def test_eval_native_function_returns_function(self) -> None:
        exp = NativeFunction("times2", lambda x: Int(x.value * 2))  # type: ignore [attr-defined]
        self.assertIs(eval_exp({}, exp), exp)

    def test_eval_apply_native_function_calls_function(self) -> None:
        exp = Apply(NativeFunction("times2", lambda x: Int(x.value * 2)), Int(3))  # type: ignore [attr-defined]
        self.assertEqual(eval_exp({}, exp), Int(6))

    def test_eval_apply_quote_returns_ast(self) -> None:
        ast = Binop(BinopKind.ADD, Int(1), Int(2))
        exp = Apply(Var("$$quote"), ast)
        self.assertIs(eval_exp({}, exp), ast)

    def test_eval_apply_closure_with_match_function_has_access_to_closure_vars(self) -> None:
        ast = Apply(Closure({"x": Int(1)}, MatchFunction([MatchCase(Var("y"), Var("x"))])), Int(2))
        self.assertEqual(eval_exp({}, ast), Int(1))

    def test_eval_less_returns_bool(self) -> None:
        ast = Binop(BinopKind.LESS, Int(3), Int(4))
        self.assertEqual(eval_exp({}, ast), TRUE)

    def test_eval_less_on_non_bool_raises_type_error(self) -> None:
        ast = Binop(BinopKind.LESS, String("xyz"), Int(4))
        with self.assertRaisesRegex(TypeError, re.escape("expected Int or Float, got String")):
            eval_exp({}, ast)

    def test_eval_less_equal_returns_bool(self) -> None:
        ast = Binop(BinopKind.LESS_EQUAL, Int(3), Int(4))
        self.assertEqual(eval_exp({}, ast), TRUE)

    def test_eval_less_equal_on_non_bool_raises_type_error(self) -> None:
        ast = Binop(BinopKind.LESS_EQUAL, String("xyz"), Int(4))
        with self.assertRaisesRegex(TypeError, re.escape("expected Int or Float, got String")):
            eval_exp({}, ast)

    def test_eval_greater_returns_bool(self) -> None:
        ast = Binop(BinopKind.GREATER, Int(3), Int(4))
        self.assertEqual(eval_exp({}, ast), FALSE)

    def test_eval_greater_on_non_bool_raises_type_error(self) -> None:
        ast = Binop(BinopKind.GREATER, String("xyz"), Int(4))
        with self.assertRaisesRegex(TypeError, re.escape("expected Int or Float, got String")):
            eval_exp({}, ast)

    def test_eval_greater_equal_returns_bool(self) -> None:
        ast = Binop(BinopKind.GREATER_EQUAL, Int(3), Int(4))
        self.assertEqual(eval_exp({}, ast), FALSE)

    def test_eval_greater_equal_on_non_bool_raises_type_error(self) -> None:
        ast = Binop(BinopKind.GREATER_EQUAL, String("xyz"), Int(4))
        with self.assertRaisesRegex(TypeError, re.escape("expected Int or Float, got String")):
            eval_exp({}, ast)

    def test_boolean_and_evaluates_args(self) -> None:
        ast = Binop(BinopKind.BOOL_AND, TRUE, Var("a"))
        self.assertEqual(eval_exp({"a": FALSE}, ast), FALSE)

        ast = Binop(BinopKind.BOOL_AND, Var("a"), FALSE)
        self.assertEqual(eval_exp({"a": TRUE}, ast), FALSE)

    def test_boolean_or_evaluates_args(self) -> None:
        ast = Binop(BinopKind.BOOL_OR, FALSE, Var("a"))
        self.assertEqual(eval_exp({"a": TRUE}, ast), TRUE)

        ast = Binop(BinopKind.BOOL_OR, Var("a"), TRUE)
        self.assertEqual(eval_exp({"a": FALSE}, ast), TRUE)

    def test_boolean_and_short_circuit(self) -> None:
        def raise_func(message: Object) -> Object:
            if not isinstance(message, String):
                raise TypeError(f"raise_func expected String, but got {type(message).__name__}")
            raise RuntimeError(message)

        error = NativeFunction("error", raise_func)
        apply = Apply(Var("error"), String("expected failure"))
        ast = Binop(BinopKind.BOOL_AND, FALSE, apply)
        self.assertEqual(eval_exp({"error": error}, ast), FALSE)

    def test_boolean_or_short_circuit(self) -> None:
        def raise_func(message: Object) -> Object:
            if not isinstance(message, String):
                raise TypeError(f"raise_func expected String, but got {type(message).__name__}")
            raise RuntimeError(message)

        error = NativeFunction("error", raise_func)
        apply = Apply(Var("error"), String("expected failure"))
        ast = Binop(BinopKind.BOOL_OR, TRUE, apply)
        self.assertEqual(eval_exp({"error": error}, ast), TRUE)

    def test_boolean_and_on_int_raises_type_error(self) -> None:
        exp = Binop(BinopKind.BOOL_AND, Int(1), Int(2))
        with self.assertRaisesRegex(TypeError, re.escape("expected #true or #false, got Int")):
            eval_exp({}, exp)

    def test_boolean_or_on_int_raises_type_error(self) -> None:
        exp = Binop(BinopKind.BOOL_OR, Int(1), Int(2))
        with self.assertRaisesRegex(TypeError, re.escape("expected #true or #false, got Int")):
            eval_exp({}, exp)

    def test_eval_record_with_spread_fails(self) -> None:
        exp = Record({"x": Spread()})
        with self.assertRaisesRegex(RuntimeError, "cannot evaluate a spread"):
            eval_exp({}, exp)

    def test_eval_variant_returns_variant(self) -> None:
        self.assertEqual(
            eval_exp(
                {},
                Variant("abc", Binop(BinopKind.ADD, Int(1), Int(2))),
            ),
            Variant("abc", Int(3)),
        )

    def test_eval_float_and_float_addition_returns_float(self) -> None:
        self.assertEqual(eval_exp({}, Binop(BinopKind.ADD, Float(1.0), Float(2.0))), Float(3.0))

    def test_eval_int_and_float_addition_returns_float(self) -> None:
        self.assertEqual(eval_exp({}, Binop(BinopKind.ADD, Int(1), Float(2.0))), Float(3.0))

    def test_eval_int_and_float_division_returns_float(self) -> None:
        self.assertEqual(eval_exp({}, Binop(BinopKind.DIV, Int(1), Float(2.0))), Float(0.5))

    def test_eval_float_and_int_division_returns_float(self) -> None:
        self.assertEqual(eval_exp({}, Binop(BinopKind.DIV, Float(1.0), Int(2))), Float(0.5))

    def test_eval_int_and_int_division_returns_float(self) -> None:
        self.assertEqual(eval_exp({}, Binop(BinopKind.DIV, Int(1), Int(2))), Float(0.5))


class EndToEndTestsBase(unittest.TestCase):
    def _run(self, text: str, env: Optional[Env] = None, check: bool = False) -> Object:
        tokens = tokenize(text)
        ast = parse(tokens)
        if check:
            infer_type(ast, OP_ENV)
        if env is None:
            env = boot_env()
        return eval_exp(env, ast)


class EndToEndTests(EndToEndTestsBase):
    def test_int_returns_int(self) -> None:
        self.assertEqual(self._run("1"), Int(1))

    def test_float_returns_float(self) -> None:
        self.assertEqual(self._run("3.14"), Float(3.14))

    def test_bytes_returns_bytes(self) -> None:
        self.assertEqual(self._run("~~QUJD"), Bytes(b"ABC"))

    def test_bytes_base85_returns_bytes(self) -> None:
        self.assertEqual(self._run("~~85'K|(_"), Bytes(b"ABC"))

    def test_bytes_base64_returns_bytes(self) -> None:
        self.assertEqual(self._run("~~64'QUJD"), Bytes(b"ABC"))

    def test_bytes_base32_returns_bytes(self) -> None:
        self.assertEqual(self._run("~~32'IFBEG==="), Bytes(b"ABC"))

    def test_bytes_base16_returns_bytes(self) -> None:
        self.assertEqual(self._run("~~16'414243"), Bytes(b"ABC"))

    def test_int_add_returns_int(self) -> None:
        self.assertEqual(self._run("1 + 2"), Int(3))

    def test_int_sub_returns_int(self) -> None:
        self.assertEqual(self._run("1 - 2"), Int(-1))

    def test_string_concat_returns_string(self) -> None:
        self.assertEqual(self._run('"abc" ++ "def"'), String("abcdef"))

    def test_list_cons_returns_list(self) -> None:
        self.assertEqual(self._run("1 >+ [2,3]"), List([Int(1), Int(2), Int(3)]))

    def test_list_cons_nested_returns_list(self) -> None:
        self.assertEqual(self._run("1 >+ 2 >+ [3,4]"), List([Int(1), Int(2), Int(3), Int(4)]))

    def test_list_append_returns_list(self) -> None:
        self.assertEqual(self._run("[1,2] +< 3"), List([Int(1), Int(2), Int(3)]))

    def test_list_append_nested_returns_list(self) -> None:
        self.assertEqual(self._run("[1,2] +< 3 +< 4"), List([Int(1), Int(2), Int(3), Int(4)]))

    def test_empty_list(self) -> None:
        self.assertEqual(self._run("[ ]"), List([]))

    def test_empty_list_with_no_spaces(self) -> None:
        self.assertEqual(self._run("[]"), List([]))

    def test_list_of_ints(self) -> None:
        self.assertEqual(self._run("[ 1 , 2 ]"), List([Int(1), Int(2)]))

    def test_list_of_exprs(self) -> None:
        self.assertEqual(
            self._run("[ 1 + 2 , 3 + 4 ]"),
            List([Int(3), Int(7)]),
        )

    def test_where(self) -> None:
        self.assertEqual(self._run("a + 2 . a = 1"), Int(3))

    def test_nested_where(self) -> None:
        self.assertEqual(self._run("a + b . a = 1 . b = 2"), Int(3))

    def test_assert_with_truthy_cond_returns_value(self) -> None:
        self.assertEqual(self._run("a + 1 ? a == 1 . a = 1"), Int(2))

    def test_assert_with_falsey_cond_raises_assertion_error(self) -> None:
        with self.assertRaisesRegex(AssertionError, "condition a == 2 failed"):
            self._run("a + 1 ? a == 2 . a = 1")

    def test_nested_assert(self) -> None:
        self.assertEqual(self._run("a + b ? a == 1 ? b == 2 . a = 1 . b = 2"), Int(3))

    def test_hole(self) -> None:
        self.assertEqual(self._run("()"), Hole())

    def test_bindings_behave_like_letstar(self) -> None:
        with self.assertRaises(NameError) as ctx:
            self._run("b . a = 1 . b = a")
        self.assertEqual(ctx.exception.args[0], "name 'a' is not defined")

    def test_function_application_two_args(self) -> None:
        self.assertEqual(self._run("(a -> b -> a + b) 3 2"), Int(5))

    def test_function_create_list_correct_order(self) -> None:
        self.assertEqual(self._run("(a -> b -> [a, b]) 3 2"), List([Int(3), Int(2)]))

    def test_create_record(self) -> None:
        self.assertEqual(self._run("{a = 1 + 3}"), Record({"a": Int(4)}))

    def test_access_record(self) -> None:
        self.assertEqual(self._run('rec@b . rec = { a = 1, b = "x" }'), String("x"))

    def test_access_list(self) -> None:
        self.assertEqual(self._run("xs@1 . xs = [1, 2, 3]"), Int(2))

    def test_access_list_var(self) -> None:
        self.assertEqual(self._run("xs@y . y = 2 . xs = [1, 2, 3]"), Int(3))

    def test_access_list_expr(self) -> None:
        self.assertEqual(self._run("xs@(1+1) . xs = [1, 2, 3]"), Int(3))

    def test_access_list_closure_var(self) -> None:
        self.assertEqual(
            self._run("list_at 1 [1,2,3] . list_at = idx -> ls -> ls@idx"),
            Int(2),
        )

    def test_functions_eval_arguments(self) -> None:
        self.assertEqual(self._run("(x -> x) c . c = 1"), Int(1))

    def test_non_var_function_arg_raises_parse_error(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            self._run("1 -> a")
        self.assertEqual(ctx.exception.args[0], "expected variable in function definition 1")

    def test_compose(self) -> None:
        self.assertEqual(self._run("((a -> a + 1) >> (b -> b * 2)) 3"), Int(8))

    def test_compose_does_not_expose_internal_x(self) -> None:
        with self.assertRaisesRegex(NameError, "name 'x' is not defined"):
            self._run("f 3 . f = ((y -> x) >> (z -> x))")

    def test_double_compose(self) -> None:
        self.assertEqual(self._run("((a -> a + 1) >> (x -> x) >> (b -> b * 2)) 3"), Int(8))

    def test_reverse_compose(self) -> None:
        self.assertEqual(self._run("((a -> a + 1) << (b -> b * 2)) 3"), Int(7))

    def test_simple_int_match(self) -> None:
        self.assertEqual(
            self._run(
                """
                inc 2
                . inc =
                  | 1 -> 2
                  | 2 -> 3
                  """
            ),
            Int(3),
        )

    def test_match_var_binds_var(self) -> None:
        self.assertEqual(
            self._run(
                """
                id 3
                . id =
                  | x -> x
                """
            ),
            Int(3),
        )

    def test_match_var_binds_first_arm(self) -> None:
        self.assertEqual(
            self._run(
                """
                id 3
                . id =
                  | x -> x
                  | y -> y * 2
                """
            ),
            Int(3),
        )

    def test_match_function_can_close_over_variables(self) -> None:
        self.assertEqual(
            self._run(
                """
        f 1 2
        . f = a ->
          | b -> a + b
        """
            ),
            Int(3),
        )

    def test_match_record_binds_var(self) -> None:
        self.assertEqual(
            self._run(
                """
                get_x rec
                . rec = { x = 3 }
                . get_x =
                  | { x = x } -> x
                """
            ),
            Int(3),
        )

    def test_match_record_binds_vars(self) -> None:
        self.assertEqual(
            self._run(
                """
                mult rec
                . rec = { x = 3, y = 4 }
                . mult =
                  | { x = x, y = y } -> x * y
                """
            ),
            Int(12),
        )

    def test_match_record_with_extra_fields_does_not_match(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
                mult rec
                . rec = { x = 3 }
                . mult =
                  | { x = x, y = y } -> x * y
                """
            )

    def test_match_record_with_constant(self) -> None:
        self.assertEqual(
            self._run(
                """
                mult rec
                . rec = { x = 4, y = 5 }
                . mult =
                  | { x = 3, y = y } -> 1
                  | { x = 4, y = y } -> 2
                """
            ),
            Int(2),
        )

    def test_match_record_with_non_record_fails(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
                get_x 3
                . get_x =
                  | { x = x } -> x
                """
            )

    def test_match_record_doubly_binds_vars(self) -> None:
        self.assertEqual(
            self._run(
                """
                get_x rec
                . rec = { a = 3, b = 3 }
                . get_x =
                  | { a = x, b = x } -> x
                """
            ),
            Int(3),
        )

    def test_match_record_spread_binds_spread(self) -> None:
        self.assertEqual(self._run("(| { a=1, ...rest } -> rest) {a=1, b=2, c=3}"), Record({"b": Int(2), "c": Int(3)}))

    def test_match_list_binds_vars(self) -> None:
        self.assertEqual(
            self._run(
                """
                mult xs
                . xs = [1, 2, 3, 4, 5]
                . mult =
                  | [1, x, 3, y, 5] -> x * y
                """
            ),
            Int(8),
        )

    def test_match_list_incorrect_length_does_not_match(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
                mult xs
                . xs = [1, 2, 3]
                . mult =
                  | [1, 2] -> 1
                  | [1, 2, 3, 4] -> 1
                  | [1, 3] -> 1
                """
            )

    def test_match_list_with_constant(self) -> None:
        self.assertEqual(
            self._run(
                """
                middle xs
                . xs = [4, 5, 6]
                . middle =
                  | [1, x, 3] -> x
                  | [4, x, 6] -> x
                  | [7, x, 9] -> x
                """
            ),
            Int(5),
        )

    def test_match_list_with_non_list_fails(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
                get_x 3
                . get_x =
                  | [2, x] -> x
                """
            )

    def test_match_list_doubly_binds_vars(self) -> None:
        self.assertEqual(
            self._run(
                """
                mult xs
                . xs = [1, 2, 3, 2, 1]
                . mult =
                  | [1, x, 3, x, 1] -> x
                """
            ),
            Int(2),
        )

    def test_match_list_spread_binds_spread(self) -> None:
        self.assertEqual(self._run("(| [x, ...xs] -> xs) [1, 2]"), List([Int(2)]))

    def test_pipe(self) -> None:
        self.assertEqual(self._run("1 |> (a -> a + 2)"), Int(3))

    def test_pipe_nested(self) -> None:
        self.assertEqual(self._run("1 |> (a -> a + 2) |> (b -> b * 2)"), Int(6))

    def test_reverse_pipe(self) -> None:
        self.assertEqual(self._run("(a -> a + 2) <| 1"), Int(3))

    def test_reverse_pipe_nested(self) -> None:
        self.assertEqual(self._run("(b -> b * 2) <| (a -> a + 2) <| 1"), Int(6))

    def test_function_can_reference_itself(self) -> None:
        result = self._run(
            """
    f 1
    . f = n -> f
    """,
            {},
        )
        self.assertEqual(result, Closure({"f": result}, Function(Var("n"), Var("f"))))

    def test_function_can_call_itself(self) -> None:
        with self.assertRaises(RecursionError):
            self._run(
                """
        f 1
        . f = n -> f n
        """
            )

    def test_match_function_can_call_itself(self) -> None:
        self.assertEqual(
            self._run(
                """
        fac 5
        . fac =
          | 0 -> 1
          | 1 -> 1
          | n -> n * fac (n - 1)
        """
            ),
            Int(120),
        )

    def test_list_access_binds_tighter_than_append(self) -> None:
        self.assertEqual(self._run("[1, 2, 3] +< xs@0 . xs = [4]"), List([Int(1), Int(2), Int(3), Int(4)]))

    def test_exponentiation(self) -> None:
        self.assertEqual(self._run("6 ^ 2"), Int(36))

    def test_modulus(self) -> None:
        self.assertEqual(self._run("11 % 3"), Int(2))

    def test_exp_binds_tighter_than_mul(self) -> None:
        self.assertEqual(self._run("5 * 2 ^ 3"), Int(40))

    def test_variant_true_returns_true(self) -> None:
        self.assertEqual(self._run("# true ()", {}), TRUE)

    def test_variant_false_returns_false(self) -> None:
        self.assertEqual(self._run("#false ()", {}), FALSE)

    def test_boolean_and_binds_tighter_than_or(self) -> None:
        self.assertEqual(self._run("#true () || #true () && boom", {}), TRUE)

    def test_compare_binds_tighter_than_boolean_and(self) -> None:
        self.assertEqual(self._run("1 < 2 && 2 < 1"), FALSE)

    def test_match_list_spread(self) -> None:
        self.assertEqual(
            self._run(
                """
        f [2, 4, 6]
        . f =
          | [] -> 0
          | [x, ...] -> x
          | c -> 1
        """
            ),
            Int(2),
        )

    def test_match_list_named_spread(self) -> None:
        self.assertEqual(
            self._run(
                """
        tail [1,2,3]
        . tail =
          | [first, ...rest] -> rest
        """
            ),
            List([Int(2), Int(3)]),
        )

    def test_match_record_spread(self) -> None:
        self.assertEqual(
            self._run(
                """
        f {x = 4, y = 5}
        . f =
          | {} -> 0
          | {x = a, ...} -> a
          | c -> 1
        """
            ),
            Int(4),
        )

    def test_match_expr_as_boolean_variants(self) -> None:
        self.assertEqual(
            self._run(
                """
        say (1 < 2)
        . say =
          | #false () -> "oh no"
          | #true () -> "omg"
        """
            ),
            String("omg"),
        )

    def test_match_variant_record(self) -> None:
        self.assertEqual(
            self._run(
                """
        f #add {x = 3, y = 4}
        . f =
          | # b () -> "foo"
          | #add {x = x, y = y} -> x + y
        """
            ),
            Int(7),
        )

    def test_int_div_returns_float(self) -> None:
        self.assertEqual(self._run("1 / 2 + 3"), Float(3.5))
        with self.assertRaisesRegex(InferenceError, "int and float"):
            self._run("1 / 2 + 3", check=True)


class ClosureOptimizeTests(unittest.TestCase):
    def test_int(self) -> None:
        self.assertEqual(free_in(Int(1)), set())

    def test_float(self) -> None:
        self.assertEqual(free_in(Float(1.0)), set())

    def test_string(self) -> None:
        self.assertEqual(free_in(String("x")), set())

    def test_bytes(self) -> None:
        self.assertEqual(free_in(Bytes(b"x")), set())

    def test_hole(self) -> None:
        self.assertEqual(free_in(Hole()), set())

    def test_spread(self) -> None:
        self.assertEqual(free_in(Spread()), set())

    def test_spread_name(self) -> None:
        # TODO(max): Should this be assumed to always be in a place where it
        # defines a name, and therefore never have free variables?
        self.assertEqual(free_in(Spread("x")), {"x"})

    def test_nativefunction(self) -> None:
        self.assertEqual(free_in(NativeFunction("id", lambda x: x)), set())

    def test_variant(self) -> None:
        self.assertEqual(free_in(Variant("x", Var("y"))), {"y"})

    def test_var(self) -> None:
        self.assertEqual(free_in(Var("x")), {"x"})

    def test_binop(self) -> None:
        self.assertEqual(free_in(Binop(BinopKind.ADD, Var("x"), Var("y"))), {"x", "y"})

    def test_empty_list(self) -> None:
        self.assertEqual(free_in(List([])), set())

    def test_list(self) -> None:
        self.assertEqual(free_in(List([Var("x"), Var("y")])), {"x", "y"})

    def test_empty_record(self) -> None:
        self.assertEqual(free_in(Record({})), set())

    def test_record(self) -> None:
        self.assertEqual(free_in(Record({"x": Var("x"), "y": Var("y")})), {"x", "y"})

    def test_function(self) -> None:
        exp = parse(tokenize("x -> x + y"))
        self.assertEqual(free_in(exp), {"y"})

    def test_nested_function(self) -> None:
        exp = parse(tokenize("x -> y -> x + y + z"))
        self.assertEqual(free_in(exp), {"z"})

    def test_match_function(self) -> None:
        exp = parse(tokenize("| 1 -> x | 2 -> y | x -> 3 | z -> 4"))
        self.assertEqual(free_in(exp), {"x", "y"})

    def test_match_case_int(self) -> None:
        exp = MatchCase(Int(1), Var("x"))
        self.assertEqual(free_in(exp), {"x"})

    def test_match_case_var(self) -> None:
        exp = MatchCase(Var("x"), Binop(BinopKind.ADD, Var("x"), Var("y")))
        self.assertEqual(free_in(exp), {"y"})

    def test_match_case_list(self) -> None:
        exp = MatchCase(List([Var("x")]), Binop(BinopKind.ADD, Var("x"), Var("y")))
        self.assertEqual(free_in(exp), {"y"})

    def test_match_case_list_spread(self) -> None:
        exp = MatchCase(List([Spread()]), Binop(BinopKind.ADD, Var("xs"), Var("y")))
        self.assertEqual(free_in(exp), {"xs", "y"})

    def test_match_case_list_spread_name(self) -> None:
        exp = MatchCase(List([Spread("xs")]), Binop(BinopKind.ADD, Var("xs"), Var("y")))
        self.assertEqual(free_in(exp), {"y"})

    def test_match_case_record(self) -> None:
        exp = MatchCase(
            Record({"x": Int(1), "y": Var("y"), "a": Var("z")}),
            Binop(BinopKind.ADD, Binop(BinopKind.ADD, Var("x"), Var("y")), Var("z")),
        )
        self.assertEqual(free_in(exp), {"x"})

    def test_match_case_record_spread(self) -> None:
        exp = MatchCase(Record({"...": Spread()}), Binop(BinopKind.ADD, Var("x"), Var("y")))
        self.assertEqual(free_in(exp), {"x", "y"})

    def test_match_case_record_spread_name(self) -> None:
        exp = MatchCase(Record({"...": Spread("x")}), Binop(BinopKind.ADD, Var("x"), Var("y")))
        self.assertEqual(free_in(exp), {"y"})

    def test_apply(self) -> None:
        self.assertEqual(free_in(Apply(Var("x"), Var("y"))), {"x", "y"})

    def test_access(self) -> None:
        self.assertEqual(free_in(Access(Var("x"), Var("y"))), {"x", "y"})

    def test_where(self) -> None:
        exp = parse(tokenize("x . x = 1"))
        self.assertEqual(free_in(exp), set())

    def test_where_same_name(self) -> None:
        exp = parse(tokenize("x . x = x+y"))
        self.assertEqual(free_in(exp), {"x", "y"})

    def test_assign(self) -> None:
        exp = Assign(Var("x"), Int(1))
        self.assertEqual(free_in(exp), set())

    def test_assign_same_name(self) -> None:
        exp = Assign(Var("x"), Var("x"))
        self.assertEqual(free_in(exp), {"x"})

    def test_closure(self) -> None:
        # TODO(max): Should x be considered free in the closure if it's in the
        # env?
        exp = Closure({"x": Int(1)}, Function(Var("_"), List([Var("x"), Var("y")])))
        self.assertEqual(free_in(exp), {"x", "y"})


class StdLibTests(EndToEndTestsBase):
    def test_stdlib_add(self) -> None:
        self.assertEqual(self._run("$$add 3 4", STDLIB), Int(7))

    def test_stdlib_quote(self) -> None:
        self.assertEqual(self._run("$$quote (3 + 4)"), Binop(BinopKind.ADD, Int(3), Int(4)))

    def test_stdlib_quote_pipe(self) -> None:
        self.assertEqual(self._run("3 + 4 |> $$quote"), Binop(BinopKind.ADD, Int(3), Int(4)))

    def test_stdlib_quote_reverse_pipe(self) -> None:
        self.assertEqual(self._run("$$quote <| 3 + 4"), Binop(BinopKind.ADD, Int(3), Int(4)))

    def test_stdlib_serialize(self) -> None:
        self.assertEqual(self._run("$$serialize 3", STDLIB), Bytes(value=b"i\x06"))

    def test_stdlib_serialize_expr(self) -> None:
        self.assertEqual(
            self._run("(1+2) |> $$quote |> $$serialize", STDLIB),
            Bytes(value=b"+\x02+i\x02i\x04"),
        )

    def test_stdlib_deserialize(self) -> None:
        self.assertEqual(self._run("$$deserialize ~~aQY="), Int(3))

    def test_stdlib_deserialize_expr(self) -> None:
        self.assertEqual(self._run("$$deserialize ~~KwIraQJpBA=="), Binop(BinopKind.ADD, Int(1), Int(2)))

    def test_stdlib_listlength_empty_list_returns_zero(self) -> None:
        self.assertEqual(self._run("$$listlength []", STDLIB), Int(0))

    def test_stdlib_listlength_returns_length(self) -> None:
        self.assertEqual(self._run("$$listlength [1,2,3]", STDLIB), Int(3))

    def test_stdlib_listlength_with_non_list_raises_type_error(self) -> None:
        with self.assertRaises(TypeError) as ctx:
            self._run("$$listlength 1", STDLIB)
        self.assertEqual(ctx.exception.args[0], "listlength expected List, but got Int")


class PreludeTests(EndToEndTestsBase):
    def test_id_returns_input(self) -> None:
        self.assertEqual(self._run("id 123"), Int(123))

    def test_filter_returns_matching(self) -> None:
        self.assertEqual(
            self._run(
                """
        filter (x -> x < 4) [2, 6, 3, 7, 1, 8]
        """
            ),
            List([Int(2), Int(3), Int(1)]),
        )

    def test_filter_with_function_returning_non_bool_raises_match_error(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
        filter (x -> #no ()) [1]
        """
            )

    def test_quicksort(self) -> None:
        self.assertEqual(
            self._run(
                """
        quicksort [2, 6, 3, 7, 1, 8]
        """
            ),
            List([Int(1), Int(2), Int(3), Int(6), Int(7), Int(8)]),
        )

    def test_quicksort_with_empty_list(self) -> None:
        self.assertEqual(
            self._run(
                """
        quicksort []
        """
            ),
            List([]),
        )

    def test_quicksort_with_non_int_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        quicksort ["a", "c", "b"]
        """
            )

    def test_concat(self) -> None:
        self.assertEqual(
            self._run(
                """
        concat [1, 2, 3] [4, 5, 6]
        """
            ),
            List([Int(1), Int(2), Int(3), Int(4), Int(5), Int(6)]),
        )

    def test_concat_with_first_list_empty(self) -> None:
        self.assertEqual(
            self._run(
                """
        concat [] [4, 5, 6]
        """
            ),
            List([Int(4), Int(5), Int(6)]),
        )

    def test_concat_with_second_list_empty(self) -> None:
        self.assertEqual(
            self._run(
                """
        concat [1, 2, 3] []
        """
            ),
            List([Int(1), Int(2), Int(3)]),
        )

    def test_concat_with_both_lists_empty(self) -> None:
        self.assertEqual(
            self._run(
                """
        concat [] []
        """
            ),
            List([]),
        )

    def test_map(self) -> None:
        self.assertEqual(
            self._run(
                """
        map (x -> x * 2) [3, 1, 2]
        """
            ),
            List([Int(6), Int(2), Int(4)]),
        )

    def test_map_with_non_function_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        map 4 [3, 1, 2]
        """
            )

    def test_map_with_non_list_raises_match_error(self) -> None:
        with self.assertRaises(MatchError):
            self._run(
                """
        map (x -> x * 2) 3
        """
            )

    def test_range(self) -> None:
        self.assertEqual(
            self._run(
                """
        range 3
        """
            ),
            List([Int(0), Int(1), Int(2)]),
        )

    def test_range_with_non_int_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        range "a"
        """
            )

    def test_foldr(self) -> None:
        self.assertEqual(
            self._run(
                """
        foldr (x -> a -> a + x) 0 [1, 2, 3]
        """
            ),
            Int(6),
        )

    def test_foldr_on_empty_list_returns_empty_list(self) -> None:
        self.assertEqual(
            self._run(
                """
        foldr (x -> a -> a + x) 0 []
        """
            ),
            Int(0),
        )

    def test_take(self) -> None:
        self.assertEqual(
            self._run(
                """
        take 3 [1, 2, 3, 4, 5]
        """
            ),
            List([Int(1), Int(2), Int(3)]),
        )

    def test_take_n_more_than_list_length_returns_full_list(self) -> None:
        self.assertEqual(
            self._run(
                """
        take 5 [1, 2, 3]
        """
            ),
            List([Int(1), Int(2), Int(3)]),
        )

    def test_take_with_non_int_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        take "a" [1, 2, 3]
        """
            )

    def test_all_returns_true(self) -> None:
        self.assertEqual(
            self._run(
                """
        all (x -> x < 5) [1, 2, 3, 4]
        """
            ),
            TRUE,
        )

    def test_all_returns_false(self) -> None:
        self.assertEqual(
            self._run(
                """
        all (x -> x < 5) [2, 4, 6]
        """
            ),
            FALSE,
        )

    def test_all_with_empty_list_returns_true(self) -> None:
        self.assertEqual(
            self._run(
                """
        all (x -> x == 5) []
        """
            ),
            TRUE,
        )

    def test_all_with_non_bool_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        all (x -> x) [1, 2, 3]
        """
            )

    def test_all_short_circuits(self) -> None:
        self.assertEqual(
            self._run(
                """
        all (x -> x > 1) [1, "a", "b"]
        """
            ),
            FALSE,
        )

    def test_any_returns_true(self) -> None:
        self.assertEqual(
            self._run(
                """
        any (x -> x < 4) [1, 3, 5]
        """
            ),
            TRUE,
        )

    def test_any_returns_false(self) -> None:
        self.assertEqual(
            self._run(
                """
        any (x -> x < 3) [4, 5, 6]
        """
            ),
            FALSE,
        )

    def test_any_with_empty_list_returns_false(self) -> None:
        self.assertEqual(
            self._run(
                """
        any (x -> x == 5) []
        """
            ),
            FALSE,
        )

    def test_any_with_non_bool_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            self._run(
                """
        any (x -> x) [1, 2, 3]
        """
            )

    def test_any_short_circuits(self) -> None:
        self.assertEqual(
            self._run(
                """
        any (x -> x > 1) [2, "a", "b"]
        """
            ),
            Variant("true", Hole()),
        )

    def test_mul_and_div_have_left_to_right_precedence(self) -> None:
        self.assertEqual(
            self._run(
                """
        1 / 3 * 3
        """
            ),
            Float(1.0),
        )


class InferenceError(Exception):
    pass


@dataclasses.dataclass
class MonoType:
    def find(self) -> MonoType:
        return self


@dataclasses.dataclass
class TyVar(MonoType):
    forwarded: MonoType | None = dataclasses.field(init=False, default=None)
    name: str

    def find(self) -> MonoType:
        result: MonoType = self
        while isinstance(result, TyVar):
            it = result.forwarded
            if it is None:
                return result
            result = it
        return result

    def __str__(self) -> str:
        return f"'{self.name}"

    def make_equal_to(self, other: MonoType) -> None:
        chain_end = self.find()
        if not isinstance(chain_end, TyVar):
            raise InferenceError(f"{self} is already resolved to {chain_end}")
        chain_end.forwarded = other

    def is_unbound(self) -> bool:
        return self.forwarded is None


@dataclasses.dataclass
class TyCon(MonoType):
    name: str
    args: list[MonoType]

    def __str__(self) -> str:
        # TODO(max): Precedence pretty-print type constructors
        if not self.args:
            return self.name
        if len(self.args) == 1:
            return f"({self.args[0]} {self.name})"
        return f"({self.name.join(map(str, self.args))})"


@dataclasses.dataclass
class TyEmptyRow(MonoType):
    def __str__(self) -> str:
        return "{}"


@dataclasses.dataclass
class TyRow(MonoType):
    fields: dict[str, MonoType]
    rest: TyVar | TyEmptyRow = dataclasses.field(default_factory=TyEmptyRow)

    def __post_init__(self) -> None:
        if not self.fields and isinstance(self.rest, TyEmptyRow):
            raise InferenceError("Empty row must have a rest type")

    def __str__(self) -> str:
        flat, rest = row_flatten(self)
        # sort to make tests deterministic
        result = [f"{key}={val}" for key, val in sorted(flat.items())]
        if isinstance(rest, TyVar):
            result.append(f"...{rest}")
        else:
            assert isinstance(rest, TyEmptyRow)
        return "{" + ", ".join(result) + "}"


def row_flatten(rec: MonoType) -> tuple[dict[str, MonoType], TyVar | TyEmptyRow]:
    if isinstance(rec, TyVar):
        rec = rec.find()
        if isinstance(rec, TyVar):
            return {}, rec
    if isinstance(rec, TyRow):
        flat, rest = row_flatten(rec.rest)
        flat.update(rec.fields)
        return flat, rest
    if isinstance(rec, TyEmptyRow):
        return {}, rec
    raise InferenceError(f"Expected record type, got {type(rec)}")


@dataclasses.dataclass
class Forall:
    tyvars: list[TyVar]
    ty: MonoType

    def __str__(self) -> str:
        return f"(forall {', '.join(map(str, self.tyvars))}. {self.ty})"


class TypeStrTests(unittest.TestCase):
    def test_tyvar(self) -> None:
        self.assertEqual(str(TyVar("a")), "'a")

    def test_tycon(self) -> None:
        self.assertEqual(str(TyCon("int", [])), "int")

    def test_tycon_one_arg(self) -> None:
        self.assertEqual(str(TyCon("list", [IntType])), "(int list)")

    def test_tycon_args(self) -> None:
        self.assertEqual(str(TyCon("->", [IntType, IntType])), "(int->int)")

    def test_tyrow_empty_closed(self) -> None:
        self.assertEqual(str(TyEmptyRow()), "{}")

    def test_tyrow_empty_open(self) -> None:
        self.assertEqual(str(TyRow({}, TyVar("a"))), "{...'a}")

    def test_tyrow_closed(self) -> None:
        self.assertEqual(str(TyRow({"x": IntType, "y": StringType})), "{x=int, y=string}")

    def test_tyrow_open(self) -> None:
        self.assertEqual(str(TyRow({"x": IntType, "y": StringType}, TyVar("a"))), "{x=int, y=string, ...'a}")

    def test_tyrow_chain(self) -> None:
        inner = TyRow({"x": IntType})
        inner_var = TyVar("a")
        inner_var.make_equal_to(inner)
        outer = TyRow({"y": StringType}, inner_var)
        self.assertEqual(str(outer), "{x=int, y=string}")

    def test_forall(self) -> None:
        self.assertEqual(str(Forall([TyVar("a"), TyVar("b")], TyVar("a"))), "(forall 'a, 'b. 'a)")


def func_type(*args: MonoType) -> TyCon:
    assert len(args) >= 2
    if len(args) == 2:
        return TyCon("->", list(args))
    return TyCon("->", [args[0], func_type(*args[1:])])


def list_type(arg: MonoType) -> TyCon:
    return TyCon("list", [arg])


def unify_fail(ty1: MonoType, ty2: MonoType) -> None:
    raise InferenceError(f"Unification failed for {ty1} and {ty2}")


def occurs_in(tyvar: TyVar, ty: MonoType) -> bool:
    if isinstance(ty, TyVar):
        return tyvar == ty
    if isinstance(ty, TyCon):
        return any(occurs_in(tyvar, arg) for arg in ty.args)
    if isinstance(ty, TyEmptyRow):
        return False
    if isinstance(ty, TyRow):
        return any(occurs_in(tyvar, val) for val in ty.fields.values()) or occurs_in(tyvar, ty.rest)
    raise InferenceError(f"Unknown type: {ty}")


def unify_type(ty1: MonoType, ty2: MonoType) -> None:
    ty1 = ty1.find()
    ty2 = ty2.find()
    if isinstance(ty1, TyVar):
        if occurs_in(ty1, ty2):
            raise InferenceError(f"Occurs check failed for {ty1} and {ty2}")
        ty1.make_equal_to(ty2)
        return
    if isinstance(ty2, TyVar):  # Mirror
        return unify_type(ty2, ty1)
    if isinstance(ty1, TyCon) and isinstance(ty2, TyCon):
        if ty1.name != ty2.name:
            unify_fail(ty1, ty2)
            return
        if len(ty1.args) != len(ty2.args):
            unify_fail(ty1, ty2)
            return
        for l, r in zip(ty1.args, ty2.args):
            unify_type(l, r)
        return
    if isinstance(ty1, TyEmptyRow) and isinstance(ty2, TyEmptyRow):
        return
    if isinstance(ty1, TyRow) and isinstance(ty2, TyRow):
        ty1_fields, ty1_rest = row_flatten(ty1)
        ty2_fields, ty2_rest = row_flatten(ty2)
        ty1_missing = {}
        ty2_missing = {}
        all_field_names = set(ty1_fields.keys()) | set(ty2_fields.keys())
        for key in sorted(all_field_names):  # Sort for deterministic error messages
            ty1_val = ty1_fields.get(key)
            ty2_val = ty2_fields.get(key)
            if ty1_val is not None and ty2_val is not None:
                unify_type(ty1_val, ty2_val)
            elif ty1_val is None:
                assert ty2_val is not None
                ty1_missing[key] = ty2_val
            elif ty2_val is None:
                assert ty1_val is not None
                ty2_missing[key] = ty1_val
        # In general, we want to:
        # 1) Add missing fields from one row to the other row
        # 2) "Keep the rows unified" by linking each row's rest to the other
        #    row's rest
        if not ty1_missing and not ty2_missing:
            # The rests are either both empty (rows were closed) or both
            # unbound type variables (rows were open); unify the rest variables
            unify_type(ty1_rest, ty2_rest)
            return
        if not ty1_missing:
            # The first row has fields that the second row doesn't have; add
            # them to the second row
            unify_type(ty2_rest, TyRow(ty2_missing, ty1_rest))
            return
        if not ty2_missing:
            # The second row has fields that the first row doesn't have; add
            # them to the first row
            unify_type(ty1_rest, TyRow(ty1_missing, ty2_rest))
            return
        # They each have fields the other lacks; create new rows sharing a rest
        # and add the missing fields to each row
        rest = fresh_tyvar()
        unify_type(ty1_rest, TyRow(ty1_missing, rest))
        unify_type(ty2_rest, TyRow(ty2_missing, rest))
        return
    if isinstance(ty1, TyRow) and isinstance(ty2, TyEmptyRow):
        raise InferenceError(f"Unifying row {ty1} with empty row")
    if isinstance(ty1, TyEmptyRow) and isinstance(ty2, TyRow):
        raise InferenceError(f"Unifying empty row with row {ty2}")
    raise InferenceError(f"Cannot unify {ty1} and {ty2}")


Context = typing.Mapping[str, Forall]


fresh_var_counter = 0


def fresh_tyvar(prefix: str = "t") -> TyVar:
    global fresh_var_counter
    result = f"{prefix}{fresh_var_counter}"
    fresh_var_counter += 1
    return TyVar(result)


IntType = TyCon("int", [])
StringType = TyCon("string", [])
FloatType = TyCon("float", [])
BytesType = TyCon("bytes", [])
HoleType = TyCon("hole", [])


Subst = typing.Mapping[str, MonoType]


def apply_ty(ty: MonoType, subst: Subst) -> MonoType:
    ty = ty.find()
    if isinstance(ty, TyVar):
        return subst.get(ty.name, ty)
    if isinstance(ty, TyCon):
        return TyCon(ty.name, [apply_ty(arg, subst) for arg in ty.args])
    if isinstance(ty, TyEmptyRow):
        return ty
    if isinstance(ty, TyRow):
        rest = apply_ty(ty.rest, subst)
        assert isinstance(rest, (TyVar, TyEmptyRow))
        return TyRow({key: apply_ty(val, subst) for key, val in ty.fields.items()}, rest)
    raise InferenceError(f"Unknown type: {ty}")


def instantiate(scheme: Forall) -> MonoType:
    fresh = {tyvar.name: fresh_tyvar() for tyvar in scheme.tyvars}
    return apply_ty(scheme.ty, fresh)


def ftv_ty(ty: MonoType) -> set[str]:
    ty = ty.find()
    if isinstance(ty, TyVar):
        return {ty.name}
    if isinstance(ty, TyCon):
        return set().union(*map(ftv_ty, ty.args))
    if isinstance(ty, TyEmptyRow):
        return set()
    if isinstance(ty, TyRow):
        return set().union(*map(ftv_ty, ty.fields.values()), ftv_ty(ty.rest))
    raise InferenceError(f"Unknown type: {ty}")


def generalize(ty: MonoType, ctx: Context) -> Forall:
    def ftv_scheme(ty: Forall) -> set[str]:
        return ftv_ty(ty.ty) - set(tyvar.name for tyvar in ty.tyvars)

    def ftv_ctx(ctx: Context) -> set[str]:
        return set().union(*(ftv_scheme(scheme) for scheme in ctx.values()))

    # TODO(max): Freshen?
    tyvars = ftv_ty(ty) - ftv_ctx(ctx)
    return Forall([TyVar(name) for name in sorted(tyvars)], ty)


def type_of(expr: Object) -> MonoType:
    ty = getattr(expr, "inferred_type", None)
    if ty is not None:
        assert isinstance(ty, MonoType)
        return ty.find()
    return set_type(expr, fresh_tyvar())


def set_type(expr: Object, ty: MonoType) -> MonoType:
    object.__setattr__(expr, "inferred_type", ty)
    return ty


def infer_common(expr: Object) -> MonoType:
    if isinstance(expr, Int):
        return set_type(expr, IntType)
    if isinstance(expr, Float):
        return set_type(expr, FloatType)
    if isinstance(expr, Bytes):
        return set_type(expr, BytesType)
    if isinstance(expr, Hole):
        return set_type(expr, HoleType)
    if isinstance(expr, String):
        return set_type(expr, StringType)
    raise InferenceError(f"{type(expr)} can't be simply inferred")


def infer_pattern_type(pattern: Object, ctx: Context) -> MonoType:
    assert isinstance(ctx, dict)
    if isinstance(pattern, (Int, Float, Bytes, Hole, String)):
        return infer_common(pattern)
    if isinstance(pattern, Var):
        result = fresh_tyvar()
        ctx[pattern.name] = Forall([], result)
        return set_type(pattern, result)
    if isinstance(pattern, List):
        list_item_ty = fresh_tyvar()
        result_ty = list_type(list_item_ty)
        for item in pattern.items:
            if isinstance(item, Spread):
                if item.name is not None:
                    ctx[item.name] = Forall([], result_ty)
                break
            item_ty = infer_pattern_type(item, ctx)
            unify_type(list_item_ty, item_ty)
        return set_type(pattern, result_ty)
    if isinstance(pattern, Record):
        fields = {}
        rest: TyVar | TyEmptyRow = TyEmptyRow()  # Default closed row
        for key, value in pattern.data.items():
            if isinstance(value, Spread):
                # Open row
                rest = fresh_tyvar()
                if value.name is not None:
                    ctx[value.name] = Forall([], rest)
                break
            fields[key] = infer_pattern_type(value, ctx)
        return set_type(pattern, TyRow(fields, rest))
    raise InferenceError(f"{type(pattern)} isn't allowed in a pattern")


def infer_type(expr: Object, ctx: Context) -> MonoType:
    if isinstance(expr, (Int, Float, Bytes, Hole, String)):
        return infer_common(expr)
    if isinstance(expr, Var):
        scheme = ctx.get(expr.name)
        if scheme is None:
            raise InferenceError(f"Unbound variable {expr.name}")
        return set_type(expr, instantiate(scheme))
    if isinstance(expr, Function):
        arg_tyvar = fresh_tyvar()
        assert isinstance(expr.arg, Var)
        body_ctx = {**ctx, expr.arg.name: Forall([], arg_tyvar)}
        body_ty = infer_type(expr.body, body_ctx)
        return set_type(expr, func_type(arg_tyvar, body_ty))
    if isinstance(expr, Binop):
        left, right = expr.left, expr.right
        op = Var(BinopKind.to_str(expr.op))
        return set_type(expr, infer_type(Apply(Apply(op, left), right), ctx))
    if isinstance(expr, Where):
        assert isinstance(expr.binding, Assign)
        name, value, body = expr.binding.name.name, expr.binding.value, expr.body
        if isinstance(value, (Function, MatchFunction)):
            # Letrec
            func_ty: MonoType = fresh_tyvar()
            value_ty = infer_type(value, {**ctx, name: Forall([], func_ty)})
        else:
            # Let
            value_ty = infer_type(value, ctx)
        value_scheme = generalize(value_ty, ctx)
        body_ty = infer_type(body, {**ctx, name: value_scheme})
        return set_type(expr, body_ty)
    if isinstance(expr, List):
        list_item_ty = fresh_tyvar()
        for item in expr.items:
            assert not isinstance(item, Spread), "Spread can only occur in list match (for now)"
            item_ty = infer_type(item, ctx)
            unify_type(list_item_ty, item_ty)
        return set_type(expr, list_type(list_item_ty))
    if isinstance(expr, MatchCase):
        pattern_ctx: Context = {}
        pattern_ty = infer_pattern_type(expr.pattern, pattern_ctx)
        body_ty = infer_type(expr.body, {**ctx, **pattern_ctx})
        return set_type(expr, func_type(pattern_ty, body_ty))
    if isinstance(expr, Apply):
        func_ty = infer_type(expr.func, ctx)
        arg_ty = infer_type(expr.arg, ctx)
        result = fresh_tyvar()
        unify_type(func_ty, func_type(arg_ty, result))
        return set_type(expr, result)
    if isinstance(expr, MatchFunction):
        result = fresh_tyvar()
        for case in expr.cases:
            case_ty = infer_type(case, ctx)
            unify_type(result, case_ty)
        return set_type(expr, result)
    if isinstance(expr, Record):
        fields = {}
        rest: TyVar | TyEmptyRow = TyEmptyRow()
        for key, value in expr.data.items():
            assert not isinstance(value, Spread), "Spread can only occur in record match (for now)"
            fields[key] = infer_type(value, ctx)
        return set_type(expr, TyRow(fields, rest))
    if isinstance(expr, Access):
        obj_ty = infer_type(expr.obj, ctx)
        value_ty = fresh_tyvar()
        assert isinstance(expr.at, Var)
        # "has field" constraint in the form of an open row
        unify_type(obj_ty, TyRow({expr.at.name: value_ty}, fresh_tyvar()))
        return value_ty
    raise InferenceError(f"Unexpected type {type(expr)}")


def minimize(ty: MonoType) -> MonoType:
    letters = iter("abcdefghijklmnopqrstuvwxyz")
    free = ftv_ty(ty)
    subst = {ftv: TyVar(next(letters)) for ftv in sorted(free)}
    return apply_ty(ty, subst)


class InferTypeTests(unittest.TestCase):
    def setUp(self) -> None:
        global fresh_var_counter
        fresh_var_counter = 0

    def test_unify_tyvar_tyvar(self) -> None:
        a = TyVar("a")
        b = TyVar("b")
        unify_type(a, b)
        self.assertIs(a.find(), b.find())

    def test_unify_tyvar_tycon(self) -> None:
        a = TyVar("a")
        unify_type(a, IntType)
        self.assertIs(a.find(), IntType)
        b = TyVar("b")
        unify_type(b, IntType)
        self.assertIs(b.find(), IntType)

    def test_unify_tycon_tycon_name_mismatch(self) -> None:
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            unify_type(IntType, StringType)

    def test_unify_tycon_tycon_arity_mismatch(self) -> None:
        l = TyCon("x", [TyVar("a")])
        r = TyCon("x", [])
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            unify_type(l, r)

    def test_unify_tycon_tycon_unifies_arg(self) -> None:
        a = TyVar("a")
        b = TyVar("b")
        l = TyCon("x", [a])
        r = TyCon("x", [b])
        unify_type(l, r)
        self.assertIs(a.find(), b.find())

    def test_unify_tycon_tycon_unifies_args(self) -> None:
        a, b, c, d = map(TyVar, "abcd")
        l = func_type(a, b)
        r = func_type(c, d)
        unify_type(l, r)
        self.assertIs(a.find(), c.find())
        self.assertIs(b.find(), d.find())
        self.assertIsNot(a.find(), b.find())

    def test_unify_recursive_fails(self) -> None:
        l = TyVar("a")
        r = TyCon("x", [TyVar("a")])
        with self.assertRaisesRegex(InferenceError, "Occurs check failed"):
            unify_type(l, r)

    def test_unify_empty_row(self) -> None:
        unify_type(TyEmptyRow(), TyEmptyRow())

    def test_unify_empty_row_open(self) -> None:
        l = TyRow({}, TyVar("a"))
        r = TyRow({}, TyVar("b"))
        unify_type(l, r)
        self.assertIs(l.rest.find(), r.rest.find())

    def test_unify_row_unifies_fields(self) -> None:
        a = TyVar("a")
        b = TyVar("b")
        l = TyRow({"x": a})
        r = TyRow({"x": b})
        unify_type(l, r)
        self.assertIs(a.find(), b.find())

    def test_unify_empty_right(self) -> None:
        l = TyRow({"x": IntType})
        r = TyEmptyRow()
        with self.assertRaisesRegex(InferenceError, "Unifying row {x=int} with empty row"):
            unify_type(l, r)

    def test_unify_empty_left(self) -> None:
        l = TyEmptyRow()
        r = TyRow({"x": IntType})
        with self.assertRaisesRegex(InferenceError, "Unifying empty row with row {x=int}"):
            unify_type(l, r)

    def test_unify_missing_closed(self) -> None:
        l = TyRow({"x": IntType})
        r = TyRow({"y": IntType})
        with self.assertRaisesRegex(InferenceError, "Unifying empty row with row {y=int, ...'t0}"):
            unify_type(l, r)

    def test_unify_one_open_one_closed(self) -> None:
        rest = TyVar("r1")
        l = TyRow({"x": IntType})
        r = TyRow({"x": IntType}, rest)
        unify_type(l, r)
        self.assertTyEqual(rest.find(), TyEmptyRow())

    def test_unify_one_open_more_than_one_closed(self) -> None:
        rest = TyVar("r1")
        l = TyRow({"x": IntType})
        r = TyRow({"x": IntType, "y": StringType}, rest)
        with self.assertRaisesRegex(InferenceError, "Unifying empty row with row {y=string, ...'r1}"):
            unify_type(l, r)

    def test_unify_one_open_one_closed_more(self) -> None:
        rest = TyVar("r1")
        l = TyRow({"x": IntType, "y": StringType})
        r = TyRow({"x": IntType}, rest)
        unify_type(l, r)
        self.assertTyEqual(rest.find(), TyRow({"y": StringType}))

    def test_unify_left_missing_open(self) -> None:
        l = TyRow({}, TyVar("r0"))
        r = TyRow({"y": IntType}, TyVar("r1"))
        unify_type(l, r)
        self.assertTyEqual(l.rest, TyRow({"y": IntType}, TyVar("r1")))
        assert isinstance(r.rest, TyVar)
        self.assertTrue(r.rest.is_unbound())

    def test_unify_right_missing_open(self) -> None:
        l = TyRow({"x": IntType}, TyVar("r0"))
        r = TyRow({}, TyVar("r1"))
        unify_type(l, r)
        assert isinstance(l.rest, TyVar)
        self.assertTrue(l.rest.is_unbound())
        self.assertTyEqual(r.rest, TyRow({"x": IntType}, TyVar("r0")))

    def test_unify_both_missing_open(self) -> None:
        l = TyRow({"x": IntType}, TyVar("r0"))
        r = TyRow({"y": IntType}, TyVar("r1"))
        unify_type(l, r)
        self.assertTyEqual(l.rest, TyRow({"y": IntType}, TyVar("t0")))
        self.assertTyEqual(r.rest, TyRow({"x": IntType}, TyVar("t0")))

    def test_minimize_tyvar(self) -> None:
        ty = fresh_tyvar()
        self.assertEqual(minimize(ty), TyVar("a"))

    def test_minimize_tycon(self) -> None:
        ty = func_type(TyVar("t0"), TyVar("t1"), TyVar("t0"))
        self.assertEqual(minimize(ty), func_type(TyVar("a"), TyVar("b"), TyVar("a")))

    def infer(self, expr: Object, ctx: Context) -> MonoType:
        return minimize(infer_type(expr, ctx))

    def assertTyEqual(self, l: MonoType, r: MonoType) -> bool:
        l = l.find()
        r = r.find()
        if isinstance(l, TyVar) and isinstance(r, TyVar):
            if l != r:
                self.fail(f"Type mismatch: {l} != {r}")
            return True
        if isinstance(l, TyCon) and isinstance(r, TyCon):
            if l.name != r.name:
                self.fail(f"Type mismatch: {l} != {r}")
            if len(l.args) != len(r.args):
                self.fail(f"Type mismatch: {l} != {r}")
            for l_arg, r_arg in zip(l.args, r.args):
                self.assertTyEqual(l_arg, r_arg)
            return True
        if isinstance(l, TyEmptyRow) and isinstance(r, TyEmptyRow):
            return True
        if isinstance(l, TyRow) and isinstance(r, TyRow):
            l_keys = set(l.fields.keys())
            r_keys = set(r.fields.keys())
            if l_keys != r_keys:
                self.fail(f"Type mismatch: {l} != {r}")
            for key in l_keys:
                self.assertTyEqual(l.fields[key], r.fields[key])
            self.assertTyEqual(l.rest, r.rest)
            return True
        self.fail(f"Type mismatch: {l} != {r}")

    def test_unbound_var(self) -> None:
        with self.assertRaisesRegex(InferenceError, "Unbound variable"):
            self.infer(Var("a"), {})

    def test_var_instantiates_scheme(self) -> None:
        ty = self.infer(Var("a"), {"a": Forall([TyVar("b")], TyVar("b"))})
        self.assertTyEqual(ty, TyVar("a"))

    def test_int(self) -> None:
        ty = self.infer(Int(123), {})
        self.assertTyEqual(ty, IntType)

    def test_float(self) -> None:
        ty = self.infer(Float(1.0), {})
        self.assertTyEqual(ty, FloatType)

    def test_string(self) -> None:
        ty = self.infer(String("abc"), {})
        self.assertTyEqual(ty, StringType)

    def test_function_returns_arg(self) -> None:
        ty = self.infer(Function(Var("x"), Var("x")), {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("a")))

    def test_nested_function_outer(self) -> None:
        ty = self.infer(Function(Var("x"), Function(Var("y"), Var("x"))), {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("b"), TyVar("a")))

    def test_nested_function_inner(self) -> None:
        ty = self.infer(Function(Var("x"), Function(Var("y"), Var("y"))), {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("b"), TyVar("b")))

    def test_apply_id_int(self) -> None:
        func = Function(Var("x"), Var("x"))
        arg = Int(123)
        ty = self.infer(Apply(func, arg), {})
        self.assertTyEqual(ty, IntType)

    def test_apply_two_arg_returns_function(self) -> None:
        func = Function(Var("x"), Function(Var("y"), Var("x")))
        arg = Int(123)
        ty = self.infer(Apply(func, arg), {})
        self.assertTyEqual(ty, func_type(TyVar("a"), IntType))

    def test_binop_add_constrains_int(self) -> None:
        expr = Binop(BinopKind.ADD, Var("x"), Var("y"))
        ty = self.infer(
            expr,
            {
                "x": Forall([], TyVar("a")),
                "y": Forall([], TyVar("b")),
                "+": Forall([], func_type(IntType, IntType, IntType)),
            },
        )
        self.assertTyEqual(ty, IntType)

    def test_binop_add_function_constrains_int(self) -> None:
        x = Var("x")
        y = Var("y")
        expr = Function(Var("x"), Function(Var("y"), Binop(BinopKind.ADD, x, y)))
        ty = self.infer(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(IntType, IntType, IntType))
        self.assertTyEqual(type_of(x), IntType)
        self.assertTyEqual(type_of(y), IntType)

    def test_let(self) -> None:
        expr = Where(Var("f"), Assign(Var("f"), Function(Var("x"), Var("x"))))
        ty = self.infer(expr, {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("a")))

    def test_apply_monotype_to_different_types_raises(self) -> None:
        expr = Where(
            Where(Var("x"), Assign(Var("x"), Apply(Var("f"), Int(123)))),
            Assign(Var("y"), Apply(Var("f"), Float(123.0))),
        )
        ctx = {"f": Forall([], func_type(TyVar("a"), TyVar("a")))}
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            self.infer(expr, ctx)

    def test_apply_polytype_to_different_types(self) -> None:
        expr = Where(
            Where(Var("x"), Assign(Var("x"), Apply(Var("f"), Int(123)))),
            Assign(Var("y"), Apply(Var("f"), Float(123.0))),
        )
        ty = self.infer(expr, {"f": Forall([TyVar("a")], func_type(TyVar("a"), TyVar("a")))})
        self.assertTyEqual(ty, IntType)

    def test_generalization(self) -> None:
        # From https://okmij.org/ftp/ML/generalization.html
        expr = parse(tokenize("x -> (y . y = x)"))
        ty = self.infer(expr, {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("a")))

    def test_generalization2(self) -> None:
        # From https://okmij.org/ftp/ML/generalization.html
        expr = parse(tokenize("x -> (y . y = z -> x z)"))
        ty = self.infer(expr, {})
        self.assertTyEqual(ty, func_type(func_type(TyVar("a"), TyVar("b")), func_type(TyVar("a"), TyVar("b"))))

    def test_id(self) -> None:
        expr = Function(Var("x"), Var("x"))
        ty = self.infer(expr, {})
        self.assertTyEqual(ty, func_type(TyVar("a"), TyVar("a")))

    def test_empty_list(self) -> None:
        expr = List([])
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, TyCon("list", [TyVar("t0")]))

    def test_list_int(self) -> None:
        expr = List([Int(123)])
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, TyCon("list", [IntType]))

    def test_list_mismatch(self) -> None:
        expr = List([Int(123), Float(123.0)])
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            infer_type(expr, {})

    def test_recursive_fact(self) -> None:
        expr = parse(tokenize("fact . fact = | 0 -> 1 | n -> n * fact (n-1)"))
        ty = infer_type(
            expr,
            {
                "*": Forall([], func_type(IntType, IntType, IntType)),
                "-": Forall([], func_type(IntType, IntType, IntType)),
            },
        )
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_match_int_int(self) -> None:
        expr = parse(tokenize("| 0 -> 1"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_match_int_int_two_cases(self) -> None:
        expr = parse(tokenize("| 0 -> 1 | 1 -> 2"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_match_int_int_int_float(self) -> None:
        expr = parse(tokenize("| 0 -> 1 | 1 -> 2.0"))
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            infer_type(expr, {})

    def test_match_int_int_float_int(self) -> None:
        expr = parse(tokenize("| 0 -> 1 | 1.0 -> 2"))
        with self.assertRaisesRegex(InferenceError, "Unification failed"):
            infer_type(expr, {})

    def test_match_var(self) -> None:
        expr = parse(tokenize("| x -> x + 1"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_match_int_var(self) -> None:
        expr = parse(tokenize("| 0 -> 1 | x -> x"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_match_list_of_int(self) -> None:
        expr = parse(tokenize("| [x] -> x + 1"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(list_type(IntType), IntType))

    def test_match_list_of_int_to_list(self) -> None:
        expr = parse(tokenize("| [x] -> [x + 1]"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(list_type(IntType), list_type(IntType)))

    def test_match_list_of_int_to_int(self) -> None:
        expr = parse(tokenize("| [] -> 0 | [x] -> 1 | [x, y] -> x+y"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(list_type(IntType), IntType))

    def test_recursive_var_is_unbound(self) -> None:
        expr = parse(tokenize("a . a = a"))
        with self.assertRaisesRegex(InferenceError, "Unbound variable"):
            infer_type(expr, {})

    def test_recursive(self) -> None:
        expr = parse(
            tokenize("""
        length
        . length =
        | [] -> 0
        | [x, ...xs] -> 1 + length xs
        """)
        )
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(list_type(TyVar("t8")), IntType))

    def test_match_list_to_list(self) -> None:
        expr = parse(tokenize("| [] -> [] | x -> x"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(list_type(TyVar("t1")), list_type(TyVar("t1"))))

    def test_match_list_spread(self) -> None:
        expr = parse(tokenize("head . head = | [x, ...] -> x"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(list_type(TyVar("t4")), TyVar("t4")))

    def test_match_list_spread_rest(self) -> None:
        expr = parse(tokenize("tail . tail = | [x, ...xs] -> xs"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(list_type(TyVar("t4")), list_type(TyVar("t4"))))

    def test_match_list_spread_named(self) -> None:
        expr = parse(tokenize("sum . sum = | [] -> 0 | [x, ...xs] -> x + sum xs"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(list_type(IntType), IntType))

    def test_match_list_int_to_list(self) -> None:
        expr = parse(tokenize("| [] -> [3] | x -> x"))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(list_type(IntType), list_type(IntType)))

    def test_inc(self) -> None:
        expr = parse(tokenize("inc . inc = | 0 -> 1 | 1 -> 2 | a -> a + 1"))
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(IntType, IntType))

    def test_bytes(self) -> None:
        expr = Bytes(b"abc")
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, BytesType)

    def test_hole(self) -> None:
        expr = Hole()
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, HoleType)

    def test_string_concat(self) -> None:
        expr = parse(tokenize('"a" ++ "b"'))
        ty = infer_type(expr, OP_ENV)
        self.assertTyEqual(ty, StringType)

    def test_cons(self) -> None:
        expr = parse(tokenize("1 >+ [2]"))
        ty = infer_type(expr, OP_ENV)
        self.assertTyEqual(ty, list_type(IntType))

    def test_append(self) -> None:
        expr = parse(tokenize("[1] +< 2"))
        ty = infer_type(expr, OP_ENV)
        self.assertTyEqual(ty, list_type(IntType))

    def test_record(self) -> None:
        expr = Record({"a": Int(1), "b": String("hello")})
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, TyRow({"a": IntType, "b": StringType}))

    def test_match_record(self) -> None:
        expr = MatchFunction(
            [
                MatchCase(
                    Record({"x": Var("x")}),
                    Var("x"),
                )
            ]
        )
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(TyRow({"x": TyVar("t1")}), TyVar("t1")))

    def test_access_poly(self) -> None:
        expr = Function(Var("r"), Access(Var("r"), Var("x")))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, func_type(TyRow({"x": TyVar("t1")}, TyVar("t2")), TyVar("t1")))

    def test_apply_row(self) -> None:
        row0 = Record({"x": Int(1)})
        row1 = Record({"x": Int(1), "y": Int(2)})
        scheme = Forall([], func_type(TyRow({"x": IntType}, TyVar("a")), IntType))
        ty0 = infer_type(Apply(Var("f"), row0), {"f": scheme})
        self.assertTyEqual(ty0, IntType)
        with self.assertRaisesRegex(InferenceError, "Unifying empty row with row {y=int}"):
            infer_type(Apply(Var("f"), row1), {"f": scheme})

    def test_apply_row_polymorphic(self) -> None:
        row0 = Record({"x": Int(1)})
        row1 = Record({"x": Int(1), "y": Int(2)})
        row2 = Record({"x": Int(1), "y": Int(2), "z": Int(3)})
        scheme = Forall([TyVar("a")], func_type(TyRow({"x": IntType}, TyVar("a")), IntType))
        ty0 = infer_type(Apply(Var("f"), row0), {"f": scheme})
        self.assertTyEqual(ty0, IntType)
        ty1 = infer_type(Apply(Var("f"), row1), {"f": scheme})
        self.assertTyEqual(ty1, IntType)
        ty2 = infer_type(Apply(Var("f"), row2), {"f": scheme})
        self.assertTyEqual(ty2, IntType)

    def test_example_rec_access(self) -> None:
        expr = parse(tokenize('rec@a . rec = { a = 1, b = "x" }'))
        ty = infer_type(expr, {})
        self.assertTyEqual(ty, IntType)

    def test_example_rec_access_poly(self) -> None:
        expr = parse(
            tokenize("""
(get_x {x=1}) + (get_x {x=2,y=3})
. get_x = | { x=x, ... } -> x
""")
        )
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, IntType)

    def test_example_rec_access_poly_named_bug(self) -> None:
        expr = parse(
            tokenize("""
(filter_x {x=1, y=2}) + 3
. filter_x = | { x=x, ...xs } -> xs
""")
        )
        with self.assertRaisesRegex(InferenceError, "Cannot unify int and {y=int}"):
            infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})

    def test_example_rec_access_rest(self) -> None:
        expr = parse(
            tokenize("""
| { x=x, ...xs } -> xs
""")
        )
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, func_type(TyRow({"x": TyVar("t1")}, TyVar("t2")), TyVar("t2")))

    def test_example_match_rec_access_rest(self) -> None:
        expr = parse(
            tokenize("""
filter_x {x=1, y=2}
. filter_x = | { x=x, ...xs } -> xs
""")
        )
        ty = infer_type(expr, {"+": Forall([], func_type(IntType, IntType, IntType))})
        self.assertTyEqual(ty, TyRow({"y": IntType}))

    def test_example_rec_access_poly_named(self) -> None:
        expr = parse(
            tokenize("""
[(filter_x {x=1, y=2}), (filter_x {x=2, y=3, z=4})]
. filter_x = | { x=x, ...xs } -> xs
""")
        )
        with self.assertRaisesRegex(InferenceError, "Unifying empty row with row {z=int}"):
            infer_type(expr, {})


class SerializerTests(unittest.TestCase):
    def _serialize(self, obj: Object) -> bytes:
        serializer = Serializer()
        serializer.serialize(obj)
        return bytes(serializer.output)

    def test_short(self) -> None:
        self.assertEqual(self._serialize(Int(-1)), TYPE_SHORT + b"\x01")
        self.assertEqual(self._serialize(Int(0)), TYPE_SHORT + b"\x00")
        self.assertEqual(self._serialize(Int(1)), TYPE_SHORT + b"\x02")
        self.assertEqual(self._serialize(Int(-(2**33))), TYPE_SHORT + b"\xff\xff\xff\xff?")
        self.assertEqual(self._serialize(Int(2**33)), TYPE_SHORT + b"\x80\x80\x80\x80@")
        self.assertEqual(self._serialize(Int(-(2**63))), TYPE_SHORT + b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01")
        self.assertEqual(self._serialize(Int(2**63 - 1)), TYPE_SHORT + b"\xfe\xff\xff\xff\xff\xff\xff\xff\xff\x01")

    def test_long(self) -> None:
        self.assertEqual(
            self._serialize(Int(2**100)),
            TYPE_LONG + b"\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00 \x00\x00\x00",
        )
        self.assertEqual(
            self._serialize(Int(-(2**100))),
            TYPE_LONG + b"\x04\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x1f\x00\x00\x00",
        )

    def test_string(self) -> None:
        self.assertEqual(self._serialize(String("hello")), TYPE_STRING + b"\nhello")

    def test_empty_list(self) -> None:
        obj = List([])
        self.assertEqual(self._serialize(obj), ref(TYPE_LIST) + b"\x00")

    def test_list(self) -> None:
        obj = List([Int(123), Int(456)])
        self.assertEqual(self._serialize(obj), ref(TYPE_LIST) + b"\x04i\xf6\x01i\x90\x07")

    def test_self_referential_list(self) -> None:
        obj = List([])
        obj.items.append(obj)
        self.assertEqual(self._serialize(obj), ref(TYPE_LIST) + b"\x02r\x00")

    def test_variant(self) -> None:
        obj = Variant("abc", Int(123))
        self.assertEqual(self._serialize(obj), TYPE_VARIANT + b"\x06abci\xf6\x01")

    def test_record(self) -> None:
        obj = Record({"x": Int(1), "y": Int(2)})
        self.assertEqual(self._serialize(obj), TYPE_RECORD + b"\x04\x02xi\x02\x02yi\x04")

    def test_var(self) -> None:
        obj = Var("x")
        self.assertEqual(self._serialize(obj), TYPE_VAR + b"\x02x")

    def test_function(self) -> None:
        obj = Function(Var("x"), Var("x"))
        self.assertEqual(self._serialize(obj), TYPE_FUNCTION + b"v\x02xv\x02x")

    def test_empty_match_function(self) -> None:
        obj = MatchFunction([])
        self.assertEqual(self._serialize(obj), TYPE_MATCH_FUNCTION + b"\x00")

    def test_match_function(self) -> None:
        obj = MatchFunction([MatchCase(Int(1), Var("x")), MatchCase(List([Int(1)]), Var("y"))])
        self.assertEqual(self._serialize(obj), TYPE_MATCH_FUNCTION + b"\x04i\x02v\x02x\xdb\x02i\x02v\x02y")

    def test_closure(self) -> None:
        obj = Closure({}, Function(Var("x"), Var("x")))
        self.assertEqual(self._serialize(obj), ref(TYPE_CLOSURE) + b"fv\x02xv\x02x\x00")

    def test_self_referential_closure(self) -> None:
        obj = Closure({}, Function(Var("x"), Var("x")))
        assert isinstance(obj.env, dict)  # For mypy
        obj.env["self"] = obj
        self.assertEqual(self._serialize(obj), ref(TYPE_CLOSURE) + b"fv\x02xv\x02x\x02\x08selfr\x00")

    def test_bytes(self) -> None:
        obj = Bytes(b"abc")
        self.assertEqual(self._serialize(obj), TYPE_BYTES + b"\x06abc")

    def test_float(self) -> None:
        obj = Float(3.14)
        self.assertEqual(self._serialize(obj), TYPE_FLOAT + b"\x1f\x85\xebQ\xb8\x1e\t@")

    def test_hole(self) -> None:
        self.assertEqual(self._serialize(Hole()), TYPE_HOLE)

    def test_assign(self) -> None:
        obj = Assign(Var("x"), Int(123))
        self.assertEqual(self._serialize(obj), TYPE_ASSIGN + b"v\x02xi\xf6\x01")

    def test_binop(self) -> None:
        obj = Binop(BinopKind.ADD, Int(3), Int(4))
        self.assertEqual(self._serialize(obj), TYPE_BINOP + b"\x02+i\x06i\x08")

    def test_apply(self) -> None:
        obj = Apply(Var("f"), Var("x"))
        self.assertEqual(self._serialize(obj), TYPE_APPLY + b"v\x02fv\x02x")

    def test_where(self) -> None:
        obj = Where(Var("a"), Var("b"))
        self.assertEqual(self._serialize(obj), TYPE_WHERE + b"v\x02av\x02b")

    def test_access(self) -> None:
        obj = Access(Var("a"), Var("b"))
        self.assertEqual(self._serialize(obj), TYPE_ACCESS + b"v\x02av\x02b")

    def test_spread(self) -> None:
        self.assertEqual(self._serialize(Spread()), TYPE_SPREAD)
        self.assertEqual(self._serialize(Spread("rest")), TYPE_NAMED_SPREAD + b"\x08rest")


class RoundTripSerializationTests(unittest.TestCase):
    def _serialize(self, obj: Object) -> bytes:
        serializer = Serializer()
        serializer.serialize(obj)
        return bytes(serializer.output)

    def _deserialize(self, flat: bytes) -> Object:
        deserializer = Deserializer(flat)
        return deserializer.parse()

    def _serde(self, obj: Object) -> Object:
        flat = self._serialize(obj)
        return self._deserialize(flat)

    def _rt(self, obj: Object) -> None:
        result = self._serde(obj)
        self.assertEqual(result, obj)

    def test_short(self) -> None:
        for i in range(-(2**16), 2**16):
            self._rt(Int(i))

        self._rt(Int(-(2**63)))
        self._rt(Int(2**63 - 1))

    def test_long(self) -> None:
        self._rt(Int(2**100))
        self._rt(Int(-(2**100)))

    def test_string(self) -> None:
        self._rt(String(""))
        self._rt(String("a"))
        self._rt(String("hello"))

    def test_list(self) -> None:
        self._rt(List([]))
        self._rt(List([Int(123), Int(345)]))

    def test_self_referential_list(self) -> None:
        ls = List([])
        ls.items.append(ls)
        result = self._serde(ls)
        self.assertIsInstance(result, List)
        assert isinstance(result, List)  # For mypy
        self.assertIsInstance(result.items, list)
        self.assertEqual(len(result.items), 1)
        self.assertIs(result.items[0], result)

    def test_record(self) -> None:
        self._rt(Record({"x": Int(1), "y": Int(2)}))

    def test_variant(self) -> None:
        self._rt(Variant("abc", Int(123)))

    def test_var(self) -> None:
        self._rt(Var("x"))

    def test_function(self) -> None:
        self._rt(Function(Var("x"), Var("x")))

    def test_empty_match_function(self) -> None:
        self._rt(MatchFunction([]))

    def test_match_function(self) -> None:
        obj = MatchFunction([MatchCase(Int(1), Var("x")), MatchCase(List([Int(1)]), Var("y"))])
        self._rt(obj)

    def test_closure(self) -> None:
        self._rt(Closure({}, Function(Var("x"), Var("x"))))

    def test_self_referential_closure(self) -> None:
        obj = Closure({}, Function(Var("x"), Var("x")))
        assert isinstance(obj.env, dict)  # For mypy
        obj.env["self"] = obj
        result = self._serde(obj)
        self.assertIsInstance(result, Closure)
        assert isinstance(result, Closure)  # For mypy
        self.assertIsInstance(result.env, dict)
        self.assertEqual(len(result.env), 1)
        self.assertIs(result.env["self"], result)

    def test_bytes(self) -> None:
        self._rt(Bytes(b"abc"))

    def test_float(self) -> None:
        self._rt(Float(3.14))

    def test_hole(self) -> None:
        self._rt(Hole())

    def test_assign(self) -> None:
        self._rt(Assign(Var("x"), Int(123)))

    def test_binop(self) -> None:
        self._rt(Binop(BinopKind.ADD, Int(3), Int(4)))

    def test_apply(self) -> None:
        self._rt(Apply(Var("f"), Var("x")))

    def test_where(self) -> None:
        self._rt(Where(Var("a"), Var("b")))

    def test_access(self) -> None:
        self._rt(Access(Var("a"), Var("b")))

    def test_spread(self) -> None:
        self._rt(Spread())
        self._rt(Spread("rest"))


class ScrapMonadTests(unittest.TestCase):
    def test_create_copies_env(self) -> None:
        env = {"a": Int(123)}
        result = ScrapMonad(env)
        self.assertEqual(result.env, env)
        self.assertIsNot(result.env, env)

    def test_bind_returns_new_monad(self) -> None:
        env = {"a": Int(123)}
        orig = ScrapMonad(env)
        result, next_monad = orig.bind(Assign(Var("b"), Int(456)))
        self.assertEqual(orig.env, {"a": Int(123)})
        self.assertEqual(next_monad.env, {"a": Int(123), "b": Int(456)})


Number = typing.Union[int, float]


class Repr(typing.Protocol):
    def __call__(self, obj: Object, prec: Number = 0) -> str: ...


# Can't use reprlib.recursive_repr because it doesn't work if the print
# function has more than one argument (for example, prec)
def handle_recursion(func: Repr) -> Repr:
    cache: typing.List[Object] = []

    @functools.wraps(func)
    def wrapper(obj: Object, prec: Number = 0) -> str:
        for cached in cache:
            if obj is cached:
                return "..."
        cache.append(obj)
        result = func(obj, prec)
        cache.remove(obj)
        return result

    return wrapper


@handle_recursion
def pretty(obj: Object, prec: Number = 0) -> str:
    if isinstance(obj, Int):
        return str(obj.value)
    if isinstance(obj, Float):
        return str(obj.value)
    if isinstance(obj, String):
        return json.dumps(obj.value)
    if isinstance(obj, Bytes):
        return f"~~{base64.b64encode(obj.value).decode()}"
    if isinstance(obj, Var):
        return obj.name
    if isinstance(obj, Hole):
        return "()"
    if isinstance(obj, Spread):
        return f"...{obj.name}" if obj.name else "..."
    if isinstance(obj, List):
        return f"[{', '.join(pretty(item) for item in obj.items)}]"
    if isinstance(obj, Record):
        return f"{{{', '.join(f'{key} = {pretty(value)}' for key, value in obj.data.items())}}}"
    if isinstance(obj, Closure):
        keys = list(obj.env.keys())
        return f"Closure({keys}, {pretty(obj.func)})"
    if isinstance(obj, EnvObject):
        return f"EnvObject({repr(obj.env)})"
    if isinstance(obj, NativeFunction):
        return f"NativeFunction(name={obj.name})"
    if isinstance(obj, Relocation):
        return f"Relocation(name={repr(obj.name)})"
    if isinstance(obj, Variant):
        op_prec = PS["#"]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"#{obj.tag} {pretty(obj.value, right_prec)}"
    if isinstance(obj, Assign):
        op_prec = PS["="]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.name, left_prec)} = {pretty(obj.value, right_prec)}"
    if isinstance(obj, Binop):
        op_prec = PS[BinopKind.to_str(obj.op)]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.left, left_prec)} {BinopKind.to_str(obj.op)} {pretty(obj.right, right_prec)}"
    if isinstance(obj, Function):
        op_prec = PS["->"]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        assert isinstance(obj.arg, Var)
        result = f"{obj.arg.name} -> {pretty(obj.body, right_prec)}"
    if isinstance(obj, MatchFunction):
        op_prec = PS["|"]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = "\n".join(
            f"| {pretty(case.pattern, left_prec)} -> {pretty(case.body, right_prec)}" for case in obj.cases
        )
    if isinstance(obj, Where):
        op_prec = PS["."]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.body, left_prec)} . {pretty(obj.binding, right_prec)}"
    if isinstance(obj, Assert):
        op_prec = PS["!"]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.value, left_prec)} ! {pretty(obj.cond, right_prec)}"
    if isinstance(obj, Apply):
        op_prec = PS[""]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.func, left_prec)} {pretty(obj.arg, right_prec)}"
    if isinstance(obj, Access):
        op_prec = PS["@"]
        left_prec, right_prec = op_prec.pl, op_prec.pr
        result = f"{pretty(obj.obj, left_prec)} @ {pretty(obj.at, right_prec)}"
    if prec >= op_prec.pl:
        return f"({result})"
    return result


class PrettyPrintTests(unittest.TestCase):
    def test_pretty_print_int(self) -> None:
        obj = Int(1)
        self.assertEqual(pretty(obj), "1")

    def test_pretty_print_float(self) -> None:
        obj = Float(3.14)
        self.assertEqual(pretty(obj), "3.14")

    def test_pretty_print_string(self) -> None:
        obj = String("hello")
        self.assertEqual(pretty(obj), '"hello"')

    def test_pretty_print_bytes(self) -> None:
        obj = Bytes(b"abc")
        self.assertEqual(pretty(obj), "~~YWJj")

    def test_pretty_print_var(self) -> None:
        obj = Var("ref")
        self.assertEqual(pretty(obj), "ref")

    def test_pretty_print_hole(self) -> None:
        obj = Hole()
        self.assertEqual(pretty(obj), "()")

    def test_pretty_print_spread(self) -> None:
        obj = Spread()
        self.assertEqual(pretty(obj), "...")

    def test_pretty_print_named_spread(self) -> None:
        obj = Spread("rest")
        self.assertEqual(pretty(obj), "...rest")

    def test_pretty_print_binop(self) -> None:
        obj = Binop(BinopKind.ADD, Int(1), Int(2))
        self.assertEqual(pretty(obj), "1 + 2")

    def test_pretty_print_binop_precedence(self) -> None:
        obj = Binop(BinopKind.ADD, Int(1), Binop(BinopKind.MUL, Int(2), Int(3)))
        self.assertEqual(pretty(obj), "1 + 2 * 3")
        obj = Binop(BinopKind.MUL, Binop(BinopKind.ADD, Int(1), Int(2)), Int(3))
        self.assertEqual(pretty(obj), "(1 + 2) * 3")

    def test_pretty_print_int_list(self) -> None:
        obj = List([Int(1), Int(2), Int(3)])
        self.assertEqual(pretty(obj), "[1, 2, 3]")

    def test_pretty_print_str_list(self) -> None:
        obj = List([String("1"), String("2"), String("3")])
        self.assertEqual(pretty(obj), '["1", "2", "3"]')

    def test_pretty_print_recursion(self) -> None:
        obj = List([])
        obj.items.append(obj)
        self.assertEqual(pretty(obj), "[...]")

    def test_pretty_print_assign(self) -> None:
        obj = Assign(Var("x"), Int(3))
        self.assertEqual(pretty(obj), "x = 3")

    def test_pretty_print_function(self) -> None:
        obj = Function(Var("x"), Binop(BinopKind.ADD, Int(1), Var("x")))
        self.assertEqual(pretty(obj), "x -> 1 + x")

    def test_pretty_print_nested_function(self) -> None:
        obj = Function(Var("x"), Function(Var("y"), Binop(BinopKind.ADD, Var("x"), Var("y"))))
        self.assertEqual(pretty(obj), "x -> y -> x + y")

    def test_pretty_print_apply(self) -> None:
        obj = Apply(Var("x"), Var("y"))
        self.assertEqual(pretty(obj), "x y")

    def test_pretty_print_compose(self) -> None:
        gensym_reset()
        obj = parse(tokenize("(x -> x + 3) << (x -> x * 2)"))
        self.assertEqual(
            pretty(obj),
            "$v0 -> (x -> x + 3) ((x -> x * 2) $v0)",
        )
        gensym_reset()
        obj = parse(tokenize("(x -> x + 3) >> (x -> x * 2)"))
        self.assertEqual(
            pretty(obj),
            "$v0 -> (x -> x * 2) ((x -> x + 3) $v0)",
        )

    def test_pretty_print_where(self) -> None:
        obj = Where(
            Binop(BinopKind.ADD, Var("a"), Var("b")),
            Assign(Var("a"), Int(1)),
        )
        self.assertEqual(pretty(obj), "a + b . a = 1")

    def test_pretty_print_assert(self) -> None:
        obj = Assert(Int(123), Variant("true", String("foo")))
        self.assertEqual(pretty(obj), '123 ! #true "foo"')

    def test_pretty_print_envobject(self) -> None:
        obj = EnvObject({"x": Int(1)})
        self.assertEqual(pretty(obj), "EnvObject({'x': Int(value=1)})")

    def test_pretty_print_matchfunction(self) -> None:
        obj = MatchFunction([MatchCase(Var("y"), Var("x"))])
        self.assertEqual(pretty(obj), "| y -> x")

    def test_pretty_print_matchfunction_precedence(self) -> None:
        obj = MatchFunction(
            [
                MatchCase(Var("a"), MatchFunction([MatchCase(Var("b"), Var("c"))])),
                MatchCase(Var("x"), MatchFunction([MatchCase(Var("y"), Var("z"))])),
            ]
        )
        self.assertEqual(
            pretty(obj),
            """\
| a -> (| b -> c)
| x -> (| y -> z)""",
        )

    def test_pretty_print_relocation(self) -> None:
        obj = Relocation("relocate")
        self.assertEqual(pretty(obj), "Relocation(name='relocate')")

    def test_pretty_print_nativefunction(self) -> None:
        obj = NativeFunction("times2", lambda x: Int(x.value * 2))  # type: ignore [attr-defined]
        self.assertEqual(pretty(obj), "NativeFunction(name=times2)")

    def test_pretty_print_closure(self) -> None:
        obj = Closure({"a": Int(123)}, Function(Var("x"), Var("x")))
        self.assertEqual(pretty(obj), "Closure(['a'], x -> x)")

    def test_pretty_print_record(self) -> None:
        obj = Record({"a": Int(1), "b": Int(2)})
        self.assertEqual(pretty(obj), "{a = 1, b = 2}")

    def test_pretty_print_access(self) -> None:
        obj = Access(Record({"a": Int(4)}), Var("a"))
        self.assertEqual(pretty(obj), "{a = 4} @ a")

    def test_pretty_print_variant(self) -> None:
        obj = Variant("x", Int(123))
        self.assertEqual(pretty(obj), "#x 123")

        obj = Variant("x", Function(Var("a"), Var("b")))
        self.assertEqual(pretty(obj), "#x (a -> b)")


def fetch(url: Object) -> Object:
    if not isinstance(url, String):
        raise TypeError(f"fetch expected String, but got {type(url).__name__}")
    with urllib.request.urlopen(url.value) as f:
        return String(f.read().decode("utf-8"))


def make_object(pyobj: object) -> Object:
    assert not isinstance(pyobj, Object)
    if isinstance(pyobj, int):
        return Int(pyobj)
    if isinstance(pyobj, str):
        return String(pyobj)
    if isinstance(pyobj, list):
        return List([make_object(o) for o in pyobj])
    if isinstance(pyobj, dict):
        # Assumed to only be called with JSON, so string keys.
        return Record({key: make_object(value) for key, value in pyobj.items()})
    raise NotImplementedError(type(pyobj))


def jsondecode(obj: Object) -> Object:
    if not isinstance(obj, String):
        raise TypeError(f"jsondecode expected String, but got {type(obj).__name__}")
    data = json.loads(obj.value)
    return make_object(data)


def listlength(obj: Object) -> Object:
    # TODO(max): Implement in scrapscript once list pattern matching is
    # implemented.
    if not isinstance(obj, List):
        raise TypeError(f"listlength expected List, but got {type(obj).__name__}")
    return Int(len(obj.items))


def serialize(obj: Object) -> bytes:
    serializer = Serializer()
    serializer.serialize(obj)
    return bytes(serializer.output)


def deserialize(data: bytes) -> Object:
    deserializer = Deserializer(data)
    return deserializer.parse()


def deserialize_object(obj: Object) -> Object:
    assert isinstance(obj, Bytes)
    return deserialize(obj.value)


STDLIB = {
    "$$add": Closure({}, Function(Var("x"), Function(Var("y"), Binop(BinopKind.ADD, Var("x"), Var("y"))))),
    "$$fetch": NativeFunction("$$fetch", fetch),
    "$$jsondecode": NativeFunction("$$jsondecode", jsondecode),
    "$$serialize": NativeFunction("$$serialize", lambda obj: Bytes(serialize(obj))),
    "$$deserialize": NativeFunction("$$deserialize", deserialize_object),
    "$$listlength": NativeFunction("$$listlength", listlength),
}


PRELUDE = """
id = x -> x

. quicksort =
  | [] -> []
  | [p, ...xs] -> (concat ((quicksort (ltp xs p)) +< p) (quicksort (gtp xs p))
    . gtp = xs -> p -> filter (x -> x >= p) xs
    . ltp = xs -> p -> filter (x -> x < p) xs)

. filter = f ->
  | [] -> []
  | [x, ...xs] -> f x |> | #true () -> x >+ filter f xs
                         | #false () -> filter f xs

. concat = xs ->
  | [] -> xs
  | [y, ...ys] -> concat (xs +< y) ys

. map = f ->
  | [] -> []
  | [x, ...xs] -> f x >+ map f xs

. range =
  | 0 -> []
  | i -> range (i - 1) +< (i - 1)

. foldr = f -> a ->
  | [] -> a
  | [x, ...xs] -> f x (foldr f a xs)

. take =
  | 0 -> xs -> []
  | n ->
    | [] -> []
    | [x, ...xs] -> x >+ take (n - 1) xs

. all = f ->
  | [] -> #true ()
  | [x, ...xs] -> f x && all f xs

. any = f ->
  | [] -> #false ()
  | [x, ...xs] -> f x || any f xs
"""


def boot_env() -> Env:
    env_object = eval_exp(STDLIB, parse(tokenize(PRELUDE)))
    assert isinstance(env_object, EnvObject)
    return env_object.env


class Completer:
    def __init__(self, env: Env) -> None:
        self.env: Env = env
        self.matches: typing.List[str] = []

    def complete(self, text: str, state: int) -> Optional[str]:
        assert "@" not in text, "TODO: handle attr/index access"
        if state == 0:
            options = sorted(self.env.keys())
            if not text:
                self.matches = options[:]
            else:
                self.matches = [key for key in options if key.startswith(text)]
        try:
            return self.matches[state]
        except IndexError:
            return None


REPL_HISTFILE = os.path.expanduser(".scrap-history")


class ScrapRepl(code.InteractiveConsole):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.env: Env = boot_env()

    def enable_readline(self) -> None:
        assert readline, "Can't enable readline without readline module"
        if os.path.exists(REPL_HISTFILE):
            readline.read_history_file(REPL_HISTFILE)
        # what determines the end of a word; need to set so $ can be part of a
        # variable name
        readline.set_completer_delims(" \t\n;")
        # TODO(max): Add completion per scope, not just for global environment.
        readline.set_completer(Completer(self.env).complete)
        readline.parse_and_bind("set show-all-if-ambiguous on")
        readline.parse_and_bind("tab: menu-complete")

    def finish_readline(self) -> None:
        assert readline, "Can't finish readline without readline module"
        histfile_size = 1000
        readline.set_history_length(histfile_size)
        readline.write_history_file(REPL_HISTFILE)

    def runsource(self, source: str, filename: str = "<input>", symbol: str = "single") -> bool:
        try:
            tokens = tokenize(source)
            logger.debug("Tokens: %s", tokens)
            ast = parse(tokens)
            if isinstance(ast, MatchFunction) and not source.endswith("\n"):
                # User might be in the middle of typing a multi-line match...
                # wait for them to hit Enter once after the last case
                return True
            logger.debug("AST: %s", ast)
            result = eval_exp(self.env, ast)
            assert isinstance(self.env, dict)  # for .update()/__setitem__
            if isinstance(result, EnvObject):
                self.env.update(result.env)
            else:
                self.env["_"] = result
                print(pretty(result))
        except UnexpectedEOFError:
            # Need to read more text
            return True
        except ParseError as e:
            print(f"Parse error: {e}", file=sys.stderr)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
        return False


def eval_command(args: argparse.Namespace) -> None:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    program = args.program_file.read()
    tokens = tokenize(program)
    logger.debug("Tokens: %s", tokens)
    ast = parse(tokens)
    logger.debug("AST: %s", ast)
    result = eval_exp(boot_env(), ast)
    print(pretty(result))


def check_command(args: argparse.Namespace) -> None:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    program = args.program_file.read()
    tokens = tokenize(program)
    logger.debug("Tokens: %s", tokens)
    ast = parse(tokens)
    logger.debug("AST: %s", ast)
    result = infer_type(ast, OP_ENV)
    result = minimize(result)
    print(result)


def apply_command(args: argparse.Namespace) -> None:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    tokens = tokenize(args.program)
    logger.debug("Tokens: %s", tokens)
    ast = parse(tokens)
    logger.debug("AST: %s", ast)
    result = eval_exp(boot_env(), ast)
    print(pretty(result))


def repl_command(args: argparse.Namespace) -> None:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    repl = ScrapRepl()
    if readline:
        repl.enable_readline()
    repl.interact(banner="")
    if readline:
        repl.finish_readline()


def test_command(args: argparse.Namespace) -> None:
    if args.debug:
        # pylint: disable=protected-access
        __import__("sys").modules["unittest.util"]._MAX_LENGTH = 999999999
    # Pass on the rest of the positionals (for filtering tests and so on)
    unittest.main(argv=[__file__, *args.unittest_args])


def env_get_split(key: str, default: Optional[typing.List[str]] = None) -> typing.List[str]:
    import shlex

    cflags = os.environ.get(key)
    if cflags:
        return shlex.split(cflags)
    if default:
        return default
    return []


def discover_cflags(cc: typing.List[str], debug: bool = True) -> typing.List[str]:
    default_cflags = ["-Wall", "-Wextra", "-fno-strict-aliasing", "-Wno-unused-function"]
    # -fno-strict-aliasing is needed because we do pointer casting a bunch
    # -Wno-unused-function is needed because we have a bunch of unused
    # functions depending on what code is compiled
    if debug:
        default_cflags += ["-O0", "-ggdb"]
    else:
        default_cflags += ["-O2", "-DNDEBUG"]
        if "cosmo" not in cc[0]:
            # cosmocc does not support LTO
            default_cflags.append("-flto")
    if "mingw" in cc[0]:
        # Windows does not support mmap
        default_cflags.append("-DSTATIC_HEAP")
    return env_get_split("CFLAGS", default_cflags)


OP_ENV = {
    "+": Forall([], func_type(IntType, IntType, IntType)),
    "-": Forall([], func_type(IntType, IntType, IntType)),
    "*": Forall([], func_type(IntType, IntType, IntType)),
    "/": Forall([], func_type(IntType, IntType, FloatType)),
    "++": Forall([], func_type(StringType, StringType, StringType)),
    ">+": Forall([TyVar("a")], func_type(TyVar("a"), list_type(TyVar("a")), list_type(TyVar("a")))),
    "+<": Forall([TyVar("a")], func_type(list_type(TyVar("a")), TyVar("a"), list_type(TyVar("a")))),
}


def compile_command(args: argparse.Namespace) -> None:
    if args.run:
        args.compile = True
    from compiler import compile_to_string

    with open(args.file, "r") as f:
        source = f.read()

    program = parse(tokenize(source))
    if args.check:
        infer_type(program, OP_ENV)
    c_program = compile_to_string(program, args.debug)

    with open(args.platform, "r") as f:
        platform = f.read()

    with open(args.output, "w") as f:
        f.write(c_program)
        f.write(platform)

    if args.format:
        import subprocess

        subprocess.run(["clang-format-15", "-i", args.output], check=True)

    if args.compile:
        import subprocess

        cc = env_get_split("CC", ["clang"])
        cflags = discover_cflags(cc, args.debug)
        if args.memory:
            cflags += [f"-DMEMORY_SIZE={args.memory}"]
        ldflags = env_get_split("LDFLAGS")
        subprocess.run([*cc, "-o", "a.out", *cflags, args.output, *ldflags], check=True)

    if args.run:
        import subprocess

        subprocess.run(["sh", "-c", "./a.out"], check=True)


def flat_command(args: argparse.Namespace) -> None:
    prog = parse(tokenize(sys.stdin.read()))
    serializer = Serializer()
    serializer.serialize(prog)
    sys.stdout.buffer.write(serializer.output)


def main() -> None:
    parser = argparse.ArgumentParser(prog="scrapscript")
    subparsers = parser.add_subparsers(dest="command")

    repl = subparsers.add_parser("repl")
    repl.set_defaults(func=repl_command)
    repl.add_argument("--debug", action="store_true")

    test = subparsers.add_parser("test")
    test.set_defaults(func=test_command)
    test.add_argument("unittest_args", nargs="*")
    test.add_argument("--debug", action="store_true")

    eval_ = subparsers.add_parser("eval")
    eval_.set_defaults(func=eval_command)
    eval_.add_argument("program_file", type=argparse.FileType("r"))
    eval_.add_argument("--debug", action="store_true")

    check = subparsers.add_parser("check")
    check.set_defaults(func=check_command)
    check.add_argument("program_file", type=argparse.FileType("r"))
    check.add_argument("--debug", action="store_true")

    apply = subparsers.add_parser("apply")
    apply.set_defaults(func=apply_command)
    apply.add_argument("program")
    apply.add_argument("--debug", action="store_true")

    comp = subparsers.add_parser("compile")
    comp.set_defaults(func=compile_command)
    comp.add_argument("file")
    comp.add_argument("-o", "--output", default="output.c")
    comp.add_argument("--format", action="store_true")
    comp.add_argument("--compile", action="store_true")
    comp.add_argument("--memory", type=int)
    comp.add_argument("--run", action="store_true")
    comp.add_argument("--debug", action="store_true", default=False)
    comp.add_argument("--check", action="store_true", default=False)
    # The platform is in the same directory as this file
    comp.add_argument("--platform", default=os.path.join(os.path.dirname(__file__), "cli.c"))

    flat = subparsers.add_parser("flat")
    flat.set_defaults(func=flat_command)

    args = parser.parse_args()
    if not args.command:
        args.debug = False
        repl_command(args)
    else:
        args.func(args)


if __name__ == "__main__":
    # This is so that we can use scrapscript.py as a main but also import
    # things from `scrapscript` and not have that be a separate module.
    sys.modules["scrapscript"] = sys.modules[__name__]
    main()
