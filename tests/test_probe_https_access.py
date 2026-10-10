"""An invalid certificate may be fingerprinted, but no HTTP request is sent."""
import hashlib
import ssl

from scripts import probe_https_access


def test_invalid_certificate_diagnostic_never_sends_http(monkeypatch):
    class VerificationError(ssl.SSLCertVerificationError):
        verify_code = 62

    class Socket:
        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class VerifiedContext:
        def wrap_socket(self, *args, **kwargs):
            raise VerificationError("certificate mismatch")

    class DiagnosticContext:
        def wrap_socket(self, *args, **kwargs):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def getpeercert(self, *, binary_form):
            assert binary_form
            return b"untrusted public certificate"

    monkeypatch.setattr(probe_https_access.socket, "create_connection", lambda *args: Socket())
    monkeypatch.setattr(probe_https_access.ssl, "create_default_context", lambda: VerifiedContext())
    monkeypatch.setattr(probe_https_access.ssl, "SSLContext", lambda *args: DiagnosticContext())
    monkeypatch.setattr(probe_https_access.http.client, "HTTPConnection",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("HTTP sent")))
    result = probe_https_access.probe("example.com", "192.0.2.1", path="/viewer?secret=hidden")
    assert result["tls_verified"] is False
    assert result["error"] == "certificate_verification_failed"
    assert result["untrusted_certificate_sha256"] == hashlib.sha256(b"untrusted public certificate").hexdigest()
    assert result["path"] == "/viewer"
    assert "hidden" not in str(result)
