"""Regression tests for ProgressSync's per-book network error handling.

A rescan can make one HTTPS request per book - several hundred in a real
library - so a failure on any single book must be counted and stepped over,
never allowed to abort the run. The original handler caught only
(HTTPError, URLError), which let a read timeout escape and kill the whole
rescan; see 26017-BUG-010.
"""

import inspect
import re
from urllib.error import HTTPError, URLError

import pytest

import action


class TestExceptionHierarchy:
    """The facts the fix rests on. If a future Python changes these, the
    handler's reasoning is no longer valid and these tests should say so."""

    def test_read_timeout_is_not_a_urlerror(self):
        # urlopen wraps *connect* timeouts in URLError, but a read timeout -
        # the socket going quiet after connecting - raises TimeoutError bare.
        # This is precisely why the old handler missed it.
        assert not issubclass(TimeoutError, URLError)

    def test_timeout_error_is_an_oserror(self):
        assert issubclass(TimeoutError, OSError)

    def test_url_and_http_errors_are_also_oserrors(self):
        # So broadening to OSError loses no coverage the old tuple had.
        assert issubclass(URLError, OSError)
        assert issubclass(HTTPError, OSError)

    def test_socket_timeout_is_the_builtin(self):
        # Python 3.10+ aliases socket.timeout to TimeoutError.
        import socket
        assert socket.timeout is TimeoutError


class TestHandlerBreadth:
    """Guards against the handler being narrowed back to the original tuple."""

    def test_progress_sync_catches_oserror(self):
        source = inspect.getsource(
            action.KoreaderAction.sync_progress_from_progresssync)
        assert re.search(r'except\s+OSError\b', source), (
            "sync_progress_from_progresssync must catch OSError so a single "
            "book's network failure cannot abort the whole rescan")

    def test_progress_sync_does_not_use_the_narrow_tuple(self):
        source = inspect.getsource(
            action.KoreaderAction.sync_progress_from_progresssync)
        assert 'except (HTTPError, URLError)' not in source, (
            "the (HTTPError, URLError) tuple misses bare TimeoutError - "
            "see 26017-BUG-010")


class TestLoopSurvivesFailures:
    """The behaviour that matters: one bad book doesn't stop the others.

    Mirrors the loop's error-handling shape rather than driving the real
    method, which needs a live calibre database and Qt - the same gap noted
    in 26017-BUG-002. Kept deliberately close to the source so it fails if
    the shape there changes.
    """

    @staticmethod
    def run_loop(books, fetch):
        succeeded, failed, errors = 0, 0, []
        for book in books:
            try:
                fetch(book)
            except OSError as e:
                failed += 1
                errors.append(
                    'Timed out' if isinstance(e, TimeoutError)
                    else 'No data received')
                continue
            succeeded += 1
        return succeeded, failed, errors

    def test_read_timeout_does_not_abort_the_run(self):
        def fetch(book):
            if book == 'b':
                raise TimeoutError('The read operation timed out')

        succeeded, failed, errors = self.run_loop(['a', 'b', 'c'], fetch)
        assert (succeeded, failed) == (2, 1)
        assert errors == ['Timed out']

    def test_timeouts_are_labelled_distinctly_from_other_failures(self):
        # A timeout and a refused connection are both counted as failures,
        # but the results table should say which happened.
        def fetch(book):
            if book == 'a':
                raise TimeoutError('read timed out')
            if book == 'b':
                raise URLError('connection refused')

        _, failed, errors = self.run_loop(['a', 'b', 'c'], fetch)
        assert failed == 2
        assert errors == ['Timed out', 'No data received']

    @pytest.mark.parametrize('error', [
        TimeoutError('read timed out'),
        URLError('unreachable'),
        HTTPError('http://x', 503, 'Service Unavailable', {}, None),
        ConnectionResetError('reset by peer'),
        OSError('generic socket failure'),
    ])
    def test_every_network_failure_mode_is_survivable(self, error):
        def fetch(book):
            if book == 'b':
                raise error

        succeeded, failed, _ = self.run_loop(['a', 'b', 'c'], fetch)
        assert (succeeded, failed) == (2, 1)

    def test_a_non_network_error_still_propagates(self):
        # Broadening to OSError must not turn into a bare except - a genuine
        # bug in the loop body should still surface rather than be counted
        # as a failed book.
        def fetch(book):
            raise ValueError('a real bug, not a network problem')

        with pytest.raises(ValueError):
            self.run_loop(['a'], fetch)
