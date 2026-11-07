"""Task directories and identity markers; progress remains exclusively in Restate."""
from hashlib import sha256
import json
import os
from pathlib import Path, PureWindowsPath
import posixpath
import re
import tempfile

from .artifacts import _source
from .settings import project_root


def public_path(path):
    """Expose the configured host spelling when the backend runs in a mount."""
    value = str(path).replace('\\', '/')
    mappings = json.loads(os.getenv('AI_WORKFLOW_PATH_MAP', '{}'))
    for source, target in sorted(mappings.items(), key=lambda x: len(x[1]), reverse=True):
        target = target.replace('\\', '/').rstrip('/')
        if value == target or value.startswith(target + '/'):
            return source.replace('\\', '/').rstrip('/') + value[len(target):]
    return value


def default_settings():
    return {'default_task_root': public_path(project_root() / '.local/tasks')}


def validate_root(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value) or '#' in value:
        raise ValueError('默认任务目录必须是非空绝对路径，不能含控制字符或 #')
    value = value.replace('\\', '/')
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()) or '://' in value:
        raise ValueError('默认任务目录必须是本机或已映射的绝对路径')
    path = _source(project_root(), value)
    path.mkdir(parents=True, exist_ok=True)
    # Check the actual backend's ability to write, not only path syntax.
    with tempfile.TemporaryFile(dir=path):
        pass
    return {'default_task_root': public_path(path)}


def allocate(run_id, root_run_id, settings, parent=None):
    base = parent['path'].rstrip('/') + '/subtasks' if parent else settings['default_task_root']
    slug = re.sub(r'[^\w.-]+', '-', run_id, flags=re.UNICODE).strip('.-')[:48] or 'run'
    name = 'task-' + slug + '-' + sha256(run_id.encode()).hexdigest()[:12]
    directory = _source(project_root(), base) / name
    if directory.resolve() != directory:
        raise ValueError('任务目录不能重定向到其他位置')
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / '.workflow-task.json'
    if marker.is_symlink():
        raise ValueError('任务标识不能使用符号链接')
    identity = {'run_id': run_id, 'root_run_id': root_run_id, 'controller': public_path(project_root())}
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('任务目录已属于其他运行或总控，不能接管')
    else:
        # The marker is a pointer only: no statuses, budgets or duplicate task ledger.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(identity, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, marker)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    return {'path': public_path(directory), 'server_path': str(directory), 'root_run_id': root_run_id}


def contains(workspace, directory):
    """Match an exact task directory or a descendant, across its declared spellings."""
    value = posixpath.normpath(directory.replace('\\', '/')).rstrip('/')
    for key in ('path', 'server_path'):
        base = workspace.get(key, '').replace('\\', '/').rstrip('/')
        if not base:
            continue
        candidate, prefix = (value.casefold(), base.casefold()) if re.match(r'^[A-Za-z]:/', base) else (value, base)
        if candidate == prefix or candidate.startswith(prefix + '/'):
            return True
    return False
