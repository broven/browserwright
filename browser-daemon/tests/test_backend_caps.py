"""P4 capability interface: every backend reports static caps()."""
from browser_daemon.backends.extension import ExtensionBackend
from browser_daemon.backends.rdp import RdpBackend
from browser_daemon.config import load


def test_extension_caps():
    caps = ExtensionBackend(load(env={})).caps()
    assert caps == {"owns_browser": False, "supports_browser_context": False}


def test_rdp_caps():
    caps = RdpBackend(load(env={})).caps()
    assert caps == {"owns_browser": True, "supports_browser_context": True}
