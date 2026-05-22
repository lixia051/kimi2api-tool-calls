# kimi2api-tool-calls

> 为 [lorsque-sir/kimi2api](https://github.com/lorsque-sir/kimi2api) 添加 OpenAI 标准 `tool_calls` 支持的外挂补丁。

kimi2api 将 Kimi Web 端（www.kimi.com）逆向为 OpenAI 兼容的 `/v1/chat/completions` 接口，但原生不支持 `tools` / `tool_calls`。本插件通过 prompt 注入 + XML 解析的方式，让 kimi2api 能返回标准的 OpenAI tool_calls 响应，使其可以接入需要工具调用能力的 AI Agent 框架（如 Hermes Agent、AutoGen、LangChain 等）。

## 工作原理

```
Client (带 tools 参数的请求)
  ↓
kimi2api + tool_compat.py
  ↓ 将工具 schema 注入 system prompt，使用 DSML XML 格式
  ↓ 指导模型输出结构化的工具调用 XML
Kimi Web API (www.kimi.com gRPC)
  ↓ 返回 DSML XML 格式的工具调用文本
kimi2api + tool_compat.py
  ↓ 解析 XML → 标准 OpenAI tool_calls 响应
Client (收到标准 tool_calls)
```

参考了 [CJackHwang/ds2api](https://github.com/CJackHwang/ds2api) 的 DSML XML 工具调用格式。

## 支持的功能

- ✅ 非流式 `tool_calls`（`stream: false`）
- ✅ 流式 `tool_calls`（`stream: true`，SSE delta 格式）
- ✅ `tool_choice: "auto"` / `"required"`
- ✅ 多工具并行调用
- ✅ 工具结果回填后继续对话（多轮 tool loop）
- ✅ 复杂参数（嵌套 JSON、长代码块、CDATA 转义）
- ✅ 不带 `tools` 的普通请求不受影响

## 安装方式

### 方式一：Volume 挂载（推荐，不修改原镜像）

1. 将 `patches/api/` 目录复制到你的 kimi2api 部署目录：

```bash
cp -r patches/ /path/to/your/kimi2api/patches/
```

2. 修改 `docker-compose.yml`，添加 volume 挂载：

```yaml
services:
  kimi2api:
    # ... 原有配置保持不变 ...
    volumes:
      - ./data:/app/data
      # 添加以下三行：
      - ./patches/api/tool_compat.py:/app/app/api/tool_compat.py:ro
      - ./patches/api/routes.py:/app/app/api/routes.py:ro
      - ./patches/api/streaming.py:/app/app/api/streaming.py:ro
```

3. 重启容器：

```bash
docker compose up -d
```

### 方式二：直接复制到容器

```bash
docker cp patches/api/tool_compat.py kimi2api:/app/app/api/tool_compat.py
docker cp patches/api/routes.py kimi2api:/app/app/api/routes.py
docker cp patches/api/streaming.py kimi2api:/app/app/api/streaming.py
docker restart kimi2api
```

> ⚠️ 此方式在容器重建后会丢失，建议使用方式一。

## 使用示例

### 非流式

```bash
curl https://your-kimi2api-endpoint/v1/chat/completions \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "kimi-k2.6",
    "messages": [{"role": "user", "content": "请查询杭州天气"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取指定城市的天气信息",
        "parameters": {
          "type": "object",
          "properties": {"city": {"type": "string"}},
          "required": ["city"]
        }
      }
    }],
    "tool_choice": "auto",
    "stream": false
  }'
```

返回：

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_xxx",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\":\"杭州\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

### 流式

请求中设置 `"stream": true`，返回 SSE 格式，包含 `delta.tool_calls`。

## 文件说明

| 文件 | 说明 |
|------|------|
| `patches/api/tool_compat.py` | 新增文件：DSML prompt 构建 + XML 解析 + OpenAI tool_calls 转换 |
| `patches/api/routes.py` | 修改：在 chat completions 入口注入 tool prompt，解析非流式响应 |
| `patches/api/streaming.py` | 修改：缓冲流式输出，检测并转换工具调用为 SSE tool_calls 格式 |

## 已知限制

- 这是 **prompt 兼容层**，不是原生 tool calling。Kimi 模型通过 prompt 指令输出 XML 格式的工具调用，再由代理层解析转换。
- 复杂场景下模型偶尔可能输出格式不规范的 XML（概率较低）。
- 模型有时会在工具调用成功后产生"幻觉"回复（如声称文件不存在），这是模型层面的问题。
- 不支持 `tool_choice` 指定具体工具名（只支持 `"auto"` 和 `"required"`）。

## 兼容性

- 基于 [lorsque-sir/kimi2api](https://github.com/lorsque-sir/kimi2api) 测试
- 经过 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 真实 agent loop 验证
- 测试通过的工具：terminal、read_file、write_file、search_files、patch

## License

MIT
