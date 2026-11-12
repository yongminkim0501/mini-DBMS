"""재귀 하강(recursive descent) SQL 파서.

문법(docs/sql-grammar.md)의 규칙 하나가 :class:`_Parser` 의 메서드 하나에 대응한다::

    statement  → _statement()       insert → _insert()       select → _select()
    row        → _row()             value  → _value()        literal → _literal()
    cond       → _cond()            op     → _op()           qualified_name → _qualified_name()

각 메서드는 "현재 토큰"부터 자기 규칙에 해당하는 토큰을 소비하고 AST 노드를 반환한다.
규칙 안에 다른 규칙이 나오면 그 메서드를 호출한다 (예: _insert 가 _row 를 반복 호출).
선택(``[ ... ]``)과 반복(``{ ... }``)은 현재 토큰을 보고(_peek) 결정한다. 문법이 LL(1) 이라
토큰 하나만 미리 보면 어느 분기인지 항상 정해지므로 되돌아가기(backtracking)가 없다.

파서는 스키마를 모른다. 테이블/컬럼 존재나 타입 검사는 다음 단계의 바인더가 한다.
파서 인스턴스는 호출마다 새로 만든다 (스레드 간 공유 상태 없음).
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import (
    DatabaseError,
    FeatureNotSupported,
    InvalidRowCountInLimitClause,
    SqlSyntaxError,
)
from .ast import Condition, Default, ExplainStmt, InsertStmt, Literal, QualifiedName, SelectStmt, Star, Statement
from .lexer import StatementSource, iter_statements, tokenize
from .tokens import Token, TokenType

__all__ = ["parse_statement", "parse_script"]

T = TokenType

# 문장 맨 앞에 오면 "지원하지 않는 문장"으로 알려줄 단어들 (나머지는 일반 구문 오류)
_UNSUPPORTED_STATEMENTS: frozenset[str] = frozenset(
    {
        "update", "delete", "create", "drop", "alter", "truncate", "begin", "commit", "rollback",
        "start", "end", "savepoint", "release", "abort", "with", "copy", "grant", "revoke",
        "set", "show", "reset", "vacuum", "prepare", "execute", "deallocate", "lock",
        "merge", "table", "do", "call", "listen", "notify", "discard", "comment", "cluster", "reindex",
    }
)  # fmt: skip

# 조건을 뒤집을 때 연산자 방향도 뒤집는다: 5 < age  ≡  age > 5
_FLIP_OP: dict[str, str] = {"=": "=", "<>": "<>", "<": ">", "<=": ">=", ">": "<", ">=": "<="}
_OP_TOKENS: dict[TokenType, str] = {T.EQ: "=", T.NE: "<>", T.LT: "<", T.LE: "<=", T.GT: ">", T.GE: ">="}
_LITERAL_START = frozenset({T.INTEGER, T.STRING, T.MINUS})

_JOIN_WORDS = frozenset({"join", "inner", "left", "right", "full", "cross", "natural"})
_CLAUSE_WORDS: dict[str, str] = {
    "order": "ORDER BY is not supported",
    "group": "GROUP BY is not supported",
    "having": "HAVING is not supported",
    "offset": "OFFSET is not supported",
    "union": "UNION is not supported",
    "intersect": "INTERSECT is not supported",
    "except": "EXCEPT is not supported",
    "fetch": "FETCH is not supported",
    "for": "FOR UPDATE/SHARE is not supported",
    "window": "WINDOW is not supported",
}


class _Parser:
    """토큰 리스트 하나(= 문장 하나)를 파싱하는 일회용 파서."""

    def __init__(self, tokens: Sequence[Token], text: str, text_shift: int, pos_base: int, end_position: int) -> None:
        """
        Args:
            tokens: 문장의 토큰 (EOF 포함).
            text: 토큰 원문을 꺼낼 문자열 (오류 메시지의 "at or near" 용).
            text_shift: ``text[tok.offset - text_shift]`` 가 토큰 시작이 되도록 하는 보정값.
            pos_base: ``position = pos_base + tok.offset + 1`` 이 원문 기준 위치가 되도록 하는 보정값.
            end_position: "end of input" 오류의 position (원문 기준).
        """
        self.toks = tokens
        self.i = 0
        self.text = text
        self.text_shift = text_shift
        self.pos_base = pos_base
        self.end_position = end_position

    # ------------------------------------------------------------ 토큰 헬퍼
    def _peek(self, k: int = 0) -> Token:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def _advance(self) -> Token:
        tok = self.toks[self.i]
        if tok.type is not T.EOF:
            self.i += 1
        return tok

    def _pos(self, tok: Token) -> int:
        """토큰의 원문 기준 1부터 시작하는 위치."""
        return self.pos_base + tok.offset + 1

    def _raw(self, tok: Token) -> str:
        return self.text[tok.offset - self.text_shift : tok.end - self.text_shift]

    def _error(self, tok: Token | None = None) -> SqlSyntaxError:
        """PostgreSQL 형식의 구문 오류: 'syntax error at or near "X"' / 'syntax error at end of input'."""
        tok = tok if tok is not None else self._peek()
        if tok.type is T.EOF:
            return SqlSyntaxError("syntax error at end of input", position=self.end_position)
        return SqlSyntaxError(f'syntax error at or near "{self._raw(tok)}"', position=self._pos(tok))

    def _unsupported(self, message: str, tok: Token) -> FeatureNotSupported:
        return FeatureNotSupported(message, position=self._pos(tok))

    def _is_keyword(self, tok: Token, kw: str) -> bool:
        return tok.type is T.KEYWORD and tok.value == kw

    @staticmethod
    def _is_word(tok: Token, words: frozenset[str] | dict[str, str]) -> bool:
        """인용되지 않은 식별자이고 ``words`` 중 하나인가 (인용된 "or" 는 그냥 식별자)."""
        return tok.type is T.IDENT and not tok.quoted and tok.value in words

    def _expect_keyword(self, kw: str) -> Token:
        tok = self._peek()
        if not self._is_keyword(tok, kw):
            raise self._error(tok)
        return self._advance()

    def _expect(self, ttype: TokenType) -> Token:
        tok = self._peek()
        if tok.type is not ttype:
            raise self._error(tok)
        return self._advance()

    def _ident(self) -> Token:
        """ident: 식별자 자리. 예약 키워드가 오면(인용 안 됨) 구문 오류."""
        tok = self._peek()
        if tok.type is not T.IDENT:
            raise self._error(tok)
        return self._advance()

    # ------------------------------------------------------------ 문법 규칙
    def parse(self) -> Statement:
        """statement 하나를 파싱하고 입력이 끝났는지 확인한다."""
        stmt = self._statement()
        tok = self._peek()
        if tok.type is not T.EOF:
            raise self._error(tok)
        return stmt

    def _statement(self) -> Statement:
        """statement := [ EXPLAIN [ ANALYZE ] ] ( insert | select )"""
        tok = self._peek()
        if self._is_keyword(tok, "EXPLAIN"):
            self._advance()
            analyze = False
            nxt = self._peek()
            if nxt.type is T.LPAREN:
                raise self._unsupported("EXPLAIN options in parentheses are not supported", nxt)
            if self._is_keyword(nxt, "ANALYZE"):
                self._advance()
                analyze = True
            inner = self._peek()
            if self._is_keyword(inner, "INSERT"):
                stmt: InsertStmt | SelectStmt = self._insert()
            elif self._is_keyword(inner, "SELECT"):
                stmt = self._select()
            else:
                raise self._error(inner)
            return ExplainStmt(stmt, analyze, position=self._pos(tok))
        if self._is_keyword(tok, "INSERT"):
            return self._insert()
        if self._is_keyword(tok, "SELECT"):
            return self._select()
        if self._is_word(tok, _UNSUPPORTED_STATEMENTS):
            raise self._unsupported(f"{tok.value.upper()} statements are not supported", tok)
        raise self._error(tok)

    def _insert(self) -> InsertStmt:
        """insert := INSERT INTO qualified_name [ '(' ident {',' ident} ')' ]
                     VALUES row {',' row} [ RETURNING ( '*' | ident {',' ident} ) ]"""
        start = self._expect_keyword("INSERT")
        self._expect_keyword("INTO")
        table = self._qualified_name()

        columns: tuple[str, ...] | None = None
        col_pos: tuple[int, ...] = ()
        if self._peek().type is T.LPAREN:
            self._advance()
            names, col_pos = self._ident_list()
            columns = names
            self._expect(T.RPAREN)

        tok = self._peek()
        if self._is_keyword(tok, "SELECT"):
            raise self._unsupported("INSERT ... SELECT is not supported", tok)
        if self._is_keyword(tok, "DEFAULT"):
            raise self._unsupported("DEFAULT VALUES is not supported", tok)
        self._expect_keyword("VALUES")

        rows = [self._row()]
        width = len(rows[0][0])
        while self._peek().type is T.COMMA:
            self._advance()
            row, row_tok = self._row()
            if len(row) != width:
                # PostgreSQL 과 같은 메시지. position 은 문제 행의 '(' 위치.
                raise SqlSyntaxError("VALUES lists must all be the same length", position=self._pos(row_tok))
            rows.append((row, row_tok))

        returning: tuple[str, ...] | Star | None = None
        ret_pos: tuple[int, ...] = ()
        if self._is_keyword(self._peek(), "RETURNING"):
            self._advance()
            if self._peek().type is T.STAR:
                returning = Star(position=self._pos(self._advance()))
            else:
                returning, ret_pos = self._ident_list()

        return InsertStmt(
            table,
            columns,
            tuple(r for r, _ in rows),
            returning,
            position=self._pos(start),
            column_positions=col_pos,
            returning_positions=ret_pos,
        )

    def _row(self) -> tuple[tuple[Literal | Default, ...], Token]:
        """row := '(' value {',' value} ')'   (행과 '(' 토큰을 반환)"""
        lp = self._expect(T.LPAREN)
        values = [self._value()]
        while self._peek().type is T.COMMA:
            self._advance()
            values.append(self._value())
        self._expect(T.RPAREN)
        return tuple(values), lp

    def _value(self) -> Literal | Default:
        """value := literal | DEFAULT"""
        tok = self._peek()
        if self._is_keyword(tok, "DEFAULT"):
            self._advance()
            return Default(position=self._pos(tok))
        return self._literal()

    def _literal(self) -> Literal:
        """literal := [ '-' ] integer | string | NULL"""
        tok = self._advance()
        tt = tok.type
        if tt is T.INTEGER or tt is T.STRING:
            return Literal(tok.value, position=self._pos(tok))
        if tt is T.MINUS:
            # '-' 바로 다음이 정수면 음수 리터럴로 합친다 (-9223372036854775808 도 이렇게 표현된다).
            num = self._peek()
            if num.type is not T.INTEGER:
                raise self._error(num)  # '-' 다음 문자열/NULL 은 구문 오류
            self._advance()
            return Literal(-num.value, position=self._pos(tok))
        if self._is_keyword(tok, "NULL"):
            return Literal(None, position=self._pos(tok))
        raise self._error(tok)

    def _select(self) -> SelectStmt:
        """select := SELECT ( '*' | ident {',' ident} ) FROM qualified_name
                     [ WHERE cond { AND cond } ] [ LIMIT ( integer | ALL ) ]"""
        start = self._expect_keyword("SELECT")
        columns: tuple[str, ...] | Star
        col_pos: tuple[int, ...] = ()
        if self._peek().type is T.STAR:
            columns = Star(position=self._pos(self._advance()))
        else:
            columns, col_pos = self._ident_list(in_select_list=True)
        self._expect_keyword("FROM")
        table = self._qualified_name()
        self._reject_unsupported_clause()

        where: list[Condition] = []
        if self._is_keyword(self._peek(), "WHERE"):
            self._advance()
            where.append(self._cond())
            while True:
                tok = self._peek()
                if self._is_keyword(tok, "AND"):
                    self._advance()
                    where.append(self._cond())
                elif self._is_word(tok, {"or"}):
                    raise self._unsupported("OR in WHERE clause is not supported", tok)
                else:
                    break
            self._reject_unsupported_clause()

        limit: int | None = None
        if self._is_keyword(self._peek(), "LIMIT"):
            self._advance()
            limit = self._limit_count()
            self._reject_unsupported_clause()

        return SelectStmt(columns, table, tuple(where), limit, position=self._pos(start), column_positions=col_pos)

    def _limit_count(self) -> int | None:
        """LIMIT ( integer | ALL )  — ALL 은 None. 음수는 2201W."""
        tok = self._peek()
        if self._is_keyword(tok, "ALL"):
            self._advance()
            return None
        if tok.type is T.INTEGER:
            self._advance()
            return tok.value
        if tok.type is T.MINUS and self._peek(1).type is T.INTEGER:
            raise InvalidRowCountInLimitClause("LIMIT must not be negative", position=self._pos(tok))
        raise self._error(tok)

    def _reject_unsupported_clause(self) -> None:
        """다음 토큰이 ORDER BY, JOIN 같은 미지원 절이면 FeatureNotSupported 로 명확히 알린다."""
        tok = self._peek()
        if tok.type is T.COMMA:
            raise self._unsupported("multiple tables in FROM clause are not supported", tok)
        if self._is_word(tok, _JOIN_WORDS):
            raise self._unsupported("JOIN is not supported", tok)
        if self._is_word(tok, _CLAUSE_WORDS):
            raise self._unsupported(_CLAUSE_WORDS[tok.value], tok)

    def _cond(self) -> Condition:
        """cond := ident op literal | literal op ident

        "literal op ident" 는 "ident op' literal" 로 뒤집어 정규화한다 (예: 5 < age → age > 5).
        """
        tok = self._peek()
        if tok.type is T.LPAREN:
            raise self._unsupported("parenthesized conditions in WHERE clause are not supported", tok)
        if self._is_word(tok, {"not"}):
            raise self._unsupported("NOT in WHERE clause is not supported", tok)

        if tok.type is T.IDENT:
            col = self._advance()
            self._reject_unsupported_predicate()
            op_tok = self._peek()
            op = self._op()
            nxt = self._peek()
            if nxt.type is T.IDENT:
                raise self._unsupported("comparison between two columns is not supported", nxt)
            value = self._literal()
            return Condition(col.value, op, value, position=self._pos(col), op_position=self._pos(op_tok))

        if tok.type in _LITERAL_START or self._is_keyword(tok, "NULL"):
            value = self._literal()
            op_tok = self._peek()
            op = self._op()
            col = self._ident()
            # 리터럴이 왼쪽이면 컬럼을 왼쪽으로 옮기고 연산자 방향도 바꾼다.
            # Phase 2 에서 "id = 상수" 인덱스 탐색 조건을 찾을 때 한 가지 모양만 보면 된다.
            return Condition(
                col.value,
                _FLIP_OP[op],
                value,
                position=self._pos(col),
                op_position=self._pos(op_tok),
                literal_first=True,
            )

        raise self._error(tok)

    def _reject_unsupported_predicate(self) -> None:
        """컬럼 뒤에 IS NULL / IN / LIKE / BETWEEN 이 오면 FeatureNotSupported."""
        tok = self._peek()
        if self._is_word(tok, {"is"}):
            raise self._unsupported("IS [NOT] NULL in WHERE clause is not supported", tok)
        if self._is_word(tok, {"in", "like", "ilike", "between"}):
            raise self._unsupported(f"{tok.value.upper()} in WHERE clause is not supported", tok)
        if self._is_word(tok, {"not"}):
            raise self._unsupported("NOT in WHERE clause is not supported", tok)

    def _op(self) -> str:
        """op := '=' | '<>' | '!=' | '<' | '<=' | '>' | '>='   ('!=' 는 '<>' 로 정규화)"""
        tok = self._peek()
        op = _OP_TOKENS.get(tok.type)
        if op is None:
            raise self._error(tok)
        self._advance()
        return op

    def _qualified_name(self) -> QualifiedName:
        """qualified_name := ident [ '.' ident ]"""
        first = self._ident()
        if self._peek().type is T.DOT:
            self._advance()
            second = self._ident()
            if self._peek().type is T.DOT:
                raise self._unsupported("cross-database references are not supported", self._peek())
            return QualifiedName(first.value, second.value, position=self._pos(first))
        return QualifiedName(None, first.value, position=self._pos(first))

    def _ident_list(self, *, in_select_list: bool = False) -> tuple[tuple[str, ...], tuple[int, ...]]:
        """ident {',' ident}  → (이름들, 위치들)"""
        names: list[str] = []
        positions: list[int] = []
        while True:
            tok = self._ident()
            if in_select_list:
                nxt = self._peek()
                if nxt.type is T.LPAREN:
                    raise self._unsupported("function calls are not supported", tok)
                if nxt.type is T.DOT:
                    raise self._unsupported("qualified column names are not supported", nxt)
            names.append(tok.value)
            positions.append(self._pos(tok))
            if self._peek().type is not T.COMMA:
                return tuple(names), tuple(positions)
            self._advance()


def parse_statement(source: StatementSource | str) -> Statement:
    """문장 하나를 파싱한다.

    Args:
        source: :func:`split_statements` 결과 원소, 또는 SQL 문자열 (세미콜론 없는 한 문장).

    Raises:
        SqlSyntaxError, FeatureNotSupported, InvalidRowCountInLimitClause, NameTooLong:
            ``position`` 은 원문(``source.start_offset`` 기준을 반영한) 1부터 시작하는 위치.
    """
    if isinstance(source, str):
        source = StatementSource(source, 0)
    if source.error is not None:
        raise source.error
    end_position = source.start_offset + len(source.text) + 1
    if source.tokens is not None:
        # split_statements 가 만든 토큰 재사용: offset 이 이미 원문 기준이다.
        eof = Token(T.EOF, None, source.start_offset + len(source.text), source.start_offset + len(source.text), 0, 0)
        toks: Sequence[Token] = (*source.tokens, eof)
        parser = _Parser(toks, source.text, source.start_offset, 0, end_position)
    else:
        toks = _tokenize_with_position(source)
        parser = _Parser(toks, source.text, 0, source.start_offset, end_position)
    return parser.parse()


def _tokenize_with_position(source: StatementSource) -> list[Token]:
    """문장 텍스트를 렉싱하고, 렉싱 오류의 position 을 원문 기준으로 보정한다."""
    try:
        toks = tokenize(source.text)
    except DatabaseError as exc:
        if exc.position is not None:
            exc.position += source.start_offset
        raise
    if any(t.type is T.SEMICOLON for t in toks):
        semi = next(t for t in toks if t.type is T.SEMICOLON)
        raise SqlSyntaxError(
            'syntax error at or near ";"', position=source.start_offset + semi.offset + 1
        )
    return toks


def parse_script(text: str) -> list[Statement]:
    """세미콜론으로 구분된 여러 문장을 파싱한다. 첫 오류에서 예외 (position 은 ``text`` 기준).

    문장별로 계속 진행하고 싶으면(``--on-error continue``) :func:`~minidb.sql.lexer.split_statements` 와
    :func:`parse_statement` 를 직접 조합한다. 문장은 스트리밍으로 분리하므로(iter_statements)
    파싱이 끝난 문장의 토큰은 바로 해제된다.
    """
    return [parse_statement(src) for src in iter_statements(text)]
