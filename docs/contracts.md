# 接口与运行契约

本页记录跨模块必须一致的语义、默认值和限制。完整 HTTP 参数以路由与模型类型为准；服务运行时提供 `/docs` 和 `/openapi.json`。本页不复制整份自动 API 文档。

## 接口分组与公开边界

所有业务接口位于 `/api/v1` 下。

| 路径组 | 用途 | 代码依据 |
|---|---|---|
| `/videos`、`/tasks`、`/analyses`、`/evidence` | 视频文件、分析提交与控制、正文、转录和依据 | [公共路由](../backend/app/routes.py) |
| `/documents`、`/document-tasks`、`/document-analyses` | 文档上传、提取、笔记任务与正文 | [文档路由](../backend/app/documents/api.py) |
| `/settings`、`/health`、`/diagnostics/client-errors` | 设置、能力摘要和前端错误登记 | [设置与诊断路由](../backend/app/routes.py) |
| `/video-downloads`、`/bilibili` | 链接解析、下载和登录 | [下载路由](../backend/app/video_download/api.py)、[登录路由](../backend/app/video_download/bilibili_session.py) |
| `/notes/search`、`/note-marks` | 最新笔记搜索与版本重点 | [搜索](../backend/app/note_search.py)、[重点](../backend/app/note_marks.py) |

数据库对象不直接作为公开任务响应。视频公开结构见 [api_schemas.py](../backend/app/api_schemas.py)，序列化白名单见 [serialization.py](../backend/app/videos/serialization.py)；文档结构见 [schemas.py](../backend/app/documents/schemas.py)，前端对应 [types.ts](../frontend/src/types.ts)。

任务轮询不返回正文 `result`，也不暴露原件指纹或密钥。公开 `config` 是筛选后的摘要，不是内部完整执行配置。模型和 MinerU 设置只返回密钥是否存在等公开信息。

## 任务回执、状态与正文

创建、停止、继续和重试成功后返回回执，可能带 `reused`：

```json
{"task_id":"example-task","status":"queued","reused":false}
```

回执表示操作已被接受，不是完整任务，也不保证下一次 GET 立即成功。前端随后读取任务；同步期间保留可读笔记并阻止重复任务写操作，不拼造任务对象或用旧终态冒充新状态。

| 任务状态 | 含义与后续动作 |
|---|---|
| `queued` | 已入队，尚未执行；可停止 |
| `running` | 正在执行；停止需经过清理 |
| `stopping` | 已接受停止，等待执行流程退出 |
| `paused` | 已暂停，可继续 |
| `interrupted` | 服务退出或重启造成中断，可继续或重试 |
| `failed` | 执行失败，可在兼容性检查后重试 |
| `succeeded` | 本次执行成功；正文是否仍保留需结合读取状态判断 |

`notes_state` 独立表示正文为 `none`、`partial` 或 `ready`，`notes_revision` 用于同一任务追加后的刷新。只有成功保留的完整版本参与版本列表；草稿和被淘汰正文不能仅凭任务状态当作可读完整结果。

视频和文档共用串行笔记队列。同一资料不能同时运行多个活动任务；相同配置可复用现有活动任务，不同配置需先停止。继续或重试使用原任务编号并排到队尾；重新生成创建新任务，成功完整笔记最多保留两份。

列表中的 `latest_task` 优先选择活动任务，再按更新时间和编号降序选择。文件置顶优先，其他排序依据最近任务更新时间等字段；只读查询和检查点补清不用于刷新业务完成时间。

## 文档提取与来源

网页上传文档传入 `defer_extraction=true`，先保存原件；直接 API 上传默认不是延后模式。`extracted` 明确表示是否已有正式材料，不能从字符数推断提取状态。

后台提取入口为 `POST /documents/{id}/extract`，状态读取为 `GET /documents/{id}/extraction`。状态包含 `source_id`、`document_id`、`task_id`，以及 `status`、`parser`、`progress`、`error`；提取状态使用 `queued/running/done/failed/interrupted`，不与笔记状态混用。

材料先保存，笔记任务后提交。`done` 中的任务编号用于接管读取，不要求列表 `latest_task` 已同步。相同提取记录只交接一次；首次进入历史已完成材料时优先采用列表的最新任务，历史提取不能覆盖后来的手动任务。

视频依据使用真实时间与稳定来源 ID；文档材料块使用 `D` 编号及页、幻灯片、正文块或行范围，生成切片另保存字符偏移。Word 来源不推算页码。请求中的短别名与内部来源通过映射关联，模型不能自行编造原件位置。

视频当前笔记 `format_version` 为 4，文档为 1。正文格式、生成流程、提示词和检查点版本各有职责，不能相互替代；旧不兼容结果不自动转换，材料与原件仍保留。

## 配置与执行快照

设置保存成功后才发布新快照，失败保留原运行值。新操作在开始时读取当前设置；任务继续和重试还原已保存的模型、识别、风格及预算选择，不采用后来修改的默认值。

任务快照不保存密钥。恢复时按原连接编号和地址找回当前密钥；同地址密钥更新可用于恢复，连接删除或地址改变时明确要求恢复连接或重新生成，不把另一组密钥发往旧地址。

视频解析和下载固定启动时的代理。请求上游失败不会自动切换模型服务；生成版本变化或材料快照不匹配时拒绝不兼容恢复。缓存的不同作用范围见 [存储与恢复](storage.md#缓存检查点与版本)。

## 默认值与限制

运行值以设置和 `/health` 返回为准，下表为代码默认值或固定边界。

| 项目 | 当前规则 | 依据 |
|---|---|---|
| 视频上传与时长 | 默认 2 GiB、7,200 秒，可由启动配置设置 | [config.py](../backend/app/config.py) |
| 文档原件 | 不超过 100 MiB，且不超过应用上传限制 | [extraction.py](../backend/app/documents/extraction.py) |
| 提取材料 | 最多 500 万字符、10,000 个来源块；PDF 页数也受限制 | [extraction.py](../backend/app/documents/extraction.py)、[imports.py](../backend/app/documents/imports.py) |
| 文档生成 | 最多 10 万字符、8 个片段、80 次实际模型请求 | [material.py](../backend/app/documents/material.py) |
| 文档图片 | 最多 200 张不同图片，单张及相关合成尺寸受 2,000 万像素限制 | [image_text.py](../backend/app/documents/image_text.py)、[extraction.py](../backend/app/documents/extraction.py) |
| MinerU 分批 | 轻量每批最多 20 页／10 MiB，精准最多 200 页／200 MiB；原件仍受应用限制，批次按实际大小缩小 | [imports.py](../backend/app/documents/imports.py) |
| 视频画面采样 | 新任务均匀取 60 个计划位置；历史任务按保存的固定间隔恢复 | [recognition/extraction.py](../backend/app/recognition/extraction.py) |
| 详尽视频分段 | 时间目标 20 分钟，并同时满足实际发送输入预算；前文取有限时间窗口 | [segments.py](../backend/app/videos/segments.py) |
| 模型并发与间隔 | 默认并发 3、发送间隔 3 秒；设置允许 1～4 和 1～60 秒 | [model_settings.py](../backend/app/settings/model_settings.py) |
| 默认输入预算 | 25,000 个 Python 字符，不是 Token；实际计量包含系统及发送文字，排除材料 JSON 包装和部分定位元数据 | [budgets.py](../backend/app/analysis/budgets.py) |

各 API 组可保存预算。默认输出上限：规划、提取、章节和引文修正各 32,768 Token，概览 16,384，连接测试 1,024；简洁阶段使用章节输出预算。配置值还需符合所用模型接口能力。

输入过大时按流程拆分或压缩发送材料；不能用截掉未覆盖原文来伪造完整结果。文档预检不通过仍可查看已提取材料，提取上限与生成上限不是同一个概念。

明确可重试的上游请求错误最多尝试三次，共用发送间隔。结构或来源校验另有一次修正；章节只修正未完成部分，先等待成功章节保存。重试和修正都计入文档实际请求次数。

## 前端读取与错误策略

任务回执接管后立即读取。正常任务读取结束后等待两秒，暂时失败后等待五秒再读；明确 404 停止自动重试并提供手动入口。新操作、任务切换和卸载使旧结果失效。

文件列表只有一个串行读取队列。普通轮询排队不使当前有效响应失效；写操作后的显式刷新使旧读取失效，并可等待本次读取结束。首次失败结束加载并显示重读入口，后续失败保留最后成功数据。

正文、版本和依据读取各自可重试；同一资料刷新保留旧结果，版本淘汰只能依据本轮成功确认。具体共享机制见 [前端状态设计](architecture.md#前端如何避免状态倒退)。

| 请求错误策略 | 自动上报 | 自动通知 |
|---|---|---|
| `automatic` | 按需补报 | 是 |
| `caller` | 按需补报 | 由调用方决定 |
| `silent` | 否 | 否 |

后端已登记的错误不重复上报，主动取消静默处理。通知订阅只展示，业务链接随通知传入；普通信息通知不固定附带 MinerU 链接。

## 错误与诊断

失败响应采用 `error` 包装，字段按实际情况提供：

```json
{
  "error": {
    "code": "document_busy",
    "message": "此文档正在后台提取，请等待完成后删除。",
    "error_id": "example-error",
    "log_file": "log/example-error.log",
    "log_saved": true
  }
}
```

示例日志路径仅说明字段用途；真实位置以响应为准。日志写入失败通过 `log_saved=false` 明确报告，不能把不存在的日志当作已保存。HTTP 响应带请求编号，参数错误不回传包含凭据的原始输入。

任务动态是有容量限制的运行记录，不进入模型输入或生成身份；历史最多保留最近 100 条，当前请求和最终错误单独提供。轮询本身不写任务更新时间，断线时页面显示最后已知进度。

Markdown 正文、来源链接和下载由各领域的读取逻辑处理。视频时间入口定位播放器；文档来源入口定位材料起点；代码区域不参与时间或来源链接替换。重点标记使用明确的目标状态写入，不通过重复请求反转状态。
