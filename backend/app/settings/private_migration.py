"""仅在启动装配前迁移已知私人配置及其备份，不提升残留临时文件。"""
from fnmatch import fnmatchcase
import logging
import re
import uuid
from ..util.private_storage import (PrivateStorageError, decode_private, read_private_json,
                                    write_private_bytes)
from .model_store import validate_stored
from .mineru import MinerUInput


# 只识别新写入器的精确临时命名，并确认它属于已知配置或历史备份。
def _uncommitted_owner(path, patterns):
    match = re.fullmatch(r"\.(.+)\.[0-9a-f]{32}\.tmp", path.name)
    if match:
        for main, names in patterns.items():
            if match[1] == main.name or any(fnmatchcase(match[1], name) for name in names):
                return main


def migrate_private_settings(settings):
    folder = settings.data_dir / "_private"
    canonical = {folder / "model-settings.json": validate_stored,
                 folder / "mineru.json": MinerUInput.model_validate}
    # 所有有效配置先通过校验，再改写任何文件。备份/临时文件仅保留原始字节。
    for path, validate in canonical.items():
        if path.exists():
            try:
                validate(read_private_json(path))
                continue
            except Exception:
                pass
            raise PrivateStorageError(f"private_settings_invalid: {path.name}")
    valid = {path for path in canonical if path.exists()}
    files = set(valid)
    patterns = {}
    for main in canonical:
        stem = main.stem
        patterns[main] = (f"{stem}.rollback-backup-*.json", f"{stem}.json.before-*.bak",
                          f"{stem}.json.bak", f"{stem}.bak", f"{stem}.tmp", f"{stem}.json.tmp",
                          f"{stem}.json.*.tmp")
        for pattern in (*patterns[main], f".{stem}*.tmp"):
            files.update(folder.glob(pattern))
    # 逐文件迁移且不重写已有密文，中断后可保留进度并在下次启动接续。
    for path in sorted(files, key=lambda path: (path in canonical, path.name)):
        try:
            if _uncommitted_owner(path, patterns) in valid:
                # 写入器只向此命名的文件写密文；尚未提交的包装可能截断，不能当成配置解密。
                retained = folder / "_uncommitted" / f"{path.name}.{uuid.uuid4().hex}.quarantined"
                retained.parent.mkdir(exist_ok=True)
                path.rename(retained)
                logging.getLogger("studybuddy").warning(
                    "private_uncommitted_file_quarantined: 已隔离保留未提交的凭据临时文件 %s；使用已校验的主配置继续启动。",
                    retained.relative_to(folder))
                continue
            payload, encrypted = decode_private(path.read_bytes())
            if not encrypted:
                write_private_bytes(path, payload)
            continue
        except Exception:
            pass
        raise PrivateStorageError(f"private_migration_failed: {path.name}")
    return settings
