"""Resolve host observation requirements without executing project-authored probes."""

from pathlib import Path

from literate_ai.adapters.component_lock_planning import (
    observe_flavor_catalog,
    observe_flavor_host_toolchain,
)
from literate_ai.adapters.host_install import HostInstallError
from literate_ai.application.flavor_selection import apply_flavor_selectors


def project_host_flavor_names(project, selectors, packaged_root: Path):
    """Use project selection authority and only trusted packaged probe programs."""
    catalog = observe_flavor_catalog(
        project.roots("flavor"),
        boundary=project.root,
        scale_limits=project.definition.scale_limits,
    )
    selected = apply_flavor_selectors(
        tuple(item.selection for item in catalog), tuple(selectors)
    )
    trusted = observe_flavor_catalog((packaged_root,), boundary=packaged_root.parent)
    by_coordinate = {item.definition.coordinate.uri: item for item in trusted}
    local_by_coordinate = {item.definition.coordinate.uri: item for item in catalog}
    names = []
    for selection in selected:
        local = local_by_coordinate[selection.coordinate_uri]
        packaged = by_coordinate.get(selection.coordinate_uri)
        local_toolchain = observe_flavor_host_toolchain(local, boundary=project.root)
        trusted_toolchain = (
            None
            if packaged is None
            else observe_flavor_host_toolchain(packaged, boundary=packaged_root.parent)
        )
        if local_toolchain is not None and local_toolchain != trusted_toolchain:
            raise HostInstallError(
                "host-install.project-toolchain-observation-unsupported",
                "selected project Flavor requires untrusted host observation probes: "
                + selection.coordinate_uri,
            )
        # Removing a copied sidecar cannot erase the packaged coordinate's tools.
        if packaged is not None:
            names.append(packaged.path.parent.name)
    return tuple(dict.fromkeys(names))
