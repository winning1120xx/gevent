# Copyright (c) 2009-2011 Denis Bilenko. See LICENSE for details.
"""
Waiting for I/O completion.
"""
from __future__ import absolute_import, division, print_function

import sys

from gevent.event import Event
from gevent.hub import _get_hub_noargs as get_hub
from gevent.hub import sleep as _g_sleep
from gevent._compat import integer_types
from gevent._compat import iteritems
from gevent._util import copy_globals
from gevent._util import _NONE

from errno import EINTR
from select import select as _real_original_select
if sys.platform.startswith('win32'):
    def _original_select(r, w, x, t):
        # windows can't handle three empty lists, but we've always
        # accepted that
        if not r and not w and not x:
            return ((), (), ())
        return _real_original_select(r, w, x, t)
else:
    _original_select = _real_original_select


try:
    from select import POLLIN, POLLOUT, POLLNVAL
    __implements__ = ['select', 'poll']
except ImportError:
    POLLIN = 1
    POLLOUT = 4
    POLLNVAL = 32
    __implements__ = ['select']

__all__ = ['error'] + __implements__
if 'poll' not in __all__:
    __all__.append('poll')

import select as __select__

error = __select__.error

__imports__ = copy_globals(__select__, globals(),
                           names_to_ignore=__all__,
                           dunder_names_to_keep=())

_EV_READ = 1
_EV_WRITE = 2

def get_fileno(obj):
    try:
        fileno_f = obj.fileno
    except AttributeError:
        if not isinstance(obj, integer_types):
            raise TypeError('argument must be an int, or have a fileno() method: %r' % (obj,))
        return obj
    else:
        return fileno_f()


class SelectResult(object):
    __slots__ = ()

    @staticmethod
    def _make_callback(ready_collection, event, mask):
        def cb(fd, watcher):
            ready_collection.append(fd)
            watcher.close()
            event.set()
        cb.mask = mask
        return cb

    @classmethod
    def _make_watchers(cls, watchers, *fd_cb):
        loop = get_hub().loop
        io = loop.io
        MAXPRI = loop.MAXPRI

        for fdlist, callback in fd_cb:
            try:
                for fd in fdlist:
                    watcher = io(get_fileno(fd), callback.mask)
                    watcher.priority = MAXPRI
                    watchers.append(watcher)
                    watcher.start(callback, fd, watcher)
            except IOError as ex:
                raise error(*ex.args)

    @staticmethod
    def _closeall(watchers):
        for watcher in watchers:
            watcher.stop()
            watcher.close()
        del watchers[:]

    def select(self, rlist, wlist, timeout):
        watchers = []
        # read and write are the collected ready objects, accumulated
        # by the callback. Note that we could get spurious callbacks
        # if the socket is closed while we're blocked. We can't easily
        # detect that (libev filters the events passed so we can't
        # pass arbitrary events). After an iteration of polling for
        # IO, libev will invoke all the pending IO watchers, and then
        # any newly added (fed) events, and then we will invoke added
        # callbacks. With libev 4.27+ and EV_VERIFY, it's critical to
        # close our watcher immediately once we get an event. That
        # could be the close event (coming just before the actual
        # close happens), and once the FD is closed, libev will abort
        # the process if we stop the watcher.
        read = []
        write = []
        event = Event()
        add_read = self._make_callback(read, event, _EV_READ)
        add_write = self._make_callback(write, event, _EV_WRITE)

        try:
            self._make_watchers(watchers,
                                (rlist, add_read),
                                (wlist, add_write))
            event.wait(timeout=timeout)
            return read, write, []
        finally:
            self._closeall(watchers)


def select(rlist, wlist, xlist, timeout=None): # pylint:disable=unused-argument
    """An implementation of :meth:`select.select` that blocks only the current greenlet.

    .. caution:: *xlist* is ignored.

    .. versionchanged:: 1.2a1
       Raise a :exc:`ValueError` if timeout is negative. This matches Python 3's
       behaviour (Python 2 would raise a ``select.error``). Previously gevent had
       undefined behaviour.
    .. versionchanged:: 1.2a1
       Raise an exception if any of the file descriptors are invalid.
    """
    if timeout is not None and timeout < 0:
        # Raise an error like the real implementation; which error
        # depends on the version. Python 3, where select.error is OSError,
        # raises a ValueError (which makes sense). Older pythons raise
        # the error from the select syscall...but we don't actually get there.
        # We choose to just raise the ValueError as it makes more sense and is
        # forward compatible
        raise ValueError("timeout must be non-negative")

    # First, do a poll with the original select system call. This is
    # the most efficient way to check to see if any of the file
    # descriptors have previously been closed and raise the correct
    # corresponding exception. (Because libev tends to just return
    # them as ready, or, if built with EV_VERIFY >= 2 and libev >=
    # 4.27, crash the process. And libuv also tends to crash the
    # process.)
    #
    # We accept the *xlist* here even though we can't
    # below because this is all about error handling.
    sel_results = ((), (), ())
    try:
        sel_results = _original_select(rlist, wlist, xlist, 0)
    except error as e:
        enumber = getattr(e, 'errno', None) or e.args[0]
        if enumber != EINTR:
            # Ignore interrupted syscalls
            raise

    if sel_results[0] or sel_results[1] or sel_results[2] or (timeout is not None and timeout == 0):
        # If we actually had stuff ready, go ahead and return it. No need
        # to go through the trouble of doing our own stuff.

        # Likewise, if the timeout is 0, we already did a 0 timeout
        # select and we don't need to do it again. Note that in libuv,
        # zero duration timers may be called immediately, without
        # cycling the event loop at all. 2.7/test_telnetlib.py "hangs"
        # calling zero-duration timers if we go to the loop here.

        # However, because this is typically a place where scheduling switches
        # can occur, we need to make sure that's still the case; otherwise a single
        # consumer could monopolize the thread. (shows up in test_ftplib.)
        _g_sleep()
        return sel_results

    result = SelectResult()
    return result.select(rlist, wlist, timeout)



class PollResult(object):
    __slots__ = ('events', 'event')

    def __init__(self):
        self.events = set()
        self.event = Event()

    def add_event(self, events, fd):
        if events < 0:
            result_flags = POLLNVAL
        else:
            result_flags = 0
            if events & _EV_READ:
                result_flags = POLLIN
            if events & _EV_WRITE:
                result_flags |= POLLOUT

        self.events.add((fd, result_flags))
        self.event.set()

class poll(object):
    """
    An implementation of :class:`select.poll` that blocks only the current greenlet.

    .. caution:: ``POLLPRI`` data is not supported.

    .. versionadded:: 1.1b1
    .. versionchanged:: 1.5
       This is now always defined, regardless of whether the standard library
       defines :func:`select.poll` or not. Note that it may have different performance
       characteristics.
    """
    def __init__(self):
        # {int -> flags}
        # We can't keep watcher objects in here because people commonly
        # just drop the poll object when they're done, without calling
        # unregister(). dnspython does this.
        self.fds = {}
        self.loop = get_hub().loop

    def register(self, fd, eventmask=_NONE):
        if eventmask is _NONE:
            flags = _EV_READ | _EV_WRITE
        else:
            flags = 0
            if eventmask & POLLIN:
                flags = _EV_READ
            if eventmask & POLLOUT:
                flags |= _EV_WRITE
            # If they ask for POLLPRI, we can't support
            # that. Should we raise an error?

        fileno = get_fileno(fd)
        self.fds[fileno] = flags

    def modify(self, fd, eventmask):
        self.register(fd, eventmask)

    def poll(self, timeout=None):
        """
        poll the registered fds.

        .. versionchanged:: 1.2a1
           File descriptors that are closed are reported with POLLNVAL.

        .. versionchanged:: 1.3a2
           Under libuv, interpret *timeout* values less than 0 the same as *None*,
           i.e., block. This was always the case with libev.
        """
        result = PollResult()
        watchers = []
        io = self.loop.io
        MAXPRI = self.loop.MAXPRI
        try:
            for fd, flags in iteritems(self.fds):
                watcher = io(fd, flags)
                watchers.append(watcher)
                watcher.priority = MAXPRI
                watcher.start(result.add_event, fd, pass_events=True)
            if timeout is not None:
                if timeout < 0:
                    # The docs for python say that an omitted timeout,
                    # a negative timeout and a timeout of None are all
                    # supposed to block forever. Many, but not all
                    # OS's accept any negative number to mean that. Some
                    # OS's raise errors for anything negative but not -1.
                    # Python 3.7 changes to always pass exactly -1 in that
                    # case from selectors.

                    # Our Timeout class currently does not have a defined behaviour
                    # for negative values. On libuv, it uses a check watcher and effectively
                    # doesn't block. On libev, it seems to block. In either case, we
                    # *want* to block, so turn this into the sure fire block request.
                    timeout = None
                elif timeout:
                    # The docs for poll.poll say timeout is in
                    # milliseconds. Our result objects work in
                    # seconds, so this should be *=, shouldn't it?
                    timeout /= 1000.0
            result.event.wait(timeout=timeout)
            return list(result.events)
        finally:
            for awatcher in watchers:
                awatcher.stop()
                awatcher.close()

    def unregister(self, fd):
        """
        Unregister the *fd*.

        .. versionchanged:: 1.2a1
           Raise a `KeyError` if *fd* was not registered, like the standard
           library. Previously gevent did nothing.
        """
        fileno = get_fileno(fd)
        del self.fds[fileno]


def _gevent_do_monkey_patch(patch_request):
    aggressive = patch_request.patch_kwargs['aggressive']
    target_mod = patch_request.target_module

    patch_request.default_patch_items()

    if aggressive:
        # since these are blocking we're removing them here. This makes some other
        # modules (e.g. asyncore)  non-blocking, as they use select that we provide
        # when none of these are available.
        patch_request.remove_item(
            'epoll'
            'kqueue',
            'kevent',
            'devpoll',
        )

    if patch_request.PY3:
        # TODO: Do we need to broadcast events about patching the selectors
        # package? If so, must be careful to deal with DoNotPatch exceptions.

        # Python 3 wants to use `select.select` as a member function,
        # leading to this error in selectors.py (because
        # gevent.select.select is not a builtin and doesn't get the
        # magic auto-static that they do):
        #
        #    r, w, _ = self._select(self._readers, self._writers, [], timeout)
        #    TypeError: select() takes from 3 to 4 positional arguments but 5 were given
        #
        # Note that this obviously only happens if selectors was
        # imported after we had patched select; but there is a code
        # path that leads to it being imported first (but now we've
        # patched select---so we can't compare them identically). It also doesn't
        # happen on Windows, because they define a normal method for _select, to work around
        # some weirdness in the handling of the third argument.

        orig_select_select = patch_request.get_original('select', 'select')
        assert target_mod.select is not orig_select_select
        selectors = __import__('selectors')
        if selectors.SelectSelector._select in (target_mod.select, orig_select_select):
            def _select(self, *args, **kwargs): # pylint:disable=unused-argument
                return select(*args, **kwargs)
            selectors.SelectSelector._select = _select
            _select._gevent_monkey = True # prove for test cases

        # Python 3.7 refactors the poll-like selectors to use a common
        # base class and capture a reference to select.poll, etc, at
        # import time. selectors tends to get imported early
        # (importing 'platform' does it: platform -> subprocess -> selectors),
        # so we need to clean that up.
        if hasattr(selectors, 'PollSelector') and hasattr(selectors.PollSelector, '_selector_cls'):
            selectors.PollSelector._selector_cls = poll

        if aggressive:
            # If `selectors` had already been imported before we removed
            # select.epoll|kqueue|devpoll, these may have been defined in terms
            # of those functions. They'll fail at runtime.
            patch_request.remove_item(
                selectors,
                'EpollSelector',
                'KqueueSelector',
                'DevpollSelector',
            )
            selectors.DefaultSelector = getattr(
                selectors,
                'PollSelector',
                selectors.SelectSelector
            )
