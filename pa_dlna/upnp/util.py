"""Utilities."""

import asyncio
import functools
import logging

logger = logging.getLogger('util')

NL_INDENT = '\n        '

def shorten(txt, head_len=10, tail_len=5):
    if len(txt) <= head_len + 3 + tail_len:
        return txt
    return txt[:head_len] + '...' + txt[len(txt)-tail_len:]

def log_exception(logger, expt_msg):
    log_backtrace = any(handler.level == logging.DEBUG for handler in
                                                logging.getLogger().handlers)
    if log_backtrace:
        logger.exception(expt_msg)
    else:
        logger.error(f"{expt_msg}{NL_INDENT}"
                     f"Run this program at the 'debug' log level to print the"
                     " exception backtrace")

def log_unhandled_exception(logger):
    """A decorator logging exceptions occuring in a coroutine.

    Its purpose is to ensure that a task may not trigger unhandled exceptions
    in code that is running in the last except clause or in code running in
    the last finally clause. Otherwise the exception shows up only when the
    event loop terminates making the problem difficult to resolve.
    """

    def decorator(coro):
        @functools.wraps(coro)
        async def wrapper(*args, **kwargs):
            try:
                return await coro(*args, **kwargs)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                task_name = asyncio.current_task().get_name()
                expt_msg = (f"Exception in task '{task_name}' - function"
                            f' {coro.__qualname__}():{NL_INDENT}{e!r}')
                log_exception(logger, expt_msg)
                return e
        return wrapper
    return decorator

class AsyncioTasks:
    """Save references to tasks, to avoid tasks being garbage collected.

    See Python github PR 29163 and the corresponding Python issues.
    """

    def __init__(self):
        self._tasks = set()

    def create_task(self, coro, name):
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    def __iter__(self):
        for t in self._tasks:
            yield t
