import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

import uvicorn

from scripts import cashbacks
from server import app, models, repo_worker


BANK_ID = 5044
ABSENT_BANK_ID = 9999
TEST_REPOSITORY_URL = "https://example.invalid/cashbacks.git"


def run_git(*arguments, cwd):
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def fuel_rule(category="Fuel", mcc=5541):
    return {"category": category, "include_mcc": [mcc]}


class TemporaryRepository:
    def __init__(self, test_case):
        self._temporary = tempfile.TemporaryDirectory()
        test_case.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.seed = self.root / "seed"
        self.remote = self.root / "remote.git"
        self.seed.mkdir()
        run_git("init", "-b", "main", cwd=self.seed)
        run_git("config", "user.name", "Cashbacks Test", cwd=self.seed)
        run_git("config", "user.email", "cashbacks@example.invalid", cwd=self.seed)
        (self.seed / ".gitignore").write_text("runtime/\n", encoding="utf-8")
        self.write_rules(
            [
                ("Fuel.json", fuel_rule()),
                ("Food.json", fuel_rule(["Food", "Groceries"], 5411)),
            ]
        )
        self.commit("initial")
        run_git("init", "--bare", os.fspath(self.remote), cwd=self.root)
        run_git("remote", "add", "origin", os.fspath(self.remote), cwd=self.seed)
        run_git("push", "-u", "origin", "main", cwd=self.seed)
        run_git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.remote)

    @property
    def categories(self):
        return self.seed / "src" / f"Bank-ru_{BANK_ID}" / "categories"

    def write_rules(self, rules, *, pending=False):
        suffix = "" if pending else str(BANK_ID)
        categories = self.seed / "src" / f"Bank-ru_{suffix}" / "categories"
        categories.mkdir(parents=True, exist_ok=True)
        for filename, entry in rules:
            (categories / filename).write_text(
                json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    def commit(self, message, *, force_paths=()):
        run_git("add", "-A", cwd=self.seed)
        for path in force_paths:
            run_git("add", "-f", path, cwd=self.seed)
        run_git("commit", "-m", message, cwd=self.seed)
        return run_git("rev-parse", "HEAD", cwd=self.seed)

    def push(self):
        run_git("push", "origin", "main", cwd=self.seed)
        return run_git("rev-parse", "HEAD", cwd=self.seed)

    def replace_fuel(self, category):
        filename = f"{category}.json"
        (self.categories / "Fuel.json").rename(self.categories / filename)
        self.write_rules([(filename, fuel_rule(category))])

    def update_fuel(self, category):
        self.replace_fuel(category)
        self.commit(f"change fuel to {category}")
        return self.push()

    def config(self, checkout=None, **changes):
        values = {
            "checkout": checkout or self.seed,
            "repository_url": os.fspath(self.remote),
            "host": "127.0.0.1",
            "port": 0,
            "remote": "origin",
            "ref": "main",
            "git_timeout_seconds": 30,
        }
        values.update(changes)
        return models.Config(**values)

    def cloned_state(self):
        checkout = self.root / "checkout"
        config = self.config(checkout=checkout)
        resolved, snapshot = repo_worker.bootstrap_checkout(config)
        return repo_worker.ServiceState(config, resolved, snapshot), checkout


def category_for_mcc(snapshot, mcc=5541):
    categories = json.loads(snapshot.banks[BANK_ID].categories_body)["categories"]
    return next(
        entry["category"] for entry in categories if entry["include_mcc"] == [mcc]
    )


class StubState:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.sync_result = "stub"
        self.sync_calls = 0

    def snapshot(self):
        return self._snapshot

    def replace_snapshot(self, snapshot):
        self._snapshot = snapshot

    def sync(self):
        self.sync_calls += 1
        return self.sync_result


@contextlib.contextmanager
def running_server(state):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    server = uvicorn.Server(
        uvicorn.Config(app.create_app(state), lifespan="off", log_level="critical")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive():
                raise RuntimeError("Uvicorn stopped before accepting connections")
            if time.monotonic() >= deadline:
                raise RuntimeError("Uvicorn did not start")
            time.sleep(0.01)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def request(port, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    result = response.status, {
        name.lower(): value for name, value in response.getheaders()
    }, payload
    connection.close()
    return result


def snapshot_with_active_and_pending(revision="a" * 40):
    sources = cashbacks.RepositorySources(
        banks=[
            cashbacks.BankSource(
                bank_id=BANK_ID,
                path=Path("active"),
                entries=[
                    fuel_rule(),
                    fuel_rule(["Food", "Café"], 5411),
                ],
            )
        ],
        pending_banks=[
            cashbacks.PendingBankSource(
                label="Pending-ru",
                path=Path("pending"),
                entries=[fuel_rule("Pending")],
            )
        ],
    )
    return models.build_snapshot(sources, revision)

def snapshot_with_bank_ids(*bank_ids, revision="e" * 40):
    sources = cashbacks.RepositorySources(
        banks=[
            cashbacks.BankSource(
                bank_id=bank_id,
                path=Path(f"bank-{bank_id}"),
                entries=[fuel_rule()],
            )
            for bank_id in bank_ids
        ],
        pending_banks=[],
    )
    return models.build_snapshot(sources, revision)

def snapshot_with_normalized_match_categories(revision="d" * 40):
    rules = [
        fuel_rule("На всё!", 1111),
        fuel_rule(["Кафе, бары", "Продукты"], 2222),
        fuel_rule("АЗС", 3333),
        fuel_rule("Café", 4444),
        fuel_rule("Фаст-фуд", 5555),
        fuel_rule("Елка", 6666),
        fuel_rule("Кафе", 7777),
        fuel_rule("Straße", 8888),
    ]
    sources = cashbacks.RepositorySources(
        banks=[
            cashbacks.BankSource(
                bank_id=BANK_ID,
                path=Path("normalized"),
                entries=rules,
            )
        ],
        pending_banks=[],
    )
    return models.build_snapshot(sources, revision), rules


class ConfigurationAndSnapshotTest(unittest.TestCase):
    def test_configuration_requires_nonempty_checkout_dir(self):
        for environ in (
            {"REPOSITORY_URL": TEST_REPOSITORY_URL},
            {"CHECKOUT_DIR": " \t", "REPOSITORY_URL": TEST_REPOSITORY_URL},
            {"CHECKOUT": "/data", "REPOSITORY_URL": TEST_REPOSITORY_URL},
        ):
            with self.subTest(environ=environ), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app.parse_config([], environ)

    def test_configuration_requires_repository_url(self):
        for environ in (
            {"CHECKOUT_DIR": "/data"},
            {"CHECKOUT_DIR": "/data", "REPOSITORY_URL": " \t"},
        ):
            with self.subTest(environ=environ), contextlib.redirect_stderr(
                io.StringIO()
            ):
                with self.assertRaises(SystemExit):
                    app.parse_config([], environ)


    def test_configuration_defaults_for_absent_and_whitespace_optional_environment(self):
        for environ in (
            {
                "CHECKOUT_DIR": " /data ",
                "REPOSITORY_URL": TEST_REPOSITORY_URL,
            },
            {
                "CHECKOUT_DIR": " /data ",
                "REPOSITORY_URL": f" {TEST_REPOSITORY_URL} ",
                "GITHUB_TOKEN": " \n",
                "HOST": " ",
                "PORT": "\t",
                "REMOTE": "\n",
                "REF": " ",
                "GIT_TIMEOUT_SECONDS": "",
            },
        ):
            with self.subTest(environ=environ):
                config = app.parse_config([], environ)

            self.assertEqual(config.checkout, Path("/data"))
            self.assertEqual(config.repository_url, TEST_REPOSITORY_URL)
            self.assertIsNone(config.github_token)
            self.assertEqual(
                (
                    config.host,
                    config.port,
                    config.remote,
                    config.ref,
                    config.git_timeout_seconds,
                ),
                ("0.0.0.0", 8080, "origin", "main", 300),
            )

    def test_configuration_trims_explicit_environment_values(self):
        token = "cashbacks token:/?@%"
        config = app.parse_config(
            [],
            {
                "CHECKOUT_DIR": " /data ",
                "REPOSITORY_URL": " https://example.invalid/cashbacks.git ",
                "GITHUB_TOKEN": f" \t{token}\n",
                "HOST": " 127.0.0.1 ",
                "PORT": " 9000 ",
                "REMOTE": " upstream ",
                "REF": " release/v1 ",
                "GIT_TIMEOUT_SECONDS": " 12 ",
            },
        )

        self.assertEqual(config.checkout, Path("/data"))
        self.assertEqual(config.repository_url, "https://example.invalid/cashbacks.git")
        self.assertEqual(config.github_token, token)
        self.assertNotIn(token, repr(config))
        self.assertEqual(
            (
                config.host,
                config.port,
                config.remote,
                config.ref,
                config.git_timeout_seconds,
            ),
            ("127.0.0.1", 9000, "upstream", "release/v1", 12),
        )

    def test_configuration_rejects_invalid_port_values(self):
        for value in ("-1", "65536", "not-a-port"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app.parse_config(
                        [],
                        {
                            "CHECKOUT_DIR": "/data",
                            "REPOSITORY_URL": TEST_REPOSITORY_URL,
                            "PORT": value,
                        },
                    )

        for value in ("0", "65535"):
            with self.subTest(value=value):
                self.assertEqual(
                    app.parse_config(
                        [],
                        {
                            "CHECKOUT_DIR": "/data",
                            "REPOSITORY_URL": TEST_REPOSITORY_URL,
                            "PORT": value,
                        },
                    ).port,
                    int(value),
                )

    def test_invalid_timeout_remote_and_ref_are_rejected(self):
        invalid_environment = [
            {"GIT_TIMEOUT_SECONDS": "0"},
            {"GIT_TIMEOUT_SECONDS": "no"},
            {"REMOTE": "-option"},
            {"REMOTE": "bad/name"},
            {"REF": "-option"},
            {"REF": "bad..name"},
            {"REF": "deadbeef"},
        ]
        for overrides in invalid_environment:
            environ = {
                "CHECKOUT_DIR": "/data",
                "REPOSITORY_URL": TEST_REPOSITORY_URL,
                **overrides,
            }
            with self.subTest(environ=environ), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app.parse_config([], environ)


    def test_service_module_help_is_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "server.app", "--help"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())

    def test_configuration_rejects_non_help_arguments(self):
        invalid_arguments = [["/data"], ["--unknown"]]
        environ = {
            "CHECKOUT_DIR": "/data",
            "REPOSITORY_URL": TEST_REPOSITORY_URL,
        }
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    app.parse_config(arguments, environ)

    def test_snapshot_is_revisioned_immutable_and_excludes_pending(self):
        snapshot = snapshot_with_active_and_pending("b" * 40)
        self.assertEqual(snapshot.revision, "b" * 40)
        self.assertEqual(set(snapshot.banks), {BANK_ID})
        self.assertEqual(json.loads(snapshot.banks_body), [BANK_ID])
        with self.assertRaises(TypeError):
            snapshot.banks[BANK_ID] = None
        with self.assertRaises(TypeError):
            snapshot.banks[BANK_ID].aliases["Other"] = b"{}"
        first = json.loads(snapshot.banks[BANK_ID].categories_body)
        first["categories"][0]["category"] = "Changed"
        second = json.loads(snapshot.banks[BANK_ID].categories_body)
        self.assertEqual(second["categories"][0]["category"], "Fuel")

    def test_empty_snapshot_has_an_empty_bank_array(self):
        snapshot = models.build_snapshot(
            cashbacks.RepositorySources(banks=[], pending_banks=[]), "c" * 40
        )

        self.assertEqual(json.loads(snapshot.banks_body), [])


class HTTPBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.state = StubState(snapshot_with_active_and_pending())
        self.server_context = running_server(self.state)
        self.port = self.server_context.__enter__()

    def tearDown(self):
        self.server_context.__exit__(None, None, None)

    def assert_json_response(self, result, status, body):
        actual_status, headers, payload = result
        self.assertEqual(actual_status, status)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json.loads(payload), body)

    def test_banks_returns_each_active_company_id_as_a_number_once(self):
        self.state.replace_snapshot(snapshot_with_bank_ids(10001, BANK_ID))

        status, headers, payload = request(self.port, "GET", "/banks")

        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        company_ids = json.loads(payload)
        self.assertCountEqual(company_ids, [10001, BANK_ID])
        self.assertTrue(all(type(company_id) is int for company_id in company_ids))

    def test_banks_returns_an_empty_array_for_empty_and_pending_only_snapshots(self):
        pending_only = models.build_snapshot(
            cashbacks.RepositorySources(
                banks=[],
                pending_banks=[
                    cashbacks.PendingBankSource(
                        "Pending-ru", Path("pending"), [fuel_rule()]
                    )
                ],
            ),
            "f" * 40,
        )
        for snapshot in (snapshot_with_bank_ids(), pending_only):
            with self.subTest(revision=snapshot.revision):
                self.state.replace_snapshot(snapshot)
                self.assert_json_response(
                    request(self.port, "GET", "/banks"), 200, []
                )

    def test_banks_reflects_snapshot_replacement(self):
        self.assert_json_response(
            request(self.port, "GET", "/banks"), 200, [BANK_ID]
        )

        self.state.replace_snapshot(snapshot_with_bank_ids(10001))

        self.assert_json_response(
            request(self.port, "GET", "/banks"), 200, [10001]
        )

    def assert_validation_error(self, result):
        self.assertEqual(result[0], 422)
        self.assertTrue(result[1]["content-type"].startswith("application/json"))
        detail = json.loads(result[2])["detail"]
        self.assertIsInstance(detail, list)
        self.assertTrue(detail)

    def test_categories_return_raw_rules_and_absent_bank_is_not_found(self):
        self.assert_json_response(
            request(self.port, "GET", f"/banks/{BANK_ID}/categories"),
            200,
            {
                "company_id": BANK_ID,
                "categories": [
                    fuel_rule(),
                    fuel_rule(["Food", "Café"], 5411),
                ],
            },
        )
        self.assert_json_response(
            request(self.port, "GET", f"/banks/{ABSENT_BANK_ID}/categories"),
            404,
            {"detail": "bank_not_found"},
        )

    def test_pending_only_snapshot_has_no_bank_identity(self):
        self.state.replace_snapshot(
            models.build_snapshot(
                cashbacks.RepositorySources(
                    banks=[],
                    pending_banks=[
                        cashbacks.PendingBankSource(
                            "Pending-ru", Path("pending"), [fuel_rule()]
                        )
                    ],
                ),
                "c" * 40,
            )
        )
        for bank_id in (BANK_ID, ABSENT_BANK_ID):
            with self.subTest(bank_id=bank_id):
                self.assert_json_response(
                    request(self.port, "GET", f"/banks/{bank_id}/categories"),
                    404,
                    {"detail": "bank_not_found"},
                )

    def test_match_normalizes_scalar_and_array_aliases_positionally(self):
        snapshot, rules = snapshot_with_normalized_match_categories()
        self.state.replace_snapshot(snapshot)
        values = [
            "  На всё! ",
            "На всё",
            "Кафе, бары",
            "Кафе бары",
            "Кафе___ \t бары",
            "АЗС",
            "азс",
            "Cafe\u0301",
            "Фаст-фуд",
            "фастфуд",
            "unknown",
            "АЗС",
        ]
        self.assert_json_response(
            request(
                self.port,
                "POST",
                f"/banks/{BANK_ID}/categories/match",
                body=json.dumps(values, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            200,
            [
                rules[0],
                rules[0],
                rules[1],
                rules[1],
                rules[1],
                rules[2],
                rules[2],
                rules[3],
                rules[4],
                rules[4],
                None,
                rules[2],
            ],
        )

    def test_match_does_not_apply_other_category_transformations(self):
        self.state.replace_snapshot(snapshot_with_normalized_match_categories()[0])
        queries = (
            "Ёлка",
            "Kafe",
            "Cafe",
            "Кафе бар",
            "Бары кафе",
            "STRASSE",
        )
        self.assert_json_response(
            request(
                self.port,
                "POST",
                f"/banks/{BANK_ID}/categories/match",
                body=json.dumps(queries, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            200,
            [None] * len(queries),
        )

    def test_empty_normalized_match_queries_are_unmatched(self):
        queries = ["", "!!!", "___"]
        result = request(
            self.port,
            "POST",
            f"/banks/{BANK_ID}/categories/match",
            body=json.dumps(queries).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        self.assert_json_response(result, 200, [None] * len(queries))

    def test_match_requires_non_empty_array_of_strict_strings(self):
        initial_snapshot = self.state.snapshot()
        invalid_bodies = (
            b"",
            b"{",
            b"{}",
            b"[]",
            b'["Fuel",1]',
            b"[true]",
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.assert_validation_error(
                    request(
                        self.port,
                        "POST",
                        f"/banks/{ABSENT_BANK_ID}/categories/match",
                        body=body,
                        headers={"Content-Type": "application/json"},
                    )
                )
                self.assertEqual(self.state.snapshot(), initial_snapshot)

    def test_match_reports_absent_active_bank(self):
        self.assert_json_response(
            request(
                self.port,
                "POST",
                f"/banks/{ABSENT_BANK_ID}/categories/match",
                body=b'["Fuel"]',
                headers={"Content-Type": "application/json"},
            ),
            404,
            {"detail": "bank_not_found"},
        )

    def test_routing_and_path_validation_follow_http_contract(self):
        self.assert_json_response(
            request(self.port, "GET", "/unknown"),
            404,
            {"detail": "Not Found"},
        )
        method_result = request(self.port, "GET", "/sync")
        self.assert_json_response(
            method_result, 405, {"detail": "Method Not Allowed"}
        )
        self.assertEqual(method_result[1]["allow"], "POST")
        banks_method_result = request(self.port, "POST", "/banks")
        self.assert_json_response(
            banks_method_result, 405, {"detail": "Method Not Allowed"}
        )
        self.assertEqual(banks_method_result[1]["allow"], "GET")
        banks_redirect_result = request(self.port, "GET", "/banks/")
        self.assertEqual(banks_redirect_result[0], 307)
        self.assertTrue(banks_redirect_result[1]["location"].endswith("/banks"))
        redirect_result = request(
            self.port, "GET", f"/banks/{BANK_ID}/categories/"
        )
        self.assertEqual(redirect_result[0], 307)
        self.assertTrue(
            redirect_result[1]["location"].endswith(
                f"/banks/{BANK_ID}/categories"
            )
        )
        for bank_id in ("0", "-1", "not-a-number"):
            with self.subTest(bank_id=bank_id):
                self.assert_validation_error(
                    request(self.port, "GET", f"/banks/{bank_id}/categories")
                )
        self.assertEqual(
            request(self.port, "GET", "/banks/05044/categories")[0], 200
        )

    def test_generated_api_description_and_documentation_are_available(self):
        openapi_result = request(self.port, "GET", "/openapi.json")
        self.assertEqual(openapi_result[0], 200)
        self.assertTrue(
            {
                "/sync",
                "/banks",
                "/banks/{company_id}/categories",
                "/banks/{company_id}/categories/match",
            }.issubset(json.loads(openapi_result[2])["paths"])
        )
        for path in ("/docs", "/redoc"):
            with self.subTest(path=path):
                status, headers, body = request(self.port, "GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(headers["content-type"].startswith("text/html"))
                self.assertTrue(body)

    def test_sync_ignores_an_undeclared_body(self):
        self.assert_json_response(
            request(self.port, "POST", "/sync", body=b"ignored"),
            200,
            {"revision": "stub"},
        )
        self.assertEqual(self.state.sync_calls, 1)

    def test_sync_failure_returns_application_detail(self):
        self.state.sync_result = None
        self.assert_json_response(
            request(self.port, "POST", "/sync"),
            503,
            {"detail": "sync_failed"},
        )

    def test_categories_remain_available_while_sync_is_in_progress(self):
        sync_entered = threading.Event()
        release_sync = threading.Event()
        sync_responses = []

        def blocking_sync():
            self.state.sync_calls += 1
            sync_entered.set()
            if not release_sync.wait(5):
                raise RuntimeError("test did not release sync")
            return self.state.sync_result

        with mock.patch.object(self.state, "sync", side_effect=blocking_sync):
            sync_thread = threading.Thread(
                target=lambda: sync_responses.append(
                    request(self.port, "POST", "/sync")
                )
            )
            sync_thread.start()
            try:
                self.assertTrue(sync_entered.wait(5))
                self.assert_json_response(
                    request(self.port, "GET", f"/banks/{BANK_ID}/categories"),
                    200,
                    {
                        "company_id": BANK_ID,
                        "categories": [
                            fuel_rule(),
                            fuel_rule(["Food", "Café"], 5411),
                        ],
                    },
                )
            finally:
                release_sync.set()
                sync_thread.join(timeout=5)

        self.assertFalse(sync_thread.is_alive())
        self.assertEqual(len(sync_responses), 1)
        self.assert_json_response(sync_responses[0], 200, {"revision": "stub"})

    def test_supported_routes_ignore_undeclared_query_parameters(self):
        requests = [
            ("GET", "/banks?ignored=value", None, {}, [BANK_ID]),
            (
                "GET",
                f"/banks/{BANK_ID}/categories?ignored=value",
                None,
                {},
                {"company_id": BANK_ID, "categories": [fuel_rule(), fuel_rule(["Food", "Café"], 5411)]},
            ),
            (
                "POST",
                f"/banks/{BANK_ID}/categories/match?ignored=value",
                b'["Fuel"]',
                {"Content-Type": "application/json"},
                [fuel_rule()],
            ),
            ("POST", "/sync?ignored=value", None, {}, {"revision": "stub"}),
        ]
        for method, path, body, headers, expected in requests:
            with self.subTest(method=method, path=path):
                self.assert_json_response(
                    request(self.port, method, path, body=body, headers=headers),
                    200,
                    expected,
                )


    def test_supported_routes_ignore_authorization(self):
        paths = [
            ("GET", "/banks", None, {}),
            ("GET", f"/banks/{BANK_ID}/categories", None, {}),
            (
                "POST",
                f"/banks/{BANK_ID}/categories/match",
                b'["Fuel"]',
                {"Content-Type": "application/json"},
            ),
            ("POST", "/sync", None, {}),
        ]
        for method, path, body, headers in paths:
            with self.subTest(method=method, path=path):
                plain = request(self.port, method, path, body=body, headers=headers)
                authorized = request(
                    self.port,
                    method,
                    path,
                    body=body,
                    headers={**headers, "Authorization": "Bearer arbitrary"},
                )
                self.assertEqual(
                    (authorized[0], authorized[2]), (plain[0], plain[2])
                )
                self.assertNotIn(authorized[0], (401, 403))


class CheckoutPreparationTest(unittest.TestCase):
    def test_absent_checkout_root_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "checkout"

            prepared = repo_worker._prepare_checkout_root(target)

            self.assertEqual(prepared, target.resolve())
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_existing_checkout_contents_are_completely_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            ordinary = Path(directory) / "checkout"
            (ordinary / "nested").mkdir(parents=True)
            (ordinary / "nested" / "data").write_text("stale", encoding="utf-8")
            (ordinary / "partial.git").mkdir()

            self.assertEqual(
                repo_worker._prepare_checkout_root(ordinary), ordinary.resolve()
            )
            self.assertEqual(list(ordinary.iterdir()), [])

        repository = TemporaryRepository(self)
        tracked = repository.categories / "Fuel.json"
        tracked.write_text("{", encoding="utf-8")
        (repository.seed / "untracked").write_text("stale", encoding="utf-8")
        ignored = repository.seed / "runtime" / "cache"
        ignored.parent.mkdir()
        ignored.write_text("ignored", encoding="utf-8")

        self.assertEqual(
            repo_worker._prepare_checkout_root(repository.seed),
            repository.seed.resolve(),
        )
        self.assertEqual(list(repository.seed.iterdir()), [])

    def test_directory_symlink_is_preserved_and_child_symlink_is_not_traversed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            configured = root / "configured"
            configured.symlink_to(checkout, target_is_directory=True)
            outside = root / "outside"
            outside.mkdir()
            outside_file = outside / "preserve"
            outside_file.write_text("operator data", encoding="utf-8")
            (checkout / "outside-link").symlink_to(
                outside, target_is_directory=True
            )
            (checkout / "nested").mkdir()
            (checkout / "nested" / "stale").write_text("stale", encoding="utf-8")

            prepared = repo_worker._prepare_checkout_root(configured)

            self.assertEqual(prepared, checkout.resolve())
            self.assertTrue(configured.is_symlink())
            self.assertEqual(list(checkout.iterdir()), [])
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "operator data")

    def test_dangling_symlinks_and_non_directory_roots_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_target = root / "file"
            file_target.write_text("preserve", encoding="utf-8")
            file_link = root / "file-link"
            file_link.symlink_to(file_target)
            dangling = root / "dangling"
            dangling.symlink_to(root / "missing")

            for target in (file_target, file_link, dangling):
                with self.subTest(target=target), self.assertRaises(
                    repo_worker.StartupError
                ):
                    repo_worker._prepare_checkout_root(target)

            self.assertEqual(file_target.read_text(encoding="utf-8"), "preserve")
            self.assertTrue(file_link.is_symlink())
            self.assertTrue(dangling.is_symlink())

    def test_creation_listing_and_deletion_failures_are_startup_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            absent = root / "absent"
            with mock.patch.object(
                Path, "mkdir", autospec=True, side_effect=PermissionError("denied")
            ), self.assertRaises(repo_worker.StartupError):
                repo_worker._prepare_checkout_root(absent)

            checkout = root / "checkout"
            checkout.mkdir()
            with mock.patch.object(
                Path, "iterdir", autospec=True, side_effect=PermissionError("denied")
            ), self.assertRaises(repo_worker.StartupError):
                repo_worker._prepare_checkout_root(checkout)

            stale = checkout / "stale"
            stale.write_text("stale", encoding="utf-8")
            with mock.patch.object(
                Path, "unlink", autospec=True, side_effect=PermissionError("denied")
            ), self.assertRaises(repo_worker.StartupError):
                repo_worker._prepare_checkout_root(checkout)
            self.assertTrue(stale.exists())


class BootstrapTest(unittest.TestCase):
    def test_startup_replaces_prior_checkout_with_a_fresh_clone(self):
        repository = TemporaryRepository(self)
        checkout = repository.root / "checkout"
        run_git(
            "clone",
            os.fspath(repository.remote),
            os.fspath(checkout),
            cwd=repository.root,
        )
        tracked = checkout / "src" / f"Bank-ru_{BANK_ID}" / "categories" / "Fuel.json"
        tracked.write_text("{", encoding="utf-8")
        (checkout / "untracked").write_text("stale", encoding="utf-8")
        ignored = checkout / "runtime" / "cache"
        ignored.parent.mkdir()
        ignored.write_text("ignored", encoding="utf-8")
        run_git(
            "remote",
            "set-url",
            "origin",
            "https://example.invalid/stale.git",
            cwd=checkout,
        )
        expected_revision = run_git("rev-parse", "HEAD", cwd=repository.seed)
        config = repository.config(checkout=checkout)

        resolved, snapshot = repo_worker.bootstrap_checkout(config)

        self.assertEqual(resolved, checkout.resolve())
        self.assertEqual(snapshot.revision, expected_revision)
        self.assertEqual(len(snapshot.revision), 40)
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), expected_revision)
        self.assertEqual(
            run_git("remote", "get-url", config.remote, cwd=checkout),
            config.repository_url,
        )
        self.assertFalse((checkout / "untracked").exists())
        self.assertFalse(ignored.exists())

    def test_bootstrap_exact_ref_uses_configured_remote_and_preserves_root(self):
        repository = TemporaryRepository(self)
        run_git("tag", "stable", cwd=repository.seed)
        run_git("push", "origin", "refs/tags/stable", cwd=repository.seed)
        targets = []
        absent = repository.root / "absent"
        targets.append((absent, absent, "main"))
        empty = repository.root / "empty"
        empty.mkdir()
        targets.append((empty, empty, "stable"))
        resolved = repository.root / "resolved"
        resolved.mkdir()
        link = repository.root / "checkout-link"
        link.symlink_to(resolved, target_is_directory=True)
        targets.append((link, resolved, "main"))
        for supplied, expected, ref in targets:
            with self.subTest(target=supplied, ref=ref):
                config = repository.config(
                    checkout=supplied, ref=ref, remote="upstream"
                )
                checkout, snapshot = repo_worker.bootstrap_checkout(config)
                self.assertEqual(checkout, expected.resolve())
                self.assertEqual(
                    snapshot.revision, run_git("rev-parse", "HEAD", cwd=checkout)
                )
                self.assertEqual(
                    run_git("remote", "get-url", config.remote, cwd=checkout),
                    config.repository_url,
                )
        self.assertTrue(link.is_symlink())

    def test_relative_repository_url_uses_process_working_directory(self):
        repository = TemporaryRepository(self)
        target = repository.root / "nested" / "checkout"
        with contextlib.chdir(repository.root):
            checkout, snapshot = repo_worker.bootstrap_checkout(
                repository.config(checkout=target, repository_url="remote.git")
            )
        self.assertEqual(checkout, target.resolve())
        self.assertEqual(snapshot.revision, run_git("rev-parse", "HEAD", cwd=checkout))

    def test_failed_startup_leaves_disposable_data_for_the_next_start(self):
        repository = TemporaryRepository(self)
        (repository.categories / "Fuel.json").write_text("{", encoding="utf-8")
        repository.commit("invalid loader data")
        repository.push()
        checkout = repository.root / "checkout"
        checkout.mkdir()
        stale = checkout / "stale"
        stale.write_text("discard", encoding="utf-8")

        with self.assertRaises(cashbacks.DataError):
            repo_worker.bootstrap_checkout(repository.config(checkout=checkout))

        self.assertFalse(stale.exists())
        self.assertTrue((checkout / ".git").is_dir())
        partial = checkout / "partial-bootstrap"
        partial.write_text("discard on retry", encoding="utf-8")
        repository.replace_fuel("Recovered")
        repository.commit("valid loader data")
        expected_revision = repository.push()

        resolved, snapshot = repo_worker.bootstrap_checkout(
            repository.config(checkout=checkout)
        )

        self.assertEqual(resolved, checkout.resolve())
        self.assertEqual(snapshot.revision, expected_revision)
        self.assertFalse(partial.exists())
        self.assertEqual(category_for_mcc(snapshot), "Recovered")

    def test_clone_failure_leaves_partial_data_for_the_next_startup_reset(self):
        repository = TemporaryRepository(self)
        checkout = repository.root / "checkout"
        checkout.mkdir()
        stale = checkout / "stale"
        stale.write_text("discard", encoding="utf-8")
        revision = "a" * 40
        git = mock.Mock()

        def run_network(arguments, *, transport_url, cwd=None, check=True):
            if arguments[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    arguments, 0, f"{revision}\trefs/heads/main\n", ""
                )
            (checkout / "partial-clone").write_text("partial", encoding="utf-8")
            raise repo_worker.GitCommandError("injected clone failure")

        git.run_network.side_effect = run_network
        with self.assertRaises(repo_worker.GitCommandError):
            repo_worker.bootstrap_checkout(repository.config(checkout=checkout), git=git)

        self.assertFalse(stale.exists())
        self.assertTrue((checkout / "partial-clone").is_file())

    def test_unresolved_and_ambiguous_refs_are_rejected(self):
        repository = TemporaryRepository(self)
        with self.assertRaises(repo_worker.StartupError):
            repo_worker.bootstrap_checkout(
                repository.config(checkout=repository.root / "absent-ref", ref="missing")
            )
        run_git("branch", "same", cwd=repository.seed)
        run_git("tag", "same", cwd=repository.seed)
        run_git(
            "push",
            "origin",
            "refs/heads/same",
            "refs/tags/same",
            cwd=repository.seed,
        )
        with self.assertRaises(repo_worker.StartupError):
            repo_worker.bootstrap_checkout(
                repository.config(checkout=repository.root / "ambiguous", ref="same")
            )


    def test_bootstrap_uses_clean_repository_urls_for_network_commands(self):
        token = "cashbacks token:/?@%"
        encoded_token = "cashbacks%20token%3A%2F%3F%40%25"
        repository_url = "https://github.com/owner/cashbacks.git"
        revision = "a" * 40
        snapshot = snapshot_with_active_and_pending(revision)
        calls = []

        def run(arguments, *, cwd=None, check=True):
            calls.append(("local", arguments))
            if arguments[0] == "rev-parse":
                return subprocess.CompletedProcess(arguments, 0, f"{revision}\n", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def run_network(arguments, *, transport_url, cwd=None, check=True):
            calls.append(("network", arguments, transport_url))
            if arguments[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    arguments, 0, f"{revision}\trefs/heads/main\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            checkout.mkdir()
            config = models.Config(
                checkout,
                repository_url=repository_url,
                github_token=token,
            )
            git = mock.Mock()
            git.run.side_effect = run
            git.run_network.side_effect = run_network
            with mock.patch.object(
                repo_worker, "_require_clean_checkout", return_value=revision
            ), mock.patch.object(repo_worker, "_load_snapshot", return_value=snapshot):
                repo_worker.bootstrap_checkout(config, git)

        network_calls = [
            (call[1], call[2]) for call in calls if call[0] == "network"
        ]
        self.assertEqual(
            {arguments[0] for arguments, _ in network_calls}, {"ls-remote", "clone"}
        )
        for arguments, transport_url in network_calls:
            with self.subTest(operation=arguments[0]):
                self.assertEqual(transport_url, repository_url)
                self.assertIn(repository_url, arguments)
                for secret in (token, encoded_token, "x-access-token", "insteadOf"):
                    self.assertNotIn(secret, " ".join(arguments))


    def test_github_token_configures_each_github_network_command(self):
        token = "cashbacks token:/?@%"
        encoded_token = "cashbacks%20token%3A%2F%3F%40%25"
        repository_url = "https://github.com/owner/cashbacks.git"
        requests = [
            ["ls-remote", "--", repository_url],
            ["clone", "--no-checkout", "--", repository_url, "/checkout"],
            ["fetch", "origin", "+refs/heads/main:refs/cashbacks-service/test"],
        ]
        subprocess_commands = []

        def successful_process(command, **_kwargs):
            subprocess_commands.append(command)
            process = mock.Mock(pid=len(subprocess_commands), returncode=0)
            process.communicate.return_value = "", ""
            return process

        with mock.patch.object(
            repo_worker.subprocess, "Popen", side_effect=successful_process
        ):
            runner = repo_worker.GitRunner(1, github_token=token)
            for arguments in requests:
                runner.run_network(arguments, transport_url=repository_url)

        rewrite = (
            "url.https://x-access-token:"
            f"{encoded_token}@github.com/.insteadOf=https://github.com/"
        )
        for command in subprocess_commands:
            self.assertEqual(command[:3], ["git", "-c", rewrite])
            self.assertIn(command[3:], requests)
        self.assertEqual(
            {tuple(command[3:]) for command in subprocess_commands},
            {tuple(arguments) for arguments in requests},
        )

    def test_command_scoped_github_rewrite_keeps_clean_named_remote(self):
        repository_url = "https://github.com/owner/cashbacks.git"
        token = "cashbacks token:/?@%"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.git"
            checkout = root / "checkout"
            global_config = root / "gitconfig"
            run_git("init", "--bare", os.fspath(source), cwd=root)
            global_config.write_text(
                f'[url "{source.as_uri()}"]\n\tinsteadOf = {repository_url}\n',
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"GIT_CONFIG_GLOBAL": os.fspath(global_config)}
            ):
                repo_worker.GitRunner(30, github_token=token).run_network(
                    [
                        "clone",
                        "--no-checkout",
                        "--origin",
                        "upstream",
                        "--",
                        repository_url,
                        os.fspath(checkout),
                    ],
                    transport_url=repository_url,
                )

            self.assertEqual(
                run_git("config", "--get", "remote.upstream.url", cwd=checkout),
                repository_url,
            )
            persisted_config = (checkout / ".git" / "config").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("insteadOf", persisted_config)
            self.assertNotIn("x-access-token", persisted_config)

    def test_github_token_leaves_other_network_transports_unmodified(self):
        token = "cashbacks token:/?@%"
        cases = [
            (None, "https://github.com/owner/cashbacks.git"),
            (token, "https://example.invalid/owner/cashbacks.git"),
            (token, "http://github.com/owner/cashbacks.git"),
            (token, "https://github.com.evil.invalid/owner/cashbacks.git"),
        ]
        subprocess_commands = []

        def successful_process(command, **_kwargs):
            subprocess_commands.append(command)
            process = mock.Mock(pid=len(subprocess_commands), returncode=0)
            process.communicate.return_value = "", ""
            return process

        with mock.patch.object(
            repo_worker.subprocess, "Popen", side_effect=successful_process
        ):
            for github_token, transport_url in cases:
                with self.subTest(
                    github_token=github_token, transport_url=transport_url
                ):
                    repo_worker.GitRunner(
                        1, github_token=github_token
                    ).run_network(
                        ["ls-remote", "--", transport_url],
                        transport_url=transport_url,
                    )

        self.assertEqual(
            {tuple(command) for command in subprocess_commands},
            {
                ("git", "ls-remote", "--", transport_url)
                for _, transport_url in cases
            },
        )

    def test_git_failure_redacts_raw_and_percent_encoded_github_tokens(self):
        token = "cashbacks token:/?@%"
        encoded_token = "cashbacks%20token%3A%2F%3F%40%25"
        repository_url = "https://github.com/owner/cashbacks.git"
        failure = (
            "fatal: raw "
            f"{token}; encoded {encoded_token}; "
            f"https://x-access-token:{encoded_token}@github.com/owner/cashbacks.git"
        )

        for stdout, stderr in ((failure, ""), ("", failure)):
            with self.subTest(stdout=bool(stdout)):
                process = mock.Mock(returncode=128)
                process.communicate.return_value = stdout, stderr
                with mock.patch.object(
                    repo_worker.subprocess, "Popen", return_value=process
                ):
                    with self.assertRaises(repo_worker.GitCommandError) as raised:
                        repo_worker.GitRunner(
                            1, github_token=token
                        ).run_network(
                            ["ls-remote", "--", repository_url],
                            transport_url=repository_url,
                        )

                error = str(raised.exception)
                self.assertNotIn(token, error)
                self.assertNotIn(encoded_token, error)
                self.assertNotIn("x-access-token", error)
                self.assertIn("github.com/owner/cashbacks.git", error)


    def test_git_timeout_terminates_and_reaps_the_process_group(self):
        process = mock.Mock(pid=1234, returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["git"], 1),
            ("", ""),
        ]

        with mock.patch.object(
            repo_worker.subprocess, "Popen", return_value=process
        ), mock.patch.object(repo_worker.os, "killpg") as killpg:
            with self.assertRaises(repo_worker.GitCommandError):
                repo_worker.GitRunner(1).run(["status"])

        killpg.assert_called_once_with(1234, signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_shutdown_cancels_running_commands_and_rejects_new_ones(self):
        process = mock.Mock(pid=1234, returncode=None)
        command_started = threading.Event()
        process_killed = threading.Event()
        failures = []

        def communicate(*, timeout=None):
            if timeout is not None:
                command_started.set()
                process_killed.wait(5)
            return "", ""

        process.communicate.side_effect = communicate
        runner = repo_worker.GitRunner(30)

        def run_command():
            try:
                runner.run(["status"])
            except repo_worker.GitCommandError as error:
                failures.append(error)

        with mock.patch.object(
            repo_worker.subprocess, "Popen", return_value=process
        ) as popen, mock.patch.object(
            repo_worker.os, "killpg", side_effect=lambda *_: process_killed.set()
        ) as killpg:
            thread = threading.Thread(target=run_command)
            thread.start()
            self.assertTrue(command_started.wait(5))
            runner.shutdown()
            thread.join(timeout=5)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], repo_worker.GitCommandError)
            with self.assertRaises(repo_worker.GitCommandError):
                runner.run(["status"])

        killpg.assert_called_once_with(1234, signal.SIGKILL)
        popen.assert_called_once()

    def test_git_timeout_redacts_repository_url_credentials(self):
        username = "checkout-user-9e10"
        token = "checkout-token-a1b2"
        repository_url = (
            f"https://{username}:{token}@example.invalid/group/cashbacks.git"
        )
        process = mock.Mock(pid=1234, returncode=None)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["git", "clone", repository_url], 1),
            ("", ""),
        ]
        with mock.patch.object(repo_worker.subprocess, "Popen", return_value=process), mock.patch.object(
            repo_worker.os, "killpg"
        ):
            with self.assertRaises(repo_worker.GitCommandError) as raised:
                repo_worker.GitRunner(1).run_network(
                    ["clone", repository_url], transport_url=repository_url
                )
        error = str(raised.exception)
        self.assertNotIn(username, error)
        self.assertNotIn(token, error)
        self.assertNotIn(repository_url, error)
        self.assertIn("example.invalid/group/cashbacks.git", error)

    def test_git_failure_redacts_repository_url_credentials(self):
        username = "checkout-user-9e10"
        token = "checkout-token-a1b2"
        repository_url = (
            f"https://{username}:{token}@example.invalid/group/cashbacks.git"
        )
        process = mock.Mock(returncode=128)
        process.communicate.return_value = (
            "",
            f"fatal: unable to access '{repository_url}': Authentication failed",
        )
        with mock.patch.object(repo_worker.subprocess, "Popen", return_value=process):
            with self.assertRaises(repo_worker.GitCommandError) as raised:
                repo_worker.GitRunner(1).run_network(
                    ["clone", repository_url], transport_url=repository_url
                )
        error = str(raised.exception)
        self.assertNotIn(username, error)
        self.assertNotIn(token, error)
        self.assertNotIn(repository_url, error)
        self.assertIn("example.invalid/group/cashbacks.git", error)


class DelegatingGit:
    def __init__(self, base):
        self.base = base

    def run(self, arguments, *, cwd=None, check=True):
        return self.base.run(arguments, cwd=cwd, check=check)

    def run_network(self, arguments, *, transport_url, cwd=None, check=True):
        return self.base.run_network(
            arguments, transport_url=transport_url, cwd=cwd, check=check
        )


class FailAfterCleanupGit(DelegatingGit):
    def __init__(self, base):
        super().__init__(base)
        self.failed = False

    def run(self, arguments, *, cwd=None, check=True):
        result = super().run(arguments, cwd=cwd, check=check)
        if arguments[:2] == ["worktree", "remove"] and not self.failed:
            self.failed = True
            raise repo_worker.GitCommandError("injected cleanup failure")
        return result


class ActivationFailureGit(DelegatingGit):
    def __init__(self, base, old_revision, *, rollback_fails):
        super().__init__(base)
        self.old_revision = old_revision
        self.rollback_fails = rollback_fails
        self.activation_failed = False

    def run(self, arguments, *, cwd=None, check=True):
        if arguments and arguments[0] == "checkout":
            revision = arguments[-1]
            if revision != self.old_revision and not self.activation_failed:
                self.activation_failed = True
                if self.rollback_fails:
                    super().run(arguments, cwd=cwd, check=check)
                raise repo_worker.GitCommandError("injected activation failure")
            if revision == self.old_revision and self.activation_failed and self.rollback_fails:
                raise repo_worker.GitCommandError("injected rollback failure")
        return super().run(arguments, cwd=cwd, check=check)


class PausingActivationGit(DelegatingGit):
    def __init__(self, base, old_revision):
        super().__init__(base)
        self.old_revision = old_revision
        self.reached = threading.Event()
        self.release = threading.Event()

    def run(self, arguments, *, cwd=None, check=True):
        if arguments and arguments[0] == "checkout" and arguments[-1] != self.old_revision:
            self.reached.set()
            if not self.release.wait(5):
                raise RuntimeError("test did not release activation")
        return super().run(arguments, cwd=cwd, check=check)


class ConcurrencyGit(DelegatingGit):
    def __init__(self, base):
        super().__init__(base)
        self._lock = threading.Lock()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum_active = 0
        self.calls = 0

    def run_network(self, arguments, *, transport_url, cwd=None, check=True):
        if arguments and arguments[0] == "ls-remote":
            with self._lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                self.calls += 1
                call_number = self.calls
            try:
                if call_number == 1:
                    self.entered.set()
                    if not self.release.wait(5):
                        raise RuntimeError("test did not release first sync")
                return super().run_network(
                    arguments, transport_url=transport_url, cwd=cwd, check=check
                )
            finally:
                with self._lock:
                    self.active -= 1
        return super().run_network(
            arguments, transport_url=transport_url, cwd=cwd, check=check
        )


class SynchronizationTest(unittest.TestCase):
    def test_sync_uses_configured_remote_as_clean_github_transport(self):
        token = "cashbacks token:/?@%"
        repository_url = "https://github.com/owner/cashbacks.git"
        remote = "upstream"
        revision = "b" * 40
        network_calls = []

        def run(arguments, *, cwd=None, check=True):
            if arguments[:3] == ["remote", "get-url", remote]:
                return subprocess.CompletedProcess(
                    arguments, 0, f"{repository_url}\n", ""
                )
            if arguments[0] == "rev-parse":
                return subprocess.CompletedProcess(arguments, 0, f"{revision}\n", "")
            return subprocess.CompletedProcess(arguments, 0, "", "")

        def run_network(arguments, *, transport_url, cwd=None, check=True):
            network_calls.append((arguments, transport_url))
            if arguments[0] == "ls-remote":
                return subprocess.CompletedProcess(
                    arguments, 0, f"{revision}\trefs/heads/main\n", ""
                )
            return subprocess.CompletedProcess(arguments, 0, "", "")

        git = mock.Mock()
        git.run.side_effect = run
        git.run_network.side_effect = run_network
        state = repo_worker.ServiceState(
            models.Config(
                Path("."),
                repository_url=TEST_REPOSITORY_URL,
                github_token=token,
                remote=remote,
            ),
            Path("."),
            snapshot_with_active_and_pending(),
            git=git,
        )

        resolved_revision, _ = state._fetch_candidate()

        self.assertEqual(resolved_revision, revision)
        self.assertEqual(
            [arguments[0] for arguments, _ in network_calls],
            ["ls-remote", "fetch"],
        )
        for arguments, transport_url in network_calls:
            with self.subTest(operation=arguments[0]):
                self.assertEqual(transport_url, repository_url)
                self.assertIn(remote, arguments)
                self.assertNotIn(repository_url, arguments)
        self.assertNotIn(
            token, " ".join(" ".join(arguments) for arguments, _ in network_calls)
        )


    def test_sync_publishes_full_revision_and_preserves_ignored_files(self):
        repository = TemporaryRepository(self)
        state, checkout = repository.cloned_state()
        ignored = checkout / "runtime" / "cache"
        ignored.parent.mkdir()
        ignored.write_text("preserve", encoding="utf-8")
        candidate = repository.update_fuel("Transport")
        self.assertEqual(state.sync(), candidate)
        self.assertEqual(state.snapshot().revision, candidate)
        self.assertEqual(category_for_mcc(state.snapshot()), "Transport")
        self.assertEqual(ignored.read_text(encoding="utf-8"), "preserve")
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), candidate)

    def test_sync_rejects_a_ref_that_becomes_ambiguous(self):
        repository = TemporaryRepository(self)
        state, checkout = repository.cloned_state()
        old = state.snapshot()
        run_git("tag", "main", cwd=repository.seed)
        run_git("push", "origin", "refs/tags/main", cwd=repository.seed)
        self.assertIsNone(state.sync())
        self.assertEqual(state.snapshot(), old)
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), old.revision)


    def test_ignored_collision_fails_before_activation_and_preserves_old_snapshot(self):
        repository = TemporaryRepository(self)
        ignored_relative = f"src/Bank-ru_{BANK_ID}/categories/Collision.json"
        with (repository.seed / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write(ignored_relative + "\n")
        repository.commit("ignore future collision")
        repository.push()
        state, checkout = repository.cloned_state()
        ignored = checkout / ignored_relative
        ignored.write_text("operator data", encoding="utf-8")
        repository.write_rules([("Collision.json", fuel_rule("Collision", 4111))])
        repository.commit("candidate collision", force_paths=(ignored_relative,))
        repository.push()
        old = state.snapshot()
        self.assertIsNone(state.sync())
        self.assertEqual(state.snapshot(), old)
        self.assertEqual(ignored.read_text(encoding="utf-8"), "operator data")
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), old.revision)

    def test_empty_ignored_directory_collision_fails_before_activation(self):
        repository = TemporaryRepository(self)
        state, checkout = repository.cloned_state()
        ignored_directory = checkout / "runtime" / "cache"
        ignored_directory.mkdir(parents=True)
        candidate_path = repository.seed / "runtime" / "cache"
        candidate_path.parent.mkdir()
        candidate_path.write_text("candidate", encoding="utf-8")
        repository.commit("candidate replaces ignored directory", force_paths=("runtime/cache",))
        repository.push()
        old = state.snapshot()
        self.assertIsNone(state.sync())
        self.assertEqual(state.snapshot(), old)
        self.assertTrue(ignored_directory.is_dir())
        self.assertEqual(list(ignored_directory.iterdir()), [])
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), old.revision)

    def test_invalid_candidate_and_cleanup_failure_keep_old_snapshot_and_later_sync_works(self):
        repository = TemporaryRepository(self)
        state, checkout = repository.cloned_state()
        old = state.snapshot()
        (repository.categories / "Fuel.json").write_text(
            '{"category":\n  }\n', encoding="utf-8"
        )
        repository.commit("invalid candidate")
        repository.push()
        diagnostics = io.StringIO()
        with running_server(state) as port:
            with contextlib.redirect_stderr(diagnostics):
                status, _, payload = request(port, "POST", "/sync")
            self.assertEqual((status, json.loads(payload)), (503, {"detail": "sync_failed"}))
            status, _, payload = request(port, "GET", f"/banks/{BANK_ID}/categories")
            self.assertEqual(
                (status, json.loads(payload)),
                (200, json.loads(old.banks[BANK_ID].categories_body)),
            )
        self.assertIn(f"src/Bank-ru_{BANK_ID}/categories/Fuel.json", diagnostics.getvalue())
        self.assertRegex(diagnostics.getvalue(), r"(?::2:3\b|\bline\s+2\b.*\bcolumn\s+3\b)")

        candidate = repository.update_fuel("Travel")
        state._git = FailAfterCleanupGit(repo_worker.GitRunner(30))
        self.assertIsNone(state.sync())
        self.assertEqual(state.snapshot(), old)
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), old.revision)
        self.assertEqual(state.sync(), candidate)
        self.assertEqual(category_for_mcc(state.snapshot()), "Travel")

    def test_sync_diagnostics_redact_credentials_through_exception_chain(self):
        repository = TemporaryRepository(self)
        token = "sync-test-token:/?@%"
        encoded_token = quote(token, safe="")
        username = "sync-test-user-9f4d"
        password = "sync-test-password-2a7c"
        repository_url = f"https://{username}:{password}@example.invalid/private.git"
        config = repository.config(
            checkout=repository.root / "checkout", github_token=token
        )
        checkout, snapshot = repo_worker.bootstrap_checkout(config)
        state = repo_worker.ServiceState(config, checkout, snapshot)
        failure_message = f"{token} {encoded_token} {repository_url}"

        def fail_load_repository(_checkout):
            try:
                raise OSError(failure_message)
            except OSError as exc:
                raise cashbacks.DataError("source loading rejected") from exc

        diagnostics = io.StringIO()
        with mock.patch.object(
            cashbacks, "load_repository", side_effect=fail_load_repository
        ), contextlib.redirect_stderr(diagnostics):
            self.assertIsNone(state.sync())
        diagnostic = diagnostics.getvalue()
        self.assertIn("source loading rejected", diagnostic)
        self.assertIn("example.invalid/private.git", diagnostic)
        self.assertFalse(
            any(secret in diagnostic for secret in (token, encoded_token, username, password)),
            "sync diagnostics leaked credentials",
        )

    def test_sync_reports_dirty_or_diverged_checkout_without_replacing_snapshot(self):
        for checkout_change in ("dirty", "diverged"):
            with self.subTest(checkout_change=checkout_change):
                repository = TemporaryRepository(self)
                state, checkout = repository.cloned_state()
                old = state.snapshot()
                if checkout_change == "dirty":
                    (checkout / "local-only").write_text("preserve", encoding="utf-8")
                else:
                    run_git(
                        "-c", "user.name=Cashbacks Test",
                        "-c", "user.email=cashbacks@example.invalid",
                        "commit", "--allow-empty", "-m", "diverged checkout",
                        cwd=checkout,
                    )
                diagnostics = io.StringIO()
                with running_server(state) as port:
                    with contextlib.redirect_stderr(diagnostics):
                        status, _, payload = request(port, "POST", "/sync")
                self.assertEqual(
                    (status, json.loads(payload)), (503, {"detail": "sync_failed"})
                )
                self.assertRegex(
                    diagnostics.getvalue(), r"(?i)checkout.*(?:clean|revision|diverg)"
                )
                self.assertEqual(state.snapshot(), old)

    def test_activation_failure_rolls_back_or_latches_reconciliation(self):
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                repository = TemporaryRepository(self)
                state, checkout = repository.cloned_state()
                old = state.snapshot()
                candidate = repository.update_fuel("Airlines")
                state._git = ActivationFailureGit(
                    repo_worker.GitRunner(30), old.revision, rollback_fails=rollback_fails
                )
                self.assertIsNone(state.sync())
                self.assertEqual(state.snapshot(), old)
                self.assertEqual(category_for_mcc(old), "Fuel")
                if rollback_fails:
                    self.assertTrue(state.reconciliation_required)
                    self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), candidate)
                    self.assertIsNone(state.sync())
                    self.assertTrue(state.reconciliation_required)
                else:
                    self.assertFalse(state.reconciliation_required)
                    self.assertEqual(run_git("rev-parse", "HEAD", cwd=checkout), old.revision)

    def test_reads_see_complete_snapshots_around_activation(self):
        repository = TemporaryRepository(self)
        state, _ = repository.cloned_state()
        old = state.snapshot()
        candidate = repository.update_fuel("New")
        pausing = PausingActivationGit(repo_worker.GitRunner(30), old.revision)
        state._git = pausing
        result = []
        thread = threading.Thread(target=lambda: result.append(state.sync()))
        thread.start()
        self.assertTrue(pausing.reached.wait(5))
        self.assertEqual(state.snapshot(), old)
        self.assertEqual(category_for_mcc(state.snapshot()), "Fuel")
        pausing.release.set()
        thread.join(timeout=10)
        self.assertEqual(result[0], candidate)
        self.assertEqual(state.snapshot().revision, candidate)
        self.assertEqual(category_for_mcc(state.snapshot()), "New")

    def test_concurrent_syncs_serialize_without_interleaved_git(self):
        class ObservableLock:
            def __init__(self, lock):
                self._lock = lock
                self.waiting = threading.Event()

            def __enter__(self):
                if not self._lock.acquire(blocking=False):
                    self.waiting.set()
                    self._lock.acquire()
                return self

            def __exit__(self, *_args):
                self._lock.release()

        repository = TemporaryRepository(self)
        state, _ = repository.cloned_state()
        candidate = repository.update_fuel("Serialized")
        concurrent_git = ConcurrencyGit(repo_worker.GitRunner(30))
        observable_lock = ObservableLock(state._sync_lock)
        state._git = concurrent_git
        state._sync_lock = observable_lock
        results = []

        def synchronize():
            results.append(state.sync())

        first = threading.Thread(target=synchronize)
        second = threading.Thread(target=synchronize)
        second_started = False
        first.start()
        try:
            self.assertTrue(concurrent_git.entered.wait(5))
            second.start()
            second_started = True
            self.assertTrue(observable_lock.waiting.wait(5))
            self.assertEqual(concurrent_git.maximum_active, 1)
        finally:
            concurrent_git.release.set()
            first.join(timeout=15)
            if second_started:
                second.join(timeout=15)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, [candidate, candidate])
        self.assertEqual(concurrent_git.maximum_active, 1)

    def test_sync_never_executes_fetched_service_code(self):
        repository = TemporaryRepository(self)
        server = repository.seed / "server"
        server.mkdir()
        (server / "app.py").write_text("ORIGIN = True\n", encoding="utf-8")
        repository.commit("data checkout application")
        repository.push()
        state, _ = repository.cloned_state()
        (server / "app.py").write_text(
            'raise RuntimeError("must not execute fetched code")\n', encoding="utf-8"
        )
        repository.replace_fuel("Safe data")
        repository.commit("data and hostile application update")
        candidate = repository.push()
        self.assertEqual(state.sync(), candidate)
        self.assertEqual(category_for_mcc(state.snapshot()), "Safe data")


class ServiceStartupTest(unittest.TestCase):
    def test_main_starts_the_bootstrapped_application_on_the_configured_address(self):
        config = models.Config(
            Path("."),
            repository_url=TEST_REPOSITORY_URL,
            host="127.0.0.1",
            port=9123,
            github_token="token",
        )
        git = mock.Mock()
        snapshot = models.Snapshot(
            revision="revision", banks={}, banks_body=b"[]"
        )
        with mock.patch.object(
            app, "parse_config", return_value=config
        ), mock.patch.object(
            repo_worker, "GitRunner", return_value=git
        ) as runner, mock.patch.object(
            repo_worker,
            "bootstrap_checkout",
            return_value=(Path("."), snapshot),
        ) as bootstrap, mock.patch.object(app.uvicorn, "run") as run:
            self.assertEqual(app.main([]), 0)

        application = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs["host"], config.host)
        self.assertEqual(run.call_args.kwargs["port"], config.port)
        self.assertEqual(run.call_args.kwargs["workers"], 1)
        bootstrap.assert_called_once_with(config, git)
        git.shutdown.assert_called_once_with()
        runner.assert_called_once_with(
            config.git_timeout_seconds, github_token=config.github_token
        )
        self.assertTrue(
            any(
                getattr(route, "path", None) == "/sync"
                for route in application.routes
            )
        )

    def test_main_does_not_listen_when_bootstrap_fails(self):
        config = models.Config(Path("."), repository_url=TEST_REPOSITORY_URL)
        git = mock.Mock()
        with mock.patch.object(
            app, "parse_config", return_value=config
        ), mock.patch.object(
            repo_worker, "GitRunner", return_value=git
        ), mock.patch.object(
            repo_worker,
            "bootstrap_checkout",
            side_effect=repo_worker.StartupError("invalid checkout"),
        ), mock.patch.object(app.uvicorn, "run") as run, contextlib.redirect_stderr(
            io.StringIO()
        ):
            self.assertEqual(app.main([]), 1)

        run.assert_not_called()
        git.shutdown.assert_called_once_with()

    def test_main_cleans_up_after_uvicorn_failure(self):
        config = models.Config(Path("."), repository_url=TEST_REPOSITORY_URL)
        git = mock.Mock()
        with mock.patch.object(
            app, "parse_config", return_value=config
        ), mock.patch.object(
            repo_worker, "GitRunner", return_value=git
        ), mock.patch.object(
            repo_worker,
            "bootstrap_checkout",
            return_value=(
                Path("."),
                models.Snapshot(revision="revision", banks={}, banks_body=b"[]"),
            ),
        ), mock.patch.object(
            app.uvicorn, "run", side_effect=OSError("listener failed")
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(app.main([]), 1)

        git.shutdown.assert_called_once_with()

if __name__ == "__main__":
    unittest.main()
