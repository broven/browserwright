# Backend Parity E2E Usage Layer

## Goal

Ensure extension, RDP attach, and RDP create sessions expose the same skill usage surface after session creation.

## Requirements

- Cover the backend-neutral skill primitives with the same script across:
  - extension attach session,
  - RDP attach session,
  - RDP create session.
- The script must not branch by backend after setup.
- Exercise opening, DOM evaluation/mutation, page info, tab listing, current tab lookup, and tab closing.

## Verification

- Run focused real Chrome E2E parity tests.
