# write4print

`write4print` 是一个用于整理手写笔记 PDF 的桌面工具。

它会读取一个或多个 PDF，将页面渲染成灰度图后进行二值化、去噪、裁边和连续排版，最后输出适合 A4 打印的新 PDF。这个项目特别适合把扫描版手写笔记、平板导出的书写 PDF，重新整理成更紧凑、更省纸的打印稿。

## 功能特点

- 支持一次导入多个 PDF，并按列表顺序拼接处理
- 自动识别纸张背景与笔迹，生成黑白打印稿
- 自动裁掉页面外围空白
- 尝试按“内容片段”连续排版到 A4，减少浪费
- 可调渲染 DPI、输出 DPI、页边距
- 自动按页数、CPU 和可用内存选择处理并行度与 OpenCV 线程数
- 单页任务走直通处理，并自动跳过无收益的 GPU/OpenCL 调度开销
- 长任务显示实时百分比进度，并区分预处理、排版和保存阶段
- 提供简单图形界面，适合直接本地使用

## 运行环境

- Python 3.10+
- 支持 `tkinter` 的 Python 环境

说明：

- `tkinter` 通常随系统 Python 一起提供，不在 `requirements.txt` 中单独安装
- 如果你的 Linux 环境缺少 `tkinter`，需要通过系统包管理器安装，例如 Debian/Ubuntu 常见包名为 `python3-tk`

## 安装

先创建并激活虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate
```

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

## 启动方式

```bash
python main.py
```

启动后会打开图形界面，你可以：

1. 添加一个或多个输入 PDF
2. 调整文件顺序
3. 选择输出 PDF 路径
4. 设置参数后点击“开始转换”

## CI 与发布

仓库包含 GitHub Actions：

- `CI`：在 push、pull request 和手动触发时运行，覆盖 Windows/Linux 与多个 Python 版本，执行依赖安装、语法编译和模块导入检查。
- `Release`：在 GitHub Actions 页面手动触发，输入发布 tag（例如 `v1.0.0`）后，会自动构建 Windows 和 Linux 二进制包，并发布到 GitHub Release。

发布产物：

- Windows：`write4print.exe`，使用无控制台窗口模式打包，双击运行时不会额外弹出黑窗口。
- Linux：`write4print` 可执行文件，并附带 `write4print.desktop` 启动文件；通过桌面入口启动时不会打开终端窗口。

## 参数说明

- `渲染 DPI`：输入 PDF 转图片时的分辨率。越高越清晰，但越慢、内存占用越大。
- `输出 DPI`：最终 A4 页面图像分辨率。影响输出 PDF 体积与打印细节。
- `页边距(mm)`：A4 页面四周留白。
- `裁掉外围空白`：启用后会尽量去除原始页面四周无内容区域。

默认思路：

- 默认保留 `渲染 DPI=600`、`输出 DPI=600`
- 页边距建议 `24 mm`
- 如果更看重速度和输出体积，可手动降低到 `300 DPI`
- 并行度、OpenCV 线程和 GPU/OpenCL 不需要手动配置，程序会自动选择吞吐更稳的策略

## 处理流程

程序大致会执行以下步骤：

1. 用 PyMuPDF 将 PDF 页面直接渲染为灰度图
2. 用 OpenCV 做 Otsu 阈值，并在背景不均匀时自动叠加自适应阈值
3. 识别有效书写区域并裁掉外围空白
4. 按行带和片段拆分内容
5. 将内容连续铺排到 A4 页面，并边排版边写入输出 PDF
6. 导出新的打印版 PDF

## 项目结构

```text
.
├── main.py            # 主程序，包含 GUI 和转换逻辑
├── requirements.txt   # Python 依赖
└── README.md          # 项目说明
```

## 适用场景

- 扫描版课堂笔记整理打印
- 平板手写导出 PDF 的打印优化
- 多份讲义/笔记合并后统一排版打印

## 已知限制

- 当前主要提供 GUI 入口，没有单独封装命令行参数界面
- 输出本质上是图像型 PDF，不是可编辑文本 PDF
- 对极端低对比度、重阴影、强彩色背景页面，效果可能需要靠 DPI 参数微调

## 代码入口

如果你想在代码里直接调用，也可以使用：

- `convert_pdfs(input_pdfs, output_pdf, options, progress=None, progress_value=None)`
- `convert_pdf(input_pdf, output_pdf, options, progress=None, progress_value=None)`

其中 `options` 类型为 `ConvertOptions`。

`progress` 用于接收日志文本；`progress_value` 用于接收实时数值进度，签名为 `callback(fraction, message)`，其中 `fraction` 范围是 `0.0` 到 `1.0`。
