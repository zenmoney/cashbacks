"""Git checkout management and synchronized snapshot publishing."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from typing import Sequence
from urllib.parse import quote
import uuid

from scripts import cashbacks
from .models import (
    Config,
    RefSelection,
    Snapshot,
    build_snapshot,
)


_HTTP_URL_WITH_USERINFO = re.compile(
    r"(?P<scheme>https?://)[^/\s]*@(?P<location>[^\s'\"<>]*)",
    re.IGNORECASE,
)


def _redact_http_url_userinfo(value: str) -> str:
    """Replace HTTP(S) URL credentials while retaining the repository location."""
    return _HTTP_URL_WITH_USERINFO.sub(
        r"\g<scheme>[redacted]@\g<location>",
        value,
    )


def _redact_git_value(value: str, secrets: Sequence[str] = ()) -> str:
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    return _redact_http_url_userinfo(value)


def _render_git_command(
    command: Sequence[str], secrets: Sequence[str] = ()
) -> str:
    return " ".join(_redact_git_value(argument, secrets) for argument in command)


class StartupError(Exception):
    """The checkout or startup configuration is not safe to serve."""


class GitCommandError(Exception):
    """A Git subprocess failed or exceeded its deadline."""


class SyncFailure(Exception):
    """A synchronization attempt failed without replacing the snapshot."""


class GitRunner:
    """Run shell-free Git commands and own their process-group lifetime."""

    _SHUTDOWN_MESSAGE = "Git runner is shut down"

    def __init__(
        self, timeout_seconds: int, github_token: str | None = None
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._github_token = github_token
        self._encoded_github_token = (
            quote(github_token, safe="") if github_token else None
        )
        self._lock = threading.Lock()
        self._cancelled = False
        self._active_process_groups: dict[
            int, tuple[subprocess.Popen[str], threading.Event, int]
        ] = {}

    @staticmethod
    def _terminate_process_group(process_group: int) -> None:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _reap(process: subprocess.Popen[str]) -> tuple[str, str]:
        while True:
            try:
                return process.communicate()
            except BaseException:
                pass

    def _is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _release(self, process: subprocess.Popen[str]) -> bool:
        with self._lock:
            active = self._active_process_groups.get(process.pid)
            if active is not None and active[0] is process:
                del self._active_process_groups[process.pid]
                active[1].set()
            return self._cancelled

    def shutdown(self) -> None:
        """Permanently prevent new commands and reap active process groups."""
        with self._lock:
            should_terminate = not self._cancelled
            self._cancelled = True
            active_processes = tuple(self._active_process_groups.items())
            if should_terminate:
                for process_group, (process, _, _) in active_processes:
                    if process.returncode is None:
                        self._terminate_process_group(process_group)

        current_thread = threading.get_ident()
        for _, (_, reaped, owner_thread) in active_processes:
            if owner_thread != current_thread:
                reaped.wait()

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(arguments, cwd=cwd, check=check, transport_url=None)

    def run_network(
        self,
        arguments: Sequence[str],
        *,
        transport_url: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            arguments, cwd=cwd, check=check, transport_url=transport_url
        )

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None,
        check: bool,
        transport_url: str | None,
    ) -> subprocess.CompletedProcess[str]:
        clean_command = ["git", *arguments]
        command = clean_command
        if (
            self._encoded_github_token is not None
            and transport_url is not None
            and transport_url.startswith("https://github.com/")
        ):
            command = [
                "git",
                "-c",
                "url.https://x-access-token:"
                f"{self._encoded_github_token}@github.com/.insteadOf=https://github.com/",
                *arguments,
            ]
        process: subprocess.Popen[str] | None = None
        completed = False
        try:
            with self._lock:
                if self._cancelled:
                    raise GitCommandError(self._SHUTDOWN_MESSAGE)
                process = subprocess.Popen(
                    command,
                    cwd=os.fspath(cwd) if cwd is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="surrogateescape",
                    start_new_session=True,
                )
                self._active_process_groups[process.pid] = (
                    process,
                    threading.Event(),
                    threading.get_ident(),
                )

            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            completed = True
            if self._release(process):
                raise GitCommandError(self._SHUTDOWN_MESSAGE)
            result = subprocess.CompletedProcess(
                clean_command, process.returncode, stdout, stderr
            )
            if check and result.returncode != 0:
                detail = (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or "Git command failed"
                )
                raise GitCommandError(
                    f"{_redact_git_value(detail, self._redaction_secrets)} "
                    f"({_render_git_command(command, self._redaction_secrets)})"
                )
            return result
        except subprocess.TimeoutExpired:
            if process is not None:
                self._terminate_process_group(process.pid)
                self._reap(process)
            raise GitCommandError(
                f"Git command timed out after {self.timeout_seconds} seconds: "
                f"{_render_git_command(command, self._redaction_secrets)}"
            ) from None
        except BaseException:
            if process is not None and not completed:
                if not self._is_cancelled():
                    self._terminate_process_group(process.pid)
                self._reap(process)
            raise
        finally:
            if process is not None:
                self._release(process)


    @property
    def _redaction_secrets(self) -> tuple[str, ...]:
        return tuple(
            secret
            for secret in (self._github_token, self._encoded_github_token)
            if secret is not None
        )

def _prepare_checkout_root(target: Path) -> Path:
    target = target.expanduser()
    try:
        try:
            target_status = target.lstat()
        except FileNotFoundError:
            target.mkdir(parents=True)
            root = target.resolve(strict=True)
        else:
            if stat.S_ISLNK(target_status.st_mode):
                root = target.resolve(strict=True)
                if not stat.S_ISDIR(root.stat().st_mode):
                    raise StartupError(
                        "checkout target symlink must resolve to a directory"
                    )
            elif stat.S_ISDIR(target_status.st_mode):
                root = target.resolve(strict=True)
            else:
                raise StartupError(
                    "checkout target must be a directory or a symlink to one"
                )

        for child in root.iterdir():
            child_status = child.lstat()
            if stat.S_ISDIR(child_status.st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()
        if next(root.iterdir(), None) is not None:
            raise StartupError(f"checkout target is not empty after clearing: {root}")
    except StartupError:
        raise
    except (OSError, RuntimeError) as exc:
        raise StartupError(f"cannot prepare checkout target {target}: {exc}") from exc
    return root


def _git_head(git: GitRunner, checkout: Path) -> str:
    return git.run(
        ["rev-parse", "--verify", "HEAD^{commit}"], cwd=checkout
    ).stdout.strip()


def _is_standard_clean(git: GitRunner, checkout: Path) -> bool:
    tracked = git.run(["ls-files", "-v", "-z"], cwd=checkout).stdout
    if any(record and not record.startswith("H ") for record in tracked.split("\0")):
        return False
    status = git.run(
        ["status", "--porcelain=v1", "--untracked-files=normal"], cwd=checkout
    )
    return status.stdout == ""


def _require_clean_checkout(git: GitRunner, checkout: Path) -> str:
    inside = git.run(
        ["rev-parse", "--is-inside-work-tree"], cwd=checkout
    ).stdout.strip()
    if inside != "true":
        raise StartupError("checkout target is not a Git working tree")
    top_level = Path(
        git.run(["rev-parse", "--show-toplevel"], cwd=checkout).stdout.strip()
    ).resolve(strict=True)
    if top_level != checkout.resolve(strict=True):
        raise StartupError("checkout target must be the Git working-tree root")
    revision = _git_head(git, checkout)
    if not revision or not _is_standard_clean(git, checkout):
        raise StartupError("checkout target must have a committed standard-clean HEAD")
    return revision


def _select_remote_ref(
    git: GitRunner,
    repository: str,
    ref: str,
    *,
    transport_url: str,
    cwd: Path | None = None,
) -> RefSelection:
    result = git.run_network(
        [
            "ls-remote",
            "--heads",
            "--tags",
            "--refs",
            "--",
            repository,
            f"refs/heads/{ref}",
            f"refs/tags/{ref}",
        ],
        transport_url=transport_url,
        cwd=cwd,
    )
    names = {
        line.split("\t", 1)[1]
        for line in result.stdout.splitlines()
        if "\t" in line
    }
    branch = f"refs/heads/{ref}"
    tag = f"refs/tags/{ref}"
    matches = [name for name in (branch, tag) if name in names]
    if len(matches) != 1:
        if not matches:
            raise StartupError(f"configured ref does not name a branch or tag: {ref}")
        raise StartupError(f"configured ref is ambiguous between branch and tag: {ref}")
    full_name = matches[0]
    return RefSelection(
        kind="branch" if full_name == branch else "tag", full_name=full_name
    )


def _load_snapshot(checkout: Path, revision: str) -> Snapshot:
    sources = cashbacks.load_repository(checkout)
    return build_snapshot(sources, revision)


def _bootstrap_clone(
    config: Config, git: GitRunner, checkout: Path
) -> tuple[Path, Snapshot]:
    selection = _select_remote_ref(
        git,
        config.repository_url,
        config.ref,
        transport_url=config.repository_url,
    )
    git.run_network(
        [
            "clone",
            "--no-checkout",
            "--origin",
            config.remote,
            "--",
            config.repository_url,
            os.fspath(checkout),
        ],
        transport_url=config.repository_url,
    )
    if selection.kind == "branch":
        local_ref = f"refs/remotes/{config.remote}/{config.ref}^{{commit}}"
    else:
        local_ref = f"refs/tags/{config.ref}^{{commit}}"
    revision = git.run(
        ["rev-parse", "--verify", local_ref], cwd=checkout
    ).stdout.strip()
    git.run(
        ["checkout", "--detach", "--force", "--no-overwrite-ignore", revision],
        cwd=checkout,
    )
    verified_revision = _require_clean_checkout(git, checkout)
    if verified_revision != revision:
        raise StartupError("bootstrapped checkout did not resolve to the selected ref")
    snapshot = _load_snapshot(checkout, revision)
    return checkout, snapshot


def bootstrap_checkout(
    config: Config, git: GitRunner | None = None
) -> tuple[Path, Snapshot]:
    git = git or GitRunner(
        config.git_timeout_seconds, github_token=config.github_token
    )
    checkout = _prepare_checkout_root(config.checkout)
    return _bootstrap_clone(config, git, checkout)


def _paths_collide(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


def _preflight_ignored_collisions(
    git: GitRunner, checkout: Path, revision: str
) -> None:
    candidate_paths = [
        path
        for path in git.run(
            ["ls-tree", "-r", "--name-only", "-z", revision], cwd=checkout
        ).stdout.split("\0")
        if path
    ]
    ignored_paths = [
        path.removesuffix("/")
        for path in git.run(
            [
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
            cwd=checkout,
        ).stdout.split("\0")
        if path
    ]
    if any(
        _paths_collide(candidate, ignored)
        for candidate in candidate_paths
        for ignored in ignored_paths
    ):
        raise SyncFailure("candidate paths collide with ignored checkout paths")


def _verify_checkout_revision(
    git: GitRunner, checkout: Path, revision: str
) -> bool:
    try:
        return _git_head(git, checkout) == revision and _is_standard_clean(git, checkout)
    except (GitCommandError, OSError):
        return False


def _stage_snapshot(
    git: GitRunner, checkout: Path, revision: str
) -> Snapshot:
    staging_root = Path(tempfile.mkdtemp(prefix="cashbacks-service-stage-"))
    staging_path = staging_root / "worktree"
    added = False
    failure: BaseException | None = None
    snapshot: Snapshot | None = None
    try:
        git.run(
            ["worktree", "add", "--detach", os.fspath(staging_path), revision],
            cwd=checkout,
        )
        added = True
        snapshot = _load_snapshot(staging_path, revision)
    except BaseException as exc:
        failure = exc
    finally:
        try:
            if added:
                git.run(
                    ["worktree", "remove", "--force", os.fspath(staging_path)],
                    cwd=checkout,
                )
                staging_root.rmdir()
            elif staging_root.exists():
                shutil.rmtree(staging_root)
        except BaseException as cleanup_exc:
            failure = cleanup_exc
    if failure is not None:
        raise SyncFailure("candidate staging or validation failed") from failure
    if snapshot is None:
        raise SyncFailure("candidate staging produced no snapshot")
    return snapshot


class ServiceState:
    def __init__(
        self,
        config: Config,
        checkout: Path,
        snapshot: Snapshot,
        *,
        git: GitRunner | None = None,
    ) -> None:
        self.config = config
        self.checkout = checkout
        self._snapshot = snapshot
        self._git = git or GitRunner(
            config.git_timeout_seconds, github_token=config.github_token
        )
        self._state_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._reconciliation_required = False
        self._shutting_down = False

    def snapshot(self) -> Snapshot:
        with self._state_lock:
            return self._snapshot

    @property
    def reconciliation_required(self) -> bool:
        with self._state_lock:
            return self._reconciliation_required

    def _set_reconciliation_required(self, value: bool) -> None:
        with self._state_lock:
            self._reconciliation_required = value

    def _publish(self, snapshot: Snapshot) -> None:
        with self._state_lock:
            self._snapshot = snapshot
            self._reconciliation_required = False

    def _is_shutting_down(self) -> bool:
        with self._state_lock:
            return self._shutting_down

    def shutdown(self) -> None:
        self._git.shutdown()
        with self._state_lock:
            self._shutting_down = True

    def _fetch_candidate(self) -> tuple[str, str]:
        remote_url = self._git.run(
            ["remote", "get-url", self.config.remote], cwd=self.checkout
        ).stdout.strip()
        selection = _select_remote_ref(
            self._git,
            self.config.remote,
            self.config.ref,
            cwd=self.checkout,
            transport_url=remote_url,
        )
        private_ref = f"refs/cashbacks-service/{uuid.uuid4().hex}"
        try:
            self._git.run_network(
                [
                    "fetch",
                    "--no-tags",
                    self.config.remote,
                    f"+{selection.full_name}:{private_ref}",
                ],
                cwd=self.checkout,
                transport_url=remote_url,
            )
            revision = self._git.run(
                ["rev-parse", "--verify", f"{private_ref}^{{commit}}"],
                cwd=self.checkout,
            ).stdout.strip()
        except (GitCommandError, OSError):
            try:
                self._git.run(
                    ["update-ref", "-d", private_ref], cwd=self.checkout
                )
            except (GitCommandError, OSError):
                pass
            raise
        return revision, private_ref

    def _restore(self, revision: str) -> bool:
        try:
            _preflight_ignored_collisions(self._git, self.checkout, revision)
            self._git.run(
                [
                    "checkout",
                    "--detach",
                    "--force",
                    "--no-overwrite-ignore",
                    revision,
                ],
                cwd=self.checkout,
            )
        except (GitCommandError, OSError, SyncFailure):
            return False
        return _verify_checkout_revision(self._git, self.checkout, revision)

    def _report_sync_failure(self, detail: str) -> None:
        token = self.config.github_token or ""
        print(
            _redact_git_value(
                f"cashbacks-service: sync failed: {detail}",
                (token, quote(token, safe="")),
            ),
            file=sys.stderr,
        )

    def sync(self) -> str | None:
        if self._is_shutting_down():
            self._report_sync_failure("shutdown is in progress")
            return None
        with self._sync_lock:
            if self._is_shutting_down():
                self._report_sync_failure("shutdown is in progress")
                return None
            previous = self.snapshot()
            if not _verify_checkout_revision(
                self._git, self.checkout, previous.revision
            ):
                self._report_sync_failure(
                    "checkout is not clean at the published revision "
                    "or could not be verified"
                )
                return None
            if self.reconciliation_required:
                self._set_reconciliation_required(False)

            private_ref: str | None = None
            activation_started = False
            try:
                candidate_revision, private_ref = self._fetch_candidate()
                candidate = _stage_snapshot(
                    self._git, self.checkout, candidate_revision
                )
                self._git.run(
                    ["update-ref", "-d", private_ref], cwd=self.checkout
                )
                private_ref = None
                _preflight_ignored_collisions(
                    self._git, self.checkout, candidate_revision
                )
                activation_started = True
                self._git.run(
                    [
                        "checkout",
                        "--detach",
                        "--force",
                        "--no-overwrite-ignore",
                        candidate_revision,
                    ],
                    cwd=self.checkout,
                )
                if not _verify_checkout_revision(
                    self._git, self.checkout, candidate_revision
                ):
                    raise SyncFailure("candidate activation verification failed")
                self._publish(candidate)
                return candidate_revision
            except (
                GitCommandError,
                OSError,
                StartupError,
                cashbacks.DataError,
                SyncFailure,
            ) as exc:
                if activation_started and not self._restore(previous.revision):
                    self._set_reconciliation_required(True)
                    self._report_sync_failure(
                        "rollback failed; checkout requires reconciliation"
                    )
                self._report_sync_failure(
                    "".join(traceback.format_exception(exc)).rstrip()
                )
                return None
            finally:
                if private_ref is not None:
                    try:
                        self._git.run(
                            ["update-ref", "-d", private_ref], cwd=self.checkout
                        )
                    except (GitCommandError, OSError):
                        pass
