"""Short, deterministic planner for the current ROOT module only."""
from __future__ import annotations

import hashlib
import json
import re
import time
from itertools import combinations
from typing import Any

MAX_BATCHES = 3
MAX_NODES = 256
MAX_TEXT_BYTES = 250_000
MAX_SECONDS = 0.25
_HARD_THRESHOLDS = {
    "atomic_count": 6,
    "scenario_count": 12,
    "text_bytes": 12_000,
    "write_operations": 10,
    "dependency_depth": 4,
}
_WRITE_WORDS = re.compile(
    r"\b(?:add|archive|assign|change|close|create|delete|edit|fork|merge|publish|remove|reopen|reset|save|set|store|submit|update)\b",
    re.I,
)
_ENTITIES = (
    "account", "session", "organization", "team", "repository", "branch", "file",
    "commit", "issue", "milestone", "pull request", "review", "release", "audit log",
)
_STATES = (
    "active", "available", "archived", "closed", "draft", "merged", "open", "pending",
    "private", "public", "rejected", "requested", "verified",
)
_PERMISSIONS = ("Owner", "Admin", "Read", "Triage", "Write", "Maintain")


def _id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("req_id") or "").strip()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _terms(text: str, terms: tuple[str, ...]) -> list[str]:
    lower = text.lower()
    return [term for term in terms if term.lower() in lower]


def _interfaces(text: str) -> list[str]:
    pairs = re.findall(r"[\u0060]([^\u0060]+)[\u0060]|[\u201c]([^\u201d]+)[\u201d]|\"([^\"]+)\"", text)
    return sorted({value for pair in pairs for value in pair if value.strip() and
                   len(value) <= 120 and not any(marker in value for marker in (": ", "{", "}"))})


def _split_delta(description: str) -> tuple[str, str, str]:
    marker = "Modified Feature Description"
    if marker in description:
        baseline, delta = description.split(marker, 1)
        return baseline.strip(), delta.strip(), "delta"
    if "Original Feature Description" in description:
        return description.strip(), "", "baseline"
    return "", description.strip(), "delta"


def _walk(module: dict[str, Any], max_nodes: int) -> tuple[
        dict[str, dict[str, Any]], list[str], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    atoms: list[str] = []
    issues: list[dict[str, Any]] = []
    started = time.monotonic()

    def visit(node: dict[str, Any], ancestors: tuple[str, ...]) -> None:
        if len(nodes) >= max_nodes:
            raise TimeoutError("splitter node guard exceeded")
        if time.monotonic() - started > MAX_SECONDS:
            raise TimeoutError("splitter time guard exceeded")
        identifier = _id(node)
        if not identifier:
            issues.append({"kind": "missing_id"})
            return
        if identifier in nodes:
            issues.append({"kind": "duplicate_id", "id": identifier})
            return
        nodes[identifier] = {
            "id": identifier, "node": node, "ancestors": ancestors,
            "children": [_id(child) for child in node.get("children", [])
                         if isinstance(child, dict)],
        }
        if node.get("type") == "ATOMIC":
            atoms.append(identifier)
        for child in node.get("children", []):
            if isinstance(child, dict):
                visit(child, (*ancestors, identifier))

    visit(module, ())
    return nodes, atoms, issues


def _dependency_graph(
    nodes: dict[str, dict[str, Any]], atoms: list[str],
    available_dependency_ids: set[str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]],
           dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    descendants: dict[str, list[str]] = {identifier: [] for identifier in nodes}
    for identifier in atoms:
        for ancestor in (*nodes[identifier]["ancestors"], identifier):
            descendants.setdefault(ancestor, []).append(identifier)
    order = {identifier: index for index, identifier in enumerate(atoms)}
    direct: dict[str, tuple[str, ...]] = {}
    external: dict[str, tuple[str, ...]] = {}
    for identifier in atoms:
        raw = [str(value)
               for owner in (*nodes[identifier]["ancestors"], identifier)
               for value in nodes[owner]["node"].get("dependencies") or []]
        found: list[str] = []
        external_values: list[str] = []
        for dependency in raw:
            if dependency not in nodes:
                if dependency in available_dependency_ids:
                    external_values.append(dependency)
                else:
                    issues.append({"kind": "missing_dependency", "module_id": identifier,
                                   "reference": dependency})
                continue
            found.extend(descendants.get(dependency, []))
        if identifier in found:
            issues.append({"kind": "cycle", "module_ids": [identifier, identifier]})
        direct[identifier] = tuple(sorted(set(found), key=order.__getitem__))
        external[identifier] = tuple(dict.fromkeys(external_values))
    closure_cache: dict[str, tuple[str, ...]] = {}
    active: list[str] = []

    def closure(identifier: str) -> tuple[str, ...]:
        if identifier in active:
            issues.append({"kind": "cycle", "module_ids": [*active, identifier]})
            return ()
        if identifier in closure_cache:
            return closure_cache[identifier]
        active.append(identifier)
        values: list[str] = []
        for dependency in direct[identifier]:
            values.extend((dependency, *closure(dependency)))
        active.pop()
        closure_cache[identifier] = tuple(dict.fromkeys(values))
        return closure_cache[identifier]

    for identifier in atoms:
        closure(identifier)
    return direct, closure_cache, external, issues


def _record(identifier: str, nodes: dict[str, dict[str, Any]],
            direct: dict[str, tuple[str, ...]], closure: dict[str, tuple[str, ...]],
            external: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    node = nodes[identifier]["node"]
    description = str(node.get("description") or "")
    baseline, delta, classification = _split_delta(description)
    scenarios = node.get("scenarios") if isinstance(node.get("scenarios"), list) else []
    text = _json_text(node)
    words = description + " " + _json_text(scenarios)
    return {
        "id": identifier,
        "parent_module_id": nodes[identifier]["ancestors"][0] if nodes[identifier]["ancestors"] else identifier,
        "direct_dependencies": list(direct[identifier]),
        "transitive_dependencies": list(closure.get(identifier, ())),
        "external_dependencies": list(external.get(identifier, ())),
        "classification": classification,
        "baseline_reference_text": baseline,
        "delta_text": delta,
        "scenario_count": len(scenarios),
        "text_bytes": len(text.encode("utf-8")),
        "write_operations": len(_WRITE_WORDS.findall(words)),
        "shared_entities": _terms(words, _ENTITIES),
        "permissions": sorted(set(_terms(words, _PERMISSIONS))),
        "states": _terms(words, _STATES),
        "interfaces": _interfaces(description),
        "persistence_objects": _terms(words, _ENTITIES)
        if re.search(r"persist|stored|save|reload|record", words, re.I) else [],
        "node": node,
    }


def _metrics(atom_ids: list[str], records: dict[str, dict[str, Any]],
             closure_ids: list[str]) -> dict[str, Any]:
    context_ids = list(dict.fromkeys([*atom_ids, *closure_ids]))
    owned = [records[identifier] for identifier in atom_ids]
    context = [records[identifier] for identifier in context_ids if identifier in records]
    return {
        "atomic_count": len(owned),
        "scenario_count": sum(item["scenario_count"] for item in owned),
        "text_bytes": sum(item["text_bytes"] for item in owned),
        "context_text_bytes": sum(item["text_bytes"] for item in context),
        "write_operations": sum(item["write_operations"] for item in owned),
        "dependency_depth": max((len(item["transitive_dependencies"]) for item in owned), default=0),
        "estimated_observation_planning_turns": 1,
        "estimated_coding_turns": 1,
        "estimated_model_calls": 2,
        "fixed_build_backend_observation_steps": 3,
    }


def _partitions(child_ids: list[str], max_parts: int) -> list[list[list[str]]]:
    result: list[list[list[str]]] = []
    for parts in range(2, min(max_parts, len(child_ids)) + 1):
        for cuts in combinations(range(1, len(child_ids)), parts - 1):
            points = (0, *cuts, len(child_ids))
            result.append([child_ids[points[i]:points[i + 1]] for i in range(parts)])
    return result


def _choose_groups(module: dict[str, Any], nodes: dict[str, dict[str, Any]],
                   atoms: list[str], direct: dict[str, tuple[str, ...]],
                   records: dict[str, dict[str, Any]], closure: dict[str, tuple[str, ...]]) -> tuple[
                       list[list[str]] | None, dict[str, Any]]:
    child_ids = [_id(child) for child in module.get("children", []) if isinstance(child, dict)]
    atom_by_child = {
        child_id: [identifier for identifier in atoms
                   if identifier == child_id or child_id in nodes[identifier]["ancestors"]]
        for child_id in child_ids
    }
    root_closure = sorted({dependency for identifier in atoms for dependency in closure.get(identifier, ())},
                          key=atoms.index)
    root_metrics = _metrics(atoms, records, root_closure)
    best: tuple[float, list[list[str]], dict[str, Any]] | None = None
    for groups in _partitions(child_ids, MAX_BATCHES):
        atom_groups = [[identifier for child_id in group for identifier in atom_by_child[child_id]]
                       for group in groups]
        if any(not group for group in atom_groups):
            continue
        positions = {identifier: index for index, group in enumerate(atom_groups) for identifier in group}
        if any(any(positions.get(dependency, position) > position
                   for dependency in direct[identifier])
               for identifier, position in positions.items()):
            continue
        batch_meta: list[dict[str, Any]] = []
        for group, atom_group in zip(groups, atom_groups):
            closure_ids = sorted({dependency for identifier in atom_group for dependency in closure.get(identifier, ())
                                  if dependency not in atom_group}, key=atoms.index)
            batch_meta.append({"child_ids": group, "atomic_ids": atom_group,
                               "dependency_closure_ids": closure_ids,
                               "metrics": _metrics(atom_group, records, closure_ids)})
        max_context = max(item["metrics"]["context_text_bytes"] for item in batch_meta)
        reduction = 1 - max_context / max(root_metrics["context_text_bytes"], 1)
        fixed = len(batch_meta) * 3
        if reduction < 0.20 or fixed > MAX_BATCHES * 3:
            continue
        score = max_context
        if best is None or score < best[0]:
            best = (score, groups, {"batches": batch_meta,
                                    "context_reduction_ratio": round(reduction, 4),
                                    "fixed_build_backend_observation_steps": fixed})
    return (best[1], best[2]) if best else (None, {"reason": "dependency_boundary_or_benefit"})


def plan_current_module_batches(module: dict[str, Any], *,
                                available_dependency_ids: set[str] | None = None,
                                max_batches: int = MAX_BATCHES,
                                max_nodes: int = MAX_NODES) -> dict[str, Any]:
    started = time.monotonic()
    root_id = _id(module)
    if not root_id:
        return {"decision": "keep_original", "stop_reason": "invalid_module", "batches": []}
    try:
        if max_batches < 1 or max_batches > MAX_BATCHES:
            raise ValueError("batch budget exceeds current-module guard")
        nodes, atoms, issues = _walk(module, max_nodes)
        direct, closure, external, graph_issues = _dependency_graph(
            nodes, atoms, available_dependency_ids or set())
        issues.extend(graph_issues)
        records = {identifier: _record(identifier, nodes, direct, closure, external) for identifier in atoms}
        metrics = _metrics(atoms, records, sorted({dependency for identifier in atoms
                                                  for dependency in closure.get(identifier, ())},
                                                 key=atoms.index))
        if len(_json_text(module).encode("utf-8")) > MAX_TEXT_BYTES:
            raise TimeoutError("splitter text guard exceeded")
        high_risk = any(metrics[key] >= threshold for key, threshold in _HARD_THRESHOLDS.items())
        chosen_groups: list[list[str]] | None = None
        choice_meta: dict[str, Any] = {}
        if not issues and high_risk:
            chosen_groups, choice_meta = _choose_groups(module, nodes, atoms, direct, records, closure)
        if issues:
            decision, stop_reason = "keep_original", "dependency_graph_invalid"
        elif not high_risk:
            decision, stop_reason = "keep_original", "low_risk"
        elif chosen_groups is None:
            decision, stop_reason = "keep_original", "dependency_boundary_or_budget"
        else:
            decision, stop_reason = "split", None
        batches: list[dict[str, Any]] = []
        groups = choice_meta.get("batches") if decision == "split" else None
        if decision == "split" and groups:
            source_groups = groups
        else:
            source_groups = [{
                "child_ids": [_id(child) for child in module.get("children", []) if isinstance(child, dict)],
                "atomic_ids": atoms,
                "dependency_closure_ids": sorted({dependency for identifier in atoms
                                                  for dependency in closure.get(identifier, ())}, key=atoms.index),
                "metrics": metrics,
            }]
        for index, item in enumerate(source_groups, 1):
            batch_id = f"{root_id}.part-{index}" if decision == "split" else root_id
            owned = item["atomic_ids"]
            closure_ids = item["dependency_closure_ids"]
            batches.append({
                "batch_id": batch_id, "root_module_id": root_id,
                "part": index if decision == "split" else None,
                "child_ids": item["child_ids"], "atomic_ids": owned,
                "direct_dependency_ids": sorted({dependency for identifier in owned
                                                 for dependency in direct[identifier]
                                                 if dependency not in owned}, key=atoms.index),
                "dependency_closure_ids": closure_ids,
                "external_dependency_ids": sorted({dependency for identifier in owned
                                                   for dependency in external[identifier]}),
                "metrics": item["metrics"],
                "root_constraints": {
                    "id": module.get("id"), "name": module.get("name"),
                    "description": module.get("description"), "dependencies": module.get("dependencies") or [],
                },
                "shared_contract": {
                    "entities": sorted({value for identifier in owned for value in records[identifier]["shared_entities"]}),
                    "permissions": sorted({value for identifier in owned for value in records[identifier]["permissions"]}),
                    "states": sorted({value for identifier in owned for value in records[identifier]["states"]}),
                    "interfaces": sorted({value for identifier in owned for value in records[identifier]["interfaces"]}),
                    "persistence_objects": sorted({value for identifier in owned for value in records[identifier]["persistence_objects"]}),
                },
                "dependency_definitions": [records[identifier]["node"] for identifier in closure_ids if identifier in records],
            })
        owned = [identifier for batch in batches for identifier in batch["atomic_ids"]]
        duplicates = sorted({identifier for identifier in owned if owned.count(identifier) > 1})
        missing = sorted(set(atoms) - set(owned), key=atoms.index)
        if duplicates or missing:
            decision, stop_reason = "keep_original", "coverage_invalid"
            issues.append({"kind": "coverage_invalid", "duplicates": duplicates, "missing": missing})
            batches = [batches[0]]
        if time.monotonic() - started > MAX_SECONDS:
            raise TimeoutError("splitter time guard exceeded")
        plan = {
            "version": 1, "root_module_id": root_id, "decision": decision,
            "stop_reason": stop_reason, "batch_count": len(batches),
            "planned_module_ids": [batch["batch_id"] for batch in batches],
            "planned_model_calls": len(batches) * 2,
            "estimated_observation_planning_turns": len(batches),
            "estimated_coding_turns": len(batches),
            "fixed_build_backend_observation_steps": len(batches) * 3,
            "atomic_coverage_ok": not duplicates and not missing,
            "duplicate_atomic_ids": duplicates, "missing_atomic_ids": missing,
            "issues": issues, "metrics": metrics, "hard_thresholds": _HARD_THRESHOLDS,
            "candidate_batches": batches if decision == "split" else [],
            "batches": batches, "atomic_records": [records[identifier] for identifier in atoms],
        }
        plan["batch_plan_sha256"] = hashlib.sha256(
            _json_text({key: value for key, value in plan.items() if key != "batch_plan_sha256"}).encode()
        ).hexdigest()
        return plan
    except (TimeoutError, ValueError, TypeError, KeyError) as error:
        fallback_atoms: list[str] = []

        def collect_fallback_atoms(node: Any) -> None:
            if not isinstance(node, dict):
                return
            identifier = _id(node)
            if identifier and node.get("type") == "ATOMIC":
                fallback_atoms.append(identifier)
            for child in node.get("children", []):
                collect_fallback_atoms(child)

        collect_fallback_atoms(module)
        fallback = {
            "version": 1, "root_module_id": root_id, "decision": "keep_original",
            "stop_reason": "splitter_guard", "batch_count": 1,
            "planned_module_ids": [root_id], "planned_model_calls": 2,
            "estimated_observation_planning_turns": 1, "estimated_coding_turns": 1,
            "fixed_build_backend_observation_steps": 3, "atomic_coverage_ok": True,
            "duplicate_atomic_ids": [], "missing_atomic_ids": [],
            "issues": [{"kind": "splitter_guard", "message": str(error)}],
            "batches": [{"batch_id": root_id, "root_module_id": root_id, "part": None,
                         "child_ids": [_id(child) for child in module.get("children", [])
                                       if isinstance(child, dict)],
                         "atomic_ids": fallback_atoms, "dependency_closure_ids": [],
                         "metrics": {"atomic_count": len(fallback_atoms)}}],
            "atomic_records": [],
        }
        fallback["batch_plan_sha256"] = hashlib.sha256(
            _json_text({key: value for key, value in fallback.items() if key != "batch_plan_sha256"}).encode()
        ).hexdigest()
        return fallback
