"""Independent ELF metadata inspection, including bounded malformed input refusal."""

import io
import struct
import unittest

from literate_ai.adapters.freebsd_package_elf import inspect_freebsd_package_elf
from literate_ai.application.packaging import PackagingError
from tests.support.fixtures_test_freebsd_package_elf import freebsd_package_elf


class FreeBsdPackageElfTests(unittest.TestCase):
    def inspect(self, content, *, abi="FreeBSD:16:amd64"):
        return inspect_freebsd_package_elf(io.BytesIO(content), len(content), abi)

    def test_exact_required_and_provided_names_do_not_resolve_or_execute_libraries(
        self,
    ):
        content = freebsd_package_elf(
            needed=("libz.so.1", "libc.so.7", "libz.so.1"), soname="libown.so.2"
        )
        self.assertEqual(
            self.inspect(content), (("libc.so.7", "libz.so.1"), ("libown.so.2",))
        )
        self.assertEqual(self.inspect(b"ordinary non-ELF resource"), ((), ()))

    def test_actual_target_machine_and_abi_are_required(self):
        original = freebsd_package_elf()
        for offset, value in ((4, 1), (5, 2), (6, 2), (7, 0), (18, 183)):
            content = bytearray(original)
            content[offset] = value
            with self.subTest(offset=offset), self.assertRaises(PackagingError):
                self.inspect(content)
        with self.assertRaises(PackagingError):
            self.inspect(original, abi="FreeBSD:16:aarch64")
        aarch64 = bytearray(original)
        struct.pack_into("<H", aarch64, 18, 183)
        self.assertEqual(
            self.inspect(aarch64, abi="FreeBSD:16:aarch64"), (("libc.so.7",), ())
        )

    def test_truncation_offsets_counts_and_string_links_are_bounded(self):
        original = freebsd_package_elf()
        for offset, encoding, value in (
            (40, "<Q", 2**64 - 1),
            (58, "<H", 63),
            (60, "<H", 8193),
            (64 + 64 + 40, "<I", 99),
            (64 + 64 + 32, "<Q", 16 * 65537),
            (64 + 128 + 32, "<Q", 16 * 1024**2 + 1),
        ):
            content = bytearray(original)
            struct.pack_into(encoding, content, offset, value)
            with self.subTest(offset=offset), self.assertRaises(PackagingError):
                self.inspect(content)
        for content in (original[:4], original[:63], original[:-1]):
            with self.subTest(size=len(content)), self.assertRaises(PackagingError):
                self.inspect(content)

    def test_dynamic_table_termination_duplicate_sonames_and_bad_names_refuse(self):
        original = freebsd_package_elf(needed=(), soname="libown.so.2")
        for position, tag, value in (
            (256, 0, 0),  # a hidden SONAME after a terminator
            (272, 14, 1),  # a second SONAME instead of a terminator
            (256, 14, 2**64 - 1),  # out-of-range string reference
        ):
            content = bytearray(original)
            struct.pack_into("<qQ", content, position, tag, value)
            if position == 256 and tag == 0:
                struct.pack_into("<qQ", content, 272, 14, 1)
            with self.subTest(tag=tag, value=value), self.assertRaises(PackagingError):
                self.inspect(content)
        for name in ("/lib/libown.so.2", "libown\n.so.2", "lib" + "a" * 253):
            with self.subTest(name=name), self.assertRaises(PackagingError):
                self.inspect(freebsd_package_elf(needed=(name,)))

    def test_sectionless_dynamic_image_cannot_hide_its_library_inventory(self):
        content = bytearray(64 + 56)
        content[:64] = freebsd_package_elf()[:64]
        struct.pack_into("<Q", content, 40, 0)
        struct.pack_into("<H", content, 60, 0)
        struct.pack_into("<Q", content, 32, 64)
        struct.pack_into("<HH", content, 54, 56, 1)
        struct.pack_into("<I", content, 64, 2)
        with self.assertRaisesRegex(
            PackagingError, "no independently readable sections"
        ):
            self.inspect(content)
