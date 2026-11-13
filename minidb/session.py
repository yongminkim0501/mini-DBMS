"""세션: 연결(클라이언트) 하나의 설정 값 (PostgreSQL 의 GUC 를 아주 작게 흉내 낸다).

- 설정 정의(:data:`SETTINGS`)는 불변이라 모듈 수준에 둔다. 값은 :class:`Session` 인스턴스에만 있다.
- ``SET name = value`` / ``SHOW name`` 이 세션 값을 바꾸고 읽는다. 이름은 대소문자를 구분하지 않는다.
- 지원 설정: ``enable_indexscan`` (불리언, 기본 on. 끄면 플래너가 항상 Seq Scan 을 고른다), ``application_name``,
  ``client_encoding`` (UTF8 계열만), 그리고 PostgreSQL 클라이언트가 시작 단계의 ParameterStatus 로 받는 읽기 전용 값들
  (``server_version`` "16.0", ``DateStyle``, ``TimeZone`` …). 읽기 전용 설정을 SET 하면 55P02.
- ``report`` 설정의 값이 SET 으로 바뀌면 :meth:`Session.take_reported_changes` 로 꺼내 ParameterStatus 를 보낸다.
- :attr:`Session.cancel` (``threading.Event`` 또는 None): wire protocol 의 쿼리 취소. 실행기가 스캔 중에 확인한다.

**스레드 안전하지 않다.** Phase 3 에서는 연결마다 Session 을 하나씩 만들고, 한 연결은 한 번에 한 스레드만
처리하므로 락이 필요 없다 (세션은 스레드 사이에 공유하지 않는다).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import CantChangeRuntimeParam, InvalidParameterValue, UndefinedObject

__all__ = ["SettingDef", "SETTINGS", "REPORTED_SETTINGS", "Session", "parse_bool", "is_utf8_encoding_name"]


def parse_bool(text: str) -> bool | None:
    """PostgreSQL ``parse_bool`` 과 같은 규칙. 해석할 수 없으면 None.

    대소문자 무시. true/false/yes/no 는 고유한 접두사도 허용 (t, tr, f, y, n ...),
    on/off 는 두 글자 이상 (o 하나는 모호), 1/0 은 정확히 한 글자.
    """
    s = text.lower()
    if not s:
        return None
    for word, value in (("true", True), ("false", False), ("yes", True), ("no", False)):
        if word.startswith(s):
            return value
    if len(s) >= 2:
        if "on".startswith(s):
            return True
        if "off".startswith(s):
            return False
    if s == "1":
        return True
    if s == "0":
        return False
    return None


def _bool_from_value(value: int | str) -> bool | None:
    # PostgreSQL 은 SET 의 정수 상수도 문자열로 바꿔 해석한다 (SET enable_indexscan = 1 → "1" → true).
    return parse_bool(str(value))


def _show_bool(value: Any) -> str:
    return "on" if value else "off"


def _string(value: int | str) -> str:
    return str(value)


def _show_string(value: Any) -> str:
    return str(value)


#: client_encoding 으로 받아들이는 이름 (대소문자 무시). minidb 는 UTF-8 만 다룬다.
_UTF8_NAMES = frozenset({"utf8", "utf-8", "unicode"})


def is_utf8_encoding_name(name: str) -> bool:
    """``UTF8`` / ``UTF-8`` / ``unicode`` (대소문자 무시) 인가. 시작 패킷의 client_encoding 검사에도 쓴다."""
    return name.lower() in _UTF8_NAMES


def _encoding(value: int | str) -> str | None:
    return "UTF8" if is_utf8_encoding_name(str(value)) else None


@dataclass(frozen=True, slots=True)
class SettingDef:
    """설정 하나의 정의 (불변).

    Attributes:
        parse: SET 값 → 파이썬 값. 해석할 수 없으면 None (→ 22023).
        readonly: True 면 SET 할 수 없다 (55P02). 값은 서버가 연결 시작 때 정한다.
        report: True 면 값이 바뀔 때 wire protocol 의 ParameterStatus 로 클라이언트에 알린다 (PostgreSQL 의 GUC_REPORT).
        invalid_message: 해석 실패 시 메시지 (``{name}``, ``{value}`` 자리표시자).
    """

    name: str
    default: Any
    type_name: str
    description: str
    parse: Callable[[int | str], Any]
    show: Callable[[Any], str]
    readonly: bool = False
    report: bool = False
    invalid_message: str = 'parameter "{name}" requires a Boolean value'


def _ro(name: str, value: str, description: str) -> SettingDef:
    """읽기 전용 + ParameterStatus 로 알리는 문자열 설정."""
    return SettingDef(name, value, "string", description, _string, _show_string, readonly=True, report=True)


_DEFS: tuple[SettingDef, ...] = (
    SettingDef(
        "enable_indexscan",
        True,
        "bool",
        "Enables the planner's use of index-scan plans.",
        _bool_from_value,
        _show_bool,
    ),
    SettingDef(
        "application_name",
        "",
        "string",
        "Sets the application name to be reported in statistics and logs.",
        _string,
        _show_string,
        report=True,
    ),
    SettingDef(
        "client_encoding",
        "UTF8",
        "string",
        "Sets the client's character set encoding (only UTF8 is supported).",
        _encoding,
        _show_string,
        report=True,
        invalid_message='invalid value for parameter "{name}": "{value}"',
    ),
    _ro("server_version", "16.0", "Shows the server version (PostgreSQL-compatible format)."),
    _ro("server_encoding", "UTF8", "Shows the server (database) character set encoding."),
    _ro("DateStyle", "ISO, MDY", "Sets the display format for date and time values."),
    _ro("integer_datetimes", "on", "Shows whether datetimes are integer based."),
    _ro("standard_conforming_strings", "on", "Causes '...' strings to treat backslashes literally."),
    _ro("TimeZone", "UTC", "Sets the time zone for displaying and interpreting time stamps."),
    _ro("IntervalStyle", "postgres", "Sets the display format for interval values."),
    _ro("is_superuser", "on", "Shows whether the current user is a superuser."),
    _ro("session_authorization", "minidb", "Sets the session user name."),
)

#: 이름(소문자) → 정의. SET/SHOW 는 대소문자를 구분하지 않는다 (``SHOW datestyle`` → 컬럼 이름 ``DateStyle``).
SETTINGS: Mapping[str, SettingDef] = MappingProxyType({d.name.lower(): d for d in _DEFS})

#: ParameterStatus 로 알리는 설정의 정식 이름 (시작 단계에서 모두 보낸다)
REPORTED_SETTINGS: tuple[str, ...] = tuple(d.name for d in _DEFS if d.report)


def _lookup(name: str, position: int | None) -> SettingDef:
    d = SETTINGS.get(name.lower())
    if d is None:
        raise UndefinedObject(f'unrecognized configuration parameter "{name}"', position=position)
    return d


class Session:
    """설정 값 묶음. ``Database.execute(sql, session)`` 에 넘긴다 (생략하면 Database 의 기본 세션).

    Args:
        **overrides: 시작 값 (예: ``Session(enable_indexscan=False)``, ``Session(session_authorization="kim")``).
            알 수 없는 이름이면 UndefinedObject. 읽기 전용 설정도 여기서는 정할 수 있다(서버가 연결 시작 때 쓴다).
            시작 값은 그 세션의 기본값이 된다 (``SET x TO DEFAULT`` 가 돌아가는 값, PostgreSQL 의 시작 패킷 값과 같다).
            bool 이 아닌 값(``"off"``, ``0``)은 SET 과 같은 규칙으로 해석한다 (잘못된 값이면 InvalidParameterValue).
    """

    __slots__ = ("_values", "_defaults", "_changed", "cancel")

    def __init__(self, **overrides: Any) -> None:
        self._values: dict[str, Any] = {d.name: d.default for d in _DEFS}
        self._changed: list[str] = []
        #: 쿼리 취소 플래그 (wire protocol 연결만 설정한다). 실행기가 스캔 루프에서 ``is_set()`` 을 확인한다.
        self.cancel: threading.Event | None = None
        for name, value in overrides.items():
            d = _lookup(name, None)
            self._values[d.name] = value if isinstance(value, bool) else self._parse(d, value, None)
        self._defaults: dict[str, Any] = dict(self._values)

    # 플래너가 매 SELECT 마다 읽는 값은 속성으로 바로 꺼낸다.
    @property
    def enable_indexscan(self) -> bool:
        return bool(self._values["enable_indexscan"])

    def get(self, name: str) -> Any:
        """설정의 현재 값 (파이썬 값)."""
        return self._values[_lookup(name, None).name]

    @staticmethod
    def _parse(d: SettingDef, value: int | str, value_position: int | None) -> Any:
        parsed = d.parse(value)
        if parsed is None:
            raise InvalidParameterValue(
                d.invalid_message.format(name=d.name, value=value), position=value_position
            )
        return parsed

    def set(
        self,
        name: str,
        value: int | str | None,
        *,
        name_position: int | None = None,
        value_position: int | None = None,
    ) -> None:
        """``SET name = value``. ``value`` 가 None 이면 ``SET name TO DEFAULT`` (세션 시작 값으로).

        Raises:
            UndefinedObject: 알 수 없는 이름 (42704).
            CantChangeRuntimeParam: 읽기 전용 설정 (55P02).
            InvalidParameterValue: 값을 해석할 수 없음 (22023).
        """
        d = _lookup(name, name_position)
        if d.readonly:
            raise CantChangeRuntimeParam(f'parameter "{d.name}" cannot be changed')
        new = self._defaults[d.name] if value is None else self._parse(d, value, value_position)
        if d.report and new != self._values[d.name] and d.name not in self._changed:
            self._changed.append(d.name)
        self._values[d.name] = new

    def show(self, name: str, *, position: int | None = None) -> tuple[str, str]:
        """``SHOW name`` → (컬럼 이름 = 정식 설정 이름, 표시 값)."""
        d = _lookup(name, position)
        return d.name, d.show(self._values[d.name])

    def reported_values(self) -> list[tuple[str, str]]:
        """ParameterStatus 로 알리는 모든 설정의 (정식 이름, 표시 값). 시작 단계에서 보낸다."""
        return [(n, SETTINGS[n.lower()].show(self._values[n])) for n in REPORTED_SETTINGS]

    def take_reported_changes(self) -> list[tuple[str, str]]:
        """마지막 호출 이후 SET 으로 값이 바뀐 report 설정들의 (이름, 표시 값). 꺼내면 비운다."""
        if not self._changed:
            return []
        out = [(n, SETTINGS[n.lower()].show(self._values[n])) for n in self._changed]
        self._changed.clear()
        return out

    def __repr__(self) -> str:
        body = ", ".join(f"{k}={SETTINGS[k.lower()].show(v)}" for k, v in self._values.items())
        return f"Session({body})"
