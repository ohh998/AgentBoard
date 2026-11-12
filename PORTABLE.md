# ZIP 便携包使用说明

此 ZIP 是完整代码便携包，解压即可使用，**需要本机已有 Python 3.11+（含 sqlite3 标准模块）和现代浏览器**。不内置 Python，无需 pip、Node、Git 或互联网连接；本地启动后浏览器访问环回地址。

## 首次启动

1. 把整个 ZIP 解压到可写的本地目录，勿直接在压缩软件内运行，也不要只复制其中的 Python 文件。保留 `web/` 等相对目录。
2. Windows 双击 `start.cmd`。如果找不到 `python`，先配置已有 Python 到 PATH；也可在解压目录终端运行 `py -3 -S -m agentboard --open`（有 Python Launcher 时）。
3. Linux / macOS 在解压目录运行 `sh ./start.sh`，或 `python3 -S -m agentboard --open`。
4. 默认浏览器自动打开并登录 `http://127.0.0.1:8765`。保持终端运行；按 Ctrl+C 停止。不能自动打开时，手工访问该地址，使用新生成的 `.agentboard/admin.token` 登录。

首次运行在解压目录创建 `.agentboard/`，保存新账本、任务 Markdown 和私有凭据。新 agent 按 [AGENTS.md](AGENTS.md) 接入。参数可附在启动脚本后，例如 `start.cmd --port 8877`。

可以先运行 `python -S -c "import sqlite3; print(sqlite3.sqlite_version)"` 检查 SQLite 支持。Python 版本通过 `python --version` 检查；Linux/macOS 将命令中的 python 换成 python3。

## 包内内容与完整性

包含网页、服务端、CLI、启动脚本、模板、测试和全部维护文档；不携带 `.git` 历史、既有任务、身份、令牌、数据库、缓存或日志，也不包含 Python/虚拟环境。

`RELEASE-MANIFEST.json` 记录版本、源代码提交号及每个源文件的 SHA256/大小（不递归记录清单本身）。ZIP 旁的 `.zip.sha256` 记录整个 ZIP 的 SHA256，可用 PowerShell `Get-FileHash 文件名.zip -Algorithm SHA256` 或 Linux/macOS `shasum -a 256 文件名.zip` 核对。校验用于检测损坏，不能代替可信的分发来源。

这是代码发行包；需要迁移旧任务时，应先停止旧服务和参与者，再单独受保护地备份/恢复完整数据目录，见 [维护手册](docs/MAINTENANCE.md)。不能让两套服务和账本同时协调同一工作区。升级时解压到新目录，保留旧目录并按备份流程恢复，不直接覆盖运行中的账本。

## 维护与重新打包

运行应用本身不需要 Git；维护解压后的代码可先初始化自己的 Git 仓库。当前发布的来源提交号保存在清单中，原始 Git 历史未包含在 ZIP。

在有已提交源代码的 Git checkout 内执行：

```powershell
python -S scripts/package_release.py
```

默认生成 `releases/agentboard-VERSION-portable.zip` 及校验文件；构建脚本只读取已提交树并检查发布白名单。存在未提交的受跟踪修改时拒绝打包。重复同版本打包需显式 `--force`。完整验证可在独立解压目录运行 `python -S -m unittest discover -s tests -v`。
