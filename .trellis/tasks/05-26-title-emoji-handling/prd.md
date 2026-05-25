# Fix Attached Page Title Emoji Handling

## Problem

Attached Chrome tabs are marked by prefixing the page title with the eye emoji.
Two issues need fixing:

- The visible title can sometimes accumulate more than one marker.
- Programmatic title reads returned by browserwright should not include the
  marker.

## Requirements

- Attached tabs should have at most one visible `👀 ` marker.
- Existing duplicate markers should be normalized back to one while attached.
- Detach should remove all browserwright title markers from the page title.
- All extension responses and announcements that expose a title should return
  the unmarked title.

## Verification

- Add focused tests for marker normalization and stripping behavior.
- Run the focused test(s) that cover the changed behavior.
