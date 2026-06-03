# Z3r0 快速开始

<p>
  <a href="QUICKSTART.md">English</a> ·
  <strong>中文</strong> ·
  <a href="README_zh.md">README</a>
</p>

本文档用于在受控、授权环境中通过 Docker Compose 启动 Z3r0。将服务暴露到网络前，请先审查运行配置。

## 前置条件

- Docker Engine 与 Docker Compose
- 运行 Z3r0 的主机具备 Docker socket 访问权限
- 可用于当前 Agent 模型配置的兼容模型服务

## 1. 克隆仓库

```bash
git clone https://github.com/yv1ing/Z3r0.git
cd Z3r0
```

## 2. 创建运行配置

```bash
cp .z3r0/config.json.example .z3r0/config.json
```

启动服务前，请编辑 `.z3r0/config.json`：

- 将 `system.encrypt_key` 设置为至少 32 个字符的随机值。
- 为 `system.bootstrap_admin.password` 设置强密码。
- 检查 `database` 配置。生产 Compose 文件会按示例中的默认值启动 PostgreSQL。
- 为 `agents` 下的每个 Agent 配置预期的 `base_url`、`api_key`、`model` 和 `context_window`。

## 3. 启动 Z3r0

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

应用默认监听：

```text
http://127.0.0.1:8000
```

使用 `.z3r0/config.json` 中配置的 bootstrap 管理员账号登录。

## 4. 验证部署

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f app
```

后端会在启动阶段创建数据库表，并从同一个应用容器中提供已构建的 React 工作台。

## 运行注意事项

- Z3r0 面向经过授权的安全测试、红队演练、代码审计、研究和教学环境。
- 应用容器会挂载 `/var/run/docker.sock` 以管理沙箱容器。请将宿主机视为可信、隔离环境。
- 模型凭据、终端访问、文件管理和沙箱容器均属于高权限资产。
- 在本地评估环境之外部署前，请修改 `docker-compose.prod.yml` 中 PostgreSQL 与 pgAdmin 的默认配置。
