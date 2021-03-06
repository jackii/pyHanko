"""
Utility to read PDF files.
Contains code from the PyPDF2 project; see :ref:`here <pypdf2-license>`
for the original license.

The implementation was tweaked with the express purpose of facilitating
historical inspection and auditing of PDF files with multiple revisions
through incremental updates.
This comes at a cost, and future iterations of this module may offer more
flexibility in terms of the level of detail with which file size is scrutinised.
"""

import struct
import os
import re
from collections import defaultdict
from io import BytesIO
from itertools import chain
from typing import Set, List, Optional, Union, Tuple

from . import generic, misc
from .misc import PdfReadError
from .crypt import (
    SecurityHandler, StandardSecurityHandler,
    EnvelopeKeyDecrypter, PubKeySecurityHandler,
)

import logging

from .rw_common import PdfHandler

logger = logging.getLogger(__name__)


__all__ = ['PdfFileReader', 'HistoricalResolver']

header_regex = re.compile(b'%PDF-(\\d).(\\d)')
catalog_version_regex = re.compile(r'/(\d).(\d)')

# General remark:
# PyPDF2 parses all files backwards.
# This means that "next" and "previous" usually mean the opposite of what one
#  might expect.


def read_next_end_line(stream):
    def _build():
        while True:
            # Prevent infinite loops in malformed PDFs
            if stream.tell() == 0:
                raise PdfReadError("Could not read malformed PDF file")
            x = stream.read(1)
            if stream.tell() < 2:
                raise PdfReadError("EOL marker not found")
            stream.seek(-2, os.SEEK_CUR)
            if x == b'\n' or x == b'\r':
                break
            yield ord(x)
        crlf = False
        while x == b'\n' or x == b'\r':
            x = stream.read(1)
            if x == b'\n' or x == b'\r':  # account for CR+LF
                stream.seek(-1, os.SEEK_CUR)
                crlf = True
            if stream.tell() < 2:
                raise PdfReadError("EOL marker not found")
            stream.seek(-2, os.SEEK_CUR)
        # if using CR+LF, go back 2 bytes, else 1
        stream.seek(2 if crlf else 1, os.SEEK_CUR)
    return bytes(reversed(tuple(_build())))


class XRefCache:

    def __init__(self, reader):
        super().__init__()
        self.reader = reader
        self.xref_sections = 0
        self.xref_locations = []
        self.in_obj_stream = {}
        self.standard_xrefs = {}
        # keep track of the xref section that last changed an entry
        #  (needed for some validation workflows)
        self.last_change = {}
        # making this a dict doesn't make much sense
        self.history = defaultdict(list)
        self._current_section_ids = set()
        self._current_section_freed = set()
        self._refs_by_section = []
        self._freed_by_section = []
        self._generations = {}
        self._previous_expected_free = {}
        self.xref_container_info = []

        self._obj_streams_by_revision = defaultdict(set)

    def _next_section(self):
        self.xref_sections += 1
        self._refs_by_section.append(self._current_section_ids)
        self._current_section_ids = set()
        self._freed_by_section.append(self._current_section_freed)
        self._current_section_freed = set()

    def used_later(self, idnum, generation) -> bool:
        # We move backwards through the xrefs, don't replace any.
        try:
            return generation in self._generations[idnum]
        except KeyError:
            return False

    def free_ref(self, idnum, next_generation):
        if not idnum:
            return
        # treat this as setting idnum, next_generation-1 to null
        prev_generation = (next_generation - 1) if next_generation else 0xffff
        self.standard_xrefs[(prev_generation, idnum)] = generic.NullObject()
        null_ref = generic.Reference(idnum, prev_generation)
        self._current_section_freed.add(null_ref)
        self._current_section_ids.add(null_ref)
        try:
            # check for sneaky reuse: does prev_generation (or any lower one)
            # still occur later in the file?
            conflicting_gen = next(
                gen for gen in self._generations[idnum]
                if gen <= prev_generation
            )
            raise PdfReadError(
                f"Generation {conflicting_gen} of object {idnum} occurs "
                f"after generation {prev_generation} was freed."
            )
        except KeyError:
            self._generations[idnum] = {prev_generation}
        except StopIteration:
            self._generations[idnum].add(prev_generation)

        if idnum not in self.last_change:
            # this revision is the last change
            self.last_change[idnum] = self.xref_sections
        try:
            # remove from expected free dict
            expected_generation = self._previous_expected_free.pop(idnum)
            if expected_generation != next_generation:
                raise PdfReadError(
                    f"Encountered freeing instruction with next generation "
                    f"{next_generation} of object ID {idnum}, but next use of "
                    f"this object has generation {expected_generation}."
                )
        except KeyError:
            # this object might simply not have been reclaimed
            pass

    def put_ref(self, idnum, generation, start):
        if idnum in self._previous_expected_free:
            raise PdfReadError(
                f"Generation {generation} of object {idnum} was "
                "never freed, but reused later."
            )
        if generation > 0xffff:  # pragma: nocover
            raise PdfReadError(
                f"Illegal generation {generation} for object ID {idnum}."
            )
        elif generation > 0:
            # we must encounter a freeing instruction further back in the file
            self._previous_expected_free[idnum] = generation
        if not self.used_later(idnum, generation):
            self.standard_xrefs[(generation, idnum)] = start
            self.last_change[idnum] = self.xref_sections
            self._generations[idnum] = {generation}
        else:
            self._generations[idnum].add(generation)
        self.history[(generation, idnum)].append((self.xref_sections, start))
        self._current_section_ids.add(
            generic.Reference(idnum, generation, self.reader)
        )

    def put_obj_stream_ref(self, idnum, obj_stream_num, obj_stream_ix):
        self._obj_streams_by_revision[self.xref_sections].add(
            generic.Reference(obj_stream_num, 0, self.reader)
        )
        marker = (obj_stream_num, obj_stream_ix)
        if not self.used_later(idnum, 0):
            self.in_obj_stream[idnum] = marker
            self.last_change[idnum] = self.xref_sections
            self._generations[idnum] = {0}

        self.history[(0, idnum)].append((self.xref_sections, marker))
        self._current_section_ids.add(generic.Reference(idnum, 0, self.reader))

    @property
    def total_revisions(self):
        return self.xref_sections

    def get_last_change(self, idnum):
        return self.xref_sections - 1 - self.last_change[idnum]

    def object_streams_used_in(self, revision):
        return self._obj_streams_by_revision[self.xref_sections - 1 - revision]

    def get_introducing_revision(self, ref: generic.Reference):
        ref_hist = self.history[(ref.generation, ref.idnum)]
        section, _ = ref_hist[len(ref_hist) - 1]
        return self.xref_sections - 1 - section

    def get_xref_container_info(self, revision):
        return self.xref_container_info[self.xref_sections - 1 - revision]

    def explicit_refs_in_revision(self, revision) -> Set[generic.Reference]:
        """
        Look up the object refs for all objects explicitly added or overwritten
        in a given revision.

        :param revision:
            A revision number. The oldest revision is zero.
        :return:
            A set of Reference objects.
        """
        rbs = self._refs_by_section
        return rbs[self.xref_sections - 1 - revision]

    def refs_freed_in_revision(self, revision) -> Set[generic.Reference]:
        """
        Look up the object refs for all objects explicitly freed
        in a given revision.

        :param revision:
            A revision number. The oldest revision is zero.
        :return:
            A set of Reference objects.
        """
        fbs = self._freed_by_section
        return fbs[self.xref_sections - 1 - revision]

    def get_startxref_for_revision(self, revision):
        """
        Look up the location of the XRef table/stream associated with a specific
        revision, as indicated by startxref or /Prev.

        :param revision:
            A revision number. The oldest revision is zero.
        :return:
            An integer pointer
        """
        return self.xref_locations[self.xref_sections - 1 - revision]

    def get_historical_ref(self, ref, revision):
        """
        Look up the location of the historical value of an object.

        :param ref:
            An object reference.
        :param revision:
            A revision number. The oldest revision is zero.
        :return:
            An integer offset, or a pair of integers indicating an object
            in an object stream.
        """
        max_index = self.xref_sections - 1
        ix = (ref.generation, ref.idnum)

        # Remember: in the history record, revisions are numbered backwards.
        # (i.e. the first item is the most recent, and the last one is
        # the oldest)
        # Hence, the first match that corresponds to a point in time at or
        # before 'revision' is the one we want
        for rev_index, marker in self.history[ix]:
            if revision >= max_index - rev_index:
                return marker
        raise PdfReadError(
            f'Could not find object ({ref.idnum} {ref.generation}) '
            f'in history at revision {revision}'
        )

    def __getitem__(self, ref):
        if ref.generation == 0 and \
                ref.idnum in self.in_obj_stream:
            return self.in_obj_stream[ref.idnum]
        else:
            try:
                return self.standard_xrefs[(ref.generation, ref.idnum)]
            except KeyError:
                raise PdfReadError("Could not find object.")

    def read_xref_table(self):
        stream = self.reader.stream
        misc.read_non_whitespace(stream)
        stream.seek(-1, os.SEEK_CUR)
        while True:
            num = generic.NumberObject.read_from_stream(stream)
            misc.read_non_whitespace(stream)
            stream.seek(-1, os.SEEK_CUR)
            size = generic.NumberObject.read_from_stream(stream)
            misc.read_non_whitespace(stream)
            stream.seek(-1, os.SEEK_CUR)
            for cnt in range(0, size):
                line = stream.read(20)

                # It's very clear in section 3.4.3 of the PDF spec
                # that all cross-reference table lines are a fixed
                # 20 bytes (as of PDF 1.7). However, some files have
                # 21-byte entries (or more) due to the use of \r\n
                # (CRLF) EOL's. Detect that case, and adjust the line
                # until it does not begin with a \r (CR) or \n (LF).
                while line[0] in b"\x0D\x0A":
                    stream.seek(-20 + 1, os.SEEK_CUR)
                    line = stream.read(20)

                # On the other hand, some malformed PDF files
                # use a single character EOL without a preceding
                # space.  Detect that case, and seek the stream
                # back one character.  (0-9 means we've bled into
                # the next xref entry, t means we've bled into the
                # text "trailer"):
                if line[-1] in b"0123456789t":
                    stream.seek(-1, os.SEEK_CUR)

                offset, generation, marker = line[:18].split(b" ")
                if marker == b'n':
                    self.put_ref(num, int(generation), int(offset))
                elif marker == b'f':
                    self.free_ref(num, int(generation))
                num += 1
            misc.read_non_whitespace(stream)
            stream.seek(-1, os.SEEK_CUR)
            trailertag = stream.read(7)
            if trailertag != b"trailer":
                # more xrefs!
                stream.seek(-7, os.SEEK_CUR)
            else:
                break
        misc.read_non_whitespace(stream)
        stream.seek(-1, os.SEEK_CUR)

        self._next_section()

    def read_xref_stream(self, xrefstream):
        stream_data = BytesIO(xrefstream.data)
        # Index pairs specify the subsections in the dictionary. If
        # none create one subsection that spans everything.
        idx_pairs = xrefstream.get("/Index", [0, xrefstream.get("/Size")])
        entry_sizes = xrefstream.get("/W")

        def get_entry(ix):
            # Reads the correct number of bytes for each entry. See the
            # discussion of the W parameter in PDF spec table 17.
            if entry_sizes[ix] > 0:
                d = stream_data.read(entry_sizes[ix])
                return convert_to_int(d, entry_sizes[ix])

            # PDF Spec Table 17: A value of zero for an element in the
            # W array indicates...the default value shall be used
            if ix == 0:
                return 1  # First value defaults to 1
            else:
                return 0

        # Iterate through each subsection
        last_end = 0
        for start, size in misc.pair_iter(idx_pairs):
            # The subsections must increase
            assert start >= last_end
            last_end = start + size
            for num in range(start, start + size):
                # The first entry is the type
                xref_type = get_entry(0)
                # The rest of the elements depend on the xref_type
                if xref_type == 1:
                    # objects that are in use but are not compressed
                    byte_offset = get_entry(1)
                    generation = get_entry(2)
                    self.put_ref(num, generation, byte_offset)
                elif xref_type == 2:
                    # compressed objects
                    objstr_num = get_entry(1)
                    objstr_idx = get_entry(2)
                    self.put_obj_stream_ref(num, objstr_num, objstr_idx)
                elif xref_type == 0:
                    # freed object
                    # we ignore the linked list aspect anyway, so discard first
                    get_entry(1)
                    next_generation = get_entry(2)
                    self.free_ref(num, next_generation)
                else:
                    # unknown type (=> ignore).
                    get_entry(1)
                    get_entry(2)

        self._next_section()


def read_object_header(stream, strict):
    # Should never be necessary to read out whitespace, since the
    # cross-reference table should put us in the right spot to read the
    # object header.  In reality... some files have stupid cross reference
    # tables that are off by whitespace bytes.
    extra = False
    misc.skip_over_comment(stream)
    extra |= misc.skip_over_whitespace(stream)
    stream.seek(-1, os.SEEK_CUR)
    idnum = misc.read_until_whitespace(stream)
    extra |= misc.skip_over_whitespace(stream)
    stream.seek(-1, os.SEEK_CUR)
    generation = misc.read_until_whitespace(stream)
    stream.read(3)
    misc.read_non_whitespace(stream, seek_back=True)

    if extra and strict:
        logger.warning(
            f"Superfluous whitespace found in object header "
            f"{idnum} {generation}"
        )
    return int(idnum), int(generation)


def process_data_at_eof(stream) -> int:
    """
    Auxiliary function that reads backwards from the current position
    in a stream to find the EOF marker and startxref value
    :param stream:
        A stream to read from
    :return:
        The value of the startxref pointer, if found.
        Otherwise a PdfReadError is raised.
    """

    # offset of last 1024 bytes of stream
    last_1k = stream.tell() - 1024 + 1
    line = b''
    while line[:5] != b"%%EOF":
        if stream.tell() < last_1k:
            raise PdfReadError("EOF marker not found")
        line = read_next_end_line(stream)

    # find startxref entry - the location of the xref table
    line = read_next_end_line(stream)
    try:
        startxref = int(line)
    except ValueError:
        # 'startxref' may be on the same line as the location
        if not line.startswith(b"startxref"):
            raise PdfReadError("startxref not found")
        startxref = int(line[9:].strip())
        logger.warning("startxref on same line as offset")
    else:
        line = read_next_end_line(stream)
        if line[:9] != b"startxref":
            raise PdfReadError("startxref not found")

    return startxref


class TrailerDictionary(generic.PdfObject):
    """
    The standard mandates that each trailer shall contain
    at least all keys used in the preceding trailer, even if unmodified.
    Of course, we cannot trust documents to actually follow this rule, so
    this class implements fallbacks.
    """

    def __init__(self):
        # trailer revisions, numbered backwards (i.e. in processing order)
        # The element at index 0 is the most recent one.
        self._trailer_revisions: List[generic.DictionaryObject] = []
        self._new_changes = generic.DictionaryObject()

    def add_trailer_revision(self, trailer_dict: generic.DictionaryObject):
        self._trailer_revisions.append(trailer_dict)

    def __getitem__(self, item):
        # the decrypt parameter doesn't matter, get_object() decrypts
        # as necessary.
        return self.raw_get(item).get_object()

    def raw_get(self, key, decrypt=True, revision=None):
        revisions = self._trailer_revisions
        if revision is None:
            try:
                return self._new_changes.raw_get(key, decrypt)
            except KeyError:
                pass
        else:
            # xref sections are numbered backwards
            section = len(revisions) - 1 - revision
            revisions = revisions[section:]

        for revision in revisions:
            try:
                return revision.raw_get(key, decrypt)
            except KeyError:
                continue
        raise KeyError(key)

    def __setitem__(self, item, value):
        self._new_changes[item] = value

    def __delitem__(self, item):
        try:
            # deleting stuff from _new_changes should be OK.
            del self._new_changes[item]
        except KeyError:
            raise misc.PdfError(
                "Cannot remove existing entries from trailer dictionary, only "
                "update them."
            )

    def flatten(self, revision=None) -> generic.DictionaryObject:
        relevant_revisions = self._trailer_revisions
        if revision is not None:
            relevant_revisions = relevant_revisions[-revision-1:]
        trailer = generic.DictionaryObject({
            k: v for revision in reversed(relevant_revisions)
            for k, v in revision.items()
        })
        if revision is None:
            trailer.update(self._new_changes)

        # ensure that the trailer isn't polluted using stream
        # compression / XRef parameters
        trailer.pop('/Length', None)
        trailer.pop('/Filter', None)
        trailer.pop('/DecodeParms', None)
        trailer.pop('/W', None)
        trailer.pop('/Type', None)
        trailer.pop('/Index', None)
        return trailer

    def __contains__(self, item):
        if item in self._new_changes:
            return True
        return any(item in revision for revision in self._trailer_revisions)

    def keys(self):
        return frozenset(chain(self._new_changes, *self._trailer_revisions))

    def __iter__(self):
        return iter(self.keys())

    def items(self):
        return self.flatten().items()

    def write_to_stream(self, stream, handler=None, container_ref=None):
        return self.flatten().write_to_stream(stream, handler, container_ref)


class PdfFileReader(PdfHandler):
    """Class implementing functionality to read a PDF file and cache
    certain data about it."""

    last_startxref = None
    has_xref_stream = False

    def __init__(self, stream, strict=True):
        """
        Initializes a PdfFileReader object.  This operation can take some time,
        as the PDF stream's cross-reference tables are read into memory.

        :param stream: A File object or an object that supports the standard
            read and seek methods similar to a File object.
        :param bool strict: Determines whether user should be warned of all
            problems and also causes some correctable problems to be fatal.
            Defaults to ``True``.
        """
        self.security_handler: Optional[SecurityHandler] = None
        self.strict = strict
        self.resolved_objects = {}
        self.input_version = None
        self.xrefs = XRefCache(self)
        self._historical_resolver_cache = {}
        self.stream = stream
        self.read()
        # override version if necessary
        try:
            # grab version info *without* triggering crypto
            root_ref = self.trailer.raw_get('/Root')
            root = self.get_object(root_ref, never_decrypt=True)
            version = root.raw_get('/Version')
            # not sure if anyone would be crazy enough to make this an indirect
            # reference, but in theory it's possible
            if isinstance(version, generic.IndirectObject):
                version = self.get_object(version.reference, never_decrypt=True)
            m = catalog_version_regex.match(str(version))
            if m is not None:
                major = int(m.group(1))
                minor = int(m.group(2))
                self.input_version = (major, minor)
        except KeyError:
            pass

        encrypt_dict = self._get_encryption_params()
        if encrypt_dict is not None:
            self.security_handler = SecurityHandler.build(encrypt_dict)

        self._embedded_signatures = None

    def _get_object_from_stream(self, idnum, stmnum, idx):
        # indirect reference to object in object stream
        # read the entire object stream into memory
        stream_ref = generic.Reference(stmnum, 0, self)
        stream = stream_ref.get_object()
        assert isinstance(stream, generic.StreamObject)
        # This is an xref to a stream, so its type better be a stream
        assert stream['/Type'] == '/ObjStm'
        # /N is the number of indirect objects in the stream
        assert idx < stream['/N']
        stream_data = BytesIO(stream.data)
        first_object = stream['/First']
        for i in range(stream['/N']):
            misc.read_non_whitespace(stream_data, seek_back=True)
            objnum = generic.NumberObject.read_from_stream(stream_data)
            misc.read_non_whitespace(stream_data, seek_back=True)
            offset = generic.NumberObject.read_from_stream(stream_data)
            misc.read_non_whitespace(stream_data, seek_back=True)
            if objnum != idnum:
                # We're only interested in one object
                continue
            if self.strict and idx != i:
                raise PdfReadError("Object is in wrong index.")
            obj_start = first_object + offset
            stream_data.seek(obj_start)
            try:
                obj = generic.read_object(
                    stream_data, generic.Reference(idnum, 0, self),
                )
            except misc.PdfStreamError as e:
                # Stream object cannot be read. Normally, a critical error, but
                # Adobe Reader doesn't complain, so continue (in strict mode?)
                logger.warning(
                    f"Invalid stream (index {i}) within object {idnum} 0: {e}"
                )

                if self.strict:
                    raise PdfReadError("Can't read object stream: %s" % e)
                # Replace with null. Hopefully it's nothing important.
                obj = generic.NullObject()
            generic.read_non_whitespace(
                stream_data, seek_back=True, allow_eof=True
            )
            return obj

        if self.strict:
            raise PdfReadError("This is a fatal error in strict mode.")
        return generic.NullObject()

    def _get_encryption_params(self) -> Optional[generic.DictionaryObject]:
        try:
            encrypt_ref = self.trailer.raw_get('/Encrypt')
        except KeyError:
            return
        if isinstance(encrypt_ref, generic.IndirectObject):
            return self.get_object(encrypt_ref.reference, never_decrypt=True)
        else:
            return encrypt_ref

    @property
    def trailer_view(self) -> generic.DictionaryObject:
        return self.trailer.flatten()

    @property
    def root_ref(self) -> generic.Reference:
        return self.trailer.raw_get('/Root', decrypt=False).reference

    @property
    def document_id(self) -> Tuple[bytes, bytes]:
        id_arr = self.trailer['/ID']
        return id_arr[0].original_bytes, id_arr[1].original_bytes

    def get_historical_root(self, revision: int):
        """
        Get the document catalog for a specific revision.

        :param revision:
            The revision to query, the oldest one being `0`.
        :return:
            The value of the document catalog dictionary for that revision.
        """
        ref = self.trailer.raw_get('/Root', revision=revision)
        marker = self.xrefs.get_historical_ref(ref, revision)
        return self._read_object(ref, marker)

    @property
    def total_revisions(self) -> int:
        """
        :return:
            The total number of revisions made to this file.
        """
        return self.xrefs.total_revisions

    def get_object(self, ref, revision=None, never_decrypt=False,
                   transparent_decrypt=True):
        """
        Read an object from the input stream.

        :param ref:
            :class:`~.generic.Reference` to the object.
        :param revision:
            Revision number, to return the historical value of a reference.
            This always bypasses the cache.
            The oldest revision is numbered `0`.
            See also :class:`.HistoricalResolver`.
        :param never_decrypt:
            Skip decryption step (only needed for parsing ``/Encrypt``)
        :param transparent_decrypt:
            If ``True``, all encrypted objects are transparently decrypted by
            default (in the sense that a user of the API in a PyPDF2 compatible
            way would only "see" decrypted objects).
            If ``False``, this method may return a proxy object that still
            allows access to the "original".

            .. danger::
                The encryption parameters are considered internal,
                undocumented API, and subject to change without notice.
        :return:
            A :class:`~.generic.PdfObject`.
        :raises PdfReadError:
            Raised if there is an issue reading the object from the file.
        """
        if revision is None:
            obj = self.cache_get_indirect_object(ref.generation, ref.idnum)
            if obj is None:
                obj = self._read_object(ref, self.xrefs[ref],
                                        never_decrypt=never_decrypt)
                # cache before (potential) decrypting
                self.cache_indirect_object(ref.generation, ref.idnum, obj)
        else:
            # never cache historical refs
            marker = self.xrefs.get_historical_ref(ref, revision)
            obj = self._read_object(ref, marker, never_decrypt=never_decrypt)

        if transparent_decrypt and \
                isinstance(obj, generic.DecryptedObjectProxy):
            obj = obj.decrypted

        return obj

    def _read_object(self, ref, marker, never_decrypt=False):
        if marker is None:
            raise PdfReadError(
                f"Reference {ref} has been freed."
            )
        elif isinstance(marker, tuple):
            # object in object stream
            (obj_stream_num, obj_stream_ix) = marker
            obj = self._get_object_from_stream(
                ref.idnum, obj_stream_num, obj_stream_ix
            )
            return obj
        else:
            obj_start = marker
            # standard indirect object
            self.stream.seek(obj_start)
            idnum, generation = read_object_header(
                self.stream, strict=self.strict
            )
            if idnum != ref.idnum or generation != ref.generation:
                raise PdfReadError(
                    f"Expected object ID ({ref.idnum} {ref.generation}) "
                    f"does not match actual ({idnum} {generation})."
                )
            retval = generic.read_object(
                self.stream, generic.Reference(idnum, generation, self)
            )
            generic.read_non_whitespace(self.stream, seek_back=True)
            obj_data_end = self.stream.tell() - 1
            endobj = self.stream.read(6)
            if endobj != b'endobj':
                if self.strict:  # pragma: nocover
                    raise PdfReadError(
                        f'Expected endobj marker at position {obj_data_end} '
                        f'but found {repr(endobj)}'
                    )
            else:
                generic.read_non_whitespace(self.stream, seek_back=True)

            # override encryption is used for the /Encrypt dictionary
            if not never_decrypt and self.encrypted:
                sh: SecurityHandler = self.security_handler
                # make sure the object that lands in the cache is always
                # a proxy object
                retval = generic.proxy_encrypted_obj(retval, sh)
            return retval

    def cache_get_indirect_object(self, generation, idnum):
        out = self.resolved_objects.get((generation, idnum))
        return out

    def cache_indirect_object(self, generation, idnum, obj):
        self.resolved_objects[(generation, idnum)] = obj
        return obj

    def _read_xref_stream(self):
        stream = self.stream
        idnum, generation = read_object_header(stream, strict=self.strict)
        xrefstream_ref = generic.Reference(idnum, generation, self)
        xrefstream = generic.StreamObject.read_from_stream(
            stream, xrefstream_ref
        )
        xrefstream.container_ref = xrefstream_ref
        assert xrefstream["/Type"] == "/XRef"
        xref_cache = self.xrefs
        xref_cache.xref_container_info.append((xrefstream_ref, stream.tell()))
        self.cache_indirect_object(generation, idnum, xrefstream)
        xref_cache.read_xref_stream(xrefstream)

        self.trailer.add_trailer_revision(xrefstream)
        return xrefstream.get('/Prev')

    def _read_xref_table(self):
        stream = self.stream
        xref_cache = self.xrefs
        xref_start = stream.tell()
        xref_cache.read_xref_table()
        xref_end = stream.tell()
        xref_cache.xref_container_info.append((xref_start, xref_end))
        new_trailer = generic.DictionaryObject.read_from_stream(
            stream, generic.TrailerReference(self)
        )
        assert isinstance(new_trailer, generic.DictionaryObject)

        self.trailer.add_trailer_revision(new_trailer)
        return new_trailer.get('/Prev')

    def _read_xrefs(self):
        # read all cross reference tables and their trailers
        stream = self.stream
        self.trailer = TrailerDictionary()
        self.trailer.container_ref = generic.TrailerReference(self)
        startxref = self.last_startxref
        xref_location_log = self.xrefs.xref_locations
        while startxref is not None:
            xref_location_log.append(startxref)
            # load the xref table
            stream.seek(startxref)
            x = stream.read(1)
            if x == b"x":
                # standard cross-reference table
                ref = stream.read(4)
                if ref[:3] != b"ref":
                    raise PdfReadError("xref table read error")
                startxref = self._read_xref_table()
            elif x.isdigit():
                # PDF 1.5+ Cross-Reference Stream
                stream.seek(-1, os.SEEK_CUR)
                startxref = self._read_xref_stream()
                self.has_xref_stream = True
            else:
                # bad xref character at startxref.  Let's see if we can find
                # the xref table nearby, as we've observed this error with an
                # off-by-one before.
                stream.seek(-11, os.SEEK_CUR)
                tmp = stream.read(20)
                xref_loc = tmp.find(b"xref")
                if xref_loc != -1:
                    startxref -= (10 - xref_loc)
                    continue
                # No explicit xref table, try finding a cross-reference stream.
                stream.seek(startxref)
                found = False
                for look in range(5):
                    if stream.read(1).isdigit():
                        # This is not a standard PDF, consider adding a warning
                        startxref += look
                        found = True
                        break
                if found:
                    continue
                # no xref table found at specified location
                raise PdfReadError(
                    "Could not find xref table at specified location"
                )

        if self.xrefs._previous_expected_free:
            orphans = ','.join(
                f'{k} {v} obj'
                for k, v in self.xrefs._previous_expected_free.items()
            )
            raise PdfReadError(
                "Xref table contains orphaned higher generation objects: "
                + orphans
            )

    def read(self):
        # first, read the header & PDF version number
        # (version number can be overridden in the document catalog later)
        stream = self.stream
        stream.seek(0)
        input_version = None
        try:
            header = misc.read_until_whitespace(stream, maxchars=20)
            # match ignores trailing chars
            m = header_regex.match(header)
            if m is not None:
                major = int(m.group(1))
                minor = int(m.group(2))
                input_version = (major, minor)
        except (UnicodeDecodeError, ValueError):
            pass
        if input_version is None:
            raise ValueError('Illegal PDF header')
        self.input_version = input_version

        # start at the end:
        stream.seek(-1, os.SEEK_END)
        if not stream.tell():
            raise PdfReadError('Cannot read an empty file')

        # This needs to be recorded for incremental update purposes
        self.last_startxref = process_data_at_eof(stream)
        self._read_xrefs()

    def decrypt(self, password: Union[str, bytes]):
        """
        When using an encrypted PDF file with the standard PDF encryption
        handler, this function will allow the file to be decrypted.
        It checks the given password against the document's user password and
        owner password, and then stores the resulting decryption key if either
        password is correct.

        Both legacy encryption schemes and PDF 2.0 encryption (based on AES-256)
        are supported.

        .. danger::
            Supplying either user or owner password will work.
            Cryptographically, both allow the decryption key to be computed,
            but processors are expected to adhere to the ``/P`` flags in the
            encryption dictionary when accessing a file with the user password.
            Currently, pyHanko does not enforce these restrictions, but it
            may in the future.

        .. danger::
            One should also be aware that the legacy encryption schemes used
            prior to PDF 2.0 are (very) weak, and we only support them for
            compatibility reasons.
            Under no circumstances should these still be used to encrypt new
            files.

        :param password: The password to match.
        """
        sh = self.security_handler
        if not isinstance(sh, StandardSecurityHandler):
            raise misc.PdfReadError(
                f"Security handler is of type '{type(sh)}', "
                f"not StandardSecurityHandler"
            )  # pragma: nocover

        return sh.authenticate(password, id1=self.document_id[0])

    def decrypt_pubkey(self, credential: EnvelopeKeyDecrypter):
        """
        Decrypt a PDF file encrypted using public-key encryption by providing
        a credential representing the private key of one of the recipients.

        .. danger::
            The same caveats as in :meth:`.decrypt` w.r.t. permission handling
            apply to this method.

        .. danger::
            The robustness of the public key cipher being used is not the only
            factor in the security of public-key encryption in PDF.
            The standard still permits weak schemes to encrypt the actual file
            data and file keys.
            PyHanko uses sane defaults everywhere, but other software may not.

        :param credential:
            The :class:`.EnvelopeKeyDecrypter` handling the recipient's
            private key.
        """
        sh = self.security_handler
        if not isinstance(sh, PubKeySecurityHandler):
            raise misc.PdfReadError(
                f"Security handler is of type '{type(sh)}', "
                f"not PubKeySecurityHandler"
            )  # pragma: nocover
        return sh.authenticate(credential)

    @property
    def encrypted(self):
        """
        :return: ``True`` if a document is encrypted, ``False`` otherwise.
        """
        return self.security_handler is not None

    def get_historical_resolver(self, revision: int) -> 'HistoricalResolver':
        """
        Return a :class:`~.rw_common.PdfHandler` instance that provides a view
        on the file at a specific revision.

        :param revision:
            The revision number to use, with `0` being the oldest.
        :return:
            An instance of :class:`~.HistoricalResolver`.
        """
        cache = self._historical_resolver_cache
        try:
            return cache[revision]
        except KeyError:
            res = HistoricalResolver(self, revision)
            cache[revision] = res
            return res

    @property
    def embedded_signatures(self):
        """
        :return:
            The signatures embedded in this document, in signing order;
            see :class:`~pyhanko.sign.validation.EmbeddedPdfSignature`.
        """
        if self._embedded_signatures is not None:
            return self._embedded_signatures
        from pyhanko.sign.fields import enumerate_sig_fields
        from pyhanko.sign.validation import EmbeddedPdfSignature
        sig_fields = enumerate_sig_fields(self, filled_status=True)

        result = sorted(
            (
                EmbeddedPdfSignature(self, sig_field)
                for _, sig_obj, sig_field in sig_fields
            ), key=lambda emb: emb.signed_revision
        )
        self._embedded_signatures = result
        return result


def convert_to_int(d, size):
    if size <= 8:
        padding = bytes(8 - size)
        return struct.unpack(">q", padding + d)[0]
    else:
        return sum(digit ** (size - ix - 1) for ix, digit in enumerate(d))


class RawPdfPath:
    """
    Class to model raw paths in a file.
    """

    def __init__(self, *path: Union[str, int]):
        self.path = path

    def __len__(self):
        return len(self.path)

    def __iter__(self):
        return iter(self.path)

    def _tag(self):
        # should give better hashing results
        return tuple(map(lambda x: (isinstance(x, int), x), self.path))

    def __hash__(self):
        return hash(self._tag())

    def __eq__(self, other):
        return (
            isinstance(other, RawPdfPath)
            and (self is other or self._tag() == other._tag())
        )

    @staticmethod
    def _fmt_node(node):
        if isinstance(node, int):
            return '[%d]' % node
        else:
            return '.' + node[1:]

    def __add__(self, other):
        if isinstance(other, RawPdfPath):
            return RawPdfPath(*self.path, *other.path)
        elif isinstance(other, (int, str)):
            return RawPdfPath(*self.path, other)
        else:  # pragma: nocover
            raise TypeError

    def __str__(self):
        return ''.join(map(RawPdfPath._fmt_node, self.path))

    def __repr__(self):  # pragma: nocover
        return f"PathInRevision('{str(self)}')"


class HistoricalResolver(PdfHandler):
    """
    :class:`~.rw_common.PdfHandler` implementation that provides a view
    on a particular revision of a PDF file.

    Instances of :class:`.HistoricalResolver` should be created by calling the
    :meth:`~.PdfFileReader.get_historical_resolver` method on a
    :class:`~.PdfFileReader` object.

    Instances of this class cache the result of :meth:`get_object` calls.

    .. note::
        Be aware that instances of this class transparently rewrite the PDF
        handler associated with any reference objects returned from the reader,
        so calling :meth:`~.generic.Reference.get_object` on an indirect
        reference object will cause the reference to be resolved within the
        selected revision.
    """

    @property
    def document_id(self) -> Tuple[bytes, bytes]:
        id_arr = self._trailer['/ID']
        return id_arr[0].original_bytes, id_arr[1].original_bytes

    def __init__(self, reader: PdfFileReader, revision):
        self.cache = {}
        self.reader = reader
        self.revision = revision
        self._trailer = self.reader.trailer.flatten(self.revision)
        self._indirect_object_access_cache = None

    @property
    def trailer_view(self) -> generic.DictionaryObject:
        return self._trailer

    def get_object(self, ref: generic.Reference):
        cache = self.cache
        try:
            return cache[ref]
        except KeyError:
            # TODO deal with container_ref

            # if the object wasn't modified after this revision
            # we can grab it from the "normal" shared cache.
            reader = self.reader
            revision = self.revision
            if reader.xrefs.get_last_change(ref.idnum) <= revision:
                obj = reader.get_object(ref)
            else:
                obj = reader.get_object(ref, revision)

            # replace all PDF handler references in the object with references
            # to this one, so that indirect references will resolve within
            # this historical revision
            cache[ref] = obj = self._subsume_object(obj)
            return obj

    def _subsume_object(self, obj):
        if isinstance(obj, generic.IndirectObject):
            return generic.IndirectObject(
                idnum=obj.idnum, generation=obj.generation, pdf=self
            )
        elif isinstance(obj, generic.StreamObject):
            return generic.StreamObject({
                k: self._subsume_object(v) for k, v in obj.items()
            }, encoded_data=obj.encoded_data)
        elif isinstance(obj, generic.DictionaryObject):
            return generic.DictionaryObject({
                k: self._subsume_object(v) for k, v in obj.items()
            })
        elif isinstance(obj, generic.ArrayObject):
            return generic.ArrayObject(
                self._subsume_object(v) for v in obj
            )
        else:
            return obj

    @property
    def root_ref(self) -> generic.Reference:
        ref: generic.IndirectObject = self.reader.trailer.raw_get(
            '/Root', revision=self.revision
        )
        return generic.Reference(
            idnum=ref.idnum, generation=ref.generation, pdf=self
        )

    def __call__(self, ref: generic.Reference):
        return self.get_object(ref)

    def explicit_refs_in_revision(self):
        return self.reader.xrefs.explicit_refs_in_revision(self.revision)

    def refs_freed_in_revision(self):
        return self.reader.xrefs.refs_freed_in_revision(self.revision)

    def object_streams_used(self):
        return self.reader.xrefs.object_streams_used_in(self.revision)

    def is_ref_available(self, ref: generic.Reference) -> bool:
        """
        Check if the reference in question would already point to an object
        in this revision.

        :param ref:
            A reference object (usually one written to by a by a newer revision)
        :return:
            ``True`` if the reference is undefined, ``False`` otherwise.
        """

        # TODO double-check behaviour of freed objects

        xref_cache = self.reader.xrefs
        try:
            xref_cache.get_historical_ref(ref, self.revision)
            # if we get here, the ref was taken
            return False
        except misc.PdfReadError:
            return True

    def collect_dependencies(self, obj: generic.PdfObject, since_revision=None):
        """
        Collect all indirect references used by an object and its descendants.

        :param obj:
            The object to inspect.
        :param since_revision:
            Optionally specify a revision number that tells the scanner to only
            include objects IDs that were added in that revision or later.

            .. warning::
                In particular, this means that the scanner will not recurse
                into older objects either.

        :return:
            A :class:`set` of :class:`~.generic.Reference` objects.
        """
        result_set = set()
        self._collect_indirect_references(obj, result_set, since_revision)
        return result_set

    def _collect_indirect_references(self, obj, seen, since_revision=None):
        if isinstance(obj, generic.IndirectObject):
            ref = obj.reference
            if ref in seen:
                return
            xrefs = self.reader.xrefs
            relevant = (
                since_revision is None
                or xrefs.get_introducing_revision(ref) >= since_revision
            )
            if relevant:
                seen.add(ref)
            elif since_revision is not None:
                # do not recurse into objects that already existed before the
                # target revision, since we won't (shouldn't!) find any new refs
                # there.
                return
            obj = self(ref)
        if isinstance(obj, generic.DictionaryObject):
            for v in obj.values():
                self._collect_indirect_references(v, seen, since_revision)
        elif isinstance(obj, generic.ArrayObject):
            for v in obj:
                self._collect_indirect_references(v, seen, since_revision)

    def _get_usages_of_ref(self, ref: generic.Reference) \
            -> Optional[Set[RawPdfPath]]:
        cache = self._indirect_object_access_cache or {}
        return cache.get(ref, None)

    def _load_reverse_xref_cache(self):
        if self._indirect_object_access_cache is not None:
            return

        collected = defaultdict(set)

        # internally, _compute_paths_to_refs works with singly linked lists
        # to avoid having to create & destroy lots of list objects
        # We flatten everything when we're done
        def _compute_paths_to_refs(obj, cur_path: misc.ConsList,
                                   seen_in_path: misc.ConsList, *,
                                   is_page_tree, page_tree_objs):

            # optimisation: page tree gets special treatment
            # to prevent unnecessary paths from being generated when the
            # tree is entered from the outside (e.g. by following /P on a form
            # field/widget annotation)
            # TODO what about the structure tree?
            # Can we deal with this more systematically? The problem is that
            # there's no a priori way to state which path from an object
            # to the trailer is the "canonical" one. For page objects
            #  things are a bit more clear-cut, so we deal with those
            # separately.

            if isinstance(obj, generic.IndirectObject):
                ref = obj.reference
                if ref in seen_in_path:
                    return
                collected[ref].add(cur_path)
                seen_in_path = seen_in_path.cons(ref)
                obj = self(ref)
                if not is_page_tree and ref in page_tree_objs:
                    return
            if isinstance(obj, generic.DictionaryObject):
                for k, v in obj.items():
                    # another hack to eliminate some spurious extra paths
                    # that don't convey any useful information
                    if k == '/Parent':
                        continue

                    _compute_paths_to_refs(
                        v, cur_path.cons(k), seen_in_path,
                        is_page_tree=is_page_tree or (
                            cur_path.head == '/Root' and k == '/Pages'
                            and cur_path.tail == misc.ConsList.empty()
                        ),
                        page_tree_objs=page_tree_objs
                    )
            elif isinstance(obj, generic.ArrayObject):
                for ix, v in enumerate(obj):
                    _compute_paths_to_refs(
                        v, cur_path.cons(ix), seen_in_path,
                        is_page_tree=is_page_tree,
                        page_tree_objs=page_tree_objs
                    )

        def _collect_page_tree_refs(pages_obj):
            for kid in pages_obj['/Kids']:
                # should always be true, but hey
                if isinstance(kid, generic.IndirectObject):
                    yield kid.reference
                kid = kid.get_object()
                if kid.get('/Type', None) == '/Pages':
                    yield from _collect_page_tree_refs(kid)

        pages_ref = self.root.raw_get('/Pages')
        page_tree_nodes = set()
        page_tree_nodes.update(
            _collect_page_tree_refs(pages_obj=pages_ref.get_object())
        )
        _compute_paths_to_refs(
            self.trailer_view, misc.ConsList.empty(), misc.ConsList.empty(),
            is_page_tree=False, page_tree_objs=page_tree_nodes
        )

        self._indirect_object_access_cache = {
            ref: {RawPdfPath(*reversed(list(p))) for p in paths}
            for ref, paths in collected.items()
        }
