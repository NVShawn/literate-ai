"""Read native pkg shared-library names from bounded accepted ELF payloads.

This inspects the ELF section representation used by pkg 2.8. It neither resolves
libraries nor claims runtime dependency admission; the accepted BOM and package
plan retain those responsibilities. Target code is never extracted or executed.
"""

import re
import struct

from literate_ai.application.packaging import PackagingError

_MAX_SECTIONS = 8192
_MAX_DYNAMIC = 65536
_MAX_STRINGS = 16 * 1024 * 1024
_LIBRARY = re.compile(r"lib[A-Za-z0-9+_.-]{1,251}\Z")


def inspect_freebsd_package_elf(stream, size, abi):
    """Return required/provided names; refuse unsupported ELF rather than guessing."""

    def fail(message):
        raise PackagingError("FreeBSD package ELF " + message)

    def read(offset, count):
        if offset < 0 or count < 0 or count > _MAX_STRINGS or offset + count > size:
            fail("metadata exceeds its accepted file bounds")
        stream.seek(offset)
        content = stream.read(count)
        if len(content) != count:
            fail("metadata is truncated")
        return content

    if size < 4 or read(0, 4) != b"\x7fELF":
        return (), ()
    header = read(0, 64)
    if header[4:7] != b"\x02\x01\x01":
        fail("requires the selected native ELF64 little-endian variant")
    kind, machine, version = struct.unpack_from("<HHI", header, 16)
    expected_machine = {"amd64": 62, "aarch64": 183}.get(abi.rsplit(":", 1)[-1])
    if machine != expected_machine or kind not in {1, 2, 3} or version != 1:
        fail("contradicts the selected target architecture or object type")
    if struct.unpack_from("<H", header, 52)[0] != 64:
        fail("header size is invalid")
    section_offset = struct.unpack_from("<Q", header, 40)[0]
    entry_size, count = struct.unpack_from("<HH", header, 58)
    if count == 0:
        # Extended section counts and sectionless runtime images need their own
        # qualified representation; an empty inventory must not hide imports.
        program_offset = struct.unpack_from("<Q", header, 32)[0]
        program_size, programs = struct.unpack_from("<HH", header, 54)
        if section_offset or programs > _MAX_SECTIONS:
            fail("uses unqualified extended or missing section metadata")
        if programs:
            if program_size != 56:
                fail("program header size is invalid")
            table = read(program_offset, programs * program_size)
            if any(
                struct.unpack_from("<I", table, index * program_size)[0] == 2
                for index in range(programs)
            ):
                fail("dynamic image has no independently readable sections")
        return (), ()
    if count > _MAX_SECTIONS or entry_size != 64 or section_offset < 64:
        fail("section table is unsupported or over budget")
    table = read(section_offset, count * entry_size)
    sections = tuple(struct.iter_unpack("<IIQQQQIIQQ", table))
    dynamic = [value for value in sections if value[1] == 6]
    if not dynamic:
        return (), ()
    if len(dynamic) != 1 or header[7] != 9:
        fail("dynamic authority is ambiguous or lacks the native FreeBSD ABI")
    section = dynamic[0]
    if (
        section[9] != 16
        or section[5] % 16
        or not 1 <= section[5] // 16 <= _MAX_DYNAMIC
        or not 0 < section[6] < len(sections)
    ):
        fail("dynamic table is invalid or over budget")
    strings = sections[section[6]]
    if strings[1] != 3 or not 1 <= strings[5] <= _MAX_STRINGS:
        fail("dynamic string table is invalid or over budget")
    names = read(strings[4], strings[5])
    entries = struct.iter_unpack("<qQ", read(section[4], section[5]))
    required, provided = set(), set()
    terminated = False
    sonames = 0
    for tag, value in entries:
        if terminated:
            if tag != 0 or value != 0:
                fail("dynamic table has entries after its terminator")
            continue
        if tag == 0:
            if value != 0:
                fail("dynamic table terminator is invalid")
            terminated = True
            continue
        if tag not in {1, 14}:
            continue
        end = names.find(b"\0", value, min(len(names), value + 253))
        if value >= len(names) or end < 0:
            fail("shared-library name is unterminated or over budget")
        try:
            name = names[value:end].decode("ascii")
        except UnicodeError:
            fail("shared-library name is not canonical ASCII")
        if _LIBRARY.fullmatch(name) is None:
            fail("shared-library name uses an unsupported native encoding")
        if tag == 1:
            required.add(name)
        else:
            sonames += 1
            if sonames != 1:
                fail("has more than one shared-library SONAME")
            provided.add(name)
    if not terminated:
        fail("dynamic table has no terminator")
    return tuple(sorted(required)), tuple(sorted(provided))
