# 分析工坊 · 数据分析与数据应用平台

面向指标口径管理、多维度聚合查询、用户分群圈选、AB实验评估、时序趋势分析与数据服务发布的数据分析与数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 业务指标 | 指标编号、指标名称、计算公式 |
| 维度注册 | `dim_registry` | 分析维度 | 维度编号、维度名称、业务键 |
| 聚合任务 | `agg_task` | 聚合计算 | 任务编号、目标指标、聚合维度 |
| 多维立方体 | `cube_builder` | 数据立方体 | 立方体编号、事实来源、维度列表 |
| 分群规则 | `cohort_rule` | 用户分群 | 分群编号、分群名称、筛选条件 |
| 实验管理 | `experiment` | AB实验 | 实验编号、实验假设、分组方案 |
| 时序分析 | `trend_series` | 时序序列 | 序列编号、观测指标、时间粒度 |
| 归因模型 | `attribution` | 归因链路 | 链路编号、归因窗口、触点类型 |
| 数据分层 | `segmentation` | 数据切片 | 切片编号、源数据集、切分维度 |
| 报表生成 | `report_gen` | 分析报表 | 报表编号、报表类型、数据范围 |
| 预测分析 | `forecast` | 预测方案 | 方案编号、预测指标、预测算法 |
| 数据集市 | `data_mart` | 分析主题 | 主题编号、主题名称、业务域 |
| 统计建模 | `stat_model` | 统计模型 | 模型编号、模型类型、因变量 |
| 数据提取 | `data_extract` | 提取任务 | 任务编号、来源数据集、筛选条件 |
| 质量监控 | `quality_mon` | 监控规则 | 规则编号、监控对象、校验类型 |
| 对比分析 | `compare_job` | 对比任务 | 任务编号、基准数据、对比数据 |
| 数据服务 | `api_service` | 服务接口 | 接口编号、查询数据集、请求参数 |
| 调度编排 | `schedule_plan` | 调度计划 | 计划编号、关联任务、Cron表达式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## AB实验就绪校验与试投排名

实验统计不再直接拿两行数据做检验，而是先按观测期校验两组样本，再决定能否发布。

- **按组门槛**：`实验管理` 的 `最小样本` 按组分计，对照组和实验组**各自**达到门槛，
  本期才就绪；未就绪只输出缺数组别与样本缺口，不发布提升量或显著性。
- **观测期隔离**：采集批次以「实验 × 观测期 × 分组」为单位登记。一次检验只使用同一个
  观测期的两组记录；下一观测期迟到的批次留在自己的观测期里，不并入本期。
- **复盘区分两种结论**：`样本未到齐`（未达门槛，没有检验结论）与
  `已检验-不显著`（样本达标、检验完成且不显著）是两个不同状态。
- **集市汇总**：统计管道把就绪状态同步到 `data_mart`，按期给有效转化率与提升量；
  未就绪的实验保留缺数组别行，`可参与排名=false`、不带提升量。
- **试投排名**：只对本期已就绪且已同步集市的实验按提升量排名，未参加排名的实验和原因
  单独列出，并给出覆盖比例。

主要接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/experiment-readiness/readiness` | 就绪数据集（按组样本、缺数组别、本期观测期） |
| `POST /api/experiment-readiness/observations` | 登记采集批次（同批次号覆盖不累加） |
| `GET /api/experiment-readiness/observations` | 查询采集批次 |
| `POST /api/experiment-readiness/sync` | 校验并同步集市（同一实验同一观测期幂等） |
| `GET /api/experiment-readiness/review/{实验编号}` | 复盘记录，区分样本未到齐与已检验不显著 |
| `GET /api/experiment-readiness/ranking` | 试投排名、可安排名单与未参加排名的实验 |
| `POST /api/experiment/{id}/min-sample` | 调整按组最小样本门槛 |
| `GET /api/experiment-mart` | 集市按期汇总（有效转化率、提升量、缺数组别） |
| `GET /api/experiment-mart/coverage` | 排名覆盖与未参加排名的原因 |
| `POST /api/pipeline/experiment-stat` | 统计管道；不传实验编号时同步全部参与分析的实验 |


领域：数据工程
子方向：数据分析与数据应用
