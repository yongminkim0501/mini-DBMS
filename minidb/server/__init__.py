"""Phase 3: 스레드 풀 기반 HTTP API 서버 (http.server/socketserver/ThreadPoolExecutor 없이 직접 구현).

- :mod:`.http`       HTTP/1.1 요청 파서와 응답 생성
- :mod:`.pool`       고정 크기 워커 풀 (백프레셔: 큐가 차면 즉시 거부)
- :mod:`.dispatcher` selectors 기반 연결 감시, 요청이 온 연결만 워커에 배정
- :mod:`.api`        엔드포인트, /query 실행, 세션 저장소
- :mod:`.server`     조립, 시작, 정상 종료
"""

from .server import Server, ServerConfig

__all__ = ["Server", "ServerConfig"]
