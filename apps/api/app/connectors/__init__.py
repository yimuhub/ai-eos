"""连接器 SDK（V6 M3）：外部管理软件的数据入口。"""
from .base import PullRecord, PullResult, get_connector, map_row, upsert_records

__all__ = ["PullRecord", "PullResult", "get_connector", "map_row",
           "upsert_records"]
