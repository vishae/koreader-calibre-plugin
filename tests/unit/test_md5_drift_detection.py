"""Tests for detecting stale MD5 hashes and recalculating them.

A stored hash silently ceasing to describe its file is the failure mode
behind 26017-BUG-007: ProgressSync just does nothing for that book, and
"Calculate Missing MD5 Hashes" won't touch it because the column isn't
empty. These cover the comparison and the decisions around it.

The actions themselves need a live calibre database and Qt, so what is
driven directly here is hash_one_book()'s contract and the comparison
logic the two actions are built on - the same approach as
test_progress_sync_network_errors.py.
"""

import hashlib

import pytest

import action
from koreader_hash import calculate_koreader_md5


def write_book(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


class TestStoredVersusComputed:
    """The comparison both new actions turn on."""

    @staticmethod
    def is_stale(stored, computed):
        # Mirrors the check in check_md5_hashes / recalculate_md5_hashes.
        return str(stored or '').strip().lower() != computed

    def test_identical_values_are_not_stale(self):
        digest = hashlib.md5(b'x').hexdigest()
        assert not self.is_stale(digest, digest)

    def test_different_values_are_stale(self):
        assert self.is_stale('a' * 32, 'b' * 32)

    def test_comparison_is_case_insensitive(self):
        # Calibre's column collates NOCASE and hashes have been entered by
        # hand before now; an upper-case stored value is not drift.
        digest = hashlib.md5(b'x').hexdigest()
        assert not self.is_stale(digest.upper(), digest)

    def test_surrounding_whitespace_is_not_drift(self):
        digest = hashlib.md5(b'x').hexdigest()
        assert not self.is_stale(f'  {digest}\n', digest)

    @pytest.mark.parametrize('stored', [None, '', '   '])
    def test_an_empty_stored_value_counts_as_different(self, stored):
        # check_md5_hashes skips these before reaching the comparison -
        # an empty column is the calculator's job, not drift. But
        # recalculate_md5_hashes does reach it, and must treat it as
        # something to fill rather than as already correct.
        assert self.is_stale(stored, 'a' * 32)


class TestHashOneBook:
    """The shared resolve-and-hash helper the three actions call, so they can
    never disagree about which format a book is judged on."""

    class FakeDB:
        def __init__(self, formats, paths):
            self._formats = formats
            self._paths = paths

        def formats(self, book_id):
            return self._formats.get(book_id, [])

        def format_abspath(self, book_id, fmt):
            return self._paths.get((book_id, fmt))

    @staticmethod
    def call(db, book_id, metadata=None, device_formats=None):
        return action.KoreaderAction.hash_one_book(
            None, db, book_id, metadata or {}, '', device_formats or {})

    def test_hashes_the_resolved_format(self, tmp_path):
        data = b'a book' * 500
        path = write_book(tmp_path, 'book.epub', data)
        db = self.FakeDB({1: ['EPUB']}, {(1, 'EPUB'): path})

        md5_value, book_format, source, skipped = self.call(db, 1)

        assert skipped is None
        assert book_format == 'EPUB'
        assert source == 'preference'
        assert md5_value == calculate_koreader_md5(path)

    def test_device_cache_decides_which_file_is_hashed(self, tmp_path):
        # The 26017-BUG-009 case: two formats, and the device holds the PDF.
        # Detection must judge the book on the same file the device reads,
        # or it reports drift that isn't there.
        epub = write_book(tmp_path, 'book.epub', b'epub bytes' * 300)
        pdf = write_book(tmp_path, 'book.pdf', b'pdf bytes' * 300)
        db = self.FakeDB({1: ['EPUB', 'PDF']},
                         {(1, 'EPUB'): epub, (1, 'PDF'): pdf})

        md5_value, book_format, source, _ = self.call(
            db, 1, device_formats={1: 'PDF'})

        assert (book_format, source) == ('PDF', 'device cache')
        assert md5_value == calculate_koreader_md5(pdf)
        assert md5_value != calculate_koreader_md5(epub)

    def test_reports_no_format_when_nothing_is_readable(self):
        db = self.FakeDB({1: ['LIT', 'LRF']}, {})
        md5_value, book_format, _, skipped = self.call(db, 1)
        assert (md5_value, book_format, skipped) == (None, None, 'no format')

    def test_reports_no_format_when_calibre_has_no_path(self):
        db = self.FakeDB({1: ['EPUB']}, {})  # format known, file unregistered
        md5_value, _, _, skipped = self.call(db, 1)
        assert (md5_value, skipped) == (None, 'no format')

    def test_reports_unreadable_when_the_file_is_gone(self, tmp_path):
        db = self.FakeDB({1: ['EPUB']},
                         {(1, 'EPUB'): str(tmp_path / 'missing.epub')})
        md5_value, book_format, _, skipped = self.call(db, 1)
        assert (md5_value, book_format, skipped) == (None, 'EPUB', 'unreadable')

    def test_a_rewritten_file_produces_a_different_hash(self, tmp_path):
        # What drift actually is: the file changed under a stored value.
        path = write_book(tmp_path, 'book.epub', b'original' * 500)
        db = self.FakeDB({1: ['EPUB']}, {(1, 'EPUB'): path})
        before, _, _, _ = self.call(db, 1)

        write_book(tmp_path, 'book.epub', b'rewritten' * 500)
        after, _, _, _ = self.call(db, 1)

        assert before != after


class TestDetectionIsReadOnly:
    """The detector must never write. Its whole value is being safe to run."""

    def test_check_md5_hashes_never_calls_set_metadata(self):
        import inspect
        source = inspect.getsource(action.KoreaderAction.check_md5_hashes)
        assert 'set_metadata' not in source, (
            'check_md5_hashes must be read-only - writing belongs to '
            'recalculate_md5_hashes')
        assert 'metadata.set(' not in source


class TestRecalculateIsScoped:
    """Overwriting is the dangerous direction: a blind whole-library pass
    would destroy hashes learned from a real KOReader device, which is
    exactly what happened once already (see 26017-BUG-008's escalation)."""

    @staticmethod
    def source():
        import inspect
        return inspect.getsource(action.KoreaderAction.recalculate_md5_hashes)

    def test_operates_on_the_selection_not_the_whole_library(self):
        assert 'selectionModel' in self.source()
        assert 'all_book_ids' not in self.source(), (
            'recalculate must never sweep the whole library')

    def test_asks_for_confirmation_before_overwriting(self):
        assert 'confirm(' in self.source()
