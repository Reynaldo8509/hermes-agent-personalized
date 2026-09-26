"""Printer controls using the VPS CUPS queue for HP Smart Tank 710-720."""
import os
import subprocess
import tempfile
from pathlib import Path

_QUEUE = "hp_display_one"
_ALLOWED_ROOTS = (Path("$HOME/hermes/outputs").resolve(), Path("/tmp").resolve())

def _cups(*args):
    return subprocess.run(["/snap/cups/current/bin/" + args[0], *args[1:]], text=True, capture_output=True, timeout=20, check=False)

def _status(_raw=""):
    r = _cups("lpstat", "-p", "-d", "-v")
    if r.returncode:
        return "Impresora no disponible: " + (r.stderr or r.stdout).strip()[:300]
    return "Impresora HP Smart Tank: cola hp_display_one operativa. " + " ".join(r.stdout.split())[:500]

def _safe_path(raw):
    p = Path((raw or "").strip()).expanduser().resolve()
    return p if any(p == root or root in p.parents for root in _ALLOWED_ROOTS) else None

def _print_file(raw):
    p = _safe_path(raw)
    if not p or not p.is_file():
        return "Uso: /imprimir_archivo <archivo dentro de hermes/outputs>."
    r = _cups("lp", "-d", _QUEUE, str(p))
    return f"Trabajo de impresion enviado: {r.stdout.strip()}" if r.returncode == 0 else "No se pudo imprimir: " + (r.stderr or r.stdout).strip()[:300]

def _print_text(raw):
    text = (raw or "").strip()
    if not text:
        return "Uso: /imprimir_texto <texto>."
    if len(text) > 12000:
        return "El texto supera el limite de 12000 caracteres."
    fd, name = tempfile.mkstemp(prefix="hermes-print-", suffix=".txt", dir="/tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f: f.write(text + "\n")
        r = _cups("lp", "-d", _QUEUE, name)
        return f"Trabajo de impresion enviado: {r.stdout.strip()}" if r.returncode == 0 else "No se pudo imprimir: " + (r.stderr or r.stdout).strip()[:300]
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass

def register(ctx):
    ctx.register_command("impresora-status", _status, "Consulta el estado de la impresora HP", "")
    ctx.register_command("imprimir-archivo", _print_file, "Imprime un archivo autorizado", "")
    ctx.register_command("imprimir-texto", _print_text, "Imprime texto bajo demanda", "")
