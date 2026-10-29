from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# 限制文件重命名和置顶请求的可写字段，避免直接提交内部文件记录。
class FileEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    filename: str | None = Field(default=None, min_length=1, max_length=200)
    pinned: bool | None = None

    @field_validator("filename")
    @classmethod
    def valid_name(cls, value):
        if value is not None and any(character in '/\\' or ord(character) < 32 for character in value):
            raise ValueError("名称不能包含路径或控制字符")
        return value

    @model_validator(mode="after")
    def not_empty(self):
        if self.filename is None and self.pinned is None:
            raise ValueError("至少提供名称或置顶状态")
        return self
