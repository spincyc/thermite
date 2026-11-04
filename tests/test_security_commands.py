"""Local commissioning and terminal startup preserve the remote auth boundary."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

import oh_brand
from oh_access import service
from oh_cli import access, remote


class Commissioning(unittest.TestCase):
    def test_setup_refuses_redirected_output_before_arming_a_code(self):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error), \
                patch.object(service, "prepare_setup") as prepare:
            result = access.main(["setup", "--config", "/unused/config.json"])
        self.assertEqual(result, 2)
        prepare.assert_not_called()
        self.assertNotIn("setup code", output.getvalue())
        self.assertIn("interactive terminal", error.getvalue())

    def test_terminal_without_credential_defers_to_gateway_discovery(self):
        scratch = AREA / ".scratch/auth-root"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            path = Path(temporary) / "credential.json"
            args = argparse.Namespace(mock=False, native=False, url="https://site.example.invalid", sandbox=False)
            with patch.object(remote, "model_file", return_value=None), \
                    patch("oh_access.client.credential_path", return_value=path), \
                    patch("oh_access.client.saved_ca", return_value="/trusted/site-ca.pem"), \
                    patch("oh_access.client.load") as load, \
                    patch.dict(os.environ, {"THERMITE_REMOTE_TOKEN_FILE": "/another/site.json"}):
                environment = remote.launch_environment(args, oh_brand.load())
            self.assertNotIn("THERMITE_REMOTE_TOKEN_FILE", environment)
            self.assertEqual(environment["NODE_EXTRA_CA_CERTS"], "/trusted/site-ca.pem")
            self.assertFalse(remote.native_mode(args))
            load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
