"""当前设置的原子引用；业务使用方只接收 read。"""
from ..config import Settings


class CurrentSettings:
    def __init__(self, settings: Settings):
        self._value = settings

    def read(self) -> Settings:
        return self._value

    # 设置服务仅在保存成功后发布新快照，业务使用方只持有读取能力。
    def replace(self, settings: Settings) -> None:
        self._value = settings
