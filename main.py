from __future__ import annotations

import math
import multiprocessing
import os
import queue
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Sequence, Tuple

import fitz  # PyMuPDF
import cv2
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


A4_W_PT = 595.2755905511812
A4_H_PT = 841.8897637795277


@dataclass
class ConvertOptions:
    render_dpi: int = 600
    output_dpi: int = 600
    margin_mm: float = 24.0
    crop_whitespace: bool = True
    worker_count: int = 0
    opencv_threads: int = 1
    use_gpu_acceleration: bool = False


@dataclass(frozen=True)
class PageJob:
    order_idx: int
    doc_idx: int
    doc_count: int
    pdf_path: str
    page_idx: int
    doc_pages: int


@dataclass
class ProcessedPage:
    order_idx: int
    doc_idx: int
    doc_count: int
    pdf_path: str
    page_idx: int
    doc_pages: int
    binary: np.ndarray


def mm_to_px(mm: float, dpi: int) -> int:
    return max(0, int(round(mm / 25.4 * dpi)))


def _configure_cv2_runtime(options: ConvertOptions) -> str:
    try:
        cv2.setUseOptimized(True)
    except Exception:
        pass

    if options.opencv_threads > 0:
        try:
            cv2.setNumThreads(options.opencv_threads)
        except Exception:
            pass

    if not options.use_gpu_acceleration:
        try:
            cv2.ocl.setUseOpenCL(False)
        except Exception:
            pass
        return "GPU/OpenCL：关闭"

    opencl_available = False
    try:
        opencl_available = bool(cv2.ocl.haveOpenCL())
        cv2.ocl.setUseOpenCL(opencl_available)
    except Exception:
        opencl_available = False

    cuda_devices = 0
    try:
        if hasattr(cv2, "cuda"):
            cuda_devices = int(cv2.cuda.getCudaEnabledDeviceCount())
    except Exception:
        cuda_devices = 0

    if opencl_available:
        return "GPU/OpenCL：已启用 OpenCV OpenCL 加速"
    if cuda_devices > 0:
        return "GPU/OpenCL：检测到 CUDA 设备，但当前 OpenCV 未启用 CUDA 算子，已回退 CPU"
    return "GPU/OpenCL：当前环境不可用，已回退 CPU"


def pdf_page_to_gray(page: fitz.Page, dpi: int) -> np.ndarray:
    scale = dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8)
    arr = arr.reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        gray = arr.copy()
    else:
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    return gray


# =========================
# 二值化与裁边
# =========================
def _otsu_polarity(gray: np.ndarray) -> Tuple[int, bool]:
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    th, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    border = np.concatenate(
        [
            gray[0, :],
            gray[-1, :],
            gray[:, 0],
            gray[:, -1],
        ]
    )
    border_mean = float(border.mean()) if border.size else float(gray.mean())

    # 边缘通常更接近背景；若不明显，再回退到“大类即背景”的假设。
    if abs(border_mean - th) >= 8:
        background_is_white = border_mean > th
    else:
        low = int((blur <= th).sum())
        high = int((blur > th).sum())
        background_is_white = high >= low

    return int(th), background_is_white


def _remove_tiny_components(binary: np.ndarray) -> np.ndarray:
    # binary: 255=背景, 0=笔迹
    inv = 255 - binary
    num, labels, stats, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    if num <= 1:
        return binary

    h, w = binary.shape
    min_area = max(2, int(round(h * w * 0.000002)))
    keep = np.zeros_like(inv)
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area >= min_area:
            keep[labels == i] = 255
    return 255 - keep


def binarize_handwriting(gray: np.ndarray) -> np.ndarray:
    gray = np.ascontiguousarray(gray)
    th, background_is_white = _otsu_polarity(gray)

    mode = cv2.THRESH_BINARY if background_is_white else cv2.THRESH_BINARY_INV
    _, global_bin = cv2.threshold(gray, th, 255, mode)

    # 自适应阈值用于处理局部亮度不均。保持同一极性：255 背景，0 笔迹。
    block = max(31, (min(gray.shape[:2]) // 20) | 1)
    if block % 2 == 0:
        block += 1
    adaptive = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        mode,
        block,
        15,
    )

    # 取“更保守”的交集，减少灰边与阴影进入前景。
    binary = cv2.bitwise_or(global_bin, adaptive)

    # 轻微闭运算，修补断裂；再次阈值确保纯黑白。
    kernel = np.ones((2, 2), dtype=np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=1)
    _, binary = cv2.threshold(binary, 127, 255, cv2.THRESH_BINARY)
    binary = _remove_tiny_components(binary)
    return binary


def crop_whitespace(binary: np.ndarray, pad_px: int = 8) -> np.ndarray:
    ink = np.where(binary < 250)
    if len(ink[0]) == 0:
        return binary

    y0 = max(0, int(ink[0].min()) - pad_px)
    y1 = min(binary.shape[0], int(ink[0].max()) + 1 + pad_px)
    x0 = max(0, int(ink[1].min()) - pad_px)
    x1 = min(binary.shape[1], int(ink[1].max()) + 1 + pad_px)
    return binary[y0:y1, x0:x1].copy()


# =========================
# 行/片段分析
# =========================
def row_ink_count(binary: np.ndarray) -> np.ndarray:
    return np.count_nonzero(binary < 250, axis=1)


def detect_bands(binary: np.ndarray) -> List[Tuple[int, int]]:
    h, w = binary.shape
    counts = row_ink_count(binary)
    threshold = max(2, int(round(w * 0.002)))
    active = counts >= threshold

    bands: List[Tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(active):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            bands.append((start, i))
            start = None
    if start is not None:
        bands.append((start, h))

    if not bands:
        return []

    # 合并非常近的带，避免同一行被误切开。
    merged: List[Tuple[int, int]] = [bands[0]]
    merge_gap = max(2, int(round(h * 0.002)))
    for y0, y1 in bands[1:]:
        py0, py1 = merged[-1]
        if y0 - py1 <= merge_gap:
            merged[-1] = (py0, y1)
        else:
            merged.append((y0, y1))

    # 过滤极细小噪声带。
    filtered: List[Tuple[int, int]] = []
    for y0, y1 in merged:
        band_h = y1 - y0
        band_max = int(counts[y0:y1].max()) if y1 > y0 else 0
        if band_h <= 1 and band_max < max(4, threshold * 2):
            continue
        filtered.append((y0, y1))
    return filtered


def best_cut_near_target(counts: np.ndarray, lo: int, hi: int, target: int) -> int:
    lo = max(0, lo)
    hi = min(len(counts), hi)
    if hi - lo <= 1:
        return min(max(target, lo), hi)

    segment = counts[lo:hi]
    min_value = int(segment.min())
    candidates = np.where(segment == min_value)[0] + lo
    if len(candidates) == 0:
        return min(max(target, lo), hi)
    return int(candidates[np.argmin(np.abs(candidates - target))])


def split_band_to_fit(
    binary: np.ndarray,
    band: Tuple[int, int],
    max_src_h: int,
) -> List[np.ndarray]:
    y0, y1 = band
    if y1 - y0 <= max_src_h:
        return [binary[y0:y1, :]]

    counts = row_ink_count(binary)
    parts: List[np.ndarray] = []
    cur = y0
    while cur < y1:
        if cur + max_src_h >= y1:
            parts.append(binary[cur:y1, :])
            break

        target = cur + max_src_h
        lo = cur + int(max_src_h * 0.72)
        hi = min(y1, cur + int(max_src_h * 1.10))
        cut = best_cut_near_target(counts, lo=lo, hi=hi, target=target)
        if cut <= cur + 10:
            cut = min(y1, target)
        parts.append(binary[cur:cut, :])
        cur = cut

    return [p for p in parts if p.size > 0]


def iter_smart_fragments(
    binary: np.ndarray,
    content_w_px: int,
    content_h_px: int,
) -> Iterable[Tuple[str, object, int]]:
    h, w = binary.shape
    if h == 0 or w == 0:
        return

    scale = content_w_px / float(w)
    max_src_h = max(32, int(math.floor(content_h_px / scale)))
    bands = detect_bands(binary)

    if not bands:
        return

    prev_end = 0
    for band in bands:
        y0, y1 = band
        gap = y0 - prev_end
        if gap > 0:
            yield ("gap", int(gap), w)
        for frag in split_band_to_fit(binary, band, max_src_h=max_src_h):
            yield ("fragment", frag, w)
        prev_end = y1

    tail_gap = h - prev_end
    if tail_gap > 0:
        yield ("gap", int(tail_gap), w)


# =========================
# 连续排版到 A4
# =========================
class ContinuousPaginator:
    def __init__(self, a4_w_px: int, a4_h_px: int, margin_px: int):
        self.a4_w_px = a4_w_px
        self.a4_h_px = a4_h_px
        self.margin_px = margin_px
        self.content_w_px = a4_w_px - 2 * margin_px
        self.content_h_px = a4_h_px - 2 * margin_px
        if self.content_w_px <= 0 or self.content_h_px <= 0:
            raise ValueError("页边距过大，导致 A4 可用区域为 0")

        self.pages: List[np.ndarray] = []
        self._reset_current_page()

    def _reset_current_page(self) -> None:
        self.current = np.full((self.a4_h_px, self.a4_w_px), 255, dtype=np.uint8)
        self.cursor_y = self.margin_px
        self.page_has_content = False

    @property
    def remaining_h(self) -> int:
        return self.margin_px + self.content_h_px - self.cursor_y

    def _flush_current_page(self) -> None:
        if self.page_has_content:
            self.pages.append(self.current.copy())
        self._reset_current_page()

    def add_gap(self, src_rows: int, src_width: int, max_rows: int = 0) -> None:
        if src_rows <= 0 or src_width <= 0:
            return
        scaled = int(round(src_rows * self.content_w_px / float(src_width)))
        if max_rows > 0:
            scaled = min(scaled, max_rows)
        if scaled <= 0:
            return

        while scaled > 0:
            rem = self.remaining_h
            if rem <= 0:
                self._flush_current_page()
                rem = self.remaining_h

            step = min(scaled, rem)
            if self.page_has_content:
                self.cursor_y += step
            # 若当前页尚无内容，则忽略页首空白，不人为下移。
            scaled -= step

            if scaled > 0:
                self._flush_current_page()

    def _resize_fragment(self, fragment: np.ndarray) -> np.ndarray:
        scale = self.content_w_px / float(fragment.shape[1])
        resized_h = max(1, int(round(fragment.shape[0] * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        resized = cv2.resize(fragment, (self.content_w_px, resized_h), interpolation=interp)
        _, resized = cv2.threshold(resized, 127, 255, cv2.THRESH_BINARY)
        return resized

    def _blit_rows(self, img: np.ndarray, y0: int, y1: int) -> None:
        rows = y1 - y0
        if rows <= 0:
            return
        dest_y0 = self.cursor_y
        dest_y1 = dest_y0 + rows
        self.current[dest_y0:dest_y1, self.margin_px : self.margin_px + self.content_w_px] = img[y0:y1, :]
        self.cursor_y = dest_y1
        self.page_has_content = True

    def add_fragment(self, fragment: np.ndarray) -> None:
        if fragment.size == 0:
            return

        resized = self._resize_fragment(fragment)
        h = resized.shape[0]

        if h <= self.remaining_h:
            self._blit_rows(resized, 0, h)
            return

        if h <= self.content_h_px:
            if self.page_has_content:
                self._flush_current_page()
            self._blit_rows(resized, 0, h)
            return

        # 兜底：若单块仍高于一整页，则在片段内部继续智能拆分。
        scale = self.content_w_px / float(fragment.shape[1])
        max_src_h = max(32, int(math.floor(self.content_h_px / scale)))
        counts = row_ink_count(fragment)
        start = 0
        src_h = fragment.shape[0]
        while start < src_h:
            if start + max_src_h >= src_h:
                sub = fragment[start:src_h, :]
                self.add_fragment(sub)
                break
            target = start + max_src_h
            lo = start + int(max_src_h * 0.72)
            hi = min(src_h, start + int(max_src_h * 1.10))
            cut = best_cut_near_target(counts, lo=lo, hi=hi, target=target)
            if cut <= start + 10:
                cut = min(src_h, target)
            sub = fragment[start:cut, :]
            if self.page_has_content:
                self._flush_current_page()
            self.add_fragment(sub)
            start = cut

    def finalize_to_pdf(self, out_doc: fitz.Document) -> int:
        if self.page_has_content:
            self.pages.append(self.current.copy())
            self._reset_current_page()

        for page_img in self.pages:
            add_image_page_to_pdf(out_doc, page_img)
        return len(self.pages)


def add_image_page_to_pdf(out_doc: fitz.Document, gray_page: np.ndarray) -> None:
    success, encoded = cv2.imencode(".png", gray_page)
    if not success:
        raise RuntimeError("PNG 编码失败")
    page = out_doc.new_page(width=A4_W_PT, height=A4_H_PT)
    page.insert_image(page.rect, stream=encoded.tobytes())


def _process_pdf_page(job: PageJob, options: ConvertOptions) -> ProcessedPage:
    _configure_cv2_runtime(options)
    with fitz.open(job.pdf_path) as src:
        page = src.load_page(job.page_idx - 1)
        gray = pdf_page_to_gray(page, dpi=options.render_dpi)

    binary = binarize_handwriting(gray)
    if options.crop_whitespace:
        binary = crop_whitespace(binary, pad_px=max(6, options.render_dpi // 30))

    return ProcessedPage(
        order_idx=job.order_idx,
        doc_idx=job.doc_idx,
        doc_count=job.doc_count,
        pdf_path=job.pdf_path,
        page_idx=job.page_idx,
        doc_pages=job.doc_pages,
        binary=binary,
    )


def _effective_worker_count(options: ConvertOptions, total_pages: int) -> int:
    if total_pages <= 1:
        return 1
    if options.worker_count > 0:
        return max(1, min(options.worker_count, total_pages))

    cpu_count = os.cpu_count() or 1
    return max(1, min(total_pages, 4, max(1, cpu_count - 1)))


def _paginate_processed_page(
    processed: ProcessedPage,
    paginator: ContinuousPaginator,
    content_w_px: int,
    content_h_px: int,
) -> int:
    binary = processed.binary
    local_fragment_count = 0
    gap_cap = max(0, int(round(content_h_px * 0.015)))
    for kind, payload, src_width in iter_smart_fragments(binary, content_w_px, content_h_px):
        if kind == "gap":
            paginator.add_gap(int(payload), src_width, max_rows=gap_cap)
        else:
            paginator.add_fragment(payload)  # type: ignore[arg-type]
            local_fragment_count += 1

    # 仅加入很小的源页/文档间留白，但不强制换页。
    if processed.page_idx != processed.doc_pages:
        paginator.add_gap(max(1, binary.shape[0] // 120), binary.shape[1], max_rows=gap_cap)
    elif processed.doc_idx != processed.doc_count:
        paginator.add_gap(max(1, binary.shape[0] // 80), binary.shape[1], max_rows=max(2, gap_cap))

    return local_fragment_count


# =========================
# 转换主逻辑
# =========================
def convert_pdfs(
    input_pdfs: Sequence[str],
    output_pdf: str,
    options: ConvertOptions,
    progress: Callable[[str], None] | None = None,
    progress_value: Callable[[float, str], None] | None = None,
) -> None:
    if not input_pdfs:
        raise ValueError("未提供输入 PDF")

    def log(msg: str) -> None:
        if progress:
            progress(msg)

    def report_progress(fraction: float, msg: str) -> None:
        if progress_value:
            progress_value(min(1.0, max(0.0, fraction)), msg)

    input_pdfs = [str(p) for p in input_pdfs]
    output_pdf = str(output_pdf)

    a4_w_px = int(round(A4_W_PT / 72.0 * options.output_dpi))
    a4_h_px = int(round(A4_H_PT / 72.0 * options.output_dpi))
    margin_px = mm_to_px(options.margin_mm, options.output_dpi)
    content_w_px = a4_w_px - 2 * margin_px
    content_h_px = a4_h_px - 2 * margin_px
    if content_w_px <= 0 or content_h_px <= 0:
        raise ValueError("页边距过大，导致 A4 可用区域为 0")

    paginator = ContinuousPaginator(a4_w_px=a4_w_px, a4_h_px=a4_h_px, margin_px=margin_px)
    out = fitz.open()

    total_input_pages = 0
    jobs: List[PageJob] = []
    for doc_idx, pdf_path in enumerate(input_pdfs, start=1):
        with fitz.open(pdf_path) as src:
            doc_pages = len(src)
            for page_idx in range(1, doc_pages + 1):
                jobs.append(
                    PageJob(
                        order_idx=total_input_pages,
                        doc_idx=doc_idx,
                        doc_count=len(input_pdfs),
                        pdf_path=pdf_path,
                        page_idx=page_idx,
                        doc_pages=doc_pages,
                    )
                )
                total_input_pages += 1

    worker_count = _effective_worker_count(options, total_input_pages)
    runtime_status = _configure_cv2_runtime(options)
    preprocessed_pages = 0
    paginated_pages = 0

    def report_page_progress(stage: str) -> None:
        if total_input_pages <= 0:
            report_progress(0.0, stage)
            return
        fraction = (preprocessed_pages * 0.65 + paginated_pages * 0.30) / total_input_pages
        report_progress(
            fraction,
            f"{stage}（预处理 {preprocessed_pages}/{total_input_pages}，排版 {paginated_pages}/{total_input_pages}）",
        )

    log(
        f"运行配置：页级并行={worker_count}，OpenCV线程/进程={max(1, options.opencv_threads)}，"
        f"{runtime_status}。"
    )
    report_page_progress("准备处理")

    try:
        if worker_count <= 1:
            for job in jobs:
                report_page_progress(f"正在预处理第 {job.order_idx + 1}/{total_input_pages} 页")
                log(
                    f"正在处理第 {job.order_idx + 1}/{total_input_pages} 个输入页 "
                    f"（文档 {job.doc_idx}/{job.doc_count}，页 {job.page_idx}/{job.doc_pages}）：渲染与识别中…"
                )
                processed = _process_pdf_page(job, options)
                preprocessed_pages += 1
                report_page_progress(f"正在排版第 {job.order_idx + 1}/{total_input_pages} 页")
                log("连续排版中…")
                local_fragment_count = _paginate_processed_page(processed, paginator, content_w_px, content_h_px)
                paginated_pages += 1
                log(
                    f"当前输入页完成，已抽取 {local_fragment_count} 个内容片段；"
                    f"当前已写满 {len(paginator.pages)} 张整页 A4。"
                )
                report_page_progress(f"已完成第 {job.order_idx + 1}/{total_input_pages} 页")
        else:
            next_submit = 0
            next_emit = 0
            completed: dict[int, ProcessedPage] = {}
            pending: dict[Future[ProcessedPage], PageJob] = {}
            max_pending = max(1, worker_count * 2)

            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                while next_emit < total_input_pages:
                    while next_submit < total_input_pages and len(pending) < max_pending:
                        job = jobs[next_submit]
                        log(
                            f"提交第 {job.order_idx + 1}/{total_input_pages} 个输入页 "
                            f"（文档 {job.doc_idx}/{job.doc_count}，页 {job.page_idx}/{job.doc_pages}）：并行渲染与识别中…"
                        )
                        pending[executor.submit(_process_pdf_page, job, options)] = job
                        next_submit += 1
                    report_page_progress(f"已提交 {next_submit}/{total_input_pages} 页")

                    if next_emit not in completed:
                        done, _ = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            job = pending.pop(future)
                            processed = future.result()
                            completed[processed.order_idx] = processed
                            preprocessed_pages += 1
                            log(
                                f"预处理完成第 {job.order_idx + 1}/{total_input_pages} 个输入页 "
                                f"（文档 {job.doc_idx}/{job.doc_count}，页 {job.page_idx}/{job.doc_pages}）。"
                            )
                        report_page_progress("并行预处理进行中")

                    while next_emit in completed:
                        processed = completed.pop(next_emit)
                        report_page_progress(f"正在排版第 {processed.order_idx + 1}/{total_input_pages} 页")
                        log(
                            f"按顺序排版第 {processed.order_idx + 1}/{total_input_pages} 个输入页 "
                            f"（文档 {processed.doc_idx}/{processed.doc_count}，页 {processed.page_idx}/{processed.doc_pages}）…"
                        )
                        local_fragment_count = _paginate_processed_page(
                            processed,
                            paginator,
                            content_w_px,
                            content_h_px,
                        )
                        log(
                            f"当前输入页完成，已抽取 {local_fragment_count} 个内容片段；"
                            f"当前已写满 {len(paginator.pages)} 张整页 A4。"
                        )
                        paginated_pages += 1
                        next_emit += 1
                        report_page_progress(f"已完成第 {processed.order_idx + 1}/{total_input_pages} 页")

        report_progress(0.96, "正在生成输出 PDF 页面…")
        out_count = paginator.finalize_to_pdf(out)
        if out_count <= 0:
            raise RuntimeError("没有生成任何输出页")

        report_progress(0.98, "正在保存输出 PDF…")
        out.save(output_pdf, deflate=True, garbage=3)
        report_progress(1.0, "转换完成")
        log(f"完成：共生成 {out_count} 张 A4，输出文件已保存。")
    finally:
        out.close()


# 向后兼容单文件调用。
def convert_pdf(
    input_pdf: str,
    output_pdf: str,
    options: ConvertOptions,
    progress: Callable[[str], None] | None = None,
    progress_value: Callable[[float, str], None] | None = None,
) -> None:
    convert_pdfs([input_pdf], output_pdf, options, progress, progress_value)


# =========================
# GUI
# =========================
class ConverterApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PDF 笔记打印转换器")
        self.root.geometry("900x650")

        self.queue: queue.Queue[Tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.output_var = tk.StringVar()
        self.render_dpi_var = tk.StringVar(value="600")
        self.output_dpi_var = tk.StringVar(value="600")
        self.margin_var = tk.StringVar(value="24")
        self.crop_var = tk.BooleanVar(value=True)
        self.worker_count_var = tk.StringVar(value="0")
        self.opencv_threads_var = tk.StringVar(value="1")
        self.gpu_var = tk.BooleanVar(value=False)
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="等待开始")

        self._build_ui()
        self.root.after(100, self._poll_queue)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True, padx=12, pady=12)

        # 输入文件列表
        lf = ttk.LabelFrame(main, text="输入 PDF（顺序即拼接顺序）")
        lf.pack(fill="x")

        list_wrap = ttk.Frame(lf)
        list_wrap.pack(fill="x", padx=10, pady=10)

        self.listbox = tk.Listbox(list_wrap, height=8, selectmode=tk.EXTENDED)
        self.listbox.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(list_wrap)
        btns.pack(side="left", padx=(10, 0), fill="y")
        ttk.Button(btns, text="添加…", command=self.choose_inputs).pack(fill="x", pady=2)
        ttk.Button(btns, text="删除所选", command=self.remove_selected).pack(fill="x", pady=2)
        ttk.Button(btns, text="上移", command=self.move_up).pack(fill="x", pady=2)
        ttk.Button(btns, text="下移", command=self.move_down).pack(fill="x", pady=2)
        ttk.Button(btns, text="清空", command=self.clear_inputs).pack(fill="x", pady=2)

        # 输出文件
        out_frame = ttk.LabelFrame(main, text="输出 PDF")
        out_frame.pack(fill="x", pady=(10, 0))
        ttk.Entry(out_frame, textvariable=self.output_var).pack(side="left", fill="x", expand=True, padx=10, pady=10)
        ttk.Button(out_frame, text="选择…", command=self.choose_output).pack(side="left", padx=(0, 10), pady=10)

        # 参数
        opt = ttk.LabelFrame(main, text="参数")
        opt.pack(fill="x", pady=(10, 0))

        row = ttk.Frame(opt)
        row.pack(fill="x", padx=10, pady=10)

        ttk.Label(row, text="渲染 DPI").grid(row=0, column=0, sticky="w")
        ttk.Entry(row, width=8, textvariable=self.render_dpi_var).grid(row=0, column=1, padx=(6, 16))

        ttk.Label(row, text="输出 DPI").grid(row=0, column=2, sticky="w")
        ttk.Entry(row, width=8, textvariable=self.output_dpi_var).grid(row=0, column=3, padx=(6, 16))

        ttk.Label(row, text="页边距(mm)").grid(row=0, column=4, sticky="w")
        ttk.Entry(row, width=8, textvariable=self.margin_var).grid(row=0, column=5, padx=(6, 16))

        ttk.Checkbutton(row, text="裁掉外围空白", variable=self.crop_var).grid(row=0, column=6, sticky="w")

        ttk.Label(row, text="并行页数").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(row, width=8, textvariable=self.worker_count_var).grid(row=1, column=1, padx=(6, 16), pady=(8, 0))

        ttk.Label(row, text="OpenCV线程").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(row, width=8, textvariable=self.opencv_threads_var).grid(
            row=1,
            column=3,
            padx=(6, 16),
            pady=(8, 0),
        )

        ttk.Checkbutton(row, text="尝试 GPU/OpenCL 加速", variable=self.gpu_var).grid(
            row=1,
            column=4,
            columnspan=3,
            sticky="w",
            pady=(8, 0),
        )

        tip = ttk.Label(
            opt,
            text="建议：普通手写笔记可先用 渲染 DPI=220、输出 DPI=300、页边距=24mm；并行页数=0 表示自动。",
            foreground="#555555",
        )
        tip.pack(anchor="w", padx=10, pady=(0, 10))

        # 进度
        progress_frame = ttk.LabelFrame(main, text="实时进度")
        progress_frame.pack(fill="x", pady=(10, 0))
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            variable=self.progress_var,
            maximum=100.0,
            mode="determinate",
        )
        self.progress_bar.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(progress_frame, textvariable=self.progress_text_var).pack(anchor="w", padx=10, pady=(0, 10))

        # 操作按钮
        action = ttk.Frame(main)
        action.pack(fill="x", pady=(10, 0))
        self.start_btn = ttk.Button(action, text="开始转换", command=self.start_convert)
        self.start_btn.pack(side="left")

        # 日志
        logf = ttk.LabelFrame(main, text="日志")
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.log_text = tk.Text(logf, height=16, wrap="word")
        self.log_text.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        logsb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
        logsb.pack(side="left", fill="y", pady=10)
        self.log_text.configure(yscrollcommand=logsb.set)

    def _get_inputs(self) -> List[str]:
        return [self.listbox.get(i) for i in range(self.listbox.size())]

    def choose_inputs(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择一个或多个 PDF",
            filetypes=[("PDF 文件", "*.pdf")],
        )
        if not files:
            return
        existing = self._get_inputs()
        for f in files:
            if f not in existing:
                self.listbox.insert("end", f)
                existing.append(f)
        if not self.output_var.get().strip():
            first = Path(self.listbox.get(0))
            default = first.with_name(f"{first.stem}_printable_merged.pdf")
            self.output_var.set(str(default))

    def remove_selected(self) -> None:
        sel = list(self.listbox.curselection())
        for idx in reversed(sel):
            self.listbox.delete(idx)

    def move_up(self) -> None:
        sel = list(self.listbox.curselection())
        if not sel or sel[0] == 0:
            return
        for idx in sel:
            text = self.listbox.get(idx)
            self.listbox.delete(idx)
            self.listbox.insert(idx - 1, text)
        for idx in [i - 1 for i in sel]:
            self.listbox.selection_set(idx)

    def move_down(self) -> None:
        sel = list(self.listbox.curselection())
        if not sel or sel[-1] >= self.listbox.size() - 1:
            return
        for idx in reversed(sel):
            text = self.listbox.get(idx)
            self.listbox.delete(idx)
            self.listbox.insert(idx + 1, text)
        for idx in [i + 1 for i in sel]:
            self.listbox.selection_set(idx)

    def clear_inputs(self) -> None:
        self.listbox.delete(0, "end")

    def choose_output(self) -> None:
        initial = self.output_var.get().strip() or "printable_output.pdf"
        f = filedialog.asksaveasfilename(
            title="选择输出 PDF",
            defaultextension=".pdf",
            initialfile=os.path.basename(initial),
            filetypes=[("PDF 文件", "*.pdf")],
        )
        if f:
            self.output_var.set(f)

    def log(self, text: str) -> None:
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")

    def set_progress(self, fraction: float, text: str) -> None:
        percent = min(100.0, max(0.0, fraction * 100.0))
        self.progress_var.set(percent)
        self.progress_text_var.set(f"{percent:5.1f}%  {text}")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.start_btn.configure(state=state)

    def _read_options(self) -> ConvertOptions:
        try:
            render_dpi = int(self.render_dpi_var.get().strip())
            output_dpi = int(self.output_dpi_var.get().strip())
            margin_mm = float(self.margin_var.get().strip())
            worker_count = int(self.worker_count_var.get().strip())
            opencv_threads = int(self.opencv_threads_var.get().strip())
        except ValueError as exc:
            raise ValueError("参数格式不正确，请检查 DPI、页边距和并行参数") from exc

        if render_dpi < 72 or output_dpi < 72:
            raise ValueError("DPI 不能小于 72")
        if margin_mm < 0:
            raise ValueError("页边距不能为负数")
        if worker_count < 0:
            raise ValueError("并行页数不能为负数；填 0 表示自动")
        if opencv_threads < 1:
            raise ValueError("OpenCV线程不能小于 1")

        return ConvertOptions(
            render_dpi=render_dpi,
            output_dpi=output_dpi,
            margin_mm=margin_mm,
            crop_whitespace=bool(self.crop_var.get()),
            worker_count=worker_count,
            opencv_threads=opencv_threads,
            use_gpu_acceleration=bool(self.gpu_var.get()),
        )

    def start_convert(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "已有任务正在运行")
            return

        input_pdfs = self._get_inputs()
        if not input_pdfs:
            messagebox.showerror("错误", "请至少选择一个输入 PDF")
            return

        output_pdf = self.output_var.get().strip()
        if not output_pdf:
            messagebox.showerror("错误", "请选择输出 PDF")
            return

        try:
            options = self._read_options()
        except Exception as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.log_text.delete("1.0", "end")
        self.log("开始转换，输入文件顺序如下：")
        for i, p in enumerate(input_pdfs, start=1):
            self.log(f"  {i}. {p}")
        self.log(f"输出文件：{output_pdf}")
        self.set_progress(0.0, "准备开始")

        def worker() -> None:
            try:
                convert_pdfs(
                    input_pdfs=input_pdfs,
                    output_pdf=output_pdf,
                    options=options,
                    progress=lambda msg: self.queue.put(("log", msg)),
                    progress_value=lambda fraction, msg: self.queue.put(("progress", (fraction, msg))),
                )
                self.queue.put(("done", output_pdf))
            except Exception:
                self.queue.put(("error", traceback.format_exc()))

        self._set_busy(True)
        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _poll_queue(self) -> None:
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self.log(str(payload))
            elif kind == "progress":
                fraction, text = payload  # type: ignore[misc]
                self.set_progress(float(fraction), str(text))
            elif kind == "done":
                self._set_busy(False)
                self.set_progress(1.0, "转换完成")
                self.log("转换完成。")
                messagebox.showinfo("完成", f"输出已保存到：\n{payload}")
            elif kind == "error":
                self._set_busy(False)
                self.progress_text_var.set("转换失败")
                self.log(str(payload))
                messagebox.showerror("转换失败", str(payload))

        self.root.after(100, self._poll_queue)


def main() -> None:
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    app = ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
