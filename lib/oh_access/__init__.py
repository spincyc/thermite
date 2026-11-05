"""Trusted local composition and HTTPS clients for a thermite site.

Importing this package starts no service, device connection, or framework.
"""

class AccessError(ValueError):
    """Canonical, credential-free operator feedback."""


#: Where an access service writes its metrics textfile (oh_access.metrics);
#: its unit sets it (oh_access.staging.units).
TEXTFILE_ENVIRONMENT = "THERMITE_TEXTFILE_DIR"
