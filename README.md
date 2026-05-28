# PPTAgent Render FastAPI

把本项目的“PDF 双语化流水线”封装成一个最小 FastAPI：上传触发 → 轮询状态/下载 PDF。

Assumption: no legacy data / consumers need compatibility.

## 接口

### 触发任务

- `POST /jobs`
- `multipart/form-data`
  - `file`: 上传的 `.pdf`

Query（可选）：
- `pipeline_mode`: `single_model` | `mixed_models`
- `page_start` / `page_end` / `dpi`
- `single_model`:
  - `model` / `thinking_budget`
- `mixed_models`:
  - `model_translate` / `model_image_desc` / `model_rest` / `thinking_budget_rest`

返回：
- `{ "job_id": "..." }`

### 轮询/下载

- `GET /jobs/{job_id}`
  - `logs_tail=200`: 返回 JSON 里附带最后 200 行日志
  - `download=1`: 任务完成后直接下载最终 PDF

## 环境变量

必须：
- `GEMINI_API_KEY`（Gemini/Gemma 都使用同一个 key；也兼容 `GOOGLE_API_KEY`）

可选（开启鉴权）：
- `PPTAGENT_API_TOKEN`（启用后所有请求要带 `Authorization: Bearer <token>`）

存储：
- `PPTAGENT_DATA_DIR`（Render 建议挂载到 persistent disk 的 mountPath，比如 `/app/result`）

## 本地运行（Docker）

```bash
docker build -t pptagent-api .
mkdir -p result
docker run --rm -p 10000:10000 \
  -e GEMINI_API_KEY="YOUR_KEY" \
  -e PPTAGENT_DATA_DIR="/app/result" \
  -v "$(pwd)/result:/app/result" \
  pptagent-api
```

## curl 示例

```bash
# 触发
curl -X POST "http://localhost:10000/jobs" -F "file=@/path/to/your.pdf"

# 轮询
curl "http://localhost:10000/jobs/<job_id>?logs_tail=200"

# 下载最终 PDF
curl -L "http://localhost:10000/jobs/<job_id>?download=1" --output out_repaired.pdf
```

## Render 部署

把本目录作为一个独立 repo push 到 GitHub，然后在 Render 用 Blueprint 指向本 repo（会读取 `render.yaml`）。

