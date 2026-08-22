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

运行本项目需要以下环境：

Python 3.10 或更高版本（运行后端）

Node.js 18.x 或 20.x LTS（安装 Codex CLI）

Codex CLI（执行免费生图，安装失败时降级到 0.119.0 版本）

ChatGPT Plus 或 Pro 账号（必需，用于 Codex CLI 免费生图）

Windows 10 或 11 操作系统（其他系统需自行适配）

4GB 以上内存（推荐 8GB）

能访问 chatgpt.com 和 pypi.org 的网络环境

无需独立显卡

如使用代理，请将 127.0.0.1 加入直连列表，避免拦截本地服务。
运行 pip install -r requirements.txt 自动安装所有 Python 依赖（fastapi、uvicorn、openpyxl、Pillow、requests）。
OpenAI API Key 为可选备选方案，使用 Codex CLI 免费生图无需额外付费。

## 首次安装

首次安装只需要做一次。下面两种方式二选一即可；如果已经让 AI 工具完成安装并启动，就不要再重复执行手动安装。

方式一：AI 工具安装并启动（当前仓库）

如果你要把项目发给别人使用，最简单的方式是直接发当前定制版 GitHub 仓库链接：

```text
https://github.com/Yan07yan/SKU-
```

请把下面这段话发给 Codex、Claude Code 等 AI 编程工具：

```text

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

```

方式二：手动安装（通用）

下载 ZIP 或 git clone https://github.com/Yan07yan/SKU-.git

双击 run.bat

浏览器访问 http://127.0.0.1:8765

如果遇到报错，参考仓库的 README。


## 安装后注意事项

一、首次安装配置

1. 安装并登录 Cockpit

打开 Cockpit 软件，在界面中添加并登录你的 ChatGPT Plus / Pro 账号，确保账号状态显示为“可用”或“在线”，并保持 Cockpit 在后台持续运行。

2. 启动本项目

双击项目目录下的 run.bat 文件，等待 CMD 窗口显示“SKU workflow dashboard running at http://127.0.0.1:8765”，然后在浏览器中打开该地址。

3. 配置 Codex 本地服务

点击网页右上角的“配置 Codex 本地服务”按钮，在弹出窗口中填写以下两项：

本地服务地址：填写 http://127.0.0.1: 后面加上 Cockpit 中显示的端口号，并在末尾加上 /v1，例如 http://127.0.0.1:62508/v1

Access Token（网站密匙）：填写 Cockpit 中“API服务”里的“旧密匙”

如何获取端口号和密匙：

打开 Cockpit，找到已登录的 ChatGPT Plus 账号，查看“本地服务地址”或“API 服务”面板，复制 localhost: 后面的数字作为端口号，再复制“旧密匙”或“Access Token”作为网站密匙。

配置完成后，网页右上角会显示“已认证”状态，即可开始使用。

二、Excel 表格要求

上传的 Excel 文件需包含以下列，表头名称必须完全一致。

用户填写区（输入）：

SPU（必填）：产品系列编号，用作文件夹名，例如 CK2608216

图片生成提示词（必填）：AI 生图描述，越详细效果越好

图片颜色（选填）：产品颜色名称，例如 红色、蓝色

图片命名（选填）：不填则程序自动生成，如 01_红色

是否添加品牌logo（选填）：填写“是”或“否”

logo文件地址（选填）：如填写“是”，则需填写 Logo 文件路径，例如 ./logo/brand.png

需校验品牌文字/元素（选填）：需校验的文字内容，例如 GREAT SWORD

程序自动回填区（输出，无需填写）：

优化后提示词：程序优化后的生图提示词

提示词原文快照：用户输入的提示词备份

生成状态：待生成 / 生成中 / 待确认 / 已通过 / 失败 / 拼图失败

失败原因：如失败则记录具体原因

生成时间：图片生成完成时间

输出文件地址：主图保存路径，例如 output/CK2608216/主图.png

Excel 导入后，程序会自动生成 9 张产品图和 1 张拼接主图，并回填所有输出字段。导出 Excel 时这些字段会自动填入，可直接用于 ERP 上架。

三、功能说明

顶部任务控制按钮：

开始生成：启动队列中所有待生成 SKU 的图片生成

暂停生成：暂停当前任务，当前图片完成后停止

继续生成：恢复被暂停的任务

停止生成：停止所有任务，未完成的回退为“待生成”

表格操作：

复选框：勾选单行或多行 SKU

全选：勾选表头复选框，一键全选或取消全选

生成图片：手动触发单个 SKU 的图片生成

重新拼图：九张单图存在但主图拼图失败时，点击重新执行拼图

下载图片：下载该 SKU 的全部图片（9张产品图 + 主图）的 ZIP 包

通过：确认图片无误，状态变为“已通过”

驳回：图片有问题，状态变为“已驳回”

批量操作：

批量下载：勾选多个 SKU，一键下载所有图片的 ZIP 包

删除选中：勾选多个 SKU，同时删除数据库记录和图片文件

预览与翻页：

点击表格任意行，右侧预览区显示该 SKU 的图片。点击 ◀ / ▶ 按钮可前后翻看 1 到 9 张产品图和主图，并显示当前进度，例如 1/10 · 1.png

导出 Excel：

点击“导出 Excel”按钮，程序自动回填生成状态、失败原因、生成时间、输出文件地址等字段，导出后可直接用于 ERP 上架。

四、常见问题

问题 1：生图报错 “503 - 本地接入集合暂无账号”

原因：Cockpit 未登录或账号不可用

解决方法：打开 Cockpit，确保 ChatGPT Plus 账号已登录且状态为“可用”

问题 2：生图报错 “401 - 未授权”

原因：Access Token 错误或过期

解决方法：重新从 Cockpit 复制“旧密匙”，更新配置

问题 3：报错 “连接失败 / 拒绝连接”

原因：Cockpit 未运行或端口不对

解决方法：保持 Cockpit 运行，核对本地服务地址端口号

问题 4：图片生成超时

原因：网络问题或额度用尽

解决方法：检查网络代理，查看 ChatGPT Plus 剩余额度

问题 5：拼图失败

原因：1 到 9 张图未全部生成

解决方法：点击“重新拼图”按钮重试

问题 6：缩略图不显示

原因：图片路径问题

解决方法：点击该行刷新预览，或重新生成

五、操作流程

第一步：打开 Cockpit，登录 ChatGPT Plus 账号，保持运行

第二步：双击 run.bat 启动项目，访问 http://127.0.0.1:8765

第三步：点击右上角“配置 Codex 本地服务”，填写地址和 Token

第四步：上传 Excel，程序自动生成 9 张图 + 主图

第五步：预览确认，点击“通过”或“驳回”

第六步：导出 Excel（含状态回填），导入 ERP 上架

六、联系支持

如有问题请联系项目负责人，并提供以下信息：

CMD 窗口中的完整报错信息（截图或文字）

浏览器 F12 控制台中的红色报错

Cockpit 中账号状态截图

祝你使用愉快！

