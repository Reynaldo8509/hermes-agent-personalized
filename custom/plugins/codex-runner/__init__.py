"""Explicit Codex CLI commands for Hermes.

Only /codex_full opts into Codex's full-access bypass. /codex is a safe,
read-only non-interactive run; Telegram cannot display Codex's interactive
approval prompt. Both run as the Hermes service user, never root.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


LOG = Path.home() / ".hermes" / "logs" / "codex-runner.log"
RESULT_LOG = Path.home() / ".hermes" / "logs" / "codex-runner-results.jsonl"
_LOG_LOCK = threading.Lock()
MAX_PROMPT = 12000
MAX_OUTPUT = 14000
SUMMARY_THRESHOLD = 1000
MAX_SUMMARY = 1600
MAX_TELEGRAM_RESPONSE = 1800
TIMEOUT = 1800
TELEGRAM_TARGETS = {
    "display_one": "telegram:8968754596",
    "reynaldo": "telegram:5029489710",
    "device_two": "telegram:device_two",
}


def _send_telegram(recipient: str, message: str) -> str:
    """Send through Hermes' configured Telegram bot to a fixed recipient."""
    message = str(message or "").strip()
    if not message:
        return f"Uso: /{recipient.title()} <mensaje>"
    try:
        from tools.send_message_tool import send_message_tool

        result = send_message_tool({
            "action": "send",
            "target": TELEGRAM_TARGETS[recipient],
            "message": message,
        })
        data = json.loads(result) if isinstance(result, str) else result
        if data.get("success"):
            return f"Mensaje enviado por Hermes a {recipient.title()} (id {data.get('message_id', '?')})."
        return f"Hermes no pudo enviar el mensaje a {recipient.title()}: {data.get('error', 'error de entrega')}"
    except Exception as exc:
        return f"Hermes no pudo enviar el mensaje a {recipient.title()}: {type(exc).__name__}"


def _run(prompt: str, full: bool, command: str | None = None) -> str:
    prompt = str(prompt or "").strip()
    if not prompt:
        return "Uso: /codex <tarea> o /codex_full <tarea>"
    if len(prompt) > MAX_PROMPT:
        return f"Tarea demasiado larga; máximo {MAX_PROMPT} caracteres."

    args = ["/usr/bin/codex"]
    if full:
        args.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        # ``codex exec`` is non-interactive and cannot ask for approval via
        # Telegram. Do not inherit the user's permissive global profile.
        args += ["--sandbox", "read-only"]
    # Hermes may be invoked from $HOME, which is not necessarily a Git
    # checkout. This flag only disables Codex's repository-location guard; it
    # does not alter the normal sandbox/approval policy.
    args += ["exec", "--ephemeral", "--color", "never", "--skip-git-repo-check", prompt]
    env = dict(os.environ)
    env.update({"HOME": "$HOME", "CODEX_HOME": "$HOME/.codex"})
    started = time.monotonic()
    status = "ERROR"
    output = ""
    try:
        proc = subprocess.run(
            args,
            cwd="$HOME",
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        status = "OK" if proc.returncode == 0 else f"EXIT_{proc.returncode}"
    except subprocess.TimeoutExpired:
        output, status = "Tiempo de espera agotado.", "TIMEOUT"
    except OSError as exc:
        output, status = f"No se pudo ejecutar Codex: {type(exc).__name__}.", "OSERROR"
    finally:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        elapsed = round(time.monotonic() - started, 2)
        audit = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "surface": "slash_command",
            "command": command or ("codex_full" if full else "codex"),
            "mode": "full" if full else "standard",
            "status": status,
            "elapsed_s": elapsed,
        }
        result_record = {
            **audit,
            "prompt": prompt,
            "result": output,
        }
        with _LOG_LOCK:
            with LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(audit, ensure_ascii=False) + "\n")
            with RESULT_LOG.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result_record, ensure_ascii=False) + "\n")
            try:
                LOG.chmod(0o600)
                RESULT_LOG.chmod(0o600)
            except OSError:
                pass

    if not output:
        output = "Codex terminó sin salida visible."
    if len(output) > MAX_OUTPUT:
        output = output[-MAX_OUTPUT:]
        output = "[salida recortada]\n" + output
    return output


def _record_summary(command: str, summary: str) -> None:
    """Store the Telegram-facing summary without replacing the full result."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "telegram_summary",
        "surface": "slash_command",
        "command": command,
        "summary": summary,
    }
    with _LOG_LOCK:
        with RESULT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            RESULT_LOG.chmod(0o600)
        except OSError:
            pass


def _telegram_response(ctx, raw: str, full: bool, command: str) -> str:
    """Run Codex and use Hermes' active LLM to make Telegram replies concise."""
    result = _run(raw, full, command)
    if len(result) <= SUMMARY_THRESHOLD:
        return result
    try:
        llm_result = ctx.llm.complete(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Resume la respuesta de Codex para Telegram en español. "
                        "Entrega primero la conclusión y después solo los datos, "
                        "errores y acciones importantes. Máximo 8 viñetas breves. "
                        "No menciones que estás resumiendo ni inventes información."
                    ),
                },
                {"role": "user", "content": result},
            ],
            max_tokens=700,
            temperature=0.2,
            purpose="codex_telegram_summary",
        )
        summary = str(llm_result.text or "").strip()
        if summary:
            if len(summary) > MAX_SUMMARY:
                summary = summary[:MAX_SUMMARY].rstrip() + "…"
            _record_summary(command, summary)
            return summary
    except Exception as exc:
        _record_summary(command, f"No se pudo generar el resumen LLM: {type(exc).__name__}")
    return result[:MAX_TELEGRAM_RESPONSE].rstrip() + "\n[respuesta recortada por Telegram]"


def register(ctx):
    ctx.register_command(
        "display_one",
        lambda raw: _send_telegram("display_one", raw),
        "Envía un mensaje por Hermes a display_one; uso: /display_one <mensaje>",
    )
    ctx.register_command(
        "reynaldo",
        lambda raw: _send_telegram("reynaldo", raw),
        "Envía un mensaje por Hermes a Reynaldo; uso: /Reynaldo <mensaje>",
    )
    ctx.register_command(
        "device_two",
        lambda raw: _send_telegram("device_two", raw),
        "Envía un mensaje por Hermes a device_two; uso: /device_two <mensaje>",
    )
    ctx.register_command(
        "codex_full",
        lambda raw: _telegram_response(ctx, raw, True, "codex_full"),
        "Codex con acceso completo; uso: /codex_full <tarea>",
    )
    # Telegram's gateway normalizes underscores to hyphens during dispatch.
    # Keep this internal alias so /codex_full reaches the same handler.
    ctx.register_command(
        "codex-full",
        lambda raw: _telegram_response(ctx, raw, True, "codex_full"),
        "Codex con acceso completo; uso: /codex_full <tarea>",
    )
    ctx.register_command(
        "codex",
        lambda raw: _telegram_response(ctx, raw, False, "codex"),
        "Codex restringido, solo lectura; uso: /codex <tarea>",
    )
