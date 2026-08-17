#!/usr/bin/env python3

"""KOReader's partial-content document hash.

KOReader identifies documents by hashing small chunks of file content read
at a fixed, deterministic set of offsets, rather than the whole file - this
keeps hashing fast even for very large books. This module replicates that
algorithm so Calibre can compute the same hash locally (see GitHub issue
kyxap/koreader-calibre-plugin#150), instead of only ever learning it from a
device that has already synced the book via genuine KOReader software.

Verified directly against KOReader's own reference implementation,
util.partialMD5() in koreader/koreader's frontend/util.lua - including its
i = -1 special case, which relies on a 32-bit left-shift overflow
(1024 << -2, masked to a 5-bit shift count of 30) landing on exactly 0.
Also cross-checked against a from-scratch reimplementation of the same
algorithm: franssjz/cpr-vcodex's lib/KOReaderSync/KOReaderDocumentId.cpp.
"""

import hashlib
import json
import os
import re

CHUNK_SIZE = 1024
OFFSET_COUNT = 12

# Calibre writes this into the root of a device's book folder, recording what
# it put there and which library book each file came from.
DEVICE_CACHE_FILENAME = '.metadata.calibre'

# Formats KOReader can open, in the order we prefer to hash them when a book
# has more than one and there is no better signal available. EPUB first
# because it is the most common and the format KOReader handles best; CBZ
# next, because comics are the usual reason a book has no EPUB at all.
#
# The hash itself is format-agnostic - partialMD5() reads bytes at fixed
# offsets and knows nothing about the container - so this list only decides
# *which file* to hash, never whether hashing is possible at all.
KOREADER_FORMATS = (
    'EPUB', 'CBZ', 'CBR', 'FB2', 'MOBI', 'AZW3', 'AZW', 'PRC',
    'PDF', 'DJVU', 'DJV', 'CHM', 'RTF', 'HTMLZ', 'DOC', 'TXT', 'ZIP',
)


def doc_path_from_sidecar(sidecar_value):
    """Pull `doc_path` out of a stored KOReader metadata sidecar.

    The sidecar column holds the sidecar's contents, which record the book's
    path *on the device* - the one piece of direct evidence about which format
    that device actually holds. The column is free-form text (JSON, sometimes
    escaped or wrapped in markup for display), so this parses defensively and
    gives up quietly rather than raising.

    :param sidecar_value: stored sidecar column value, or None
    :return: the recorded device path, or None if it can't be determined
    """
    if not sidecar_value:
        return None

    text = str(sidecar_value)
    try:
        return json.loads(text).get('doc_path') or None
    except (ValueError, AttributeError):
        pass

    # Fall back to reading the field directly, which survives the value having
    # been escaped or wrapped in markup somewhere along the way.
    match = re.search(r'"doc_path"\s*:\s*"(.*?)(?<!\\)"', text)
    if not match:
        return None

    return match.group(1).replace('\\/', '/').replace('\\\\', '\\') or None


def format_from_device_path(device_path):
    """Extract a Calibre format name from a path recorded by a device.

    KOReader's metadata sidecar records the full path of the file as it exists
    on the device, e.g. `/mnt/us/library/Some Book.cbz`. Its extension is the
    only direct evidence of which format the device actually holds, which is
    the format whose hash will match what the device reports.

    :param device_path: a path string, or None
    :return: upper-case format name (e.g. 'CBZ'), or None if not determinable
    """
    if not device_path:
        return None

    extension = os.path.splitext(str(device_path).strip())[1]
    if not extension:
        return None

    return extension.lstrip('.').upper() or None


def load_device_format_map(device_folder):
    """Read which format each book was actually sent to a device as.

    Calibre writes a `.metadata.calibre` cache into the root of a device's
    book folder: a JSON array with one entry per book, carrying the library
    book id (`application_id`) and the file's path on the device (`lpath`).
    That path's extension is the only direct evidence of which format the
    device holds, and calibre only ever puts one format of a book on a device,
    so the mapping is one book to one format.

    Entries whose file is no longer at the recorded path are skipped: caches
    left behind by an older transfer can name files that no longer exist, and
    a stale entry is worse than no entry.

    Any problem reading the cache yields an empty map rather than an error -
    a missing or malformed cache simply means falling back to the other
    resolution routes.

    :param device_folder: path to the device's book folder, or None
    :return: dict of {book_id: format name}, possibly empty
    """
    if not device_folder:
        return {}

    cache_path = os.path.join(device_folder, DEVICE_CACHE_FILENAME)
    try:
        with open(cache_path, 'rb') as cache_file:
            entries = json.load(cache_file)
    except (OSError, ValueError):
        return {}

    if not isinstance(entries, list):
        return {}

    formats = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        book_id = entry.get('application_id')
        lpath = entry.get('lpath')
        if book_id is None or not lpath:
            continue
        if not os.path.exists(os.path.join(device_folder, lpath)):
            continue
        book_format = format_from_device_path(lpath)
        if book_format:
            formats[book_id] = book_format

    return formats


def resolve_format(available_formats, device_format=None, device_path=None):
    """Pick which of a book's formats to hash, and say why.

    Resolution order, first hit wins:

    1. `device_format` - what calibre actually sent, from the device's
       `.metadata.calibre` cache. The strongest evidence available.
    2. `device_path` - the format named by a KOReader sidecar's `doc_path`.
       A sidecar exists only because KOReader opened that file, so its format
       is readable by definition, and is trusted even when the preference list
       below doesn't contain it.
    3. The first entry of KOREADER_FORMATS the book has.

    Hashing a format the device doesn't hold produces a valid-looking hash
    that can never match anything - worse than no hash at all, because the
    column then looks populated.

    :param available_formats: iterable of Calibre format names for the book
    :param device_format: format the device holds, from the device cache
    :param device_path: path recorded by a sidecar, if known
    :return: (format name, source) - source is 'device cache', 'sidecar' or
        'preference'; both are None if nothing KOReader reads is available
    """
    available = {str(fmt).upper() for fmt in available_formats or () if fmt}
    if not available:
        return None, None

    if device_format and str(device_format).upper() in available:
        return str(device_format).upper(), 'device cache'

    sidecar_format = format_from_device_path(device_path)
    if sidecar_format and sidecar_format in available:
        return sidecar_format, 'sidecar'

    for fmt in KOREADER_FORMATS:
        if fmt in available:
            return fmt, 'preference'

    return None, None


def choose_format_to_hash(available_formats, device_path=None,
                          device_format=None):
    """Pick which of a book's formats to hash - see resolve_format().

    :return: chosen format name, or None if the book has nothing KOReader reads
    """
    return resolve_format(
        available_formats, device_format=device_format,
        device_path=device_path)[0]


def _offset_for_index(i):
    """Byte offset for index i: 0 for i == -1, else 1024 << (2*i)."""
    if i < 0:
        return 0
    return CHUNK_SIZE << (2 * i)


def calculate_koreader_md5(file_path):
    """Calculate KOReader's partial-content MD5 hash for a file.

    Reads up to 1024 bytes at each offset in the sequence 0, 1KB, 4KB, 16KB,
    ... up to 1GB (any offset at or beyond the file's size is skipped), and
    returns the MD5 hex digest of the concatenated chunks - matching the
    hash KOReader itself would compute and send to a ProgressSync server.

    :param file_path: path to the file to hash
    :return: 32-character lowercase hex digest, or None if the file can't be read
    """
    try:
        file_size = os.path.getsize(file_path)
    except OSError:
        return None

    md5 = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            for i in range(-1, OFFSET_COUNT - 1):
                offset = _offset_for_index(i)
                if offset >= file_size:
                    continue
                f.seek(offset)
                chunk = f.read(CHUNK_SIZE)
                if chunk:
                    md5.update(chunk)
    except OSError:
        return None

    return md5.hexdigest()
