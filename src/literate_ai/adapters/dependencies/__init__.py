"""Dependency inventory adapters."""

from .admission import python_import_names, reconcile_generated_dependencies
from .cyclonedx import (
    CycloneDxBomError,
    build_cyclonedx_bom,
    validate_cyclonedx_bom,
    validate_resolved_cyclonedx_bom,
)
from .observation import (
    LinuxElfDependencyObserver,
    MacOsMachODependencyObserver,
    NpmDependencyResolver,
    NpmDistributionAuthority,
    NpmLiteralDependencyResolver,
    PortableHostDependencyObserver,
    WindowsPeDependencyObserver,
    declare_optional_python_distributions,
    installed_python_distribution_payload,
    observe_installed_python_distributions,
    observe_npm_distribution,
    parse_dumpbin_dependents,
    parse_dyld_info_links,
    parse_dyld_info_rpaths,
    parse_dyld_info_summary,
    parse_ldconfig_cache,
    parse_llvm_readobj_imports,
    parse_readelf_dynamic,
    resolve_windows_npm_cmd_target,
)
from .resolution import CycloneDxLifecycleResolver
from .types import DependencyObservationError, HostDependencyObservation

__all__ = [
    "CycloneDxBomError",
    "CycloneDxLifecycleResolver",
    "DependencyObservationError",
    "HostDependencyObservation",
    "LinuxElfDependencyObserver",
    "MacOsMachODependencyObserver",
    "NpmDependencyResolver",
    "NpmDistributionAuthority",
    "NpmLiteralDependencyResolver",
    "PortableHostDependencyObserver",
    "WindowsPeDependencyObserver",
    "build_cyclonedx_bom",
    "declare_optional_python_distributions",
    "validate_cyclonedx_bom",
    "validate_resolved_cyclonedx_bom",
    "installed_python_distribution_payload",
    "parse_dumpbin_dependents",
    "parse_dyld_info_links",
    "parse_dyld_info_rpaths",
    "parse_dyld_info_summary",
    "parse_ldconfig_cache",
    "parse_llvm_readobj_imports",
    "parse_readelf_dynamic",
    "resolve_windows_npm_cmd_target",
    "python_import_names",
    "observe_installed_python_distributions",
    "observe_npm_distribution",
    "reconcile_generated_dependencies",
]
