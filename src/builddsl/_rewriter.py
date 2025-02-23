"""
Rewrite BuildDSL code to pure Python code.
"""

import contextlib
import enum
import logging
import re
import string
import sys
import typing as t
from dataclasses import dataclass

from nr.io.lexer import Cursor, ProxyToken as _ProxyToken, RuleSet, Tokenizer, rules

try:
    from termcolor import colored
except ImportError:

    def colored(s, *a, **kw) -> str:  # type: ignore
        return str(s)


from ._util import debug_trace

logger = logging.getLogger(__name__)


@dataclass
class Grammar:
    """
    Grammar settings.
    """

    #: Whether unparenthesized function calls are allowed.
    unparen_calls: bool = True

    #: Whether keyword arguments may be specified using colons (`:`) as well as equal signs (`=`).
    colon_kwargs: bool = True

    #: Accept function arguments with comma separation.
    nocomma_args: bool = True

    #: Whether the `def varname = ...` syntax is allowed and understood. This is an important
    #: syntax feature when enabling the #NameRewriter with #TranspileOptions.closure_target.
    local_def: bool = False

    #: The keyword for local definitions.
    local_keyword: str = "def"

    #: The prefix for variable names that are localized. This is picked up by the transpiler.
    local_prefix: str = "_def_"


class Token(enum.Enum):
    Eof = enum.auto()
    Indent = enum.auto()
    Whitespace = enum.auto()
    Newline = enum.auto()
    Comment = enum.auto()
    Name = enum.auto()
    Literal = enum.auto()
    Control = enum.auto()


PYTHON_BLOCK_KEYWORDS = frozenset(["class", "def", "if", "elif", "else", "for", "while", "with"])
ASSIGNMENT_OPERATORS = ["=", "+=", "-=", "*=", "/=", "%=", "//=", "**=", "&=", "|=", "^=", ">>=", "<<=", "@="]
BINARY_OPERATORS = [x[:-1] for x in ASSIGNMENT_OPERATORS[1:]] + [
    ".",
    "<",
    ">",
    "==",
    "<=",
    ">=",
    "!=",
    ":=",
    "is",
    "and",
    "or",
]
UNARY_OPERATORS = ["not", "~"]
OTHER_CONTROL_CHARACTERS = list("()[]{},:;") + ["->"]
_ALL_CONTROL_CHARACTERS = sorted(
    ASSIGNMENT_OPERATORS + BINARY_OPERATORS + UNARY_OPERATORS + OTHER_CONTROL_CHARACTERS,
    key=len,
    reverse=True,
)
_WORD_CONTROL_CHARACTERS = [op for op in _ALL_CONTROL_CHARACTERS if op.isalpha()]

rule_set = RuleSet((Token.Eof, ""))
rule_set.rule(Token.Indent, rules.regex_extract(r"[\t ]*", at_line_start_only=True))
rule_set.rule(Token.Newline, rules.regex_extract(r"\n"))
rule_set.rule(Token.Whitespace, rules.regex_extract(r"\s+"))
rule_set.rule(Token.Comment, rules.regex_extract(r"#.*"))
rule_set.rule(Token.Control, rules.regex_extract("(" + "|".join(map(re.escape, _WORD_CONTROL_CHARACTERS)) + r")\b"))
rule_set.rule(Token.Name, rules.regex_extract(r"[A-Za-z\_][A-Za-z0-9\_]*"))
rule_set.rule(Token.Literal, rules.regex_extract(r"[+\-]?(\d+)(\.\d*)?"))
rule_set.rule(Token.Literal, rules.string_literal())
rule_set.rule(Token.Control, rules.regex_extract("|".join(map(re.escape, _ALL_CONTROL_CHARACTERS))))


class ProxyToken(_ProxyToken[Token, str]):
    """
    Extension class that adds some useful utility methods to test the contents of the token.
    """

    def is_ignorable(self, newlines: bool = False) -> bool:
        if newlines and self.type == Token.Newline:
            return True
        return self.type in (Token.Indent, Token.Whitespace, Token.Comment)

    def is_control(self, charpool: t.Collection[str]) -> bool:
        return self.type == Token.Control and self.value in charpool


class ParseMode(enum.IntFlag):
    """Flags that describe the current parse environment."""

    #: Nothing specific.
    DEFAULT = 0

    #: The currently parsed expression is grouped in parenthesis and may wrap over lines.
    GROUPED = 1 << 0

    #: The currently parsed expression is the outter parenthesis of a function call.
    FUNCTION_CALL = 1 << 1

    #: The currently parsed expression is an argument to a function call.
    CALL_ARGS = 1 << 2


@dataclass
class SyntaxError(Exception):
    """
    Specialized BuildDSL syntax error (the internal SyntaxError class is weird).

    If the `termcolor` module is installed, the error message will be color coded.
    """

    message: str
    filename: str
    line: int
    column: int
    text: str

    def get_text_hint(self) -> str:
        return "\n".join((self.text, "~" * self.column + "^"))

    def __str__(self) -> str:
        lines = [
            "",
            f'  in {colored(self.filename, "blue")} at line {self.line}: {colored(self.message, "red")}',
            *("  |" + line for line in self.get_text_hint().splitlines()),
        ]
        return "\n".join(lines)


@dataclass
class Closure:
    """
    Contains the definition of a closure in text format.
    """

    #: A unique ID for the closure, usually derived from the number of closures that have already
    #: been parsed in the same file or it's parent closures.
    id: str

    #: The line number where the closure begins.
    line: int

    #: The indentation of the closure's body. For a single expression, this represents the
    #: offset of the expression in line.
    indent: int

    #: The parameter names of the closure. May be `None` to indicate that closure had no header.
    parameters: t.Optional[t.List[str]]

    #: The body of the closure. May be `None` if the closure body is not constructed using curly
    #: braces to encapsulate multiple statements. In that case, the #expr field is set instead.
    body: t.Optional[str]

    #: Only set if the body of the closure is just an expression.
    expr: t.Optional[str]


@dataclass
class RewriteResult:
    """
    The result of rewriting BuildDSL code to pure Python code.
    """

    #: The BuildDSL code rewritten as Python code.
    code: str

    #: The closures extracted from the code.
    closures: t.Dict[str, Closure]


class Rewriter:
    """
    This class rewrites BuildDSL code to pure Python code. Closures are extracted from the code
    and replaced with whitespace where appropriate to keep line and column numbers of the code in
    tact as much as possible (not always fully accurate).
    """

    def __init__(self, text: str, filename: str, grammar: t.Optional[Grammar] = None) -> None:
        """
        # Arguments
        text: The BuildDSL code to parse and turn into an AST-like structure.
        filename: The filename where the DSL code is from.
        """

        self.tokenizer = Tokenizer(rule_set, text)
        self.filename = filename
        self.grammar = grammar or Grammar()
        self._closure_stack: t.List[str] = []  #: Used to construct nested closure names.
        self._closure_counter = 0  #: Used to assign a unique number to every closure.
        self._closures: t.Dict[str, Closure] = {}

    @contextlib.contextmanager
    def _lookahead(self) -> t.Iterator[t.Callable[[], None]]:
        """
        Context manager to save the current tokenizer and closure state and restore it on exit. This is
        useful for lookaheads, like :meth:`_test_dict`. If the returned callable is called, the
        tokenizer and closure state is not restored.
        """

        state = self.tokenizer.state
        closure_state = self._closure_counter, self._closures.copy(), self._closure_stack[:]
        do_restore = True

        def commit() -> None:
            nonlocal do_restore
            do_restore = False

        try:
            yield commit
        finally:
            if do_restore:
                self.tokenizer.state = state
                self._closure_counter, self._closures, self._closure_stack = closure_state

    def _syntax_error(self, msg: str, pos: t.Optional[Cursor] = None) -> SyntaxError:
        """Raise a syntax error on the current position of the tokenizer, or the specified *pos*."""

        pos = pos or self.tokenizer.current.pos
        text = self.tokenizer.scanner.getline(pos)
        return SyntaxError(msg, self.filename, pos.line, pos.column, text)

    @debug_trace
    def _consume_whitespace(self, newlines: t.Union[bool, ParseMode] = False, reset_to_indent: bool = True) -> str:
        """
        Consumes whitespace, indents, comments, and, if enabled, newlines until a different token is
        encountered. If *reset_to_indent* is enabled (default) then the tokenizer will be moved back
        to the indent token before that different token.
        """

        if isinstance(newlines, ParseMode):
            newlines = bool(newlines & ParseMode.GROUPED)

        token = ProxyToken(self.tokenizer)
        parts: t.List[str] = []
        state = token.save()
        while token.is_ignorable(newlines):
            parts.append(token.value)
            state = token.save()
            token.next()
        if reset_to_indent and state.token and state.token.type == Token.Indent:
            token.load(state)
            parts.pop()
        return "".join(parts)

    @debug_trace
    def _parse_closure(self) -> t.Optional[Closure]:
        """
        Attempts to parse a closure at the current position of the tokenizer. Closures can have the
        following syntactical variants:

        1. `() -> { stmts }`
        2. `arg -> { stmts }`
        3. `(arg1, arg2) -> { stmts }`
        4. `{ stmts }`

        Closures of the fourth form can conflict syntactically with Python set literals and will thus
        override the native syntactic feature. The fourth form also results in the #node.Closure
        parameter's list to be `None`.

        The first to third form may also exist without curly braces to define a single expression
        as the closure body (returning the value of that expression from the closure).

        1. `() -> expr`
        2. `arg -> expr`
        3. `(arg1, arg2) -> expr`

        Returns #None if no closure can be parsed at the current position of the tokenizer.
        """

        token = ProxyToken(self.tokenizer)
        pos = token.pos
        state = token.save()
        arglist = self._parse_closure_header()
        body: t.Optional[str] = None
        expr: t.Optional[str] = None
        closure_id = "".join(self._closure_stack) + f"_closure_{self._closure_counter + 1}"
        self._closure_stack.append(closure_id)

        if token.tv == (Token.Control, "{"):
            body = self._parse_closure_body()
        if body is None and arglist is not None:
            # We only parse an expression for the Closure body if an arglist was specified.
            expr = self._rewrite_expr(mode=ParseMode.DEFAULT)

        assert self._closure_stack.pop() == closure_id

        if not (body or expr):
            # NOTE(NiklasRosenstein): We could raise our own SyntaxError here if an arglist was provided
            #     as that is a strong indicator that a Closure expression or body should be provided,
            #     but we can also just leave the complaining to the Python parser.
            token.load(state)
            return None

        self._closure_counter += 1
        return Closure(closure_id, pos.line, pos.column, arglist, body, expr)

    @debug_trace
    def _parse_closure_body(self) -> t.Optional[str]:
        """
        Parses the body of a closure and returns it's code. Expects the tokenizer to point to the
        opening curly brace of the closure.
        """

        token = ProxyToken(self.tokenizer)
        assert token.tv == (Token.Control, "{"), token
        token.next()

        code = self._consume_whitespace(True)
        if "\n" in code:  # Multiline closure
            code += self._rewrite_stmt_block() + self._consume_whitespace(True, False)
        else:  # Singleline closure
            while token.type != Token.Newline and token.tv != (Token.Control, "}"):
                code += self._rewrite_stmt_singleline() + self._consume_whitespace(True, False)

        if token.tv != (Token.Control, "}"):
            raise self._syntax_error("expected closure closing brace")

        token.next()
        return code

    @debug_trace
    def _parse_closure_header(self) -> t.Optional[t.List[str]]:
        """
        Handles the possible formats for a closure header, f.e. a single argument name or an arglist
        followed by an arrow (`->`). Returns `None` if there can be no closure header extracted from
        the current position of the lexer.
        """

        token = ProxyToken(self.tokenizer)
        state = token.save()

        with token.set_skipped(Token.Whitespace):
            arglist: t.Optional[t.List[str]] = None
            if token.tv == (Token.Control, "("):
                arglist = self._parse_closure_arglist()
            elif token.type == Token.Name:
                arglist = [token.value]
                token.next()

            if arglist is None or token.tv != (Token.Control, "->"):
                # We may have found something that looks like an arglist, but isn't, or we found an
                # arglist but no following arrow, so we go back to where we started and let someone
                # else handle these tokens.
                token.load(state)
                return None

            token.next()
            return arglist

    @debug_trace
    def _parse_closure_arglist(self) -> t.Optional[t.List[str]]:
        """
        This method expects an open parenthesis as the current token and attempts to extract a list of
        argument names. Returns `None` if no argument list was actually extracted.
        """

        token = ProxyToken(self.tokenizer)
        assert token.tv == (Token.Control, "("), token

        state = token.save()
        with token.set_skipped({Token.Whitespace, Token.Comment, Token.Newline, Token.Indent}):
            token.next()

            arglist: t.List[str] = []
            is_delimited = True

            while token.tv != (Token.Control, ")"):
                if (
                    not is_delimited  # Token is not preceeded by an opening parentheses or comma.
                    or token.type != Token.Name
                ):  # We can only accept a name at this position.
                    token.load(state)
                    return None

                arglist.append(token.value)
                token.next()
                is_delimited = token.tv == (Token.Control, ",")
                if is_delimited:
                    token.next()

            assert token.tv == (Token.Control, ")"), token

            token.next()
            return arglist

    @debug_trace
    def _rewrite_expr(self, mode: ParseMode) -> str:
        """
        Consumes a Python expression and returns it's code. Does not parse over a comma.

        :param mode: The current parse mode that provides context about the level that is currently
          being parsed.
        """

        code = self._consume_whitespace(mode, False)
        code += self._rewrite_atom(mode)

        token = ProxyToken(self.tokenizer)
        while token:
            code += self._consume_whitespace(mode)

            if token.type == Token.Control and token.value in BINARY_OPERATORS:
                code += token.value
                token.next()
                code += self._rewrite_expr(mode)

            elif token.is_control("(["):
                code += self._consume_whitespace(True, False)
                code += self._rewrite_atom(
                    ParseMode.FUNCTION_CALL | ParseMode.GROUPED if token.value == "(" else ParseMode.DEFAULT
                )

            else:
                break

        return code

    @debug_trace
    def _find_current_line_indent(self) -> int:
        with self._lookahead():
            self.tokenizer.scanner.pos = self.tokenizer.scanner.pos.get_line_begin()
            token = self.tokenizer.next()
            assert token.type == Token.Indent
            return len(token.value)

    @debug_trace
    def _check_next_indent(self, min_indent: int) -> t.Union[int, None]:
        with self._lookahead():
            token = ProxyToken(self.tokenizer)
            assert token.type in (Token.Newline, Token.Indent), token
            whitespace = self._consume_whitespace(True, False).splitlines()
            indent = len(whitespace[-1])
            if indent < min_indent:
                return None
            return indent

    @debug_trace
    def _rewrite_items(self, mode: ParseMode) -> str:
        """
        Rewrites expressions separated by commas.
        """

        line_indent = self._find_current_line_indent()
        continuation_indent: t.Union[int, None] = None

        token = ProxyToken(self.tokenizer)
        code = ""
        upsert_comma = False
        upgraded_to_call_args = False
        do_break = False

        while True:
            code += self._consume_whitespace(mode)

            if token.type == Token.Newline and not (mode & ParseMode.GROUPED):
                break

            with self._lookahead() as commit:
                try:
                    code += self._rewrite_expr(mode=mode)
                except SyntaxError:
                    logger.debug("syntax error consumed while trying to parse expression", exc_info=sys.exc_info())
                    break
                commit()

            code += self._consume_whitespace(mode)

            if mode & ParseMode.CALL_ARGS and (
                token.is_control("=") or (self.grammar.colon_kwargs and token.is_control(":"))
            ):
                code += "="
                token.next()
                # TODO(NiklasRosenstein): This may be problematic in unparenthesised calls?
                code += self._rewrite_expr(mode=mode)

            if token.is_control(","):
                code += ","
                token.next()
                upsert_comma = False

            elif mode & ParseMode.CALL_ARGS and self.grammar.nocomma_args:
                code += ","
                upsert_comma = True

            else:
                do_break = True

            if token.type == Token.Newline:
                if mode & ParseMode.GROUPED:
                    pass

                elif continuation_indent is None:
                    continuation_indent = self._check_next_indent(line_indent + 1)
                    if continuation_indent is None:
                        break
                    if mode == ParseMode.DEFAULT and self.grammar.unparen_calls:
                        mode = ParseMode.CALL_ARGS
                        code += "("
                        upgraded_to_call_args = True

                elif self._check_next_indent(continuation_indent) is None:
                    break
                code += self._consume_whitespace(True)
                continue

            if do_break:
                break

        if upsert_comma:
            code = code.rstrip(",")
        if upgraded_to_call_args:
            code += ")"
        return code

    @debug_trace
    def _rewrite_atom(self, mode: ParseMode = ParseMode.DEFAULT) -> str:
        """
        Consumes a Python or BuildDSL language atom and returns it rewritten as pure Python code. If
        a closure is encountered, it will be replaced with a name reference and the closure itself will
        be stored in the #_closures mapping.
        """

        token = ProxyToken(self.tokenizer)

        if token.is_control("{") and self._test_dict():
            return self._rewrite_dict()

        code = ""
        closure = self._parse_closure()
        if closure:
            code += closure.id
            self._closures[closure.id] = closure

        elif token.is_control("([{"):
            assert not (mode & ParseMode.FUNCTION_CALL) or token.is_control(
                "("
            ), "ParseMode.FUNCTION_CALL requires current token be opening parenthesis"

            expected_close_token = {"(": ")", "[": "]", "{": "}"}[token.value]
            code += token.value
            token.next()
            code += self._consume_whitespace(True)
            if not token.is_control(expected_close_token):
                new_mode = ParseMode.CALL_ARGS if mode & ParseMode.FUNCTION_CALL else ParseMode.DEFAULT
                code += self._rewrite_items(new_mode | ParseMode.GROUPED) + self._consume_whitespace(mode, False)
            if not token.is_control(expected_close_token):
                raise self._syntax_error(f"expected {expected_close_token} but got {token}")

            code += expected_close_token
            token.next()

        elif mode & ParseMode.CALL_ARGS and (token.is_control(["*", "**"])):
            code += token.value
            token.next()
            code += self._rewrite_expr(mode=ParseMode.DEFAULT)

        elif token.type in (Token.Name, Token.Literal):
            code += token.value
            token.next()

        elif token.type == Token.Control and token.value in UNARY_OPERATORS:
            code += token.value
            token.next()
            code += self._rewrite_expr(mode=mode)
            return code

        else:
            raise self._syntax_error(f"not sure how to deal with {token} {mode}")

        return code

    @debug_trace
    def _test_dict(self) -> bool:
        """
        Tests if the code from the current opening curly brace looks like a dictionary definition.
        This does not match an empty dictionary, but only one with at least one key.
        """

        token = ProxyToken(self.tokenizer)
        assert token.is_control("{"), False

        with self._lookahead():
            token.next()
            self._consume_whitespace(True, False)
            try:
                self._rewrite_expr(mode=ParseMode.GROUPED)
                self._consume_whitespace(True, False)
                return token.is_control(":")
            except SyntaxError:
                return False

    @debug_trace
    def _rewrite_dict(self) -> str:
        token = ProxyToken(self.tokenizer)
        assert token.is_control("{"), token
        token.next()
        code = "{"

        while not token.is_control("}"):
            code += self._consume_whitespace(True, False)
            code += self._rewrite_expr(mode=ParseMode.GROUPED)
            code += self._consume_whitespace(True, False)
            if not token.is_control(":"):
                raise self._syntax_error("expected :")
            code += ":"
            token.next()
            code += self._consume_whitespace(True, False)
            code += self._rewrite_expr(mode=ParseMode.GROUPED)
            code += self._consume_whitespace(True, False)
            if not token.is_control(","):
                break
            code += ","
            token.next()
            code += self._consume_whitespace(True, False)

        if not token.is_control("}"):
            raise self._syntax_error("expected }")

        token.next()
        return code + "}"

    @debug_trace
    def _rewrite_stmt_singleline(self) -> str:
        token = ProxyToken(self.tokenizer)
        code = self._consume_whitespace(False)

        if token.type == Token.Name and token.value == "pass":
            token.next()
            return code + "pass" + self._consume_whitespace(True)

        elif token.type == Token.Name and token.value in ("assert", "return", "yield"):
            code += token.value
            is_yield = token.value == "yield"
            token.next()
            code += self._consume_whitespace(False)
            if is_yield and token.tv == (Token.Name, "from"):
                code += token.value
                token.next()
            code += self._rewrite_items(ParseMode.DEFAULT) + self._consume_whitespace(True)
            return code

        elif token.type == Token.Name and token.value in ("import", "from"):
            while token.type != Token.Newline and token.tv != (Token.Control, ";"):
                code += token.value
                token.next()
            code += token.value
            token.next()
            return code

        else:
            return code + self._rewrite_stmt_line_expr_or_assign()

    @debug_trace
    def _rewrite_stmt_line_expr_or_assign(self) -> str:
        token = ProxyToken(self.tokenizer)
        code = self._rewrite_items(ParseMode.DEFAULT)

        if not code:
            # TODO (@nrosenstein): Better error message. How to reproduce reaching this line:
            #
            #   print('hello, world'!)
            #
            # Note how the exclamation mark is outside the string literal.
            raise self._syntax_error("unable to parse statement")

        code += self._consume_whitespace(newlines=False)

        if token.type == Token.Control and token.value in ASSIGNMENT_OPERATORS:
            op = token.value
            token.next()
            code += op + self._consume_whitespace(newlines=False) + self._rewrite_items(ParseMode.DEFAULT)

        elif token and not token.is_ignorable(True) and not token.is_control(")]}:") and self.grammar.unparen_calls:
            if code[-1].isspace():
                code = code[:-1]
            # TODO(NiklasRosenstein): We may want to indicate here that we're parsing call arguments,
            #   but that the call is not parenthesised.
            code += "(" + self._rewrite_items(ParseMode.CALL_ARGS) + ")"

        # TODO (@nrosenstein): This is a nasty hack to figure out if the current line contains _just_ a name or
        #   a dotted name which, with unparenthesized calls enabled, should act as a call without arguments. Since
        #   we'll end up parsing code that was previously re-written for example in the case of closures, we could
        #   end up parsing a closure name (eg. just "_closure_1") if a closure was defined on its own, however that
        #   causing the closure to be called immediately seems a bit of an erratic behaviour so we want to catch it.
        elif (
            not (set(code) - set(string.ascii_letters + string.digits + "." + "_")) and self.grammar.unparen_calls
        ) and not code.startswith("_closure_"):
            code += "()"

        return code + self._consume_whitespace(True)

    @debug_trace
    def _test_local_def(self) -> t.Optional[str]:
        """
        Tests if the current `def` keyword introduces a local variable assignment, and if so,
        returns the code for the rewritten code for the entire assignment.
        """

        token = ProxyToken(self.tokenizer)
        assert token.tv == (Token.Name, self.grammar.local_keyword), token

        with self._lookahead() as commit:
            token.next()
            self._consume_whitespace(False)
            if token.type != Token.Name:
                return None
            code = self.grammar.local_prefix + token.value
            token.next()
            code += self._consume_whitespace(False)
            if not token.is_control("="):
                return None
            code += token.value
            token.next()
            code += self._rewrite_expr(ParseMode.DEFAULT)
            commit()
            return code

    @debug_trace
    def _rewrite_stmt(self, indentation: int) -> str:
        """
        Parses a line statement of Python code. Returns an empty string if the actual indendation of
        the code is lower than *indentation*. Handles parsing of Python block statements (such as if,
        try, etc.) recursively.
        """

        code = self._consume_whitespace(True)

        token = ProxyToken(self.tokenizer)
        assert token.type == Token.Indent, token
        if len(token.value) < indentation:
            return ""
        elif len(token.value) > indentation:
            raise self._syntax_error("unexpected indentation")

        code += token.value
        token.next()

        if self.grammar.local_def and token.tv == (Token.Name, self.grammar.local_keyword):
            defcode = self._test_local_def()
            if defcode:
                return code + defcode

        if token.type == Token.Name and token.value in PYTHON_BLOCK_KEYWORDS:
            # Parse to the next colon.
            # TODO(nrosenstein): If we want to support BuildDSL syntax in the expressions of block
            #   statements, we'll need to rewrite them on a more granular level.
            while token and token.tv not in ((Token.Newline, "\n"), (Token.Control, ":")):
                code += token.value
                token.next()
            if token.tv != (Token.Control, ":"):
                raise self._syntax_error(f"expected semicolon, found {token}")
            code += ":"
            token.next()

            return code + self._rewrite_stmt_block(indentation)

        if token.is_control("}"):
            return code

        else:
            return code + self._rewrite_stmt_singleline()

    @debug_trace
    def _rewrite_stmt_block(self, parent_indentation: t.Optional[int] = None) -> str:
        """
        Rewrites an entire statement block and returns it's rewritten code.
        """

        token = ProxyToken(self.tokenizer)
        code = self._consume_whitespace(True)
        if not token:
            return code
        assert token.type == Token.Indent, token
        if parent_indentation is not None and len(token.value) <= parent_indentation:
            raise self._syntax_error(f"expected indent > {parent_indentation}, found {token}")
        indentation = len(token.value)
        while token:
            stmt = self._rewrite_stmt(indentation)
            if not stmt:
                break
            code += stmt + self._consume_whitespace(True)
        return code

    @debug_trace
    def rewrite(self) -> RewriteResult:
        """
        Rewrite the code and return the #RewriteResult. This can be interpreted by the
        #builddsl.transpiler.ClosureRewriter to re-inject the code for closures.
        """

        return RewriteResult(self._rewrite_stmt_block(), self._closures)
