# pool/impl.py
# Copyright (C) 2005-2025 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php


"""Pool implementation classes.

"""

import traceback
import weakref

from .base import _AsyncConnDialect
from .base import _ConnectionFairy
from .base import _ConnectionRecord
from .base import Pool
from .. import exc
from .. import util
from ..util import chop_traceback
from ..util import queue as sqla_queue
from ..util import threading


class QueuePool(Pool):

    """A :class:`_pool.Pool`
    that imposes a limit on the number of open connections.

    :class:`.QueuePool` is the default pooling implementation used for
    all :class:`_engine.Engine` objects, unless the SQLite dialect is in use.

    """

    _is_asyncio = False
    _queue_class = sqla_queue.Queue

    def __init__(
        self,
        creator,
        pool_size=5,
        max_overflow=10,
        timeout=30.0,
        use_lifo=False,
        **kw
    ):
        r"""
        Construct a QueuePool.

        :param creator: a callable function that returns a DB-API
          connection object, same as that of :paramref:`_pool.Pool.creator`.

        :param pool_size: The size of the pool to be maintained,
          defaults to 5. This is the largest number of connections that
          will be kept persistently in the pool. Note that the pool
          begins with no connections; once this number of connections
          is requested, that number of connections will remain.
          ``pool_size`` can be set to 0 to indicate no size limit; to
          disable pooling, use a :class:`~sqlalchemy.pool.NullPool`
          instead.

        :param max_overflow: The maximum overflow size of the
          pool. When the number of checked-out connections reaches the
          size set in pool_size, additional connections will be
          returned up to this limit. When those additional connections
          are returned to the pool, they are disconnected and
          discarded. It follows then that the total number of
          simultaneous connections the pool will allow is pool_size +
          `max_overflow`, and the total number of "sleeping"
          connections the pool will allow is pool_size. `max_overflow`
          can be set to -1 to indicate no overflow limit; no limit
          will be placed on the total number of concurrent
          connections. Defaults to 10.

        :param timeout: The number of seconds to wait before giving up
          on returning a connection. Defaults to 30.0. This can be a float
          but is subject to the limitations of Python time functions which
          may not be reliable in the tens of milliseconds.

        :param use_lifo: use LIFO (last-in-first-out) when retrieving
          connections instead of FIFO (first-in-first-out). Using LIFO, a
          server-side timeout scheme can reduce the number of connections used
          during non-peak periods of use.   When planning for server-side
          timeouts, ensure that a recycle or pre-ping strategy is in use to
          gracefully handle stale connections.

          .. versionadded:: 1.3

          .. seealso::

            :ref:`pool_use_lifo`

            :ref:`pool_disconnects`

        :param \**kw: Other keyword arguments including
          :paramref:`_pool.Pool.recycle`, :paramref:`_pool.Pool.echo`,
          :paramref:`_pool.Pool.reset_on_return` and others are passed to the
          :class:`_pool.Pool` constructor.

        """
        Pool.__init__(self, creator, **kw)
        self._pool = self._queue_class(pool_size, use_lifo=use_lifo)
        self._overflow = 0 - pool_size
        self._max_overflow = max_overflow
        self._timeout = timeout
        self._overflow_lock = threading.Lock()

    def _do_return_conn(self, conn):
        try:
            self._pool.put(conn, False)
        except sqla_queue.Full:
            try:
                conn.close()
            finally:
                self._dec_overflow()

    def _do_get(self):
        use_overflow = self._max_overflow > -1

        try:
            wait = use_overflow and self._overflow >= self._max_overflow
            return self._pool.get(wait, self._timeout)
        except sqla_queue.Empty:
            # don't do things inside of "except Empty", because when we say
            # we timed out or can't connect and raise, Python 3 tells
            # people the real error is queue.Empty which it isn't.
            pass
        if use_overflow and self._overflow >= self._max_overflow:
            if not wait:
                return self._do_get()
            else:
                raise exc.TimeoutError(
                    "QueuePool limit of size %d overflow %d reached, "
                    "connection timed out, timeout %0.2f"
                    % (self.size(), self.overflow(), self._timeout),
                    code="3o7r",
                )

        if self._inc_overflow():
            try:
                return self._create_connection()
            except:
                with util.safe_reraise():
                    self._dec_overflow()
        else:
            return self._do_get()

    def _inc_overflow(self):
        if self._max_overflow == -1:
            self._overflow += 1
            return True
        with self._overflow_lock:
            if self._overflow < self._max_overflow:
                self._overflow += 1
                return True
            else:
                return False

    def _dec_overflow(self):
        if self._max_overflow == -1:
            self._overflow -= 1
            return True
        with self._overflow_lock:
            self._overflow -= 1
            return True

    def recreate(self):
        self.logger.info("Pool recreating")
        return self.__class__(
            self._creator,
            pool_size=self._pool.maxsize,
            max_overflow=self._max_overflow,
            pre_ping=self._pre_ping,
            use_lifo=self._pool.use_lifo,
            timeout=self._timeout,
            recycle=self._recycle,
            echo=self.echo,
            logging_name=self._orig_logging_name,
            reset_on_return=self._reset_on_return,
            _dispatch=self.dispatch,
            dialect=self._dialect,
        )

    def dispose(self):
        while True:
            try:
                conn = self._pool.get(False)
                conn.close()
            except sqla_queue.Empty:
                break

        self._overflow = 0 - self.size()
        self.logger.info("Pool disposed. %s", self.status())

    def status(self):
        return (
            "Pool size: %d  Connections in pool: %d "
            "Current Overflow: %d Current Checked out "
            "connections: %d"
            % (
                self.size(),
                self.checkedin(),
                self.overflow(),
                self.checkedout(),
            )
        )

    def size(self):
        return self._pool.maxsize

    def timeout(self):
        return self._timeout

    def checkedin(self):
        return self._pool.qsize()

    def overflow(self):
        return self._overflow

    def checkedout(self):
        return self._pool.maxsize - self._pool.qsize() + self._overflow


class AsyncAdaptedQueuePool(QueuePool):
    _is_asyncio = True
    _queue_class = sqla_queue.AsyncAdaptedQueue
    _dialect = _AsyncConnDialect()


class FallbackAsyncAdaptedQueuePool(AsyncAdaptedQueuePool):
    _queue_class = sqla_queue.FallbackAsyncAdaptedQueue


class NullPool(Pool):

    """A Pool which does not pool connections.

    Instead it literally opens and closes the underlying DB-API connection
    per each connection open/close.

    Reconnect-related functions such as ``recycle`` and connection
    invalidation are not supported by this Pool implementation, since
    no connections are held persistently.

    """

    def status(self):
        return "NullPool"

    def _do_return_conn(self, conn):
        conn.close()

    def _do_get(self):
        return self._create_connection()

    def recreate(self):
        self.logger.info("Pool recreating")

        return self.__class__(
            self._creator,
            recycle=self._recycle,
            echo=self.echo,
            logging_name=self._orig_logging_name,
            reset_on_return=self._reset_on_return,
            pre_ping=self._pre_ping,
            _dispatch=self.dispatch,
            dialect=self._dialect,
        )

    def dispose(self):
        pass


class SingletonThreadPool(Pool):

    """A Pool that maintains one connection per thread.

    Maintains one connection per each thread, never moving a connection to a
    thread other than the one which it was created in.

    .. warning::  the :class:`.SingletonThreadPool` will call ``.close()``
       on arbitrary connections that exist beyond the size setting of
       ``pool_size``, e.g. if more unique **thread identities**
       than what ``pool_size`` states are used.   This cleanup is
       non-deterministic and not sensitive to whether or not the connections
       linked to those thread identities are currently in use.

       :class:`.SingletonThreadPool` may be improved in a future release,
       however in its current status it is generally used only for test
       scenarios using a SQLite ``:memory:`` database and is not recommended
       for production use.


    Options are the same as those of :class:`_pool.Pool`, as well as:

    :param pool_size: The number of threads in which to maintain connections
        at once.  Defaults to five.

    :class:`.SingletonThreadPool` is used by the SQLite dialect
    automatically when a memory-based database is used.
    See :ref:`sqlite_toplevel`.

    """

    _is_asyncio = False

    def __init__(self, creator, pool_size=5, **kw):
        Pool.__init__(self, creator, **kw)
        self._conn = threading.local()
        self._fairy = threading.local()
        self._all_conns = set()
        self.size = pool_size

    def recreate(self):
        self.logger.info("Pool recreating")
        return self.__class__(
            self._creator,
            pool_size=self.size,
            recycle=self._recycle,
            echo=self.echo,
            pre_ping=self._pre_ping,
            logging_name=self._orig_logging_name,
            reset_on_return=self._reset_on_return,
            _dispatch=self.dispatch,
            dialect=self._dialect,
        )

    def dispose(self):
        """Dispose of this pool."""

        for conn in self._all_conns:
            try:
                conn.close()
            except Exception:
                # pysqlite won't even let you close a conn from a thread
                # that didn't create it
                pass

        self._all_conns.clear()

    def _cleanup(self):
        while len(self._all_conns) >= self.size:
            c = self._all_conns.pop()
            c.close()

    def status(self):
        return "SingletonThreadPool id:%d size: %d" % (
            id(self),
            len(self._all_conns),
        )

    def _do_return_conn(self, conn):
        pass

    def _do_get(self):
        try:
            c = self._conn.current()
            if c:
                return c
        except AttributeError:
            pass
        c = self._create_connection()
        self._conn.current = weakref.ref(c)
        if len(self._all_conns) >= self.size:
            self._cleanup()
        self._all_conns.add(c)
        return c

    def connect(self):
        # vendored from Pool to include the now removed use_threadlocal
        # behavior
        try:
            rec = self._fairy.current()
        except AttributeError:
            pass
        else:
            if rec is not None:
                return rec._checkout_existing()

        return _ConnectionFairy._checkout(self, self._fairy)

    def _return_conn(self, record):
        try:
            del self._fairy.current
        except AttributeError:
            pass
        self._do_return_conn(record)


class StaticPool(Pool):

    """A Pool of exactly one connection, used for all requests.

    Reconnect-related functions such as ``recycle`` and connection
    invalidation (which is also used to support auto-reconnect) are only
    partially supported right now and may not yield good results.


    """

    @util.memoized_property
    def connection(self):
        return _ConnectionRecord(self)

    def status(self):
        return "StaticPool"

    def dispose(self):
        if (
            "connection" in self.__dict__
            and self.connection.dbapi_connection is not None
        ):
            self.connection.close()
            del self.__dict__["connection"]

    def recreate(self):
        self.logger.info("Pool recreating")
        return self.__class__(
            creator=self._creator,
            recycle=self._recycle,
            reset_on_return=self._reset_on_return,
            pre_ping=self._pre_ping,
            echo=self.echo,
            logging_name=self._orig_logging_name,
            _dispatch=self.dispatch,
            dialect=self._dialect,
        )

    def _transfer_from(self, other_static_pool):
        # used by the test suite to make a new engine / pool without
        # losing the state of an existing SQLite :memory: connection
        self._invoke_creator = (
            lambda crec: other_static_pool.connection.dbapi_connection
        )

    def _create_connection(self):
        raise NotImplementedError()

    def _do_return_conn(self, conn):
        pass

    def _do_get(self):
        rec = self.connection
        if rec._is_hard_or_soft_invalidated():
            del self.__dict__["connection"]
            rec = self.connection

        return rec


class AssertionPool(Pool):

    """A :class:`_pool.Pool` that allows at most one checked out connection at
    any given time.

    This will raise an exception if more than one connection is checked out
    at a time.  Useful for debugging code that is using more connections
    than desired.

    """

    def __init__(self, *args, **kw):
        self._conn = None
        self._checked_out = False
        self._store_traceback = kw.pop("store_traceback", True)
        self._checkout_traceback = None
        Pool.__init__(self, *args, **kw)

    def status(self):
        return "AssertionPool"

    def _do_return_conn(self, conn):
        if not self._checked_out:
            raise AssertionError("connection is not checked out")
        self._checked_out = False
        assert conn is self._conn

    def dispose(self):
        self._checked_out = False
        if self._conn:
            self._conn.close()

    def recreate(self):
        self.logger.info("Pool recreating")
        return self.__class__(
            self._creator,
            echo=self.echo,
            pre_ping=self._pre_ping,
            recycle=self._recycle,
            reset_on_return=self._reset_on_return,
            logging_name=self._orig_logging_name,
            _dispatch=self.dispatch,
            dialect=self._dialect,
        )

    def _do_get(self):
        if self._checked_out:
            if self._checkout_traceback:
                suffix = " at:\n%s" % "".join(
                    chop_traceback(self._checkout_traceback)
                )
            else:
                suffix = ""
            raise AssertionError("connection is already checked out" + suffix)

        if not self._conn:
            self._conn = self._create_connection()

        self._checked_out = True
        if self._store_traceback:
            self._checkout_traceback = traceback.format_stack()
        return self._conn
