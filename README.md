# SKU 图片工作流1.0

本地网页工作流，覆盖第一版范围：商品资料入库、素材生成与标准化、校验、状态回写、Excel 标准化导出。

## 启动

双击 `run.bat`，浏览器打开：

```text
http://127.0.0.1:8765
```

## 核心约定

- `tasks.db` 是唯一状态源，Excel 只负责批量导入和标准化导出。
- 图片输出到 `output/{SKU}/main.png`。
- Excel 导出在 `exports/`，同时网页会直接下载。
- 生图通过 `codex exec` + `$imagegen` 串行执行，并发数固定为 1。
- `failed` 表示真失败，可重试。
- `quota_exhausted` 表示额度或频率限制，任务保留在 SQLite，网页显示“加入待续跑队列”。

## Excel 导入识别

程序会全量读取 Excel 第一张表的所有列，并优先识别这些字段：

- SKU：`SKU`、`sku`、`SPU`、`货号`、`商品编码`
- 商品名称：`商品名称`、`产品名称`、`标题`、`品名`、`Name`
- 生图提示词：`生图提示词`、`Prompt`、`prompt`、`提示词`

其他列会完整保存在数据库的原始资料 JSON 中，后续可以继续扩展 ERP 映射。

## 环境要求
必需环境
依赖	版本	用途
Python	3.10+	运行后端服务
Node.js	18.x 或 20.x LTS	安装 Codex CLI（生图工具）
Codex CLI	最新版（失败时用 0.119.0）	调用 ChatGPT Plus 免费生图
Git（可选）	最新版	如使用 git clone 下载代码
Python 依赖（自动安装）
运行 pip install -r requirements.txt 安装：
fastapi + uvicorn（Web 服务）
openpyxl（Excel 读写）
Pillow（拼图/Logo）
requests（API 调用）

账号要求
ChatGPT Plus / Pro 账号（必需，用于 Codex CLI 免费生图）
OpenAI API Key（可选，备选方案）

硬件与网络
系统：Windows 10/11
内存：4GB+（推荐 8GB）
网络：能访问 chatgpt.com 和 pypi.org
显卡：不需要独立显卡

## 首次安装
首次安装只需要做一次。下面两种方式二选一即可；如果已经让 AI 工具完成安装并启动，就不要再重复执行手动安装。

方式一：AI 工具安装并启动（当前仓库）
如果你要把项目发给别人使用，最简单的方式是直接发当前定制版 GitHub 仓库链接：
https://github.com/Yan07yan/SKU-

请把下面这段话发给 Codex、Claude Code 等 AI 编程工具：
请把这个 GitHub 项目安装到我的本地电脑并启动：
https://github.com/Yan07yan/SKU-
要求：
先确认本机已安装 Python 3.10 或更新版本。打开 CMD，运行 python --version 检查。如果未安装，请从 https://www.python.org/downloads/ 下载安装，安装时必须勾选 Add Python to PATH。
获取项目代码：
如果本地还没有项目，运行 git clone https://github.com/Yan07yan/SKU-.git 克隆仓库。
如果已经下载 ZIP 或源码文件夹，直接进入现有项目目录，不要重复下载。
进入项目目录后，运行以下命令安装 Python 依赖：
pip install -r requirements.txt
如果提示 pip 找不到，用 python -m pip install -r requirements.txt。
本项目通过 Codex CLI 调用 ChatGPT Plus 会员额度生图（免费）。请确认本机已安装 Node.js 18 或更新版本（因为 Codex CLI 依赖 npm），然后安装 Codex CLI：
npm install -g @openai/codex@latest
如果安装报错 Missing optional dependency @openai/codex-win32-x64，改用旧版本：
npm uninstall -g @openai/codex
npm install -g @openai/codex@0.119.0
安装完成后登录 ChatGPT Plus 账号：
codex login
启动项目：
Windows 用户：直接双击项目目录下的 run.bat 即可启动。
或打开 CMD，进入项目目录，运行：
python app.py
启动成功后，告诉我浏览器应该打开哪个本地地址。根据项目 README，启动后访问 http://127.0.0.1:8765。

方式二：手动安装（通用）
下载 ZIP 或 git clone https://github.com/Yan07yan/SKU-.git
双击 run.bat
浏览器访问 http://127.0.0.1:8765
如果遇到报错，参考仓库的 README。

## 首次启动
第一步：安装依赖
在项目目录下打开 CMD，执行：
cmd
pip install -r requirements.txt
第二步：登录 Codex CLI（免费生图必需）
cmd
npm install -g @openai/codex@latest
codex login
按提示在浏览器中登录 ChatGPT Plus 账号并授权。
第三步：启动服务
双击项目目录下的 run.bat 即可。

## 日常启动
直接双击 run.bat，看到以下提示即表示成功：
text
SKU workflow dashboard running at http://127.0.0.1:8765
浏览器访问：http://127.0.0.1:8765
停止服务
直接关闭 CMD 窗口即可。

