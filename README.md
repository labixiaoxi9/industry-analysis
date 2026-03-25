# Industry Analysis API (Standalone)

一个独立于原项目的 FastAPI 服务，提供：

1. 后端代理接口：`/api/industry-analysis`
2. 前端页面：`/`（简化商务风资讯页）

## 1. 安装依赖

```bash
pip install -r requirements.txt
```

## 2. 配置环境变量

复制 `.env.example` 为 `.env`，并填写真实 Token：

- `UPSTREAM_BASE_URL`：上游服务地址
- `UPSTREAM_PATH`：上游接口路径
- `UPSTREAM_TOKEN`：鉴权 Token
- `UPSTREAM_TIMEOUT`：超时秒数

## 3. 启动服务

```bash
uvicorn main:app --host 0.0.0.0 --port 8008 --reload
```

## 4. 使用说明

- 页面地址：`http://127.0.0.1:8008/`
- 健康检查：`http://127.0.0.1:8008/health`
- 代理接口：

```text
GET /api/industry-analysis?keyword=宠物&page=1&page_size=10
```

## 5. 备注

当前页面对上游返回结构做了兼容解析（`data.data` / `data.items` / `data.list`），
如果你的上游字段不同，可在 `templates/index.html` 的 `normalizeItems` 和渲染字段中微调。
