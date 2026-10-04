"""Standard runtime-driver source and persistent-service process projection."""

from __future__ import annotations

from pathlib import Path

from literate_ai._filesystem import path_is_link_or_reparse
from literate_ai.contracts import (
    LITAI_SERVE_MODE_FLAG,
    LITAI_SMOKE_MODE_FLAG,
    canonical_relative_posix_path,
)

STANDARD_ELIXIR_RUNTIME_DRIVER = (
    "[_artifact,export,layout,relative|arguments]=System.argv();"
    'path=if layout=="file",do: export,else: Path.join(export,relative);'
    "System.argv(arguments);Code.require_file(path)"
)

STANDARD_PYTHON_RUNTIME_DRIVER = (
    "import pathlib,runpy,sys;"
    "sys.dont_write_bytecode=True;"
    "_artifact,export,layout,relative,*arguments=sys.argv[1:];"
    "path=pathlib.Path(export) if layout=='file' else "
    "pathlib.Path(export)/pathlib.PurePosixPath(relative);"
    "sys.path.insert(0,str(path.parent));"
    "sys.argv=[str(path),*arguments];runpy.run_path(str(path),run_name='__main__')"
)

# This is a single locked argv token. Avoid braces, which command contracts reserve
# for role placeholders. The driver stays in the application process for services.
_PYTHON_SDK_BOOTSTRAP = """import hashlib,json,os,pathlib,runpy,stat,sys,types
sys.dont_write_bytecode=True
manifest,_artifact,export,layout,relative,*arguments=sys.argv[1:]
manifest=pathlib.Path(manifest)
def require(ok):
    if not ok: raise ValueError('native SDK runtime input verification failed')
require(sys.flags.isolated and not manifest.is_symlink())
raw=manifest.read_bytes()
require(len(raw)<=16777216 and hashlib.sha256(raw).hexdigest()==manifest.stem)
document=json.loads(raw)
require(document['schema']=='literate-ai/native-sdk-execution-inputs@1')
def child(root,name):
    value=pathlib.PurePosixPath(name)
    require(not value.is_absolute() and value.parts and '..' not in value.parts)
    path=root
    for part in value.parts:
        path=path/part
        require(not path.is_symlink())
        require(not bool(getattr(path.lstat(),'st_file_attributes',0)&1024))
    return path
roots=[]
handles=[]
sdk_bindings=[]
for item in document['imports']:
    root=child(manifest.parent,item['root'])
    snapshot=item['snapshot']
    require(snapshot['import_surface']['language']=='python')
    require(snapshot['import_surface']['package']!='literate_ai_native_sdk')
    require(all(
        value['module'].split('.')[0]!='literate_ai_native_sdk'
        for value in snapshot['import_surface']['capabilities']))
    expected=set()
    for value in snapshot['files']:
        path=child(root,value['path'])
        info=path.stat()
        require(stat.S_ISREG(info.st_mode))
        with path.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        require(digest==value['blob']['digest'] and info.st_size==value['blob']['size'])
        require(bool(info.st_mode&0o111)==value['executable'])
        expected.add(value['path'])
    actual=set()
    for path in root.rglob('*'):
        require(not path.is_symlink())
        require(not bool(getattr(path.lstat(),'st_file_attributes',0)&1024))
        if not path.is_dir(): actual.add(path.relative_to(root).as_posix())
    require(actual==expected)
    encoded=json.dumps(snapshot,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
    sdk_bindings.append((snapshot['import_surface']['package'],types.MappingProxyType(dict(
        sdk_snapshot_identity='sha256:'+hashlib.sha256(encoded).hexdigest(),
        target_identity='sha256:'+snapshot['target_identity']['digest']))))
    roots.append(str(child(root,snapshot['import_root'])))
    if os.name=='nt':
        for name in snapshot['native_libraries']:
            handles.append(os.add_dll_directory(str(child(root,name).parent)))
def sdk_binding_factory(bindings):
    def binding(package):
        matches=[value for name,value in bindings if name==package]
        if not isinstance(package,str) or not package or len(matches)!=1:
            raise LookupError('native SDK package binding is absent or ambiguous')
        return matches[0]
    return binding
require('literate_ai_native_sdk' not in sys.modules)
sdk_metadata=types.ModuleType('literate_ai_native_sdk')
sdk_metadata.binding=sdk_binding_factory(tuple(sdk_bindings))
sys.modules['literate_ai_native_sdk']=sdk_metadata
"""

STANDARD_PYTHON_SDK_RUNTIME_DRIVER = (
    _PYTHON_SDK_BOOTSTRAP + "path=pathlib.Path(export) if layout=='file' else "
    "child(pathlib.Path(export),relative)\n"
    """require(path.is_file() and not path.is_symlink())
sys.path[:0]=[*roots,str(path.parent)]
sys.argv=[str(path),*arguments]
runpy.run_path(str(path),run_name='__main__')
"""
)

STANDARD_PYTHON_SDK_LIBRARY_IMPORT_DRIVER = (
    _PYTHON_SDK_BOOTSTRAP
    + """import base64,importlib,zlib
require(layout=='tree' and arguments==['--litai-smoke'])
source=child(pathlib.Path(export),'source').resolve(strict=True)
require(source.is_dir())
surface=json.loads(zlib.decompress(base64.urlsafe_b64decode(relative)))
require(surface['language']=='python')
sys.path[:0]=[str(source),*roots]
observed=[]
for capability in surface['capabilities']:
    module=importlib.import_module(capability['module'])
    origin=pathlib.Path(module.__file__).resolve(strict=True)
    require(source==origin or source in origin.parents)
    for symbol in capability['symbols']: getattr(module,symbol)
    observed.append(capability['capability'])
print(json.dumps(dict(schema='literate-ai/library-import-observation@1',capabilities=observed),sort_keys=True,separators=(',',':')))
"""
)

# The wheel profile invokes this with -I -S -B. No site startup hooks, .pth
# execution, PYTHONPATH or ambient site-packages enter the selected interpreter.
# Artifact custody must be checked by the lifecycle before launching this driver.
STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER = (
    "import pathlib,runpy,sys;"
    "artifact,export,layout,relative,*arguments=sys.argv[1:];"
    "root=pathlib.Path(artifact).resolve(strict=True);"
    "packages=root/'python-runtime'/'site';"
    "packages.is_dir() or sys.exit('verified Python payload is absent');"
    "layout=='tree' or sys.exit('Python wheel runtime requires a tree export');"
    "path=pathlib.Path(export)/pathlib.PurePosixPath(relative);"
    "sys.path[:0]=[str(path.parent),str(packages)];"
    "sys.argv=[str(path),*arguments];runpy.run_path(str(path),run_name='__main__')"
)

# The child stdio is inherited rather than captured so a one-shot generated command
# writes directly to the lifecycle driver's descriptors. Persistent-service acceptance
# unwraps this driver through ``direct_service_process_argv`` below: readiness and
# process ownership must observe the server itself, not this intermediate process.
# This source is one argv token in a locked command, so it must contain no ``{``/``}``
# characters (they would be read as portable placeholders).
STANDARD_NODE_RUNTIME_DRIVER = (
    "const p=require('path');"
    "const cp=require('child_process');"
    "const [artifact,exp,layout,relative,...args]=process.argv.slice(1);"
    "const entry=layout==='file'?exp:p.join(exp,...relative.split('/'));"
    "const options=Object.fromEntries([['stdio','inherit']]);"
    "const child=cp.spawn(process.execPath,[entry,...args],options);"
    "child.on('error',(err)=>process.exit(1));"
    "child.on('exit',(code)=>process.exitCode=Number.isInteger(code)?code:1);"
)

STANDARD_NATIVE_RUNTIME_DRIVER = (
    "import subprocess,sys;"
    "_artifact,export,*arguments=sys.argv[1:];"
    "raise SystemExit(subprocess.run([export,*arguments]).returncode)"
)


def _entrypoint(export: str, layout: str, relative: str) -> Path:
    if layout == "file":
        entrypoint = Path(export)
    elif layout == "tree":
        portable = canonical_relative_posix_path(
            relative, label="Standard runtime entrypoint"
        )
        export_root = Path(export)
        if path_is_link_or_reparse(export_root) or not export_root.is_dir():
            raise ValueError("Standard runtime tree export is not a regular directory")
        entrypoint = export_root
        for part in portable.parts:
            entrypoint /= part
            if path_is_link_or_reparse(entrypoint):
                raise ValueError("Standard runtime entrypoint path contains a link")
    else:
        raise ValueError("Standard runtime driver has an unknown artifact layout")
    if path_is_link_or_reparse(entrypoint) or not entrypoint.is_file():
        raise ValueError("Standard runtime entrypoint is not a regular packaged file")
    return entrypoint


def direct_service_process_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Make the packaged service, rather than its runtime wrapper, the owned PID.

    Standard TEST/EXECUTE commands use small drivers so file/tree artifacts share one
    locked command shape. A persistent service is different: its liveness and process
    tree are acceptance evidence, so the lifecycle must own the actual entrypoint.
    Only the exact three Standard driver shapes are unwrapped. An otherwise valid
    direct command retains the issue-214 smoke-to-serve substitution.
    """

    if not argv or argv[-1] != LITAI_SMOKE_MODE_FLAG:
        raise ValueError(
            "persistent-service EXECUTE command does not end with the expected "
            "runtime dispatcher mode"
        )

    if len(argv) >= 9 and argv[-8:-5] == ("-e", STANDARD_ELIXIR_RUNTIME_DRIVER, "--"):
        tool = argv[:-8]
        if not tool:
            raise ValueError("Standard Elixir runtime command has no bound tool")
        _artifact, export, layout, relative, _mode = argv[-5:]
        entrypoint = _entrypoint(export, layout, relative)
        return (*tool, str(entrypoint), LITAI_SERVE_MODE_FLAG)

    if len(argv) >= 8:
        tool = argv[:-7]
        flag, driver, _artifact, export, layout, relative, _mode = argv[-7:]
        if (flag, driver) in {
            ("-c", STANDARD_PYTHON_RUNTIME_DRIVER),
            ("-e", STANDARD_NODE_RUNTIME_DRIVER),
        }:
            if not tool:
                raise ValueError("Standard runtime command has no bound tool")
            entrypoint = _entrypoint(export, layout, relative)
            return (*tool, str(entrypoint), LITAI_SERVE_MODE_FLAG)

    if len(argv) >= 6:
        _tool, flag, driver, _artifact, export, _mode = argv[-6:]
        if flag == "-c" and driver == STANDARD_NATIVE_RUNTIME_DRIVER:
            entrypoint = _entrypoint(export, "file", export)
            return (str(entrypoint), LITAI_SERVE_MODE_FLAG)

    return (*argv[:-1], LITAI_SERVE_MODE_FLAG)


__all__ = [
    "STANDARD_NATIVE_RUNTIME_DRIVER",
    "STANDARD_ELIXIR_RUNTIME_DRIVER",
    "STANDARD_NODE_RUNTIME_DRIVER",
    "STANDARD_PYTHON_RUNTIME_DRIVER",
    "STANDARD_PYTHON_SDK_RUNTIME_DRIVER",
    "STANDARD_PYTHON_SDK_LIBRARY_IMPORT_DRIVER",
    "STANDARD_PYTHON_WHEEL_RUNTIME_DRIVER",
    "direct_service_process_argv",
]
