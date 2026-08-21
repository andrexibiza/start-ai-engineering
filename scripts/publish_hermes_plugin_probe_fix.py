#!/usr/bin/env python3
"""Publish the Desktop runtime-plugin nonthrowing discovery fix."""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

UPSTREAM = "NousResearch/hermes-agent"
FORK = "andrexibiza/hermes-agent"
BRANCH = "fix/desktop-runtime-plugin-probe-noise"


def run(args: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args))
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=capture,
    )


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one source block, found {count}")
    return text.replace(old, new, 1)


def existing_pr() -> str | None:
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            UPSTREAM,
            "--head",
            f"andrexibiza:{BRANCH}",
            "--state",
            "open",
            "--json",
            "number",
            "--jq",
            ".[0].number // empty",
        ],
        capture=True,
    )
    return result.stdout.strip() or None


def modify_loader(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '''interface DiskRoot {
  /** Root-level enable posture, forwarded to the loader (see LoadOptions). */
  defaultEnabled?: boolean
  dir: string
  /** Resolve a scanned folder to its candidate plugin entry file. */
  entry: (folderPath: string) => string
}
''',
        '''interface DiskRoot {
  /** Root-level enable posture, forwarded to the loader (see LoadOptions). */
  defaultEnabled?: boolean
  dir: string
  /** Path segments below each scanned package folder. Discovery walks
   *  directory metadata to this file instead of throwing a content read for
   *  every ordinary package that has no Desktop half. */
  entrySegments: readonly string[]
}
''',
        "DiskRoot authority",
    )
    text = replace_once(
        text,
        "    roots.push({ dir: standalone, entry: folder => `${folder}/plugin.js` })",
        "    roots.push({ dir: standalone, entrySegments: ['plugin.js'] })",
        "standalone entry shape",
    )
    text = replace_once(
        text,
        "    roots.push({ defaultEnabled: false, dir: unified, entry: folder => `${folder}/desktop/plugin.js` })",
        "    roots.push({ defaultEnabled: false, dir: unified, entrySegments: ['desktop', 'plugin.js'] })",
        "unified entry shape",
    )
    text = replace_once(
        text,
        "async function loadDiskPlugin(entry: DiskPlugin): Promise<void> {",
        "async function loadDiskPlugin(entry: DiskPlugin): Promise<boolean> {",
        "load result contract",
    )
    text = replace_once(
        text,
        '''    if (id && id !== entry.origin) {
      dropOriginRecord(entry.origin, entry)
    }
  } catch {
    // File vanished mid-read — the next scan reconciles.
  }
}

async function scanDiskPlugins(): Promise<void> {
''',
        '''    if (id && id !== entry.origin) {
      dropOriginRecord(entry.origin, entry)
    }

    return true
  } catch {
    // File vanished mid-read. The caller uses false to reconcile/unload the
    // registration instead of retaining a live ghost for a missing entry.
    return false
  }
}

async function resolveDiskPluginEntry(
  desktop: Window['hermesDesktop'],
  folderPath: string,
  segments: readonly string[]
): Promise<string | null> {
  let currentDir = folderPath

  for (let index = 0; index < segments.length; index += 1) {
    const { entries } = await desktop.readDir(currentDir)
    const entry = entries.find(candidate => candidate.name === segments[index])

    if (!entry) {
      return null
    }

    const last = index === segments.length - 1

    if (last) {
      return entry.isDirectory ? null : entry.path
    }

    if (!entry.isDirectory) {
      return null
    }

    currentDir = entry.path
  }

  return null
}

async function scanDiskPlugins(): Promise<void> {
''',
        "entry metadata resolver",
    )
    text = replace_once(
        text,
        '''      for (const dir of entries.filter(e => e.isDirectory)) {
        const file = root.entry(dir.path)
        seen.add(file)

        if (disk.has(file)) {
          continue
        }

        try {
          await desktop.readFileText(file)
        } catch {
          continue // No entry file (yet) — not a plugin folder for this root.
        }

        const record: DiskPlugin = {
          defaultEnabled: root.defaultEnabled,
          file,
          id: null,
          origin: dir.name,
          watchId: null
        }

        disk.set(file, record)
        await loadDiskPlugin(record)

        try {
          record.watchId = (await desktop.watchPreviewFile(file)).id
        } catch {
          // Unwatchable — the poll still reconciles new folders; edits need a
          // manual "Reload desktop plugins".
        }
      }
''',
        '''      for (const dir of entries.filter(e => e.isDirectory)) {
        let file: string | null

        try {
          file = await resolveDiskPluginEntry(desktop, dir.path, root.entrySegments)
        } catch {
          continue // Folder changed during the metadata walk; the next tick reconciles.
        }

        if (!file) {
          continue // Ordinary agent package with no Desktop half — not an error.
        }

        seen.add(file)

        if (disk.has(file)) {
          continue
        }

        const record: DiskPlugin = {
          defaultEnabled: root.defaultEnabled,
          file,
          id: null,
          origin: dir.name,
          watchId: null
        }

        disk.set(file, record)

        if (!(await loadDiskPlugin(record))) {
          disk.delete(file)
          continue
        }

        try {
          record.watchId = (await desktop.watchPreviewFile(file)).id
        } catch {
          // Unwatchable — the poll still reconciles new folders; edits need a
          // manual "Reload desktop plugins".
        }
      }
''',
        "nonthrowing scan loop",
    )
    text = replace_once(
        text,
        '''      if (record.watchId === id) {
        void loadDiskPlugin(record)

        return
      }
''',
        '''      if (record.watchId === id) {
        void loadDiskPlugin(record).then(readable => {
          if (!readable) {
            void scanDiskPlugins()
          }
        })

        return
      }
''',
        "file-disappearance reconciliation",
    )
    path.write_text(text, encoding="utf-8")


def modify_tests(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()\nconst onPreviewFileChanged = vi.fn()",
        "const watchPreviewFile = vi.fn<(path: string) => Promise<{ id: string }>>()\nconst stopPreviewFileWatch = vi.fn<(id: string) => Promise<boolean>>()\nconst onPreviewFileChanged = vi.fn()",
        "stop-watch mock declaration",
    )
    text = replace_once(
        text,
        "  watchPreviewFile.mockReset()\n  onPreviewFileChanged.mockReset()",
        "  watchPreviewFile.mockReset()\n  stopPreviewFileWatch.mockReset()\n  stopPreviewFileWatch.mockResolvedValue(true)\n  onPreviewFileChanged.mockReset()",
        "stop-watch reset",
    )
    text = replace_once(
        text,
        "    readFileText,\n    watchDirectory,\n    watchPreviewFile",
        "    readFileText,\n    stopPreviewFileWatch,\n    watchDirectory,\n    watchPreviewFile",
        "stop-watch bridge",
    )
    text = replace_once(
        text,
        '''  it('probes desktop/plugin.js inside agent-plugin packages (unified packaging)', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockImplementation(async dir =>
      dir === '/local/.hermes/plugins'
        ? { entries: [{ isDirectory: true, name: 'my-feature', path: '/local/.hermes/plugins/my-feature' }] }
        : { entries: [] }
    )
    // No desktop half in this package — probe must target desktop/plugin.js.
    readFileText.mockRejectedValue(new Error('ENOENT'))

    await discoverRuntimePlugins()

    expect(readFileText).toHaveBeenCalledWith('/local/.hermes/plugins/my-feature/desktop/plugin.js')
    // The Python half's files must never be probed as a desktop entry.
    expect(readFileText).not.toHaveBeenCalledWith('/local/.hermes/plugins/my-feature/plugin.js')
  })
''',
        '''  it('treats a package without a Desktop half as metadata, not a throwing file read', async () => {
    desktopPluginsRoot.mockResolvedValue('/local/.hermes/desktop-plugins')
    agentPluginsRoot.mockResolvedValue('/local/.hermes/plugins')
    readDir.mockImplementation(async dir => {
      if (dir === '/local/.hermes/plugins') {
        return { entries: [{ isDirectory: true, name: 'my-feature', path: '/local/.hermes/plugins/my-feature' }] }
      }

      if (dir === '/local/.hermes/plugins/my-feature') {
        return {
          entries: [{ isDirectory: false, name: 'plugin.yaml', path: '/local/.hermes/plugins/my-feature/plugin.yaml' }]
        }
      }

      return { entries: [] }
    })

    await discoverRuntimePlugins()

    expect(readDir).toHaveBeenCalledWith('/local/.hermes/plugins/my-feature')
    expect(readDir).not.toHaveBeenCalledWith('/local/.hermes/plugins/my-feature/desktop')
    expect(readFileText).not.toHaveBeenCalled()
  })
''',
        "missing desktop half regression",
    )
    text = replace_once(
        text,
        '''    readDir.mockImplementation(async dir =>
      dir === '/local/.hermes/plugins'
        ? { entries: [{ isDirectory: true, name: 'uni', path: '/local/.hermes/plugins/uni' }] }
        : { entries: [] }
    )

    const register = vi.fn()
''',
        '''    let desktopEntryPresent = true

    readDir.mockImplementation(async dir => {
      if (dir === '/local/.hermes/plugins') {
        return { entries: [{ isDirectory: true, name: 'uni', path: '/local/.hermes/plugins/uni' }] }
      }

      if (dir === '/local/.hermes/plugins/uni') {
        return {
          entries: [{ isDirectory: true, name: 'desktop', path: '/local/.hermes/plugins/uni/desktop' }]
        }
      }

      if (dir === '/local/.hermes/plugins/uni/desktop') {
        return {
          entries: desktopEntryPresent
            ? [
                {
                  isDirectory: false,
                  name: 'plugin.js',
                  path: '/local/.hermes/plugins/uni/desktop/plugin.js'
                }
              ]
            : []
        }
      }

      return { entries: [] }
    })

    const register = vi.fn()
''',
        "unified metadata tree",
    )
    text = replace_once(
        text,
        '''      await setPluginEnabled('uni', true)
      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().uni.status).toBe('loaded')
    } finally {
''',
        '''      await setPluginEnabled('uni', true)
      expect(register).toHaveBeenCalledTimes(1)
      expect($pluginRecords.get().uni.status).toBe('loaded')

      // Removing only desktop/plugin.js (while the Python package folder
      // remains) unloads the previous Desktop registration instead of leaving
      // a live ghost behind.
      desktopEntryPresent = false
      await discoverRuntimePlugins()
      expect($pluginRecords.get().uni).toBeUndefined()
      expect(stopPreviewFileWatch).toHaveBeenCalledWith('w-uni')
    } finally {
''',
        "entry deletion regression",
    )
    path.write_text(text, encoding="utf-8")


def main() -> int:
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("ARES_REVIEW_TOKEN is unavailable")
    prior = existing_pr()
    if prior:
        print(f"PR #{prior} already exists; one-shot publisher is complete.")
        return 0

    repo = Path("/tmp/hermes-plugin-probe")
    shutil.rmtree(repo, ignore_errors=True)
    run(["git", "clone", "--filter=blob:none", f"https://github.com/{UPSTREAM}.git", str(repo)])
    run(["git", "config", "user.name", "Axl Ibiza"], cwd=repo)
    run(["git", "config", "user.email", "84248988+andrexibiza@users.noreply.github.com"], cwd=repo)
    run(
        [
            "git",
            "remote",
            "add",
            "fork",
            f"https://x-access-token:{os.environ['GH_TOKEN']}@github.com/{FORK}.git",
        ],
        cwd=repo,
    )
    run(["git", "fetch", "--no-tags", "origin", "main"], cwd=repo)
    run(["git", "checkout", "-B", BRANCH, "origin/main"], cwd=repo)
    base_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True).stdout.strip()

    modify_loader(repo / "apps/desktop/src/contrib/runtime-loader.ts")
    modify_tests(repo / "apps/desktop/src/contrib/runtime-loader.test.ts")
    run(["git", "diff", "--check"], cwd=repo)
    changed = run(["git", "diff", "--name-only"], cwd=repo, capture=True).stdout.strip().splitlines()
    expected = [
        "apps/desktop/src/contrib/runtime-loader.test.ts",
        "apps/desktop/src/contrib/runtime-loader.ts",
    ]
    if changed != expected:
        raise RuntimeError(f"unexpected path set: {changed}")

    run(["npm", "ci", "--ignore-scripts"], cwd=repo)
    run(
        [
            "npm",
            "--workspace",
            "apps/desktop",
            "exec",
            "--",
            "vitest",
            "run",
            "--project",
            "ui",
            "src/contrib/runtime-loader.test.ts",
        ],
        cwd=repo,
    )
    run(
        [
            "npm",
            "--workspace",
            "apps/desktop",
            "exec",
            "--",
            "eslint",
            "src/contrib/runtime-loader.ts",
            "src/contrib/runtime-loader.test.ts",
        ],
        cwd=repo,
    )
    run(["npm", "--workspace", "apps/desktop", "run", "typecheck"], cwd=repo)

    run(["git", "add", "--", *expected], cwd=repo)
    run(["git", "commit", "-m", "fix(desktop): stop missing plugin entries from flooding IPC logs"], cwd=repo)
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True).stdout.strip()
    run(["git", "push", "--force", "fork", f"HEAD:{BRANCH}"], cwd=repo)

    body = textwrap.dedent(
        f"""\
        ## Summary

        Closes a deterministic Desktop error loop reproduced on a real Windows packaged app: the runtime-plugin scanner used `readFileText()` as an existence probe for every package under `~/.hermes/plugins`, even though most agent plugins legitimately have no `desktop/plugin.js`. Electron logs every rejected IPC handler before the renderer's catch runs, so the five-second scan produced repeating bursts of `Text preview failed: file does not exist` and buried real errors.

        Discovery now walks directory metadata to the declared entry segments (`plugin.js` or `desktop/plugin.js`) and reads content only after a real file is present. Missing Desktop halves are ordinary inventory state, not exceptions.

        The same model also closes the other side of the class: if an entry file disappears while its package directory remains, the next reconciliation no longer marks the absent candidate as seen. The prior Desktop registration is unloaded, its inventory row is removed, and its file watch is retired instead of leaving a live ghost plugin.

        ## Field evidence

        A real Windows Desktop log showed `hermes:readFileText` ENOENT handler failures repeating continuously after launch. The bursts correspond to the scanner's five-second pass across agent-plugin directories without Desktop halves.

        ## Exact head

        [`{head_sha}`](https://github.com/andrexibiza/hermes-agent/commit/{head_sha})

        Built directly on upstream `main` at `{base_sha}`. Before publication the exact two-file change passed:

        - focused `runtime-loader.test.ts` UI suite;
        - ESLint on both changed files;
        - all Desktop TypeScript projects through `npm run typecheck`;
        - `git diff --check` and exact path-set validation.

        ## Negative contracts

        - no new permissive filesystem IPC was added;
        - missing roots/packages remain non-fatal;
        - content reads still fail closed for a file that disappears after metadata discovery;
        - runtime plugin evaluation, integrity checks, enable posture, and local-root authority are unchanged.
        """
    )
    body_path = Path("/tmp/plugin-probe-pr.md")
    body_path.write_text(body, encoding="utf-8")
    url = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            UPSTREAM,
            "--head",
            f"andrexibiza:{BRANCH}",
            "--base",
            "main",
            "--title",
            "fix(desktop): stop missing plugin entries from flooding IPC logs",
            "--body-file",
            str(body_path),
        ],
        capture=True,
    ).stdout.strip()
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
