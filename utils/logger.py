import logging
import sys

def get_logger():
    logger = logging.getLogger("okx_quant")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s %(extra)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    h.setFormatter(fmt)
    logger.addHandler(h)

    def _log_with_extra(level, msg, extra=None):
        if extra is None:
            extra = {}
        logger.log(level, msg, extra={"extra": extra})

    logger.info = lambda msg, extra=None: _log_with_extra(logging.INFO, msg, extra)
    logger.warning = lambda msg, extra=None: _log_with_extra(logging.WARNING, msg, extra)
    logger.error = lambda msg, extra=None: _log_with_extra(logging.ERROR, msg, extra)
    logger.exception = lambda msg, extra=None: _log_with_extra(logging.ERROR, msg, extra)

    return logger
