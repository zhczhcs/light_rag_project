"""
控制台表格输出工具
提供对齐、美观的表格打印，替代散乱的纯文本日志

核心改进：按终端显示宽度（而非字符数）处理中文/Emoji，解决对齐错位问题
"""

import unicodedata


def _char_width(ch: str) -> int:
    """计算单个字符的终端显示宽度（CJK/Emoji/全角=2，其余=1）"""
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("F", "W"):       # Fullwidth / Wide
        return 2
    return 1


def _str_width(s: str) -> int:
    """计算字符串的终端显示宽度"""
    return sum(_char_width(ch) for ch in s)


def _truncate(s: str, width: int, ellipsis: str = "...") -> str:
    """按显示宽度截断字符串，超长时末尾加省略号"""
    if _str_width(s) <= width:
        return s
    ew = _str_width(ellipsis)
    target = width - ew
    result = []
    cur = 0
    for ch in s:
        cw = _char_width(ch)
        if cur + cw > target:
            break
        result.append(ch)
        cur += cw
    return "".join(result) + ellipsis


def _pad_right(s: str, width: int) -> str:
    """右填充空格到指定显示宽度"""
    sw = _str_width(s)
    if sw >= width:
        return _truncate(s, width)
    return s + " " * (width - sw)


def _pad_center(s: str, width: int) -> str:
    """居中到指定显示宽度"""
    sw = _str_width(s)
    if sw >= width:
        return _truncate(s, width)
    left = (width - sw) // 2
    right = width - sw - left
    return " " * left + s + " " * right


class TablePrinter:
    """
    简单表格打印机，用 Unicode 框线字符画对齐表格（正确处理中文宽度）。

    用法:
        table = TablePrinter(["字段", "值"], col_widths=[16, 50])
        table.add_row(["模型", "qwen3.6-plus"])
        table.add_row(["耗时", "12.34s"])
        table.print()
    """

    def __init__(self, headers: list[str], col_widths: list[int],
                 title: str = None, align: str = "left", prefix_newline: bool = False):
        """
        Args:
            headers: 表头列表
            col_widths: 每列的**显示宽度**（注意是终端占据的列数，不是字符数）
            title: 表格标题（可选，会打印在表格上方）
            align: 默认对齐方式 "left" / "right" / "center"
            prefix_newline: 是否在表格前加空行（用于 Celery Worker 日志，
                           让 WARNING 前缀落在空行上，表格内容从新行开始）
        """
        self.headers = headers
        # 全局增加 3 的余量，避免截断省略号导致右侧框线错位
        self.col_widths = [w + 3 for w in col_widths]
        self.title = title
        self.align = align
        self.prefix_newline = prefix_newline
        self.rows: list[list[str]] = []

        self.h_line = "─"
        self.v_line = "│"
        self.cross_top = "┬"
        self.cross_mid = "┼"
        self.cross_bot = "┴"
        self.corner_tl = "┌"
        self.corner_tr = "┐"
        self.corner_bl = "└"
        self.corner_br = "┘"

    def add_row(self, row: list[str]):
        """添加一行数据"""
        if len(row) != len(self.headers):
            raise ValueError(f"行数据列数 {len(row)} 与表头列数 {len(self.headers)} 不一致")
        self.rows.append([str(cell) for cell in row])

    def _pad(self, text: str, width: int, align: str = None) -> str:
        """按指定显示宽度裁剪/填充文本"""
        align = align or self.align
        if align == "right":
            # 右对齐：前面补空格
            sw = _str_width(text)
            if sw >= width:
                return _truncate(text, width)
            return " " * (width - sw) + text
        elif align == "center":
            return _pad_center(text, width)
        else:
            return _pad_right(text, width)

    def _make_horizontal(self, left: str, mid: str, right: str) -> str:
        """生成水平分隔线（col_widths 是显示宽度，每条线 = 宽度 + 2 个空格padding）"""
        parts = [self.h_line * (w + 2) for w in self.col_widths]
        return left + mid.join(parts) + right

    def _make_row(self, cells: list[str]) -> str:
        """生成一行内容"""
        parts = []
        for cell, width in zip(cells, self.col_widths):
            parts.append(f" {self._pad(cell, width)} ")
        return self.v_line + self.v_line.join(parts) + self.v_line

    def build(self) -> str:
        """构建完整表格字符串"""
        lines = []

        # 标题行（显示宽度居中）
        if self.title:
            total_inner = sum(self.col_widths) + 3 * len(self.col_widths) - 1
            lines.append(self._make_horizontal("┌", "┬", "┐"))
            lines.append(f"│{_pad_center(self.title, total_inner)}│")
            lines.append(self._make_horizontal("├", "┼", "┤"))
        else:
            lines.append(self._make_horizontal(self.corner_tl, self.cross_top, self.corner_tr))

        # 表头
        lines.append(self._make_row(self.headers))
        lines.append(self._make_horizontal("├", self.cross_mid, "┤"))

        # 数据行
        for row in self.rows:
            lines.append(self._make_row(row))

        # 底边
        lines.append(self._make_horizontal(self.corner_bl, self.cross_bot, self.corner_br))

        result = "\n".join(lines)
        if self.prefix_newline:
            result = "\n" + result
        return result

    def print(self):
        """直接打印到控制台"""
        print(self.build())


def print_kv_table(title: str, data: dict, key_width: int = 16, val_width: int = 50,
                   prefix_newline: bool = False):
    """
    快速打印键值对表格。

    Args:
        title: 表格标题
        data: {key: value} 字典
        key_width: 键列**显示宽度**（中文占2，英文占1）
        val_width: 值列**显示宽度**
        prefix_newline: 表格前加空行（Celery Worker 用）
    """
    table = TablePrinter(["字段", "值"], [key_width, val_width], title=title,
                         prefix_newline=prefix_newline)
    for k, v in data.items():
        table.add_row([str(k), str(v)])
    table.print()


def print_simple_table(title: str, headers: list[str], rows: list[list],
                       col_widths: list[int] = None, prefix_newline: bool = False):
    """
    快速打印通用表格。

    Args:
        title: 表格标题
        headers: 表头
        rows: 数据行（元素会自动转 str）
        col_widths: 每列显示宽度，默认自动计算
    """
    if col_widths is None:
        col_widths = []
        for i in range(len(headers)):
            max_w = _str_width(headers[i])
            for row in rows:
                if i < len(row):
                    max_w = max(max_w, _str_width(str(row[i])))
            col_widths.append(max(max_w + 2, 12))

    table = TablePrinter(headers, col_widths, title=title, prefix_newline=prefix_newline)
    for row in rows:
        table.add_row([str(c) for c in row])
    table.print()
