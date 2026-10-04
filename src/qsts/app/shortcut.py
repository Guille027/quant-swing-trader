"""Create a "QSTS" shortcut on the Windows desktop (and in the Start menu) that opens the app with a double click.

Run once:  python -m qsts.app.shortcut   (or double-click "Crear acceso directo.bat" in the project folder)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from qsts.app.launcher import project_root


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def shortcut_script(link: Path, target: Path, arguments: str, workdir: Path, icon: Path, description: str) -> str:
    """PowerShell that writes a .lnk through the WScript.Shell COM object (no extra software needed)."""
    return "; ".join([
        "$ws = New-Object -ComObject WScript.Shell",
        f"$s = $ws.CreateShortcut({_ps_quote(link)})",
        f"$s.TargetPath = {_ps_quote(target)}",
        f"$s.Arguments = {_ps_quote(arguments)}",
        f"$s.WorkingDirectory = {_ps_quote(workdir)}",
        f"$s.IconLocation = {_ps_quote(str(icon) + ',0')}",
        f"$s.Description = {_ps_quote(description)}",
        "$s.Save()",
    ])


def _powershell(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def windowless_python() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def create() -> list[Path]:
    if sys.platform != "win32":
        raise SystemExit("El acceso directo de escritorio es solo para Windows. Usa: python -m qsts.cli desktop")
    root = project_root()
    icon = root / "src" / "qsts" / "ui" / "static" / "qsts.ico"
    target = windowless_python()
    made = []
    for folder in ("Desktop", "Programs"):  # Programs = Start menu (Desktop handles OneDrive redirection)
        base = Path(_powershell(f"[Environment]::GetFolderPath({_ps_quote(folder)})"))
        link = base / "QSTS.lnk"
        _powershell(shortcut_script(link, target, "-m qsts.app.launcher", root, icon,
                                    "QSTS: investigación de estrategias (abre la app)"))
        made.append(link)
    return made


def main() -> None:
    try:
        links = create()
    except Exception as e:  # noqa: BLE001
        print(f"No se pudo crear el acceso directo: {e}")
        sys.exit(1)
    print("Acceso directo creado:")
    for p in links:
        print(f"  {p}")
    print("\nYa puedes abrir QSTS con doble clic en el icono 'QSTS' de tu escritorio.")


if __name__ == "__main__":
    main()
