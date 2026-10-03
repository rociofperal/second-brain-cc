---
id: 2026-10-03-decision-supported-environments-macos-linux-and-windows
title: Supported environments are macOS, Linux and Windows, and every session is told which machine it is on
type: decision
area: [harness]
projects: []
tags: [machines, portability, macos, linux, windows, session-start, capabilities, scheduled-tasks, decision]
status: active
confidence: high
source: agent
provenance: "generalized from real incidents in a working vault; names and numbers are illustrative"
updated: 2026-10-03
supersedes: [2026-09-21-decision-supported-environments-macos-and-linux]
---

## What was decided

The harness has three supported environments: **macOS**, **Linux** and **Windows**. This widens
[[2026-09-21-decision-supported-environments-macos-and-linux]]; everything else that note decided
still holds: capabilities are probed and never inferred from the OS, skills and scheduled tasks
work in every environment or say they are bound to one, and generic content names the
environment ("on a Windows machine"), never a particular host.

## What Windows needs

- **Scheduled jobs** are Task Scheduler tasks, installed by the first run, the counterpart of
  launchd on macOS and systemd on Linux. Code that supervises one scheduler does nothing when it
  is absent.
- **Python runs with `-X utf8`** (or `PYTHONUTF8=1`): without it files are read and written in the
  ANSI code page. Every command the harness generates starts Python that way.
- **Git for Windows** is a requirement. Claude Code on Windows runs the hooks through it.
- Behaviour on macOS and Linux does not change: platform differences live in one place
  (`_bin/osproc.py`, `_bin/oslink.py`) and every Windows branch is tested by injection on any OS.

## Links

- [[2026-09-21-decision-supported-environments-macos-and-linux]]
- [[2026-09-21-decision-machine-identity-is-a-stable-id-plus-a-human-label]]
