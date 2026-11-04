"""Explicit first-use output namespace custody for retained-tree HTML.

This unused first-use boundary refuses any existing index or namespace. It can
reserve and publish one complete initial generation inside its actual live
context. It does not recognize historical output or recover an interruption.
A saved plan or owner.json cannot recreate the context yielded here.
"""

from __future__ import annotations

import html
import os
import secrets
import stat
import weakref
from contextlib import ExitStack, contextmanager
from pathlib import Path

from literate_ai.contracts.html_trees import HtmlTreeRenderRequest
from literate_ai.contracts.identity import canonical_identity, canonical_json_bytes

from .exclusive_directory import publish_directory_between_owned_parents_exclusive
from .html_publication import OutputSnapshot, publish_html
from .html_tree_emitter import directory_page_path
from .html_tree_file_staging import stage_html_tree_files
from .html_tree_generation import StagedHtmlTreeGeneration
from .html_tree_generation_files import observe_html_tree_generation_files
from .repository_refresh import _local_identity_wire
from .retained_project_inputs import _root

_ISSUER = object()
_CAPTURE_OWNERS = weakref.WeakKeyDictionary()
MAX_INITIAL_TREE_INDEX_BYTES = 65536


def _node(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _signature(value):
    return (
        _node(value),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_at(directory, name, maximum):
    fd = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        dir_fd=directory,
    )
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
        ):
            raise ValueError("html.tree.output-control-file-unsafe")
        content = bytearray()
        while len(content) <= before.st_size:
            block = os.read(fd, min(65536, before.st_size - len(content) + 1))
            if not block:
                break
            content.extend(block)
        signature = _signature(before)
        if (
            len(content) != before.st_size
            or _signature(os.fstat(fd)) != signature
            or _signature(os.stat(name, dir_fd=directory, follow_symlinks=False))
            != signature
        ):
            raise ValueError("html.tree.output-control-file-changed")
        return signature, bytes(content)
    finally:
        os.close(fd)


def _publication_signature(signature):
    return (
        signature[0][0],
        signature[0][1],
        signature[2],
        signature[3],
        signature[4],
        signature[0][2],
    )


def _initial_index(request, staged):
    link = (
        f".litai-tree-{request.request_id}/g-{staged.manifest.digest}/"
        + directory_page_path(".")
    )
    content = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="litai-tree-request" content="'
        + html.escape(request.identity.uri, quote=True)
        + '"><meta name="litai-tree-manifest" content="'
        + html.escape(staged.manifest.identity, quote=True)
        + '"><title>Directory information cards</title></head><body>'
        "<main><h1>Directory information cards</h1><p>"
        + str(staged.page_count)
        + ' pages of directory metadata.</p><p><a href="'
        + html.escape(link, quote=True)
        + '">Open the root directory card</a></p></main></body></html>'
    ).encode("utf-8")
    if (
        len(content) > min(MAX_INITIAL_TREE_INDEX_BYTES, request.limits.max_page_bytes)
        or staged.page_bytes + len(content) > request.limits.max_total_generated_bytes
    ):
        raise ValueError("html.tree.top-index-output-policy-exceeded")
    return content


def _selection(project_root, request):
    if os.name != "posix":
        raise ValueError("html.tree.native-output-initialization-unsupported")
    if not isinstance(request, HtmlTreeRenderRequest):
        raise TypeError("html.tree.typed-output-request-required")
    project = _root(Path(project_root))
    output = _root(project / request.output_root)
    if not output.is_relative_to(project):
        raise ValueError("html.tree.output-outside-selected-project")
    if output.lstat().st_uid != os.geteuid():
        raise ValueError("html.tree.output-parent-not-current-user-owned")
    namespace = project / request.namespace_path
    index = project / request.index_path
    if os.path.lexists(namespace) or os.path.lexists(index):
        raise ValueError("html.tree.initial-output-already-exists")
    return project, output, namespace, index


def plan_initial_html_tree_output(project_root, request):
    """Describe an absent destination without writing or adopting anything."""
    project, output, namespace, index = _selection(project_root, request)
    return {
        "schema": "literate-ai/initial-tree-html-output-plan@1",
        "request_identity": request.identity.uri,
        "project_node": _local_identity_wire(_node(project.lstat())),
        "output_node": _local_identity_wire(_node(output.lstat())),
        "namespace": namespace.relative_to(project).as_posix(),
        "index": index.relative_to(project).as_posix(),
        "namespace_absent": True,
        "index_absent": True,
        "generation_published": False,
    }


class InitialHtmlTreeOutput:
    """Actual first-use namespace custody, invalid outside its creating context."""

    __slots__ = ("_values", "__weakref__")

    def __init__(
        self,
        path,
        guard,
        stage,
        publish,
        *,
        retained_memory,
        published_index_guard,
        _issuer,
    ):
        if _issuer is not _ISSUER:
            raise TypeError("an actual newly created output context is required")
        object.__setattr__(
            self,
            "_values",
            (path, guard, stage, publish, retained_memory, published_index_guard),
        )

    def __setattr__(self, name, value):
        raise TypeError("initial output custody is immutable")

    @property
    def path(self):
        self.require_current()
        return self._values[0]

    def require_current(self, *, reserved_memory_bytes=0):
        self._values[1](reserved_memory_bytes=reserved_memory_bytes)

    @property
    def retained_memory_bytes(self):
        """Descriptive original-context allocation estimate, not output custody."""
        return self._values[4]()

    def stage_generation(self, tree, store, staged):
        """Reserve and retain one actual provisional generation, not publish it."""
        self.require_current()
        return self._values[2](tree, store, staged)

    def publish_generation(
        self,
        *,
        require_source_current=None,
        source_observation=None,
        require_publication_current=None,
    ):
        """Publish this live owner's exact staged generation and index last."""
        self.require_current()
        self._values[3](
            require_source_current, source_observation, require_publication_current
        )

    def require_published_index(self, snapshot):
        """Join a reopened snapshot to this original live owner's exact index."""
        if type(self) is not InitialHtmlTreeOutput or self not in _CAPTURE_OWNERS:
            raise TypeError("html.tree.original-output-owner-required")
        self._values[5](snapshot)

    def __copy__(self):
        raise TypeError("output custody cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("output custody cannot be copied")

    def __reduce__(self):
        raise TypeError("output custody cannot be restored")


def _html_tree_capture_exclusions(owner, source, *, reserved_memory_bytes=0):
    """Select only an original live output owner's exact physical members.

    This private observation is not an ignore-list API. Copies, lookalike tuples,
    saved manifests and manually constructed instances have no issuance entry.
    Complete owner checks remain required around each canonical inventory.
    """
    if type(owner) is not InitialHtmlTreeOutput:
        raise TypeError("html.tree.original-output-owner-required")
    selection = _CAPTURE_OWNERS.get(owner)
    if selection is None:
        raise ValueError("html.tree.output-owner-not-issued-or-ended")
    return selection(source, reserved_memory_bytes)


@contextmanager
def initialize_html_tree_output(project_root, request, *, acknowledged_plan_identity):
    """Apply explicitly acknowledged first-use intent, preserving interruptions.

    The caller owns the lifecycle lock and operator authorization. A matching plan
    is a revalidation requirement, not an execution or publication capability.
    One explicitly reserved generation can move from its actual stage into an
    immutable destination, followed by the index as commit point. Existing output,
    and recovery remain separately guarded operations. Inside-source publication
    additionally requires an original source view tied to this output context.
    """
    if os.name != "posix":
        raise ValueError("html.tree.native-output-initialization-unsupported")
    plan = plan_initial_html_tree_output(project_root, request)
    if canonical_identity(plan) != acknowledged_plan_identity:
        raise ValueError("html.tree.output-plan-acknowledgement-mismatch")
    project, output, namespace, index = _selection(project_root, request)
    project_node = _node(project.lstat())
    output_node = _node(output.lstat())
    if (
        _local_identity_wire(project_node) != plan["project_node"]
        or _local_identity_wire(output_node) != plan["output_node"]
    ):
        raise ValueError("html.tree.output-plan-stale")
    owner = canonical_json_bytes(
        {
            "schema": "literate-ai/initial-tree-html-output-assertion@1",
            "request_identity": request.identity.uri,
            "plan_identity": canonical_identity(plan).uri,
            "nonce": secrets.token_hex(32),
            "published": False,
            "recovery_is_not_live_ownership": True,
        }
    )
    if len(owner) > min(4096, request.limits.max_retained_generation_bytes):
        raise ValueError("html.tree.initial-output-storage-policy-exceeded")
    process = os.getpid()
    active = True
    provisional = None
    generation = None
    generation_inputs = None
    wrapper = None
    wrapper_fd = None
    wrapper_node = None
    wrapper_owner = None
    published_index = None
    pending_index = None
    issued_owner = None
    staging_started = False
    staging_contexts = ExitStack()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    output_fd = os.open(output, flags)
    namespace_fd = None
    try:
        if (
            _node(os.fstat(output_fd)) != output_node
            or plan_initial_html_tree_output(project, request) != plan
        ):
            raise ValueError("html.tree.output-plan-stale")
        os.mkdir(namespace.name, 0o700, dir_fd=output_fd)
        namespace_fd = os.open(namespace.name, flags, dir_fd=output_fd)
        namespace_node = _node(os.fstat(namespace_fd))
        if (
            namespace_node[0] != os.fstat(output_fd).st_dev
            or namespace_node[3] != os.geteuid()
            or stat.S_IMODE(namespace_node[2]) != 0o700
        ):
            raise ValueError("html.tree.private-direct-output-namespace-required")
        descriptor = os.open(
            "owner.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=namespace_fd,
        )
        try:
            owner_node = _node(os.fstat(descriptor))
            remaining = memoryview(owner)
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise ValueError("html.tree.output-owner-write-failed")
                remaining = remaining[count:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        def retained_memory():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.initial-output-context-ended")
            # Source geometry is shared with the caller and charged separately.
            # Keep both original maps charged after an owned move: their actual
            # contexts remain open until the parent exits, even though only the
            # generation observer is subsequently used for file currentness.
            return (
                65536
                + (0 if provisional is None else provisional.retained_memory_bytes)
                + (0 if generation is None else generation.retained_memory_bytes)
            )

        def require_paths():
            if not active or os.getpid() != process:
                raise ValueError("html.tree.initial-output-context-ended")
            if (
                _node(_root(project).lstat()) != project_node
                or _node(_root(output).lstat()) != output_node
                or _node(os.fstat(output_fd)) != output_node
                or _node(
                    os.stat(namespace.name, dir_fd=output_fd, follow_symlinks=False)
                )
                != namespace_node
                or _node(os.fstat(namespace_fd)) != namespace_node
            ):
                raise ValueError("html.tree.initial-output-custody-changed")
            if published_index is None:
                if os.path.lexists(index):
                    raise ValueError("html.tree.initial-index-raced")
            elif (
                _read_at(output_fd, index.name, len(published_index[1]))
                != published_index
            ):
                raise ValueError("html.tree.published-index-changed")

        def require_namespace(reserved_memory_bytes=0):
            allowed = {"owner.json"}
            if generation is not None:
                generation_path = generation.observed_path(
                    reserved_memory_bytes=(
                        reserved_memory_bytes
                        + 65536
                        + provisional.retained_memory_bytes
                    )
                )
                if (
                    generation_path.parent != namespace
                    or wrapper.parent != namespace
                    or _node(
                        os.stat(
                            wrapper.name, dir_fd=namespace_fd, follow_symlinks=False
                        )
                    )
                    != wrapper_node
                    or _node(os.fstat(wrapper_fd)) != wrapper_node
                    or _read_at(wrapper_fd, "owner.json", 4096) != wrapper_owner
                ):
                    raise ValueError("html.tree.published-generation-custody-changed")
                with os.scandir(wrapper_fd) as entries:
                    expected = {"owner.json"}
                    for entry in entries:
                        if entry.name not in expected:
                            raise ValueError("html.tree.foreign-published-stage-member")
                        expected.remove(entry.name)
                    if expected:
                        raise ValueError("html.tree.foreign-published-stage-member")
                allowed.update((wrapper.name, generation_path.name))
            elif provisional is not None:
                staged_wrapper = provisional.wrapper_path
                if staged_wrapper.parent != namespace:
                    raise ValueError("html.tree.foreign-provisional-output")
                allowed.add(staged_wrapper.name)
            if pending_index is not None:
                temporary, written = pending_index
                signature, content = _read_at(
                    namespace_fd, temporary.name, len(written.content)
                )
                if (
                    temporary.parent != namespace
                    or _publication_signature(signature) != written.signature
                    or content != written.content
                ):
                    raise ValueError("html.tree.index-staging-custody-changed")
                allowed.add(temporary.name)
            with os.scandir(namespace_fd) as entries:
                names = set()
                for entry in entries:
                    if entry.name not in allowed or entry.name in names:
                        raise ValueError("html.tree.foreign-initial-output-member")
                    names.add(entry.name)
            if names != allowed:
                raise ValueError("html.tree.initial-output-owner-missing")

        def require_current(*, reserved_memory_bytes=0):
            source_memory = (
                0
                if generation_inputs is None
                else generation_inputs[0].index.metadata_memory_bound
            )
            if (
                type(reserved_memory_bytes) is not int
                or not 0
                <= reserved_memory_bytes
                <= request.limits.max_metadata_memory_bytes
                or reserved_memory_bytes + source_memory + retained_memory()
                > request.limits.max_metadata_memory_bytes
            ):
                raise ValueError("html.tree.output-memory-policy-exceeded")
            require_paths()
            require_namespace(reserved_memory_bytes)
            signature, content = _read_at(namespace_fd, "owner.json", 4096)
            if signature[0] != owner_node or content != owner:
                raise ValueError("html.tree.initial-output-owner-changed")
            require_namespace(reserved_memory_bytes)
            require_paths()

        def stage_generation(tree, store, staged):
            nonlocal staging_started, provisional, generation_inputs
            require_current()
            if staging_started:
                raise ValueError("html.tree.initial-generation-already-reserved")
            if not isinstance(staged, StagedHtmlTreeGeneration) or any(
                type(value) is not int or value < 0 or value > 2**63 - 1
                for value in (
                    staged.page_count,
                    staged.page_bytes,
                    staged.metadata_bytes,
                    staged.metadata_record_count,
                )
            ):
                raise ValueError("html.tree.bounded-generation-reservation-required")
            # Descriptive counts are not trusted to create files: the physical
            # staging adapter first reproduces the entire deterministic CAS
            # generation and compares its exact receipt, before any mkdir/write.
            # Metadata totals conservatively count repeated CAS descriptors.
            # Reserve both private controls (each <=4096) and the future bounded
            # index before allowing the first generation byte to be written.
            reserved = (
                len(owner)
                + 4096
                + staged.page_bytes
                + staged.metadata_bytes
                + min(MAX_INITIAL_TREE_INDEX_BYTES, request.limits.max_page_bytes)
            )
            if reserved > request.limits.max_retained_generation_bytes:
                raise ValueError("html.tree.initial-generation-storage-policy-exceeded")
            staging_started = True

            def revalidate(candidate):
                nonlocal provisional
                if candidate is None:
                    if provisional is not None:
                        raise ValueError("html.tree.provisional-output-handoff-changed")
                else:
                    if provisional is not None:
                        raise ValueError(
                            "html.tree.provisional-output-handoff-repeated"
                        )
                    provisional = candidate
                require_current()

            observed = staging_contexts.enter_context(
                stage_html_tree_files(
                    namespace,
                    tree,
                    request,
                    store,
                    staged,
                    revalidate_output=revalidate,
                    reserved_memory_bytes=65536,
                )
            )
            if observed is not provisional:
                raise ValueError("html.tree.provisional-output-handoff-differs")
            generation_inputs = tree, store, staged
            require_current()
            return observed

        def publish_generation(
            require_source_current, source_observation, require_publication_current
        ):
            nonlocal generation, wrapper, wrapper_fd, wrapper_node
            nonlocal wrapper_owner, published_index, pending_index
            require_current()
            if (
                generation_inputs is None
                or provisional is None
                or generation is not None
                or published_index is not None
            ):
                raise ValueError("html.tree.actual-first-generation-required")
            source = _root(project / request.source_root)
            inside_source = namespace.is_relative_to(source) or index.is_relative_to(
                source
            )
            if source_observation is not None:
                from .html_tree_inputs import _require_html_tree_source_observation

                require_source_current = _require_html_tree_source_observation(
                    source_observation,
                    project,
                    request,
                    issued_owner,
                    expected_tree=generation_inputs[0],
                )
            elif inside_source:
                raise ValueError("html.tree.original-source-observation-required")
            if not callable(require_source_current):
                raise TypeError("html.tree.actual-source-currentness-required")
            if require_publication_current is not None and not callable(
                require_publication_current
            ):
                raise TypeError("html.tree.publication-currentness-callback-required")

            def require_all():
                if require_publication_current is not None:
                    require_publication_current()
                require_source_current()
                require_current()
                if require_publication_current is not None:
                    require_publication_current()

            tree, store, staged = generation_inputs
            content = _initial_index(request, staged)
            require_all()
            payload = provisional.payload_path
            wrapper = provisional.wrapper_path
            wrapper_fd = os.open(wrapper, flags)
            staging_contexts.callback(os.close, wrapper_fd)
            wrapper_node = _node(os.fstat(wrapper_fd))
            if wrapper_node != _node(wrapper.lstat()):
                raise ValueError("html.tree.provisional-wrapper-changed")
            wrapper_owner = _read_at(wrapper_fd, "owner.json", 4096)
            destination = namespace / ("g-" + staged.manifest.digest)
            expected_payload = _node(payload.lstat())
            observed = staging_contexts.enter_context(
                observe_html_tree_generation_files(
                    payload,
                    tree,
                    request,
                    store,
                    staged,
                    expected_node=expected_payload,
                    revalidate_parent_paths=require_paths,
                    reserved_memory_bytes=65536 + provisional.retained_memory_bytes,
                )
            )
            require_all()
            publish_directory_between_owned_parents_exclusive(
                payload,
                destination,
                expected_source=expected_payload[:3],
                expected_source_parent=wrapper_node[:3],
                expected_destination_parent=namespace_node[:3],
            )
            # Follow the original held directory/files across the actual move;
            # no saved manifest, node tuple or new path recreates an owner.
            observed.rebind_after_owned_move(destination)
            generation = observed
            require_current()
            os.fsync(wrapper_fd)
            os.fsync(namespace_fd)

            def before_commit():
                require_all()

            def index_staged(temporary, written):
                nonlocal pending_index
                if (
                    pending_index is not None
                    or temporary.parent != namespace
                    or not temporary.name.startswith(".litai-html-")
                    or len(temporary.name) > 128
                    or type(written) is not OutputSnapshot
                    or written.content != content
                ):
                    raise ValueError("html.tree.actual-index-staging-required")
                # The canonical writer has joined this actual temporary's inode
                # to its original creation descriptor. Accept only that one live
                # file, never an ignored name or a restored publication grant.
                pending_index = temporary, written
                require_current()

            try:
                created = publish_html(
                    index,
                    content,
                    None,
                    revalidate=before_commit,
                    maximum_bytes=min(
                        MAX_INITIAL_TREE_INDEX_BYTES, request.limits.max_page_bytes
                    ),
                    return_snapshot=True,
                    temporary_parent=namespace,
                    revalidate_staged=index_staged,
                )
            finally:
                pending_index = None
            snapshot = _read_at(output_fd, index.name, len(content))
            if (
                created is None
                or snapshot[1] != content
                or created.content != content
                or _publication_signature(snapshot[0]) != created.signature
            ):
                raise ValueError("html.tree.initial-index-publication-differs")
            published_index = snapshot
            os.fsync(output_fd)
            require_all()

        def require_published_index(snapshot):
            if (
                type(snapshot) is not OutputSnapshot
                or type(snapshot.content) is not bytes
                or not 0 < len(snapshot.content) <= MAX_INITIAL_TREE_INDEX_BYTES
            ):
                raise TypeError("html.tree.bounded-published-index-snapshot-required")
            reserved = 16 * len(snapshot.content)
            require_current(reserved_memory_bytes=reserved)
            if (
                published_index is None
                or _publication_signature(published_index[0]) != snapshot.signature
                or published_index[1] != snapshot.content
            ):
                raise ValueError("html.tree.published-index-observation-differs")
            require_current(reserved_memory_bytes=reserved)

        require_current()
        os.fsync(namespace_fd)
        os.fsync(output_fd)

        def capture_selection(selected_source, reserved_memory_bytes=0):
            require_current(reserved_memory_bytes=reserved_memory_bytes)
            selected_source = _root(Path(selected_source))
            if selected_source != _root(project / request.source_root):
                raise ValueError("html.tree.output-owner-source-selection-mismatch")
            directories = frozenset(
                (namespace.relative_to(selected_source).as_posix(),)
                if namespace.is_relative_to(selected_source)
                else ()
            )
            files = frozenset(
                (index.relative_to(selected_source).as_posix(),)
                if index.is_relative_to(selected_source) and published_index is not None
                else ()
            )
            return directories, files, require_current

        issued_owner = InitialHtmlTreeOutput(
            namespace,
            require_current,
            stage_generation,
            publish_generation,
            retained_memory=retained_memory,
            published_index_guard=require_published_index,
            _issuer=_ISSUER,
        )
        _CAPTURE_OWNERS[issued_owner] = capture_selection
        yield issued_owner
        require_current()
    finally:
        active = False
        if issued_owner is not None:
            _CAPTURE_OWNERS.pop(issued_owner, None)
        try:
            staging_contexts.close()
        finally:
            if namespace_fd is not None:
                os.close(namespace_fd)
            os.close(output_fd)
