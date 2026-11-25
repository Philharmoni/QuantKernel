"""Shared verification policy for iterative, standard, and audit runs."""
import argparse
import os

VERIFICATION_MODES = ("fast", "standard", "audit")


def verification_mode(value: str | None = None) -> str:
    mode = value or os.environ.get("QUANTKERNEL_VERIFICATION", "standard")
    if mode not in VERIFICATION_MODES:
        raise ValueError(f"Unknown verification mode {mode!r}; choose from {VERIFICATION_MODES}")
    return mode


def verification_metadata(value: str | None = None) -> dict:
    mode = verification_mode(value)
    return {"verification": mode, "audit_complete": mode == "audit"}


def add_verification_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--verification", choices=VERIFICATION_MODES, default=None,
                        help="Verification depth; defaults to QUANTKERNEL_VERIFICATION or standard")
