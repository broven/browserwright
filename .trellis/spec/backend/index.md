# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Package layout, sync skill vs async daemon layers | ✅ Filled |
| [Persistence & State](./database-guidelines.md) | No ORM — file-locked JSON ledger + in-memory DaemonState | ✅ Filled |
| [Error Handling](./error-handling.md) | Two exception hierarchies, exit codes, actionable `fix` | ✅ Filled |
| [Quality Guidelines](./quality-guidelines.md) | uv/pytest gates, type hints, dataclasses, session-scoping | ✅ Filled |
| [Logging Guidelines](./logging-guidelines.md) | stdlib logging, BD_LOG_JSON, metrics counters | ✅ Filled |
| [Playwright CDP Facade](./playwright-cdp-facade.md) | connect_over_cdp facade: additive transport, fan-out await-ordering, extension CRPage init fidelity | ✅ Filled |
| [Agent Surface: Playwright](./agent-playwright-surface.md) | heredoc Playwright page/context + aria-ref snapshot; auto-bind to current tab; lazy connect, never close real tabs | ✅ Filled |
| [Persistent Executor](./agent-executor-model.md) | phase B resident per-session executor: live page/context/state across heredocs, control/data plane split, decoupled readiness, rdp-Chrome-style lifecycle | ✅ Filled |

---

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
