# Skill 插件系统

## 接口

```python
class BaseSkill(ABC):
    id/name/description/version/input_schema/output_schema/permissions
    async def execute(input_data: dict) -> dict
```

## 内置插件
- text_processing: 字数统计、URL 提取
- file_reader: 读取 workspace 文本文件
- web_search: 骨架实现（Phase 2 接真实搜索后端）

## 扩展方式
plugins/skills/ 目录放新插件文件 -> register_skill(Cls) 注册 ->
GET /skills 自动同步清单到数据库 -> POST install 绑定给 Agent。
Runtime 通过 build_context 将已装技能告知 LLM，禁止硬编码（Rule 3/6）。\n