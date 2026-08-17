"""Tests for choosing which of a book's formats to hash.

The hash itself is format-agnostic (see test_koreader_hash.py); these cover
only the decision of *which file* gets hashed, which is where a wrong answer
silently produces a valid-looking hash that never matches anything.
"""

import pytest

import json
import os

from koreader_hash import (
    DEVICE_CACHE_FILENAME,
    KOREADER_FORMATS,
    choose_format_to_hash,
    doc_path_from_sidecar,
    format_from_device_path,
    load_device_format_map,
    resolve_format,
)


def make_device_folder(tmp_path, entries, create_files=True):
    """Build a device book folder with a .metadata.calibre cache.

    :param entries: list of (application_id, lpath) pairs
    :param create_files: whether the files named by lpath actually exist
    """
    cache = []
    for book_id, lpath in entries:
        cache.append({'application_id': book_id, 'lpath': lpath,
                      'title': f'Book {book_id}'})
        if create_files:
            target = tmp_path / lpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'not really a book')
    (tmp_path / DEVICE_CACHE_FILENAME).write_text(json.dumps(cache))
    return str(tmp_path)

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


class TestLoadDeviceFormatMap:
    """Reading which format calibre actually put on the device."""

    def test_maps_book_ids_to_the_format_that_was_sent(self, tmp_path):
        folder = make_device_folder(tmp_path, [
            (182, 'Night Watch/2.0 Day Watch.pdf'),
            (776, 'Legend of Exorcism/1.0 Legend of Exorcism.epub'),
            (870, 'manga/Sasori to Otome.cbz'),
        ])
        assert load_device_format_map(folder) == {
            182: 'PDF', 776: 'EPUB', 870: 'CBZ'}

    def test_entries_whose_file_is_missing_are_skipped(self, tmp_path):
        # Real case: Serena's ~/Documents/books and SD card both carried
        # caches from an older transfer whose lpaths resolved to nothing.
        # A stale entry is worse than no entry.
        folder = make_device_folder(
            tmp_path, [(182, 'Night Watch/2.0 Day Watch.pdf')],
            create_files=False)
        assert load_device_format_map(folder) == {}

    def test_a_partly_stale_cache_still_yields_its_live_entries(self, tmp_path):
        folder = make_device_folder(tmp_path, [
            (182, 'gone/Day Watch.pdf'),
            (776, 'here/Legend of Exorcism.epub'),
        ], create_files=False)
        (tmp_path / 'here').mkdir()
        (tmp_path / 'here' / 'Legend of Exorcism.epub').write_bytes(b'x')
        assert load_device_format_map(folder) == {776: 'EPUB'}

    @pytest.mark.parametrize('value', [None, '', '   '])
    def test_no_folder_configured_yields_an_empty_map(self, value):
        assert load_device_format_map(value) == {}

    def test_missing_cache_file_yields_an_empty_map(self, tmp_path):
        assert load_device_format_map(str(tmp_path)) == {}

    def test_malformed_cache_yields_an_empty_map(self, tmp_path):
        (tmp_path / DEVICE_CACHE_FILENAME).write_text('{ not json')
        assert load_device_format_map(str(tmp_path)) == {}

    def test_cache_that_is_not_a_list_yields_an_empty_map(self, tmp_path):
        (tmp_path / DEVICE_CACHE_FILENAME).write_text('{"lpath": "x.epub"}')
        assert load_device_format_map(str(tmp_path)) == {}

    def test_entries_missing_required_fields_are_skipped(self, tmp_path):
        (tmp_path / 'a.epub').write_bytes(b'x')
        (tmp_path / DEVICE_CACHE_FILENAME).write_text(json.dumps([
            {'lpath': 'a.epub'},                       # no application_id
            {'application_id': 5},                     # no lpath
            'not even a dict',
            {'application_id': 7, 'lpath': 'a.epub'},  # the good one
        ]))
        assert load_device_format_map(str(tmp_path)) == {7: 'EPUB'}

    def test_extensionless_device_file_is_skipped(self, tmp_path):
        (tmp_path / 'no-extension').write_bytes(b'x')
        (tmp_path / DEVICE_CACHE_FILENAME).write_text(json.dumps([
            {'application_id': 1, 'lpath': 'no-extension'}]))
        assert load_device_format_map(str(tmp_path)) == {}


class TestResolveFormat:
    """Precedence between the device cache, a sidecar, and the fallback list."""

    def test_device_cache_wins_over_the_preference_list(self):
        # The live failure this bug was raised for: books 182 and 184 hold
        # EPUB and PDF, calibre sent the PDF, the preference list said EPUB.
        assert resolve_format(['EPUB', 'PDF'], device_format='PDF') == (
            'PDF', 'device cache')

    def test_device_cache_wins_over_the_sidecar(self):
        # If they disagree, what's on the device now beats what a reader
        # recorded at some point in the past.
        chosen, source = resolve_format(
            ['EPUB', 'PDF'], device_format='PDF',
            device_path='/mnt/us/Book.epub')
        assert (chosen, source) == ('PDF', 'device cache')

    def test_sidecar_used_when_the_book_is_not_on_the_device(self):
        chosen, source = resolve_format(
            ['EPUB', 'CBZ'], device_format=None,
            device_path='/mnt/us/manga/Vol 1.cbz')
        assert (chosen, source) == ('CBZ', 'sidecar')

    def test_preference_list_used_when_there_is_no_other_evidence(self):
        assert resolve_format(['AZW3', 'EPUB']) == ('EPUB', 'preference')

    def test_device_format_ignored_when_the_library_lacks_it(self):
        # The device holds a format that has since been removed from the
        # library: fall through rather than name something unhashable.
        assert resolve_format(['EPUB'], device_format='PDF') == (
            'EPUB', 'preference')

    def test_device_format_is_case_insensitive(self):
        assert resolve_format(['epub', 'pdf'], device_format='pdf') == (
            'PDF', 'device cache')

    def test_no_usable_format_reports_no_source(self):
        assert resolve_format(['LIT', 'LRF']) == (None, None)

    def test_choose_format_to_hash_still_returns_just_the_format(self):
        # The thin wrapper the earlier tests use must keep working.
        assert choose_format_to_hash(['EPUB', 'PDF'], device_format='PDF') == 'PDF'
        assert choose_format_to_hash(['AZW3', 'CBZ']) == 'CBZ'


class TestEndToEndResolution:
    """The device cache and the resolver working together, as action.py uses
    them: read the cache once, then resolve per book."""

    def test_the_182_and_184_case_resolves_correctly(self, tmp_path):
        folder = make_device_folder(tmp_path, [
            (182, 'Night Watch/2.0 Day Watch.pdf'),
            (184, 'Night Watch/4.0 Last Watch, The.epub'),
        ])
        device_formats = load_device_format_map(folder)

        # 182 was sent as PDF despite holding an EPUB - hash the PDF.
        assert resolve_format(['EPUB', 'PDF'],
                              device_format=device_formats.get(182)) == (
            'PDF', 'device cache')
        # 184 was sent as EPUB after the format map was corrected.
        assert resolve_format(['EPUB', 'PDF'],
                              device_format=device_formats.get(184)) == (
            'EPUB', 'device cache')

    def test_a_book_not_on_the_device_falls_back(self, tmp_path):
        folder = make_device_folder(tmp_path, [(182, 'a/Day Watch.pdf')])
        device_formats = load_device_format_map(folder)
        assert resolve_format(['EPUB', 'PDF'],
                              device_format=device_formats.get(999)) == (
            'EPUB', 'preference')
