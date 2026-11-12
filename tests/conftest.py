import sys
import sysconfig


def pytest_report_header(config):
    """어떤 빌드에서 테스트가 도는지 헤더에 표시한다 (uv 의 venv 재사용 함정 확인용)."""
    ft = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    gil = getattr(sys, "_is_gil_enabled", lambda: "N/A")()
    return f"minidb: python {sys.version.split()[0]}, free-threaded build={ft}, GIL enabled={gil}"
