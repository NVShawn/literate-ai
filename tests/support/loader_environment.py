"""Host environments with operator-provisioned Linux loader paths.

An operator provisions LD_LIBRARY_PATH without empty entries, which the dynamic
loader would read as the current directory and the product refuses
(linux.loader.paths-invalid). Login shells often leave one, as in
"/usr/local/lib:", so tests that pass the host environment to the product drop
empty entries first.
"""

from collections.abc import Mapping


def without_empty_loader_entries(environment: Mapping[str, str]) -> dict[str, str]:
    result = dict(environment)
    loader = [p for p in result.pop("LD_LIBRARY_PATH", "").split(":") if p]
    if loader:
        result["LD_LIBRARY_PATH"] = ":".join(loader)
    return result
