"""What the first run on a Mac found: no certificates, no User-Agent, unreadable errors,
programs that are installed but not on the PATH an app gets."""
import os
import ssl
import stat

from talos import engines, llm, util
from tests.base import PYTHON, Case
from tests.test_cli import CliCase


class NoCertificates:
    """Stands in for the context of a Python that ships no certificate authorities."""

    def __init__(self):
        self.loaded = []

    def cert_store_stats(self):
        return {"x509_ca": len(self.loaded)}

    def load_verify_locations(self, cafile=None):
        if cafile.endswith("broken.pem"):
            raise ssl.SSLError("not a certificate file")
        self.loaded.append(cafile)


class NetTest(Case):
    def setUp(self):
        super().setUp()
        self.kept = (util._tls, util.CA_BUNDLES, ssl.create_default_context)

        def restore():
            util._tls, util.CA_BUNDLES, ssl.create_default_context = self.kept

        self.closers.append(restore)

    def test_loads_the_system_certificates_when_python_has_none(self):
        missing, broken, good = self.tmp / "missing.pem", self.tmp / "broken.pem", self.tmp / "good.pem"
        broken.write_text("x", encoding="utf-8")
        good.write_text("x", encoding="utf-8")
        empty = NoCertificates()
        ssl.create_default_context = lambda: empty
        util._tls, util.CA_BUNDLES = None, (str(missing), str(broken), str(good))
        self.assertIs(util.tls_context(), empty)
        self.assertEqual(empty.loaded, [str(good)])               # skipped the missing and the broken one
        self.assertIs(util.tls_context(), empty)                  # kept, not rebuilt for every call

    def test_a_python_with_certificates_is_left_alone(self):
        full = NoCertificates()
        full.loaded.append("its own")
        ssl.create_default_context = lambda: full
        util._tls = None
        util.tls_context()
        self.assertEqual(full.loaded, ["its own"])

    def test_the_real_context_checks_certificates(self):
        util._tls = None
        context = util.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        if not context.cert_store_stats().get("x509_ca") and not ssl.get_default_verify_paths().capath:
            self.skipTest("this computer has no certificate list in any of the usual places")

    def test_model_calls_say_who_is_calling(self):
        answer = {"choices": [{"message": {"content": "pong"}}]}
        server = self.http(lambda method, path, body, headers: (200, answer))
        model = {"name": "test/m", "id": "m", "base_url": server.url, "api_key": "", "headers": {}}
        self.assertEqual(llm.LLM({}).chat(model, [{"role": "user", "content": "ping"}])["content"], "pong")
        self.assertTrue(server.requests[0][3]["User-Agent"].startswith("talos/"))
        util.http("GET", server.url + "/x")
        self.assertTrue(server.requests[1][3]["User-Agent"].startswith("talos/"))
        # a User-Agent set in the provider's own headers wins
        model["headers"] = {"User-Agent": "mine/1"}
        llm.LLM({}).chat(model, [{"role": "user", "content": "ping"}])
        self.assertEqual(server.requests[2][3]["User-Agent"], "mine/1")

    def test_an_http_address_that_moves_to_https_gets_the_same_certificate_list(self):
        import urllib.request
        for address in ("http://example.com/v1", "https://example.com/v1", "http://127.0.0.1:1/v1"):
            handlers = [item for item in util._opener(address).handlers if isinstance(item, urllib.request.HTTPSHandler)]
            self.assertEqual(len(handlers), 1, address)
            self.assertIs(handlers[0]._context, util.tls_context(), address)

    def test_a_huge_error_page_is_not_read_whole(self):
        page = "<html><head><title>Too big</title></head>" + "x" * 3_000_000
        server = self.http(lambda method, path, body, headers: (500, page, "text/html"))
        model = {"name": "test/m", "id": "m", "base_url": server.url, "api_key": "", "headers": {}}
        read = []
        kept = util.explain_http_error
        util.explain_http_error = lambda status, body: read.append(len(body)) or kept(status, body)
        self.closers.append(lambda: setattr(util, "explain_http_error", kept))
        with self.assertRaises(llm.ModelError) as caught:
            llm.LLM({"retry_wait": 0}).chat(model, [{"role": "user", "content": "ping"}])
        self.assertIn("(Too big)", str(caught.exception))
        self.assertTrue(read and max(read) <= 65536, read)

    def test_a_failed_call_is_said_in_one_line(self):
        page = "<!doctype html>\n<html><head>\n<title>Access denied |\n example.com</title></head><body>" + "x" * 3000
        self.assertEqual(util.explain_http_error(403, page),
                         "HTTP 403: the server answered with a web page, not an API answer (Access denied | example.com)")
        self.assertIn("error 1010", util.explain_http_error(403, "error code: 1010\n"))
        self.assertEqual(util.explain_http_error(429, '{"type":"error","error":{"type":"Limit","message":"Free usage limit reached."}}'),
                         "HTTP 429: Free usage limit reached.")
        self.assertEqual(util.explain_http_error(401, '{"error":"bad key"}'), "HTTP 401: bad key")
        self.assertEqual(util.explain_http_error(500, '{"detail":"boom"}'), "HTTP 500: boom")
        self.assertEqual(util.explain_http_error(502, ""), "HTTP 502")
        self.assertEqual(util.explain_http_error(500, "plain words"), "HTTP 500: plain words")

        # and that line is what the user gets, with the provider's name in front
        server = self.http(lambda method, path, body, headers: (403, page, "text/html"))
        model = {"name": "test/m", "id": "m", "base_url": server.url, "api_key": "", "headers": {}}
        with self.assertRaises(llm.ModelError) as caught:
            llm.LLM({}).chat(model, [{"role": "user", "content": "ping"}])
        self.assertEqual(str(caught.exception),
                         "test/m: HTTP 403: the server answered with a web page, not an API answer (Access denied | example.com)")

    def test_finds_a_program_that_is_not_on_the_path(self):
        folder = self.user / ".local" / "bin"
        folder.mkdir(parents=True)
        program = folder / ("talos-test-engine.cmd" if os.name == "nt" else "talos-test-engine")
        program.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
        program.chmod(program.stat().st_mode | stat.S_IXUSR)
        self.assertIsNone(util.find_program("talos-no-such-program"))
        found = util.find_program("talos-test-engine")
        self.assertEqual(os.path.normcase(found), os.path.normcase(str(program)))
        loaded = self.config(engines={"mine": ["talos-test-engine"]})
        self.assertEqual(os.path.normcase(engines.available(loaded)["mine"]), os.path.normcase(str(program)))
        self.assertEqual(os.path.normcase(engines.argv(loaded, "mine", "hi")[0]), os.path.normcase(str(program)))

    def test_an_engine_works_in_the_project_folder_and_answers_without_colour_codes(self):
        show = ("import os, sys; print('\\x1b[0m$ \\x1b[1;32md\\x1b[38:5:196mo\\x1b]8;;http://x\\x1b\\\\n\\x1b]8;;\\x07e\\x1b[0m');"
                "print('cwd=' + os.path.realpath(os.getcwd())); print('PWD=' + os.path.realpath(os.environ.get('PWD', os.getcwd())))")
        loaded = self.config(engines={"mine": {"command": PYTHON, "args": ["-c", show, "{prompt}"]}})
        saved = os.environ.get("PWD")
        os.environ["PWD"] = str(self.tmp)                         # what a shell left behind: another folder
        self.closers.append(lambda: os.environ.pop("PWD", None) if saved is None else os.environ.__setitem__("PWD", saved))
        answer = engines.run(loaded, "mine", "x", str(self.work))
        here = os.path.realpath(str(self.work))
        self.assertEqual(answer.splitlines(), ["$ done", "cwd=" + here, "PWD=" + here])


class WhenTheModelSaysNoTest(CliCase):
    def test_the_command_says_why_and_what_else_works(self):
        limit = {"type": "error", "error": {"type": "FreeUsageLimitError", "message": "Free usage limit reached."}}
        server = self.http(lambda method, path, body, headers: (429, limit))
        settings = {"model": "lim/m", "provider": {"lim": {"options": {"baseURL": server.url}}}, "retry_wait": 0}
        code, output, _ = self.talos("run", "hello", "--yes",
                                     extra=dict(settings, engines={"mine": [PYTHON, "-c", "print(1)"]}))
        self.assertEqual(code, 1)
        self.assertIn("lim/m: HTTP 429: Free usage limit reached.", output)
        self.assertIn('Another way now: talos engine mine "your task"', output)
        self.assertNotIn("{", output)                                   # no raw JSON, no HTML

        code, output, _ = self.talos("run", "hello", "--yes", extra=dict(settings, engines={}))
        self.assertEqual(code, 1)
        self.assertIn("talos models", output)        # which sentence depends on what this computer has installed
