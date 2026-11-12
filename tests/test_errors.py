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
