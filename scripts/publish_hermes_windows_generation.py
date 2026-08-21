#!/usr/bin/env python3
"""Publish the reviewed Hermes Windows generation barrier onto live main."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

UPSTREAM_REPO = "NousResearch/hermes-agent"
FORK_REPO = "andrexibiza/hermes-agent"
EVIDENCE_BRANCH = "ci/windows-generation-source-artifact"
EVIDENCE_COMMIT = "03d6ed88760566635aee3f3267c71ac3021bbef1"
SOURCE_BRANCH = "fix/windows-update-generation-barrier-v2"
REVIEWED_BASE = "fcbd1076a93841fa88855acce810e342a5b78101"
RECEIPT_MERGE = "1acbeed1461e619767334c74e07bb19d338996c6"
EXPECTED_PATCH_BLOB = "576af882db8851413af01f4e89195ffe858dbaab"
EXPECTED_PATCH_SHA256 = "418dcfb39d3edd4ed29b8cc7435eda010bef3ed8e6925837dafa8e42754118a9"
EXPECTED_PATCH_BYTES = 79046

REVIEWED_PATHS = (
    "hermes_cli/update_cmd.py",
    "hermes_cli/update_generation.py",
    "hermes_cli/update_generation_worker.py",
    "hermes_cli/update_lock.py",
    "tests/hermes_cli/test_update_generation.py",
    "tests/hermes_cli/test_update_lock.py",
)
PUBLISHED_PATHS = (*REVIEWED_PATHS[:4], "tests/hermes_cli/test_update_gateway_runtime_intent.py", *REVIEWED_PATHS[4:])


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args))
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        check=check,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
        env=env,
    )


def output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def existing_pr() -> tuple[str, str] | None:
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            UPSTREAM_REPO,
            "--head",
            f"andrexibiza:{SOURCE_BRANCH}",
            "--state",
            "open",
            "--json",
            "number,headRefOid",
            "--jq",
            ".[0] | if . == null then empty else (.number|tostring) + \" \" + .headRefOid end",
        ],
        capture=True,
    )
    raw = result.stdout.strip()
    if not raw:
        return None
    number, sha = raw.split(" ", 1)
    return number, sha


def has_windows_receipt(pr_number: str, head_sha: str) -> bool:
    result = run(
        [
            "gh",
            "api",
            f"repos/{UPSTREAM_REPO}/issues/{pr_number}/comments",
            "--paginate",
            "--jq",
            ".[]._body // .[].body",
        ],
        check=False,
        capture=True,
    )
    marker = f"Native Windows exact-head verification passed for `{head_sha}`"
    return marker in result.stdout


def patch_gateway_runtime_intent(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    pause_start = text.index(
        "    if not running_pids:\n",
        text.index("def _pause_windows_gateways_for_update"),
    )
    pause_end = text.index("    profile_processes = {}\n", pause_start)
    pause_replacement = (
        "    if not running_pids:\n"
        "        # Preserve observed runtime state, not installation/configuration\n"
        "        # state. A Scheduled Task or Startup entry proves only that\n"
        "        # autostart is configured; it does not prove the gateway was\n"
        "        # running when this update began. With no observed process there\n"
        "        # is nothing to pause and therefore nothing to resume.\n"
        "        return None\n\n"
    )
    text = text[:pause_start] + pause_replacement + text[pause_end:]

    old_resume = (
        '    profiles = token.get("profiles") or {}\n'
        '    unmapped = token.get("unmapped") or []\n'
        '    cold_start = bool(token.get("cold_start_if_installed"))\n'
        '    if not profiles and not any(u.get("argv") for u in unmapped):\n'
        '        if cold_start:\n'
        '            _m()._cold_start_windows_gateway_after_update()\n'
        '        return\n'
    )
    new_resume = (
        '    profiles = token.get("profiles") or {}\n'
        '    unmapped = token.get("unmapped") or []\n'
        '    if not profiles and not any(u.get("argv") for u in unmapped):\n'
        '        # Resume only process generations this updater positively\n'
        '        # observed and paused. Never infer runtime intent from an\n'
        '        # installed autostart entry or a legacy cold-start token: an\n'
        '        # explicitly stopped gateway must stay stopped, including\n'
        '        # after an early update refusal.\n'
        '        return\n'
    )
    count = text.count(old_resume)
    if count != 1:
        raise RuntimeError(f"expected one cold-start resume block, found {count}")
    text = text.replace(old_resume, new_resume, 1)
    resume_slice = text[
        text.index("def _resume_windows_gateways_after_update") : text.index(
            "def _discard_lockfile_churn"
        )
    ]
    if "cold_start_if_installed" in resume_slice:
        raise RuntimeError("legacy cold-start branch survived resume rewrite")
    path.write_text(text, encoding="utf-8")


def write_runtime_intent_test(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            '''\
            """Windows updater preserves observed gateway runtime intent.

            An installed Scheduled Task or Startup entry is configuration, not
            proof that its gateway process was running. The updater may resume
            only generations it positively observed and paused.
            """

            from __future__ import annotations

            from unittest.mock import patch

            from hermes_cli import main as cli_main


            @patch.object(cli_main, "_is_windows", return_value=True)
            def test_installed_but_stopped_gateway_is_not_scheduled_for_resume(
                _win,
                monkeypatch,
            ):
                import hermes_cli.gateway as gateway_mod
                from hermes_cli import gateway_windows

                monkeypatch.setattr(
                    gateway_mod,
                    "find_gateway_pids",
                    lambda **_kwargs: [],
                )

                def installation_state_is_not_runtime_state():
                    raise AssertionError(
                        "pause path must not consult installed autostart state"
                    )

                monkeypatch.setattr(
                    gateway_windows,
                    "is_installed",
                    installation_state_is_not_runtime_state,
                )

                assert cli_main._pause_windows_gateways_for_update() is None


            @patch.object(cli_main, "_is_windows", return_value=True)
            def test_legacy_cold_start_token_cannot_resurrect_stopped_gateway(
                _win,
                monkeypatch,
            ):
                calls: list[str] = []
                monkeypatch.setattr(
                    cli_main,
                    "_refresh_windows_gateway_launchers",
                    lambda: calls.append("refresh"),
                )
                monkeypatch.setattr(
                    cli_main,
                    "_cold_start_windows_gateway_after_update",
                    lambda: calls.append("cold-start"),
                )
                token = {
                    "resume_needed": True,
                    "profiles": {},
                    "unmapped_pids": [],
                    "unmapped": [],
                    "cold_start_if_installed": True,
                }

                cli_main._resume_windows_gateways_after_update(token)

                assert token["resume_needed"] is False
                assert calls == ["refresh"]
            '''
        ),
        encoding="utf-8",
    )


def validate(repo: Path) -> None:
    expected = "\n".join(sorted(PUBLISHED_PATHS))
    actual = run(
        ["git", "diff", "--name-only"], cwd=repo, capture=True
    ).stdout.strip()
    if actual != expected:
        raise RuntimeError(f"unexpected path set\nEXPECTED:\n{expected}\nACTUAL:\n{actual}")
    run(["git", "diff", "--check"], cwd=repo)

    update_text = (repo / "hermes_cli/update_cmd.py").read_text(encoding="utf-8")
    generation_text = (repo / "hermes_cli/update_generation.py").read_text(
        encoding="utf-8"
    )
    for marker in (
        "begin_update_receipt",
        "collect_fleet_versions",
        "begin_dependency_handoff",
        "settlement_partial_reasons",
    ):
        if marker not in update_text:
            raise RuntimeError(f"missing composed update marker: {marker}")
    if "GenerationTransaction" not in generation_text:
        raise RuntimeError("missing generation transaction authority")

    packages = [
        "pytest==9.1.1",
        "psutil==7.2.2",
        "python-dotenv==1.2.2",
        "pyyaml==6.0.3",
        "ruff==0.15.10",
    ]
    run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", *packages],
        cwd=repo,
    )
    compile_paths = [
        *PUBLISHED_PATHS[:4],
        "hermes_cli/update_receipt.py",
        *PUBLISHED_PATHS[4:],
        "tests/hermes_cli/test_update_receipt.py",
    ]
    run([sys.executable, "-m", "py_compile", *compile_paths], cwd=repo)
    run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *PUBLISHED_PATHS,
        ],
        cwd=repo,
    )
    test_env = os.environ.copy()
    test_env["HERMES_PYTHON"] = sys.executable
    run(
        [
            "bash",
            "scripts/run_tests.sh",
            "tests/hermes_cli/test_update_gateway_runtime_intent.py",
            "tests/hermes_cli/test_update_generation.py",
            "tests/hermes_cli/test_update_lock.py",
            "tests/hermes_cli/test_update_receipt.py",
            "-q",
        ],
        cwd=repo,
        env=test_env,
    )
    run(
        [
            sys.executable,
            "scripts/check-windows-footguns.py",
            *PUBLISHED_PATHS,
        ],
        cwd=repo,
    )


def pr_body(head_sha: str, current_main: str) -> str:
    return textwrap.dedent(
        f"""\
        ## Summary

        Implements the Windows process-generation barrier required by #88683 and Phase 2 of #91277, composed with the merged structured-receipt and fleet-generation work from #91283.

        Generation N may settle source, but it may not mutate or certify generation N+1 while still executing from the environment being replaced. Hermes now records a durable capability-bound transaction and hands dependency/post-update settlement to a standard-library worker outside the target venv. The worker waits for generation N to exit, realizes and verifies generation N+1, and re-enters the original command through the verified target interpreter.

        This head also closes the field-reproduced gateway-intent regression: an installed Scheduled Task is configuration, not proof that a gateway was running. The updater resumes only process generations it positively observed and paused. An explicitly stopped gateway therefore remains stopped, including when the update refuses before mutation.

        ## Contract

        - atomic complete-file update-lock publication with one concurrent owner;
        - capability-bound same-generation adoption only for an explicit worker resume;
        - durable previous/target SHA, argv, profile, gateway, snapshot, and dependency state;
        - no Python dependency mutation from generation N;
        - worker selected outside the target venv before mutation;
        - exact checkout-generation and target-interpreter provenance checks;
        - core, optional, lazy backend, Hermes Tools, memory-provider, and console-script verification;
        - terminal `complete`, `partial`, and `failed` journal states;
        - no second fetch/pull when resuming a recorded transaction;
        - #91283 receipts and fleet verification survive the generation handoff;
        - installed autostart state never fabricates a runtime-generation resume obligation.

        ## Field reproduction closed

        On Windows, `hermes gateway stop && hermes update` could refuse on lingering Desktop backends and then print `Gateway started via cold-start after update`. The finalizer inferred liveness from `gateway_windows.is_installed()`, resurrecting a service the operator had explicitly stopped. Regression coverage locks both the normal path and legacy-token compatibility path.

        ## Exact head

        [`{head_sha}`](https://github.com/andrexibiza/hermes-agent/commit/{head_sha})

        Built directly on current upstream `main` at `{current_main}`. The generation source was reconstructed from Git blob `{EXPECTED_PATCH_BLOB}` / SHA-256 `{EXPECTED_PATCH_SHA256}`, three-way composed with current main, then passed focused generation, lock, receipt, runtime-intent, compile, Ruff, and Windows-footgun gates before publication. Native Windows verification runs against the exact published head.

        ## Interlocks

        - #91079 owns the Desktop package candidate/rollback transaction.
        - #91316 owns authoritative deployment-plan admission.
        - #88764 owns secondary Desktop provisioning authority.
        - #91283 owns structured receipts and post-restart fleet-generation verification.

        Refs #88683
        Part of #91277
        """
    )


def main() -> int:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("ARES_REVIEW_TOKEN is unavailable")

    prior = existing_pr()
    if prior is not None:
        pr_number, head_sha = prior
        if has_windows_receipt(pr_number, head_sha):
            print(f"PR #{pr_number} already has an exact-head Windows receipt; done.")
            return 0
        print(f"PR #{pr_number} exists without an exact-head Windows receipt; reverify.")
        output("head_sha", head_sha)
        output("pr_number", pr_number)
        return 0

    root = Path("/tmp/hermes")
    reviewed_base = Path("/tmp/reviewed-base")
    reviewed_source = Path("/tmp/reviewed-source")
    for path in (root, reviewed_base, reviewed_source):
        shutil.rmtree(path, ignore_errors=True)

    run(["git", "clone", "--filter=blob:none", f"https://github.com/{UPSTREAM_REPO}.git", str(root)])
    run(["git", "config", "user.name", "Axl Ibiza"], cwd=root)
    run(
        [
            "git",
            "config",
            "user.email",
            "84248988+andrexibiza@users.noreply.github.com",
        ],
        cwd=root,
    )
    run(["git", "config", "core.autocrlf", "false"], cwd=root)
    run(["git", "config", "core.longpaths", "true"], cwd=root)
    token = os.environ["GH_TOKEN"]
    run(
        [
            "git",
            "remote",
            "add",
            "fork",
            f"https://x-access-token:{token}@github.com/{FORK_REPO}.git",
        ],
        cwd=root,
    )
    run(["git", "fetch", "--no-tags", "origin", "main"], cwd=root)
    run(
        [
            "git",
            "fetch",
            "--no-tags",
            "fork",
            f"+refs/heads/{EVIDENCE_BRANCH}:refs/remotes/fork/evidence",
        ],
        cwd=root,
    )
    current_main = run(
        ["git", "rev-parse", "origin/main"], cwd=root, capture=True
    ).stdout.strip()
    evidence_head = run(
        ["git", "rev-parse", "refs/remotes/fork/evidence"], cwd=root, capture=True
    ).stdout.strip()
    print(f"Current upstream main: {current_main}")
    print(f"Evidence branch head: {evidence_head}")
    run(["git", "cat-file", "-e", f"{EVIDENCE_COMMIT}^{{commit}}"], cwd=root)
    run(["git", "merge-base", "--is-ancestor", EVIDENCE_COMMIT, evidence_head], cwd=root)
    run(["git", "merge-base", "--is-ancestor", REVIEWED_BASE, current_main], cwd=root)
    run(["git", "merge-base", "--is-ancestor", RECEIPT_MERGE, current_main], cwd=root)

    patch_data = run(
        [
            "git",
            "show",
            f"{EVIDENCE_COMMIT}:.hermes-patches/windows-generation/source.patch",
        ],
        cwd=root,
        capture=True,
    ).stdout.encode("utf-8")
    if len(patch_data) != EXPECTED_PATCH_BYTES:
        raise RuntimeError(f"unexpected patch size: {len(patch_data)}")
    if git_blob_sha(patch_data) != EXPECTED_PATCH_BLOB:
        raise RuntimeError("unexpected source patch Git blob")
    if hashlib.sha256(patch_data).hexdigest() != EXPECTED_PATCH_SHA256:
        raise RuntimeError("unexpected source patch SHA-256")
    patch_path = Path("/tmp/windows-generation.patch")
    patch_path.write_bytes(patch_data)

    run(["git", "worktree", "add", "--detach", str(reviewed_base), REVIEWED_BASE], cwd=root)
    run(["git", "worktree", "add", "--detach", str(reviewed_source), REVIEWED_BASE], cwd=root)
    run(
        ["git", "apply", "--index", "--unidiff-zero", "--check", str(patch_path)],
        cwd=reviewed_source,
    )
    run(
        ["git", "apply", "--index", "--unidiff-zero", str(patch_path)],
        cwd=reviewed_source,
    )
    run(["git", "diff", "--cached", "--check"], cwd=reviewed_source)
    reviewed_actual = run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=reviewed_source,
        capture=True,
    ).stdout.strip().splitlines()
    if tuple(sorted(reviewed_actual)) != tuple(sorted(REVIEWED_PATHS)):
        raise RuntimeError(f"unexpected reviewed path set: {reviewed_actual}")

    run(["git", "checkout", "-B", SOURCE_BRANCH, "origin/main"], cwd=root)
    for relative in (
        "hermes_cli/update_cmd.py",
        "hermes_cli/update_lock.py",
        "tests/hermes_cli/test_update_lock.py",
    ):
        merged = run(
            [
                "git",
                "merge-file",
                "-p",
                str(reviewed_source / relative),
                str(reviewed_base / relative),
                str(root / relative),
            ],
            check=False,
            capture=True,
        )
        if merged.returncode != 0:
            raise RuntimeError(f"three-way composition conflicted in {relative}\n{merged.stdout}")
        if any(marker in merged.stdout for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            raise RuntimeError(f"conflict markers survived in {relative}")
        (root / relative).write_text(merged.stdout, encoding="utf-8")

    for relative in (
        "hermes_cli/update_generation.py",
        "hermes_cli/update_generation_worker.py",
        "tests/hermes_cli/test_update_generation.py",
    ):
        probe = run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=root,
            check=False,
            capture=True,
        )
        if probe.returncode == 0:
            raise RuntimeError(f"current main now owns {relative}; explicit reconciliation required")
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(reviewed_source / relative, destination)

    patch_gateway_runtime_intent(root / "hermes_cli/update_cmd.py")
    write_runtime_intent_test(root / "tests/hermes_cli/test_update_gateway_runtime_intent.py")
    validate(root)

    run(["git", "add", "--", *PUBLISHED_PATHS], cwd=root)
    run(
        [
            "git",
            "commit",
            "-m",
            "fix(update): cross the Windows process-generation barrier (#88683)",
        ],
        cwd=root,
    )
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=root, capture=True).stdout.strip()
    run(["git", "push", "--force", "fork", f"HEAD:{SOURCE_BRANCH}"], cwd=root)

    body_path = Path("/tmp/pr-body.md")
    body_path.write_text(pr_body(head_sha, current_main), encoding="utf-8")
    created = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            UPSTREAM_REPO,
            "--head",
            f"andrexibiza:{SOURCE_BRANCH}",
            "--base",
            "main",
            "--title",
            "fix(update): cross the Windows process-generation barrier",
            "--body-file",
            str(body_path),
        ],
        capture=True,
    ).stdout.strip()
    pr_number = created.rstrip("/").rsplit("/", 1)[-1]

    run(
        [
            "gh",
            "issue",
            "comment",
            "88683",
            "--repo",
            UPSTREAM_REPO,
            "--body",
            f"Published the current-main Windows process-generation barrier in #{pr_number} at exact head `{head_sha}`. It composes with merged #91283 and preserves stopped-gateway runtime intent; no fixed-on-main claim is made before merge and upstream-object verification.",
        ]
    )
    run(
        [
            "gh",
            "issue",
            "comment",
            "91277",
            "--repo",
            UPSTREAM_REPO,
            "--body",
            f"Phase 2 implementation slice published in #{pr_number}: capability-bound N→N+1 dependency settlement, exact generation verification, and observed-only gateway resume semantics. Exact head: `{head_sha}`.",
        ]
    )
    output("head_sha", head_sha)
    output("pr_number", pr_number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
