from __future__ import annotations

RELEASE_VERSION = "0.2.0"
RELEASE_DATE = "2026-07-22"
AUTOMATION_MANIFEST_VERSION = "2"


def release_metadata() -> dict[str, str]:
    return {
        "release_version": RELEASE_VERSION,
        "release_date": RELEASE_DATE,
        "automation_manifest_version": AUTOMATION_MANIFEST_VERSION,
    }
