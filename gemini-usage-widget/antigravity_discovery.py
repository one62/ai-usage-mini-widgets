"""Discover Antigravity executables, daemon files, running processes, and TCP listening ports."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PROCESS_PREFIXES = ("language_server", "agy", "antigravity")
_CSRF_TOKEN_RE = re.compile(r'--csrf[_-]token(?:=|\s+)(?:"([^"]+)"|\'([^\']+)\'|(\S+))')
_SERVER_PORT_RE = re.compile(
    r"--(?:hub[-_]port|https[-_]server[-_]port|http[-_]server[-_]port|server[-_]port|connect[-_]port|port)(?:=|\s+)(\d+)"
)
_EXT_PORT_RE = re.compile(r"--extension[-_]server[-_]port(?:=|\s+)(\d+)")
_URL_PORT_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)", re.IGNORECASE)

_WIN32_LOCK = threading.Lock()
_WIN32_PROCESS_APIS: tuple[ctypes.WinDLL, ctypes.WinDLL] | None = None
_IPHLPAPI_CACHE: ctypes.WinDLL | None = None
_TCP_TABLE_LOCAL = threading.local()


@dataclass(frozen=True)
class _LanguageServerProcess:
    pid: int
    name: str
    command_line: str
    csrf_token: str
    server_ports: tuple[int, ...] = ()
    extension_port: int | None = None
    priority: int = 1


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_void_p),
    ]


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("ucLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("ucRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwState", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _get_win32_process_apis() -> tuple[ctypes.WinDLL, ctypes.WinDLL]:
    global _WIN32_PROCESS_APIS
    with _WIN32_LOCK:
        if _WIN32_PROCESS_APIS is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
            kernel32.Process32FirstW.restype = wintypes.BOOL
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
            kernel32.Process32NextW.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtQueryInformationProcess.argtypes = [
                wintypes.HANDLE,
                wintypes.ULONG,
                ctypes.c_void_p,
                wintypes.ULONG,
                ctypes.POINTER(wintypes.ULONG),
            ]
            ntdll.NtQueryInformationProcess.restype = ctypes.c_long
            _WIN32_PROCESS_APIS = (kernel32, ntdll)
        return _WIN32_PROCESS_APIS


def _get_iphlpapi() -> ctypes.WinDLL:
    global _IPHLPAPI_CACHE
    with _WIN32_LOCK:
        if _IPHLPAPI_CACHE is None:
            iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
            iphlpapi.GetExtendedTcpTable.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.BOOL,
                wintypes.ULONG,
                ctypes.c_int,
                wintypes.ULONG,
            ]
            iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
            _IPHLPAPI_CACHE = iphlpapi
        return _IPHLPAPI_CACHE


def find_antigravity_executables() -> list[str]:
    """Find Antigravity desktop executables on Windows."""
    candidates: list[Path] = []
    override = os.environ.get("GEMINI_USAGE_WIDGET_ANTIGRAVITY")
    if override:
        candidates.append(Path(override).expanduser())

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Antigravity" / "Antigravity.exe")

    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Antigravity" / "Antigravity.exe")

    for command in ("Antigravity.exe", "antigravity", "agy"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))

    available: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.resolve(strict=True)
            if not path.is_file():
                continue
            resolved = str(path)
        except OSError:
            continue
        key = os.path.normcase(resolved)
        if key not in seen:
            seen.add(key)
            available.append(resolved)
    return available


def _append_valid_port(ports: list[int], raw_port: Any) -> None:
    if raw_port is None or isinstance(raw_port, bool):
        return
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        return
    if 0 < port <= 65535 and port not in ports:
        ports.append(port)


def parse_command_line_ports(command_line: str) -> tuple[list[int], int | None]:
    """Extract (server_ports, extension_server_port) from an Antigravity process command line."""
    if not command_line:
        return [], None
    server_ports: list[int] = []
    for match in _SERVER_PORT_RE.finditer(command_line):
        _append_valid_port(server_ports, match.group(1))

    ext_match = _EXT_PORT_RE.search(command_line)
    ext_port: int | None = None
    if ext_match:
        try:
            candidate = int(ext_match.group(1))
            if 0 < candidate <= 65535:
                ext_port = candidate
        except ValueError:
            ext_port = None
    return server_ports, ext_port


def parse_language_server_command_line(command_line: str) -> tuple[str | None, int | None]:
    """Extract (csrf_token, port) from a language_server or agy command line."""
    if not command_line:
        return None, None
    token_match = _CSRF_TOKEN_RE.search(command_line)
    csrf_token: str | None = None
    if token_match:
        csrf_token = next((group for group in token_match.groups() if group), None)

    server_ports, ext_port = parse_command_line_ports(command_line)
    fallback_port = ext_port if ext_port is not None else (server_ports[0] if server_ports else None)
    return csrf_token, fallback_port


def _is_antigravity_process_name(exe_name: str, command_line: str) -> bool:
    name_lower = exe_name.lower()
    if any(name_lower.startswith(prefix) for prefix in _PROCESS_PREFIXES):
        return True
    cmd_lower = command_line.lower()
    return name_lower == "node.exe" and ("antigravity" in cmd_lower or "agy" in cmd_lower)


def _process_priority(exe_name: str, command_line: str) -> int:
    name_lower = exe_name.lower()
    cmd_lower = command_line.lower()
    if "--standalone" in cmd_lower or "--hub" in cmd_lower or name_lower.startswith("agy"):
        return 0
    if "antigravity" in cmd_lower or name_lower.startswith("antigravity"):
        return 1
    return 2


def _build_process_candidate(pid: int, name: str, cmd: str) -> _LanguageServerProcess | None:
    if not _is_antigravity_process_name(name, cmd):
        return None
    csrf_token, _ = parse_language_server_command_line(cmd)
    if not csrf_token:
        return None
    server_ports, ext_port = parse_command_line_ports(cmd)
    return _LanguageServerProcess(
        pid=pid,
        name=name,
        command_line=cmd,
        csrf_token=csrf_token,
        server_ports=tuple(server_ports),
        extension_port=ext_port,
        priority=_process_priority(name, cmd),
    )


def _discover_daemon_candidates(daemon_dir: Path | None = None) -> list[_LanguageServerProcess]:
    """Read local Antigravity daemon metadata files (e.g. ls_*.json) if accessible."""
    root = daemon_dir or (Path.home() / ".gemini" / "antigravity" / "daemon")
    try:
        if not root.is_dir():
            return []
        files = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []

    found: list[_LanguageServerProcess] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        csrf_token: str | None = None
        for key in ("csrfToken", "csrf_token", "token"):
            raw = data.get(key)
            if isinstance(raw, str) and raw.strip():
                csrf_token = raw.strip()
                break
        if not csrf_token:
            continue

        ports: list[int] = []
        for key in (
            "httpsPort",
            "https_port",
            "port",
            "httpPort",
            "http_port",
            "hubPort",
            "hub_port",
            "serverPort",
            "server_port",
            "connectPort",
            "connect_port",
        ):
            _append_valid_port(ports, data.get(key))
        for url_key in ("url", "endpoint", "baseUrl", "base_url"):
            raw_url = data.get(url_key)
            if isinstance(raw_url, str):
                match = _URL_PORT_RE.search(raw_url)
                if match:
                    _append_valid_port(ports, match.group(1))

        pid = 0
        raw_pid = data.get("pid") if "pid" in data else data.get("processId")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid

        if not ports and pid <= 0:
            continue

        found.append(
            _LanguageServerProcess(
                pid=pid,
                name=path.name,
                command_line="",
                csrf_token=csrf_token,
                server_ports=tuple(ports),
                extension_port=None,
                priority=0,
            )
        )
    return found


def _read_process_command_line_win32(pid: int) -> str | None:
    """Query a process command line using NtQueryInformationProcess(ProcessCommandLineInformation)."""
    if os.name != "nt" or pid <= 0:
        return None
    try:
        kernel32, ntdll = _get_win32_process_apis()
    except OSError:
        return None

    process_query_limited_information = 0x1000
    process_command_line_information = 60

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        needed = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(
            handle,
            process_command_line_information,
            None,
            0,
            ctypes.byref(needed),
        )
        buf_size = max(int(needed.value), 4096)
        buf = ctypes.create_string_buffer(buf_size)
        status = ntdll.NtQueryInformationProcess(
            handle,
            process_command_line_information,
            buf,
            buf_size,
            ctypes.byref(needed),
        )
        if status != 0:
            return None
        ustr = _UNICODE_STRING.from_buffer_copy(buf)
        if not ustr.Buffer or ustr.Length == 0:
            return None
        return ctypes.wstring_at(ustr.Buffer, ustr.Length // ctypes.sizeof(wintypes.WCHAR))
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(handle)


def _discover_processes_win32_status() -> tuple[bool, list[_LanguageServerProcess]]:
    if os.name != "nt":
        return False, []
    try:
        kernel32, _ = _get_win32_process_apis()
    except OSError:
        return False, []

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snapshot or snapshot == invalid_handle:
        return False, []

    found: list[_LanguageServerProcess] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            exe_name = entry.szExeFile
            name_lower = exe_name.lower()
            if any(name_lower.startswith(prefix) for prefix in _PROCESS_PREFIXES) or name_lower == "node.exe":
                pid = int(entry.th32ProcessID)
                cmd = _read_process_command_line_win32(pid) or ""
                candidate = _build_process_candidate(pid, exe_name, cmd)
                if candidate is not None:
                    found.append(candidate)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)

    found.sort(key=lambda proc: (proc.priority, -proc.pid))
    return True, found


def _discover_processes_win32() -> list[_LanguageServerProcess]:
    return _discover_processes_win32_status()[1]


def _discover_processes_powershell() -> list[_LanguageServerProcess]:
    if os.name != "nt":
        return []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    ps_command = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name LIKE 'language_server%' OR Name LIKE 'agy%' OR Name LIKE 'antigravity%' "
        "OR (Name = 'node.exe' AND (CommandLine LIKE '%antigravity%' OR CommandLine LIKE '%agy%'))\" "
        "| Select-Object ProcessId,Name,CommandLine "
        "| ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []

    items = parsed if isinstance(parsed, list) else [parsed]
    found: list[_LanguageServerProcess] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        pid_raw = item.get("ProcessId")
        cmd = str(item.get("CommandLine") or "")
        name = str(item.get("Name") or "")
        if not isinstance(pid_raw, int) or not cmd:
            continue
        candidate = _build_process_candidate(pid_raw, name, cmd)
        if candidate is not None:
            found.append(candidate)
    found.sort(key=lambda proc: (proc.priority, -proc.pid))
    return found


def _deduplicate_processes(processes: list[_LanguageServerProcess]) -> list[_LanguageServerProcess]:
    merged: list[_LanguageServerProcess] = []
    by_key: dict[tuple[int, str], int] = {}
    for proc in processes:
        key = (proc.pid, proc.csrf_token) if proc.pid > 0 else (-1, proc.csrf_token)
        if key in by_key:
            idx = by_key[key]
            existing = merged[idx]
            ports = list(existing.server_ports)
            for port in proc.server_ports:
                if port not in ports:
                    ports.append(port)
            merged[idx] = _LanguageServerProcess(
                pid=existing.pid or proc.pid,
                name=existing.name,
                command_line=existing.command_line or proc.command_line,
                csrf_token=existing.csrf_token,
                server_ports=tuple(ports),
                extension_port=existing.extension_port or proc.extension_port,
                priority=min(existing.priority, proc.priority),
            )
        else:
            by_key[key] = len(merged)
            merged.append(proc)
    return merged


def _discover_language_server_processes() -> list[_LanguageServerProcess]:
    candidates = _discover_daemon_candidates()
    win32_ok, processes = _discover_processes_win32_status()
    if not win32_ok:
        processes = _discover_processes_powershell()
    return _deduplicate_processes(candidates + processes)


def _query_tcp_table_all(
    iphlpapi: ctypes.WinDLL,
    family: int,
    row_cls: type[ctypes.Structure],
) -> tuple[bool, dict[int, list[int]]]:
    tcp_table_owner_pid_listener = 3
    error_insufficient_buffer = 122

    size = wintypes.DWORD(0)
    res = iphlpapi.GetExtendedTcpTable(
        None,
        ctypes.byref(size),
        False,
        family,
        tcp_table_owner_pid_listener,
        0,
    )
    if res not in (0, error_insufficient_buffer):
        return False, {}
    if size.value == 0:
        return True, {}

    buf = None
    for _ in range(3):
        alloc_size = int(size.value) + 1024
        size = wintypes.DWORD(alloc_size)
        buf = ctypes.create_string_buffer(alloc_size)
        res = iphlpapi.GetExtendedTcpTable(
            buf,
            ctypes.byref(size),
            False,
            family,
            tcp_table_owner_pid_listener,
            0,
        )
        if res == 0:
            break
        if res != error_insufficient_buffer:
            return False, {}
    else:
        return False, {}

    assert buf is not None
    num_entries = wintypes.DWORD.from_buffer_copy(buf, 0).value
    row_size = ctypes.sizeof(row_cls)
    offset = ctypes.sizeof(wintypes.DWORD)
    ports_by_pid: dict[int, list[int]] = {}
    for _ in range(int(num_entries)):
        if offset + row_size > len(buf):
            break
        row = row_cls.from_buffer_copy(buf, offset)
        offset += row_size
        owning_pid = int(row.dwOwningPid)
        if owning_pid <= 0:
            continue
        raw_port = int(row.dwLocalPort) & 0xFFFF
        port = ((raw_port & 0xFF) << 8) | ((raw_port >> 8) & 0xFF)
        bucket = ports_by_pid.setdefault(owning_pid, [])
        _append_valid_port(bucket, port)
    return True, ports_by_pid


def _get_all_listening_ports_win32() -> tuple[bool, dict[int, list[int]]]:
    """Return (ok, {pid: [ports]}) for local IPv4/IPv6 TCP listeners via GetExtendedTcpTable."""
    if os.name != "nt":
        return False, {}
    try:
        iphlpapi = _get_iphlpapi()
    except OSError:
        return False, {}

    combined: dict[int, list[int]] = {}
    any_ok = False
    for family, row_cls in (
        (2, _MIB_TCPROW_OWNER_PID),
        (23, _MIB_TCP6ROW_OWNER_PID),
    ):
        ok, table = _query_tcp_table_all(iphlpapi, family, row_cls)
        if ok:
            any_ok = True
            for pid, pid_ports in table.items():
                dest = combined.setdefault(pid, [])
                for port in pid_ports:
                    if port not in dest:
                        dest.append(port)
    return any_ok, combined


def _get_listening_ports_win32(target_pid: int) -> list[int]:
    """Return local IPv4/IPv6 TCP listening ports owned by target_pid via GetExtendedTcpTable."""
    if os.name != "nt" or target_pid <= 0:
        return []
    ok, table = _get_all_listening_ports_win32()
    return list(table.get(target_pid, ())) if ok else []


def _get_listening_ports_netstat(target_pid: int) -> list[int]:
    if os.name != "nt" or target_pid <= 0:
        return []
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=4.0,
            creationflags=creation_flags,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    ports: list[int] = []
    pid_str = str(target_pid)
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if not parts[0].upper().startswith("TCP") or parts[3].upper() != "LISTENING" or parts[4] != pid_str:
            continue
        local_addr = parts[1]
        if ":" not in local_addr:
            continue
        port_part = local_addr.rsplit(":", 1)[-1]
        _append_valid_port(ports, port_part)
    return ports


def _candidate_ports_for_process(proc: _LanguageServerProcess) -> list[int]:
    ports: list[int] = list(proc.server_ports)
    if proc.pid > 0:
        cached_table = getattr(_TCP_TABLE_LOCAL, "snapshot", None)
        if cached_table is not None:
            win32_ok, table = cached_table
            os_ports = list(table.get(proc.pid, ())) if win32_ok else []
        else:
            win32_ok, table = _get_all_listening_ports_win32()
            os_ports = list(table.get(proc.pid, ())) if win32_ok else []
        if not win32_ok:
            os_ports = _get_listening_ports_netstat(proc.pid)
        for port in os_ports:
            if port not in ports:
                ports.append(port)
    if proc.extension_port and proc.extension_port not in ports:
        ports.append(proc.extension_port)
    return ports
