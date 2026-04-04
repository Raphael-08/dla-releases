#!/usr/bin/env python3
"""
DLA Installer — install & uninstall with structured JSON output.

Zero third-party dependencies (stdlib only).
Designed to be driven by the Tauri desktop app over SSH.

Usage:
    python3 installer.py install  [--version 2.4.2] [--repo user/repo] [--token TOKEN] [--wheel /tmp/dla.whl] [--port 8420] [--no-serve]
    python3 installer.py uninstall [--yes] [--keep-data]
    python3 installer.py status

Every action emits one JSON line per event:
    {"step":"java","status":"running","detail":"Downloading JDK 21..."}
    {"step":"java","status":"done","detail":"Installed to ~/.dla/java/"}
    {"step":"java","status":"failed","error":"HTTP 404"}
    {"step":"java","status":"skipped","detail":"Already installed"}
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import urllib.error
import urllib.request
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

HOME = Path.home()
DLA_HOME = Path(os.environ.get("DLA_HOME", HOME / ".dla"))
DLA_CONFIG = Path(os.environ.get("DLA_CONFIG_DIR", HOME / "config"))
LOCAL_BIN = HOME / ".local" / "bin"

PYTHON_VERSION = "3.12"
JAVA_VERSION = "21"
SPARK_VERSION = "4.1.1"

ADOPTIUM_TAG = "jdk-21.0.5%2B11"
ADOPTIUM_DIR_NAME = "jdk-21.0.5+11"
ADOPTIUM_BASE = "https://github.com/adoptium/temurin21-binaries/releases/download"

SPARK_MIRROR = "https://dlcdn.apache.org/spark"
SPARK_ARCHIVE = "https://archive.apache.org/dist/spark"

DEFAULT_REPO = "Raphael-08/dla"
DEFAULT_PORT = 8420


# ── JSON emitter ─────────────────────────────────────────────────────────────

def emit(step: str, status: str, **kw: str) -> None:
    """Print a single JSON event line. Tauri provisioner parses these."""
    print(json.dumps({"step": step, "status": status, **kw}), flush=True)


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: str | list[str], *, check: bool = True, capture: bool = True, stream_step: str = "") -> subprocess.CompletedProcess:
    """Run a shell command. Uses shell=True for string commands.
    If stream_step is set, streams stderr/stdout as JSON events (for long-running commands).
    """
    is_shell = isinstance(cmd, str)
    env = {**os.environ, "PATH": f"{LOCAL_BIN}:{os.environ.get('PATH', '')}"}

    if stream_step:
        import threading
        import time as _time

        # Stream output line-by-line so the UI shows progress during long installs.
        proc = subprocess.Popen(
            cmd, shell=is_shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        stdout_lines: list[str] = []
        last_output = _time.monotonic()

        def _reader() -> None:
            nonlocal last_output
            for raw in proc.stdout:  # type: ignore[union-attr]
                line = raw.rstrip()
                if line:
                    stdout_lines.append(line)
                    # Skip noisy DEBUG lines, show the useful ones
                    if not line.startswith("DEBUG"):
                        emit(stream_step, "running", detail=line)
                    last_output = _time.monotonic()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        # Emit progress pings while waiting
        while t.is_alive():
            t.join(timeout=3.0)
            if t.is_alive():
                elapsed = int(_time.monotonic() - last_output)
                if elapsed >= 3:
                    emit(stream_step, "running", detail=f"Installing... ({elapsed}s)")

        proc.wait()
        result = subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(stdout_lines), "")
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, result.stdout, result.stderr)
        return result

    return subprocess.run(
        cmd,
        shell=is_shell,
        capture_output=capture,
        text=True,
        env=env,
        check=check,
    )


def which(name: str) -> str | None:
    """Find executable on PATH (including ~/.local/bin)."""
    for d in [str(LOCAL_BIN)] + os.environ.get("PATH", "").split(":"):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def download(url: str, dest: Path, *, token: str = "") -> None:
    """Download a URL to a local file. Supports GitHub token auth."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


def detect_platform() -> tuple[str, str]:
    """Return (os, arch) normalized for download URLs."""
    os_name = platform.system().lower()  # linux, darwin
    machine = platform.machine().lower()
    arch_map = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}
    return os_name, arch_map.get(machine, machine)


def shell_rc() -> Path:
    """Return the user's shell RC file."""
    zshrc = HOME / ".zshrc"
    return zshrc if zshrc.exists() else HOME / ".bashrc"


def add_to_rc(line: str) -> None:
    """Append a line to the shell RC if not already present."""
    rc = shell_rc()
    if rc.exists() and line in rc.read_text():
        return
    with open(rc, "a") as f:
        f.write(f"{line}\n")


def is_semver(version: str) -> bool:
    """Check if version looks like a release (X.Y.Z)."""
    return bool(re.match(r"^\d+\.\d+\.\d+", version))


# ── Install steps ────────────────────────────────────────────────────────────

def install_uv() -> None:
    """Install uv package manager."""
    step = "uv"
    if which("uv"):
        ver = run("uv --version").stdout.strip()
        emit(step, "skipped", detail=f"Already installed: {ver}")
        return

    emit(step, "running", detail="Installing uv...")
    try:
        script = "/tmp/uv_install.sh"
        download("https://astral.sh/uv/install.sh", Path(script))
        run(f"sh {script} </dev/null")
        Path(script).unlink(missing_ok=True)
    except Exception as e:
        emit(step, "failed", error=f"uv installation failed: {e}")
        sys.exit(1)

    if not which("uv"):
        emit(step, "failed", error="uv binary not found after install")
        sys.exit(1)

    add_to_rc('export PATH="$HOME/.local/bin:$PATH"')
    emit(step, "done", detail="Installed")


def install_python() -> None:
    """Ensure Python 3.12+ is available."""
    step = "python"
    emit(step, "running", detail=f"Checking Python {PYTHON_VERSION}...")

    # Check existing Python
    for cmd in ["python3", "python"]:
        if which(cmd):
            try:
                ver = run(f"{cmd} -c \"import sys; print(f'{{sys.version_info.major}}.{{sys.version_info.minor}}')\"").stdout.strip()
                major, minor = ver.split(".")
                if int(minor) >= 12:
                    emit(step, "skipped", detail=f"Python {ver} found")
                    return
            except Exception:
                continue

    emit(step, "running", detail=f"Installing Python {PYTHON_VERSION} via uv...")
    try:
        run(f"uv python install {PYTHON_VERSION}")
        emit(step, "done", detail=f"Python {PYTHON_VERSION} installed")
    except subprocess.CalledProcessError as e:
        emit(step, "failed", error=f"Python install failed: {e.stderr}")
        sys.exit(1)


def install_java(*, skip: bool = False) -> Path | None:
    """Install Adoptium JDK 21. Returns JAVA_HOME path."""
    step = "java"
    java_home = DLA_HOME / "java" / ADOPTIUM_DIR_NAME

    if skip:
        emit(step, "skipped", detail="Skipped (--skip-java)")
        return None

    # Check system Java
    if which("java"):
        try:
            ver_out = run("java -version", check=False).stderr
            if '"21' in ver_out:
                emit(step, "skipped", detail="Java 21 found (system)")
                return None
        except Exception:
            pass

    # Check DLA-managed Java
    if java_home.is_dir():
        emit(step, "skipped", detail=f"Found at {java_home}")
        os.environ["JAVA_HOME"] = str(java_home)
        os.environ["PATH"] = f"{java_home / 'bin'}:{os.environ.get('PATH', '')}"
        return java_home

    # Download
    os_name, arch = detect_platform()
    if os_name == "linux":
        filename = f"OpenJDK21U-jdk_{arch}_linux_hotspot_21.0.5_11.tar.gz"
    elif os_name == "darwin":
        filename = f"OpenJDK21U-jdk_{arch}_mac_hotspot_21.0.5_11.tar.gz"
    else:
        emit(step, "failed", error=f"Unsupported OS: {os_name}")
        sys.exit(1)

    url = f"{ADOPTIUM_BASE}/{ADOPTIUM_TAG}/{filename}"
    tmp_tar = Path("/tmp/java.tar.gz")

    emit(step, "running", detail="Downloading Adoptium JDK 21...")
    try:
        DLA_HOME.joinpath("java").mkdir(parents=True, exist_ok=True)
        download(url, tmp_tar)
        emit(step, "running", detail="Extracting JDK...")
        with tarfile.open(tmp_tar) as tf:
            tf.extractall(DLA_HOME / "java")
        tmp_tar.unlink(missing_ok=True)

        # macOS bundles Contents/Home inside the JDK directory
        contents_home = java_home / "Contents" / "Home"
        if os_name == "darwin" and contents_home.is_dir():
            tmp = java_home.with_name(java_home.name + "_tmp")
            java_home.rename(tmp)
            (tmp / "Contents" / "Home").rename(java_home)
            shutil.rmtree(tmp)

    except Exception as e:
        emit(step, "failed", error=f"Java install failed: {e}")
        sys.exit(1)

    os.environ["JAVA_HOME"] = str(java_home)
    os.environ["PATH"] = f"{java_home / 'bin'}:{os.environ.get('PATH', '')}"
    add_to_rc(f'export JAVA_HOME="{java_home}"')
    add_to_rc('export PATH="$JAVA_HOME/bin:$PATH"')
    emit(step, "done", detail=f"Installed to {java_home}")
    return java_home


def install_spark(*, skip: bool = False) -> Path | None:
    """Install Apache Spark. Returns SPARK_HOME path."""
    step = "spark"
    spark_dir = DLA_HOME / "spark" / f"spark-{SPARK_VERSION}-bin-hadoop3"

    if skip:
        emit(step, "skipped", detail="Skipped (--skip-spark)")
        return None

    if spark_dir.is_dir():
        emit(step, "skipped", detail=f"Found at {spark_dir}")
        os.environ["SPARK_HOME"] = str(spark_dir)
        os.environ["PATH"] = f"{spark_dir / 'bin'}:{os.environ.get('PATH', '')}"
        return spark_dir

    existing = os.environ.get("SPARK_HOME")
    if existing and Path(existing).is_dir():
        emit(step, "skipped", detail=f"SPARK_HOME already set: {existing}")
        return Path(existing)

    # Download from primary mirror, fall back to archive
    tgz_name = f"spark-{SPARK_VERSION}-bin-hadoop3.tgz"
    primary = f"{SPARK_MIRROR}/spark-{SPARK_VERSION}/{tgz_name}"
    fallback = f"{SPARK_ARCHIVE}/spark-{SPARK_VERSION}/{tgz_name}"
    tmp_tgz = Path("/tmp/spark.tgz")

    emit(step, "running", detail=f"Downloading Spark {SPARK_VERSION}...")
    DLA_HOME.joinpath("spark").mkdir(parents=True, exist_ok=True)

    try:
        try:
            download(primary, tmp_tgz)
        except Exception:
            emit(step, "running", detail="Primary mirror failed, trying archive...")
            download(fallback, tmp_tgz)

        emit(step, "running", detail="Extracting Spark...")
        with tarfile.open(tmp_tgz) as tf:
            tf.extractall(DLA_HOME / "spark")
        tmp_tgz.unlink(missing_ok=True)

    except Exception as e:
        emit(step, "failed", error=f"Spark install failed: {e}")
        sys.exit(1)

    os.environ["SPARK_HOME"] = str(spark_dir)
    os.environ["PATH"] = f"{spark_dir / 'bin'}:{os.environ.get('PATH', '')}"
    add_to_rc(f'export SPARK_HOME="{spark_dir}"')
    add_to_rc('export PATH="$SPARK_HOME/bin:$PATH"')
    emit(step, "done", detail=f"Installed to {spark_dir}")
    return spark_dir


def install_dla(*, version: str, repo: str, token: str, wheel: str) -> None:
    """Install DLA via uv tool install.

    Strategy (in priority order):
    1. Local wheel provided (--wheel)     → install directly
    2. Semver version (e.g. 2.4.2)        → download wheel from GitHub Releases
    3. Token + any version                 → git clone private repo
    4. Branch name (e.g. main)            → git clone public repo
    """
    step = "dla"
    emit(step, "running", detail="Installing DLA...")

    # Clean previous installation
    if which("dla"):
        emit(step, "running", detail="Removing previous installation...")
        run("uv tool uninstall dla", check=False)
        (LOCAL_BIN / "dla").unlink(missing_ok=True)

    try:
        if wheel:
            _install_from_wheel(step, wheel)
        elif is_semver(version):
            _install_from_release(step, version, repo, token)
        elif token:
            emit(step, "running", detail="Installing from private repository...")
            run(f"uv tool install --verbose \"git+https://{token}@github.com/{repo}.git@{version}\" --force", stream_step=step)
        else:
            emit(step, "running", detail=f"Installing from branch {version}...")
            run(f"uv tool install --verbose \"git+https://github.com/{repo}.git@{version}\" --force", stream_step=step)

    except subprocess.CalledProcessError as e:
        emit(step, "failed", error=f"DLA install failed: {e.stderr or e.stdout}")
        sys.exit(1)

    if not which("dla"):
        emit(step, "failed", error="DLA binary not found after installation")
        sys.exit(1)

    ver_out = run("dla --version", check=False).stdout.strip().split("\n")[0]
    emit(step, "done", detail=f"Installed: {ver_out}")


def _install_from_wheel(step: str, wheel: str) -> None:
    """Install from a local .whl file."""
    wheel_path = Path(wheel)
    # Ensure wheel has a valid PEP 440 version in its filename
    match = re.match(r"^dla-(.+?)-py3", wheel_path.name)
    ver_part = match.group(1) if match else ""
    if not re.match(r"^\d", ver_part):
        safe_wheel = Path("/tmp/dla-0.0.0-py3-none-any.whl")
        shutil.copy2(wheel_path, safe_wheel)
        wheel = str(safe_wheel)
    emit(step, "running", detail=f"Installing from wheel: {Path(wheel).name}")
    run(f"uv tool install --verbose \"{wheel}\" --force", stream_step=step)


def _install_from_release(step: str, version: str, repo: str, token: str) -> None:
    """Download a wheel from GitHub Releases and install it."""
    ver = version.replace("v", "")
    wheel_name = f"dla-{ver}-py3-none-any.whl"
    wheel_url = f"https://github.com/{repo}/releases/download/v{ver}/{wheel_name}"
    tmp_wheel = Path(f"/tmp/{wheel_name}")

    emit(step, "running", detail=f"Downloading DLA v{ver} from releases...")
    try:
        download(wheel_url, tmp_wheel, token=token)
    except Exception as e:
        raise subprocess.CalledProcessError(
            1, "download", "", f"Failed to download wheel from {wheel_url}: {e}"
        ) from e

    emit(step, "running", detail=f"Installing {wheel_name}...")
    run(f"uv tool install --verbose \"{tmp_wheel}\" --force", stream_step=step)
    tmp_wheel.unlink(missing_ok=True)


def init_config() -> None:
    """Initialize DLA config directory."""
    step = "init"
    emit(step, "running", detail="Initializing config...")
    run(f"dla init -p \"{HOME / 'config'}\"", check=False)
    emit(step, "done", detail=f"Config at {HOME / 'config'}")


def create_service(*, port: int, java_home: Path | None, spark_home: Path | None) -> None:
    """Create and start a systemd user service for dla serve."""
    step = "service"

    service_dir = HOME / ".config" / "systemd" / "user"
    service_dir.mkdir(parents=True, exist_ok=True)

    java_bin = f"{java_home}/bin" if java_home else ""
    spark_bin = f"{spark_home}/bin" if spark_home else ""
    path_parts = [str(LOCAL_BIN)]
    if java_bin:
        path_parts.append(java_bin)
    if spark_bin:
        path_parts.append(spark_bin)
    path_parts.extend(["/usr/local/bin", "/usr/bin", "/bin"])

    env_lines = [f"Environment=PATH={':'.join(path_parts)}"]
    if java_home:
        env_lines.append(f"Environment=JAVA_HOME={java_home}")
    if spark_home:
        env_lines.append(f"Environment=SPARK_HOME={spark_home}")

    unit = textwrap.dedent(f"""\
        [Unit]
        Description=DLA API Server
        After=network.target

        [Service]
        ExecStart={LOCAL_BIN / 'dla'} serve --port {port}
        Restart=always
        RestartSec=5
        {chr(10).join(env_lines)}

        [Install]
        WantedBy=default.target
    """)

    service_file = service_dir / "dla-serve.service"
    service_file.write_text(unit)

    emit(step, "running", detail="Starting systemd service...")
    run("loginctl enable-linger $(whoami)", check=False)
    run("systemctl --user daemon-reload", check=False)
    run("systemctl --user enable dla-serve", check=False)
    run("systemctl --user start dla-serve", check=False)

    # Poll for health instead of blind sleep
    emit(step, "running", detail="Waiting for API...")
    healthy = False
    import time
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2):
                healthy = True
                break
        except Exception:
            time.sleep(0.5)

    if not healthy:
        # Fallback: start manually without systemd
        emit(step, "running", detail="systemd unavailable, starting manually...")
        run(
            f"nohup {LOCAL_BIN / 'dla'} serve --port {port} > {DLA_HOME / 'serve.log'} 2>&1 &",
            check=False,
        )
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=2):
                    healthy = True
                    break
            except Exception:
                time.sleep(0.5)

    if healthy:
        emit(step, "done", detail=f"API responding on port {port}")
    else:
        emit(step, "failed", error=f"API not responding on port {port} after 25s")
        sys.exit(1)


# ── Uninstall steps ──────────────────────────────────────────────────────────

def uninstall_stop() -> None:
    """Kill running DLA processes."""
    step = "stop"
    emit(step, "running", detail="Stopping DLA processes...")
    result = run("pkill -f 'dla serve'", check=False)
    if result.returncode == 0:
        emit(step, "done", detail="Killed running DLA API")
    else:
        emit(step, "skipped", detail="No running DLA process")


def uninstall_service() -> None:
    """Remove systemd service."""
    step = "service"
    emit(step, "running", detail="Removing systemd service...")
    run("systemctl --user stop dla-serve", check=False)
    run("systemctl --user disable dla-serve", check=False)

    service_file = HOME / ".config" / "systemd" / "user" / "dla-serve.service"
    if service_file.exists():
        service_file.unlink()
        run("systemctl --user daemon-reload", check=False)
        emit(step, "done", detail="Service removed")
    else:
        emit(step, "skipped", detail="No service file found")


def uninstall_cli() -> None:
    """Uninstall DLA CLI via uv."""
    step = "cli"
    emit(step, "running", detail="Uninstalling DLA CLI...")

    if which("uv"):
        uv_list = run("uv tool list", check=False).stdout or ""
        if re.search(r"^dla ", uv_list, re.MULTILINE):
            run("uv tool uninstall dla", check=False)
            emit(step, "done", detail="Uninstalled via uv")
        else:
            emit(step, "skipped", detail="DLA not in uv tools")
    else:
        emit(step, "skipped", detail="uv not found")

    # Remove leftover binary
    dla_bin = LOCAL_BIN / "dla"
    if dla_bin.exists() or dla_bin.is_symlink():
        dla_bin.unlink()


def uninstall_uv() -> None:
    """Remove uv, managed Python runtimes, and cache."""
    step = "uv"
    emit(step, "running", detail="Removing uv ecosystem...")
    removed = []

    targets = [
        (HOME / ".local" / "share" / "uv" / "python", "Python runtimes"),
        (HOME / ".local" / "share" / "uv" / "tools", "Tool environments"),
        (Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "uv", "Cache"),
        (HOME / ".local" / "share" / "uv", "Data directory"),
    ]

    for path, label in targets:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(label)

    # Remove uv binaries
    for name in ["uv", "uvx"]:
        p = LOCAL_BIN / name
        if p.exists():
            p.unlink()

    if removed:
        emit(step, "done", detail=f"Removed: {', '.join(removed)}")
    else:
        emit(step, "skipped", detail="uv not installed")


def uninstall_data() -> None:
    """Remove DLA home (Java, Spark, logs) and config."""
    step = "data"
    emit(step, "running", detail="Removing DLA data...")
    removed = []

    if DLA_HOME.is_dir():
        shutil.rmtree(DLA_HOME, ignore_errors=True)
        removed.append(str(DLA_HOME))

    # Only remove DLA-specific items from config dir (don't nuke unrelated files)
    if DLA_CONFIG.is_dir():
        for item in ["config.yaml", "connections", "jobs", "logs"]:
            target = DLA_CONFIG / item
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
                removed.append(str(target))
        # Remove config dir if empty
        try:
            DLA_CONFIG.rmdir()
            removed.append(str(DLA_CONFIG))
        except OSError:
            pass  # not empty — other files exist

    if removed:
        emit(step, "done", detail=f"Removed: {', '.join(removed)}")
    else:
        emit(step, "skipped", detail="No DLA data found")


def uninstall_shell_rc() -> None:
    """Remove DLA-added lines from shell RC files."""
    step = "shell_rc"
    emit(step, "running", detail="Cleaning shell config...")
    cleaned_any = False

    for rc_path in [HOME / ".bashrc", HOME / ".zshrc"]:
        if not rc_path.is_file():
            continue
        lines = rc_path.read_text().splitlines()
        original_count = len(lines)

        # Filter out DLA-related lines
        filtered = []
        for line in lines:
            is_dla = any([
                ".dla/" in line,
                "added by DLA installer" in line,
                line.startswith('export PATH="$JAVA_HOME/bin'),
                line.startswith('export PATH="$SPARK_HOME/bin'),
                ".local/bin" in line and "uv" in line,
            ])
            if not is_dla:
                filtered.append(line)

        if len(filtered) < original_count:
            rc_path.write_text("\n".join(filtered) + "\n")
            cleaned_any = True

    if cleaned_any:
        emit(step, "done", detail="Shell RC cleaned")
    else:
        emit(step, "skipped", detail="No DLA lines found")


# ── Status command ───────────────────────────────────────────────────────────

def check_status() -> None:
    """Emit status of each component."""
    components = {
        "uv": which("uv"),
        "python": which("python3"),
        "java": str(DLA_HOME / "java" / ADOPTIUM_DIR_NAME) if (DLA_HOME / "java" / ADOPTIUM_DIR_NAME).is_dir() else which("java"),
        "spark": str(DLA_HOME / "spark" / f"spark-{SPARK_VERSION}-bin-hadoop3") if (DLA_HOME / "spark" / f"spark-{SPARK_VERSION}-bin-hadoop3").is_dir() else None,
        "dla": which("dla"),
        "config": str(DLA_CONFIG) if DLA_CONFIG.is_dir() else None,
        "service": None,
    }

    # Check systemd service
    svc = run("systemctl --user is-active dla-serve", check=False)
    if "active" in svc.stdout:
        components["service"] = "active"

    for name, value in components.items():
        if value:
            emit(name, "installed", detail=str(value))
        else:
            emit(name, "missing")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="DLA Installer")
    sub = parser.add_subparsers(dest="command", required=True)

    # install
    p_install = sub.add_parser("install")
    p_install.add_argument("--version", default=os.environ.get("DLA_VERSION", "master"))
    p_install.add_argument("--repo", default=os.environ.get("DLA_REPO", DEFAULT_REPO))
    p_install.add_argument("--token", default="")
    p_install.add_argument("--wheel", default="")
    p_install.add_argument("--port", type=int, default=int(os.environ.get("DLA_PORT", DEFAULT_PORT)))
    p_install.add_argument("--no-serve", action="store_true")
    p_install.add_argument("--skip-java", action="store_true")
    p_install.add_argument("--skip-spark", action="store_true")

    # uninstall
    p_uninstall = sub.add_parser("uninstall")
    p_uninstall.add_argument("--yes", "-y", action="store_true")
    p_uninstall.add_argument("--keep-data", action="store_true")

    # status
    sub.add_parser("status")

    args = parser.parse_args()
    DLA_HOME.mkdir(parents=True, exist_ok=True)

    if args.command == "install":
        emit("start", "running", detail=f"Installing DLA to {DLA_HOME}")

        install_uv()
        install_python()
        java_home = install_java(skip=args.skip_java)
        spark_home = install_spark(skip=args.skip_spark)
        install_dla(version=args.version, repo=args.repo, token=args.token, wheel=args.wheel)
        init_config()
        if not args.no_serve:
            create_service(port=args.port, java_home=java_home, spark_home=spark_home)
        else:
            emit("service", "skipped", detail="--no-serve")

        emit("complete", "done", detail="Installation complete")

    elif args.command == "uninstall":
        emit("start", "running", detail="Uninstalling DLA")

        uninstall_stop()
        uninstall_service()
        uninstall_cli()
        if not args.keep_data:
            uninstall_uv()
            uninstall_data()
            uninstall_shell_rc()
        else:
            emit("uv", "skipped", detail="--keep-data")
            emit("data", "skipped", detail="--keep-data")
            emit("shell_rc", "skipped", detail="--keep-data")

        emit("complete", "done", detail="Uninstall complete")

    elif args.command == "status":
        check_status()


if __name__ == "__main__":
    main()
