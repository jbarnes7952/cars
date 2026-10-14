# Changelog

One entry per tagged release. Versions match `.claude-plugin/plugin.json`
and the `vX.Y.Z` git tag. Releases before 1.18.0 are untagged; see the git
log for them.

## 1.18.6

- An out-of-date `serve` process stands down from eviction while a newer
  release is running on the same machine. The existing guard only noticed
  when the process's own file changed on disk, which never happens for an
  installed plugin (each release gets its own cache directory), so a
  days-old process went on evicting with superseded rules. Each `serve` now
  records its pid and loaded version in a new `servers` table. Processes
  older than this release cannot take part and stop only when restarted. (#28)

## 1.18.5

- Peer tools are callable under the name `tools/list` advertises. Since
  1.6 they have been listed as `ask_<agent>__<role-slug>`, but `tools/call`
  only routed the legacy `peer_*` prefix to the peer handler, so every
  advertised peer tool answered `unknown tool`. Both prefixes now route. (#27)

## 1.18.4

- Presence checks a declared pid with `os.kill(pid, 0)` instead of looking
  for `/proc/<pid>`. macOS has no procfs, so every pid there read as dead:
  pid-only peers were never `live`, and running sessions' `<pid>.sock` rows
  could be evicted. Linux behaviour is unchanged. Windows is still not
  supported, since signal 0 there sends Ctrl-C. (#26)

## 1.18.3

- The serve process's registry poll holds one read connection open instead
  of opening and closing one every cycle. SQLite no longer creates and
  deletes the WAL side files on each poll, which was a directory write every
  second or two across a fleet. (#18)

## 1.18.2

- Hooks never take a row carrying another session's `session_id` for their
  own, and SessionEnd never deletes on a shared `cwd`. An unregistered
  session exiting next to a registered peer used to delete the peer's row.
  (#25, fixes #23)

## 1.18.1

- Plugin author and marketplace owner are "The Ark Project". README installs
  via the ark-project catalog. A test rejects home-directory paths in any
  tracked file. (#24)

## 1.18.0

- SessionStart titles an untitled session `<project>-<sid[:4]>` and labels
  the tmux window once, behind `LEDGER_SESSION_TITLE` and
  `LEDGER_TMUX_RELABEL`. The register nudge passes that title as
  `session_name`. The project-driven tmux relabel hook is gone. (#20)
