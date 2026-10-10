from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext, suppress
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
from arcbench_agent_runtime import AgentRuntime

import yaml
import engineering
import batch_splitter


@dataclass(frozen=True)
class RequirementModule:
    index: int
    total: int
    node_id: str
    name: str
    subtree: dict[str, Any]
    frozen_script: bytes | None = None
    chain_titles: tuple[str, ...] = ()
    planning_authority: dict[str, Any] | None = None
    frozen_version: int = 1
    root_module_id: str | None = None
    batch_id: str | None = None
    batch_plan_sha256: str | None = None
    batch_context: dict[str, Any] | None = None


_DEFAULT_TEMPLATE_EVOLUTION_ALLOWLIST = {
    "sheets": (
        "Evolution Requirements for an Online Spreadsheet Data Workspace",
        frozenset({"REQ-1-1-1", "REQ-1-3-2", "REQ-3-1-3", "REQ-4-1-1"}),
    ),
    "github": (
        "GitHub Collaboration Platform Core Requirements",
        frozenset(),
    ),
}


# Soft limits only record a warning; hard limits are the interrupt boundary.
MODEL_TURN_SOFT_TIMEOUT_SECONDS = 1200.0
MODEL_TURN_HARD_TIMEOUT_SECONDS = 2700.0
MODEL_TURN_RETRY_HARD_TIMEOUT_SECONDS = 1800.0
# Backward-compatible name used by focused tests and callers.
MODEL_TURN_TIMEOUT_SECONDS = MODEL_TURN_HARD_TIMEOUT_SECONDS


class ModelTurnTimeout(RuntimeError):
    """A model turn exceeded the host's bounded wait."""

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__(f"model turn timed out after {timeout_seconds:g} seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Implement an ARC-Bench task with Codex.")
    parser.add_argument("requirement_path", help="Directory containing requirements.yaml.")
    parser.add_argument("--output-dir", required=True, help="Target directory for the generated project.")
    parser.add_argument("--resume-from", default=os.environ.get("ARCBENCH_RESUME_FROM"),
                        help="Previous output directory or a copied codex-recovery directory.")
    parser.add_argument(
        "--type",
        dest="task_type",
        default=os.environ.get("ARCBENCH_TASK_TYPE", "web"),
        help="Task type supplied by ARC-Bench.",
    )
    return parser.parse_args()


def copy_template_contents(template_dir: Path, output_dir: Path) -> None:
    """Copy the bundled template's children into the output directory."""
    if not template_dir.is_dir():
        # ARC-Bench assembles the authoritative task template before invoking
        # the agent (and evolution runs already contain the previous-stage
        # application). Reference archives intentionally omit a duplicate
        # template directory, so preserve the prepared output in that case.
        if output_dir.is_dir() and any(output_dir.iterdir()):
            return
        raise FileNotFoundError(f"Starter template directory not found: {template_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml" or _excluded_snapshot_path(source.relative_to(template_dir)) or source.is_symlink():
            continue
        destination = output_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True,
                            ignore=lambda parent, names: [name for name in names
                                if _excluded_snapshot_path((Path(parent) / name).relative_to(template_dir))
                                or (Path(parent) / name).is_symlink()])
        else:
            shutil.copy2(source, destination)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> str:
    message = str(value)
    for key, secret in os.environ.items():
        if len(secret) >= 8 and any(marker in key.upper() for marker in
                                    ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")):
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)((?:api[_-]?key|token|secret|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(https?://)[^/?#\s]+@", r"\1[REDACTED]@", message)
    message = re.sub(
        r"(?i)([?&][\w-]*(?:key|token|secret|password|auth|credential|signature)[\w-]*=)[^&#\s]+",
        r"\1[REDACTED]", message,
    )
    return message[:8000]


def append_event(output_dir: Path, event: dict[str, Any]) -> None:
    target = output_dir / ".arc" / "codex-recovery" / "events.jsonl"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(_redact_json(event), ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        print(f"[reporting] event write failed: {redact(exc)}", file=sys.stderr)


def digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def digest_bundled_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if name.lower() not in {
            ".git", ".arc", "__pycache__", "node_modules", "dist", "build", ".cache",
            ".pytest_cache", ".venv", "venv", "recovery-input"
        } and not name.lower().endswith((".egg-info", ".dist-info")))
        for name in sorted(files):
            if name.lower().endswith((".pyc", ".log")):
                continue
            path = parent / name
            if path.is_symlink():
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def input_identity(requirements_dir: Path, agent_root: Path) -> dict[str, Any]:
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    return {
        "requirements_sha256": digest_tree(requirements_dir),
        "template_sha256": digest_tree(agent_root / "template") if (agent_root / "template").is_dir() else "unavailable",
        "agent_sha256": hashlib.sha256((agent_root / "main.py").read_bytes() +
                                       (agent_root / "requirements.txt").read_bytes() +
                                       ((agent_root / "engineering.py").read_bytes()
                                        if (agent_root / "engineering.py").is_file() else b"") +
                                       ((agent_root / "batch_splitter.py").read_bytes()
                                        if (agent_root / "batch_splitter.py").is_file() else b"")).hexdigest(),
        "skills_sha256": digest_bundled_tree(agent_root / "skills"),
        "runtime_sha256": digest_bundled_tree(agent_root / "arcbench-agent-runtime"),
        "model": os.environ.get("MODEL", "").strip() or "unavailable",
        "visual_model": os.environ.get("VISUAL_MODEL", "").strip() or "unavailable",
        "base_url_sha256": hashlib.sha256(base_url.encode("utf-8")).hexdigest(),
    }


def _excluded_snapshot_path(relative: Path) -> bool:
    parts = [part.lower() for part in relative.parts]
    if any(part in {".arc", ".git", ".codex", "node_modules", "dist", "build", "coverage",
                    ".cache", ".next", "__pycache__", ".vite"} for part in parts):
        return True
    name = parts[-1]
    if name.endswith(tuple(extension + suffix for extension in (".db", ".sqlite", ".sqlite3")
                           for suffix in ("", "-wal", "-shm", "-journal"))):
        return True
    if "module-observations" in parts and name.endswith((
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov", ".zip", ".har", ".trace")):
        return True
    return (name.startswith(".env") or name in {
        ".npmrc", ".pypirc", "id_rsa", "credentials.json", "secrets.json",
        "auth.json", "tokens.json", "token.json"
    } or name.endswith((".pem", ".key")))


def copy_source_snapshot(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for directory, dirs, files in os.walk(source):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if not _excluded_snapshot_path((parent / name).relative_to(source))
                         and not (parent / name).is_symlink())
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(source)
            if _excluded_snapshot_path(relative) or path.is_symlink():
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_ENGINEERING_MEMORY_STATUSES = {"candidate", "verified", "stale", "rejected"}
_ENGINEERING_MEMORY_VOLATILE_FIELDS = {
    "attempt", "batch", "duration_seconds", "started_at", "ended_at", "timestamp",
    "request_id", "retry_count",
}


def engineering_memory_path(output_dir: Path) -> Path:
    """Return the optional, non-authoritative engineering memory file."""
    return output_dir / ".arc" / "engineering" / "memory.json"


def _memory_error_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _memory_error_value(item) for key, item in sorted(value.items())
                if key not in _ENGINEERING_MEMORY_VOLATILE_FIELDS}
    if isinstance(value, list):
        return [_memory_error_value(item) for item in value]
    return value


def failure_fingerprint(error: Any) -> str:
    """Create a stable digest for an error while ignoring run-specific counters."""
    if isinstance(error, str):
        canonical = " ".join(error.split())
    else:
        canonical = json.dumps(_memory_error_value(error), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_engineering_memory(output_dir: Path) -> dict[str, Any]:
    """Read optional memory; malformed or unavailable memory is an empty hint."""
    try:
        value = json.loads(engineering_memory_path(output_dir).read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("version") != 1 or
                not isinstance(value.get("entries"), list)):
            raise ValueError("invalid engineering memory")
        return value
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": 1, "entries": []}


def remember_engineering_memory(output_dir: Path, source_sha256: str, module_id: str,
                                run_id: str, error: Any, status: str,
                                details: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Best-effort lifecycle record; memory never controls primary delivery."""
    if status not in _ENGINEERING_MEMORY_STATUSES:
        return None
    try:
        memory = load_engineering_memory(output_dir)
        fingerprint = failure_fingerprint(error) if error is not None else None
        now = utc_now()
        entry = next((item for item in memory["entries"] if isinstance(item, dict) and
                      item.get("source_sha256") == source_sha256 and
                      item.get("module_id") == module_id and item.get("run_id") == run_id and
                      item.get("error_fingerprint") == fingerprint), None)
        if entry is None:
            entry = {"source_sha256": source_sha256, "module_id": module_id,
                     "run_id": run_id, "error_fingerprint": fingerprint,
                     "created_at": now, "action": "created"}
            memory["entries"].append(entry)
        else:
            entry["action"] = "updated"
        details = details if isinstance(details, dict) else {}
        fixed_fields = {
            "phase": "", "error_kind": "", "message": "", "source_stage": "",
            "requirement_ids": [], "input_digest": "", "root_cause": "", "repair": "",
            "verification_evidence": [], "applicability": "", "non_applicability": "",
        }
        for key, default in fixed_fields.items():
            entry.setdefault(key, default)
        entry.update({key: details[key] for key in fixed_fields if key in details})
        entry.update(status=status, updated_at=now)
        atomic_json(engineering_memory_path(output_dir), memory)
        return dict(entry)
    except Exception:
        return None


def should_skip_engineering_request(output_dir: Path, source_sha256: str, module_id: str,
                                    run_id: str, error: Any, include_candidate: bool = False) -> bool:
    """Skip only an identical error for the same source, module and run."""
    if include_candidate and model_failure_kind(error) == "transient":
        return False
    fingerprint = failure_fingerprint(error)
    for entry in load_engineering_memory(output_dir).get("entries", []):
        if (isinstance(entry, dict) and entry.get("source_sha256") == source_sha256 and
                entry.get("module_id") == module_id and entry.get("run_id") == run_id and
                entry.get("error_fingerprint") == fingerprint and
                entry.get("status") in ({"candidate", "verified"} if include_candidate else {"verified"})):
            return True
    return False


def engineering_memory_hints(output_dir: Path, source_sha256: str,
                             module_id: str, limit: int = 3) -> list[dict[str, Any]]:
    """Return a small source-bound hint list; stale or rejected entries are ignored."""
    hints = []
    for entry in load_engineering_memory(output_dir).get("entries", []):
        if (not isinstance(entry, dict) or entry.get("source_sha256") != source_sha256 or
                entry.get("module_id") != module_id or entry.get("status") not in {"candidate", "verified"}):
            continue
        hints.append({key: entry.get(key) for key in (
            "status", "error_fingerprint", "phase", "error_kind", "message", "source_stage",
            "requirement_ids", "input_digest", "source_sha256", "root_cause", "repair",
            "verification_evidence", "applicability", "non_applicability")})
        if len(hints) >= max(0, limit):
            break
    return hints


def engineering_memory_report(output_dir: Path) -> dict[str, Any]:
    path = engineering_memory_path(output_dir)
    memory = load_engineering_memory(output_dir)
    readable = not path.exists()
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            readable = isinstance(value, dict) and value.get("version") == 1 and isinstance(value.get("entries"), list)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            readable = False
    statuses: dict[str, int] = {}
    for entry in memory.get("entries", []):
        if isinstance(entry, dict):
            statuses[entry.get("status", "unknown")] = statuses.get(entry.get("status", "unknown"), 0) + 1
    actions = [entry.get("action") for entry in memory.get("entries", [])
               if isinstance(entry, dict) and entry.get("action")]
    try:
        events = (output_dir / ".arc" / "codex-recovery" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if any('"duplicate_model_request": true' in line for line in events):
            actions.append("skipped")
    except OSError:
        pass
    return {"path": path.relative_to(output_dir).as_posix(), "status": "available" if readable else "unavailable",
            "entries": len(memory.get("entries", [])), "statuses": statuses, "actions": actions}


def emit_runtime_event(callback: Any, output_dir: Path, *args: Any) -> None:
    try:
        callback(*args)
    except Exception as exc:
        issue = {"stage": "reporting", "error": error_evidence(exc), "ended_at": utc_now()}
        try:
            path = output_dir / ".arc/codex-recovery/reporting-issues.json"
            issues = json.loads(path.read_text()) if path.is_file() else []
            issues.append(issue)
            atomic_json(path, issues)
            append_event(output_dir, {**issue, "status": "failed"})
        except Exception as reporting_error:
            print(f"[reporting] {redact(exc)}; record failed: {redact(reporting_error)}", file=sys.stderr)


@contextmanager
def _sdk_session(config: Any):
    """Keep a body/login failure when SDK exit also raises."""
    client = None
    entered = False
    body_error: BaseException | None = None
    body_traceback = None
    try:
        client = Codex(config=config)
        value = client.__enter__()
        entered = True
        try:
            yield value
        except BaseException as exc:
            body_error = exc
            body_traceback = sys.exc_info()[2]
            raise
    finally:
        if client is not None and entered:
            try:
                client.__exit__(type(body_error), body_error, body_traceback)
            except Exception as exit_error:
                if body_error is not None:
                    raise RuntimeError(
                        "SDK body/login error: " + redact(body_error) +
                        "; SDK exit error: " + redact(exit_error)
                    ) from body_error
                raise


def save_checkpoint(output_dir: Path, completed_ids: list[str], identity: dict[str, Any],
                    build: dict[str, Any], *, completed_batches: list[str] | None = None,
                    current_batch: dict[str, Any] | None = None) -> None:
    recovery = output_dir / ".arc" / "codex-recovery"
    checkpoints = recovery / "checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    name = f"module-{len(completed_ids):03d}-{uuid.uuid4().hex[:12]}"
    temporary = checkpoints / (".tmp-" + name)
    final = checkpoints / name
    try:
        copy_source_snapshot(output_dir, temporary / "source")
        manifest = {"completed_ids": completed_ids, "identity": identity, "build": build,
                    "created_at": utc_now(), "completed_batches": completed_batches or [],
                    "current_batch": current_batch,
                    "batch_plan_sha256": (current_batch or {}).get("batch_plan_sha256")}
        manifest["engineering_files"] = {}
        for filename in ("contract.json", "state.json", "preflight.json", "current-batch-plan.json"):
            engineering_file = output_dir / ".arc/engineering" / filename
            if engineering_file.is_file():
                target = temporary / "engineering" / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(engineering_file, target)
                manifest["engineering_files"][filename] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest["source_sha256"] = digest_tree(temporary / "source")
        atomic_json(temporary / "checkpoint.json", manifest)
        os.replace(temporary, final)
        atomic_json(recovery / "latest.json", {"checkpoint": name, "completed_ids": completed_ids,
                                               "identity": identity,
                                               "completed_batches": completed_batches or [],
                                               "current_batch": current_batch,
                                               "batch_plan_sha256": (current_batch or {}).get("batch_plan_sha256")})
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def save_working_checkpoint(output_dir: Path, module_id: str, attempt: int,
                            identity: dict[str, Any], stage: str, *,
                            completed_ids: list[str] | None = None,
                            current_batch: dict[str, Any] | None = None,
                            source_state: dict[str, Any] | None = None,
                            prompt_context: str | None = None,
                            failure: dict[str, Any] | None = None) -> str:
    """Keep a non-authoritative batch snapshot without replacing delivery authority.

    Working checkpoints are deliberately separate from authoritative module checkpoints.  They
    are the restore point for a single model turn and therefore carry the prefix and compact
    turn context needed to retry or skip that batch safely.
    """
    root = output_dir / ".arc" / "codex-recovery" / "turn-checkpoints"
    root.mkdir(parents=True, exist_ok=True)
    name = f"{module_id}-attempt-{attempt}-{uuid.uuid4().hex[:12]}"
    temporary = root / (".tmp-" + name)
    final = root / name
    try:
        # Keep the same product scope as repair restores, including bundled .codex/skills;
        # remove the helper manifest because the working manifest is the authority.
        copy_repair_snapshot(output_dir, temporary / "source")
        (temporary / "source" / "snapshot-manifest.json").unlink(missing_ok=True)
        source_status = {"product_source_sha256": product_source_digest(output_dir),
                         "snapshot_sha256": digest_tree(temporary / "source"),
                         **(source_state or {})}
        engineering_dir = output_dir / ".arc" / "engineering"
        saved_engineering: dict[str, str] = {}
        for filename in ("preflight.json", "state.json"):
            source = engineering_dir / filename
            if source.is_file():
                target = temporary / "engineering" / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                saved_engineering[filename] = hashlib.sha256(target.read_bytes()).hexdigest()
        prompt_payload = redact(prompt_context or "")[-12000:]
        manifest = {"module_id": module_id, "attempt": attempt, "stage": stage,
                    "identity": identity, "created_at": utc_now(),
                    "completed_ids": list(completed_ids or []),
                    "current_batch": current_batch or {"batch_id": module_id},
                    "source_status": source_status,
                    "source_sha256": source_status["snapshot_sha256"],
                    "engineering_files": saved_engineering,
                    "prompt_context": prompt_payload,
                    "failure": failure}
        atomic_json(temporary / "checkpoint.json", manifest)
        atomic_json(temporary / "state.json", {
            "completed_ids": list(completed_ids or []),
            "module_id": module_id,
            "current_batch": current_batch or {"batch_id": module_id},
            "source_status": source_status,
            "preflight": (source_state or {}).get("preflight"),
            "state": (source_state or {}).get("state"),
            "prompt_context": prompt_payload,
            "failure": failure,
        })
        if prompt_payload:
            atomic_json(temporary / "prompt-context.json", {"prompt": prompt_payload})
        if failure is not None:
            atomic_json(temporary / "failure.json", failure)
        os.replace(temporary, final)
        return final.relative_to(output_dir).as_posix()
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _recovery_dir(resume_from: Path) -> Path:
    if (resume_from / "latest.json").is_file():
        return resume_from
    return resume_from / ".arc" / "codex-recovery"


def _recovery_lineage(root: Path) -> bool:
    recovery = _recovery_dir(root)
    return ((root / ".arc/engineering/preflight.json").exists() or
            (recovery.is_dir() and any(recovery.iterdir())))


def restore_checkpoint(resume_from: Path, output_dir: Path, identity: dict[str, Any],
                       agent_root: Path, module_ids: list[str] | None = None) -> list[str]:
    recovery = _recovery_dir(resume_from)
    record = _checkpoint_record(resume_from, identity, module_ids)
    if record is None:
        raise ValueError("No complete checkpoint is available; earlier modules cannot be skipped")
    latest, manifest, checkpoint = record["latest"], record["manifest"], record["checkpoint"]
    source = checkpoint / "source"
    if output_dir == resume_from or output_dir in resume_from.parents or resume_from in output_dir.parents:
        raise ValueError("Resume output must be a separate directory")
    # The runner may have staged the same starter app. Preserve its metadata, but reject an
    # unrelated pre-existing app rather than mixing it with a recovered one.
    for name in ("frontend", "backend"):
        existing = output_dir / name
        if existing.exists():
            starter = agent_root / "template" / name
            if not starter.is_dir() or digest_tree(existing) != digest_tree(starter):
                raise ValueError(f"Resume output contains a different {name} application")
            shutil.rmtree(existing)
    for path in source.iterdir():
        destination = output_dir / path.name
        if destination.exists():
            if (path.is_file() and destination.is_file() and path.read_bytes() == destination.read_bytes()) or (
                path.is_dir() and destination.is_dir() and digest_tree(path) == digest_tree(destination)
            ):
                continue
            raise ValueError(f"Resume output already contains different {path.name}")
        if path.is_dir():
            shutil.copytree(path, destination)
        else:
            shutil.copy2(path, destination)
    for filename in manifest.get("engineering_files", {}):
        destination = output_dir / ".arc/engineering" / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkpoint / "engineering" / filename, destination)
    restored_recovery = output_dir / ".arc" / "codex-recovery"
    restored_recovery.mkdir(parents=True, exist_ok=True)
    shutil.copy2(recovery / "latest.json", restored_recovery / "latest.json")
    shutil.copytree(checkpoint, restored_recovery / "checkpoints" / latest["checkpoint"])
    return list(manifest["completed_ids"])


def dependency_fingerprint(project_dir: Path) -> str:
    documents = {name: (project_dir / name).read_text(encoding="utf-8")
                 if (project_dir / name).is_file() else None
                 for name in ("package.json", "package-lock.json")}
    documents["install_mode"] = "include-dev"
    return hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()


def ensure_project_dependencies(project_dir: Path, output_dir: Path) -> dict[str, Any]:
    if not (project_dir / "package.json").is_file():
        raise RuntimeError(f"{project_dir.name}/package.json is unavailable")
    state_file = output_dir / ".arc" / "codex-recovery" / "installed-dependencies.json"
    installed = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    project = str(project_dir.resolve())
    if (project_dir / "node_modules").is_dir() and installed.get(project) == dependency_fingerprint(project_dir):
        return {"status": "passed", "dependency_install": "existing", "commands": []}
    npm = "npm.cmd" if os.name == "nt" else "npm"
    commands: list[dict[str, Any]] = []

    def execute(method: str) -> subprocess.CompletedProcess[str]:
        command = [npm, method, "--include=dev", "--no-audit", "--no-fund"]
        command_text = " ".join(command)
        append_event(output_dir, {"stage": "dependencies", "project": project, "status": "started",
                                  "command": command_text, "started_at": utc_now()})
        try:
            result = subprocess.run(command, cwd=project_dir, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", shell=False, timeout=600)
        except subprocess.TimeoutExpired as exc:
            append_event(output_dir, {"stage": "dependencies", "project": project,
                                      "status": "timeout", "command": command_text,
                                      "timeout_seconds": 600, "ended_at": utc_now()})
            raise RuntimeError(f"{command_text} timed out after 600 seconds: " +
                               redact(str(exc.stdout or "") + "\n" + str(exc.stderr or ""))) from exc
        commands.append({"command": command_text, "exit_code": result.returncode})
        append_event(output_dir, {"stage": "dependencies", "project": project,
                                  "status": "command", "command": command_text,
                                  "exit_code": result.returncode, "ended_at": utc_now()})
        return result

    method = "ci" if (project_dir / "package-lock.json").is_file() else "install"
    result = execute(method)
    if result.returncode and method == "ci" and (
            "can only install packages when your package.json and package-lock.json" in
            (result.stdout + result.stderr)):
        method = "install"
        result = execute(method)
    if result.returncode:
        raise RuntimeError(f"npm {method} failed (exit {result.returncode}): " +
                           redact((result.stdout + "\n" + result.stderr)[-8000:]))
    installed[project] = dependency_fingerprint(project_dir)
    atomic_json(state_file, installed)
    return {"status": "passed", "dependency_install": f"npm {method}", "commands": commands}


def build_frontend(output_dir: Path) -> dict[str, Any]:
    frontend = output_dir / "frontend"
    if not (frontend / "package.json").is_file():
        raise RuntimeError("frontend/package.json is unavailable")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    dependencies = ensure_project_dependencies(frontend, output_dir)
    commands = list(dependencies["commands"])

    def execute(command: list[str]) -> subprocess.CompletedProcess[str]:
        append_event(output_dir, {"stage": "build", "status": "started", "command": " ".join(command),
                                  "started_at": utc_now()})
        try:
            result = subprocess.run(command, cwd=frontend, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", shell=False, timeout=180)
        except subprocess.TimeoutExpired as exc:
            append_event(output_dir, {"stage": "build", "status": "timeout", "command": " ".join(command),
                                      "timeout_seconds": 180, "ended_at": utc_now()})
            raise RuntimeError("npm run build timed out after 180 seconds: " +
                               redact(str(exc.stdout or "") + "\n" + str(exc.stderr or ""))) from exc
        commands.append({"command": " ".join(command), "exit_code": result.returncode})
        append_event(output_dir, {"stage": "build", "status": "command", "command": " ".join(command),
                                  "exit_code": result.returncode, "ended_at": utc_now()})
        return result

    result = execute([npm, "run", "build"])
    if result.returncode:
        raise RuntimeError(f"npm run build failed (exit {result.returncode}): " +
                           redact((result.stdout + "\n" + result.stderr)[-8000:]))
    if not (frontend / "dist" / "index.html").is_file():
        raise RuntimeError("frontend build did not produce dist/index.html")
    return {"command": "npm run build", "dependency_install": dependencies["dependency_install"],
            "commands": commands, "status": "passed", "checked_at": utc_now()}


def verify_backend_startup(output_dir: Path, timeout_seconds: float = 15,
                           port: int | None = None) -> dict[str, Any]:
    backend = output_dir / "backend"
    if not (backend / "package.json").is_file():
        raise RuntimeError("backend/package.json is unavailable")
    npm = "npm.cmd" if os.name == "nt" else "npm"
    ensure_project_dependencies(backend, output_dir)
    if port is None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
    env = os.environ.copy()
    env["PORT"] = str(port)
    with tempfile.TemporaryDirectory(dir=backend, prefix="backend-startup-") as temporary, \
            tempfile.TemporaryFile(mode="w+b") as log:
        database = str(Path(temporary) / "startup.sqlite")
        env.update({"ARC_DB_FILE": database, "DATABASE_FILE": database})
        process = subprocess.Popen([npm, "run", "start"], cwd=backend, env=env,
                                   stdout=log, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=(os.name != "nt"),
                                   creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
        failure = "backend startup timed out"
        try:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    failure = f"backend exited with code {process.returncode}"
                    break
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                        if response.status == 200:
                            log.seek(0, os.SEEK_END)
                            log.seek(max(0, log.tell() - 8000))
                            startup_log = log.read(8000).decode("utf-8", errors="replace")
                            log.seek(0, os.SEEK_END)
                            match = re.search(
                                r"(?i)\bSQLITE_(?:ERROR|CONSTRAINT(?:_[A-Z_]+)?)\b",
                                startup_log,
                            )
                            if match:
                                raise RuntimeError("backend startup reported SQLite error: " +
                                                   redact(startup_log[max(0, match.start() - 200):match.end() + 1200]))
                            return {"status": "passed", "command": "npm run start", "checked_at": utc_now()}
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(0.1)
        finally:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, check=False)
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                if os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=2)
        log.seek(0, os.SEEK_END)
        log.seek(max(0, log.tell() - 4000))
        raise RuntimeError(f"{failure}: " + redact(log.read(4000).decode("utf-8", errors="replace")))


def copy_skills_to_output(skills_dir: Path, output_dir: Path) -> Path:
    """Make the bundled ARC-Bench skills available inside Codex's workspace."""
    if not skills_dir.is_dir():
        raise FileNotFoundError(f"Skills directory not found: {skills_dir}")
    destination = output_dir / ".codex" / "skills"
    shutil.copytree(skills_dir, destination, dirs_exist_ok=True)
    return destination


def load_root_context(requirements_dir: Path) -> dict[str, Any]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")
    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")
    return {key: payload[key] for key in ("id", "name", "description", "data") if key in payload}


def load_root_modules(requirements_dir: Path) -> list[RequirementModule]:
    requirements_path = requirements_dir / "requirements.yaml"
    if not requirements_path.is_file():
        raise FileNotFoundError(f"requirements.yaml not found: {requirements_path}")

    payload = yaml.safe_load(requirements_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or str(payload.get("id") or "").strip() != "ROOT":
        raise ValueError("requirements.yaml must contain a ROOT mapping")

    children = [item for item in payload.get("children", []) if isinstance(item, dict)]
    if not children:
        raise ValueError("ROOT must contain at least one child module")

    children, _ = engineering.module_order(children)
    modules: list[RequirementModule] = []
    for index, subtree in enumerate(children, start=1):
        node_id = str(subtree.get("id") or subtree.get("req_id") or "").strip()
        if not node_id:
            raise ValueError(f"ROOT child {index} has no id")
        modules.append(
            RequirementModule(
                index=index,
                total=len(children),
                node_id=node_id,
                name=str(subtree.get("name") or node_id).strip(),
                subtree=subtree,
            )
        )
    return modules


def plan_current_module_batches(module: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Plan only the subtree for the ROOT module currently being entered."""
    return batch_splitter.plan_current_module_batches(module, **kwargs)


def materialize_current_batches(module: RequirementModule, plan: dict[str, Any]) -> list[RequirementModule]:
    """Turn one current-module plan into batch modules without changing ROOT order."""
    result: list[RequirementModule] = []
    children = [child for child in module.subtree.get("children", []) if isinstance(child, dict)]
    by_id = {str(child.get("id") or child.get("req_id") or ""): child for child in children}
    batches = plan.get("batches") or []
    for batch in batches:
        batch_id = str(batch.get("batch_id") or module.node_id)
        selected = [by_id[child_id] for child_id in batch.get("child_ids", []) if child_id in by_id]
        if {str(child.get("id") or child.get("req_id") or "") for child in selected} != set(batch.get("child_ids") or []):
            raise ValueError("Current-module batch plan does not select complete YAML subtrees")
        subtree = dict(module.subtree)
        subtree["id"] = batch_id
        subtree["children"] = selected
        result.append(replace(
            module, node_id=batch_id, name=f"{module.name} ({batch_id})", subtree=subtree,
            root_module_id=module.node_id, batch_id=batch_id,
            batch_plan_sha256=plan.get("batch_plan_sha256"), batch_context=batch,
        ))
    return result or [module]


def write_current_module_plan(output_dir: Path, root_module_id: str,
                              plan: dict[str, Any]) -> None:
    """Persist only the module currently being entered; later modules are untouched."""
    atomic_json(output_dir / ".arc/engineering/current-batch-plan.json", plan)
    atomic_json(output_dir / "module-observations" / root_module_id / "batch-plan.json", plan)


def archive_current_module_plan(output_dir: Path, root_module_id: str,
                                plan: dict[str, Any], identity: dict[str, Any]) -> str:
    archive = output_dir / ".arc/codex-recovery" / "batch-plans"
    archive.mkdir(parents=True, exist_ok=True)
    path = archive / f"{root_module_id}-{plan['batch_plan_sha256']}.json"
    archived = {**plan, "_archive_identity": identity}
    atomic_json(path, archived)
    # Keep one stable pointer per ROOT.  Hash-named evidence files remain immutable,
    # while acceptance must never choose a plan by lexicographic hash order.
    atomic_json(archive / f"{root_module_id}.json", archived)
    (output_dir / ".arc/engineering/current-batch-plan.json").unlink(missing_ok=True)
    return path.relative_to(output_dir).as_posix()


def load_completed_batch_modules(output_dir: Path,
                                 root_modules: list[RequirementModule],
                                 identity: dict[str, Any] | None = None) -> list[RequirementModule]:
    """Expand only archived/current plans so final acceptance replays frozen batch scripts."""
    result: list[RequirementModule] = []
    archive = output_dir / ".arc/codex-recovery" / "batch-plans"
    current = output_dir / ".arc/engineering" / "current-batch-plan.json"
    for root in root_modules:
        # The stable ROOT pointer is overwritten atomically when that module is
        # archived.  Hash-named files are evidence only and are never selected
        # by filename ordering.
        path = archive / f"{root.node_id}.json"
        if not path.is_file():
            path = current
        plan: dict[str, Any] | None = None
        if path.is_file():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                unsigned = {key: value for key, value in parsed.items()
                            if key not in {"batch_plan_sha256", "_archive_identity"}}
                expected_hash = hashlib.sha256(json.dumps(
                    unsigned, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                hash_name = archive / f"{root.node_id}-{parsed.get('batch_plan_sha256')}.json"
                archived = json.loads(hash_name.read_text(encoding="utf-8")) if hash_name.is_file() else None
                if (parsed.get("root_module_id") == root.node_id and
                        parsed.get("batch_plan_sha256") == expected_hash and
                        isinstance(archived, dict) and
                        {key: value for key, value in archived.items()
                         if key not in {"batch_plan_sha256", "_archive_identity"}} == unsigned and
                        (identity is None or parsed.get("_archive_identity") == identity) and
                        (identity is None or archived.get("_archive_identity") == identity)):
                    plan = {key: value for key, value in parsed.items() if key != "_archive_identity"}
            except (OSError, ValueError):
                plan = None
        if plan and plan.get("decision") == "split":
            result.extend(materialize_current_batches(root, plan))
        else:
            result.append(root)
    return result


def _atomic_requirement_index(subtree: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], ancestors: list[str]) -> None:
        node_id = str(node.get('id') or node.get('req_id') or '').strip()
        if node.get('type') == 'ATOMIC':
            result.append({
                'id': node_id,
                'name': str(node.get('name') or node_id),
                'ancestor_ids': ancestors,
            })
        for child in node.get('children', []):
            if isinstance(child, dict):
                visit(child, [*ancestors, node_id])

    visit(subtree, [])
    return result


def observation_contract(module: RequirementModule) -> dict[str, Any]:
    """Host-owned one-to-one mapping from source atoms to independent test titles."""
    requirements = _atomic_requirement_index(module.subtree)
    identifiers = [item["id"] for item in requirements]
    if not identifiers or any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Observation source must contain unique nonempty atomic IDs")
    return {"requirements": requirements,
            "chains": [{"title": identifier, "requirement_ids": [identifier]} for identifier in identifiers]}


def requirement_inventory(module: RequirementModule, contract: dict[str, Any],
                          implementation: Any, observation: dict[str, Any] | None,
                          output_dir: Path) -> dict[str, dict[str, Any]]:
    reports = implementation.get("requirements", []) if isinstance(implementation, dict) else []
    reports = reports if isinstance(reports, list) else []
    by_id = {item.get("id"): item for item in reports if isinstance(item, dict) and
             item.get("id") in {entry["id"] for entry in contract["requirements"]}}
    chains = contract["chains"]
    observed = {item.get("title"): item for item in (observation or {}).get("tests", [])}
    local_script_error = (observation or {}).get("configuration_error_scope") == "test"
    result: dict[str, dict[str, Any]] = {}
    for requirement in contract["requirements"]:
        identifier = requirement["id"]
        claim = by_id.get(identifier)
        files = claim.get("files") if isinstance(claim, dict) else None
        unfinished = claim.get("unfinished") if isinstance(claim, dict) else None
        if claim is None or not isinstance(files, list) or not isinstance(unfinished, str):
            model_status = "not_reported"
        elif not files or any(not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts or
                 not (output_dir / path).is_file() for path in files):
            model_status = "missing_files"
        else:
            model_status = "incomplete" if unfinished.strip() else "reported"
        selected = [chain for chain in chains if identifier in chain["requirement_ids"]]
        passed = (bool(selected) and observation is not None and
                  (observation.get("status") in {"passed", "failed"} or local_script_error) and
                  (not observation.get("configuration_error") or local_script_error) and
                  (not observation.get("process_timed_out") or local_script_error) and all(
            (test := observed.get(chain["title"])) is not None and
            test.get("status") == "expected" and test.get("executed") and test.get("result_status") == "passed" and
            not test.get("configuration_error")
            for chain in selected))
        result[identifier] = {"model_status": model_status, "files": files if isinstance(files, list) else [],
                              "unfinished": unfinished if isinstance(unfinished, str) else "not reported",
                              "observation_status": "observed_passed" if passed else "unverified",
                              "chain_titles": [chain["title"] for chain in selected]}
    return result


def _codex_config(CodexConfig: Any) -> Any:
    env = os.environ.copy()
    overrides: list[str] = ['web_search="disabled"']
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        overrides.append(f"openai_base_url={json.dumps(base_url)}")
    return CodexConfig(env=env, config_overrides=tuple(overrides))


def _developer_instructions(_skills_dir: Path) -> str:
    bundled_guide = Path(__file__).resolve().parent / "template" / "TEMPLATE_GUIDE.md"
    return textwrap.dedent(
        f"""
        You implement an ARC-Bench application in the current working directory.
        The directory already contains a starter application. Current requirements govern existing code;
        preserve unrelated correct work and correct behavior that contradicts those requirements.
        Before your first code change, read TEMPLATE_GUIDE.md in the project root when present;
        otherwise read {bundled_guide}.
        Reuse the documented utilities when they fit the current requirements.

        Each request supplies ROOT-wide constraints and one direct child subtree. Implement that
        subtree, including all of its descendants, in the current project. The ROOT constraints
        always apply; read dependency clauses in requirements.yaml when needed. Make practical code
        changes and leave the application runnable. If the actual source already meets a clause,
        leave it unchanged; check and report it without forcing a code diff.

        Before changing a shared value or object shape, inspect its definition and its actual
        callers. Reuse one representation and update all affected consumers; do not guess
        property names or introduce a second near-identical shape.

        For editable controls, keep the committed value, the draft value, and the draft's
        original owner distinct. A focus/selection change without user edits must not write
        data. Commit a dirty draft to its captured owner once, including Enter/blur ordering;
        never resolve the write target from a later selection. Clear dirty state before a
        second event can submit again. Preserve the last successful state on failed writes.

        Use current requirements for control role, accessible name and steps. A visually
        similar component is not an equivalent contract. Verify dependent display, exports,
        history and structural updates against the same raw/derived data representation.

        Keep the current module's existing shared interfaces unless a requirement forces a
        change. If it does, update the real callers now. Do not finish with an unconnected
        helper or a consumer still reading the old field names.

        In this same coding turn, run bounded dependency preparation, build, startup and focused
        functional checks when needed to verify your application changes. Reuse existing project
        commands and tools, including the Playwright CLI for browser checks when available; do not
        assume a browser MCP tool or add a test runner. Prepare missing dependencies once, without
        repeated installation attempts. For a live check, use the project's documented formal startup entry,
        a free port and a temporary isolated database; batch checks against that owned startup and
        stop only processes you started.
        Set an actual execution deadline on each command when the tool supports it; a yield/poll
        interval is not a deadline. If the tool lacks one on Linux, use the existing shell timeout -k
        option (for example, timeout -k 5s 180s <command>) with a finite bound appropriate to
        the operation. If no bounded execution is available, leave the check unverified.

        Assert source-required observable state and data after actions and reloads when persistence
        applies. HTTP 200, file existence or no exception alone is not functional proof.
        A nonzero exit or failed assertion is feedback within this turn: inspect the actual output,
        make one concentrated application fix, and recheck affected behavior. If it still fails
        after the recheck, record the confirmed residual failure and affected atomic IDs, then end
        this turn normally without another repair loop. A missing tool,
        broken check script or timeout leaves that check unverified; do not infer a business defect
        or make blanket application changes from it. Preserve source and report the concrete gap.

        Your work is application code and the requested small implementation declaration or repair
        note. The host independently repeats dependency sync, build and startup, then runs frozen
        browser observations, runtime reporting and source checkpoints after this turn. Do not
        create or edit frozen scripts/results, acceptance reports, runtime states or checkpoints;
        do not invoke runtime, traceability or checkpoint skills. Your declaration is not evidence.
        """
    ).strip()


def _module_prompt(
    module: RequirementModule,
    requirements_dir: Path,
    completed_ids: list[str],
    skills_dir: Path,
    root_context: dict[str, Any],
    pending_issues: list[dict[str, Any]],
    memory_hints: list[dict[str, Any]] | None = None,
) -> str:
    completed = ", ".join(completed_ids) if completed_ids else "none"
    batch_context = module.batch_context or {}
    return textwrap.dedent(
        f"""
        Implement ROOT module {module.index}/{module.total}: {module.node_id} - {module.name}

        Requirement source directory: {requirements_dir}
        Previously completed ROOT modules: {completed}
        Skills directory: {skills_dir}

        Current-module batch scope (program-owned; do not split or merge it):
        ```json
        {json.dumps(batch_context, ensure_ascii=False, indent=2)}
        ```
        Implement only this batch's selected subtree and delta text. Reuse the listed upstream
        dependency closure and shared baseline entities as read-only interfaces; preserve the
        ROOT constraints above and do not invent missing dependencies.

        ROOT-wide constraints:
        ```json
        {json.dumps(root_context, ensure_ascii=False, indent=2)}
        ```

        Prior issues and provisional observations (not final business verdicts):
        ```json
        {json.dumps(pending_issues, ensure_ascii=False, indent=2)}
        ```

        Small source-bound engineering memory hints (advisory only; current requirements and
        trusted checkpoints always win):
        ```json
        {json.dumps(memory_hints or [], ensure_ascii=False, indent=2)}
        ```

        Implement this complete subtree in the current target directory:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Atomic requirement index (navigation aid; the original text above is authoritative):
        {json.dumps(_atomic_requirement_index(module.subtree), ensure_ascii=False, indent=2)}

        Read each atomic ID's original description, scenarios, ancestors, ROOT constraints,
        and referenced source dependencies. The original text above is authoritative; preserve
        exact control names, roles, scope, permissions, observable results, and persistence rules.
        Early observations can be blocked by modules not implemented yet. Check prerequisite
        ownership before treating an early failure as an application defect; final acceptance
        will independently rerun the selected frozen scripts after all modules are built.
        If source clauses conflict, state the assumption and unresolved impact in the relevant
        implementation.json unfinished field instead of silently choosing; do not create a separate
        report. Leave clauses already satisfied by actual source unchanged.

        If IMPLEMENTATION_NOTES.md exists in the target root, read it first and then read the real
        source it points at; code and original text win over the note. You may keep this note at
        the target root at no more than 80 lines, updated in this same turn, covering only: the
        definition file and fields of shared data structures; the owner of edit commit and
        persistence; the real routes and callers; and requirement dependencies not yet wired.
        It is a reading aid that cannot block anything, and it must not claim that tests pass.
        If it is missing or unusable, continue from the actual source instead of repairing it.

        In the same coding turn, make the current module's main user flows and high-risk shared
        behaviors runnable. Establish their real preconditions through the documented formal startup entry,
        then check explicit expected results with the existing tools; include create, edit, save, and reload
        when persistence applies. Assert observable state and data against the source
        after the actions and reload. Inspect actual UI and code before choosing browser interactions.
        Treat a nonzero exit as feedback, fix one concrete cause and recheck the affected flow once.
        If it still fails, put the confirmed residual failure and affected IDs in the unfinished
        fields, then return normally without a further repair loop.
        If a prerequisite or check tool is unavailable, name the missing dependency and affected
        atomic IDs as unverified, separate from confirmed failures; never invent records or defer
        a blanket rewrite to later modules.

        For each atomic ID write module-observations/{module.node_id}/implementation.json
        with {{"requirements": [{{"id": "source ID", "files": ["relative implementation path"],
        "unfinished": "remaining work or unverified check, or empty string"}}]}}. Include every atomic ID.
        Add an interfaces list for shared objects actually implemented: each item has name,
        definition (real relative file), fields (actual representation), consumers (real relative
        files). Include existing consumers inspected during this turn. Missing/unwired consumers
        belong in unfinished; do not invent files. The host binds declarations to actual file hashes.
        This is a model declaration, not evidence of functional passage.

        End with a brief changed-file and check summary. The host repeats build and startup, then
        performs independent frozen observation, reporting and checkpoints after your turn returns.
        """
    ).strip()


def module_retry_prompt(module: RequirementModule, requirements_dir: Path,
                        skills_dir: Path, failure: dict[str, Any]) -> str:
    """Retry a module with source authority intact but without repeated history."""
    return textwrap.dedent(
        f"""
        Implement ROOT module {module.index}/{module.total}: {module.node_id} - {module.name} (bounded retry)

        Original requirement source (authoritative): {requirements_dir / 'requirements.yaml'}
        Skills directory: {skills_dir}

        Current-module batch scope (program-owned; do not split or merge it):
        ```json
        {json.dumps(module.batch_context or {}, ensure_ascii=False, indent=2)}
        ```

        Current module subtree:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Atomic requirement index:
        {json.dumps(_atomic_requirement_index(module.subtree), ensure_ascii=False, indent=2)}

        The previous turn stopped before a trusted module checkpoint. Read the original ROOT
        constraints, actual source files and shared interfaces again; preserve every valid file
        already in the workspace. Repair only this module and the concrete failure below. Keep
        the same requirement names, roles, permissions, persistence rules and Stage lineage.
        Do one bounded implementation pass, run only focused checks, record remaining work in
        implementation.json, and return without another repair loop.

        Previous failure:
        ```json
        {json.dumps(failure, ensure_ascii=False, indent=2)[:6000]}
        ```
        """
    ).strip()


def sdk_turn_capabilities(thread: Any, handle: Any | None = None) -> dict[str, bool]:
    """Probe the optional bounded-turn API without assuming an SDK version."""
    return {
        "thread_turn": callable(getattr(thread, "turn", None)),
        "turn_interrupt": callable(getattr(handle, "interrupt", None)) if handle is not None else False,
    }


def model_turn_limits(stage: str, retry: bool = False) -> tuple[float, float]:
    """Return (soft, hard) seconds without making retries another long run."""
    if retry:
        return 300.0, MODEL_TURN_RETRY_HARD_TIMEOUT_SECONDS
    if stage in {"observation_planning", "observation_script_correction", "observation_repair"}:
        return 900.0, 1800.0
    return MODEL_TURN_SOFT_TIMEOUT_SECONDS, MODEL_TURN_HARD_TIMEOUT_SECONDS


def is_remote_compaction_error(error: Any) -> bool:
    evidence = error if isinstance(error, dict) else error_evidence(error)
    message = str(evidence.get("message", ""))
    return bool(re.search(
        r"remote compaction v2 expected exactly one compaction output item, got \d+ from \d+ output items",
        message, re.I))


def run_model_turn(thread: Any, prompt: str,
                   timeout_seconds: float = MODEL_TURN_TIMEOUT_SECONDS,
                   soft_timeout_seconds: float | None = None,
                   on_soft_timeout: Any | None = None,
                   on_turn_started: Any | None = None) -> Any:
    """Run a bounded SDK turn when the installed client exposes that API.

    Older clients retain the original thread.run path; the caller's outer process
    remains responsible for any hard kill in that compatibility mode.
    """
    turn_factory = getattr(thread, "turn", None)
    if not callable(turn_factory):
        if on_turn_started is not None:
            on_turn_started({"thread_turn": False, "turn_interrupt": False, "turn_id": None})
        return thread.run(prompt)
    handle = turn_factory(prompt)
    if on_turn_started is not None:
        on_turn_started({**sdk_turn_capabilities(thread, handle),
                         "turn_id": getattr(handle, "id", None)})
    interrupt = getattr(handle, "interrupt", None)
    if not callable(interrupt):
        return handle.run()
    state = {"timed_out": False, "interrupt_error": None, "soft_timeout": False}

    def warn_turn() -> None:
        state["soft_timeout"] = True
        if on_soft_timeout is not None:
            on_soft_timeout()

    def stop_turn() -> None:
        state["timed_out"] = True
        try:
            interrupt()
        except Exception as exc:  # pragma: no cover - provider-specific failure
            state["interrupt_error"] = error_evidence(exc)

    soft_timer = None
    if soft_timeout_seconds is not None and 0 < soft_timeout_seconds < timeout_seconds:
        soft_timer = threading.Timer(soft_timeout_seconds, warn_turn)
        soft_timer.daemon = True
        soft_timer.start()
    timer = threading.Timer(timeout_seconds, stop_turn)
    timer.daemon = True
    timer.start()
    try:
        result = handle.run()
    finally:
        timer.cancel()
        if soft_timer is not None:
            soft_timer.cancel()
    if state["timed_out"]:
        error = ModelTurnTimeout(timeout_seconds)
        if state["interrupt_error"]:
            error.interrupt_error = state["interrupt_error"]
        raise error
    return result


def _completed_turn(thread: Any, prompt: str, output_dir: Path | None = None,
                    stage: str = "model", timeout_seconds: float | None = None,
                    soft_timeout_seconds: float | None = None,
                    context_reset: bool = False) -> None:
    started = time.monotonic()
    if timeout_seconds is None or soft_timeout_seconds is None:
        default_soft, default_hard = model_turn_limits(stage)
        soft_timeout_seconds = default_soft if soft_timeout_seconds is None else soft_timeout_seconds
        timeout_seconds = default_hard if timeout_seconds is None else timeout_seconds
    if output_dir:
        append_event(output_dir, {"stage": stage, "status": "started", "started_at": utc_now()})
    completed = False
    try:
        def soft_timeout_event() -> None:
            if output_dir:
                latest = _recovery_dir(output_dir) / "latest.json"
                append_event(output_dir, {"stage": stage, "status": "soft_timeout",
                                          "timeout_seconds": soft_timeout_seconds,
                                          "checkpoint_available": latest.is_file(),
                                          "ended_at": utc_now()})

        def turn_started_event(capabilities: dict[str, Any]) -> None:
            if output_dir:
                append_event(output_dir, {"stage": stage, "status": "turn_started",
                                          "turn_mode": "turn_handle" if capabilities.get("thread_turn") else "thread_run",
                                          "turn_id": capabilities.get("turn_id"),
                                          "interrupt_available": capabilities.get("turn_interrupt", False),
                                          "context_reset": context_reset,
                                          "started_at": utc_now()})

        result = run_model_turn(thread, prompt, timeout_seconds=timeout_seconds,
                                soft_timeout_seconds=soft_timeout_seconds,
                                on_soft_timeout=soft_timeout_event,
                                on_turn_started=turn_started_event)
        if getattr(result, "error", None) is not None:
            raise ModuleResultError(result.error)
        status = getattr(result, "status", "completed")
        if getattr(status, "value", status) != "completed":
            raise RuntimeError(f"Codex turn did not complete: {redact(status)}")
        completed = True
    except ModelTurnTimeout as exc:
        if output_dir:
            append_event(output_dir, {"stage": stage, "status": "timeout",
                                      "timeout_seconds": exc.timeout_seconds,
                                      "error": error_evidence(exc), "ended_at": utc_now()})
        raise
    finally:
        if output_dir:
            emit_runtime_event(append_event, output_dir, output_dir,
                               {"stage": stage, "status": "passed" if completed else "failed",
                                "ended_at": utc_now(), "duration_seconds": round(time.monotonic()-started, 3)})


def _planning_workspace(requirements_dir: Path, workspace: Path,
                        module: RequirementModule | None = None) -> None:
    """Copy only the current observation scope into the planner workspace."""
    target = workspace / "requirements"
    target.mkdir()
    source_payload = yaml.safe_load((requirements_dir / "requirements.yaml").read_text(encoding="utf-8"))
    if not isinstance(source_payload, dict):
        raise ValueError("requirements.yaml must contain a mapping")
    if module is None:
        scoped_payload = source_payload
        context: dict[str, Any] = {}
    else:
        scoped_payload = dict(source_payload)
        scoped_payload["children"] = [json.loads(json.dumps(module.subtree))]
        context = {
            "root_module_id": module.root_module_id or module.node_id,
            "batch_id": module.batch_id or module.node_id,
            "batch_plan_sha256": module.batch_plan_sha256,
            "batch": module.batch_context or {},
        }
    (target / "requirements.yaml").write_text(
        yaml.safe_dump(scoped_payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if module is not None:
        atomic_json(target / "batch-context.json", context)

    allowed_suffixes = {".md", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"}
    scope_text = json.dumps(scoped_payload, ensure_ascii=False) + json.dumps(context, ensure_ascii=False)
    for source in requirements_dir.rglob("*"):
        if (not source.is_file() or source.is_symlink() or
                source.suffix.lower() not in allowed_suffixes):
            continue
        relative = source.relative_to(requirements_dir)
        # Reference files are copied only when their path or basename occurs in
        # the scoped source.  This avoids handing the planner unrelated images.
        if module is not None and source.name not in scope_text and relative.as_posix() not in scope_text:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def plan_module_observation(codex: Any, module: RequirementModule, requirements_dir: Path,
                            output_dir: Path, root_context: dict[str, Any], model: str | None) -> tuple[dict[str, Any], bytes]:
    observation_dir = output_dir / "module-observations" / module.node_id
    observation_dir.mkdir(parents=True, exist_ok=True)
    previous = [observation_dir / name for name in
                ("contract.json", "checks.spec.js", "frozen-v1.spec.js", "planning-failure.json")
                if (observation_dir / name).is_file()]
    if previous:
        history = observation_dir / ("planning-prior-" + uuid.uuid4().hex[:12])
        history.mkdir()
        for path in previous:
            shutil.copy2(path, history / path.name)
    with tempfile.TemporaryDirectory(prefix="arc-observation-plan-") as temporary:
        workspace = Path(temporary)
        _planning_workspace(requirements_dir, workspace, module)
        thread = codex.thread_start(cwd=str(workspace), sandbox=Sandbox.full_access,
                                    approval_mode=ApprovalMode.deny_all, model=model,
                                    developer_instructions=(
                                        "Plan source-based browser observations only. This directory contains copied "
                                        "requirements and reference images. Do not inspect application source, target "
                                        "output, prior tests, or use an application path. This is context isolation, "
                                        "not an operating-system security sandbox. Read only the current requirement "
                                        "source, necessary dependency clauses, and reference images. Produce only "
                                        "checks.spec.js, then return immediately. Do not explore "
                                        "runtime, environment, or tools; install dependencies; run build, lint, or "
                                        "tests; start a server; or perform reporting or checkpoint work."))
        prompt = textwrap.dedent(f"""
            Plan final acceptance with one independent @playwright/test test per source atomic ID.
            Read only the current batch requirements at requirements/requirements.yaml and
            requirements/batch-context.json. Do not seek or reconstruct sibling modules from
            another source. Reference images are limited to the current batch workspace.
            ROOT context: {json.dumps(root_context, ensure_ascii=False)}
            Current-module batch scope (program-owned): {json.dumps(module.batch_context or {}, ensure_ascii=False)}
            Current subtree: {json.dumps(module.subtree, ensure_ascii=False)}
            Exact test titles: {json.dumps([item["id"] for item in _atomic_requirement_index(module.subtree)], ensure_ascii=False)}
            Write only checks.spec.js. Each test title is exactly its source atomic ID; explanations
            belong in test.step. The host owns requirement metadata. Include prerequisites and
            affected cross-module behavior described in
            prose, even when the explicit dependency list omits them. The complete application
            will exist when the host executes these checks; do not inspect implementation now.
            Do not combine independent
            atomic checks into one test or use serial mode that skips later tests after a failure.
            Include only source-required names, roles, scopes, actions and results. Start each test
            at '/' and reach controls through the visible UI. For an exact accessible name use
            page.getByRole('button', {{ name: sourceName, exact: true }}); `exact` belongs in the
            options object, never inside the locator string.
            Only assume startup-provisioned data when the source explicitly requires it. Example
            inputs and arbitrary values are not pre-existing seed data. Establish other prerequisite
            values through the visible UI in this test, then confirm them before the target action.
            Each test owns its preparation: use a record created by this test where the source permits,
            or restore shared records changed by this test so later checks do not inherit its changes.
            Do not turn details the source leaves
            open - such as which cell gains focus after Enter - into required conditions. After a
            non-modifying action, check that stored data is unchanged. For formulas and other derived
            values, distinguish "never computed" from "computed correctly and then cleared by a later
            action", and keep the order of operations in the failure message.
            No skips, .first(), forced clicks, broad text substitutes, private APIs, database writes,
            or storage injection. Do not invent intermediate states and do not hardcode control names
            the source never states. If a check cannot be executed correctly, report it as unverified
            rather than stretching the run. Do not inspect or mention target application source or old
            tests. Write only checks.spec.js, then return immediately. The host discovers and runs the
            frozen script after your turn; do not execute it or report progress.
        """).strip()
        script_path = workspace / "checks.spec.js"
        turn_completed = False
        try:
            _completed_turn(thread, prompt, output_dir, stage="observation_planning")
            turn_completed = True
        finally:
            try:
                for source in (script_path,):
                    if source.is_file():
                        data = source.read_bytes()
                        for key, secret in os.environ.items():
                            if len(secret) >= 8 and any(marker in key.upper() for marker in
                                                        ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH",
                                                         "CREDENTIAL")):
                                data = data.replace(secret.encode("utf-8"), b"[REDACTED]")
                        (observation_dir / source.name).write_bytes(data)
            except Exception as export_error:
                warning = {"module_id": module.node_id, "stage": "observation_script_export",
                           "status": "failed", "error": error_evidence(export_error)}
                emit_runtime_event(atomic_json, output_dir,
                                   observation_dir / "planning-export-warning.json", warning)
                append_event(output_dir, warning)
                print(f"[planning export] {redact(export_error)}", file=sys.stderr)
                if turn_completed:
                    raise  # A successful turn still requires the new script; never freeze an old export.
        if not script_path.is_file():
            raise ValueError("Invalid observation script: planner omitted checks.spec.js")
        script = (observation_dir / "checks.spec.js").read_bytes()
        if not script.strip():
            raise ValueError("Invalid observation script: empty script")
        contract = observation_contract(module)
        atomic_json(observation_dir / "contract.json", contract)
        (observation_dir / "frozen-v1.spec.js").write_bytes(script)
        return contract, script


def correct_observation_script(codex: Any, module: RequirementModule, requirements_dir: Path,
                               output_dir: Path, receipt: dict[str, Any], observed: dict[str, Any],
                               model: str | None, modules: list[RequirementModule]) -> bool:
    """Spend at most one existing repair turn on a definite test-file execution error."""
    error = str(observed.get("configuration_error") or "")
    unfinished_step = any(
        item.get('executed') and item.get('result_status') == 'failed' and
        item.get('error_origin') != 'application' and re.search(
            r'(?:^|[;\n]\s*)Error: ARC_OBSERVATION_UNFINISHED_STEP:',
            str(item.get('configuration_error') or '')) for item in observed.get('tests', []))
    definite_script_error = bool(
        ("Playwright discovery failed:" in error and "SyntaxError:" in error and "checks.spec.js" in error) or
        re.search(r"ReferenceError: (?:test|expect) is not defined[\s\S]{0,500}checks\.spec\.js", error))
    if (observed.get("status") != "error" or receipt.get("script_corrections_used", 0) or
            not (definite_script_error or unfinished_step) or
            (not unfinished_step and re.search(
                r"page\.evaluate|https?://|document\.|locator|strict mode|expect\(|TimeoutError", error, re.I))):
        return False
    entry = receipt.get("observations", {}).get(module.node_id, {})
    if entry.get("status") != "prepared" or entry.get("version", 1) != 1:
        return False
    frozen = engineering.observation_from_authority(output_dir, module.node_id, receipt["identity"],
                                                    receipt["source_sha256"], receipt)
    if frozen is None:
        return False
    folder = output_dir / "module-observations" / module.node_id
    original = frozen[1]
    error_record = {"module_id": module.node_id, "original_script_sha256": hashlib.sha256(original).hexdigest(),
                    "error": error, "evidence_path": observed.get("evidence_path")}
    atomic_json(folder / "correction-v1-error.json", error_record)
    receipt["script_corrections_used"] = 1
    receipt['script_correction_pending'] = True
    modules[:] = [replace(item, planning_authority=json.loads(json.dumps(receipt))) for item in modules]
    try:
        # Pending authority is a mandatory control write, not auxiliary reporting.
        atomic_json(output_dir / ".arc/engineering/preflight.json", receipt)
        checkpoint = _checkpoint_record(output_dir, receipt['identity'],
                                        [item.node_id for item in modules])
        if checkpoint is None:
            raise ValueError('Observation correction requires a trusted non-empty checkpoint; no reconstruction allowed')
        saved_ids = checkpoint['latest']['completed_ids']
        save_checkpoint(output_dir, saved_ids, receipt["identity"],
                        {"status": "not_rechecked", "purpose": "script_correction_budget"})
        authority = engineering.checkpoint_authority(output_dir, receipt['identity'])
        if (authority != receipt or
                engineering.source_index(requirements_dir)['source_sha256'] != receipt['source_sha256'] or
                engineering.observation_from_authority(output_dir, module.node_id, receipt['identity'],
                    receipt['source_sha256'], authority) != frozen):
            raise ValueError('Correction pending authority is not durably bound to the trusted source and script')
    except Exception as exc:
        detail = {'stage': 'script_correction_integrity',
                  'message': 'Correction pending authority could not be verified; all later model requests stopped',
                  'error': error_evidence(exc)}
        exc.host_integrity_stop = detail
        receipt['host_integrity_stop'] = detail
        modules[:] = [replace(item, planning_authority=json.loads(json.dumps(receipt))) for item in modules]
        append_event(output_dir, {**detail, 'status': 'failed', 'ended_at': utc_now()})
        raise
    latest = engineering._read(output_dir / ".arc/codex-recovery/latest.json")
    print(f"[planning] Script correction {module.node_id} started; normal repair budget 1/8 used", flush=True)
    baseline = application_digest(output_dir)
    protected_paths = [output_dir / '.arc/engineering/preflight.json',
                       output_dir / '.arc/engineering/contract.json', folder / 'correction-v1-error.json']
    for identifier, planned in receipt['observations'].items():
        if planned.get('status') == 'prepared':
            protected_paths.extend(output_dir / 'module-observations' / identifier / name
                for name in ('contract.json', 'checks.spec.js', 'frozen-v1.spec.js', 'frozen-v2.spec.js'))
    protected_bytes = {path: path.read_bytes() if path.is_file() else None for path in protected_paths}
    original_authority = json.loads(json.dumps(receipt))
    snapshot = None
    first_error = None
    restoration_verified = True
    try:
        snapshot = Path(tempfile.mkdtemp(prefix='arc-script-correction-source-'))
        copy_repair_snapshot(output_dir, snapshot)
        with tempfile.TemporaryDirectory(prefix="arc-observation-correction-") as temporary:
            workspace = Path(temporary)
            _planning_workspace(requirements_dir, workspace, module)
            (workspace / "original.spec.js").write_bytes(original)
            atomic_json(workspace / "error.json", error_record)
            try:
                thread = codex.thread_start(cwd=str(workspace), sandbox=Sandbox.full_access,
                                        approval_mode=ApprovalMode.deny_all, model=model,
                                        developer_instructions=(
                                            "Correct only a definite test-script execution error. This workspace "
                                            "contains source requirements, original test script and error evidence. "
                                            "Do not inspect application source or target output. Never remove a test, "
                                            "assertion, exact role, name, or expected value; never change product code."))
                _completed_turn(thread, "Read only the current batch requirements/requirements.yaml, "
                            "requirements/batch-context.json, original.spec.js and error.json. "
                            "Fix only the evidenced script execution defect. Preserve exact source IDs, "
                            "test expectations and actions. Preserve every original byte in order. Only "
                            "prefix a missing test/expect import from @playwright/test, or append closing "
                            "delimiters for an evidenced Unexpected end of input. An evidenced "
                            "ARC_OBSERVATION_UNFINISHED_STEP permits inserting only 'await ' before existing "
                            "test.step calls at the start of their lines. Do not change any existing "
                            "line, value, action, assertion or control flow. If this cannot fix the defect, "
                            "return the original unchanged. Write corrected.spec.js and return.",
                            output_dir, stage="observation_script_correction")
                candidate = (workspace / "corrected.spec.js").read_bytes()
            except Exception as turn_error:
                first_error = turn_error
                raise
        if (application_digest(output_dir) != baseline or any(
                (path.read_bytes() if path.is_file() else None) != data
                for path, data in protected_bytes.items())):
            raise ValueError('Script correction changed files outside correction workspace')
        if not candidate.strip() or candidate == original:
            raise ValueError("Script correction supplied no changed script")
        imports = [f"const {{ {names} }} = require('@playwright/test');\n".encode()
                   for names in ('test', 'expect', 'test, expect')]
        imports += [f"import {{ {names} }} from '@playwright/test';\n".encode()
                    for names in ('test', 'expect', 'test, expect')]
        missing_import = bool(re.search(r'ReferenceError: (?:test|expect) is not defined', error))
        closing_only = ('Unexpected end of input' in error and candidate.startswith(original) and
                        re.fullmatch(rb'[\s)}\];]+', candidate[len(original):]) is not None)
        without_import = candidate
        if missing_import:
            without_import = next((candidate[len(prefix):] for prefix in imports if candidate.startswith(prefix)),
                                  candidate)
        old_lines, new_lines = original.splitlines(keepends=True), without_import.splitlines(keepends=True)
        added_await = unfinished_step and len(old_lines) == len(new_lines) and all(
            old == new or (old.lstrip(b' \t').startswith(b'test.step(') and
                           new == old[:len(old) - len(old.lstrip(b' \t'))] + b'await ' + old.lstrip(b' \t'))
            for old, new in zip(old_lines, new_lines))
        if not (missing_import and any(candidate == prefix + original for prefix in imports)) and not (
                closing_only or added_await):
            raise ValueError('Script correction must preserve source assertions and actions byte-for-byte')
        (folder / "frozen-v2.spec.js").write_bytes(candidate)
        (folder / "checks.spec.js").write_bytes(candidate)
        entry.update(version=2, original_script_sha256=hashlib.sha256(original).hexdigest(),
                     script_sha256=hashlib.sha256(candidate).hexdigest())
        receipt["observations"][module.node_id] = entry
        terminal_receipt = json.loads(json.dumps(receipt))
        terminal_receipt['script_correction_pending'] = False
        atomic_json(output_dir / ".arc/engineering/preflight.json", terminal_receipt)
        save_checkpoint(output_dir, latest["completed_ids"], receipt["identity"],
                        {"status": "not_rechecked", "purpose": "selected_script_version"})
        receipt.clear()
        receipt.update(terminal_receipt)
        modules[:] = [replace(item, planning_authority=json.loads(json.dumps(receipt))) for item in modules]
        print(f"[planning] Script correction {module.node_id}: frozen v2; normal repair budget 1/8 used", flush=True)
        return True
    except Exception as exc:
        failure = first_error or exc
        restore_errors = []
        try:
            if snapshot and (snapshot / 'snapshot-manifest.json').is_file():
                restore_repair_snapshot(snapshot, output_dir)
        except Exception as restore_error:
            restore_errors.append(error_evidence(restore_error))
        for path, data in protected_bytes.items():
            try:
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
            except Exception as restore_error:
                restore_errors.append(error_evidence(restore_error))
        receipt.clear()
        receipt.update(original_authority)
        original_error = failure.error if isinstance(failure, ModuleResultError) else failure
        if model_failure_kind(original_error) == 'blocked':
            receipt['model_blocked_reason'] = {'stage': 'observation_script_correction',
                                               'error': error_evidence(original_error)}
        try:
            restoration_verified = application_digest(output_dir) == baseline and all(
                (path.read_bytes() if path.is_file() else None) == data
                for path, data in protected_bytes.items())
        except Exception as restore_error:
            restoration_verified = False
            restore_errors.append(error_evidence(restore_error))
        if not restoration_verified:
            detail = {'stage': 'script_correction_integrity',
                      'message': 'Correction restoration could not be verified; all later model requests stopped',
                      'snapshot_path': str(snapshot), 'restoration_errors': restore_errors}
            failure.host_integrity_stop = detail
            receipt['host_integrity_stop'] = detail
        terminal_receipt = json.loads(json.dumps(receipt))
        if restoration_verified and not (receipt.get('host_integrity_stop') or receipt.get('model_blocked_reason')):
            terminal_receipt['script_correction_pending'] = False
        try:
            # Publish terminal control metadata from trustworthy pre-call source, never the failed draft.
            with tempfile.TemporaryDirectory(prefix='arc-correction-stop-') as temporary:
                trusted_output = Path(temporary) / 'output'
                trusted_output.mkdir()
                saved_ids = restore_checkpoint(output_dir, trusted_output, receipt['identity'],
                                               Path(__file__).resolve().parent)
                atomic_json(trusted_output / '.arc/engineering/preflight.json', terminal_receipt)
                save_checkpoint(trusted_output, saved_ids, receipt['identity'],
                                {'status': 'not_rechecked', 'purpose': 'correction_terminal'})
                staged_recovery = trusted_output / '.arc/codex-recovery'
                stopped_latest = engineering._read(staged_recovery / 'latest.json')
                recovery = output_dir / '.arc/codex-recovery'
                shutil.copytree(staged_recovery / 'checkpoints' / stopped_latest['checkpoint'],
                                recovery / 'checkpoints' / stopped_latest['checkpoint'])
                atomic_json(recovery / 'latest.json', stopped_latest)
            receipt.clear()
            receipt.update(terminal_receipt)
        except Exception as stop_error:
            restoration_verified = False
            restore_errors.append(error_evidence(stop_error))
            detail = {'stage': 'script_correction_integrity',
                      'message': 'Correction terminal state could not be checkpointed; all later model requests stopped',
                      'snapshot_path': str(snapshot), 'restoration_errors': restore_errors}
            failure.host_integrity_stop = detail
            receipt['host_integrity_stop'] = detail
        with suppress(Exception):
            emit_runtime_event(atomic_json, output_dir, output_dir / '.arc/engineering/preflight.json', receipt)
        modules[:] = [replace(item, planning_authority=json.loads(json.dumps(receipt))) for item in modules]
        with suppress(Exception):
            emit_runtime_event(atomic_json, output_dir, folder / "correction-failure.json",
                               {**error_record, "correction_error": error_evidence(failure),
                                'restoration_errors': restore_errors,
                                'host_integrity_stop': receipt.get('host_integrity_stop')})
            print(f"[planning] Script correction {module.node_id}: unverified; {redact(failure)}", flush=True)
        raise failure
    finally:
        if snapshot and restoration_verified:
            with suppress(Exception):
                shutil.rmtree(snapshot, ignore_errors=True)


def _engineering_receipt(requirements_dir: Path, output_dir: Path,
                         modules: list[RequirementModule], identity: dict[str, Any],
                         allow_fresh: bool = False,
                         external_predecessors: set[str] | None = None) -> dict[str, Any]:
    source = engineering.source_index(requirements_dir)
    external_predecessors = external_predecessors or set()
    engineering.validate_source_dependencies(source, external_predecessors)
    for module in modules:
        observation_contract(module)
    receipt_path = output_dir / ".arc/engineering/preflight.json"
    trusted = next((module.planning_authority for module in modules if module.planning_authority), None)
    if not allow_fresh and _recovery_lineage(output_dir) and _checkpoint_record(
            output_dir, identity, [module.node_id for module in modules]) is None:
        raise ValueError("Engineering recovery integrity unavailable; checkpoint pointer or manifest is untrusted")
    trusted = trusted or engineering.checkpoint_authority(output_dir, identity)
    if (trusted and trusted.get("version") == engineering.VERSION and trusted.get("identity") == identity and
            trusted.get("source_sha256") == source["source_sha256"] and trusted.get("finished")):
        return json.loads(json.dumps(trusted))
    if trusted or (not allow_fresh and _recovery_lineage(output_dir)):
        raise ValueError("Engineering recovery integrity unavailable; refusing to reset observation or correction budgets")
    _, conflicts = engineering.module_order(source["payload"].get("children", []))
    return {"version": engineering.VERSION, "identity": identity,
            "source_sha256": source["source_sha256"], "dependency_issues": conflicts,
            "module_order": [module.node_id for module in modules],
            "external_predecessors": sorted(external_predecessors),
            "contract": {"status": "source_only"}, "observations": {}, "finished": True}


def prepare_engineering(requirements_dir: Path, output_dir: Path, modules: list[RequirementModule],
                         identity: dict[str, Any], codex: Any = None,
                         external_predecessors: set[str] | None = None) -> dict[str, Any]:
    """Validate original source without a model turn; freeze checks per module before coding."""
    receipt = _engineering_receipt(requirements_dir, output_dir, modules, identity,
                                   external_predecessors=external_predecessors)
    emit_runtime_event(atomic_json, output_dir, output_dir / ".arc/engineering/preflight.json", receipt)
    modules[:] = [replace(module, planning_authority=json.loads(json.dumps(receipt))) for module in modules]
    return receipt


def prepare_module_observation(codex: Any, module: RequirementModule, requirements_dir: Path,
                               output_dir: Path, root_context: dict[str, Any], model: str | None,
                               receipt: dict[str, Any], modules: list[RequirementModule]) -> dict[str, Any]:
    """Freeze one source-only script before that module's implementation turn."""
    folder = output_dir / "module-observations" / module.node_id
    entry = receipt["observations"].get(module.node_id)
    if entry is not None:
        if entry.get("status") == "prepared" and engineering.observation_from_authority(
                output_dir, module.node_id, receipt["identity"], receipt["source_sha256"], receipt):
            return entry
        return {"status": "unverified", "reason": {"stage": "planning", "message":
                "Existing frozen observation unavailable or stale; no late model replan"}}
    planning_started = time.monotonic()
    print(f"[planning] Observation {module.index}/{module.total}: {module.node_id} started", flush=True)
    try:
        contract, script = plan_module_observation(codex, module, requirements_dir, output_dir,
                                                   root_context, model)
        entry = {"status": "prepared", "script_sha256": hashlib.sha256(script).hexdigest(),
                 "contract_sha256": hashlib.sha256((folder / "contract.json").read_bytes()).hexdigest()}
    except Exception as exc:
        error = exc.error if isinstance(exc, ModuleResultError) else exc
        reason = {"module_id": module.node_id, "stage": "early_observation_planning",
                  "error": error_evidence(error)}
        entry = {"status": "unverified", "reason": reason}
        emit_runtime_event(atomic_json, output_dir, folder / "planning-failure.json", reason)
    entry["duration_seconds"] = round(time.monotonic() - planning_started, 3)
    receipt["observations"][module.node_id] = entry
    emit_runtime_event(atomic_json, output_dir, output_dir / ".arc/engineering/preflight.json", receipt)
    modules[:] = [replace(item, planning_authority=json.loads(json.dumps(receipt))) for item in modules]
    print(f"[planning] Observation {module.node_id}: {entry['status']}" +
          ("; " + redact(entry.get("reason", {}).get("error", {}).get("message", ""))
           if entry["status"] != "prepared" else ""), flush=True)
    return entry


def engineering_context(requirements_dir: Path, output_dir: Path,
                        module: RequirementModule | None = None) -> str:
    # Original requirements remain in the implementation prompt; metadata is auxiliary.
    try:
        handoff = engineering.handoff(
            output_dir, requirements_dir,
            module_ids=[module.node_id] if module is not None else None)
    except Exception as exc:
        append_event(output_dir, {"stage": "engineering_handoff", "status": "unverified",
                                  "error": error_evidence(exc), "ended_at": utc_now()})
        handoff = {"status": "unverified", "instruction":
                   "Read original requirements and actual source files directly; handoff unavailable."}
    return ("\n\nHost file-bound implementation handoff (declarations are not business proof):\n" +
            json.dumps(handoff, ensure_ascii=False))


def parse_observation_report(report: dict[str, Any], exit_code: int,
                             process_timed_out: bool = False, timeout_seconds: float = 95) -> dict[str, Any]:
    tests: list[dict[str, Any]] = []

    def visit(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            if not isinstance(spec, dict):
                continue
            for test in spec.get("tests", []):
                if not isinstance(test, dict):
                    continue
                results = test.get("results") or []
                last = results[-1] if results and isinstance(results[-1], dict) else {}
                result_status = last.get("status")
                message = "; ".join(redact(item.get("message", "")) for item in last.get("errors", [])
                                    if isinstance(item, dict))
                errors = [item for item in last.get('errors', []) if isinstance(item, dict)]
                application_origin = bool(errors) and all(
                    re.match(r'^https?://', str((item.get('location') or {}).get('file', ''))) and
                    re.match(r'^[^\n]*\n\s+at document\.[^\n]*\(' +
                             re.escape(str(item['location']['file'])), item.get('message', '')) and
                    'page.evaluate' not in item.get('message', '') for item in errors)
                tests.append({"title": spec.get("title", ""), "status": test.get("status"),
                              "executed": any(isinstance(item, dict) and item.get("status") not in
                                              {None, "skipped"} for item in results),
                              "result_status": result_status,
                              "duration_ms": sum(item.get("duration", 0) for item in results
                                                 if isinstance(item, dict)),
                              "error": message, "attachments": last.get("attachments", []),
                              **({'error_origin': 'application'} if application_origin else {})})
        for child in suite.get("suites", []):
            if isinstance(child, dict):
                visit(child)

    if isinstance(report, dict):
        for suite in report.get("suites", []):
            if isinstance(suite, dict):
                visit(suite)
    valid = bool(tests) and not report.get("errors") and all(
        item["executed"] and item["result_status"] not in {None, "skipped", "interrupted"}
        for item in tests)
    statuses = [item["status"] for item in tests]
    script_errors = []
    for item in tests:
        if item.get('error_origin') != 'application' and (re.match(
                    r"^(?:(?:ReferenceError|TypeError|SyntaxError):|"
                    r"(?:Error: )?(?:browserType\.launch:|Cannot find module))", item["error"]) or re.search(
                r"(?:^|[;\n]\s*)Error: ARC_OBSERVATION_UNFINISHED_STEP:", item["error"])):
            item["configuration_error"] = item["error"]
            script_errors.append(item["error"])
    if process_timed_out or script_errors:
        status = "error"
    elif valid and exit_code == 0 and all(item["status"] == "expected" and item["executed"] and
                                           item["result_status"] == "passed"
                                         for item in tests):
        status = "passed"
    elif valid and any(status == "unexpected" for status in statuses):
        status = "failed"
    else:
        status = "error"
    return {"status": status, "tests": tests, "process_timed_out": process_timed_out,
            **({"configuration_error": "; ".join(script_errors),
                "configuration_error_scope": "test" if valid and not process_timed_out else "global"}
               if script_errors else {}),
            **({"error": f"Observation process exceeded {timeout_seconds:g} seconds"} if process_timed_out else {})}


def _stop_service(process: subprocess.Popen[Any]) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=2)


def _redact_json(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json(item) for key, item in value.items()}
    return value


def validate_discovered_chains(report: dict[str, Any], expected_titles: list[str]) -> list[str]:
    titles = [item["title"] for item in parse_observation_report(report, 0)["tests"]]
    if (report.get("errors") or not expected_titles or len(titles) != len(expected_titles) or
            len(set(titles)) != len(titles) or set(titles) != set(expected_titles)):
        raise ValueError(f"Observation chain configuration error: discovered {titles}, expected {expected_titles}")
    return titles


def copy_observation_attachments(report: dict[str, Any], test_results: Path,
                                evidence: Path, output_dir: Path) -> None:
    paths = {}
    screenshots = evidence / "screenshots"
    for index, source in enumerate(sorted(test_results.rglob("*.png")), 1):
        screenshots.mkdir(exist_ok=True)
        destination = screenshots / f"{index:02d}-{source.name}"
        shutil.copy2(source, destination)
        paths[str(source.resolve())] = destination.relative_to(output_dir).as_posix()
    def visit(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                for attempt in test.get("results", []):
                    for attachment in attempt.get("attachments", []):
                        original = attachment.get("path")
                        if original:
                            source = Path(original)
                            if not source.is_absolute():
                                source = test_results.parent / source
                            if str(source.resolve()) in paths:
                                attachment["path"] = paths[str(source.resolve())]
        for child in suite.get("suites", []):
            visit(child)
    for suite in report.get("suites", []):
        visit(suite)


def run_module_observation(output_dir: Path, module: RequirementModule, phase: str) -> dict[str, Any]:
    titles = [item["id"] for item in _atomic_requirement_index(module.subtree)]
    if not titles:
        return _run_atomic_observation(output_dir, module, phase)
    if phase not in {"early", "before", "after", "final"}:
        raise ValueError("Observation phase must be early, before, after or final")
    folder = output_dir / "module-observations" / module.node_id
    evidence = folder / phase
    if evidence.exists() and any(evidence.iterdir()):
        evidence = folder / (phase + "-" + uuid.uuid4().hex[:12])
    evidence.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runs = []
    report: dict[str, Any] = {"suites": [], "errors": []}
    stdout, stderr = [], []
    for title in titles:
        run = _run_atomic_observation(output_dir, module, phase, title)
        runs.append({"atomic_id": title, **run})
        child = output_dir / run["evidence_path"]
        raw = json.loads((child / "result.json").read_text(encoding="utf-8"))
        report["suites"].extend(raw.get("suites", []))
        report["errors"].extend(raw.get("errors", []))
        stdout.append(title + "\n" + (child / "stdout.txt").read_text(encoding="utf-8"))
        stderr.append(title + "\n" + (child / "stderr.txt").read_text(encoding="utf-8"))
    result = {"status": "error" if any(run["status"] == "error" for run in runs) else (
              "failed" if any(run["status"] == "failed" for run in runs) else "passed"),
              "tests": [test for run in runs for test in run.get("tests", [])],
              "duration_seconds": round(time.monotonic() - started, 3),
              "evidence_path": evidence.relative_to(output_dir).as_posix(), "atomic_results": runs,
              "exit_code": 0 if all(run.get("exit_code") == 0 for run in runs) else 1,
              "process_timed_out": any(run.get("process_timed_out") for run in runs),
              "timeout_seconds": sum(run.get("timeout_seconds", 0) for run in runs),
               "script_version": f"v{module.frozen_version}", "discovered_titles": list(dict.fromkeys(
                  title for run in runs for title in run.get("discovered_titles", []))),
              "delivered_script_changed": any(run.get("delivered_script_changed") for run in runs),
              "frozen_file_changed": any(run.get("frozen_file_changed") for run in runs)}
    errors = [run for run in runs if run["status"] == "error"]
    if errors:
        result["configuration_error"] = "; ".join(run.get("configuration_error") or run.get("error") or
            "Atomic observation did not execute correctly: " + run["atomic_id"] for run in errors)
        result["configuration_error_scope"] = "test"
        for run in errors:
            for test in run.get("tests", []):
                test["configuration_error"] = run.get("configuration_error") or run.get("error") or "Invalid atomic observation"
    (evidence / "stdout.txt").write_text("\n".join(stdout), encoding="utf-8")
    (evidence / "stderr.txt").write_text("\n".join(stderr), encoding="utf-8")
    atomic_json(evidence / "result.json", report)
    return result


_STEP_GUARD = b"""const { test } = require('@playwright/test');
const originalStep = test.step;
const pending = new Set();
test.step = Object.assign(function (...args) {
  const tracked = originalStep.apply(test, args).then(
    value => { pending.delete(tracked); return value; },
    error => { pending.delete(tracked); throw error; }
  );
  pending.add(tracked);
  return tracked;
}, originalStep);
test.beforeEach(() => pending.clear());
test.afterEach(() => {
  if (pending.size) {
    throw new Error(`ARC_OBSERVATION_UNFINISHED_STEP: ${pending.size} test.step promise(s) pending at test end`);
  }
});
"""


def _run_atomic_observation(output_dir: Path, module: RequirementModule, phase: str,
                             atomic_id: str | None = None) -> dict[str, Any]:
    if phase not in {"early", "before", "after", "final"}:
        raise ValueError("Observation phase must be early, before, after or final")
    observation_dir = output_dir / "module-observations" / module.node_id
    evidence = observation_dir / (phase + "-atomic-" + uuid.uuid4().hex[:12] if atomic_id else phase)
    if evidence.exists() and any(evidence.iterdir()):
        evidence = observation_dir / (phase + "-" + uuid.uuid4().hex[:12])
    evidence.mkdir(parents=True, exist_ok=True)
    relative = evidence.relative_to(output_dir).as_posix()
    script = observation_dir / "checks.spec.js"
    script_bytes = module.frozen_script if module.frozen_script is not None else (
        script.read_bytes() if script.is_file() else None)
    started = time.monotonic()
    result: dict[str, Any] = {"status": "error", "tests": [], "duration_seconds": 0,
                              "evidence_path": relative}
    stdout = ""
    stderr = ""
    report: dict[str, Any] = {"suites": [], "errors": [{"message": "Observation did not run"}]}
    exit_code: int | None = None
    process_timed_out = False
    source_titles = [item["id"] for item in _atomic_requirement_index(module.subtree)]
    expected_titles = [atomic_id] if atomic_id else source_titles
    runner_timeout_ms = 30000 * len(expected_titles) + 15000
    process_timeout_seconds = runner_timeout_ms / 1000 + 5
    result["timeout_seconds"] = process_timeout_seconds
    if script_bytes is not None:
        (evidence / "checks.spec.js").write_bytes(script_bytes)
        result["delivered_script_changed"] = not script.is_file() or script.read_bytes() != script_bytes
        result["script_version"] = f"v{module.frozen_version}"
        frozen_file = observation_dir / f"frozen-v{module.frozen_version}.spec.js"
        result["frozen_file_changed"] = not frozen_file.is_file() or frozen_file.read_bytes() != script_bytes
        backend = output_dir / "backend"
        npm = "npm.cmd" if os.name == "nt" else "npm"
        try:
            ensure_project_dependencies(backend, output_dir)
            with tempfile.TemporaryDirectory(dir=backend, prefix="module-observation-") as temporary:
                work = Path(temporary)
                runner_script = script_bytes + b"\n;require('./arc-step-guard.cjs');\n"
                (work / "checks.spec.js").write_bytes(runner_script)
                (work / "arc-step-guard.cjs").write_bytes(_STEP_GUARD)
                (evidence / "runner-checks.spec.js").write_bytes(runner_script)
                (evidence / "arc-step-guard.cjs").write_bytes(_STEP_GUARD)
                database = work / "observation.sqlite"
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                config = work / "playwright.config.cjs"
                config.write_text("module.exports = {testDir: __dirname, testMatch: 'checks.spec.js', "
                                  f"workers: 1, retries: 0, timeout: 30000, globalTimeout: {runner_timeout_ms}, "
                                  "reporter: [['json', {outputFile: 'result.json'}]], "
                                  "outputDir: 'test-results', use: {baseURL: 'http://127.0.0.1:" +
                                  str(port) + "', screenshot: 'only-on-failure', trace: 'off', video: 'off'}};\n",
                                  encoding="utf-8")
                env = os.environ.copy()
                env.update({"PORT": str(port), "ARC_DB_FILE": str(database), "DATABASE_FILE": str(database)})
                binary = backend / "node_modules" / ".bin" / ("playwright.cmd" if os.name == "nt" else "playwright")
                discovery = subprocess.run([str(binary), "test", "--config", str(config), "--list"],
                                           cwd=work, env=env, capture_output=True, text=True,
                                           encoding="utf-8", errors="replace", timeout=30)
                (evidence / "discovery.txt").write_text(redact(discovery.stdout + discovery.stderr), encoding="utf-8")
                if discovery.returncode:
                    raise ValueError("Observation chain configuration error: Playwright discovery failed: " +
                                     redact((discovery.stdout + discovery.stderr)[-2000:]))
                discovery_report = json.loads((work / "result.json").read_text(encoding="utf-8"))
                result["discovered_titles"] = validate_discovered_chains(discovery_report, source_titles)
                atomic_json(evidence / "discovery.json", _redact_json(discovery_report))
                (work / "result.json").unlink()
                browser_stopped: bool | None = None
                with (work / "service.log").open("w+b") as service_log:
                    process = subprocess.Popen([npm, "run", "start"], cwd=backend, env=env,
                                               stdout=service_log, stderr=subprocess.STDOUT,
                                               start_new_session=(os.name != "nt"),
                                               creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
                    try:
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            if process.poll() is not None:
                                raise RuntimeError(f"observation backend exited with code {process.returncode}")
                            try:
                                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.5) as response:
                                    if response.status == 200:
                                        break
                            except (urllib.error.URLError, TimeoutError):
                                pass
                            time.sleep(0.1)
                        else:
                            raise RuntimeError("observation backend startup timed out")
                        command = [str(binary), "test", "--config", str(config)]
                        if atomic_id:
                            command.extend(["--grep", r"(?:^|\s)" + re.escape(atomic_id) + "$"])
                        execution = subprocess.Popen(command, cwd=work, env=env, stdout=subprocess.PIPE,
                                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                                     errors="replace", start_new_session=(os.name != "nt"),
                                                     creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0))
                        try:
                            stdout, stderr = execution.communicate(timeout=process_timeout_seconds)
                            exit_code = execution.returncode
                        except subprocess.TimeoutExpired:
                            process_timed_out = True
                            _stop_service(execution)
                            stdout, stderr = execution.communicate()
                            stderr += f"\nObservation process exceeded {process_timeout_seconds:g} seconds"
                        browser_stopped = execution.poll() is not None
                        report_file = work / "result.json"
                        if report_file.is_file():
                            report = json.loads(report_file.read_text(encoding="utf-8"))
                            report = _redact_json(report)
                            copy_observation_attachments(report, work / "test-results", evidence, output_dir)
                    finally:
                        _stop_service(process)
                        service_stopped = process.poll() is not None
                        service_log.seek(0)
                        service_output = redact(service_log.read().decode("utf-8", "replace")[-4000:])
                        normal_database = os.environ.get("ARC_DB_FILE") or os.environ.get("DATABASE_FILE") or str(backend / "database.db")
                        stderr += (f"\nObservation database: {database}; normal database: {normal_database}; "
                                   f"service PID: {process.pid}; service stopped: {service_stopped}; "
                                   f"browser process stopped: {browser_stopped}")
                        if service_output:
                            stderr += "\nBackend: " + service_output
        except Exception as exc:
            stderr += "\n" + redact(exc)
            if "chain configuration error" in str(exc):
                result["configuration_error"] = redact(exc)
    else:
        stderr = f"Missing observation script: {script.relative_to(output_dir).as_posix()}"
        result["configuration_error"] = stderr
        result["configuration_error_scope"] = "global"
    if isinstance(report, dict):
        result.update(parse_observation_report(report, exit_code if exit_code is not None else -1,
                                               process_timed_out=process_timed_out, timeout_seconds=process_timeout_seconds))
    try:
        validate_discovered_chains(report, expected_titles)
    except ValueError as exc:
        result["status"] = "error"
        result.setdefault("configuration_error", redact(exc))
        result["configuration_error_scope"] = "global"
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["exit_code"] = exit_code
    (evidence / "stdout.txt").write_text(redact(stdout), encoding="utf-8")
    (evidence / "stderr.txt").write_text(redact(stderr), encoding="utf-8")
    atomic_json(evidence / "result.json", report if isinstance(report, dict) else {"error": "Invalid report"})
    return result


def error_evidence(error: Any) -> dict[str, str]:
    raw_message = error.get("message", error) if isinstance(error, dict) else getattr(error, "message", error)
    message = redact(raw_message)
    data = getattr(error, "data", None)
    status = getattr(error, "status_code", None)
    request_id = getattr(error, "request_id", None)
    retry_count = getattr(error, "retry_count", None)
    kind = "unavailable"
    codex_info = getattr(error, "codex_error_info", None)
    if codex_info is not None:
        variant = getattr(codex_info, "root", codex_info)
        kind = getattr(variant, "value", type(variant).__name__)
        for field in ("http_connection_failed", "response_stream_connection_failed",
                      "response_stream_disconnected", "response_too_many_failed_attempts"):
            detail = getattr(variant, field, None)
            if detail is not None:
                status = status or getattr(detail, "http_status_code", None)
                if field == "response_too_many_failed_attempts":
                    kind = "responseTooManyFailedAttempts"
    if isinstance(data, dict):
        status = status or data.get("status_code") or data.get("status")
        request_id = request_id or data.get("request_id") or data.get("requestId")
        retry_count = retry_count if retry_count is not None else data.get("retry_count")
    if status is None:
        match = re.search(r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?|status(?:_code)?)\s*[:=]?\s*([45]\d\d)\b", message)
        status = match.group(1) if match else None
    if request_id is None:
        match = re.search(r"(?i)\b(?:request[_ -]?id)\s*[:=]\s*([\w-]+)", message)
        request_id = match.group(1) if match else None
    return {
        "type": type(error).__name__,
        "message": message,
        "codex_error_info": redact(kind),
        "status_code": redact(status) if status is not None else "unavailable",
        "request_id": redact(request_id) if request_id is not None else "unavailable",
        "retry_count": redact(retry_count) if retry_count is not None else "unavailable",
        "retry_exhausted": "yes" if (kind == "responseTooManyFailedAttempts" or
                                     re.search(r"(?i)(retry limit|too many failed attempts)", message)) else "unavailable",
    }


def _stable_blocked_reason(reason: Any, stage: str) -> dict[str, Any]:
    """Keep every repair block inspectable through reason.error.message."""
    if isinstance(reason, dict):
        result = dict(reason)
        if isinstance(result.get("error"), dict) and "message" in result["error"]:
            return result
        source = result.get("message", result)
        result["stage"] = result.get("stage", stage)
        result["error"] = error_evidence(source)
        return result
    return {"stage": stage, "error": error_evidence(reason)}


class ModuleResultError(Exception):
    def __init__(self, error: Any):
        self.error = error
        super().__init__(redact(getattr(error, "message", error)))


def model_failure_kind(error: Any) -> str | None:
    evidence = error if isinstance(error, dict) else error_evidence(error)
    message = str(evidence.get("message", "")).lower()
    status_code = str(evidence.get("status_code", "unavailable"))
    if (status_code in {"401", "403"} or
            re.search(r"quota exceeded|insufficient quota|invalid api key|unauthorized|authentication failed|billing", message)):
        return "blocked"
    if (status_code == "429" or status_code.startswith("5") or
            is_remote_compaction_error(evidence) or
            re.search(r"proxy(?:_error| error)?|proxyerror|upstream connection|remote compaction|"
                      r"remote compact(?:ion)? task|compact task|connection reset|"
                      r"connection refused|connection timed out|timed out|stream disconnected|"
                      r"temporarily unavailable|service unavailable|too many requests|timeout", message)):
        return "transient"
    return None


def classify_observation_failure(observed: dict[str, Any]) -> str | None:
    """Classify an early observation without turning every failed check into a hard stop."""
    if observed.get("status") == "passed":
        return None
    tests = observed.get("tests") or []
    text = " ".join(str(observed.get(key, "")) for key in
                     ("configuration_error", "error", "stderr"))
    text += " " + " ".join(str(test.get("error", "")) for test in tests if isinstance(test, dict))
    lowered = text.lower()
    if re.search(r"missing observation script|invalid observation script|checks\.spec\.js[^\n]*(?:missing|not found)|"
                 r"(?:missing|not found)[^\n]*checks\.spec\.js", lowered):
        return "invalid_observation_script"
    if re.search(r"dependency|npm (?:ci|install)|cannot find module|startup|backend exited|database", lowered):
        return "dependency_startup_hard_failure"
    if (observed.get("configuration_error") or observed.get("configuration_error_scope") or
            any(isinstance(test, dict) and test.get("configuration_error") for test in tests)):
        return "test_observation_error"
    if (observed.get("process_timed_out") or re.search(
            r"connection|timed out|timeout|disconnected|service unavailable|network", lowered)):
        return "environment_connection_timeout"
    if observed.get("status") == "error" and not any(
            isinstance(test, dict) and test.get("executed") and test.get("status") == "unexpected"
            for test in tests):
        return "test_observation_error"
    if observed.get("status") == "failed" and any(
            isinstance(test, dict) and test.get("executed") and test.get("status") == "unexpected"
            for test in tests):
        return "product_behavior"
    return "environment_connection_timeout"


def _early_observation_failures(module: RequirementModule, observed: dict[str, Any],
                                requirements_dir: Path, output_dir: Path,
                                failure_class: str) -> list[dict[str, Any]]:
    evidence_path = str((output_dir / str(observed.get("evidence_path", ""))).resolve())
    frozen_script = str((output_dir / "module-observations" / module.node_id / "checks.spec.js").resolve())
    errors = [item for item in observed.get("tests", [])
              if isinstance(item, dict) and (item.get("status") == "unexpected" or item.get("error"))]
    if not errors:
        errors = [{"title": item["id"], "error": observed.get("configuration_error") or
                   observed.get("error") or "Early observation did not pass"}
                  for item in _atomic_requirement_index(module.subtree)]
    return [{"module_id": module.node_id, "atomic_id": item.get("title") or item.get("id"),
             "requirement_file": str((requirements_dir / "requirements.yaml").resolve()),
             "frozen_script": frozen_script, "evidence_path": evidence_path,
             "failure_class": failure_class,
             "error_excerpt": redact(item.get("error") or "Early observation did not pass")[:800]}
            for item in errors]


def save_batch_working_checkpoint(output_dir: Path, module_id: str, attempt: int,
                                  identity: dict[str, Any], stage: str,
                                  failure: dict[str, Any] | None = None,
                                  build: dict[str, Any] | None = None,
                                  backend: dict[str, Any] | None = None,
                                  completed_ids: list[str] | None = None,
                                  current_batch: dict[str, Any] | None = None,
                                  prompt_context: str | None = None,
                                  source_state: dict[str, Any] | None = None) -> str:
    """Persist a unique recovery point with the original failure and host validation state."""
    state = {"build": build, "backend": backend, **(source_state or {})}
    path = output_dir / save_working_checkpoint(
        output_dir, module_id, attempt, identity, stage,
        completed_ids=completed_ids, current_batch=current_batch,
        source_state=state, prompt_context=prompt_context, failure=failure)
    atomic_json(path / "state.json", {"module_id": module_id, "attempt": attempt, "stage": stage,
                                       "completed_ids": list(completed_ids or []),
                                       "current_batch": current_batch or {"batch_id": module_id},
                                       "source_sha256": product_source_digest(output_dir),
                                       "build": build, "backend": backend,
                                       "preflight": state.get("preflight"),
                                       "state": state.get("state"),
                                       "prompt_context": redact(prompt_context or ""),
                                       "failure": failure})
    if failure is not None:
        atomic_json(path / "failure.json", failure)
    return path.relative_to(output_dir).as_posix()


def _early_repair_prompt(module: RequirementModule, requirements_dir: Path,
                         failures: list[dict[str, Any]], retry: bool = False,
                         runtime_state: dict[str, Any] | None = None) -> str:
    return textwrap.dedent(f"""
        Observation repair for current batch {module.node_id}{' (bounded retry)' if retry else ''}.

        Read only the current module subtree below, the failure evidence, and the actual source
        files needed by its callers. Do not load or restate the full ROOT requirements tree.
        Requirement source path: {requirements_dir / 'requirements.yaml'}

        Current batch scope:
        ```json
        {json.dumps(module.batch_context or {{}}, ensure_ascii=False, indent=2)}
        ```

        Current module subtree:
        ```json
        {json.dumps(module.subtree, ensure_ascii=False, indent=2)}
        ```

        Host source/build/backend state:
        ```json
        {json.dumps(runtime_state or {}, ensure_ascii=False, indent=2)}
        ```

        Failure evidence:
        ```json
        {json.dumps(failures, ensure_ascii=False, indent=2)[:8000]}
        ```

        Repair only this batch. Preserve unrelated correct source and frozen observation files.
        Make one concentrated application fix, leave a short repair note, and return normally.
        The host will independently run build, backend startup and the same frozen observation.
    """).strip()


def repair_module_batch(codex: Any, module: RequirementModule, requirements_dir: Path,
                        output_dir: Path, skills_dir: Path, model: str | None,
                        failures: list[dict[str, Any]], retry: bool = False,
                        runtime_state: dict[str, Any] | None = None) -> None:
    """Run one fresh, subtree-scoped repair turn for an early observation failure."""
    thread = codex.thread_start(cwd=str(output_dir), sandbox=Sandbox.full_access,
                                approval_mode=ApprovalMode.deny_all, model=model,
                                developer_instructions=_developer_instructions(skills_dir))
    prompt = _early_repair_prompt(module, requirements_dir, failures, retry=retry,
                                  runtime_state=runtime_state)
    soft, hard = model_turn_limits("observation_repair", retry=retry)
    _completed_turn(thread, prompt, output_dir, stage="observation_repair",
                    timeout_seconds=hard, soft_timeout_seconds=soft, context_reset=retry)


def repair_early_batch(codex: Any, module: RequirementModule, requirements_dir: Path,
                       output_dir: Path, skills_dir: Path, model: str | None,
                       failures: list[dict[str, Any]], identity: dict[str, Any],
                       build: dict[str, Any], backend: dict[str, Any]) -> dict[str, Any]:
    """Repair one early-failed batch, retrying only transient turn failures once."""
    result: dict[str, Any] = {"status": "failed", "attempts": 0, "failures": [],
                              "working_checkpoints": []}
    snapshot = Path(tempfile.mkdtemp(prefix="arc-early-repair-snapshot-"))
    try:
        copy_repair_snapshot(output_dir, snapshot)
        for attempt in (1, 2):
            result["attempts"] = attempt
            try:
                checkpoint = save_batch_working_checkpoint(
                    output_dir, module.node_id, attempt, identity, "early_observation",
                    failure=failures[0] if failures else None, build=build, backend=backend)
                result["working_checkpoints"].append(checkpoint)
                append_event(output_dir, {"module_id": module.node_id, "stage": "observation_repair",
                                          "status": "started", "attempt": attempt,
                                          "prompt_mode": "current_subtree_and_failure_evidence",
                                          "checkpoint": checkpoint, "started_at": utc_now()})
                repair_module_batch(codex, module, requirements_dir, output_dir, skills_dir, model,
                                    failures, retry=attempt == 2,
                                    runtime_state={"source": product_source_digest(output_dir),
                                                   "build": build, "backend": backend,
                                                   "observation": "failed"})
                save_batch_working_checkpoint(
                    output_dir, module.node_id, attempt, identity, "post_observation_repair_turn",
                    failure=failures[0] if failures else None, build=build, backend=backend)
                repaired_build = build_frontend(output_dir)
                if repaired_build.get("status") != "passed":
                    raise RuntimeError("frontend build after early observation repair did not pass")
                repaired_backend = verify_backend_startup(output_dir)
                if repaired_backend.get("status") != "passed":
                    raise RuntimeError("backend startup after early observation repair did not pass")
                observed = run_module_observation(output_dir, module, "early")
                if observed.get("status") == "passed":
                    result.update(status="passed", observation=observed, build=repaired_build,
                                  backend=repaired_backend)
                    append_event(output_dir, {"module_id": module.node_id, "stage": "observation_repair",
                                              "status": "passed", "attempt": attempt, "ended_at": utc_now()})
                    return result
                failure = {"module_id": module.node_id, "stage": "early_observation_repair",
                           "failure_class": classify_observation_failure(observed),
                           "error": error_evidence(observed.get("configuration_error") or
                                                    observed.get("error") or "Repaired observation did not pass")}
                result["failures"].append(failure)
                append_event(output_dir, {**failure, "status": "failed", "attempt": attempt,
                                          "ended_at": utc_now()})
                break
            except Exception as exc:
                error = exc.error if isinstance(exc, ModuleResultError) else exc
                failure = {"module_id": module.node_id, "stage": "observation_repair",
                           "failure_class": ("remote_compaction" if is_remote_compaction_error(error)
                                             else (model_failure_kind(error) or "repair_error")),
                           "error": error_evidence(error)}
                result["failures"].append(failure)
                try:
                    failed_draft = output_dir / ".arc/codex-recovery/repair-drafts" / (
                        f"early-{module.node_id}-attempt-{attempt}-{uuid.uuid4().hex[:8]}")
                    copy_repair_snapshot(output_dir, failed_draft)
                    atomic_json(failed_draft / "failure.json", failure)
                    failure["draft_path"] = failed_draft.relative_to(output_dir).as_posix()
                except Exception as draft_error:
                    failure["draft_warning"] = error_evidence(draft_error)
                append_event(output_dir, {**failure, "status": "failed", "attempt": attempt,
                                          "ended_at": utc_now()})
                if attempt == 1 and model_failure_kind(error) == "transient":
                    try:
                        append_event(output_dir, {"module_id": module.node_id,
                                                  "stage": "repair_restore", "status": "started",
                                                  "attempt": attempt, "checkpoint": result["working_checkpoints"][-1],
                                                  "error": failure["error"], "started_at": utc_now()})
                        restore_repair_snapshot(snapshot, output_dir)
                        invalidate_dependency_state(output_dir)
                        append_event(output_dir, {"module_id": module.node_id,
                                                  "stage": "repair_restore", "status": "restored",
                                                  "attempt": attempt, "checkpoint": result["working_checkpoints"][-1],
                                                  "ended_at": utc_now()})
                        continue
                    except Exception as restore_error:
                        failure["restore_error"] = error_evidence(restore_error)
                break
        try:
            append_event(output_dir, {"module_id": module.node_id, "stage": "repair_restore",
                                      "status": "started", "attempt": result["attempts"],
                                      "checkpoint": (result["working_checkpoints"][-1]
                                                     if result["working_checkpoints"] else str(snapshot)),
                                      "error": (result["failures"][-1].get("error")
                                                if result["failures"] else None),
                                      "started_at": utc_now()})
            restore_repair_snapshot(snapshot, output_dir)
            invalidate_dependency_state(output_dir)
            result["restored"] = True
            append_event(output_dir, {"module_id": module.node_id, "stage": "repair_restore",
                                      "status": "restored", "attempt": result["attempts"],
                                      "checkpoint": (result["working_checkpoints"][-1]
                                                     if result["working_checkpoints"] else str(snapshot)),
                                      "ended_at": utc_now()})
        except Exception as restore_error:
            result["restore_error"] = error_evidence(restore_error)
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    result["status"] = "failed"
    return result


def _observation_records(output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = output_dir / "module-observations" / "summary.json"
    if not path.is_file():
        return [], []
    saved = json.loads(path.read_text(encoding="utf-8"))
    return list(saved.get("modules", [])), list(saved.get("observation_issues", []))


def _save_observation_records(output_dir: Path, records: list[dict[str, Any]],
                              issues: list[dict[str, Any]]) -> None:
    atomic_json(output_dir / "module-observations" / "summary.json",
                {"modules": records, "observation_issues": issues})


def _observation_record(module: RequirementModule, before: dict[str, Any],
                        after: dict[str, Any] | None,
                        contract: dict[str, Any]) -> dict[str, Any]:
    final = after or before
    tests = before.get("tests", []) or (after or {}).get("tests", [])
    titles = [item.get("title", "") for item in tests] or list(module.chain_titles)
    return {"module_id": module.node_id,
            "root_module_id": module.root_module_id or module.node_id,
            "status": final["status"],
            "initial_status": before["status"], "repair_attempted": after is not None,
            "script_changed": any(item.get("delivered_script_changed") or item.get("frozen_file_changed")
                                  for item in (before, after) if item),
            "before": before, "after": after,
            "requirement_ids": sorted({identifier for chain in contract["chains"]
                                       if chain["title"] in titles for identifier in
                                       chain["requirement_ids"]}),
            "chain_titles": titles,
            "duration_seconds": round(sum(item.get("duration_seconds", 0) for item in
                                          (before, after) if item), 3),
            "evidence_paths": [item["evidence_path"] for item in (before, after) if item]}


def commit_module(runtime: AgentRuntime, module_id: str, output_dir: Path, label: str) -> str:
    runtime.git.ensure_repo(create_initial_commit=False)
    runtime.git.commit(f"{module_id} (implement): {label}")
    head = runtime.git.current_head()
    if not head:
        raise RuntimeError("Workspace commit has no HEAD")
    append_event(output_dir, {"stage": "commit", "module_id": module_id,
                              "status": "passed", "git_head": head})
    return head


def _stage_external_predecessors(requirements_dir: Path, output_dir: Path,
                                 prepared_input: bool,
                                 verified_prior_stage: bool = False) -> set[str]:
    """Allow frozen historical IDs only for a default-template evolution task."""
    if not prepared_input or verified_prior_stage:
        return set()
    source = engineering.source_index(requirements_dir)
    root_name = str(source.get("payload", {}).get("name") or "")
    allowlist = next((ids for _task, (name, ids) in _DEFAULT_TEMPLATE_EVOLUTION_ALLOWLIST.items()
                      if root_name == name), None)
    if allowlist is None:
        return set()
    return engineering.missing_source_dependencies(source).intersection(allowlist)


def implement_modules(
    requirements_dir: Path,
    output_dir: Path,
    modules: list[RequirementModule],
    skills_dir: Path,
    identity: dict[str, Any],
    completed_ids: list[str],
    prepared_input: bool = False,
    verified_prior_stage: bool = False,
    resume_batch_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    generation_started = time.monotonic()
    first_coding_seconds = None
    model = os.environ.get("MODEL", "").strip() or None
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    root_context = load_root_context(requirements_dir)
    memory_run_id = uuid.uuid4().hex
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    pending_issues: list[dict[str, Any]] = []
    _, observation_issues = _observation_records(output_dir)
    module_results: list[dict[str, Any]] = []
    early_observations: list[dict[str, Any]] = []
    early_feedback: list[dict[str, Any]] = []
    attempted_ids: list[str] = []
    attempted_batch_ids: list[str] = []
    completed_batches: list[str] = list((resume_batch_state or {}).get("completed_batches", []))
    blocked_batches: list[dict[str, Any]] = []
    skipped_batches: list[dict[str, Any]] = []
    pending_batch_plan: tuple[str, dict[str, Any]] | None = None
    blocked = False
    external_predecessors = _stage_external_predecessors(
        requirements_dir, output_dir, prepared_input, verified_prior_stage=verified_prior_stage
    )
    lineage_before_preparation = _recovery_lineage(output_dir)
    try:
        preflight = prepare_engineering(requirements_dir, output_dir, modules, identity,
                                        external_predecessors=external_predecessors)
    except Exception as exc:
        # Continue only with validated source and trustworthy recovery authority.
        append_event(output_dir, {"stage": "engineering_preflight", "status": "failed",
                                  "error": error_evidence(exc), "ended_at": utc_now()})
        error = exc.error if isinstance(exc, ModuleResultError) else exc
        failure = {"stage": "engineering_preflight", "error": error_evidence(error)}
        pending_issues.append(failure)
        if model_failure_kind(error) == "blocked":
            blocked = True
        else:
            try:
                preflight = _engineering_receipt(requirements_dir, output_dir, modules, identity,
                                                 allow_fresh=not lineage_before_preparation,
                                                 external_predecessors=external_predecessors)
                modules[:] = [replace(item, planning_authority=json.loads(json.dumps(preflight))) for item in modules]
            except Exception as integrity_error:
                pending_issues.append({"stage": "engineering_integrity", "error": error_evidence(integrity_error)})
                blocked = True
    if not blocked:
        resource_failure = (preflight.get('host_integrity_stop') or preflight.get('model_blocked_reason') or
                            ({'stage': 'script_correction_pending', 'message':
                              'Correction terminal state is not checkpointed; no later model requests'}
                             if preflight.get('script_correction_pending') else None) or
                            preflight.get("contract", {}).get("reason")) or next((
            entry.get("reason") for entry in preflight.get("observations", {}).values()
            if model_failure_kind((entry.get("reason") or {}).get("error", {})) == "blocked"), None)
        if resource_failure:
            pending_issues.append(resource_failure)
            blocked = True
    lifecycle_stage = "sdk_constructor_enter"
    try:
        with (nullcontext(None) if blocked else _sdk_session(_codex_config(CodexConfig))) as codex:
            try:
                lifecycle_stage = "sdk_login"
                if api_key and not blocked:
                    codex.login_api_key(api_key)
                lifecycle_stage = "generation"
                for root_module in modules:
                    if blocked:
                        break
                    resume_current = (resume_batch_state or {}).get("current_batch") or {}
                    resume_root_id = ((resume_batch_state or {}).get("root_module_id") or
                                      resume_current.get("root_module_id"))
                    if (root_module.node_id in completed_ids and
                            resume_root_id != root_module.node_id):
                        continue
                    attempted_ids.append(root_module.node_id)
                    try:
                        # The source index contains only the current requirements
                        # package.  Prepared evolution inputs may also expose a
                        # strictly allowlisted baseline predecessor set.
                        available_ids = (set(engineering.source_index(requirements_dir).get("nodes", {})) |
                                         set(external_predecessors))
                        resume_plan_path = output_dir / ".arc/engineering/current-batch-plan.json"
                        if (resume_root_id == root_module.node_id and
                                resume_plan_path.is_file()):
                            batch_plan = json.loads(resume_plan_path.read_text(encoding="utf-8"))
                            resume_plan_hash = ((resume_batch_state or {}).get("batch_plan_sha256") or
                                                resume_current.get("batch_plan_sha256"))
                            if batch_plan.get("batch_plan_sha256") != resume_plan_hash:
                                raise ValueError("Current batch plan hash differs from checkpoint")
                            resume_batch_state = None
                        else:
                            batch_plan = plan_current_module_batches(
                                root_module.subtree, available_dependency_ids=available_ids)
                        write_current_module_plan(output_dir, root_module.node_id, batch_plan)
                        current_batches = [item for item in materialize_current_batches(root_module, batch_plan)
                                           if item.batch_id not in completed_batches]
                        current_module_is_split = batch_plan.get("decision") == "split"
                        pending_batch_ids = {item.node_id for item in current_batches}
                        succeeded_batch_ids: set[str] = set()
                    except Exception as plan_error:
                        batch_plan = {"root_module_id": root_module.node_id, "decision": "keep_original",
                                      "stop_reason": "splitter_exception", "batch_count": 1,
                                      "planned_module_ids": [root_module.node_id],
                                      "planned_model_calls": 2, "batches": [{
                                          "batch_id": root_module.node_id,
                                          "root_module_id": root_module.node_id,
                                          "child_ids": [str(child.get("id") or child.get("req_id") or "")
                                                        for child in root_module.subtree.get("children", [])
                                                        if isinstance(child, dict)],
                                          "atomic_ids": [item["id"] for item in _atomic_requirement_index(root_module.subtree)],
                                      }], "issues": [{"kind": "splitter_exception",
                                                    "message": redact(plan_error)}]}
                        append_event(output_dir, {"module_id": root_module.node_id,
                                                  "stage": "current_module_splitter",
                                                  "status": "fallback",
                                                  "error": error_evidence(plan_error)})
                        write_current_module_plan(output_dir, root_module.node_id, batch_plan)
                        current_batches = [root_module]
                        current_module_is_split = False
                    for module in current_batches:
                        if blocked:
                            break
                        if module.node_id in completed_ids:
                            continue
                        prefix_completed_ids = list(completed_ids)
                        prefix_completed_batches = list(completed_batches)
                        batch_completed = False
                        batch_failed = False
                        record: dict[str, Any] = {}
                        root_id = module.root_module_id or module.node_id
                        is_split_batch = module.batch_id is not None and module.batch_id != root_id
                        batch_position = next((index for index, item in enumerate(current_batches)
                                               if item.node_id == module.node_id), 0)
                        current_scope = {
                            "root_module_id": root_id,
                            "batch_id": module.batch_id or root_id,
                            "batch_plan_sha256": (module.batch_plan_sha256 or
                                                  batch_plan.get("batch_plan_sha256")),
                            "batch_ids": [item.batch_id or item.node_id for item in current_batches],
                            "batch_position": batch_position,
                        }
                        entry = prepare_module_observation(codex, module, requirements_dir, output_dir,
                                                           root_context, model, preflight, modules)
                        reason = entry.get("reason") or {}
                        if entry.get("status") != "prepared":
                            early_feedback.append({"module_id": module.node_id, "provisional": True,
                                                   "business_verdict": "unverified", "reason": _redact_json(reason)})
                        if model_failure_kind(reason.get("error", {})) == "blocked":
                            pending_issues.append(reason)
                            blocked = True
                            break
                        if entry.get("status") != "prepared":
                            pending_issues.append(reason or {"module_id": module.node_id,
                                                             "stage": "planning",
                                                             "message": "Batch observation planning was not prepared"})
                            batch_failed = True
                            blocked_record = {"module_id": module.node_id,
                                              "batch_id": module.batch_id or module.node_id,
                                              "root_module_id": root_id,
                                              "attempts": 0,
                                              "failure_class": "batch_planning_failure",
                                              "failure": reason}
                            blocked_batches.append(blocked_record)
                            skipped_batches.append({"module_id": module.node_id,
                                                    "batch_id": module.batch_id or module.node_id,
                                                    "reason": "batch_planning_failure"})
                            try:
                                blocked_record["recovery_marker"] = save_run_recovery_marker(
                                    output_dir, prefix_completed_ids, current_scope, identity,
                                    failure=reason)
                            except Exception as marker_error:
                                blocked_record["recovery_marker_error"] = error_evidence(marker_error)
                            completed_ids[:] = prefix_completed_ids
                            completed_batches[:] = prefix_completed_batches
                            continue
                        attempted_batch_ids.append(module.node_id)
                        started = time.monotonic()
                        if first_coding_seconds is None:
                            first_coding_seconds = round(time.monotonic() - generation_started, 3)
                        started_at = utc_now()
                        append_event(output_dir, {"module_id": module.node_id, "stage": "model", "status": "started",
                                                  "started_at": started_at})
                        previous_failure: dict[str, Any] | None = None
                        transport_retries = 0
                        technical_retries = 0
                        failure_history: list[dict[str, Any]] = []
                        working_checkpoint: str | None = None
                        for attempt in (1, 2, 3):
                            stage = "model"
                            try:
                                try:
                                    memory_source = product_source_digest(output_dir)
                                except OSError:
                                    memory_source = preflight.get("source_sha256", "")
                                previous_error = (previous_failure.get("error") if previous_failure else None)
                                if previous_error is None and previous_failure:
                                    previous_error = ((previous_failure.get("failures") or [{}])[-1]).get("error")
                                previous_source = (previous_failure.get("memory_source", memory_source)
                                                   if previous_failure else memory_source)
                                if previous_error and should_skip_engineering_request(
                                        output_dir, previous_source, module.node_id, memory_run_id,
                                        previous_error, include_candidate=True):
                                    failure = {"stage": "model", "error": previous_error,
                                               "duplicate_model_request": True,
                                               "message": "duplicate root cause already known; model request skipped"}
                                    failure_history.append({"attempt": attempt, **failure})
                                    append_event(output_dir, {"module_id": module.node_id, **failure,
                                                              "status": "skipped", "ended_at": utc_now()})
                                    pending_issues.append({"module_id": module.node_id, **failure})
                                    module_results.append({"module_id": module.node_id, "status": "blocked",
                                                           "attempts": attempt, "observation": None,
                                                           "failure_history": failure_history, **failure})
                                    blocked = True
                                    break
                                emit_runtime_event(runtime.events.mark_implementation_started, output_dir, module.node_id)
                                print(f"[codex] Implementing {module.index}/{module.total}: {module.node_id}", flush=True)
                                if previous_failure:
                                    prompt = module_retry_prompt(module, requirements_dir, skills_dir,
                                                                  previous_failure)
                                else:
                                     prompt = _module_prompt(module, requirements_dir, completed_ids, skills_dir,
                                                             root_context, pending_issues + early_feedback,
                                                             engineering_memory_hints(output_dir, memory_source,
                                                                                      module.node_id))
                                     prompt += engineering_context(requirements_dir, output_dir, module)
                                working_checkpoint = save_working_checkpoint(
                                    output_dir, module.node_id, attempt, identity, "pre_model_turn",
                                    completed_ids=prefix_completed_ids,
                                    current_batch=current_scope,
                                    source_state={"preflight": preflight,
                                                  "state": {"stage": "model", "attempt": attempt}},
                                    prompt_context=prompt,
                                )
                                append_event(output_dir, {"module_id": module.node_id,
                                                          "stage": "model", "status": "working_checkpoint",
                                                          "attempt": attempt, "path": working_checkpoint,
                                                          "completed_ids": prefix_completed_ids,
                                                          "current_batch": current_scope,
                                                          "started_at": utc_now()})
                                thread = codex.thread_start(
                                    cwd=str(output_dir), sandbox=Sandbox.full_access,
                                    approval_mode=ApprovalMode.deny_all, model=model,
                                    developer_instructions=_developer_instructions(skills_dir),
                                )
                                latest_checkpoint = _recovery_dir(output_dir) / "latest.json"
                                append_event(output_dir, {"module_id": module.node_id, "stage": "model",
                                                          "status": "context_reset" if previous_failure else "context_initial",
                                                          "attempt": attempt, "fresh_session": True,
                                                          "checkpoint_available": latest_checkpoint.is_file(),
                                                          "prompt_mode": "compact_retry" if previous_failure else "full",
                                                          "started_at": utc_now()})
                                soft_limit, hard_limit = model_turn_limits("model", retry=bool(previous_failure))
                                _completed_turn(thread, prompt, output_dir, stage="model",
                                                timeout_seconds=hard_limit,
                                                soft_timeout_seconds=soft_limit,
                                                context_reset=bool(previous_failure))
                                append_event(output_dir, {"module_id": module.node_id,
                                                          "stage": "model", "status": "working_checkpoint_complete",
                                                          "attempt": attempt, "path": working_checkpoint,
                                                          "ended_at": utc_now()})
                                try:
                                    engineering.capture_module_state(output_dir, module.node_id,
                                        _read_implementation(output_dir / "module-observations" / module.node_id),
                                        requirements_dir)
                                except Exception as metadata_error:
                                    emit_runtime_event(append_event, output_dir, output_dir,
                                        {"module_id": module.node_id, "stage": "engineering_handoff",
                                         "status": "unverified", "error": error_evidence(metadata_error)})
                                append_event(output_dir, {"module_id": module.node_id, "stage": "model", "status": "passed",
                                                          "attempt": attempt, "ended_at": utc_now()})
                                stage = "build"
                                build = build_frontend(output_dir)
                                if build.get("status") != "passed":
                                    raise RuntimeError("frontend build did not pass")
                                stage = "backend"
                                backend = verify_backend_startup(output_dir)
                                if backend.get("status") != "passed":
                                    raise RuntimeError("backend startup did not pass")
                                root_id = module.root_module_id or module.node_id
                                is_split_batch = module.batch_id is not None and module.batch_id != root_id
                                is_last_batch = (not is_split_batch or
                                                 module.batch_id == current_batches[-1].batch_id)
                                batch_position = next((index for index, item in enumerate(current_batches)
                                                       if item.node_id == module.node_id), 0)
                                prior_batches_complete = all(
                                    item.batch_id in completed_batches or item.node_id in succeeded_batch_ids
                                    for item in current_batches[:batch_position]
                                )
                                continuous = (len(completed_ids) == module.index - 1 and
                                              prior_batches_complete)
                                stage = "commit"
                                checkpoint_ids = ([*completed_ids, root_id]
                                                  if (continuous and is_last_batch)
                                                  else list(completed_ids))
                                checkpoint_batches = list(completed_batches)
                                if is_split_batch and module.node_id not in checkpoint_batches:
                                    checkpoint_batches.append(module.node_id)
                                current_scope = {"root_module_id": root_id,
                                                 "batch_id": module.batch_id or root_id,
                                                 "batch_plan_sha256": (module.batch_plan_sha256 or
                                                                       batch_plan.get("batch_plan_sha256")),
                                                 "batch_ids": [item.batch_id for item in current_batches]}
                                emit_runtime_event(runtime.events.mark_implementation_done, output_dir, module.node_id)
                                head = commit_module(runtime, module.node_id, output_dir, "runnable; acceptance pending")
                                stage = "checkpoint"
                                if continuous:
                                    save_checkpoint(output_dir, checkpoint_ids, identity, build,
                                                    completed_batches=checkpoint_batches,
                                                    current_batch=(None if (continuous and is_last_batch)
                                                                   else current_scope))
                                    completed_batches[:] = checkpoint_batches
                                    if checkpoint_ids != completed_ids:
                                        completed_ids[:] = checkpoint_ids
                                try:
                                    verified_source = product_source_digest(output_dir)
                                except OSError:
                                    verified_source = memory_source
                                if continuous and is_last_batch:
                                    module_status = "pending_observation"
                                elif is_split_batch:
                                    module_status = "pending_observation"
                                else:
                                    module_status = "runtime_passed_uncheckpointed"
                                append_event(output_dir, {"module_id": module.node_id, "stage": "checkpoint",
                                                          "status": "passed", "attempt": attempt, "ended_at": utc_now(),
                                                          "completed_ids": completed_ids, "build": build, "backend": backend,
                                                          "git_head": head})
                                module_results.append({"module_id": module.node_id,
                                                        "root_module_id": root_id,
                                                        "status": module_status,
                                                        "attempts": attempt, "observation": None, "git_head": head,
                                                        "failure_history": failure_history})
                                if entry.get("status") == "prepared":
                                    try:
                                        frozen = engineering.observation_from_authority(
                                            output_dir, module.node_id, identity, preflight["source_sha256"], preflight)
                                        if frozen is None:
                                            raise ValueError("Early observation authority changed")
                                        contract, script = frozen
                                        planned = replace(module, frozen_script=script,
                                                          chain_titles=tuple(chain["title"] for chain in contract["chains"]),
                                                          frozen_version=entry.get("version", 1))
                                        print(f"[observation] Early {module.node_id} started", flush=True)
                                        observed = run_module_observation(output_dir, planned, "early")
                                        if correct_observation_script(codex, module, requirements_dir, output_dir,
                                                                      preflight, observed, model, modules):
                                            updated = engineering.observation_from_authority(
                                                output_dir, module.node_id, identity, preflight["source_sha256"], preflight)
                                            if updated is None:
                                                raise ValueError("Corrected observation authority is unavailable")
                                            observed = run_module_observation(output_dir,
                                                replace(planned, frozen_script=updated[1], frozen_version=2), "early")
                                        record = {"module_id": module.node_id, "status": observed["status"],
                                                  "provisional": True, "business_verdict": "unverified",
                                                  "application_sha256": application_digest(output_dir),
                                                  "tests": [{"title": item.get("title"), "status": item.get("status"),
                                                             "executed": item.get("executed"),
                                                            "result_status": item.get("result_status"),
                                                            **({"error": redact(item.get("configuration_error") or
                                                                               item.get("error"))[:400]}
                                                               if item.get("configuration_error") or item.get("error") else {})}
                                                            for item in observed.get("tests", [])],
                                                  "evidence_path": observed["evidence_path"]}
                                    except Exception as exc:
                                        error = exc.error if isinstance(exc, ModuleResultError) else exc
                                        observed = {"status": "error", "error": redact(error),
                                                    "tests": [], "evidence_path": ""}
                                        record = {"module_id": module.node_id, "status": "error", "provisional": True,
                                                  "business_verdict": "unverified",
                                                 "reason": {"stage": "early_observation", "error": error_evidence(error)}}
                                        integrity = getattr(exc, 'host_integrity_stop', None)
                                        if integrity:
                                            record['reason']['host_integrity_stop'] = integrity
                                        if preflight.get('script_correction_pending'):
                                            record['reason']['script_correction_pending'] = True
                                        if integrity or preflight.get('script_correction_pending') or model_failure_kind(error) == "blocked":
                                            pending_issues.append(record["reason"])
                                            if model_failure_kind(error) == "blocked":
                                                record["reason"]["model_blocked"] = True
                                            blocked = True
                                    print(f"[observation] Early {module.node_id}: {record['status']} (provisional)", flush=True)
                                    if record.get("status") != "passed" and not blocked:
                                        failure_class = classify_observation_failure(observed)
                                        record["failure_class"] = failure_class
                                        failures = _early_observation_failures(
                                            module, observed, requirements_dir, output_dir, failure_class)
                                        record["failure_evidence"] = failures
                                        try:
                                            record["working_checkpoint"] = save_batch_working_checkpoint(
                                                output_dir, module.node_id, 1, identity, "early_observation",
                                                failure=failures[0] if failures else None,
                                                build=build, backend=backend)
                                        except Exception as checkpoint_error:
                                            record["checkpoint_error"] = error_evidence(checkpoint_error)
                                            failure_class = "dependency_startup_hard_failure"
                                            record["failure_class"] = failure_class
                                        reason = {"module_id": module.node_id, "stage": "early_observation",
                                                  "failure_class": failure_class,
                                                  "error": error_evidence(
                                                      observed.get("configuration_error") or observed.get("error") or
                                                      "Batch observation did not pass")}
                                        original_reason = record.get("reason") or {}
                                        for key in ("host_integrity_stop", "script_correction_pending", "model_blocked"):
                                            if key in original_reason:
                                                reason[key] = original_reason[key]
                                        record["reason"] = reason
                                        if failure_class == "dependency_startup_hard_failure":
                                            pending_issues.append(reason)
                                            blocked = True
                                        elif failure_class in {"test_observation_error", "invalid_observation_script"}:
                                            # Script/configuration failures remain unverified and do not stop generation.
                                            pending_issues.append(reason)
                                        else:
                                            print(f"[observation] Early {module.node_id}: repair started", flush=True)
                                            repair = repair_early_batch(
                                                codex, planned, requirements_dir, output_dir, skills_dir, model,
                                                failures, identity, build, backend)
                                            record["repair"] = repair
                                            if repair.get("status") == "passed":
                                                observed = repair["observation"]
                                                record["status"] = observed["status"]
                                                record["evidence_path"] = observed["evidence_path"]
                                                record["tests"] = [{"title": item.get("title"),
                                                                    "status": item.get("status"),
                                                                    "executed": item.get("executed"),
                                                                    "result_status": item.get("result_status"),
                                                                    **({"error": redact(item.get("error"))[:400]}
                                                                       if item.get("error") else {})}
                                                                   for item in observed.get("tests", [])]
                                                print(f"[observation] Early {module.node_id}: repair passed", flush=True)
                                            else:
                                                pending_issues.append({**reason, "repair": repair})
                                                last_repair_failure = (repair.get("failures") or [{}])[-1]
                                                repair_error = last_repair_failure.get("error", {})
                                                if (model_failure_kind(repair_error) == "blocked" or
                                                        repair.get("restore_error")):
                                                    blocked = True
                                    if record.get("status") != "passed" and not blocked:
                                        # A script-only observation error is unverified, but it still cannot
                                        # make the current batch look like a completed prefix.
                                        completed_ids[:] = prefix_completed_ids
                                        completed_batches[:] = prefix_completed_batches
                                    elif (record.get("status") != "passed" and blocked and
                                          not (record.get("reason", {}).get("host_integrity_stop") or
                                               record.get("reason", {}).get("script_correction_pending") or
                                               record.get("reason", {}).get("model_blocked"))):
                                        completed_ids[:] = prefix_completed_ids
                                        completed_batches[:] = prefix_completed_batches
                                    if record.get("status") != "passed" and not blocked:
                                        try:
                                            save_checkpoint(output_dir, list(prefix_completed_ids), identity,
                                                            build, completed_batches=list(prefix_completed_batches),
                                                            current_batch=current_scope)
                                        except Exception as checkpoint_error:
                                            record["checkpoint_error"] = error_evidence(checkpoint_error)
                                    early_observations.append(record)
                                    early_feedback.append(record)
                                    batch_completed = record.get("status") == "passed" and not blocked
                                    if batch_completed and continuous and is_last_batch:
                                        module_status = "completed"
                                    elif batch_completed and is_split_batch:
                                        module_status = "batch_completed"
                                    if module_results and module_results[-1].get("module_id") == module.node_id:
                                        module_results[-1]["status"] = (
                                            module_status if batch_completed else (
                                                "unverified" if record.get("failure_class") in
                                                {"test_observation_error", "invalid_observation_script"}
                                                else "pending"))
                                        module_results[-1]["observation"] = record
                                break
                            except Exception as exc:
                                error = exc.error if isinstance(exc, ModuleResultError) else exc
                                evidence = error_evidence(error)
                                failure = {"stage": stage, "error": evidence}
                                failure_history.append({"attempt": attempt, **failure})
                                print("[generation failure] " + json.dumps(
                                    {"module_id": module.node_id, "attempt": attempt, **failure}, ensure_ascii=False), flush=True)
                                append_event(output_dir, {"module_id": module.node_id, "stage": stage, "status": "failed",
                                                          "attempt": attempt, "started_at": started_at, "ended_at": utc_now(),
                                                          "duration_seconds": round(time.monotonic() - started, 3),
                                                          "error": evidence})
                                emit_runtime_event(runtime.events.mark_implementation_failed, output_dir, module.node_id)
                                kind = model_failure_kind(error) if stage == "model" else None
                                failure_class = ("transient_model_failure" if stage == "model" and kind == "transient"
                                                 else (kind or "batch_failure"))
                                failure["failure_class"] = failure_class
                                failure_history[-1]["failure_class"] = failure_class
                                if working_checkpoint:
                                    try:
                                        atomic_json(output_dir / working_checkpoint / "failure.json", failure)
                                    except Exception as checkpoint_error:
                                        failure["checkpoint_error"] = error_evidence(checkpoint_error)
                                if stage == "model":
                                    remember_engineering_memory(output_dir, memory_source, module.node_id,
                                                                memory_run_id, evidence, "candidate",
                                                                {"phase": "model", "source_stage": stage,
                                                                 "error_kind": failure_class,
                                                                 "message": evidence.get("message", "")})
                                    append_event(output_dir, {"module_id": module.node_id,
                                                              "stage": failure_class, "status": "failed",
                                                              "attempt": attempt, "error": evidence,
                                                              "ended_at": utc_now()})
                                if kind == "blocked":
                                    blocked = True
                                elif stage == "model" and kind == "transient" and not transport_retries:
                                    append_event(output_dir, {"module_id": module.node_id,
                                                              "stage": "repair_restore", "status": "started",
                                                              "attempt": attempt, "failure_class": failure_class,
                                                              "checkpoint": working_checkpoint,
                                                              "started_at": utc_now()})
                                    recovery = restore_failed_module(output_dir, module.node_id, identity,
                                                                     Path(__file__).resolve().parent)
                                    failure["recovery"] = recovery
                                    failure_history[-1]["recovery"] = recovery
                                    append_event(output_dir, {"module_id": module.node_id,
                                                              "stage": "repair_restore",
                                                              "status": recovery.get("status", "unavailable"),
                                                              "attempt": attempt, "failure_class": failure_class,
                                                              "checkpoint": recovery.get("checkpoint"),
                                                              "ended_at": utc_now()})
                                    if (recovery["status"] == "restored" and
                                            recovery.get("completed_ids") == prefix_completed_ids
                                            and not recovery.get("engineering_warning")):
                                        transport_retries += 1
                                        previous_failure = {"failures": failure_history, "recovery": recovery,
                                                             "memory_source": memory_source,
                                                             "failure_class": failure_class}
                                        continue
                                    batch_failed = True
                                elif attempt < 3 and stage in {"build", "backend"} and not technical_retries:
                                    technical_retries += 1
                                    previous_failure = failure
                                    continue
                                else:
                                    if stage in {"model", "build", "backend"} and "recovery" not in failure:
                                        append_event(output_dir, {"module_id": module.node_id,
                                                                  "stage": "repair_restore", "status": "started",
                                                                  "attempt": attempt, "failure_class": failure_class,
                                                                  "checkpoint": working_checkpoint,
                                                                  "started_at": utc_now()})
                                        recovery = restore_failed_module(output_dir, module.node_id, identity,
                                                                         Path(__file__).resolve().parent)
                                        failure["recovery"] = recovery
                                        failure_history[-1]["recovery"] = recovery
                                        append_event(output_dir, {"module_id": module.node_id,
                                                                  "stage": "repair_restore",
                                                                  "status": recovery.get("status", "unavailable"),
                                                                  "attempt": attempt, "failure_class": failure_class,
                                                                  "checkpoint": recovery.get("checkpoint"),
                                                                  "ended_at": utc_now()})
                                        if (recovery.get("status") != "restored" or
                                                recovery.get("engineering_warning")):
                                            blocked = True
                                        else:
                                            batch_failed = True
                                    elif stage in {"checkpoint", "commit"}:
                                        batch_failed = True
                                    else:
                                        batch_failed = True
                                if batch_failed and not blocked:
                                    completed_ids[:] = prefix_completed_ids
                                    completed_batches[:] = prefix_completed_batches
                                    blocked_record = {"module_id": module.node_id,
                                                      "batch_id": module.batch_id or module.node_id,
                                                      "root_module_id": module.root_module_id or module.node_id,
                                                      "attempts": attempt,
                                                      "failure_class": failure_class,
                                                      "failure": failure}
                                    blocked_batches.append(blocked_record)
                                    skipped_batches.append({"module_id": module.node_id,
                                                            "batch_id": module.batch_id or module.node_id,
                                                            "reason": failure_class})
                                    try:
                                        blocked_record["recovery_marker"] = save_run_recovery_marker(
                                            output_dir, prefix_completed_ids, current_scope, identity,
                                            working_checkpoint=working_checkpoint,
                                            failure=failure)
                                    except Exception as marker_error:
                                        blocked_record["recovery_marker_error"] = error_evidence(marker_error)
                                if stage in {"model", "build", "backend"} and "recovery" not in failure and blocked:
                                    recovery = restore_failed_module(output_dir, module.node_id, identity,
                                                                     Path(__file__).resolve().parent)
                                    failure["recovery"] = recovery
                                    failure_history[-1]["recovery"] = recovery
                                pending_issues.append({"module_id": module.node_id, **failure})
                                module_results.append({"module_id": module.node_id,
                                                       "root_module_id": module.root_module_id or module.node_id,
                                                       "batch_id": module.batch_id or module.node_id,
                                                       "status": "commit_failed" if stage == "commit" else (
                                                           "blocked" if blocked else
                                                           ("unverified" if stage == "model" else "pending")),
                                                       "attempts": attempt, "observation": None,
                                                       "failure_history": failure_history, **failure})
                                break
                        if batch_completed:
                            succeeded_batch_ids.add(module.node_id)
                        elif current_module_is_split and not blocked:
                            pending_batch_plan = (root_module.node_id, batch_plan)
                        if blocked:
                            break
                    if (not blocked and pending_batch_ids == succeeded_batch_ids):
                        archive_current_module_plan(output_dir, root_module.node_id, batch_plan, identity)
            except Exception as exc:
                error = exc.error if isinstance(exc, ModuleResultError) else exc
                failure = {"stage": lifecycle_stage, "error": error_evidence(error)}
                pending_issues.append(failure)
                append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
                blocked = True
            lifecycle_stage = "sdk_exit"
    except Exception as exc:
        error = exc.error if isinstance(exc, ModuleResultError) else exc
        failure = {"stage": lifecycle_stage, "error": error_evidence(error)}
        pending_issues.append(failure)
        append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
        blocked = True
    if pending_batch_plan is not None:
        write_current_module_plan(output_dir, *pending_batch_plan)
    runnable_statuses = {"completed", "batch_completed", "pending_observation", "runtime_passed_uncheckpointed"}
    any_success = any(item.get("git_head") or item.get("status") in runnable_statuses
                      for item in module_results)
    generation_status = ("blocked" if blocked or (module_results and not any_success)
                         else ("partial" if (pending_issues or blocked_batches or skipped_batches)
                               else "complete"))
    return {"status": generation_status,
             "completed_ids": completed_ids, "pending_issues": pending_issues,
            "early_observations": early_observations,
            "observation_issues": observation_issues,
            "attempted_ids": attempted_ids,
            "attempted_root_ids": attempted_ids,
            "attempted_batch_ids": attempted_batch_ids,
            "blocked_batches": blocked_batches,
            "skipped_batches": skipped_batches,
            "unattempted_ids": [module.node_id for module in modules if module.node_id not in
                                completed_ids and module.node_id not in attempted_ids],
            "unattempted_root_ids": [module.node_id for module in modules if module.node_id not in
                                      completed_ids and module.node_id not in attempted_ids],
            "module_results": module_results,
            "completed_batches": completed_batches,
            "timing": {"first_coding_seconds": first_coding_seconds}, "usage": None}


def application_digest(output_dir: Path) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(output_dir):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs
                         if name != "module-observations" and
                         not _excluded_snapshot_path((parent / name).relative_to(output_dir)) and
                         not (parent / name).is_symlink())
        for name in sorted(files):
            path = parent / name
            relative = path.relative_to(output_dir)
            if _excluded_snapshot_path(relative) or path.is_symlink():
                continue
            digest.update(relative.as_posix().encode("utf-8"))
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def product_source_digest(output_dir: Path) -> str:
    """Digest of generated product source only; never agent bookkeeping or build output."""
    digest = hashlib.sha256()
    for name in ("frontend", "backend"):
        root = output_dir / name
        if not root.is_dir():
            continue
        for directory, dirs, files in os.walk(root):
            parent = Path(directory)
            dirs[:] = sorted(item for item in dirs
                             if not _excluded_snapshot_path((parent / item).relative_to(output_dir))
                             and not (parent / item).is_symlink())
            for filename in sorted(files):
                path = parent / filename
                relative = path.relative_to(output_dir)
                if _excluded_snapshot_path(relative) or path.is_symlink():
                    continue
                digest.update(relative.as_posix().encode("utf-8"))
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _checkpoint_record(output_dir: Path, identity: dict[str, Any] | None = None,
                       module_ids: list[str] | None = None) -> dict[str, Any] | None:
    """Load one checkpoint only when its pointer, manifest and source agree."""
    recovery = _recovery_dir(output_dir)
    latest_path = recovery / "latest.json"
    if not latest_path.is_file():
        return None
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(latest, dict):
        return None
    if identity is not None and latest.get("identity") != identity:
        return None
    completed = latest.get("completed_ids")
    if (not isinstance(completed, list) or any(not isinstance(item, str) for item in completed) or
            (not completed and not isinstance(latest.get("current_batch"), dict))):
        return None
    if len(completed) != len(set(completed)):
        return None
    if module_ids is not None and completed != module_ids[:len(completed)]:
        return None
    name = latest.get("checkpoint")
    if not isinstance(name, str) or not re.fullmatch(r"module-\d{3}-[0-9a-f]{12}", name):
        return None
    checkpoint = recovery / "checkpoints" / name
    source = checkpoint / "source"
    if not (checkpoint / "checkpoint.json").is_file() or not source.is_dir():
        return None
    try:
        manifest = json.loads((checkpoint / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    if (manifest.get("identity") != latest.get("identity") or
            manifest.get("completed_ids") != completed or
            manifest.get("completed_batches", latest.get("completed_batches", [])) !=
            latest.get("completed_batches", []) or
            manifest.get("current_batch") != latest.get("current_batch") or
            manifest.get("batch_plan_sha256") != latest.get("batch_plan_sha256") or
            (module_ids is not None and manifest.get("completed_ids") != module_ids[:len(completed)])):
        return None
    engineering_files = manifest.get("engineering_files", {})
    if not isinstance(engineering_files, dict) or "preflight.json" not in engineering_files:
        return None
    try:
        if manifest.get("source_sha256") != digest_tree(source):
            return None
    except OSError:
        return None
    for filename, digest in manifest.get("engineering_files", {}).items():
        if filename not in {"contract.json", "state.json", "preflight.json", "current-batch-plan.json"}:
            return None
        metadata = checkpoint / "engineering" / filename
        if not metadata.is_file() or hashlib.sha256(metadata.read_bytes()).hexdigest() != digest:
            return None
    current_batch = manifest.get("current_batch")
    if isinstance(current_batch, dict):
        plan_file = checkpoint / "engineering" / "current-batch-plan.json"
        if not plan_file.is_file():
            return None
        try:
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            plan_hash = plan.get("batch_plan_sha256")
            unsigned = {key: value for key, value in plan.items() if key != "batch_plan_sha256"}
            calculated = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False,
                                                   sort_keys=True).encode("utf-8")).hexdigest()
            if plan_hash != current_batch.get("batch_plan_sha256") or plan_hash != calculated:
                return None
            live_plan_file = output_dir / ".arc/engineering/current-batch-plan.json"
            if live_plan_file.is_file():
                live_plan = json.loads(live_plan_file.read_text(encoding="utf-8"))
                live_unsigned = {key: value for key, value in live_plan.items()
                                 if key != "batch_plan_sha256"}
                live_calculated = hashlib.sha256(json.dumps(
                    live_unsigned, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                if (live_plan.get("batch_plan_sha256") != plan_hash or
                        live_calculated != plan_hash):
                    return None
        except (OSError, ValueError, TypeError):
            return None
    return {"latest": latest, "manifest": manifest, "checkpoint": checkpoint}


def _checkpoint_candidate(output_dir: Path, identity: dict[str, Any] | None = None,
                          module_ids: list[str] | None = None) -> bool:
    """A candidate is deliverable only when its checkpoint lineage is trusted."""
    return _checkpoint_record(output_dir, identity, module_ids) is not None


def _working_checkpoint_record(output_dir: Path, module_id: str,
                               identity: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Return the newest trusted per-turn checkpoint for one batch."""
    root = output_dir / ".arc" / "codex-recovery" / "turn-checkpoints"
    if not root.is_dir():
        return None
    candidates = sorted((path for path in root.iterdir()
                         if path.is_dir() and path.name.startswith(f"{module_id}-attempt-")),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            manifest = json.loads((path / "checkpoint.json").read_text(encoding="utf-8"))
            source = path / "source"
            if (not isinstance(manifest, dict) or manifest.get("module_id") != module_id or
                    (identity is not None and manifest.get("identity") != identity) or
                    not source.is_dir() or manifest.get("source_sha256") != digest_tree(source)):
                continue
            return {"path": path, "manifest": manifest}
        except (OSError, ValueError):
            continue
    return None


def _snapshot_manifest(source: Path) -> dict[str, str]:
    return {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source.rglob("*") if path.is_file() and not path.is_symlink()}


def restore_working_checkpoint(output_dir: Path, module_id: str,
                               identity: dict[str, Any]) -> dict[str, Any]:
    """Restore a batch working checkpoint when no complete module checkpoint exists."""
    record = _working_checkpoint_record(output_dir, module_id, identity)
    if record is None:
        raise ValueError("No working checkpoint is available for the current batch")
    path = record["path"]
    manifest = record["manifest"]
    with tempfile.TemporaryDirectory(dir=output_dir.parent, prefix="working-checkpoint-restore-") as temporary:
        snapshot = Path(temporary) / "snapshot"
        snapshot.mkdir()
        source = path / "source"
        for relative in _snapshot_manifest(source):
            source_file = source / relative
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
        atomic_json(snapshot / "snapshot-manifest.json", _snapshot_manifest(source))
        failed_source = output_dir / ".arc" / "codex-recovery" / "failed-candidates" / uuid.uuid4().hex
        copy_repair_snapshot(output_dir, failed_source)
        restored = restore_repair_snapshot(snapshot, output_dir)
        engineering_source = path / "engineering"
        for filename in manifest.get("engineering_files", {}):
            source_file = engineering_source / filename
            if source_file.is_file():
                destination = output_dir / ".arc" / "engineering" / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, destination)
        invalidate_dependency_state(output_dir)
    return {"status": "restored", "checkpoint_type": "working",
            "completed_ids": list(manifest.get("completed_ids", [])),
            "current_batch": manifest.get("current_batch"),
            "checkpoint": path.relative_to(output_dir).as_posix(),
            "failed_source": failed_source.relative_to(output_dir).as_posix(),
            "restored_files": restored.get("restored", [])}


def save_run_recovery_marker(output_dir: Path, completed_ids: list[str],
                             current_batch: dict[str, Any], identity: dict[str, Any],
                             *, next_module_id: str | None = None,
                             working_checkpoint: str | None = None,
                             failure: dict[str, Any] | None = None) -> str:
    """Persist run-level continuation state even when no complete checkpoint exists."""
    recovery = output_dir / ".arc" / "codex-recovery"
    marker = {"status": "continuable", "created_at": utc_now(),
              "identity": identity, "completed_ids": list(completed_ids),
              "current_batch": current_batch, "next_module_id": next_module_id,
              "working_checkpoint": working_checkpoint, "failure": failure}
    atomic_json(recovery / "run-recovery.json", marker)
    append_event(output_dir, {"stage": "run_recovery", "status": "marker_saved",
                              "completed_ids": list(completed_ids),
                              "current_batch": current_batch,
                              "next_module_id": next_module_id,
                              "working_checkpoint": working_checkpoint,
                              "failure": failure, "ended_at": utc_now()})
    return (recovery / "run-recovery.json").relative_to(output_dir).as_posix()


def candidate_available(output_dir: Path, baseline_digest: str | None = None,
                        extra_evidence: bool = False, identity: dict[str, Any] | None = None,
                        module_ids: list[str] | None = None) -> bool:
    """Whether real deliverable source exists; never derived from a model claim or a test result."""
    recovery = _recovery_dir(output_dir)
    if _recovery_lineage(output_dir) and not _checkpoint_candidate(output_dir, identity, module_ids):
        # A working checkpoint/run marker records a continuable partial candidate before the
        # first authoritative checkpoint exists.  Once an authoritative pointer exists, a
        # corrupt or foreign lineage remains unavailable and still requires an integrity stop.
        recovery = _recovery_dir(output_dir)
        latest = recovery / "latest.json"
        complete_dir = recovery / "checkpoints"
        marker = recovery / "run-recovery.json"
        has_authoritative_lineage = latest.is_file() or (
            complete_dir.is_dir() and any(path.is_dir() and path.name.startswith("module-")
                                          for path in complete_dir.iterdir()))
        if has_authoritative_lineage or not marker.is_file():
            return False
    if extra_evidence or _checkpoint_candidate(output_dir, identity, module_ids):
        return True
    if baseline_digest is None:
        return False
    try:
        return product_source_digest(output_dir) != baseline_digest
    except OSError:
        return False


def delivery_outcome(candidate: bool, generation_complete: bool) -> dict[str, Any]:
    """Delivery truth table: auxiliary checks decide recorded status, never whether we deliver."""
    return {"status": ("ready" if generation_complete else "partial") if candidate else "unavailable",
            "exit_code": 0 if candidate else 1}


def _repair_snapshot_excluded(relative: Path) -> bool:
    """Product-source scope of the pre-repair snapshot.

    `.codex/skills` is bundled product surface; the rest of `.codex` holds credentials and
    sessions. `.arc`, `module-observations` and build output stay out of scope so that a restore
    can never delete original evidence.
    """
    parts = [part.lower() for part in relative.parts]
    if parts and parts[0] == ".codex":
        # The `.codex` directory itself stays walkable so that `.codex/skills` can be captured.
        return len(parts) > 1 and parts[1] != "skills"
    if "module-observations" in parts:
        return True
    return _excluded_snapshot_path(relative)


def copy_repair_snapshot(source: Path, target: Path) -> dict[str, str]:
    """Copy the pre-repair product source outside the workspace with a path -> hash manifest."""
    target.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {}
    for directory, dirs, files in os.walk(source):
        parent = Path(directory)
        dirs[:] = sorted(item for item in dirs
                         if not _repair_snapshot_excluded((parent / item).relative_to(source))
                         and not (parent / item).is_symlink())
        for filename in sorted(files):
            path = parent / filename
            relative = path.relative_to(source)
            if _repair_snapshot_excluded(relative) or path.is_symlink():
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not manifest:
        raise RuntimeError("Pre-repair source snapshot captured no product file")
    atomic_json(target / "snapshot-manifest.json", manifest)
    return manifest


def restore_repair_snapshot(snapshot: Path, output_dir: Path) -> dict[str, Any]:
    """Restore product source to the snapshot: overwrite changed, re-add deleted, drop added."""
    manifest = json.loads((snapshot / "snapshot-manifest.json").read_text(encoding="utf-8"))
    expected = set(manifest)
    removed: list[str] = []
    for directory, _dirs, files in os.walk(output_dir, topdown=False):
        parent = Path(directory)
        for filename in files:
            path = parent / filename
            relative = path.relative_to(output_dir)
            if _repair_snapshot_excluded(relative) or path.is_symlink():
                continue
            if relative.as_posix() not in expected:
                path.unlink()
                removed.append(relative.as_posix())
        if parent != output_dir and not _repair_snapshot_excluded(parent.relative_to(output_dir)):
            try:
                parent.rmdir()
            except OSError:
                pass
    restored: list[str] = []
    for relative in sorted(manifest):
        source = snapshot / relative
        destination = output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == manifest[relative]:
            continue
        shutil.copy2(source, destination)
        restored.append(relative)
    return {"restored": sorted(restored), "removed": sorted(removed)}


def restore_failed_module(output_dir: Path, module_id: str, identity: dict[str, Any],
                          agent_root: Path) -> dict[str, Any]:
    """Keep a failed draft, then put the last verified source back in the delivered directory."""
    recovery = output_dir / ".arc" / "codex-recovery"
    try:
        complete_error: dict[str, Any] | None = None
        with tempfile.TemporaryDirectory(dir=output_dir.parent, prefix="checkpoint-restore-") as temporary:
            workspace = Path(temporary)
            restored = workspace / "restored"
            restored.mkdir()
            try:
                completed_ids = restore_checkpoint(output_dir, restored, identity, agent_root)
                checkpoint_type = "complete"
            except Exception as exc:
                # A first module may have no authoritative complete checkpoint yet.  Its
                # per-turn working checkpoint is still sufficient to retry this batch and to
                # continue the run from the untouched prefix.
                complete_error = error_evidence(exc)
                authoritative = _recovery_dir(output_dir)
                complete_dir = authoritative / "checkpoints"
                if ((authoritative / "latest.json").is_file() or
                        (complete_dir.is_dir() and any(path.is_dir() and path.name.startswith("module-")
                                                       for path in complete_dir.iterdir()))):
                    return {"status": "unavailable", "warning": complete_error,
                            "checkpoint_type": "complete", "completed_ids": []}
                working = restore_working_checkpoint(output_dir, module_id, identity)
                return {**working, "complete_checkpoint_error": complete_error}
            copy_skills_to_output(agent_root / "skills", restored)
            checkpoint_source = workspace / "checkpoint-source"
            copy_repair_snapshot(restored, checkpoint_source)

            failed_source = recovery / "failed-candidates" / uuid.uuid4().hex
            copy_repair_snapshot(output_dir, failed_source)
            result = {"status": "restored", "checkpoint_type": checkpoint_type,
                      "completed_ids": completed_ids,
                      "checkpoint": json.loads((recovery / "latest.json").read_text(encoding="utf-8"))["checkpoint"],
                      "failed_source": failed_source.relative_to(output_dir).as_posix()}
            try:
                restore_repair_snapshot(checkpoint_source, output_dir)
                expected = json.loads((checkpoint_source / "snapshot-manifest.json").read_text(encoding="utf-8"))
                delivered = {path.relative_to(output_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in output_dir.rglob("*") if path.is_file() and not path.is_symlink() and
                    not _repair_snapshot_excluded(path.relative_to(output_dir))}
                if delivered != expected:
                    raise RuntimeError("Restored product source differs from the verified checkpoint")
                invalidate_dependency_state(output_dir)
            except Exception as error:
                result.update(status="rolled_back", warning=error_evidence(error))
                try:
                    restore_repair_snapshot(failed_source, output_dir)
                    invalidate_dependency_state(output_dir)
                except Exception as rollback_error:
                    result.update(status="indeterminate", rollback_warning=error_evidence(rollback_error))
            if result["status"] == "restored":
                try:
                    engineering.restore_module_state(restored, output_dir, module_id)
                except Exception as metadata_error:
                    result["engineering_warning"] = error_evidence(metadata_error)
            return result
    except Exception as error:
        return {"status": "unavailable", "warning": error_evidence(error)}


def invalidate_dependency_state(output_dir: Path) -> None:
    """Force the next dependency sync to follow the restored package files, not repair artefacts."""
    (output_dir / ".arc" / "codex-recovery" / "installed-dependencies.json").unlink(missing_ok=True)


def _read_implementation(folder: Path) -> Any:
    try:
        return json.loads((folder / "implementation.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def publish_generation(output_dir: Path, modules: list[RequirementModule],
                       summary: dict[str, Any]) -> None:
    """Export delivered generation truth outside runner-private .arc metadata."""
    results = {item["module_id"]: item for item in summary.get("module_results", [])}
    observations = {item["module_id"]: item for item in
                    (summary.get("acceptance") or {}).get("records", [])}
    completed = set(summary.get("completed_ids", []))
    report = {"status": summary["status"], "delivery": summary.get("delivery"),
              "application_sha256": application_digest(output_dir), "modules": [],
              "root_module_order": [module.node_id for module in modules],
              "attempted_root_ids": summary.get("attempted_root_ids", summary.get("attempted_ids", [])),
              "attempted_batch_ids": summary.get("attempted_batch_ids", []),
              "unattempted_root_ids": summary.get("unattempted_root_ids", summary.get("unattempted_ids", [])),
              "completed_batches": summary.get("completed_batches", []),
              "blocked_batches": summary.get("blocked_batches", []),
              "skipped_batches": summary.get("skipped_batches", []),
              "pending_issues": summary.get("pending_issues", []),
              "acceptance_status": (summary.get("acceptance") or {}).get("status", "unavailable"),
              "unverified_count": 0,
              "engineering_memory": engineering_memory_report(output_dir)}
    for module in modules:
        result = results.get(module.node_id, {})
        batch_results = [item for item in (summary.get("module_results") or [])
                         if item.get("module_id") == module.node_id or
                         item.get("module_id", "").startswith(module.node_id + ".part-")]
        batch_observations = [item for item in (summary.get("acceptance") or {}).get("records", [])
                              if item.get("root_module_id", item.get("module_id")) == module.node_id]
        status = "implemented" if module.node_id in completed else (
            result.get("status") if result else "unattempted")
        folder = output_dir / "module-observations" / module.node_id
        inventory: dict[str, Any] = {}
        for observation in batch_observations:
            inventory.update(observation.get("requirements") or {})
        if not inventory:
            inventory = observations.get(module.node_id, {}).get("requirements") or requirement_inventory(
                module, observation_contract(module),
                _read_implementation(folder) if module.node_id in completed else None, None, output_dir)
        inventory = {identifier: dict(item) for identifier, item in inventory.items()}
        for item in inventory.values():
            item["generation_status"] = status
        record = {"module_id": module.node_id, "generation_status": status,
                  "checkpointed": module.node_id in completed,
                  "attempts": sum(item.get("attempts", 0) for item in batch_results) or result.get("attempts", 0),
                  "requirements": inventory, "failure_history": result.get("failure_history", []),
                  "implementation_declaration": "delivered_checkpoint_claim" if module.node_id in completed else
                      "not_delivery_evidence"}
        if result.get("error"):
            record["failure"] = {key: result[key] for key in ("stage", "error", "recovery") if key in result}
        report["modules"].append(record)
        report["unverified_count"] += sum(item["observation_status"] != "observed_passed"
                                           for item in inventory.values())
    summary["unverified_count"] = report["unverified_count"]
    for failure in summary.get("pending_issues", []):
        print("[generation failure] " + json.dumps(_redact_json(failure), ensure_ascii=False), flush=True)
    try:
        atomic_json(output_dir / "module-observations/generation.json", _redact_json(report))
    except Exception as exc:
        print(f"[reporting] generation report write failed: {redact(exc)}", file=sys.stderr)


def atomic_phase_results(record: dict[str, Any], phase: str) -> dict[str, str]:
    observation = record.get(phase) or {}
    identifiers = tuple(record.get("requirements", {}))
    tests = observation.get("tests", [])
    by_id = {item.get("title"): item for item in tests}
    local_error = observation.get("configuration_error_scope") == "test"
    invalid = (record.get("stale") or len(tests) != len(identifiers) or set(by_id) != set(identifiers) or
               any(observation.get(key) for key in ("version_error", "chain_error", "script_change_error")) or
               (observation.get("status") not in {"passed", "failed"} and not local_error) or
               ((observation.get("configuration_error") or observation.get("process_timed_out")) and not local_error))
    results = {}
    for identifier in identifiers:
        item = by_id.get(identifier) or {}
        status = "unverified"
        if not invalid and item.get("executed") and not item.get("configuration_error"):
            if item.get("status") == "expected" and item.get("result_status") == "passed": status = "passed"
            elif item.get("status") == "unexpected" and item.get("result_status") == "failed": status = "failed"
        results[identifier] = status
    return results


def repair_final_candidate(
    codex: Any, output_dir: Path, requirements_dir: Path,
    skills_dir: Path, model: str | None, failures: list[dict[str, Any]],
    runtime_state: dict[str, Any] | None = None
) -> None:
    """One bounded batch in a fresh coding thread; recovery budget belongs to the host."""
    thread = codex.thread_start(cwd=str(output_dir), sandbox=Sandbox.full_access,
                                approval_mode=ApprovalMode.deny_all, model=model,
                                developer_instructions=_developer_instructions(skills_dir))
    prompt = textwrap.dedent(f"""
        Observation repair. Repair only this bounded batch (at most eight atoms).

        Current batch requirement source: {requirements_dir / "requirements.yaml"}

        Failed atoms (each with its source ID, frozen script path, evidence directory and the
        first actionable error):
        ```json
        {json.dumps(failures, ensure_ascii=False, indent=2)}
        ```

        Current batch subtree:
        ```json
        {json.dumps(next((item.get("batch_subtree") for item in failures
                           if isinstance(item, dict) and item.get("batch_subtree") is not None), {}),
                   ensure_ascii=False, indent=2)}
        ```

        Host source/build/backend/observation state:
        ```json
        {json.dumps(runtime_state or {}, ensure_ascii=False, indent=2)}
        ```

        For every failed atom, check the frozen script, the original requirement text, and whether
        the stated prerequisite state actually existed. If a script's assumption contradicts the
        source, say so in the repair note instead of following it. Never create, edit, skip,
        weaken or delete a frozen script, and never change application behavior to satisfy an
        assertion that the source does not require.
        Prioritize real public entry points, data loss, state persistence and shared-field errors.
        Fix the shared cause and its direct callers only; keep unrelated correct behavior working.
        Read the requirement clauses you need from the source path above.

        In this same coding turn, run bounded targeted checks of the repaired user flow using
        the documented formal startup entry and existing tools, including Playwright CLI when
        available. Assert source-required observable state and data, including persistence after
        reload where applicable; HTTP 200 alone is insufficient. Leave already-correct source
        unchanged. Treat a nonzero exit as feedback: make one concentrated fix and recheck once.
        If it still fails, record the confirmed residual failure and affected atom in the repair
        note, then return normally without another repair loop. If a prerequisite, tool or
        check script is missing, broken or timed out, record the affected atom as unverified in the
        repair note separately; do not reinterpret it as an application failure or edit frozen evidence.

        Write module-observations/repair-note.txt naming each changed file, the concrete fix, its
        source requirement and any remaining unverified check. The host independently repeats
        dependency sync, build and startup, then replays frozen observations and reports results.
    """).strip()
    module_ids = {item["module_id"] for item in failures}
    module = next((item for item in load_root_modules(requirements_dir) if item.node_id in module_ids), None)
    if module is None:
        subtree = next((item.get("batch_subtree") for item in failures
                        if isinstance(item, dict) and item.get("batch_subtree") is not None), None)
        module_id = next(iter(module_ids), "current-batch")
        if isinstance(subtree, dict):
            module = RequirementModule(index=1, total=1, node_id=module_id, name=module_id,
                                       subtree=subtree)
    context = engineering_context(requirements_dir, output_dir, module)
    prior = engineering._read(output_dir / "module-observations/before-repair.json")
    prior = {"application_sha256": prior.get("application_sha256"), "atoms": [
        {"module_id": record["module_id"], "id": identifier,
         "result_status": status,
         **({"unverified_reason": redact(json.dumps(record.get("reason") or observation.get("configuration_error") or
                observation.get("error") or next((item.get("configuration_error") or item.get("error")
                    for item in observation.get("tests", []) if item.get("title") == identifier), None) or
                "Observation did not execute validly", ensure_ascii=False))[:400]}
            if status == "unverified" else {})}
        for record in prior.get("records", []) if record.get("module_id") in module_ids
        for observation in [record.get("before") or {}]
        for identifier, status in atomic_phase_results(record, "before").items()]}
    handoff_path = output_dir / ".arc/codex-recovery/repair-handoff.json"
    current = engineering._read(handoff_path)
    atomic_json(handoff_path, {**current, "failures": failures, "prior": prior,
                              "engineering_context": context})
    prompt += ("\nRead the compact host handoff at " + str(handoff_path) +
               ". It includes current file-bound interfaces and this batch's recovery state. "
               "Read actual definitions and consumers before changing shared data; declarations "
               "are only reading aids. A fresh recovery starts from restored valid source; failed "
               "drafts are evidence, never a valid implementation to resume blindly.")
    prompt += ("\n\nPrior observations for this batch only:\n" + json.dumps(prior, ensure_ascii=False))
    _completed_turn(thread, prompt, output_dir, stage="observation_repair", context_reset=True)


def _completed_preflight_authority_matches(checkpoint: dict[str, Any], current: Path,
                                           identity: dict[str, Any], module_ids: list[str],
                                           completed_ids: list[str],
                                           completed_authority_ids: list[str] | None = None) -> bool:
    """Validate only the immutable completed prefix; live unfinished observations may grow."""
    checkpoint_file = checkpoint["checkpoint"] / "engineering/preflight.json"
    try:
        saved = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        live = json.loads(current.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(saved, dict) or not isinstance(live, dict):
        return False
    if saved.get("identity") != identity or live.get("identity") != identity:
        return False
    for key in ("version", "source_sha256", "source_tree_sha256", "module_order", "external_predecessors"):
        if saved.get(key) != live.get(key):
            return False
    if completed_ids != module_ids[:len(completed_ids)]:
        return False
    saved_observations = saved.get("observations", {})
    live_observations = live.get("observations", {})
    for module_id in (completed_authority_ids or completed_ids):
        if saved_observations.get(module_id) != live_observations.get(module_id):
            return False
    return True


def _json_canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def run_final_acceptance(requirements_dir: Path, output_dir: Path, modules: list[RequirementModule],
                         skills_dir: Path, identity: dict[str, Any],
                         completed_ids: list[str] | None = None) -> dict[str, Any]:
    offline = completed_ids is not None
    root_modules = list(modules)
    modules = load_completed_batch_modules(output_dir, root_modules, identity)
    memory_run_id = uuid.uuid4().hex
    runtime = AgentRuntime.from_env(project_dir=str(output_dir))
    if not offline:
        runtime.git.ensure_repo(create_initial_commit=False)
    current_digest = application_digest(output_dir)
    module_ids = [module.node_id for module in root_modules]
    recovery = _recovery_dir(output_dir)
    latest_path = recovery / "latest.json"
    lineage = _recovery_lineage(output_dir)
    checkpoint = _checkpoint_record(output_dir, identity, module_ids)
    integrity_stop: dict[str, Any] | None = None
    completed_authority_ids: list[str] = []
    if lineage:
        if checkpoint is None:
            integrity_stop = {"stage": "checkpoint_integrity",
                              "message": "Checkpoint lineage is missing, corrupt, foreign or non-contiguous; integrity stop"}
            if completed_ids is None:
                completed_ids = []
            offline = True
        else:
            checkpoint_ids = checkpoint["latest"]["completed_ids"]
            completed_authority_ids = list(dict.fromkeys(
                [*checkpoint_ids, *(checkpoint["manifest"].get("completed_batches") or [])]))
            checkpoint_preflight = checkpoint["manifest"].get("engineering_files", {}).get("preflight.json")
            current_preflight = output_dir / ".arc/engineering/preflight.json"
            current_receipt: dict[str, Any] | None = None
            try:
                parsed = json.loads(current_preflight.read_text(encoding="utf-8"))
                if isinstance(parsed, dict) and parsed.get("identity") == identity:
                    current_receipt = parsed
            except (OSError, ValueError):
                current_receipt = None
            checkpoint_preflight_file = checkpoint["checkpoint"] / "engineering/preflight.json"
            checkpoint_preflight_ok = (isinstance(checkpoint_preflight, str) and
                checkpoint_preflight_file.is_file() and
                hashlib.sha256(checkpoint_preflight_file.read_bytes()).hexdigest() == checkpoint_preflight)
            prefix_ok = (current_receipt is not None and checkpoint_preflight_ok and
                         _completed_preflight_authority_matches(
                             checkpoint, current_preflight, identity, module_ids, list(checkpoint_ids),
                             completed_authority_ids))
            if not prefix_ok:
                integrity_stop = {"stage": "preflight_integrity",
                                  "message": "Completed checkpoint prefix authority is missing or changed; integrity stop"}
                completed_ids = list(checkpoint_ids)
                offline = True
    def observation_inputs_digest() -> str:
        digest = hashlib.sha256()
        for module in modules:
            for name in ("contract.json", "checks.spec.js", "frozen-v1.spec.js", "frozen-v2.spec.js"):
                path = output_dir / "module-observations" / module.node_id / name
                digest.update((module.node_id + "/" + name).encode())
                digest.update(path.read_bytes() if path.is_file() else b"missing")
        receipt_file = output_dir / ".arc/engineering/preflight.json"
        if receipt_file.is_file():
            try:
                receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
                if completed_ids is not None:
                    authority_ids = completed_authority_ids or completed_ids
                    receipt["observations"] = {identifier: (receipt.get("observations") or {}).get(identifier)
                                               for identifier in authority_ids}
                digest.update(_json_canonical(receipt))
            except (OSError, ValueError):
                digest.update(b"invalid")
        else:
            digest.update(b"missing")
        return digest.hexdigest()
    acceptance_path = output_dir / "module-observations" / "acceptance.json"
    if acceptance_path.is_file():
        try:
            cached = json.loads(acceptance_path.read_text(encoding="utf-8"))
        except ValueError:
            cached = {}
        if (not offline and cached.get("status") == "passed" and cached.get("identity") == identity and
                cached.get("application_sha256") == current_digest):
            if cached.get("observation_inputs_sha256") == observation_inputs_digest():
                return cached
        shutil.copy2(acceptance_path, acceptance_path.with_name("acceptance-prior-" + uuid.uuid4().hex[:12] + ".json"))
    root_context = load_root_context(requirements_dir)
    model = os.environ.get("MODEL", "").strip() or None
    planning_authority = None if integrity_stop else next((module.planning_authority for module in modules
                               if module.planning_authority is not None), None)
    if planning_authority is None and not integrity_stop:
        planning_authority = engineering.checkpoint_authority(output_dir, identity)
    preflight = (planning_authority if planning_authority is not None else
                 engineering._read(output_dir / ".arc/engineering/preflight.json"))
    source_sha256 = engineering.source_index(requirements_dir)["source_sha256"]
    records: list[dict[str, Any]] = []
    prepared: dict[str, tuple[RequirementModule, dict[str, Any]]] = {}
    technical_failure: dict[str, Any] | None = None
    stopped: dict[str, Any] | None = integrity_stop or preflight.get('host_integrity_stop') or (
        {'stage': 'script_correction_pending', 'message':
         'Correction terminal state is not checkpointed; no later model requests'}
        if preflight.get('script_correction_pending') else None)
    baseline_digest = current_digest
    stage = "planning"
    repair_outcome: dict[str, Any] = {"attempted": False, "kept": False, "restored": False,
                                      "failed_atoms": [], "batches": [], "model_turns": 0,
                                      "recovery_attempts": preflight.get("compaction_recovery_used", 0),
                                      "unprocessed_atoms": [],
                                      "script_correction_turns": preflight.get("script_corrections_used", 0),
                                      "selection": "unchanged"}
    baseline_snapshot: Path | None = None

    def unverified(module: RequirementModule, reason: dict[str, Any]) -> dict[str, Any]:
        contract = {"requirements": [{"id": item["id"]} for item in
                                     _atomic_requirement_index(module.subtree)], "chains": []}
        return {"module_id": module.node_id, "status": "error", "initial_status": "error",
                "repair_attempted": False, "before": None, "after": None,
                "chain_titles": [item["id"] for item in contract["requirements"]],
                "evidence_paths": [], "reason": reason,
                "requirements": requirement_inventory(module, contract, None, None, output_dir)}

    def collect_failures() -> list[dict[str, Any]]:
        """Only an executed failure in a valid run is repair evidence.

        A check error (script, configuration, missing or unexecuted step, timeout) is not a
        business failure. Only valid failed atomic runs feed repair, even when a sibling
        check error makes the aggregate observation an `error`.
        """
        failures: list[dict[str, Any]] = []
        for record in records:
            before = record.get("before")
            if not isinstance(before, dict):
                continue
            module_id = record["module_id"]
            valid_titles = {chain["title"] for chain in prepared[module_id][1]["chains"]}
            for observation in before.get("atomic_results") or [before]:
                if observation.get("status") != "failed":
                    continue
                for test in observation.get("tests", []):
                    if (test.get("configuration_error") or test.get("status") != "unexpected"
                            or not test.get("executed") or test.get("title") not in valid_titles):
                        continue
                    failures.append({
                        "module_id": module_id,
                        "atomic_id": test["title"],
                        "requirement_file": str((requirements_dir / "requirements.yaml").resolve()),
                        "frozen_script": str((output_dir / "module-observations" / module_id /
                                              "checks.spec.js").resolve()),
                        "evidence_path": str((output_dir / str(observation.get("evidence_path", ""))).resolve()),
                        "error_excerpt": redact(test.get("error") or "Assertion failed")[:800],
                    })
        return failures

    try:
        with (nullcontext(None) if offline else _sdk_session(_codex_config(CodexConfig))) as codex:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if api_key and not offline:
                codex.login_api_key(api_key)
            # Pass one: plan, freeze and observe every module. Nothing modifies the application here.
            for module in modules:
                root_id = module.root_module_id or module.node_id
                if offline and root_id not in completed_ids:
                    records.append(unverified(module, {"stage": "generation",
                        "message": "Module has no completed delivered checkpoint; not independently observed"}))
                    continue
                if stopped:
                    records.append(unverified(module, stopped))
                    continue
                folder = output_dir / "module-observations" / module.node_id
                stage = "planning"
                try:
                    emit_runtime_event(runtime.events.mark_design_started, output_dir, module.node_id)
                    authority = module.planning_authority or planning_authority
                    cached_plan = engineering.observation_from_authority(
                        output_dir, module.node_id, identity, source_sha256, authority)
                    if cached_plan is not None:
                        contract, script = cached_plan
                    elif preflight or offline:
                        reason = preflight.get("observations", {}).get(module.node_id, {})
                        raise ValueError("Early frozen observation unavailable or stale; no late model replan: " +
                                         json.dumps(reason, ensure_ascii=False))
                    else:
                        # Standalone acceptance of legacy outputs keeps its original source-only planner.
                        contract, script = plan_module_observation(
                            codex, module, requirements_dir, output_dir, root_context, model)
                    contract = observation_contract(module)
                    atomic_json(folder / "contract.json", contract)
                    frozen = replace(module, frozen_script=script,
                                      chain_titles=tuple(chain["title"] for chain in contract["chains"]),
                                      frozen_version=authority.get("observations", {}).get(module.node_id, {}).get(
                                          "version", 1) if authority else 1)
                    prepared[module.node_id] = (frozen, contract)
                    emit_runtime_event(runtime.events.mark_design_done, output_dir, module.node_id)
                except Exception as exc:
                    reason = {"module_id": module.node_id, "stage": stage, "error": error_evidence(exc)}
                    atomic_json(folder / "planning-failure.json", reason)
                    emit_runtime_event(runtime.events.mark_design_failed, output_dir, module.node_id)
                    append_event(output_dir, {**reason, "status": "failed", "ended_at": utc_now()})
                    records.append(unverified(module, reason))
                    if model_failure_kind(exc.error if isinstance(exc, ModuleResultError) else exc) == "blocked":
                        stopped = reason
                    continue
                stage = "observation"
                try:
                    print(f"[observation] Final {module.node_id} started", flush=True)
                    before = run_module_observation(output_dir, frozen, "before")
                    if (not offline and authority and correct_observation_script(
                            codex, module, requirements_dir, output_dir, preflight, before, model, modules)):
                        updated = engineering.observation_from_authority(
                            output_dir, module.node_id, identity, source_sha256, preflight)
                        if updated is None:
                            raise ValueError("Corrected observation authority is unavailable")
                        frozen = replace(frozen, frozen_script=updated[1], frozen_version=2)
                        prepared[module.node_id] = (frozen, contract)
                        before = run_module_observation(output_dir, frozen, "before")
                    repair_outcome["script_correction_turns"] = preflight.get("script_corrections_used", 0)
                    print(f"[observation] Final {module.node_id}: {before['status']}", flush=True)
                except Exception as exc:
                    error = exc.error if isinstance(exc, ModuleResultError) else exc
                    reason = {"stage": stage, "error": error_evidence(error)}
                    integrity = getattr(exc, 'host_integrity_stop', None)
                    if integrity:
                        reason['host_integrity_stop'] = integrity
                    if preflight.get('script_correction_pending'):
                        reason['script_correction_pending'] = True
                    records.append(unverified(module, reason))
                    if integrity or preflight.get('script_correction_pending') or model_failure_kind(error) == "blocked":
                        stopped = reason
                    continue
                record = _observation_record(frozen, before, None, contract)
                record["requirements"] = requirement_inventory(
                    frozen, contract, _read_implementation(folder), before, output_dir)
                record["observed_application_sha256"] = application_digest(output_dir)
                records.append(record)

            # Pass two: one repair pass, with small module batches and one compaction recovery.
            stage = "observation"
            failures = collect_failures()
            if failures and not offline:
                baseline_snapshot = (output_dir / ".arc/codex-recovery/repair-baselines" /
                                     uuid.uuid4().hex[:12])
                copy_repair_snapshot(output_dir, baseline_snapshot)
                repair_outcome["baseline_path"] = str(baseline_snapshot)
            atomic_json(output_dir / "module-observations/before-repair.json",
                        {"application_sha256": application_digest(output_dir), "records": records})
            repair_outcome["failed_atoms"] = [item["atomic_id"] for item in failures]
            early_blocked = (preflight.get('host_integrity_stop') or preflight.get('model_blocked_reason') or
                             preflight.get("contract", {}).get("reason")) or next((entry.get("reason") for entry in preflight.get("observations", {}).values()
                if model_failure_kind((entry.get("reason") or {}).get("error", {})) == "blocked"), None)
            repair_blocked = stopped or early_blocked or (
                {"stage": "generation", "message": "Partial generation acceptance is observation-only"}
                if offline else None)
            if repair_blocked:
                repair_outcome["blocked_reason"] = _stable_blocked_reason(repair_blocked, "observation_repair")
            normal_budget = max(0, 8 - preflight.get("script_corrections_used", 0)
                                   - preflight.get("normal_repairs_used", 0))
            if failures and not repair_blocked and not normal_budget:
                repair_outcome["blocked_reason"] = _stable_blocked_reason(
                    {"stage": "observation_repair", "message": "Normal repair budget exhausted"},
                    "observation_repair")
                repair_outcome["unprocessed_atoms"] = [item["atomic_id"] for item in failures]
            if failures and not repair_blocked and normal_budget:
                repair_outcome["attempted"] = True
                groups: dict[str, list[dict[str, Any]]] = {}
                for item in failures:
                    groups.setdefault(item["module_id"], []).append(item)
                batches = [items[index:index + 8] for items in groups.values()
                           for index in range(0, len(items), 8)]
                processed = set()
                for number, batch in enumerate(batches[:normal_budget], 1):
                    outcome = {"batch": number, "module_id": batch[0]["module_id"],
                               "atoms": [item["atomic_id"] for item in batch],
                               "status": "unverified", "attempts": 0, "failures": [],
                               "working_checkpoints": []}
                    repair_outcome["batches"].append(outcome)
                    snapshot: Path | None = None
                    restored = False
                    stop = False
                    try:
                        stage = "repair_snapshot"
                        snapshot = Path(tempfile.mkdtemp(prefix="arc-repair-snapshot-"))
                        copy_repair_snapshot(output_dir, snapshot)
                        for attempt in (1, 2):
                            last_failure = outcome["failures"][-1] if outcome["failures"] else None
                            if (attempt == 2 and preflight.get("compaction_recovery_used", 0) and
                                    last_failure and is_remote_compaction_error(last_failure.get("error", {}))):
                                break
                            atomic_json(output_dir / ".arc/codex-recovery/repair-handoff.json", {
                                "batch": number, "recovery": attempt == 2,
                                "completed_batches": [item for item in repair_outcome["batches"]
                                                      if item["status"] == "runtime_valid"],
                                "previous_failure": outcome["failures"][-1] if outcome["failures"] else None})
                            try:
                                stage = "observation_repair"
                                restored = False
                                try:
                                    repair_source = product_source_digest(output_dir)
                                except OSError:
                                    repair_source = source_sha256
                                repair_source += ":" + hashlib.sha256(
                                    json.dumps(sorted(outcome["atoms"]), ensure_ascii=False).encode("utf-8")
                                ).hexdigest()
                                previous_error = (outcome["failures"][-1].get("error")
                                                  if outcome["failures"] else None)
                                if previous_error is None:
                                    previous_error = outcome.get("memory_error") or {
                                        "message": batch[0].get("error_excerpt") or "Assertion failed"}
                                if attempt == 1 and previous_error and should_skip_engineering_request(
                                        output_dir, repair_source, outcome["module_id"], memory_run_id,
                                        previous_error, include_candidate=True):
                                    duplicate = {"module_id": outcome["module_id"], "batch": number,
                                                 "stage": stage, "error": previous_error,
                                                 "duplicate_model_request": True,
                                                 "message": "duplicate root cause already known; model request skipped"}
                                    outcome["failures"].append(duplicate)
                                    outcome["status"] = "unverified"
                                    repair_outcome["blocked_reason"] = _stable_blocked_reason(
                                        duplicate, "observation_repair")
                                    append_event(output_dir, {**duplicate, "status": "skipped",
                                                              "ended_at": utc_now()})
                                    stop = True
                                    break
                                if attempt == 1:
                                    preflight["normal_repairs_used"] = preflight.get("normal_repairs_used", 0) + 1
                                elif previous_error and is_remote_compaction_error(previous_error):
                                    preflight["compaction_recovery_used"] = 1
                                atomic_json(output_dir / ".arc/engineering/preflight.json", preflight)
                                save_checkpoint(output_dir, [module.node_id for module in modules], identity,
                                                {"status": "not_rechecked", "purpose": "repair_budget"})
                                outcome["attempts"] += 1
                                outcome["memory_source"] = repair_source
                                repair_outcome["model_turns"] += 1
                                processed.update(outcome["atoms"])
                                outcome["memory_error"] = previous_error
                                remember_engineering_memory(
                                    output_dir, repair_source, outcome["module_id"], memory_run_id,
                                    previous_error, "candidate",
                                    {"phase": "repair", "source_stage": "observation_repair",
                                     "error_kind": "observation_failure",
                                     "message": previous_error.get("message", "")
                                     if isinstance(previous_error, dict) else str(previous_error),
                                     "requirement_ids": outcome["atoms"]})
                                append_event(output_dir, {"module_id": outcome["module_id"],
                                                          "batch": number, "stage": "observation_repair",
                                                          "status": "context_reset", "attempt": attempt,
                                                          "fresh_session": True, "checkpoint_available": True,
                                                          "prompt_mode": "module_atoms_and_changed_files",
                                                          "started_at": utc_now()})
                                attempt_checkpoint = save_working_checkpoint(
                                    output_dir, outcome["module_id"], attempt, identity,
                                    "pre_observation_repair_attempt")
                                outcome["working_checkpoints"].append(attempt_checkpoint)
                                append_event(output_dir, {"module_id": outcome["module_id"],
                                                          "batch": number, "stage": "observation_repair",
                                                          "status": "working_checkpoint",
                                                          "path": attempt_checkpoint, "attempt": attempt,
                                                          "phase": "before_turn", "ended_at": utc_now()})
                                repair_failures = list(batch)
                                if previous_error and attempt > 1:
                                    repair_failures[0] = {**repair_failures[0],
                                                          "repair_retry_error": previous_error}
                                if repair_failures:
                                    repair_failures[0] = {**repair_failures[0],
                                                          "host_state": {"source": repair_source,
                                                                          "build": "pending",
                                                                          "backend": "pending",
                                                                          "observation": "failed"},
                                                          "batch_subtree": prepared.get(
                                                              outcome["module_id"], (None, None))[0].subtree
                                                          if prepared.get(outcome["module_id"]) else None}
                                repair_final_candidate(codex, output_dir, requirements_dir, skills_dir,
                                                       model, repair_failures)
                                working_checkpoint = save_working_checkpoint(
                                    output_dir, outcome["module_id"], attempt, identity,
                                    "post_observation_repair_turn")
                                outcome["working_checkpoints"].append(working_checkpoint)
                                append_event(output_dir, {"module_id": outcome["module_id"],
                                                          "batch": number, "stage": "observation_repair",
                                                          "status": "working_checkpoint",
                                                          "path": working_checkpoint,
                                                          "attempt": attempt, "ended_at": utc_now()})
                                stage = "observation_repair_build"
                                if build_frontend(output_dir).get("status") != "passed":
                                    raise RuntimeError("frontend build after observation repair did not pass")
                                stage = "observation_repair_backend"
                                if verify_backend_startup(output_dir).get("status") != "passed":
                                    raise RuntimeError("backend startup after observation repair did not pass")
                                stage = "observation_repair_observation"
                                repaired_module = prepared.get(outcome["module_id"], (None, None))[0]
                                if repaired_module is None:
                                    raise RuntimeError("repaired batch observation authority is unavailable")
                                repaired_observation = run_module_observation(
                                    output_dir, repaired_module, "after")
                                outcome["observation"] = repaired_observation
                                observed_by_id = {item.get("title"): item for item in
                                                  repaired_observation.get("tests", [])}
                                batch_passed = all(
                                    observed_by_id.get(identifier, {}).get("status") == "expected" and
                                    observed_by_id.get(identifier, {}).get("executed") and
                                    observed_by_id.get(identifier, {}).get("result_status") == "passed"
                                    for identifier in outcome["atoms"])
                                if not batch_passed:
                                    raise RuntimeError(
                                        "observation after repair did not pass for current batch: " +
                                        redact(json.dumps(repaired_observation, ensure_ascii=False))[:1200])
                                repair_outcome["kept"] = True
                                repair_outcome["selection"] = "candidate_pending_replay"
                                candidate_snapshot = (output_dir / ".arc/codex-recovery/repair-candidates" /
                                                       uuid.uuid4().hex[:12])
                                copy_repair_snapshot(output_dir, candidate_snapshot)
                                repair_outcome["repaired_path"] = str(candidate_snapshot)
                                outcome["status"] = "runtime_valid"
                                break
                            except Exception as exc:
                                error = exc.error if isinstance(exc, ModuleResultError) else exc
                                failure = {"module_id": outcome["module_id"], "batch": number,
                                           "stage": stage, "error": error_evidence(error)}
                                outcome["failures"].append(failure)
                                if stage == "observation_repair":
                                    remember_engineering_memory(
                                        output_dir, repair_source, outcome["module_id"], memory_run_id,
                                        failure["error"], "candidate",
                                        {"phase": "repair", "source_stage": stage,
                                         "error_kind": model_failure_kind(error) or "error",
                                         "message": failure["error"].get("message", ""),
                                         "requirement_ids": outcome["atoms"]})
                                blocked = stage == "observation_repair" and model_failure_kind(error) == "blocked"
                                compact = (stage == "observation_repair" and not blocked and
                                           is_remote_compaction_error(failure["error"]))
                                failure["failure_class"] = ("remote_compaction" if compact else
                                                              ("transient" if model_failure_kind(error) == "transient"
                                                               else "repair_error"))
                                append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
                                if compact:
                                    draft = output_dir / ".arc/codex-recovery/repair-drafts" / f"batch-{number}-attempt-{attempt}"
                                    try:
                                        copy_repair_snapshot(output_dir, draft)
                                        atomic_json(draft / "failure.json", failure)
                                        failure["draft_path"] = str(draft)
                                    except Exception as draft_error:
                                        failure["draft_warning"] = error_evidence(draft_error)
                                stage = "repair_restore"
                                append_event(output_dir, {"module_id": outcome["module_id"],
                                                          "batch": number, "stage": stage,
                                                          "status": "started", "attempt": attempt,
                                                          "checkpoint": (outcome["working_checkpoints"][-1]
                                                                         if outcome["working_checkpoints"] else str(snapshot)),
                                                          "error": failure["error"], "started_at": utc_now()})
                                restore_repair_snapshot(snapshot, output_dir)
                                restored = True
                                repair_outcome["restored"] = True
                                invalidate_dependency_state(output_dir)
                                stage = "restored_runtime"
                                if build_frontend(output_dir).get("status") != "passed":
                                    raise RuntimeError("restored frontend build did not pass")
                                if verify_backend_startup(output_dir).get("status") != "passed":
                                    raise RuntimeError("restored backend startup did not pass")
                                append_event(output_dir, {"module_id": outcome["module_id"],
                                                          "batch": number, "stage": stage,
                                                          "status": "restored", "attempt": attempt,
                                                          "checkpoint": (outcome["working_checkpoints"][-1]
                                                                         if outcome["working_checkpoints"] else str(snapshot)),
                                                          "error": failure["error"], "ended_at": utc_now()})
                                retryable = model_failure_kind(error) == "transient"
                                if compact and not repair_outcome["recovery_attempts"]:
                                    repair_outcome["recovery_attempts"] += 1
                                    continue
                                if retryable and attempt == 1 and not compact:
                                    continue
                                if compact:
                                    repair_outcome.setdefault("transport_failures", []).append(failure)
                                else:
                                    technical_failure = failure
                                outcome["status"] = "interrupted"
                                if blocked:
                                    repair_outcome["blocked_reason"] = _stable_blocked_reason(
                                        failure, "observation_repair")
                                stop = blocked
                                break
                    except Exception as exc:
                        technical_failure = {"module_id": outcome["module_id"], "batch": number,
                                             "stage": stage, "error": error_evidence(exc)}
                        outcome["failures"].append(technical_failure)
                        outcome["status"] = "unverified"
                        append_event(output_dir, {**technical_failure, "status": "failed", "ended_at": utc_now()})
                        if stage == "repair_snapshot": repair_outcome["snapshot_failure"] = error_evidence(exc)
                        stop = True
                    finally:
                        if snapshot is None:
                            pass
                        elif outcome["status"] == "runtime_valid" or restored or stage == "repair_snapshot":
                            shutil.rmtree(snapshot, ignore_errors=True)
                        else:
                            # An unsuccessful restore retains its only recovery source.
                            outcome["snapshot_path"] = str(snapshot)
                    if stop: break
                repair_outcome["unprocessed_atoms"] = [item["atomic_id"] for item in failures
                                                         if item["atomic_id"] not in processed]

            # Only a kept repair that actually changed deliverable source needs one frozen replay.
            replay_digest = application_digest(output_dir)
            replay_regressed = False
            replay_unverified = False
            if repair_outcome["kept"] and replay_digest != baseline_digest:
                for record in records:
                    if record["module_id"] not in prepared:
                        continue
                    frozen, contract = prepared[record["module_id"]]
                    replay = run_module_observation(output_dir, frozen, "after")
                    record["observed_application_sha256"] = application_digest(output_dir)
                    if record["observed_application_sha256"] != replay_digest:
                        replay["status"] = "error"
                        replay["version_error"] = "Application changed during repair replay"
                    if (len(replay.get("tests", [])) != len(frozen.chain_titles) or
                            {item.get("title") for item in replay.get("tests", [])} !=
                            set(frozen.chain_titles)):
                        replay["status"] = "error"
                        replay["chain_error"] = "Repair replay did not cover every original operation chain"
                    delivered = output_dir / "module-observations" / record["module_id"] / "checks.spec.js"
                    if (frozen.frozen_script is not None and
                            (not delivered.is_file() or delivered.read_bytes() != frozen.frozen_script)):
                        replay["status"] = "error"
                        replay["script_change_error"] = "Implementation changed a frozen observation script"
                    record["after"] = replay
                    if (replay.get("status") == "error" or replay.get("configuration_error") or
                            replay.get("process_timed_out") or any(
                                test.get("configuration_error") or test.get("error_origin") not in (None, "application") or
                                not test.get("executed", True)
                                for test in replay.get("tests", []))):
                        replay_unverified = True
                    if record.get("before", {}).get("status") == "passed" and replay.get("status") == "failed":
                        replay_regressed = True
                    record["repair_attempted"] = True
                    record["status"] = replay["status"]
                    record["evidence_paths"] = [*(record.get("evidence_paths") or []),
                                                replay["evidence_path"]]
                    record["requirements"] = requirement_inventory(
                        frozen, contract, None, replay, output_dir)
            if replay_unverified:
                repair_outcome["kept"] = False
                repair_outcome["selection"] = "unverified"
            elif repair_outcome["kept"] and not replay_regressed:
                for outcome in repair_outcome["batches"]:
                    if outcome.get("status") != "runtime_valid":
                        continue
                    original_failure = (outcome["failures"][-1] if outcome["failures"]
                                        else {"error": outcome.get("memory_error")})
                    remember_engineering_memory(
                        output_dir, outcome.get("memory_source", replay_digest), outcome["module_id"], memory_run_id,
                        original_failure.get("error"), "verified",
                        {"phase": "repair", "source_stage": "replay",
                         "requirement_ids": outcome.get("atoms", []),
                         "root_cause": (original_failure.get("error") or {}).get("message", ""),
                         "repair": "bounded observation repair followed by frozen replay",
                         "verification_evidence": [record.get("evidence_paths", [])
                                                   for record in records if record.get("after")]})
            elif replay_regressed and baseline_snapshot is not None:
                try:
                    restore_repair_snapshot(baseline_snapshot, output_dir)
                    repair_outcome["kept"] = False
                    repair_outcome["restored"] = True
                    repair_outcome["replay_regressed"] = True
                    repair_outcome["selection"] = "baseline"
                except Exception as exc:
                    repair_outcome["replay_restore_error"] = error_evidence(exc)
                    repair_outcome["kept"] = False
                    repair_outcome["selection"] = "unverified"
    except Exception as exc:
        reason = {"stage": stage, "error": error_evidence(exc)}
        if stage == "planning":
            stopped = reason
        else:
            technical_failure = reason
        recorded = {record["module_id"] for record in records}
        for module in modules:
            if module.node_id in recorded:
                continue
            atomic_json(output_dir / "module-observations" / module.node_id / "planning-failure.json",
                        {"module_id": module.node_id, **reason})
            records.append(unverified(module, reason))

    final_digest = application_digest(output_dir)
    issues = []
    unverified_count = 0
    counts = {"passed": 0, "failed": 0, "unverified": 0}
    for record in records:
        if (record.get("observed_application_sha256") and
                record["observed_application_sha256"] != final_digest):
            record["status"] = "error"
            record["stale"] = True
            if record["module_id"] in prepared:
                frozen, contract = prepared[record["module_id"]]
                record["requirements"] = requirement_inventory(frozen, contract, None, None, output_dir)
        record["application_sha256"] = final_digest
        inventory = record.get("requirements", {})
        unverified_count += sum(item["observation_status"] != "observed_passed" for item in inventory.values())
        if not inventory:
            unverified_count += 1
        for identifier, item in inventory.items():
            if item["observation_status"] == "observed_passed":
                emit_runtime_event(runtime.events.mark_test_passed, output_dir, identifier)
            elif not offline or record["module_id"] in completed_ids:
                emit_runtime_event(runtime.traceability.upsert_node_state, output_dir, identifier, "IMPLEMENTED", "implement")
        observed = record.get("final") or record.get("after") or record.get("before") or {}
        for test in observed.get("tests", []):
            key = "passed" if test.get("status") == "expected" and test.get("executed") and test.get("result_status") == "passed" else (
                "failed" if test.get("status") == "unexpected" and test.get("executed", True) else "unverified")
            if (test.get("configuration_error") or (record["status"] == "error" and (
                    observed.get("configuration_error_scope") != "test" or
                    record.get("stale") or record.get("unresolved")))):
                key = "unverified"
            counts[key] += 1
        counts["unverified"] += max(0, len(record.get("chain_titles", [])) - len(observed.get("tests", [])))
        if record["status"] == "failed":
            emit_runtime_event(runtime.events.mark_test_failed, output_dir, record["module_id"])
        elif record["status"] == "passed" and inventory and all(
                item["observation_status"] == "observed_passed" for item in inventory.values()):
            emit_runtime_event(runtime.events.mark_test_passed, output_dir, record["module_id"])
        elif not offline or record["module_id"] in completed_ids:
            emit_runtime_event(runtime.traceability.upsert_node_state, output_dir, record["module_id"], "IMPLEMENTED", "implement")
        if record["status"] != "passed" or any(item["observation_status"] != "observed_passed"
                                              for item in inventory.values()):
            issues.append({"module_id": record["module_id"], "status": record["status"],
                           "chain_titles": record.get("chain_titles", []), "evidence_paths": record.get("evidence_paths", []),
                           "reason": record.get("reason") or record.get("unresolved")})
        atomic_json(output_dir / "module-observations" / record["module_id"] / "requirement-status.json", inventory)
    status = "failed" if counts["failed"] else ("unverified" if technical_failure or unverified_count or
             any(record["status"] == "error" for record in records) else "passed")
    acceptance = {"status": status, "application_sha256": final_digest, "identity": identity,
                  "observation_inputs_sha256": observation_inputs_digest(),
                  "records": records, "issues": issues, "unverified_count": unverified_count,
                  "chain_counts": counts, "technical_failure": technical_failure,
                  "repair": repair_outcome}
    if integrity_stop:
        acceptance["integrity_stop"] = integrity_stop
    if offline:
        acceptance.update(mode="observation_only", completed_ids=list(completed_ids),
                          module_scope=[module.node_id for module in modules])
    acceptance.update(passed_chains=counts["passed"], failed_chains=counts["failed"],
        unverified_ids=sorted({identifier for record in records for identifier, item in
            record.get("requirements", {}).items() if item["observation_status"] != "observed_passed"}))
    acceptance["atomic_changes"] = []
    for record in records:
        before = atomic_phase_results(record, "before")
        after = atomic_phase_results(record, "after") if record.get("after") else before
        for identifier in sorted(set(before) | set(after)):
            initial, final = before.get(identifier, "unverified"), after.get(identifier, "unverified")
            acceptance["atomic_changes"].append({"id": identifier, "before": initial, "after": final,
                "change": "unverified" if final == "unverified" else (
                    "regressed" if initial == "passed" and final == "failed" else (
                    "improved" if initial != "passed" and final == "passed" else "unchanged"))})
    repair_outcome["remaining_failed_atoms"] = [item["id"] for item in acceptance["atomic_changes"]
                                                if item["after"] == "failed"]
    repair_outcome["unverified_atoms"] = [item["id"] for item in acceptance["atomic_changes"]
                                          if item["after"] == "unverified"]
    _save_observation_records(output_dir, records, issues)
    atomic_json(acceptance_path, acceptance)
    return acceptance


def main() -> int:
    generation_started = time.monotonic()
    args = parse_args()
    requirements_dir = Path(args.requirement_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if not requirements_dir.is_dir():
        raise FileNotFoundError(f"Requirement directory not found: {requirements_dir}")

    agent_root = Path(__file__).resolve().parent
    modules = load_root_modules(requirements_dir)
    identity = input_identity(requirements_dir, agent_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    resume_from = Path(args.resume_from).expanduser().resolve() if args.resume_from else None
    delivery_warnings: list[dict[str, Any]] = []
    baseline_digest: str | None = None
    prepared_input = False
    verified_prior_stage = False
    resume_commit_failed = False
    resume_integrity_failure: dict[str, Any] | None = None
    resume_batch_state: dict[str, Any] | None = None
    if resume_from:
        try:
            completed_ids = restore_checkpoint(resume_from, output_dir, identity, agent_root,
                                               [module.node_id for module in modules])
        except Exception as exc:
            resume_integrity_failure = {"stage": "checkpoint_integrity", "error": error_evidence(exc),
                                        "message": "Recovery checkpoint is unavailable or untrusted; integrity stop"}
            completed_ids = []
            append_event(output_dir, {**resume_integrity_failure, "status": "failed", "ended_at": utc_now()})
        if resume_integrity_failure:
            summary = {"status": "blocked", "completed_ids": [], "pending_issues": [resume_integrity_failure],
                       "observation_issues": [], "unattempted_ids": [module.node_id for module in modules],
                       "module_results": [{"module_id": module.node_id, "status": "unverified",
                                           "reason": resume_integrity_failure} for module in modules],
                       "delivery_warnings": []}
        else:
            prepared_input = True
            try:
                resume_batch_state = json.loads(
                    (output_dir / ".arc/codex-recovery/latest.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                resume_batch_state = None
            authority = engineering.checkpoint_authority(resume_from, identity)
            if authority:
                modules = [replace(module, planning_authority=authority) for module in modules]
            if completed_ids != [module.node_id for module in modules[:len(completed_ids)]]:
                raise ValueError("Checkpoint completed modules do not match ROOT order")
            try:
                commit_module(AgentRuntime.from_env(project_dir=str(output_dir)), "recovery-baseline",
                              output_dir, "restored runnable baseline; acceptance pending")
            except Exception as exc:
                # An auxiliary Git failure never withholds the restored candidate.
                failure = {"module_id": "recovery-baseline", "stage": "commit", "error": error_evidence(exc)}
                append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
                summary = {"status": "partial", "completed_ids": completed_ids, "pending_issues": [failure],
                           "observation_issues": [],
                           "unattempted_ids": [module.node_id for module in modules
                                               if module.node_id not in completed_ids],
                           "module_results": [{**failure, "status": "commit_failed"}],
                           "delivery_warnings": [failure]}
                summary["delivery"] = delivery_outcome(
                    candidate_available(output_dir, identity=identity,
                                       module_ids=[module.node_id for module in modules]), False)
                resume_commit_failed = True
            for label, action in (("resume_build", build_frontend), ("resume_backend", verify_backend_startup)):
                try:
                    action(output_dir)
                except Exception as exc:
                    delivery_warnings.append({"module_id": "recovery-baseline", "stage": label,
                                              "error": error_evidence(exc)})
    else:
        frontend = (output_dir / "frontend").exists()
        backend = (output_dir / "backend").exists()
        if frontend != backend:
            raise ValueError("Output contains a partial application")
        prepared_input = frontend and backend
        if not frontend:
            copy_template_contents(agent_root / "template", output_dir)
        completed_ids = []
        baseline_digest = product_source_digest(output_dir)
    skills_dir = copy_skills_to_output(agent_root / "skills", output_dir)
    verified_prior_stage = engineering.checkpoint_authority(output_dir, identity) is not None
    if resume_integrity_failure:
        pass
    elif resume_commit_failed:
        pass  # Keep the verified baseline; proceed only to offline observation and reports.
    elif completed_ids != [module.node_id for module in modules]:
        summary = implement_modules(requirements_dir, output_dir, modules, skills_dir, identity, completed_ids,
                                    prepared_input=prepared_input,
                                    verified_prior_stage=verified_prior_stage,
                                    resume_batch_state=resume_batch_state)
    else:
        summary = {"status": "complete", "completed_ids": completed_ids,
                   "pending_issues": [], "observation_issues": [],
                   "unattempted_ids": [],
                   "module_results": [{"module_id": module.node_id, "status": "completed",
                                       "observation": None} for module in modules]}
    summary.setdefault("timing", {"first_coding_seconds": None})["generation_seconds"] = round(time.monotonic() - generation_started, 3)
    summary.setdefault("usage", None)
    summary.setdefault("completed_batches", [])
    summary.setdefault("blocked_batches", [])
    summary.setdefault("skipped_batches", [])
    summary.setdefault("root_module_order", [module.node_id for module in modules])
    summary.setdefault("attempted_root_ids", summary.get("attempted_ids", []))
    summary.setdefault("attempted_batch_ids", [])
    summary.setdefault("unattempted_root_ids", summary.get("unattempted_ids", []))
    if summary["status"] == "complete" or summary["completed_ids"]:
        try:
            options = {} if summary["status"] == "complete" else {"completed_ids": summary["completed_ids"]}
            acceptance = run_final_acceptance(requirements_dir, output_dir, modules, skills_dir, identity, **options)
        except Exception as exc:
            failure = {"stage": "acceptance", "error": error_evidence(exc)}
            append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
            delivery_warnings.append(failure)
            records = [{"module_id": module.node_id, "status": "error", "reason": failure,
                        "requirements": requirement_inventory(module, observation_contract(module), None, None, output_dir)}
                       for module in modules]
            identifiers = sorted(identifier for record in records for identifier in record["requirements"])
            acceptance = {"status": "unverified", "records": records, "issues": [failure],
                          "unverified_count": len(identifiers), "unverified_ids": identifiers,
                          "chain_counts": {"passed": 0, "failed": 0, "unverified": len(identifiers)},
                          "technical_failure": failure, "application_sha256": None,
                          "repair": {"attempted": False, "kept": False, "restored": False}}
        summary["acceptance"] = acceptance
        summary["observation_issues"] = acceptance.get("issues", [])
        by_id = {record["module_id"]: record for record in acceptance.get("records", [])}
        for item in summary["module_results"]:
            item["observation"] = by_id.get(item["module_id"])
        if acceptance.get("technical_failure"):
            summary["pending_issues"].append(acceptance["technical_failure"])
        if acceptance.get("integrity_stop"):
            summary["pending_issues"].append(acceptance["integrity_stop"])
            summary["status"] = "blocked"
    if summary["status"] == "complete":
        stage = "final_build"
        try:
            build = build_frontend(output_dir)
            if build.get("status") != "passed":
                raise RuntimeError("final frontend build did not pass")
            stage = "final_backend"
            if verify_backend_startup(output_dir).get("status") != "passed":
                raise RuntimeError("final backend startup did not pass")
            stage = "final_version"
            current_digest = application_digest(output_dir)
            if acceptance.get("application_sha256") and current_digest != acceptance["application_sha256"]:
                # Old pass evidence is stale, but the current candidate is still delivered.
                acceptance["status"] = "unverified"
                acceptance["application_sha256"] = current_digest
                identifiers = []
                for record in acceptance["records"]:
                    record.update(status="error", stale=True, application_sha256=current_digest)
                    for identifier, item in record.get("requirements", {}).items():
                        item["observation_status"] = "unverified"
                        identifiers.append(identifier)
                acceptance.update(unverified_ids=sorted(set(identifiers)), unverified_count=len(set(identifiers)),
                                  passed_chains=0, failed_chains=0,
                                  chain_counts={"passed": 0, "failed": 0,
                                                "unverified": sum(len(record.get("chain_titles", []))
                                                                  for record in acceptance["records"])})
                acceptance["issues"].append({"stage": stage,
                                             "message": "Application changed after final acceptance"})
                atomic_json(output_dir / "module-observations/acceptance.json", acceptance)
                _save_observation_records(output_dir, acceptance["records"], acceptance["issues"])
            stage = "checkpoint"
            save_checkpoint(output_dir, list(completed_ids), identity, build)
            stage = "commit"
            summary["final_git_head"] = commit_module(AgentRuntime.from_env(project_dir=str(output_dir)),
                "final-candidate", output_dir, "runnable; acceptance " + str(acceptance.get("status")))
        except Exception as exc:
            failure = {"stage": stage, "error": error_evidence(exc)}
            append_event(output_dir, {**failure, "status": "failed", "ended_at": utc_now()})
            summary["pending_issues"].append(failure)
            delivery_warnings.append(failure)
    reporting_path = output_dir / ".arc/codex-recovery/reporting-issues.json"
    try:
        summary["reporting_issues"] = json.loads(reporting_path.read_text()) if reporting_path.is_file() else []
    except (OSError, ValueError) as exc:
        summary["reporting_issues"] = []
        delivery_warnings.append({"stage": "reporting", "error": error_evidence(exc)})
    if delivery_warnings:
        summary["delivery_warnings"] = delivery_warnings
    summary["delivery"] = delivery_outcome(
        candidate_available(output_dir, baseline_digest,
                            extra_evidence=prepared_input or (resume_from is not None and
                                                               resume_integrity_failure is None),
                            identity=identity,
                            module_ids=[module.node_id for module in modules]),
        summary["status"] == "complete")
    generation_status = summary["status"]
    acceptance_status = (summary.get("acceptance") or {}).get("status", "unavailable")
    summary["unverified_count"] = sum(len(_atomic_requirement_index(module.subtree)) for module in modules)
    summary["engineering_memory"] = engineering_memory_report(output_dir)
    emit_runtime_event(publish_generation, output_dir, output_dir, modules, summary)
    try:
        atomic_json(output_dir / ".arc" / "codex-recovery" / "summary.json", summary)
    except Exception as exc:
        print(f"[delivery] summary write failed: {redact(exc)}", file=sys.stderr)
    chains = (summary.get("acceptance") or {}).get("chain_counts", {})
    print(f"[codex] delivery={summary['delivery']['status']}; generation={generation_status}; "
          f"acceptance={acceptance_status}; {len(summary['completed_ids'])}/{len(modules)} checkpointed, "
          f"{sum(bool(item.get('git_head')) for item in summary['module_results'])} committed, "
          f"{chains.get('passed', 0)} passed chains, {chains.get('failed', 0)} failed chains, "
          f"{summary['unverified_count']} unverified requirements, "
          f"{len(summary['pending_issues'])} unresolved, {len(delivery_warnings)} delivery warnings",
          flush=True)
    return summary["delivery"]["exit_code"]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[codex] {redact(exc)}", file=sys.stderr)
        raise SystemExit(1) from None
