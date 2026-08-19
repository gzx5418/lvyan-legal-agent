# external/ 第三方资源目录

本目录存放通过 **git submodule** 引入的外部资源，父仓库本身不跟踪其数据
文件。`external/README.md` 是父仓库跟踪的占位文件，作用：

- 保证 `external/` 目录在 git 检出与 Docker 构建上下文中始终非空，
  使 `Dockerfile` 中 `COPY external/ ./external/` 在 submodule 未 init 时
  也能成功（仅缺少法条数据，应用运行时降级到精编知识库）。

## 官方法律全文库（lvyan-lawtext）

- submodule 路径：`external/lvyan-lawtext`（法规全文采集自 flk.npc.gov.cn）
- 首次检出：`git submodule update --init --recursive`
- 构建 Docker 镜像前**必须**先检出，否则镜像内无官方法条全文数据
  （构建不失败，但检索仅有精编知识库子集）。
- submodule 内部的 `.git` / `.gitmodules` 已被 `.dockerignore` 排除，
  不会进入镜像。
