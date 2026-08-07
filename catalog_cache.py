"""프레임워크 독립적인 TTL + 중복요청방지 캐시 — 탑툰 카탈로그 전용.

배경
----
`client.TopToonClient.fetch_catalog()` 는 ~6MB JSON 을 ``timeout=120`` 으로
받는다. `mod_basic.py` 의 `browse_list` 커맨드가 요청 스레드에서 이걸 매번
기다리면 최대 120초 블로킹이 생기고, nginx 기본 ``proxy_read_timeout``(보통
60초)에 걸려 504 나 브라우저 멈춤으로 나타난다("연재목록" 메뉴를 여는 순간).

여기서는 값을 프로세스 메모리에 캐시하고, 여러 요청이 동시에 갱신을
트리거해도 실제 fetch 는 한 번만 일어나게 한다(dedupe). ``refresh_async()``
가 백그라운드로 값을 채우는 동안 호출자는 ``snapshot()`` 으로 즉시 응답을
만들 수 있다.

`mod_basic.py` 밖으로 뺀 이유
------------------------------
`mod_basic.py` 는 ``from .setup import *`` 로 flaskfarm 프레임워크에 묶여
있어 단독 임포트/테스트가 불가능하다(``shutdown.py`` 와 동일한 이유 —
``shutdown.py`` 도 같은 이유로 별도 파일이다). 이 파일은 표준 라이브러리만
쓰므로 프레임워크 없이 단독 테스트할 수 있다(``test_catalog_cache.py``).
"""
import threading
import time
from typing import Any, Callable, Optional


class TTLDedupeCache:
    """값 하나를 담는 TTL 캐시.

    상태 전이: ``idle`` → ``fetching`` → (``ready`` | ``error``).
    ``refresh_async()`` 를 여러 번 불러도 이미 ``fetching`` 중이면 두 번째
    호출부터는 아무 스레드도 새로 만들지 않는다 — 동시 요청이 같은 6MB 를
    중복으로 받는 것을 막는 핵심 장치다.
    """

    def __init__(self, start_thread: Optional[Callable[[Callable[[], None]], None]] = None):
        self._lock = threading.Lock()
        self._value: Any = None
        self._fetched_at: float = 0.0
        self._status: str = 'idle'   # idle | fetching | ready | error
        self._error: str = ''
        # 스레드 시작 방법을 주입 가능하게 한다 — 기본은 진짜 데몬 스레드지만
        # 테스트에서는 동기 실행 스텁으로 바꿔치기해 실제 스레딩 없이
        # 상태 전이만 검증할 수 있다.
        self._start_thread = start_thread or (
            lambda fn: threading.Thread(target=fn, daemon=True).start())

    def snapshot(self) -> dict:
        """현재 상태의 얕은 스냅샷. 호출자가 변조해도 내부 상태엔 영향 없다."""
        with self._lock:
            return {'value': self._value, 'fetched_at': self._fetched_at,
                    'status': self._status, 'error': self._error}

    def is_stale(self, ttl_seconds: float) -> bool:
        """값이 없거나 fetched_at 이 ttl_seconds 이상 지났으면 True.

        ``>=`` 를 쓴다(``>`` 아님) — ttl_seconds=0 은 "항상 즉시 stale"이어야
        하는데, 시계 해상도 때문에 경과시간이 정확히 0.0 으로 관측될 수
        있어(같은 틱에서 fetched_at 과 now 가 같은 값) ``>`` 로는 그 순간
        stale 판정이 빠지는 경계 버그가 생긴다.
        """
        with self._lock:
            if self._value is None:
                return True
            return (time.time() - self._fetched_at) >= ttl_seconds

    def refresh_async(self, fetch_fn: Callable[[], Any]) -> bool:
        """``fetch_fn()`` 결과로 캐시를 채우는 백그라운드 작업을 건다.

        이미 진행 중이면 아무것도 하지 않고 False. 새로 걸었으면 True.
        ``fetch_fn`` 이 예외를 던지면 ``status='error'`` 로 기록하고(``str(e)``)
        스레드 밖으로 전파하지 않는다 — 호출자는 ``snapshot()`` 으로 폴링한다.
        """
        with self._lock:
            if self._status == 'fetching':
                return False
            self._status = 'fetching'
            self._error = ''

        def _run():
            try:
                value = fetch_fn()
                with self._lock:
                    self._value = value
                    self._fetched_at = time.time()
                    self._status = 'ready'
                    self._error = ''
            except Exception as e:
                with self._lock:
                    self._status = 'error'
                    self._error = str(e)

        self._start_thread(_run)
        return True

    def set_error_sync(self, msg: str) -> None:
        """네트워크 호출 없이 즉시 판정 가능한 오류(예: 쿠키 미설정)를 동기로 기록.

        ``refresh_async()`` 처럼 ``fetching`` 상태를 거치지 않고 바로
        ``error`` 로 남긴다 — 스레드를 띄울 필요가 없는 빠른 실패 경로.
        """
        with self._lock:
            self._status = 'error'
            self._error = msg


# ── 프로세스 전역 공유 싱글턴 ─────────────────────────────────────────
# mod_basic.py(browse_list 커맨드)와 manual_worker.py(analyze)가 둘 다
# 탑툰 카탈로그(~6MB)를 필요로 한다. 각자 TTLDedupeCache() 를 따로 만들면
# (1) 메모리에 6MB 를 두 벌 들고 있고 (2) 캐시/dedupe 효과가 반으로
# 쪼개진다(한쪽이 갱신해도 다른 쪽은 모른다) — 두 모듈이 정확히 같은
# 인스턴스를 봐야 의미가 있다. 여기 한 곳에만 만들고 접근자로 노출한다.
#
# 지연 생성(lazy singleton)을 쓰는 이유: 이 모듈은 임포트만으로 부작용이
# 없어야 한다(프레임워크 독립 모듈이라 단독 테스트 대상이다) — 모듈
# 레벨에서 바로 `TTLDedupeCache()` 를 인스턴스화해도 사실 부작용은 없지만
# (생성자가 스레드를 안 띄운다), 호출부에서 "언제 만들어지는지"를 명시적으로
# 통제하고 싶어 접근자 함수로 감쌌다.
_shared_catalog_cache: Optional['TTLDedupeCache'] = None
_shared_catalog_cache_lock = threading.Lock()


def get_shared_catalog_cache() -> 'TTLDedupeCache':
    """프로세스 전역 카탈로그 캐시 싱글턴. mod_basic.py 와 manual_worker.py
    가 반드시 이 함수를 통해서만 캐시를 얻어야 한다 — 각자 새
    TTLDedupeCache() 를 만들면 공유가 깨진다.
    """
    global _shared_catalog_cache
    if _shared_catalog_cache is None:
        with _shared_catalog_cache_lock:
            if _shared_catalog_cache is None:
                _shared_catalog_cache = TTLDedupeCache()
    return _shared_catalog_cache
