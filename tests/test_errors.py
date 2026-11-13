import pytest

from minidb import errors as E


@pytest.mark.parametrize(
    "cls, code",
    [
        (E.DatatypeMismatch, "42804"),
        (E.NumericValueOutOfRange, "22003"),
        (E.NotNullViolation, "23502"),
        (E.ProgramLimitExceeded, "54000"),
        (E.DataCorrupted, "XX001"),
        (E.UndefinedTable, "42P01"),
        (E.InvalidSchemaDefinition, "42P16"),
        (E.InvalidParameterValue, "22023"),
        (E.SequenceGeneratorLimitExceeded, "2200H"),
        (E.CharacterNotInRepertoire, "22021"),
        (E.SqlSyntaxError, "42601"),
        (E.FeatureNotSupported, "0A000"),
        (E.NameTooLong, "42622"),
        (E.InvalidRowCountInLimitClause, "2201W"),
        (E.InvalidSchemaName, "3F000"),
        (E.UndefinedColumn, "42703"),
        (E.DuplicateColumn, "42701"),
        (E.GeneratedAlways, "428C9"),
        (E.UndefinedFunction, "42883"),
        (E.InvalidTextRepresentation, "22P02"),
        (E.UniqueViolation, "23505"),
        (E.UndefinedObject, "42704"),
    ],
)
def test_sqlstate_codes(cls, code):
    err = cls("boom")
    assert isinstance(err, E.DatabaseError)
    assert err.sqlstate == code
    assert err.message == "boom"
    assert str(err) == f"ERROR:  boom (SQLSTATE {code})"


def test_position_optional():
    assert E.DatabaseError("x").position is None
    err = E.SqlSyntaxError("bad", position=7)
    assert err.position == 7
    assert str(err) == "ERROR:  bad (SQLSTATE 42601)"
    assert not issubclass(E.SqlSyntaxError, SyntaxError)


def test_detail_hint_optional():
    e = E.GeneratedAlways("m", detail="d", hint="h")
    assert (e.detail, e.hint) == ("d", "h")
    assert E.DatabaseError("x").detail is None and E.DatabaseError("x").hint is None
