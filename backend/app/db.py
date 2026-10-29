from datetime import datetime, timezone
from sqlalchemy import create_engine, String, Float, Integer, Boolean, Text, JSON, ForeignKey, Index, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ACTIVE_TASK_STATUSES = frozenset({"queued", "running", "stopping"})
TASK_CONTROL_STATUSES = {"stop": ACTIVE_TASK_STATUSES | {"paused"},
                         "resume": frozenset({"paused", "interrupted"}),
                         "retry": frozenset({"failed", "interrupted"})}


def now():
    return datetime.now(timezone.utc).isoformat()


class Base(DeclarativeBase):
    pass


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, unique=True)
    filename: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    duration: Mapped[float] = mapped_column(Float)
    video_codec: Mapped[str] = mapped_column(String)
    audio_codec: Mapped[str | None] = mapped_column(String, nullable=True)
    media_info: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[str] = mapped_column(String, default=now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String, unique=True)
    filename: Mapped[str] = mapped_column(String)
    format: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(Integer)
    unit_count: Mapped[int] = mapped_column(Integer)
    character_count: Mapped[int] = mapped_column(Integer)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[str] = mapped_column(String, default=now)


# 保存视频执行快照、状态和正文；公开轮询响应由序列化模块另行筛选。
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"))
    status: Mapped[str] = mapped_column(String, default="queued")
    stage: Mapped[str] = mapped_column(String, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now)
    updated_at: Mapped[str] = mapped_column(String, default=now)


# 文档任务单独记录请求次数、正文修订号和完成时间，仍与视频共用队列。
class DocumentTask(Base):
    __tablename__ = "document_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    status: Mapped[str] = mapped_column(String, default="queued")
    stage: Mapped[str] = mapped_column(String, default="checking")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    notes_revision: Mapped[int] = mapped_column(Integer, default=0)
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now)
    updated_at: Mapped[str] = mapped_column(String, default=now)
    completed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        Index("document_task_created", "document_id", "created_at"),
        Index("document_one_active_task", "document_id", unique=True,
              sqlite_where=text("status IN ('queued','running','stopping')")),
    )


# 以稳定编号保存原始语音或画面依据及时间，生成副本去重不修改此记录。
class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), index=True)
    kind: Mapped[str] = mapped_column(String)
    start_seconds: Mapped[float] = mapped_column(Float)
    end_seconds: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    image_name: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


# 用资料类型、任务、章节和小节组成重点身份，标记绑定具体笔记版本。
class NoteMark(Base):
    __tablename__ = "note_marks"
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    task_id: Mapped[str] = mapped_column(String, primary_key=True)
    chapter_id: Mapped[str] = mapped_column(String, primary_key=True)
    heading_index: Mapped[int] = mapped_column(Integer, primary_key=True)


# 创建 SQLite 会话并启用 WAL 和外键，已有数据库仅补充兼容字段。
def database(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def configure(connection, _):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    # 为已有本地数据库补充置顶字段，保留所有视频与任务。
    with engine.begin() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(videos)")}
        if "pinned" not in columns:
            connection.exec_driver_sql("ALTER TABLE videos ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0")
        if "media_info" not in columns:
            connection.exec_driver_sql("ALTER TABLE videos ADD COLUMN media_info JSON")
    return engine, sessionmaker(engine, expire_on_commit=False)
