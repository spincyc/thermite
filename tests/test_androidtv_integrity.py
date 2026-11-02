from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
import warnings
import zipfile

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
from oh_androidtv_patch.integrity import compare_cached_jar, IntegrityError, manifest_headers, UPDATE_LOCATION


class CachedIntegrity(unittest.TestCase):
    def test_native_repacking_only_exact_update_header_is_permitted(self):
        scratch = AREA.parents[1] / ".scratch"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            source, cache = root / "source.jar", root / "cache.jar"
            manifest = b"Manifest-Version: 1.0\r\nBundle-Version: 5.2.3.thermite1\r\nX-Original: unchanged\r\n\r\n"
            entries = {"META-INF/MANIFEST.MF": manifest, "Class.class": b"actual class bytes", "resource.xml": b"unchanged resource"}

            def write(path, values, duplicate=None):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as out:
                        for name, value in reversed(list(values.items())):
                            out.writestr(name, value)
                        if duplicate:
                            out.writestr(*duplicate)

            write(source, entries)
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            added = ("Bundle-UpdateLocation: " + UPDATE_LOCATION[:65] + "\r\n " + UPDATE_LOCATION[65:] + "\r\n\r\n").encode()
            native = {**entries, "META-INF/MANIFEST.MF": manifest[:-2] + added}
            write(cache, native)
            result = compare_cached_jar(source, cache, expected)
            self.assertEqual(result["only_manifest_change"], "exact_Bundle-UpdateLocation_added")
            self.assertEqual(result["entries"], 3)
            self.assertNotEqual(result["staged_sha256"], result["cached_sha256"])
            write(cache, entries)
            self.assertEqual(compare_cached_jar(source, cache, expected)["only_manifest_change"], "none")

            attacks = [
                {**native, "Class.class": b"tampered"},
                {**native, "resource.xml": b"tampered"},
                {key: value for key, value in native.items() if key != "resource.xml"},
                {**native, "extra.class": b"extra"},
                {**native, "META-INF/MANIFEST.MF": native["META-INF/MANIFEST.MF"].replace(b"X-Original: unchanged", b"X-Original: changed")},
                {**native, "META-INF/MANIFEST.MF": native["META-INF/MANIFEST.MF"].replace(b"5.2.3.thermite1", b"5.2.3.thermite2")},
                {**native, "META-INF/MANIFEST.MF": manifest[:-2] + b"Extra-Header: secret4444\r\n\r\n"},
                {**native, "META-INF/MANIFEST.MF": manifest[:-2] + b"Bundle-UpdateLocation: file:/bad4444\r\n\r\n"},
                {**native, "META-INF/MANIFEST.MF": native["META-INF/MANIFEST.MF"][:-2] + b"bundle-version: duplicate4444\r\n\r\n"},
                {**native, "META-INF/MANIFEST.MF": native["META-INF/MANIFEST.MF"] + b"Name: extra\r\nDigest: bad4444\r\n\r\n"},
            ]
            for values in attacks:
                with self.subTest(values=list(values)):
                    write(cache, values)
                    with self.assertRaises(IntegrityError) as error:
                        compare_cached_jar(source, cache, expected)
                    self.assertNotIn("4444", str(error.exception))
            write(cache, native, duplicate=("Class.class", b"duplicate"))
            with self.assertRaisesRegex(IntegrityError, "duplicate_entry"):
                compare_cached_jar(source, cache, expected)
            write(cache, native)
            with self.assertRaisesRegex(IntegrityError, "staged_sha_mismatch"):
                compare_cached_jar(source, cache, "0" * 64)

    def test_manifest_duplicate_and_invalid_folding_fail_closed(self):
        for value in (b" Manifest-Version: 1.0\r\n\r\n", b"Manifest-Version: 1.0\r\nmanifest-version: 1.0\r\n\r\n",
                      b"Manifest-Version: 1.0\r\n\r\nExtra: no\r\n", b"Manifest-Version: 1.0\rOops: bad\n\n"):
            with self.subTest(value=value), self.assertRaises(IntegrityError):
                manifest_headers(value)
