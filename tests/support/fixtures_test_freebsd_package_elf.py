"""Small ELF wire fixtures; not executable native-build qualification."""

import struct


def freebsd_package_elf(*, needed=("libc.so.7",), soname=None):
    names = bytearray(b"\0")
    entries = []
    declarations = [(1, name) for name in needed]
    if soname is not None:
        declarations.append((14, soname))
    for tag, name in declarations:
        entries.append((tag, len(names)))
        names.extend(name.encode("ascii") + b"\0")
    dynamic = b"".join(struct.pack("<qQ", *entry) for entry in (*entries, (0, 0)))
    dynamic_offset = 64 + 3 * 64
    strings_offset = dynamic_offset + len(dynamic)
    header = bytearray(64)
    header[:8] = b"\x7fELF\x02\x01\x01\x09"
    struct.pack_into("<HHI", header, 16, 3, 62, 1)
    struct.pack_into("<Q", header, 40, 64)
    struct.pack_into("<H", header, 52, 64)
    struct.pack_into("<HH", header, 58, 64, 3)
    sections = (
        bytes(64)
        + struct.pack(
            "<IIQQQQIIQQ", 0, 6, 0, 0, dynamic_offset, len(dynamic), 2, 0, 8, 16
        )
        + struct.pack("<IIQQQQIIQQ", 0, 3, 0, 0, strings_offset, len(names), 0, 0, 1, 0)
    )
    return bytes(header) + sections + dynamic + names
