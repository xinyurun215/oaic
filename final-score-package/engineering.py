"""Source-linked design and bounded handoff; declarations never prove business passage."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

VERSION = 1


def source_index(requirements_dir: Path) -> dict[str, Any]:
    raw = (requirements_dir / 'requirements.yaml').read_bytes()
    tree = hashlib.sha256()
    for file in sorted(requirements_dir.rglob('*')):
        if file.is_file() and not file.is_symlink():
            tree.update(file.relative_to(requirements_dir).as_posix().encode())
            tree.update(hashlib.sha256(file.read_bytes()).digest())
    payload = yaml.safe_load(raw)
    nodes, atoms = {}, []
    def visit(node, path, module_id, ancestors):
        identifier = str(node.get('id') or node.get('req_id') or '')
        item = {'id': identifier, 'module_id': module_id, 'path': path,
                'node': node, 'ancestors': ancestors}
        if identifier in nodes:
            raise ValueError('Source IDs must be unique')
        nodes[identifier] = item
        if node.get('type') == 'ATOMIC':
            atoms.append(item)
        for index, child in enumerate(node.get('children', [])):
            if isinstance(child, dict):
                visit(child, path + '/children/' + str(index),
                      (child.get('id') or child.get('req_id')) if path == '' else module_id,
                      [*ancestors, identifier])
    visit(payload, '', None, [])
    return {'source_sha256': hashlib.sha256(raw).hexdigest(), 'source_tree_sha256': tree.hexdigest(), 'payload': payload,
            'nodes': nodes, 'atoms': atoms}


def module_order(children: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    owners, dependencies, issues = {'ROOT': None}, {}, []
    def visit(node, owner):
        identifier = str(node.get('id') or node.get('req_id') or '')
        owners[identifier] = owner
        dependencies.setdefault(owner, []).extend(node.get('dependencies') or [])
        for child in node.get('children', []):
            if isinstance(child, dict): visit(child, owner)
    for module in children:
        visit(module, str(module.get('id') or module.get('req_id') or ''))
    required = {owner: set() for owner in dependencies}
    for owner, references in dependencies.items():
        for reference in references:
            if reference not in owners:
                issues.append({'kind': 'missing_dependency', 'module_id': owner, 'reference': reference})
            elif owners[reference] is not None and owners[reference] != owner:
                required[owner].add(owners[reference])
    pending, ordered, completed = list(children), [], set()
    while pending:
        ready = next((item for item in pending if required[str(item.get('id') or item.get('req_id'))] <= completed), None)
        if ready is None:
            issues.append({'kind': 'cycle', 'module_ids': [str(item.get('id') or item.get('req_id')) for item in pending]})
            ordered.extend(pending)
            break
        pending.remove(ready)
        ordered.append(ready)
        completed.add(str(ready.get('id') or ready.get('req_id')))
    return ordered, issues


def missing_source_dependencies(source: dict[str, Any]) -> set[str]:
    missing: set[str] = set()
    for item in source.get('nodes', {}).values():
        for dependency in (item.get('node') or {}).get('dependencies') or []:
            if dependency not in source['nodes']:
                missing.add(str(dependency))
    return missing


def validate_source_dependencies(source: dict[str, Any],
                                 external_predecessors: set[str] | None = None) -> None:
    """Validate original graph; folded module cycles remain ordering warnings."""
    external_predecessors = external_predecessors or set()
    checked, active = set(), set()
    def check_dependencies(identifier):
        if identifier not in source['nodes']:
            if identifier in external_predecessors:
                return
            raise ValueError('Engineering source dependency missing: ' + str(identifier))
        if identifier in active:
            raise ValueError('Engineering source dependency cycle: ' + identifier)
        if identifier in checked: return
        active.add(identifier)
        item = source['nodes'][identifier]
        for owner in [*item['ancestors'], identifier]:
            for dependency in source['nodes'][owner]['node'].get('dependencies') or []:
                # Same-owner ancestor prerequisites grant scope, not sibling reverse edges.
                if (owner == identifier or dependency not in source['nodes'] or
                        source['nodes'][dependency]['module_id'] != item['module_id']):
                    if dependency not in source['nodes'] and dependency in external_predecessors:
                        continue
                    check_dependencies(dependency)
        active.remove(identifier)
        checked.add(identifier)
    for identifier in source['nodes']:
        check_dependencies(identifier)


def validate_contract(proposal: Any, requirements_dir: Path) -> dict[str, Any]:
    source = source_index(requirements_dir)
    if not isinstance(proposal, dict) or proposal.get('source_sha256') != source['source_sha256']:
        raise ValueError('Engineering source digest mismatch')
    if proposal.get('source_tree_sha256', source['source_tree_sha256']) != source['source_tree_sha256']:
        raise ValueError('Engineering reference source digest mismatch')
    records = proposal.get('requirements')
    expected = {item['id']: item for item in source['atoms']}
    if (not isinstance(records, list) or any(not isinstance(item, dict) for item in records) or
        len(records) != len(expected) or {item.get('id') for item in records} != set(expected)):
        raise ValueError('Engineering contract must cover each unique source atomic ID exactly once')
    def resolve(path):
        value = source['payload']
        for part in path.strip('/').split('/'):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    validate_source_dependencies(source)
    def dependency_sources(original):
        """Inherited clauses grant source scope; they do not invent reverse atomic graph edges."""
        found = set()
        def visit(identifier):
            if identifier in found: return
            found.add(identifier)
            item = source['nodes'][identifier]
            for owner in [*item['ancestors'], identifier]:
                for dependency in source['nodes'][owner]['node'].get('dependencies') or []:
                    visit(dependency)
        # Ancestors themselves are still restricted to their descriptions.
        for owner in [*original['ancestors'], original['id']]:
            for dependency in source['nodes'][owner]['node'].get('dependencies') or []:
                visit(dependency)
        return [source['nodes'][identifier]['path'] for identifier in found]
    for record in records:
        original = expected[record['id']]
        if record.get('module_id') != original['module_id']:
            raise ValueError('Engineering module ownership mismatch')
        references = record.get('source_refs')
        if not isinstance(references, list) or not references:
            raise ValueError('Engineering source references missing')
        own_reference = False
        allowed = [source['nodes'][identifier]['path'] for identifier in original['ancestors']]
        dependencies = dependency_sources(original)
        for reference in references:
            if not isinstance(reference, dict): raise ValueError('Invalid engineering source reference')
            path, quote = reference.get('path'), reference.get('quote')
            if not isinstance(path, str) or not isinstance(quote, str) or not quote.strip():
                raise ValueError('Invalid engineering source quote')
            if '/description' not in path and '/scenarios/' not in path:
                raise ValueError('Engineering quote must locate source prose')
            try: text = resolve(path)
            except (KeyError, IndexError, ValueError, TypeError):
                raise ValueError('Engineering source locator does not exist') from None
            if not isinstance(text, str) or quote not in text:
                raise ValueError('Engineering quote does not correspond to source text')
            if path.startswith(original['path'] + '/'):
                own_reference = True
            elif not (any(path == prefix + '/description' for prefix in allowed) or
                      any(path == prefix + '/description' or
                          (prefix and path.startswith(prefix + '/')) for prefix in dependencies)):
                raise ValueError('Engineering reference belongs to another atomic source')
        if not own_reference: raise ValueError('Engineering record needs its own atomic source quote')
        for field in ('entry', 'persistence'):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError('Engineering field missing: ' + field)
        for field in ('roles', 'actions', 'observable_results', 'shared_objects', 'unknowns', 'preconditions'):
            if not isinstance(record.get(field), list): raise ValueError('Engineering field missing: ' + field)
        for condition in record['preconditions']:
            if (not isinstance(condition, dict) or condition.get('kind') not in {'startup', 'ui', 'unknown'} or
                    not isinstance(condition.get('text'), str)):
                raise ValueError('Engineering precondition must distinguish startup, UI and unknown')
            if condition['kind'] == 'startup':
                reference = condition.get('source_ref')
                if not isinstance(reference, dict) or reference not in references:
                    raise ValueError('Startup precondition requires a checked source quote')
    shared = proposal.get('shared_objects')
    modules = {item['module_id'] for item in source['atoms']}
    if not isinstance(shared, list) or not isinstance(proposal.get('conflicts'), list):
        raise ValueError('Engineering shared objects and conflicts must be lists')
    names = []
    for item in shared:
        if (not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name'] or
                item.get('owner_module') not in modules or not isinstance(item.get('consumer_modules'), list) or
                any(consumer not in modules for consumer in item['consumer_modules']) or
                not isinstance(item.get('fields'), list) or not isinstance(item.get('unknowns'), list)):
            raise ValueError('Invalid shared engineering object')
        names.append(item['name'])
    if len(names) != len(set(names)) or any(name not in names for record in records for name in record['shared_objects']):
        raise ValueError('Shared engineering objects must be unique and declared')
    return {**proposal, 'source_tree_sha256': source['source_tree_sha256'], 'version': VERSION,
            'validation': 'source_linked_declaration_not_functional_proof'}


def _file_digest(output_dir: Path, name: Any) -> str | None:
    if not isinstance(name, str) or '\\' in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts or ':' in name:
        return None
    target = output_dir / name
    try:
        if not target.resolve().is_relative_to(output_dir.resolve()) or not target.is_file(): return None
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError): return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def capture_module_state(output_dir: Path, module_id: str, declaration: Any, requirements_dir: Path) -> None:
    issues = [] if isinstance(declaration, dict) else ['Implementation declaration absent or invalid']
    declaration = declaration if isinstance(declaration, dict) else {}
    records = {}
    for field in ('interfaces', 'requirements'):
        records[field] = declaration.get(field, [])
        if not isinstance(records[field], list):
            issues.append('Invalid implementation declaration list: ' + field)
            records[field] = []
    interfaces, files = [], {}
    for item in records['interfaces']:
        if not isinstance(item, dict):
            issues.append('Invalid interface declaration')
            continue
        paths = [item.get('definition'), *(item.get('consumers') if isinstance(item.get('consumers'), list) else [])]
        valid = bool(item.get('name')) and isinstance(item.get('fields'), list)
        for path in paths:
            digest = _file_digest(output_dir, path)
            if digest is None: valid = False
            elif isinstance(path, str): files[path] = digest
        if not valid: issues.append('Unverified interface: ' + str(item.get('name', 'unnamed')))
        interfaces.append({**item, 'fields': item.get('fields') if isinstance(item.get('fields'), list) else [],
                           'consumers': item.get('consumers') if isinstance(item.get('consumers'), list) else [],
                           'status': 'current' if valid else 'invalid'})
    for item in records['requirements']:
        if not isinstance(item, dict): continue
        for path in item.get('files', []) if isinstance(item.get('files'), list) else []:
            digest = _file_digest(output_dir, path)
            if digest: files[path] = digest
    state_path = output_dir / '.arc/engineering/state.json'
    state = _read(state_path)
    state.setdefault('modules', {})[module_id] = {
        'module_id': module_id, 'source_sha256': source_index(requirements_dir)['source_sha256'],
        'source_tree_sha256': source_index(requirements_dir)['source_tree_sha256'],
        'requirements': records['requirements'], 'interfaces': interfaces, 'files': files,
        'declaration_status': 'unverified' if issues else 'reported', 'declaration_issues': issues}
    _write(state_path, state)


def restore_module_state(restored: Path, output_dir: Path, module_id: str) -> None:
    """Restore this module's checked metadata; other records and frozen plans retain their origin."""
    state_path = output_dir / '.arc/engineering/state.json'
    current = _read(state_path)
    previous = _read(restored / '.arc/engineering/state.json').get('modules', {})
    modules = current.setdefault('modules', {})
    if module_id in previous: modules[module_id] = previous[module_id]
    else: modules.pop(module_id, None)
    _write(state_path, current)


def handoff(output_dir: Path, requirements_dir: Path, module_ids: list[str] | None = None) -> dict[str, Any]:
    source = source_index(requirements_dir)
    state = _read(output_dir / '.arc/engineering/state.json')
    records = []
    for identifier, record in state.get('modules', {}).items():
        if module_ids is not None and identifier not in module_ids: continue
        source_status = 'current' if (record.get('source_sha256') == source['source_sha256'] and
            record.get('source_tree_sha256') == source['source_tree_sha256']) else 'stale'
        changed = [path for path, digest in record.get('files', {}).items() if _file_digest(output_dir, path) != digest]
        interfaces = []
        for item in record.get('interfaces', []):
            paths = [item.get('definition'), *(item.get('consumers') or [])]
            status = item.get('status')
            if status == 'current' and (source_status == 'stale' or any(path in changed for path in paths)):
                status = 'stale'
            interfaces.append({**item, 'status': status})
        evidence = []
        fields = [field for item in interfaces for field in item.get('fields', []) if isinstance(field, str)]
        for path in list(record.get('files', {}))[:8]:
            current = _file_digest(output_dir, path)
            if not current: continue
            lines = (output_dir / path).read_text(encoding='utf-8', errors='replace').splitlines()
            hits = [index for index, line in enumerate(lines)
                    if any(re.search(r'\b' + re.escape(field) + r'\b', line) for field in fields)]
            selected = sorted({index for hit in hits[:4] for index in range(max(0, hit-2), min(len(lines), hit+3))})
            if not selected: selected = list(range(min(len(lines), 16)))
            evidence.append({'path': path, 'sha256': current,
                             'excerpt': [{'line': index+1, 'text': lines[index]} for index in selected[:20]]})
        records.append({**record, 'implementation_evidence': evidence, 'interfaces': interfaces, 'source_status': source_status,
                        'status': 'stale' if changed or source_status == 'stale' else 'current', 'changed_files': changed})
    return {'source_sha256': source['source_sha256'], 'modules': records,
            'instruction': 'Declarations are reading aids. Re-read actual definitions and all consumers before changing shared fields; stale records cannot establish current shape.'}


def cached_observation(output_dir: Path, module_id: str, identity: dict[str, Any], source_sha256: str) -> tuple[dict[str, Any], bytes] | None:
    receipt = checkpoint_authority(output_dir, identity)
    return observation_from_authority(output_dir, module_id, identity, source_sha256, receipt)


def checkpoint_authority(root: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    recovery = root if (root / 'latest.json').is_file() else root / '.arc/codex-recovery'
    latest = _read(recovery / 'latest.json')
    name = latest.get('checkpoint')
    if latest.get('identity') != identity or not isinstance(name, str) or not re.fullmatch(r'module-\d{3}-[0-9a-f]{12}', name):
        return None
    checkpoint = recovery / 'checkpoints' / name
    manifest = _read(checkpoint / 'checkpoint.json')
    metadata = checkpoint / 'engineering/preflight.json'
    if manifest.get('identity') != identity or not metadata.is_file(): return None
    if hashlib.sha256(metadata.read_bytes()).hexdigest() != manifest.get('engineering_files', {}).get('preflight.json'):
        return None
    tree = hashlib.sha256()
    for file in sorted((checkpoint / 'source').rglob('*')):
        if file.is_file() and not file.is_symlink():
            tree.update(file.relative_to(checkpoint / 'source').as_posix().encode())
            tree.update(hashlib.sha256(file.read_bytes()).digest())
    if tree.hexdigest() != manifest.get('source_sha256'): return None
    return _read(metadata)


def observation_from_authority(output_dir: Path, module_id: str, identity: dict[str, Any],
                               source_sha256: str, receipt: dict[str, Any] | None) -> tuple[dict[str, Any], bytes] | None:
    receipt = receipt or {}
    entry = receipt.get('observations', {}).get(module_id, {})
    if (receipt.get('version') != VERSION or receipt.get('identity') != identity or
            receipt.get('source_sha256') != source_sha256 or entry.get('status') != 'prepared'):
        return None
    folder = output_dir / 'module-observations' / module_id
    try:
        version = entry.get('version', 1)
        if version not in (1, 2): return None
        script = (folder / f'frozen-v{version}.spec.js').read_bytes()
        contract = (folder / 'contract.json').read_bytes()
        if (hashlib.sha256(script).hexdigest() != entry.get('script_sha256') or
                hashlib.sha256(contract).hexdigest() != entry.get('contract_sha256') or
                (folder / 'checks.spec.js').read_bytes() != script): return None
        if version == 2 and (hashlib.sha256((folder / 'frozen-v1.spec.js').read_bytes()).hexdigest()
                             != entry.get('original_script_sha256')): return None
        return json.loads(contract), script
    except (OSError, ValueError): return None


if __name__ == '__main__':
    import sys
    try:
        validate_contract(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')), Path(sys.argv[2]))
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    print('Complete source-linked engineering contract checked; behavior remains unverified.')
