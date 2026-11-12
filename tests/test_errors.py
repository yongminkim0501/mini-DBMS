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
    ],
)
def test_sqlstate_codes(cls, code):
    err = cls("boom")
    assert isinstance(err, E.DatabaseError)
    assert err.sqlstate == code
    assert err.message == "boom"
    assert str(err) == f"ERROR:  boom (SQLSTATE {code})"
