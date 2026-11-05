"""The owner application installs only PyYAML (pyproject.toml); the gateway's
release installs lib/requirements-site.txt. So every module the owner side may
import loads with the standard library and PyYAML alone: only the gateway's
own service modules may need more."""
import subprocess
import sys
import unittest
from pathlib import Path

AREA = Path(__file__).resolve().parents[1]

#: Modules only the gateway's release runs, in its own environment.
GATEWAY_ONLY = ("oh_access.metrics", "oh_access.worker", "oh_identity")

PROBE = r"""
import importlib, importlib.abc, importlib.machinery, sys, sysconfig
from pathlib import Path

lib = Path(sys.argv[1])
gateway_only = tuple(sys.argv[2:])
installed = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}


class OwnerEnvironment(importlib.abc.MetaPathFinder):
    # Anything installed beside PyYAML is the gateway's.
    def find_spec(self, name, path, target=None):
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        origin = Path(spec.origin).resolve() if spec and spec.origin and spec.has_location else None
        if origin and installed & set(origin.parents) and name.partition(".")[0] != "yaml":
            raise ImportError(f"{name} is not in the owner environment")
        return None


sys.meta_path.insert(0, OwnerEnvironment())
sys.path.insert(0, str(lib))
names = sorted({".".join(p.relative_to(lib).with_suffix("").parts).removesuffix(".__init__")
                for p in lib.rglob("*.py") if "tests" not in p.parts})
for name in names:
    if name == "__init__" or name.startswith(gateway_only):
        continue
    try:
        importlib.import_module(name)
    except ImportError as exc:
        print(f"{name}: {exc}")
"""


class OwnerImports(unittest.TestCase):
    def test_every_module_but_the_gateways_own_loads_with_pyyaml_alone(self):
        result = subprocess.run([sys.executable, "-I", "-c", PROBE, str(AREA / "lib"), *GATEWAY_ONLY],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
