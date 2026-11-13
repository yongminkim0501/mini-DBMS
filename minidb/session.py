"""세션: 연결(클라이언트) 하나의 설정 값 (PostgreSQL 의 GUC 를 아주 작게 흉내 낸다).

- 설정 정의(:data:`SETTINGS`)는 불변이라 모듈 수준에 둔다. 값은 :class:`Session` 인스턴스에만 있다.
- ``SET name = value`` / ``SHOW name`` 이 세션 값을 바꾸고 읽는다. 이름은 대소문자를 구분하지 않는다.
- 지원 설정: ``enable_indexscan`` (불리언, 기본 on). 끄면 플래너가 항상 Seq Scan 을 고른다.

**스레드 안전하지 않다.** Phase 3 에서는 연결마다 Session 을 하나씩 만들고, 한 연결은 한 번에 한 스레드만
처리하므로 락이 필요 없다 (세션은 스레드 사이에 공유하지 않는다).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .errors import InvalidParameterValue, UndefinedObject

__all__ = ["SettingDef", "SETTINGS", "Session", "parse_bool"]


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


@dataclass(frozen=True, slots=True)
class SettingDef:
    """설정 하나의 정의 (불변)."""

    name: str
    default: Any
    type_name: str
    description: str
    parse: Callable[[int | str], Any]
    show: Callable[[Any], str]


SETTINGS: Mapping[str, SettingDef] = MappingProxyType(
    {
        "enable_indexscan": SettingDef(
            "enable_indexscan",
            True,
            "bool",
            "Enables the planner's use of index-scan plans.",
            _bool_from_value,
            _show_bool,
        ),
    }
)


def _lookup(name: str, position: int | None) -> SettingDef:
    d = SETTINGS.get(name.lower())
    if d is None:
        raise UndefinedObject(f'unrecognized configuration parameter "{name}"', position=position)
    return d


class Session:
    """설정 값 묶음. ``Database.execute(sql, session)`` 에 넘긴다 (생략하면 Database 의 기본 세션).

    Args:
        **overrides: 시작 값 (예: ``Session(enable_indexscan=False)``). 알 수 없는 이름이면 UndefinedObject.
    """

    __slots__ = ("_values",)

    def __init__(self, **overrides: Any) -> None:
        self._values: dict[str, Any] = {name: d.default for name, d in SETTINGS.items()}
        for name, value in overrides.items():
            self._values[_lookup(name, None).name] = value

    # 플래너가 매 SELECT 마다 읽는 값은 속성으로 바로 꺼낸다.
    @property
    def enable_indexscan(self) -> bool:
        return bool(self._values["enable_indexscan"])

    def get(self, name: str) -> Any:
        """설정의 현재 값 (파이썬 값)."""
        return self._values[_lookup(name, None).name]

    def set(
        self,
        name: str,
        value: int | str | None,
        *,
        name_position: int | None = None,
        value_position: int | None = None,
    ) -> None:
        """``SET name = value``. ``value`` 가 None 이면 ``SET name TO DEFAULT`` (기본값으로).

        Raises:
            UndefinedObject: 알 수 없는 이름 (42704).
            InvalidParameterValue: 값을 해석할 수 없음 (22023).
        """
        d = _lookup(name, name_position)
        if value is None:
            self._values[d.name] = d.default
            return
        parsed = d.parse(value)
        if parsed is None:
            raise InvalidParameterValue(
                f'parameter "{d.name}" requires a Boolean value', position=value_position
            )
        self._values[d.name] = parsed

    def show(self, name: str, *, position: int | None = None) -> tuple[str, str]:
        """``SHOW name`` → (컬럼 이름 = 정식 설정 이름, 표시 값)."""
        d = _lookup(name, position)
        return d.name, d.show(self._values[d.name])

    def __repr__(self) -> str:
        body = ", ".join(f"{k}={SETTINGS[k].show(v)}" for k, v in self._values.items())
        return f"Session({body})"
