"""Qualify real loopback Git transport without editing host SSH configuration."""

import argparse
import hashlib
import json
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from literate_ai.adapters.source.repository_git import (
    GitRepositorySourceAcquirer,
    GitRepositorySourceCapturer,
)
from literate_ai.contracts import (
    DependencyKind,
    RepositoryRevisionKind,
    RepositoryRevisionSelector,
    RepositorySourceDependency,
)

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--fixture", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--project", type=Path, required=True)
p.add_argument("--qualification-script", type=Path, required=True)
a = p.parse_args()
root = a.output.resolve()
root.mkdir(mode=0o700)
source = root / "source"
shutil.copytree(a.fixture.resolve(strict=True), source)
# The authored producer separates ignored build outputs from captured source.
(source / ".gitignore").write_text("_build/\n")


def run(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, timeout=30, **kw)


for argv in (
    ["git", "init", "--template=", "--quiet"],
    ["git", "add", "."],
    [
        "git",
        "-c",
        "user.name=SDK transport qualification",
        "-c",
        "user.email=sdk@example.test",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--quiet",
        "-m",
        "Exact SDK qualification input",
    ],
):
    run(argv, cwd=source)
revision = run(["git", "rev-parse", "HEAD"], cwd=source).stdout.decode().strip()
for name in ("host", "client"):
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)])
account = pwd.getpwuid(os.getuid()).pw_name
public = (root / "client.pub").read_text().strip()
(root / "authorized_keys").write_text(public + "\n")
(root / "authorized_keys").chmod(0o600)
# sshd checks every parent of an authorized-key file. Keep that one temporary
# copy below the actual account's private home, rather than world-writable /tmp.
authorized_root = Path(
    tempfile.mkdtemp(prefix=".litai-sdk-ssh-", dir=pwd.getpwuid(os.getuid()).pw_dir)
)
authorized = authorized_root / "keys"
authorized.write_text(public + "\n")
authorized.chmod(0o600)
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
backend = Path(shutil.which("git-upload-pack")).resolve(strict=True)
config = (
    "\n".join(
        [
            "ListenAddress 127.0.0.1",
            f"Port {port}",
            f"HostKey {root / 'host'}",
            f"PidFile {root / 'sshd.pid'}",
            f"AuthorizedKeysFile {authorized}",
            "PasswordAuthentication no",
            "KbdInteractiveAuthentication no",
            "UsePAM no",
            "StrictModes yes",
            f"AllowUsers {account}",
            "PermitRootLogin no",
            "AllowAgentForwarding no",
            "AllowTcpForwarding no",
            "X11Forwarding no",
            "PermitTTY no",
            "PermitUserEnvironment no",
            "PermitUserRC no",
            "ForceCommand " + shlex.join([str(backend), str(source)]),
        ]
    )
    + "\n"
)
(root / "sshd_config").write_text(config)
host_public = (root / "host.pub").read_text().split()
(root / "known_hosts").write_text(
    f"[127.0.0.1]:{port} {host_public[0]} {host_public[1]}\n"
)
ssh = shlex.join(
    [
        shutil.which("ssh"),
        "-F",
        "/dev/null",
        "-i",
        str(root / "client"),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=" + str(root / "known_hosts"),
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=5",
    ]
)
sshd = "/usr/sbin/sshd"
with (root / "sshd.log").open("xb") as log:
    server = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(root / "sshd_config")], stdout=log, stderr=log
    )
    result = {
        "schema": "literate-ai/sdk-source-transport-qualification@1",
        "canonical_installed_cli_qualified": False,
        "compiler_sdk_admitted": False,
        "release_acceptance": False,
    }
    try:
        for _ in range(50):
            if server.poll() is not None:
                raise RuntimeError("Owned SSH server exited before readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Owned SSH server readiness deadline")
        # This is an actual authentication configuration, not a transport mock.
        os.environ["GIT_SSH_COMMAND"] = ssh
        dependency = RepositorySourceDependency(
            "native-sdk-qualification",
            f"ssh://{account}@127.0.0.1:{port}{source}",
            RepositoryRevisionSelector(RepositoryRevisionKind.COMMIT, revision),
            DependencyKind.BUILD,
        )
        with GitRepositorySourceAcquirer().acquire(dependency) as checkout:
            capture = GitRepositorySourceCapturer().capture(checkout)
            assert checkout.resolved_commit == revision
            expected = {
                str(f.relative_to(source)): hashlib.sha256(f.read_bytes()).hexdigest()
                for f in source.rglob("*")
                if f.is_file() and ".git" not in f.relative_to(source).parts
            }
            actual = {
                str(f.relative_to(checkout.source_root)): hashlib.sha256(
                    f.read_bytes()
                ).hexdigest()
                for f in checkout.source_root.rglob("*")
                if f.is_file()
                and ".git" not in f.relative_to(checkout.source_root).parts
            }
            assert actual == expected and actual
            result.update(
                ok=True,
                resolved_commit=revision,
                files=len(actual),
                source_snapshot=capture.snapshot.identity.uri,
                source_tree=capture.snapshot.tree_identity.uri,
            )
        child = subprocess.run(
            [
                sys.executable,
                "-I",
                str(a.qualification_script),
                "--project",
                str(a.project),
                "--source-url",
                dependency.repository_url,
                "--source-commit",
                revision,
                "--source-license",
                str(source / "LICENSE"),
                "--output",
                str(root / "qualification"),
            ],
            timeout=6500,
        )
        result["qualification_exit_code"] = child.returncode
        assert child.returncode == 0

    except BaseException as exc:
        result.update(ok=False, error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        result["owned_server_terminal"] = server.returncode
        authorized.unlink()
        authorized_root.rmdir()
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
