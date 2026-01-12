import time
from typing import Callable, Type, Tuple

def retry(
    tries: int = 3,
    delay: float = 0.5,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    def deco(fn: Callable):
        def wrapper(*args, **kwargs):
            _tries, _delay = tries, delay
            last_exc = None
            while _tries > 0:
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    _tries -= 1
                    if _tries <= 0:
                        raise
                    time.sleep(_delay)
                    _delay *= backoff
            raise last_exc
        return wrapper
    return deco
