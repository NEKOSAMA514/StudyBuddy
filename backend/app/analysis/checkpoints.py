"""笔记检查点与淘汰版本的文件清理。"""
import re
from sqlalchemy import or_, select

CHECKPOINT_CLEANUP = "checkpoint_cleanup_pending"
MIND_MAP_CLEANUP = "mind_map_cleanup_pending"


def mind_map_path(folder, task_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise ValueError("invalid_mind_map_task_id")
    path = folder / "mind-maps" / f"{task_id}.png"
    if path.resolve() != path:
        raise ValueError("invalid_mind_map_path")
    return path


def checkpoint_path(folder, task_id, version, operation, key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise ValueError("invalid_checkpoint_task_id")
    stem = f"blocks-v{version}-{operation}-{key}"
    path = folder / f"{stem}-{task_id}-recent.json"
    # 旧摘要已经包含任务编号；只接管本次实际请求的精确旧路径。
    for suffix in (".json", ".split.json", ".tmp", ".split.tmp"):
        old = folder / f"{stem}{suffix}"
        target = path.with_suffix(suffix)
        if old.exists() and not target.exists():
            old.rename(target)
    return path


def clear_checkpoints(folder, task_id):
    pattern = re.compile(
        rf"^(blocks-v\d+-[a-z_]+-[0-9a-f]{{16}}-{re.escape(task_id)}-)"
        r"(recent|success)(\.(?:split\.)?(?:json|tmp))$")
    if not folder.exists():
        return
    successful = []
    for path in folder.iterdir():
        match = pattern.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        if match[2] == "recent":
            target = folder / f"{match[1]}success{match[3]}"
            path.replace(target)
            path = target
        successful.append(path)
    # 全部改名后再删除；中断时两种后缀都能被下一次清理接续。
    for path in set(successful):
        path.unlink(missing_ok=True)


# 共用成功后的文件清理流程，失败时保留数据库标志供稍后重试。
class CheckpointCleanup:
    def __init__(self, sessions, task_table, root, source_id, diagnostics):
        self.sessions, self.task_table = sessions, task_table
        self.root, self.source_id, self.diagnostics = root.resolve(), source_id, diagnostics

    def folder(self, task):
        folder = (self.root / self.source_id(task)).resolve()
        if folder.parent != self.root:
            raise ValueError("invalid_checkpoint_folder")
        return folder

    # 在淘汰事务内登记现有图片，文件删除留给提交后的清理。
    def defer_mind_maps(self, retired):
        for task in retired:
            try:
                path = mind_map_path(self.folder(task), task.id)
                pending = path.exists() or path.with_suffix(".tmp").exists()
            except (OSError, ValueError):
                pending = True
            if pending:
                task.config = {**task.config, MIND_MAP_CLEANUP: True}

    # 文件清理后重新确认成功状态，再移除标志；不修改任务完成时间。
    def cleanup(self, task_id):
        try:
            with self.sessions() as session:
                task = session.get(self.task_table, task_id)
                if task is None or task.status != "succeeded":
                    return
                pending = [flag for flag in (CHECKPOINT_CLEANUP, MIND_MAP_CLEANUP) if task.config.get(flag)]
                if not pending:
                    return
                folder = self.folder(task)
            cleared = []
            for flag in pending:
                try:
                    if flag == MIND_MAP_CLEANUP:
                        path = mind_map_path(folder, task_id)
                        path.unlink(missing_ok=True)
                        path.with_suffix(".tmp").unlink(missing_ok=True)
                    else:
                        clear_checkpoints(folder, task_id)
                except Exception as exc:
                    self.diagnostics.record("checkpoint_cleanup_failed", "笔记文件清理失败，稍后重试。", exc,
                                            task_id=task_id, cleanup=flag)
                else:
                    cleared.append(flag)
            if not cleared:
                return
            with self.sessions.begin() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                task = session.get(self.task_table, task_id)
                if task is not None and task.status == "succeeded":
                    task.config = {key: value for key, value in task.config.items() if key not in cleared}
        except Exception as exc:
            self.diagnostics.record("checkpoint_cleanup_failed", "笔记已成功保存，检查点清理失败，稍后重试。", exc, task_id=task_id)

    # 从数据库查找尚未清理的成功任务，支持运行中补清和重启恢复。
    def retry(self):
        try:
            with self.sessions() as session:
                table = self.task_table
                ids = list(session.scalars(select(table.id).where(
                    table.status == "succeeded", or_(table.config[CHECKPOINT_CLEANUP].as_boolean().is_(True),
                                                     table.config[MIND_MAP_CLEANUP].as_boolean().is_(True)))))
            for task_id in ids:
                self.cleanup(task_id)
        except Exception as exc:
            self.diagnostics.record("checkpoint_cleanup_failed", "读取待清理检查点失败，稍后重试。", exc)
