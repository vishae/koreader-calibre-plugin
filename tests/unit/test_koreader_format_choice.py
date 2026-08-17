"""Tests for choosing which of a book's formats to hash.

The hash itself is format-agnostic (see test_koreader_hash.py); these cover
only the decision of *which file* gets hashed, which is where a wrong answer
silently produces a valid-looking hash that never matches anything.
"""

import pytest

from koreader_hash import (
    KOREADER_FORMATS,
    choose_format_to_hash,
    doc_path_from_sidecar,
    format_from_device_path,
)

# A real sidecar value taken from Serena's library (book 804), trimmed.
REAL_SIDECAR = (
    '{\n  "doc_path": "/mnt/us/calibre-library/Natsu Hyuuga/'
    'Apothecary Diaries (Light Novel), The/02 Apothecary Diaries_ '
    'Volume 02 (Light Novel), The.epub",\n  "summary": {\n    '
    '"status": "unknown",\n    "modified": "2026-06-10"\n  }\n}'
)


class TestDocPathFromSidecar:
    def test_reads_doc_path_from_real_sidecar(self):
        path = doc_path_from_sidecar(REAL_SIDECAR)
        assert path.endswith('(Light Novel), The.epub')
        assert path.startswith('/mnt/us/calibre-library/')

    def test_round_trips_through_format_choice(self):
        # The whole point of parsing the sidecar: it decides the format.
        path = doc_path_from_sidecar(REAL_SIDECAR)
        assert choose_format_to_hash(['EPUB', 'CBZ'], path) == 'EPUB'

    def test_falls_back_to_regex_when_not_valid_json(self):
        # Value wrapped in markup for display - json.loads will refuse it.
        wrapped = f'<div>{REAL_SIDECAR}</div>'
        assert doc_path_from_sidecar(wrapped).endswith('.epub')

    def test_unescapes_escaped_separators(self):
        value = '<p>{"doc_path": "\\/mnt\\/us\\/books\\/Vol 1.cbz"}</p>'
        assert doc_path_from_sidecar(value) == '/mnt/us/books/Vol 1.cbz'

    @pytest.mark.parametrize('value', [
        None, '', '{}', '{"summary": {}}', 'not json at all',
    ])
    def test_returns_none_when_absent(self, value):
        assert doc_path_from_sidecar(value) is None


class TestFormatFromDevicePath:
    def test_extracts_upper_case_extension(self):
        assert format_from_device_path('/mnt/us/books/Some Book.epub') == 'EPUB'

    def test_handles_cbz(self):
        assert format_from_device_path('/mnt/us/manga/Vol 1.cbz') == 'CBZ'

    def test_ignores_dots_in_directory_names(self):
        path = '/mnt/us/calibre-library/A. Author/Book v1.2.azw3'
        assert format_from_device_path(path) == 'AZW3'

    def test_strips_surrounding_whitespace(self):
        assert format_from_device_path('  /mnt/us/x.pdf  ') == 'PDF'

    @pytest.mark.parametrize('value', [None, '', '/mnt/us/no-extension'])
    def test_returns_none_when_undeterminable(self, value):
        assert format_from_device_path(value) is None


class TestChooseFormatToHash:
    def test_single_format_is_chosen(self):
        assert choose_format_to_hash(['CBZ']) == 'CBZ'

    def test_epub_wins_by_default(self):
        assert choose_format_to_hash(['AZW3', 'CBZ', 'EPUB']) == 'EPUB'

    def test_cbz_beats_azw3_when_no_epub(self):
        # The real-world manga case: Calibre holds AZW3 + CBZ, KOReader reads
        # the CBZ. Picking AZW3 here would be the silent-wrong-answer bug.
        assert choose_format_to_hash(['AZW3', 'CBZ']) == 'CBZ'

    def test_device_path_overrides_preference_order(self):
        # Book has both, but the device holds the CBZ - hash what the device
        # actually has, or the hash can never match.
        chosen = choose_format_to_hash(
            ['EPUB', 'CBZ'], device_path='/mnt/us/manga/Vol 51.cbz')
        assert chosen == 'CBZ'

    def test_device_path_ignored_when_book_lacks_that_format(self):
        # Stale sidecar pointing at a format that's since been removed from
        # the library: fall back rather than returning something unusable.
        chosen = choose_format_to_hash(
            ['EPUB'], device_path='/mnt/us/manga/Vol 51.cbz')
        assert chosen == 'EPUB'

    def test_unreadable_formats_are_never_chosen(self):
        assert choose_format_to_hash(['KFX', 'ORIGINAL_EPUB']) is None

    def test_device_path_rescues_a_format_not_in_the_preference_list(self):
        # A sidecar exists only because KOReader opened that file, so its
        # format is readable whether or not the preference list knows it.
        # Real case: book 213, a .doc with a genuine sidecar.
        chosen = choose_format_to_hash(
            ['LIT'], device_path='/mnt/us/calibre-library/Some Book.lit')
        assert chosen == 'LIT'

    def test_unreadable_formats_are_skipped_over(self):
        assert choose_format_to_hash(['KFX', 'CBZ']) == 'CBZ'

    def test_format_names_are_case_insensitive(self):
        assert choose_format_to_hash(['epub', 'cbz']) == 'EPUB'

    @pytest.mark.parametrize('formats', [None, [], [None, '']])
    def test_returns_none_without_usable_formats(self, formats):
        assert choose_format_to_hash(formats) is None

    def test_preference_list_has_no_duplicates(self):
        assert len(KOREADER_FORMATS) == len(set(KOREADER_FORMATS))

    def test_preference_list_is_upper_case(self):
        assert all(fmt == fmt.upper() for fmt in KOREADER_FORMATS)
