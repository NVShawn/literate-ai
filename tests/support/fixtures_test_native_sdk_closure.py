"""Shared fixtures extracted from ``tests.unit.test_native_sdk_closure``."""


import shutil



from copy import deepcopy












from literate_ai.contracts.authoring_markdown import (
    parse_authoring_markdown,
    render_authoring_markdown,
)






def linked_recipe(fixture):
    root = fixture.component
    original = root.with_name("original")
    root.rename(original)
    root.mkdir()
    original.rename(root / "provider")
    provider = root / "provider"
    for name in ("specs", "acceptance"):
        shutil.copytree(provider / name, root / name)
    path = provider / "component.md"
    document, body = parse_authoring_markdown(path.read_bytes(), source=str(path))
    application = deepcopy(document)
    application["source_dependencies"] = []
    application["provides"][0]["name"] = "sample.wrapper-app"
    application["requires"] = [
        dict(
            requirement_id="provider",
            capability="sample.portable-app",
            version_range=">=1,<2",
            dependency_kind="runtime",
            optional=False,
            constraints=[],
        )
    ]
    (root / "component.md").write_bytes(render_authoring_markdown(application, body))
    document["entrypoints"] = []
    document["provides"][0]["interface"] = {"uri": "integration.md", "pin": None}
    path.write_bytes(render_authoring_markdown(document, body))

