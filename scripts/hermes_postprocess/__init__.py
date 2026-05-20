"""Repo-local Hermes postprocess operator helpers.

The package is intentionally deterministic and provider-neutral: it discovers one
candidate stem, resolves paths, validates staging artifacts, and promotes only
when explicitly requested by the caller.
"""

from .schemas import Candidate, CandidatePaths, PromptBundle, ValidationResult

__all__ = [
    "Candidate",
    "CandidatePaths",
    "PromptBundle",
    "ValidationResult",
]
