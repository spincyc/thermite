"""The integration contract (lib/oh_integrations/_hooks.py), the modules
that define its hooks and the generic code that reads them.

Most of these read syntax trees, of every Python source of the application:
a rule here is about how code is written (nothing asks hasattr() of a hook,
no generic module imports an integration), which no run of that code shows.
"""

import ast
import functools
from pathlib import Path
import sys
import types
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_integrations as integrations  # noqa: E402
from oh_integrations import _hooks  # noqa: E402
from oh_integrations._hooks import HOOKS, UndeclaredHook  # noqa: E402

#: The integration modules, by the name they are imported under.
MODULES = {module.__name__.rpartition(".")[2]: module for module in integrations.REGISTRY}
#: Their own sources, which define the hooks and read none.
OWN = {name: str(Path(module.__file__).relative_to(AREA)) for name, module in MODULES.items()}
TABLE = "lib/oh_integrations/_hooks.py"
#: A name one letter from a hook's, which no table declares.
MISSPELT = "REMOTE_LAYOT"

#: Generic code finds an integration through REGISTRY and imports none by
#: name. These are not generic: each is one vendor's own verb, feature or
#: stand-in, and names the integration it belongs to.
BY_NAME = {
    "lib/oh_access/native_boundary.py": ({"sonos"}, "the callback relay takes Sonos's UPnP events"),
    "lib/oh_cli/cameras.py": ({"unifi_protect"}, "thermite cameras provisions the Protect bridges"),
    "lib/oh_cli/ir_profile.py": ({"globalcache_ir"}, "thermite ir-profile checks a Global Caché profile"),
    "lib/oh_cli/pair.py": ({"androidtv_googletv", "pyatv_http", "sony_ip_control"},
                           "thermite pair runs each of these integrations' own pairing"),
    "lib/oh_features/remote_cameras.py": ({"unifi_protect"}, "the camera catalogue is Protect's"),
    "lib/oh_testing/routing.py": ({"vrroom_telnet"}, "the routing check waits out the VRROOM rule's poll"),
}

#: hasattr() and getattr() of these names are asked of something that is no
#: integration module.
NOT_A_MODULE = {
    ("lib/oh_identity/policy_store.py", "keys"): "a mapping's keys()",
}

#: Public names of an integration that nothing uses by name, and are no hook.
UNNAMED = {
    ("sony_ip_control", "USERDATA_DIR"): "its stand-in (oh_testing.controls) reads it from the module it is handed",
}

#: The kinds of node these tests read; a source is walked for them once.
READ = (ast.Attribute, ast.Call, ast.Import, ast.ImportFrom, ast.Assign, ast.For)


@functools.cache
def trees() -> dict[str, ast.Module]:
    """Every Python source of the application, by its path from the root."""
    paths = [path for top in ("lib", "tests") for path in sorted((AREA / top).rglob("*.py"))] + [AREA / "bin/thermite"]
    return {str(path.relative_to(AREA)): ast.parse(path.read_text(encoding="utf-8"), str(path)) for path in paths}


@functools.cache
def sources() -> dict[str, tuple[ast.AST, ...]]:
    """The nodes of each source that these tests read."""
    return {path: tuple(node for node in ast.walk(tree) if isinstance(node, READ)) for path, tree in trees().items()}


def generic() -> dict[str, tuple[ast.AST, ...]]:
    """The sources that may read a hook: the application's code, without
    its tests, the integrations' own modules and the table."""
    return {path: nodes for path, nodes in sources().items()
            if "tests" not in Path(path).parts and path not in OWN.values() and path != TABLE}


def packages(nodes) -> set[str]:
    """The names a source knows the integrations package by."""
    return {alias.asname or "oh_integrations" for node in nodes if isinstance(node, ast.Import) for alias in node.names
            if alias.name == "oh_integrations" or alias.name.startswith("oh_integrations.") and not alias.asname}


def naming(nodes):
    """A source's way to tell which integration an expression names: the
    function expression -> integration module name, or None. It knows what
    the source imports from the package, and what it assigns one of those
    to (`tv = integrations.sony_ip_control`)."""
    local, package = {}, packages(nodes)

    def module(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return local.get(node.id)
        if isinstance(node, ast.Attribute) and node.attr in MODULES and isinstance(node.value, ast.Name):
            return node.attr if node.value.id in package else None
        return None

    for node in nodes:
        # `from . import sonos` inside the package, `from oh_integrations import sonos` outside it.
        if isinstance(node, ast.ImportFrom) and (node.module or "oh_integrations").rpartition(".")[2] == "oh_integrations":
            local.update({alias.asname or alias.name: alias.name for alias in node.names if alias.name in MODULES})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                package_name, _, name = alias.name.rpartition(".")
                if alias.asname and package_name == "oh_integrations" and name in MODULES:
                    local[alias.asname] = name
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            pairs = (zip(target.elts, value.elts, strict=True) if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple)
                     and len(target.elts) == len(value.elts) else [(target, value)])
            local.update({name.id: found for name, expression in pairs
                          if isinstance(name, ast.Name) and (found := module(expression))})
    return module


def by_name(nodes) -> set[tuple[str, str]]:
    """(integration, name) of everything a source reads from an integration
    it names: an attribute of the module, or a name imported from it."""
    module = naming(nodes)
    found = set()
    for node in nodes:
        if isinstance(node, ast.Attribute) and (owner := module(node.value)):
            found.add((owner, node.attr))
        elif isinstance(node, ast.ImportFrom) and (owner := (node.module or "").rpartition(".")[2]) in MODULES:
            found.update((owner, alias.name) for alias in node.names)
    return found


def imported(nodes) -> set[str]:
    """The integrations a source outside the package imports or reaches by name."""
    package = packages(nodes)
    found = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "oh_integrations":
            parts = (node.module or "").split(".")
            found.update(parts[1:2] if len(parts) > 1 else [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[1] for alias in node.names if alias.name.startswith("oh_integrations."))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in package:
            found.add(node.attr)
    return found & set(MODULES)


def literal(node: ast.AST, functions: tuple[str, ...], position: int) -> str | None:
    """The string a call of one of `functions` is given at `position`."""
    if not isinstance(node, ast.Call) or len(node.args) <= position:
        return None
    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
    argument = node.args[position]
    return argument.value if name in functions and isinstance(argument, ast.Constant) and isinstance(argument.value, str) else None


def asked(nodes) -> set[str]:
    """The hooks a source asks defining() or declared() for by name."""
    return {name for node in nodes if (name := literal(node, ("defining", "declared"), 0)) is not None}


def first_answers(nodes) -> set[str]:
    """The hooks a source takes one answer of: defining(hook) under next(),
    or in a loop that returns or breaks."""
    found = set()
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "next" and node.args:
            loops = [loop.iter for loop in getattr(node.args[0], "generators", ())]
        elif isinstance(node, ast.For) and literal(node.iter, ("defining",), 0) and any(
                isinstance(inner, (ast.Return, ast.Break)) for statement in node.body for inner in ast.walk(statement)):
            loops = [node.iter]
        else:
            continue
        found.update(name for loop in loops if (name := literal(loop, ("defining",), 0)) is not None)
    return found


def defined(body: list[ast.stmt]):
    """The names a module's own statements define (not what it imports)."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                yield from (name.id for name in ast.walk(target) if isinstance(name, ast.Name))
        elif isinstance(node, (ast.If, ast.Try)):
            yield from defined([*node.body, *node.orelse])


class Table(unittest.TestCase):
    def test_every_integration_answers_through_the_table_and_defines_what_is_required(self):
        for name, module in MODULES.items():
            with self.subTest(integration=name):
                self.assertIs(vars(module).get("__getattr__"), _hooks.absent)
                self.assertEqual([hook.name for hook in HOOKS.values() if hook.required and hook.name not in vars(module)], [])

    def test_a_hook_is_defined_as_its_kind_and_with_its_companions(self):
        for name, module in MODULES.items():
            for hook in HOOKS.values():
                if hook.name not in vars(module):
                    continue
                with self.subTest(integration=name, hook=hook.name):
                    self.assertTrue(_hooks.KINDS[hook.kind](vars(module)[hook.name]), f"not a {hook.kind}")
                    self.assertEqual([needed for needed in hook.needs if needed not in vars(module)], [])

    def test_every_hook_names_hooks_and_integrations_that_exist(self):
        names = {module.NAME for module in integrations.REGISTRY}
        for hook in HOOKS.values():
            with self.subTest(hook=hook.name):
                self.assertLessEqual(set(hook.needs), set(HOOKS))
                self.assertLessEqual(set(hook.order or ()), names)
                self.assertTrue(hook.doc)
                # One object answers for every module that lacks the hook.
                self.assertIsInstance(hook.default, (type(None), bool, tuple))
                self.assertFalse(hook.required and (hook.default is not None or hook.order or hook.needs))

    def test_every_hook_is_defined_by_an_integration_and_read_by_generic_code(self):
        read = set()
        for nodes in generic().values():
            module = naming(nodes)
            read |= asked(nodes) | {node.attr for node in nodes if isinstance(node, ast.Attribute) and not module(node.value)}
        for hook in HOOKS.values():
            with self.subTest(hook=hook.name):
                self.assertTrue(integrations.defining(hook.name), "no integration defines it")
                self.assertIn(hook.name, read, "nothing reads it")

    def test_an_order_names_exactly_the_integrations_that_define_the_hook(self):
        for hook in HOOKS.values():
            if hook.order is None:
                continue
            with self.subTest(hook=hook.name):
                self.assertEqual(sorted(hook.order), sorted(module.NAME for module in integrations.REGISTRY
                                                            if hook.name in vars(module)))
                self.assertEqual(tuple(module.NAME for module in integrations.defining(hook.name)), hook.order)

    def test_a_hook_without_an_order_is_asked_in_render_order(self):
        self.assertIsNone(HOOKS["files"].order)
        asked_in = [module.NAME for module in integrations.defining("files")]
        self.assertEqual(asked_in, [module.NAME for module in integrations.REGISTRY if "files" in vars(module)])
        self.assertGreater(len(asked_in), 1)


class Absent(unittest.TestCase):
    def test_a_module_answers_a_hook_it_lacks_with_the_hooks_default(self):
        module = integrations.network_ping
        self.assertFalse({"remote", "CONTROL", "NEEDS_IP", "DEVICE_KEYS"} & set(vars(module)))
        self.assertEqual((module.remote, module.CONTROL, module.NEEDS_IP, module.DEVICE_KEYS), (None, False, True, ()))

    def test_a_name_that_is_no_hook_is_an_error_however_it_is_read(self):
        module = integrations.network_ping
        self.assertNotIn(MISSPELT, HOOKS)
        for read in (lambda: module.REMOTE_LAYOT, lambda: hasattr(module, MISSPELT), lambda: getattr(module, MISSPELT, None)):
            with self.assertRaisesRegex(UndeclaredHook, f"{MISSPELT} is not a hook of an integration"):
                read()
        with self.assertRaises(KeyError):
            integrations.defining(MISSPELT)
        # Python asks a module for names of its own (__path__, say), which it only lacks.
        self.assertFalse(hasattr(module, "__path__"))

    def test_a_stand_in_for_an_integration_answers_as_one_does(self):
        module = types.ModuleType("example")
        module.__getattr__ = _hooks.absent
        self.assertEqual((module.remote, module.PROBED), (None, True))
        with self.assertRaisesRegex(AttributeError, "an integration must define NAME"):
            module.NAME  # noqa: B018 (read for the error it raises)


class Readers(unittest.TestCase):
    def test_nothing_asks_whether_a_hook_is_there_or_gives_one_a_default(self):
        # A module of the registry answers every hook, so hasattr() of one is
        # always true, and getattr()'s default is never the one that answers.
        for path, nodes in sources().items():
            if path == TABLE:
                continue
            module = naming(nodes)
            for node in nodes:
                name = literal(node, ("hasattr", "getattr"), 1)
                if name in HOOKS and (path, name) not in NOT_A_MODULE:
                    with self.subTest(path=path, line=node.lineno):
                        self.fail(f"{node.func.id}(..., {name!r}): read the hook as an attribute, or ask "
                                  "oh_integrations.defining() who defines it")
                # Whatever the name, and however it is spelt out: these tests alone ask on purpose.
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) >= 2
                        and (node.func.id == "hasattr" or node.func.id == "getattr" and len(node.args) == 3)
                        and module(node.args[0]) and path != "tests/test_hooks.py"):
                    with self.subTest(path=path, line=node.lineno):
                        self.fail(f"{node.func.id}() of the integration {module(node.args[0])}: vars() tells what it defines")
        for (path, name), why in NOT_A_MODULE.items():
            with self.subTest(path=path, name=name):
                self.assertTrue(why)
                self.assertIn(name, {literal(node, ("hasattr", "getattr"), 1) for node in sources()[path]},
                              "a stale NOT_A_MODULE entry")

    def test_defining_and_declared_are_asked_for_hooks_only(self):
        for path, nodes in sources().items():
            with self.subTest(path=path):
                self.assertLessEqual(asked(nodes), set(HOOKS))

    def test_a_reader_that_takes_one_answer_has_the_hooks_order_to_say_which(self):
        taken = set()
        for path, nodes in generic().items():
            for name in sorted(first_answers(nodes)):
                taken.add(name)
                with self.subTest(path=path, hook=name):
                    self.assertIsNotNone(HOOKS[name].order, "give the hook an order: the registry's decides nothing")
        # The readers this test must be seeing.
        self.assertLessEqual({"remote", "stage", "fields", "problem"}, taken)
        self.assertNotIn("diagnostics", taken, "signal observations combine every enabled provider")

    def test_no_generic_module_imports_an_integration(self):
        for path, nodes in generic().items():
            if path.startswith("lib/oh_integrations/"):
                continue
            allowed, why = BY_NAME.get(path, (set(), "generic"))
            with self.subTest(path=path):
                self.assertTrue(why)
                self.assertEqual(imported(nodes), allowed)
        self.assertLessEqual(set(BY_NAME), set(generic()))

    def test_a_public_name_nothing_uses_by_name_is_a_hook_nobody_declared(self):
        # A name of an integration that is no hook is used by the module
        # itself or by code that names the module. One that is used by
        # neither is dead, misspelt, or read as a hook the table lacks.
        used = set().union(*(by_name(nodes) for nodes in sources().values()))
        unnamed = set()
        for name, path in OWN.items():
            tree = trees()[path]
            loads = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
            unnamed |= {(name, public) for public in defined(tree.body)
                        if not public.startswith("_") and public not in HOOKS
                        and public not in loads and (name, public) not in used}
        self.assertEqual(sorted(unnamed), sorted(UNNAMED))
        self.assertTrue(all(UNNAMED.values()))


if __name__ == "__main__":
    unittest.main()
